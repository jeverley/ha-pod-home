"""DataUpdateCoordinator for the Pod Home integration."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from podpoint_mobile_api import PodHomeApiClient, PodHomeApiError, PodHomeAuthError
from .const import CHARGING_STATE_OPTIONS, DOMAIN

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

_LOGGER = logging.getLogger(__name__)

# How far back to look for "the most recent charge" each poll. /charges needs a date range
# (see the findings doc) and doesn't come back pre-sorted, so this needs to be wide enough to
# reliably include the last session even across a quiet weekend, but not so wide it's a heavy
# call every scan interval.
RECENT_CHARGES_LOOKBACK = datetime.timedelta(days=14)

# --- Adaptive polling ---
# Measured live against a real account: the charger checks in with Pod Point's cloud (i.e.
# connectivity-status-v2's lastSeenAt actually changes) roughly every 300s on a quiet baseline,
# PLUS extra out-of-band check-ins triggered by physical events (plug/unplug, state changes) -
# noisy enough (observed gaps: 18s-283s alongside the clean 300s baseline) that predicting an
# exact next-check-in timestamp isn't reliable. Backing off based on time-since-last-observed-
# change is simpler and self-correcting: poll fast right after any change (catches event
# clusters like plug-in -> charge-start), drift slow once things have been quiet for a while.
#
# Also confirmed live: the cloud can't push to the charger at all - a charge-override issued
# via the app produced no lastSeenAt reaction until the charger's own next check-in. Commands
# are pull-based (the charger fetches pending actions when IT calls home), so once charge-now/
# remote-lock exist, they inherit this same up-to-~5-minute latency regardless of how fast we
# poll - faster polling only helps us SEE state sooner, it can't make the charger act sooner.
#
# Request-volume matters here, not just responsiveness (see appropriate-polling in
# QUALITY_SCALE.md) - a first draft of these constants (30s/150s) would have made the IDLE
# baseline poll faster than the legacy integration's proven-acceptable 300s default for zero
# benefit (nothing's changing while idle), and pushed a sustained multi-hour charging session
# to ~10x the old request volume. SLOW_POLL_INTERVAL is deliberately kept at the same 300s the
# legacy integration has run at for real users, so idle-time load doesn't increase at all - the
# adaptive part only kicks in (and only costs more) during a bounded window of actual activity.
FAST_POLL_INTERVAL = datetime.timedelta(seconds=60)
SLOW_POLL_INTERVAL = datetime.timedelta(seconds=300)  # matches the legacy integration's default
RECENT_CHANGE_WINDOW = datetime.timedelta(seconds=360)  # generous vs. the noisiest gap seen
# (283s) so one quiet gap mid-session doesn't flap back to slow and miss the next check-in


@dataclass
class PodHomeCharge:
    """One entry from /charges."""

    id: str
    started_at: datetime.datetime | None
    ended_at: datetime.datetime | None
    duration: int | None
    energy_total: float | None
    cost_amount: int | None
    cost_currency: str | None
    plugged_in_at: datetime.datetime | None
    unplugged_at: datetime.datetime | None

    @property
    def cable_connected(self) -> bool:
        """True if this (the most recent) charge session's cable hasn't been unplugged yet.

        HEURISTIC, not a field the API gives us directly - see the findings doc. Untested
        against a live "currently plugged in" session; verify the first time this matters.
        """
        return self.plugged_in_at is not None and self.unplugged_at is None


@dataclass
class PodHomeCharger:
    """Aggregated view of one charger, built from several endpoint responses each poll."""

    ppid: str
    unit_id: int | None
    timezone: str | None
    model_style: str | None
    model_colour: str | None
    architecture: str | None
    connection_state: str | None
    charging_state: str | None
    connection_quality: int | None
    last_seen_at: datetime.datetime | None
    latest_charge: PodHomeCharge | None
    # Month-to-date figures for the Energy Dashboard sensors - see PodHomeEnergyMonthSensor/
    # PodHomeCostMonthSensor in sensor.py. Minor units (e.g. pence for GBP) for cost, matching
    # cost_amount elsewhere - divided down in the sensor, not here.
    month_energy_kwh: float | None
    month_cost_amount: int | None


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _LOGGER.debug("Couldn't parse timestamp %r", value)
        return None


def _resolve_timezone(timezone_name: str | None) -> ZoneInfo | None:
    """Resolve a charger's reported timezone, falling back to HA's configured default (via
    dt_util.now(None)) if missing or invalid - correctness matters here, not just convenience:
    computing "today"/"this month" in UTC would misattribute the first hour of each local day
    during BST, corrupting the month-to-date sensors' totals right after a reset.
    """
    if not timezone_name:
        return None
    try:
        return ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001 - genuinely any bad tz string should just fall back
        _LOGGER.debug("Unrecognized timezone %r, falling back to HA's default", timezone_name)
        return None


class PodHomeDataUpdateCoordinator(DataUpdateCoordinator[dict[str, PodHomeCharger]]):
    """Polls mobile-api.pod-point.com and builds a PodHomeCharger per ppid."""

    def __init__(
        self, hass: HomeAssistant, config_entry: "PodHomeConfigEntry", api: PodHomeApiClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            # Overwritten after every successful poll based on observed activity - see
            # _async_adjust_poll_interval. Start fast (rather than guessing slow) so the very
            # first few polls establish a real baseline quickly.
            update_interval=FAST_POLL_INTERVAL,
        )
        self.api = api
        # Account-level billing currency, fetched once (not every poll - it essentially never
        # changes) the first time _async_update_data succeeds. None until then.
        self.currency: str | None = None
        self._warned_keys: set[str] = set()
        # Wall-clock time WE observed each charger's lastSeenAt last actually change - not the
        # lastSeenAt value itself, which is the charger's clock, not ours. Drives adaptive
        # polling (see FAST_POLL_INTERVAL/SLOW_POLL_INTERVAL above).
        self._last_seen_changed_at: dict[str, datetime.datetime] = {}

    # --- non-fatal-error logging: warn once per distinct failure, drop to debug on repeats,
    # log once at info on recovery. Used for every "this call failing shouldn't take the whole
    # integration down" case below, and for the unknown-chargingState-value case in B1 -
    # without this, a persistently-failing call would log a fresh WARNING every 5 minutes
    # forever, and HA core logs an ERROR on every state read for an out-of-range ENUM sensor. ---

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned_keys:
            _LOGGER.debug(message)
            return
        _LOGGER.warning(message)
        self._warned_keys.add(key)

    def _clear_warning(self, key: str) -> None:
        if key in self._warned_keys:
            _LOGGER.info("Recovered: %s", key)
            self._warned_keys.discard(key)

    async def _safe_call(self, key: str, message: str, coro) -> dict:
        """Await `coro`; on PodHomeApiError, log (deduped) and return {} instead of raising -
        for calls where a failure shouldn't take the whole coordinator update down.

        Note this deliberately does NOT catch PodHomeAuthError - that's left to propagate up
        to _async_update_data's outer handler so an auth failure anywhere (not just on the
        first call) converts to ConfigEntryAuthFailed and triggers HA's reauth flow, instead
        of being swallowed as if it were just a non-fatal API error.
        """
        try:
            result = await coro
            self._clear_warning(key)
            # A successful call can still return None (e.g. a 2xx with an empty body - seen
            # for real on /chargers/arch5/{ppid}); never hand that back as if it were a dict,
            # every caller here does unguarded .get() on the result.
            return result or {}
        except PodHomeApiError as exc:
            self._warn_once(key, f"{message}: {exc}")
            return {}

    async def _async_fetch_currency(self) -> str | None:
        """Returns the account's billing currency, or None if it couldn't be fetched this
        poll. Deliberately does NOT fall back to a hardcoded default here - the caller retries
        every poll while this stays None, rather than permanently locking in a guess after one
        transient failure (Pod Point serves both GBP and EUR accounts, so a wrong-but-plausible
        default is worse than briefly showing no unit at all).
        """
        try:
            users = await self.api.async_get_users()
            currency = ((users or {}).get("balance") or {}).get("currency")
            if currency:
                return currency
        except PodHomeApiError as exc:
            self._warn_once("currency", f"Couldn't fetch account currency (will retry): {exc}")
        return None

    async def _async_update_data(self) -> dict[str, PodHomeCharger]:
        try:
            return await self._async_fetch_data()
        except PodHomeAuthError as exc:
            # Catches an auth failure raised from ANY call below, not just the initial
            # chargers fetch - _safe_call/_async_fetch_currency deliberately let
            # PodHomeAuthError propagate rather than swallowing it as a non-fatal error,
            # specifically so it reaches HA's reauth flow instead of just being logged.
            raise ConfigEntryAuthFailed(str(exc)) from exc

    async def _async_fetch_data(self) -> dict[str, PodHomeCharger]:
        try:
            chargers_raw = await self.api.async_list_chargers()
        except PodHomeApiError as exc:
            raise UpdateFailed(str(exc)) from exc

        if self.currency is None:
            self.currency = await self._async_fetch_currency()

        if not chargers_raw:
            if self.data:
                # Could be a transient glitch rather than "this account genuinely has zero
                # chargers now" - keep the last known good state rather than wiping every
                # entity to unavailable over one odd empty response.
                self._warn_once(
                    "empty_chargers",
                    "GET /chargers returned no chargers this poll; keeping previous data",
                )
                return self.data
            return {}
        self._clear_warning("empty_chargers")

        today_utc = dt_util.now(datetime.timezone.utc).date()
        lookback_start = today_utc - RECENT_CHARGES_LOOKBACK

        charges_raw = await self._safe_call(
            "charges", "Couldn't fetch recent charges (non-fatal)",
            self.api.async_charges(lookback_start, today_utc),
        )
        latest_charge_by_ppid = self._latest_charge_per_ppid(charges_raw)

        result: dict[str, PodHomeCharger] = {}
        for raw in chargers_raw:
            ppid = raw.get("ppid")
            if not ppid:
                _LOGGER.warning("Skipping a /chargers entry with no ppid: %r", raw)
                continue

            model_info = raw.get("modelInfo") or {}
            timezone_name = raw.get("timezone")
            tz = _resolve_timezone(timezone_name)
            today_local = dt_util.now(tz).date()
            month_start_local = today_local.replace(day=1)

            status, month_stats = await asyncio.gather(
                self._safe_call(
                    f"connectivity:{ppid}",
                    f"Couldn't fetch connectivity status for {ppid} (non-fatal)",
                    self.api.async_connectivity_status(ppid),
                ),
                self._safe_call(
                    f"charge_stats:{ppid}",
                    f"Couldn't fetch month-to-date charge statistics for {ppid} (non-fatal)",
                    self.api.async_charge_statistics(ppid, month_start_local, today_local),
                ),
            )

            charging_state = status.get("chargingState")
            if charging_state and charging_state not in CHARGING_STATE_OPTIONS:
                self._warn_once(
                    f"unknown_charging_state:{charging_state}",
                    f"Unrecognized chargingState {charging_state!r} for {ppid} - this is a "
                    "real API value we haven't seen before, worth reporting",
                )

            month_energy = (month_stats.get("energy") or {}).get("totalUsage")
            month_cost = (month_stats.get("energy") or {}).get("cost")

            last_seen_at = _parse_dt(status.get("lastSeenAt"))
            previous = self.data.get(ppid) if self.data else None
            if (
                ppid not in self._last_seen_changed_at
                or last_seen_at != (previous.last_seen_at if previous else None)
            ):
                # First time seeing this charger, or its lastSeenAt genuinely moved since last
                # poll - either way, treat "now" as an activity signal for the adaptive
                # interval below.
                self._last_seen_changed_at[ppid] = dt_util.utcnow()

            latest_charge = latest_charge_by_ppid.get(ppid)
            if latest_charge is None and self.data and ppid in self.data:
                # This poll's lookback window (RECENT_CHARGES_LOOKBACK) found no session for
                # this charger - that doesn't mean the last known session stopped being true,
                # just that it's now older than the window. Carry it forward rather than
                # dropping Last Charge Duration/Energy/Cost and Cable Status to unknown for
                # any charger that's simply gone unused for a while.
                latest_charge = self.data[ppid].latest_charge

            result[ppid] = PodHomeCharger(
                ppid=ppid,
                unit_id=raw.get("unitId"),
                timezone=timezone_name,
                model_style=model_info.get("style"),
                model_colour=model_info.get("colour"),
                architecture=model_info.get("architecture"),
                connection_state=status.get("connectionState"),
                charging_state=charging_state,
                connection_quality=status.get("connectionQuality"),
                last_seen_at=last_seen_at,
                latest_charge=latest_charge,
                month_energy_kwh=month_energy,
                month_cost_amount=month_cost,
            )

        self._async_adjust_poll_interval()
        return result

    def _async_adjust_poll_interval(self) -> None:
        """Speed up or slow down future polls based on how recently any charger's lastSeenAt
        actually changed - see the FAST_POLL_INTERVAL/SLOW_POLL_INTERVAL block above for why
        this backs off from an observation rather than a predicted check-in timestamp.
        Reassigning self.update_interval here takes effect starting from the NEXT scheduled
        poll, same mechanism used by other HA integrations for dynamic polling cadence.
        """
        now = dt_util.utcnow()
        recent = any(
            now - changed_at <= RECENT_CHANGE_WINDOW
            for changed_at in self._last_seen_changed_at.values()
        )
        new_interval = FAST_POLL_INTERVAL if recent else SLOW_POLL_INTERVAL
        if new_interval != self.update_interval:
            _LOGGER.debug(
                "Switching poll interval to %s (%s activity in the last %s)",
                new_interval,
                "recent" if recent else "no recent",
                RECENT_CHANGE_WINDOW,
            )
            self.update_interval = new_interval

    @staticmethod
    def _latest_charge_per_ppid(charges_raw: dict) -> dict[str, PodHomeCharge]:
        entries = ((charges_raw or {}).get("data") or {}).get("charges") or []

        latest: dict[str, PodHomeCharge] = {}
        latest_started: dict[str, datetime.datetime] = {}

        for entry in entries:
            charger = entry.get("charger") or {}
            ppid = charger.get("id")
            if not ppid:
                continue

            started_at = _parse_dt(entry.get("startedAt"))
            if started_at is None:
                continue

            if ppid in latest_started and started_at <= latest_started[ppid]:
                continue

            cost = entry.get("cost") or {}
            latest_started[ppid] = started_at
            latest[ppid] = PodHomeCharge(
                id=entry.get("id"),
                started_at=started_at,
                ended_at=_parse_dt(entry.get("endedAt")),
                duration=entry.get("duration"),
                energy_total=entry.get("energyTotal"),
                cost_amount=cost.get("amount"),
                cost_currency=cost.get("currency"),
                plugged_in_at=_parse_dt(charger.get("pluggedInAt")),
                unplugged_at=_parse_dt(charger.get("unpluggedAt")),
            )

        return latest
