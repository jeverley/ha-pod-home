"""DataUpdateCoordinator for the Pod Home integration."""
from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

# TEMPORARY: vendored copy, see __init__.py's import comment.
from .podpoint_mobile_api import PodHomeApiClient, PodHomeApiError, PodHomeAuthError
from .const import (
    CHARGING_STATE_CABLE_CONNECTED,
    CHARGING_STATE_CHARGING,
    CHARGING_STATE_OPTIONS,
    CHARGING_STATE_SUSPENDED_EV,
    DELEGATED_CONTROL_ACTIVE,
    DELEGATED_CONTROL_OPTIONS,
    DOMAIN,
)
from .helpers import (
    cumulative_charging_seconds,
    is_momentarily_unplugged,
    resolve_timezone,
)

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

_LOGGER = logging.getLogger(__name__)

# How far back to look for "the most recent charge" each poll.
RECENT_CHARGES_LOOKBACK = datetime.timedelta(days=14)

# Adaptive polling constants.
FAST_POLL_INTERVAL = datetime.timedelta(seconds=60)
SLOW_POLL_INTERVAL = datetime.timedelta(seconds=300)
RECENT_CHANGE_WINDOW = datetime.timedelta(seconds=360)

# Firmware/tariffs rarely change; re-checked on this cadence rather than every poll or never.
FIRMWARE_TARIFF_REFRESH_INTERVAL = datetime.timedelta(hours=6)

# Month-to-date charge-statistics and the most-recent-charge lookup: fetched every poll while a
# charge is active (see the "was charging last poll" checks below), otherwise on this slower
# cadence as a fallback for non-session-driven changes (e.g. a billing correction).
CHARGE_STATS_REFRESH_INTERVAL = datetime.timedelta(minutes=30)

# api3's session/pod-id mapping (see async_create_api3_session()'s docstring) - account-level,
# rarely changes, same cadence as firmware/tariffs above.
API3_ACCOUNT_REFRESH_INTERVAL = datetime.timedelta(hours=6)

# Linked-vehicle data (battery/range/odometer/charge-rate/etc, from
# async_smart_charging_chargers_and_vehicles) refresh cadence: every poll while a vehicle is
# actively charging, CHARGE_STATS_REFRESH_INTERVAL while plugged in but not charging, this
# faster interval while fully unplugged (the one case the vehicle itself might be moving). Tier
# is decided from the PREVIOUS poll's charger-side cable-connected state, not the vehicle's own
# is_plugged_in_to_this_charger flag - see the fetch site's comment in _async_fetch_data.
VEHICLE_REFRESH_INTERVAL = datetime.timedelta(minutes=5)

# A connection-level failure (couldn't reach mobile-api.pod-point.com at all - PodHomeApiError
# with status 0) on GET /chargers below, the one call whose failure is fatal to the whole poll,
# gets a few quick retries before giving up. Lives here, not in podpoint_mobile_api's client -
# that package is also used by the scratch/ probe scripts, where retry-vs-fail-fast is a caller
# preference, not a client property. NOT retried: a genuine HTTP error response (4xx/5xx) or
# PodHomeAuthError, a different exception type this doesn't catch at all.
CONNECTION_RETRY_ATTEMPTS = 3
CONNECTION_RETRY_DELAY_SECONDS = 2

_NEVER_FETCHED = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

# Status's sticky timestamps persist across HA restarts via HA's own Store helper - they
# change every poll (up to once a minute), so a delayed/coalesced save rather than one per poll.
STICKY_STATE_STORAGE_VERSION = 1
STICKY_STATE_SAVE_DELAY = 10  # seconds


def _safe_dict(value) -> dict:
    """Coerce a JSON value to a dict, discarding anything else. Guards nested .get() chains on
    response data that isn't guaranteed to match the expected shape at every level."""
    return value if isinstance(value, dict) else {}


@dataclass
class PodHomeCharge:
    """One charge session - a finished entry from mobile-api's /charges (latest_charge), or the
    live in-progress one from api3's charges endpoint (current_charge). ended_at is None on a
    current_charge; duration/energy_total/cost_amount are computed/read live rather than
    finalized.

    current_charge.duration starts as a naive "time since plug-in" default (set in
    _async_refresh_api3_charges), then refined to actual cumulative charging time via
    cumulative_charging_seconds() (helpers.py) whenever a Smart Charging schedule is available
    for this poll; stays the naive default in Basic Charging mode."""

    id: str
    started_at: datetime.datetime | None
    ended_at: datetime.datetime | None
    duration: int | None
    energy_total: float | None
    cost_amount: int | None
    cost_currency: str | None
    plugged_in_at: datetime.datetime | None
    unplugged_at: datetime.datetime | None


@dataclass
class PodHomeTariffWindow:
    """One recurring price window from /chargers/{ppid}/tariffs. `price` is whole currency
    units per kWh (e.g. 0.0863 = 8.63p). `end` can be earlier than `start` (wraps past
    midnight)."""

    days: list[str]
    start: str
    end: str
    price: float | None


@dataclass
class PodHomeRewards:
    """Account-wide rewards balance, from GET /reward-wallet - no per-charger dimension. GBP-
    denominated regardless of the account's own billing currency (a UK-specific rewards scheme
    with its own fixed unit). balance_miles/balance_points are the same balance in other units,
    kept as sensor attributes rather than separate entities. allowance_balance_gbp/
    annual_allowance_gbp are an annual earnings cap, distinct from the balance itself;
    payout_threshold_gbp is the minimum balance needed before a payout can be requested."""

    balance_gbp: float | None
    balance_miles: float | None
    balance_points: int | None
    allowance_balance_gbp: float | None
    annual_allowance_gbp: float | None
    payout_threshold_gbp: float | None


@dataclass
class PodHomeFirmware:
    """From /chargers/{ppid}/firmware - a ppid-addressed replacement for the legacy
    /api3/v5/units/{unitId}/firmware path."""

    manifest_id: str | None
    update_available: bool | None
    serial_number: str | None


@dataclass
class PodHomeManualScheduleWindow:
    """One entry from /chargers/{ppid}/manual-schedules - a fixed, recurring charge window
    independent of Smart Charging. start_day/end_day are ISO day-of-week integers (1=Monday,
    7=Sunday); a window can in principle span two days (not observed live)."""

    uid: str | None
    start_day: int | None
    start_time: str | None
    end_day: int | None
    end_time: str | None
    is_active: bool | None


@dataclass
class PodHomeSmartScheduleWindow:
    """One entry from /chargers/{ppid}/smart-schedules/active - Smart Charging's concrete plan
    for the current plugged-in session. type is PLUGGED_IN (a point-in-time marker: only
    `timestamp` is set) or PAUSED/CHARGING (a window: `from_timestamp`/`to_timestamp`,
    `tariff_rate` only meaningful for CHARGING)."""

    type: str | None
    timestamp: datetime.datetime | None
    from_timestamp: datetime.datetime | None
    to_timestamp: datetime.datetime | None
    tariff_rate: str | None


@dataclass
class PodHomeVehicle:
    """The primary vehicle currently linked (via Enode) to a charger, from
    /smart-charging/delegated-controls/vehicles. Persists across plug/unplug -
    is_plugged_in_to_this_charger is the only field that reflects that."""

    id: str
    display_name: str | None
    brand: str | None
    model: str | None
    # Not currently used to compute anything - kept as captured data in case it's useful later.
    battery_capacity_kwh: float | None
    battery_level_percent: int | None
    range_km: float | None
    is_charging: bool | None
    odometer_km: float | None
    ready_by: datetime.datetime | None
    is_plugged_in_to_this_charger: bool | None
    # Target charge level and who/what set it. charge_limit_source's confirmed values: "vehicle",
    # "user", "default". Only "default" additionally seen live.
    charge_limit_percent: int | None
    charge_limit_source: str | None
    # Smart Charging's live prediction for the current ready_by target, given constraints like
    # Charge Priority - distinct from charge_limit_percent (what was asked for).
    # cannot_meet_target_reason's confirmed values: "PRICE", "TIME". Only "PRICE" additionally
    # seen live.
    expected_charge_percent: int | None
    can_meet_target: bool | None
    cannot_meet_target_reason: str | None
    # Raw chargeState fields not otherwise surfaced - back the debug sensors. power_delivery_state
    # candidates: PLUGGED_IN:CHARGING/COMPLETE/FAULT/INITIALIZING/NO_POWER/STOPPED - only STOPPED
    # confirmed live. charge_rate/max_current/charge_time_remaining only ever observed null on
    # this account - unit/shape unconfirmed.
    power_delivery_state: str | None
    is_fully_charged: bool | None
    charge_rate: float | None
    max_current: float | None
    charge_time_remaining: int | None
    # The literal per-day Smart Charging config from intents.details[] - what the Target
    # Charge/Ready By write entities actually read and write, as opposed to
    # ready_by/charge_limit_percent above (Smart Charging's own live-resolved view of the same
    # target, which can lag a just-written change). All 7 days are confirmed live to always be
    # identical - representative day picked from whichever entry is first.
    intent_charge_by_time: str | None
    intent_charge_kwh: float | None
    # When Enode itself last synced this vehicle's chargeState, not when we last polled it -
    # confirmed live to lag the real world by a variable amount, so exposed as an attribute
    # (Battery sensor) rather than assumed near-current.
    synced_at: datetime.datetime | None


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
    delegated_control_status: str | None
    # When delegated_control_status last changed, from GET /smart-charging/delegated-controls/
    # {ppid} - the server's own record, not when pod_home itself first noticed.
    delegated_control_status_effective_from: datetime.datetime | None
    # Sticky signals backing Status's SuspendedEV/SuspendedEVSE handling (see charger_status() in
    # helpers.py) - the wall-clock time WE last observed each condition true, not a value from
    # the API itself. Whichever is most recent wins: Finished is only reported while
    # charge_finished_at is more recent than the other two, letting it survive chargingState
    # later wandering through Finishing/Preparing/SuspendedEVSE instead of reverting just because
    # the current instant no longer looks like "just finished". Persisted across HA restarts via
    # Store (see _sticky_store below) - unlike adaptive-poll tracking (_last_seen_changed_at).
    charging_started_at: datetime.datetime | None
    cable_unplugged_at: datetime.datetime | None
    charge_finished_at: datetime.datetime | None
    connection_quality: int | None
    last_seen_at: datetime.datetime | None
    latest_charge: PodHomeCharge | None
    # The live in-progress charge, if one is active right now - from api3's charges endpoint,
    # not mobile-api's own /charges (which only shows finalized sessions). Last Charge
    # duration/energy/cost sensors prefer this over latest_charge when set - see
    # PodHomeLastChargeDurationSensor's docstring in sensor.py. None when nothing is currently
    # charging, or api3 couldn't be reached this poll (non-fatal).
    current_charge: PodHomeCharge | None
    # Month-to-date figures - finalized charges only, matching what the app shows. Cost in minor
    # units (e.g. pence for GBP), matching cost_amount elsewhere - divided down in the sensor.
    month_energy_kwh: float | None
    month_cost_amount: int | None
    # Running lifetime-since-tracking-started totals - see PodHomeTotalEnergySensor's docstring
    # (sensor.py): incrementally accumulated from newly-finalized charges only, persisted via
    # Store, NOT including current_charge (the sensor adds that on top at display time).
    # total_started_at is the wall-clock time THIS ppid was first seen with no persisted total
    # yet, not the account's real charging history.
    total_energy_kwh: float | None
    total_started_at: datetime.datetime | None
    firmware: PodHomeFirmware | None
    tariff_windows: list[PodHomeTariffWindow] | None
    manual_schedule_windows: list[PodHomeManualScheduleWindow] | None
    smart_schedule_windows: list[PodHomeSmartScheduleWindow] | None
    vehicle: PodHomeVehicle | None
    # Smart Charging's account-level preference (GET/PATCH .../delegated-controls/{ppid}/
    # preferences) driving the Charge Priority select - both read and write. See
    # charging_priority_label()/max_price_for_charging_priority() in helpers.py.
    max_price: float | None
    # The active boost ("Charge Now")'s end time, if one is currently running - from
    # GET /chargers/{ppid}/charge-overrides, see _current_boost_end_at() above for how "current"
    # is decided. None when no boost is active.
    boost_end_at: datetime.datetime | None
    # Whether the charger's currently-configured tariff supports Smart Charging at all - a
    # tariff with more than two rates, or one where the supplier controls charging directly,
    # forces Basic Charging. From the same already-fetched tariffs response as tariff_windows
    # above. Not the source of truth for current mode (delegated_control_status is - see
    # schedule_mode() in helpers.py).
    smart_charging_supported: bool | None
    # Remote Lock's current state - GET /remote-lock/{ppid}, `RemoteLockDTO.offMode`. True
    # locked, False unlocked, None when unset or the charger model doesn't support Remote Lock
    # at all - see lock.py, DECISIONS.md.
    remote_lock_off_mode: bool | None


def _parse_dt(value: str | None) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp. Always returns a timezone-aware datetime (assumes UTC if
    the string has no offset), so a naive value never propagates into a comparison later."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _LOGGER.debug("Couldn't parse timestamp %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _current_boost_end_at(
    charge_overrides_raw: list, now: datetime.datetime
) -> datetime.datetime | None:
    """The active boost's end time, if any, from GET /chargers/{ppid}/charge-overrides. Entries
    are returned newest-first, but that ordering isn't trusted here; "current" is whichever
    non-deleted, not-yet-ended entry has the latest requestedAt. A deleted entry (deletedAt set)
    or one whose endAt has already passed is not a current boost."""
    best: tuple[datetime.datetime, datetime.datetime] | None = None
    for raw_entry in charge_overrides_raw:
        entry = _safe_dict(raw_entry)
        if entry.get("deletedAt") is not None:
            continue
        end_at = _parse_dt(entry.get("endAt"))
        if end_at is None or end_at <= now:
            continue
        requested_at = _parse_dt(entry.get("requestedAt")) or end_at
        if best is None or requested_at > best[0]:
            best = (requested_at, end_at)
    return best[1] if best else None


class PodHomeDataUpdateCoordinator(DataUpdateCoordinator[dict[str, PodHomeCharger]]):
    """Polls mobile-api.pod-point.com and builds a PodHomeCharger per ppid."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: "PodHomeConfigEntry",
        api: PodHomeApiClient,
        *,
        email: str,
        password: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            # Overwritten every poll by _async_adjust_poll_interval.
            update_interval=FAST_POLL_INTERVAL,
        )
        self.api = api
        # Needed for the api3 session call below (POST .../sessions requires the plain
        # email/password, not just the Firebase bearer token used to build `api` above).
        self._email = email
        self._password = password
        # Account-level billing currency, fetched once (not every poll). None until then.
        self.currency: str | None = None
        # The account's preferred distance unit ("mi"/"km", via GET /users'
        # preferences.unitOfDistance) - used to pick Range/Odometer's suggested display unit
        # (sensor.py) instead of guessing from billing currency, which isn't a reliable proxy.
        # None until the first successful fetch.
        self.unit_of_distance: str | None = None
        # Staleness cadence for the fetch above.
        self._account_preferences_fetched_at: datetime.datetime | None = None
        # Account-wide rewards balance - see PodHomeRewards' docstring above. None until the
        # first successful fetch.
        self.rewards: PodHomeRewards | None = None
        self._rewards_fetched_at: datetime.datetime | None = None
        # Linked-vehicle data (battery/range/odometer/charge state) - one account-wide call,
        # re-fetched every poll only while something's charging (see VEHICLE_REFRESH_INTERVAL
        # above). Cached across skipped polls so a charger's `vehicle` field doesn't go
        # stale-empty just because this poll skipped the fetch.
        self._vehicle_by_ppid: dict[str, PodHomeVehicle] = {}
        self._vehicles_fetched_at: datetime.datetime | None = None
        # Per-charger data that rarely changes - fetched on FIRMWARE_TARIFF_REFRESH_INTERVAL and
        # cached, not gathered every poll. Retried sooner while still missing.
        self._firmware_by_ppid: dict[str, PodHomeFirmware] = {}
        self._firmware_fetched_at: dict[str, datetime.datetime] = {}
        self._tariff_windows_by_ppid: dict[str, list[PodHomeTariffWindow]] = {}
        self._tariff_windows_fetched_at: dict[str, datetime.datetime] = {}
        self._manual_schedules_by_ppid: dict[str, list[PodHomeManualScheduleWindow]] = {}
        self._manual_schedules_fetched_at: dict[str, datetime.datetime] = {}
        self._status_effective_from_by_ppid: dict[str, datetime.datetime | None] = {}
        self._status_effective_from_fetched_at: dict[str, datetime.datetime] = {}
        # offMode from GET /remote-lock/{ppid}: True locked, False unlocked, None either unset or
        # unsupported by this charger model (see DECISIONS.md). Fetched every poll alongside
        # preferences/charge_overrides below, not staleness-cached - a lock/unlock write should
        # be reflected the moment the next poll runs, same reasoning as max_price/boost_end_at.
        self._remote_lock_off_mode_by_ppid: dict[str, bool | None] = {}
        # Fetched every poll alongside connectivity, not staleness-cached.
        self._max_price_by_ppid: dict[str, float | None] = {}
        # The active boost's end time, if any - also fetched every poll, same reasoning as
        # max_price above. Only overwritten on a genuine list response, so a transient fetch
        # failure doesn't flap an in-progress boost to None.
        self._boost_end_at_by_ppid: dict[str, datetime.datetime | None] = {}
        # No separate staleness tracking - piggybacks on the tariffs fetch/cache below, since
        # it's parsed from that same already-fetched response, not a second API call.
        self._smart_charging_supported_by_ppid: dict[str, bool | None] = {}
        # Month-to-date charge-statistics - fetched every poll while that charger was charging as
        # of the previous poll, otherwise on CHARGE_STATS_REFRESH_INTERVAL.
        self._month_stats_by_ppid: dict[str, tuple[float | None, float | None]] = {}
        self._month_stats_fetched_at: dict[str, datetime.datetime] = {}
        # /charges (latest_charge) is one account-wide call, not per-ppid, so its own staleness
        # is tracked as a single timestamp rather than a dict - fetched every poll while ANY
        # charger was charging as of the previous poll, otherwise on CHARGE_STATS_REFRESH_INTERVAL.
        self._charges_fetched_at: datetime.datetime | None = None
        self._latest_charge_by_ppid: dict[str, PodHomeCharge] = {}
        # Running lifetime-since-tracking-started totals - see PodHomeTotalEnergySensor's
        # docstring (sensor.py) and _accumulate_total_energy() below. Persisted via Store (same
        # file as the sticky Charger Status signals - see _sticky_store below), fed from the
        # same /charges response fetched above. _total_watermark_by_ppid holds each ppid's most
        # recently counted finalized charge's endedAt, so a poll never re-adds an already-counted
        # charge.
        self._total_energy_kwh_by_ppid: dict[str, float] = {}
        self._total_watermark_by_ppid: dict[str, datetime.datetime] = {}
        self._total_started_at_by_ppid: dict[str, datetime.datetime] = {}
        # api3 (mobile-api's proxy to the account's older backend generation) account-level
        # session/pod-id mapping. Rarely changes - refreshed on the same cadence as
        # firmware/tariffs below. `_api3_user_id` is None until the first successful session
        # call; `_api3_pod_id_by_ppid` maps this account's chargers to the id api3's /charges
        # endpoint uses to identify them - /pods' own `unit_id` field, NOT its `id` field, despite
        # /charges naming its own field `pod.id` (two different api3 endpoints using "id" for
        # different underlying values).
        self._api3_user_id: int | None = None
        self._api3_pod_id_by_ppid: dict[str, int] = {}
        self._api3_account_fetched_at: datetime.datetime | None = None
        # The live in-progress charge, if any - same staleness/charging-aware gating as
        # latest_charge above (one account-wide call, filtered per-ppid by api3 pod id).
        self._api3_charges_fetched_at: datetime.datetime | None = None
        self._current_charge_by_ppid: dict[str, PodHomeCharge] = {}
        self._warned_keys: set[str] = set()
        # Wall-clock time WE observed each charger's lastSeenAt last change - not the
        # lastSeenAt value itself, which is the charger's own clock. Drives adaptive polling.
        self._last_seen_changed_at: dict[str, datetime.datetime] = {}
        # Sticky signals for Status - see PodHomeCharger's docstring on these three fields.
        # Updated every poll, not staleness-cached like firmware/tariffs above.
        self._charging_started_at_by_ppid: dict[str, datetime.datetime] = {}
        self._cable_unplugged_at_by_ppid: dict[str, datetime.datetime] = {}
        self._charge_finished_at_by_ppid: dict[str, datetime.datetime] = {}
        # Scoped per config entry (not per-domain) so a second Pod Home account gets its own file
        # rather than colliding.
        self._sticky_store: Store = Store(
            hass, STICKY_STATE_STORAGE_VERSION, f"{DOMAIN}_{config_entry.entry_id}_status"
        )
        # Separate Store (and separate load/save try/except below) from the sticky Charger
        # Status signals above, deliberately not sharing one file - a corrupt/unreadable status
        # file is low-stakes (self-heals within a poll or two), but the same failure wiping this
        # Total Energy running total would silently drop accumulated history.
        self._total_energy_store: Store = Store(
            hass, STICKY_STATE_STORAGE_VERSION, f"{DOMAIN}_{config_entry.entry_id}_total_energy"
        )
        # Live PodHomeBoostDurationTime instances, keyed by ppid (time.py registers/deregisters
        # itself in async_added_to_hass/async_will_remove_from_hass) - lets button.py reset the
        # entity back to unset after a boost, which HA's generic time.set_value service can't do
        # (its `time` field is required, no way to clear via it) - pod_home owns both entities,
        # so a direct call is the right tool here, not a cross-integration service. Loosely typed
        # (not PodHomeBoostDurationTime) to avoid coordinator.py importing a platform module.
        self.boost_duration_entities: dict[str, Any] = {}

    async def async_load_sticky_state(self) -> None:
        """Restore Status's sticky timestamps and the Total Energy running total from a previous
        run. Called once, before the first refresh, from async_setup_entry. The two are loaded
        independently - a failure loading one doesn't affect the other."""
        try:
            data = await self._sticky_store.async_load()
        except Exception:  # noqa: BLE001 - a corrupt/unreadable store file must not block setup
            _LOGGER.warning("Couldn't load saved charge-status state, starting fresh", exc_info=True)
            data = None
        if isinstance(data, dict):
            self._charging_started_at_by_ppid = self._parse_sticky_dict(data.get("charging_started_at"))
            self._cable_unplugged_at_by_ppid = self._parse_sticky_dict(data.get("cable_unplugged_at"))
            self._charge_finished_at_by_ppid = self._parse_sticky_dict(data.get("charge_finished_at"))

        try:
            total_data = await self._total_energy_store.async_load()
        except Exception:  # noqa: BLE001 - a corrupt/unreadable store file must not block setup
            _LOGGER.warning("Couldn't load saved total-energy state, starting fresh", exc_info=True)
            return
        if not isinstance(total_data, dict):
            return
        self._total_watermark_by_ppid = self._parse_sticky_dict(total_data.get("total_watermark"))
        self._total_started_at_by_ppid = self._parse_sticky_dict(total_data.get("total_started_at"))
        self._total_energy_kwh_by_ppid = self._parse_number_dict(total_data.get("total_energy_kwh"))

    @staticmethod
    def _parse_number_dict(raw) -> dict[str, float]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, float] = {}
        for ppid, value in raw.items():
            if isinstance(value, (int, float)):
                result[ppid] = value
        return result

    @staticmethod
    def _parse_sticky_dict(raw) -> dict[str, datetime.datetime]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, datetime.datetime] = {}
        for ppid, value in raw.items():
            parsed = _parse_dt(value)
            if parsed:
                result[ppid] = parsed
        return result

    def _sticky_state_for_storage(self) -> dict:
        return {
            "charging_started_at": {
                p: dt.isoformat() for p, dt in self._charging_started_at_by_ppid.items()
            },
            "cable_unplugged_at": {
                p: dt.isoformat() for p, dt in self._cable_unplugged_at_by_ppid.items()
            },
            "charge_finished_at": {
                p: dt.isoformat() for p, dt in self._charge_finished_at_by_ppid.items()
            },
        }

    def _total_energy_state_for_storage(self) -> dict:
        return {
            "total_watermark": {
                p: dt.isoformat() for p, dt in self._total_watermark_by_ppid.items()
            },
            "total_started_at": {
                p: dt.isoformat() for p, dt in self._total_started_at_by_ppid.items()
            },
            "total_energy_kwh": dict(self._total_energy_kwh_by_ppid),
        }

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

    @staticmethod
    def _stale(
        fetched_at: dict[str, datetime.datetime],
        ppid: str,
        now: datetime.datetime,
        interval: datetime.timedelta = FIRMWARE_TARIFF_REFRESH_INTERVAL,
    ) -> bool:
        return now - fetched_at.get(ppid, _NEVER_FETCHED) >= interval

    async def _safe_call(self, key: str, message: str, coro) -> dict | list:
        """Await `coro`; on PodHomeApiError, log (deduped) and return {} instead of raising.
        Most endpoints here return a dict; a couple (e.g. GET /chargers/{ppid}/firmware) return
        a bare list instead - `result or {}` only coerces a falsy result, a genuine list result
        passes through unchanged. Does not catch PodHomeAuthError, which propagates to trigger
        reauth.
        """
        try:
            result = await coro
            self._clear_warning(key)
            return result or {}
        except PodHomeApiError as exc:
            self._warn_once(key, f"{message}: {exc}")
            return {}

    async def _fetch_smart_schedule(self, ppid: str) -> dict:
        """Like _safe_call, but a 404 with one of these error codes is an expected, common state,
        not a real problem - logged at debug, not warning. Any other failure still goes through
        the normal _warn_once path.

        - NO_ACTIVE_CHARGING_SESSION: nothing currently plugged in/scheduled.
        - AFTER_INTENT_TIME: fires once the current Ready By target has passed, before a new one
          is set."""
        key = f"smart_schedule:{ppid}"
        expected_404_errors = ("NO_ACTIVE_CHARGING_SESSION", "AFTER_INTENT_TIME")
        try:
            result = await self.api.async_smart_schedule_active(ppid)
            self._clear_warning(key)
            return result or {}
        except PodHomeApiError as exc:
            if exc.status == 404 and isinstance(exc.body, dict) and exc.body.get("error") in (
                expected_404_errors
            ):
                _LOGGER.debug("No active smart schedule for %s (%s): %s", ppid, exc.body.get("error"), exc)
                return {}
            self._warn_once(
                key, f"Couldn't fetch active smart schedule for {ppid} (non-fatal): {exc}"
            )
            return {}

    async def _async_fetch_account_preferences(self, now: datetime.datetime) -> None:
        """Fetch the account's billing currency and preferred distance unit together, from GET
        /users. Sets self.currency/self.unit_of_distance directly; leaves whichever was already
        known alone on a partial/failed response rather than clearing it. Stamps
        _account_preferences_fetched_at on any successful response, even one where a field is
        legitimately absent, so the caller's gate settles on the normal staleness cadence rather
        than retrying every poll forever for that field alone."""
        try:
            users = await self.api.async_get_users()
        except PodHomeApiError as exc:
            self._warn_once(
                "account_preferences", f"Couldn't fetch account preferences (will retry): {exc}"
            )
            return
        self._account_preferences_fetched_at = now
        currency = ((users or {}).get("balance") or {}).get("currency")
        if currency:
            self.currency = currency
        unit_of_distance = ((users or {}).get("preferences") or {}).get("unitOfDistance")
        if unit_of_distance:
            self.unit_of_distance = unit_of_distance

    async def _async_refresh_rewards(self, now: datetime.datetime) -> None:
        """Refresh the account-wide rewards balance (see PodHomeRewards' docstring above).
        Non-fatal - a failure leaves self.rewards unchanged. Doesn't stamp _rewards_fetched_at
        on failure, so a genuine fetch error gets retried next poll."""
        try:
            raw = await self.api.async_reward_wallet()
        except PodHomeApiError as exc:
            self._warn_once("rewards", f"Couldn't fetch rewards balance (non-fatal): {exc}")
            return
        self._clear_warning("rewards")
        rewards = raw.get("rewards") or {}
        allowance = raw.get("allowance") or {}
        payments = raw.get("payments") or {}
        self.rewards = PodHomeRewards(
            balance_gbp=rewards.get("balanceGbp"),
            balance_miles=rewards.get("balanceMiles"),
            balance_points=rewards.get("balancePoints"),
            allowance_balance_gbp=allowance.get("balanceGbp"),
            annual_allowance_gbp=allowance.get("allowancePoundsEstimated"),
            payout_threshold_gbp=payments.get("thresholdGbp"),
        )
        self._rewards_fetched_at = now

    async def _async_refresh_api3_account(self, now: datetime.datetime) -> None:
        """Refresh api3's user_id and ppid->pod_id mapping. Non-fatal throughout: a failure means
        current_charge stays unavailable this poll. Doesn't stamp _api3_account_fetched_at
        unless both calls succeed, so a partial failure gets retried next poll rather than
        waiting out the full interval with an empty pod-id mapping."""
        try:
            session_resp = await self.api.async_create_api3_session(self._email, self._password)
        except PodHomeApiError as exc:
            self._warn_once("api3_session", f"Couldn't create api3 session (non-fatal): {exc}")
            return
        user_id = ((session_resp or {}).get("sessions") or {}).get("user_id")
        if user_id is None:
            self._warn_once("api3_session", "api3 session response had no user_id (non-fatal)")
            return
        self._clear_warning("api3_session")
        self._api3_user_id = user_id

        try:
            pods_resp = await self.api.async_api3_pods(user_id)
        except PodHomeApiError as exc:
            self._warn_once("api3_pods", f"Couldn't fetch api3 pods (non-fatal): {exc}")
            return
        self._clear_warning("api3_pods")
        self._api3_pod_id_by_ppid = {
            pod["ppid"]: pod["unit_id"]
            for pod in (pods_resp or {}).get("pods") or []
            if pod.get("ppid") and pod.get("unit_id") is not None
        }
        self._api3_account_fetched_at = now

    async def _async_refresh_api3_charges(self, now: datetime.datetime) -> None:
        """Refresh the live in-progress charge per ppid, from api3's charges endpoint - filtered
        by _api3_pod_id_by_ppid, since the endpoint returns every one of the account's pods'
        charges together, not scoped to one charger. Non-fatal. Entries come back newest-first,
        so the first open (ends_at is None) entry seen for a given ppid is the current one -
        duration/cost aren't populated live by the API on an open entry (both 0), so duration is
        computed here instead; cost has no reliable way to derive live, so it's left None rather
        than surfacing the API's misleading 0 as if it were real."""
        try:
            charges_resp = await self.api.async_api3_charges(self._api3_user_id)
        except PodHomeApiError as exc:
            self._warn_once("api3_charges", f"Couldn't fetch api3 charges (non-fatal): {exc}")
            return
        self._clear_warning("api3_charges")
        self._api3_charges_fetched_at = now

        pod_id_to_ppid = {pod_id: ppid for ppid, pod_id in self._api3_pod_id_by_ppid.items()}
        current_by_ppid: dict[str, PodHomeCharge] = {}
        open_entry_seen = False
        unmatched_pod_ids: set = set()
        for entry in (charges_resp or {}).get("charges") or []:
            if entry.get("ends_at") is not None:
                continue  # finished - mobile-api's own /charges (latest_charge) covers this
            open_entry_seen = True
            raw_pod_id = (entry.get("pod") or {}).get("id")
            ppid = pod_id_to_ppid.get(raw_pod_id)
            if not ppid:
                unmatched_pod_ids.add(raw_pod_id)
                continue  # unknown pod - see the warning below if this happens for every entry
            if ppid in current_by_ppid:
                continue  # this ppid's current charge was already found
            started_at = _parse_dt(entry.get("starts_at"))
            if started_at is None:
                continue
            billing = entry.get("billing_event") or {}
            current_by_ppid[ppid] = PodHomeCharge(
                id=str(entry["id"]) if entry.get("id") is not None else None,
                started_at=started_at,
                ended_at=None,
                duration=int((now - started_at).total_seconds()),
                energy_total=entry.get("kwh_used"),
                cost_amount=None,
                cost_currency=billing.get("currency")
                or (entry.get("billing_account") or {}).get("currency"),
                plugged_in_at=None,
                unplugged_at=None,
            )
        # At least one open session and a real pod-id mapping to check it against, but none
        # matched - warn once rather than letting current_charge quietly stay empty forever.
        if open_entry_seen and pod_id_to_ppid and not current_by_ppid:
            self._warn_once(
                "api3_charges_unmatched",
                "api3 /charges has an open session, but its pod id didn't match any known "
                f"charger (tried: {unmatched_pod_ids}, known unit_ids: "
                f"{set(pod_id_to_ppid)}) - current_charge will stay unavailable until this is "
                "investigated",
            )
        else:
            self._clear_warning("api3_charges_unmatched")
        self._current_charge_by_ppid = current_by_ppid

    async def _async_update_data(self) -> dict[str, PodHomeCharger]:
        try:
            return await self._async_fetch_data()
        except PodHomeAuthError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc

    async def _async_with_connection_retry(self, attempt):
        """Retry `attempt` (a zero-arg async callable performing one API call) up to
        CONNECTION_RETRY_ATTEMPTS times, but only for connection-level failures - see
        CONNECTION_RETRY_ATTEMPTS' comment above."""
        last_exc: PodHomeApiError | None = None
        for attempt_number in range(CONNECTION_RETRY_ATTEMPTS):
            try:
                return await attempt()
            except PodHomeApiError as exc:
                if exc.status != 0:
                    raise
                last_exc = exc
                if attempt_number < CONNECTION_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(CONNECTION_RETRY_DELAY_SECONDS)
        raise last_exc

    async def _async_fetch_data(self) -> dict[str, PodHomeCharger]:
        try:
            chargers_raw = await self._async_with_connection_retry(self.api.async_list_chargers)
        except PodHomeApiError as exc:
            raise UpdateFailed(str(exc)) from exc

        # Captured once and reused for every staleness check this poll, so every check stays
        # consistent with the others.
        now = dt_util.utcnow()

        account_preferences_stale = (
            self._account_preferences_fetched_at is None
            or now - self._account_preferences_fetched_at >= FIRMWARE_TARIFF_REFRESH_INTERVAL
        )
        # Computed from the PREVIOUS poll's data, before this poll's chargers_raw is parsed
        # below - both /charges' and the linked-vehicle fetch's tiered cadence depend on it.
        any_charging_last_poll = any(
            charger.charging_state == CHARGING_STATE_CHARGING
            for charger in (self.data or {}).values()
        )
        # Cable-connected covers every state meaning a car is physically plugged in, not just
        # Charging - used only to pick the linked-vehicle fetch's cadence tier below, so an
        # unrecognized chargingState (CHARGING_STATE_CABLE_CONNECTED.get() returning None) errs
        # toward "treat as connected" (fetch more often), unlike the Cable Status sensor itself,
        # which surfaces that ambiguity as unknown.
        any_cable_connected_last_poll = any(
            CHARGING_STATE_CABLE_CONNECTED.get(charger.charging_state) is not False
            for charger in (self.data or {}).values()
        )
        # The charger's own chargingState and the linked vehicle's own is_charging (from Enode)
        # aren't guaranteed to agree on which confirms "charging" first. Checking both here
        # closes a gap: if the vehicle side confirms charging before the charger does, the fast
        # "every poll" tier still needs to kick in immediately rather than waiting on the
        # charger to catch up.
        any_vehicle_charging_last_poll = any(
            charger.vehicle is not None and charger.vehicle.is_charging
            for charger in (self.data or {}).values()
        )
        if any_charging_last_poll or any_vehicle_charging_last_poll:
            vehicles_stale = True
        elif any_cable_connected_last_poll:
            vehicles_stale = (
                self._vehicles_fetched_at is None
                or now - self._vehicles_fetched_at >= CHARGE_STATS_REFRESH_INTERVAL
            )
        else:
            vehicles_stale = (
                self._vehicles_fetched_at is None
                or now - self._vehicles_fetched_at >= VEHICLE_REFRESH_INTERVAL
            )
        # account_preferences and vehicles are independent of each other - gathered together
        # rather than awaited one at a time so a poll where both are stale costs one round-trip's
        # worth of latency, not two.
        preflight_calls: dict[str, Any] = {}
        if account_preferences_stale:
            preflight_calls["account_preferences"] = self._async_fetch_account_preferences(now)
        if vehicles_stale:
            preflight_calls["vehicles"] = self._safe_call(
                "vehicles", "Couldn't fetch smart-charging vehicles (non-fatal)",
                self.api.async_smart_charging_chargers_and_vehicles(),
            )
        if preflight_calls:
            preflight_results = dict(
                zip(preflight_calls.keys(), await asyncio.gather(*preflight_calls.values()))
            )
            vehicles_raw = preflight_results.get("vehicles")
            if vehicles_raw:
                self._vehicle_by_ppid = self._vehicle_per_ppid(vehicles_raw)
                self._vehicles_fetched_at = now
        vehicle_by_ppid = self._vehicle_by_ppid

        if not chargers_raw:
            if self.data:
                self._warn_once(
                    "empty_chargers",
                    "GET /chargers returned no chargers this poll; keeping previous data",
                )
                return self.data
            return {}
        self._clear_warning("empty_chargers")

        today_utc = dt_util.now(datetime.timezone.utc).date()
        lookback_start = today_utc - RECENT_CHARGES_LOOKBACK
        charges_stale = (
            self._charges_fetched_at is None
            or now - self._charges_fetched_at >= CHARGE_STATS_REFRESH_INTERVAL
        )
        # api3 account mapping (user_id, ppid->pod_id) - own conservative cadence, not
        # charging-aware like the two blocks above (it changes essentially never).
        api3_account_stale = (
            self._api3_account_fetched_at is None
            or now - self._api3_account_fetched_at >= API3_ACCOUNT_REFRESH_INTERVAL
        )
        # Rewards balance - account-wide, no per-charger dimension, so fetched once per account
        # on the same conservative cadence as firmware/tariffs/api3 account mapping, not per-ppid.
        rewards_stale = (
            self._rewards_fetched_at is None
            or now - self._rewards_fetched_at >= FIRMWARE_TARIFF_REFRESH_INTERVAL
        )
        # charges/api3_account/rewards are independent of each other (api3_charges needs
        # api3_account's user_id, but that's awaited separately below) - gathered together
        # rather than awaited one at a time, same reasoning as the account_preferences/vehicles
        # gather above.
        account_calls: dict[str, Any] = {}
        if any_charging_last_poll or charges_stale:
            account_calls["charges"] = self._safe_call(
                "charges", "Couldn't fetch recent charges (non-fatal)",
                self.api.async_charges(lookback_start, today_utc),
            )
        if api3_account_stale:
            account_calls["api3_account"] = self._async_refresh_api3_account(now)
        if rewards_stale:
            account_calls["rewards"] = self._async_refresh_rewards(now)
        if account_calls:
            account_results = dict(
                zip(account_calls.keys(), await asyncio.gather(*account_calls.values()))
            )
            charges_raw = account_results.get("charges")
            if charges_raw:
                charge_entries = list(self._charge_entries_by_ppid(charges_raw))
                self._latest_charge_by_ppid = self._latest_charge_per_ppid(charge_entries)
                self._charges_fetched_at = now
                self._accumulate_total_energy(charge_entries)
        latest_charge_by_ppid = self._latest_charge_by_ppid

        # The live in-progress charge - only worth asking for once there's an api3 user_id,
        # then the same charging-aware/slow-fallback gating as /charges above.
        if self._api3_user_id is not None:
            api3_charges_stale = (
                self._api3_charges_fetched_at is None
                or now - self._api3_charges_fetched_at >= CHARGE_STATS_REFRESH_INTERVAL
            )
            if any_charging_last_poll or api3_charges_stale:
                await self._async_refresh_api3_charges(now)
        current_charge_by_ppid = self._current_charge_by_ppid

        result: dict[str, PodHomeCharger] = {}
        for raw in chargers_raw:
            ppid = raw.get("ppid")
            if not ppid:
                _LOGGER.warning("Skipping a /chargers entry with no ppid: %r", raw)
                continue

            model_info = raw.get("modelInfo") or {}
            timezone_name = raw.get("timezone")
            tz = resolve_timezone(timezone_name)
            today_local = dt_util.now(tz).date()
            month_start_local = today_local.replace(day=1)

            # Known before any request this poll - drives whether smart-schedules/active is
            # worth calling at all (see below).
            delegated_control_status = (raw.get("delegatedControl") or {}).get("status")

            # Charge Priority (chargingStrategy/maxPrice) is fetched every poll alongside
            # connectivity, not staleness-cached like firmware/tariffs below - a setting the
            # user may change in the app at any time. Relevant in both charging modes: Charge
            # Priority stays viewable/changeable regardless of Smart/Basic mode, unlike
            # smart-schedules/active below.
            preferences_call = self._safe_call(
                f"preferences:{ppid}",
                f"Couldn't fetch smart charging preferences for {ppid} (non-fatal, will retry)",
                self.api.async_smart_charging_preferences(ppid),
            )
            # A boost ("Charge Now") is short-lived and something the user just did in the app -
            # fetched every poll alongside Charge Priority above, not staleness-cached. Not
            # mode-gated either: the override endpoint isn't tied to delegatedControl.status the
            # way smart-schedules/active is, so fetched in both branches below like
            # preferences_call.
            charge_overrides_call = self._safe_call(
                f"charge_overrides:{ppid}",
                f"Couldn't fetch charge overrides for {ppid} (non-fatal, will retry)",
                self.api.async_get_charge_overrides(ppid),
            )
            # Remote Lock: also fetched every poll, not staleness-cached - see the
            # _remote_lock_off_mode_by_ppid comment above for why.
            remote_lock_call = self._safe_call(
                f"remote_lock:{ppid}",
                f"Couldn't fetch Remote Lock status for {ppid} (non-fatal, will retry)",
                self.api.async_get_remote_lock_status(ppid),
            )
            if delegated_control_status == DELEGATED_CONTROL_ACTIVE:
                # smart-schedules/active describes the current Smart Charging session's plan -
                # meaningless in Basic Charging mode (404 NO_ACTIVE_CHARGING_SESSION), so only
                # worth calling in Smart Charging mode. Re-fetched every poll rather than on the
                # slow staleness cadence, since it reflects the live session's own plan.
                status, smart_schedule_raw, preferences_raw, charge_overrides_raw, remote_lock_raw = await asyncio.gather(
                    self._safe_call(
                        f"connectivity:{ppid}",
                        f"Couldn't fetch connectivity status for {ppid} (non-fatal)",
                        self.api.async_connectivity_status(ppid),
                    ),
                    self._fetch_smart_schedule(ppid),
                    preferences_call,
                    charge_overrides_call,
                    remote_lock_call,
                )
            else:
                status, preferences_raw, charge_overrides_raw, remote_lock_raw = await asyncio.gather(
                    self._safe_call(
                        f"connectivity:{ppid}",
                        f"Couldn't fetch connectivity status for {ppid} (non-fatal)",
                        self.api.async_connectivity_status(ppid),
                    ),
                    preferences_call,
                    charge_overrides_call,
                    remote_lock_call,
                )
                smart_schedule_raw = {}
            smart_schedule_windows = self._parse_smart_schedule(smart_schedule_raw)
            if preferences_raw:
                self._max_price_by_ppid[ppid] = preferences_raw.get("maxPrice")
            # Only overwrite on a genuine list response (a failed fetch falls back to {} via
            # _safe_call, not a list) - leaves the last known boost end time in place rather
            # than flapping to None on a transient error.
            if isinstance(charge_overrides_raw, list):
                self._boost_end_at_by_ppid[ppid] = _current_boost_end_at(charge_overrides_raw, now)
            # remote_lock_raw is {"offMode": bool | None} - a genuine `null` (unset, or this
            # charger model doesn't support Remote Lock at all) is still a successful fetch, not
            # left unset.
            if remote_lock_raw:
                self._remote_lock_off_mode_by_ppid[ppid] = remote_lock_raw.get("offMode")

            charging_state = status.get("chargingState")
            if charging_state and charging_state not in CHARGING_STATE_OPTIONS:
                self._warn_once(
                    f"unknown_charging_state:{charging_state}",
                    f"Unrecognized chargingState {charging_state!r} for {ppid} - this is a "
                    "real API value we haven't seen before, worth reporting",
                )

            if delegated_control_status and delegated_control_status not in DELEGATED_CONTROL_OPTIONS:
                self._warn_once(
                    f"unknown_delegated_control_status:{delegated_control_status}",
                    f"Unrecognized delegatedControl.status {delegated_control_status!r} for "
                    f"{ppid} - this is a real API value we haven't seen before, worth reporting",
                )

            vehicle = vehicle_by_ppid.get(ppid)

            # Refresh whichever of the three Status sticky signals is true THIS poll - see
            # PodHomeCharger's docstring on charging_started_at/cable_unplugged_at/
            # charge_finished_at.
            if charging_state == CHARGING_STATE_CHARGING:
                self._charging_started_at_by_ppid[ppid] = now
            if is_momentarily_unplugged(charging_state):
                self._cable_unplugged_at_by_ppid[ppid] = now
            if charging_state == CHARGING_STATE_SUSPENDED_EV:
                self._charge_finished_at_by_ppid[ppid] = now

            # These six are independent of each other - gather whichever are actually due this
            # poll instead of awaiting them one at a time.
            stale_calls: dict[str, Any] = {}
            # Fetched every poll while this charger was charging as of the previous poll,
            # otherwise on the slower CHARGE_STATS_REFRESH_INTERVAL cadence.
            was_charging_last_poll = (
                self.data.get(ppid).charging_state if self.data and ppid in self.data else None
            ) == CHARGING_STATE_CHARGING
            if was_charging_last_poll or self._stale(
                self._month_stats_fetched_at, ppid, now, CHARGE_STATS_REFRESH_INTERVAL
            ):
                stale_calls["charge_stats"] = self._safe_call(
                    f"charge_stats:{ppid}",
                    f"Couldn't fetch month-to-date charge statistics for {ppid} (non-fatal)",
                    self.api.async_charge_statistics(ppid, month_start_local, today_local),
                )
            if self._stale(self._firmware_fetched_at, ppid, now):
                stale_calls["firmware"] = self._safe_call(
                    f"firmware:{ppid}",
                    f"Couldn't fetch firmware for {ppid} (non-fatal, will retry)",
                    self.api.async_charger_firmware(ppid),
                )
            if self._stale(self._tariff_windows_fetched_at, ppid, now):
                stale_calls["tariffs"] = self._safe_call(
                    f"tariffs:{ppid}",
                    f"Couldn't fetch tariffs for {ppid} (non-fatal, will retry)",
                    self.api.async_tariffs(ppid),
                )
            if self._stale(self._manual_schedules_fetched_at, ppid, now):
                stale_calls["manual_schedules"] = self._safe_call(
                    f"manual_schedules:{ppid}",
                    f"Couldn't fetch manual schedules for {ppid} (non-fatal, will retry)",
                    self.api.async_manual_schedules(ppid),
                )
            if self._stale(self._status_effective_from_fetched_at, ppid, now):
                stale_calls["delegated_control"] = self._safe_call(
                    f"delegated_control:{ppid}",
                    f"Couldn't fetch delegated control detail for {ppid} (non-fatal, will retry)",
                    self.api.async_delegated_control(ppid),
                )

            if stale_calls:
                results = dict(
                    zip(stale_calls.keys(), await asyncio.gather(*stale_calls.values()))
                )

                # Gate "fetched" on the raw response, not the parsed result - a genuinely empty
                # response is still a successful fetch and must not be retried every poll.
                charge_stats_raw = results.get("charge_stats")
                if charge_stats_raw:
                    energy = charge_stats_raw.get("energy") or {}
                    self._month_stats_by_ppid[ppid] = (energy.get("totalUsage"), energy.get("cost"))
                    self._month_stats_fetched_at[ppid] = now

                # Only stamp fetched_at once parsing actually produced something, so a
                # bad/unexpected response gets retried next poll instead of serving a stale
                # cached value for the full interval.
                firmware_raw = results.get("firmware")
                if firmware_raw:
                    firmware = self._parse_firmware(firmware_raw)
                    if firmware:
                        self._firmware_by_ppid[ppid] = firmware
                        self._firmware_fetched_at[ppid] = now

                tariffs_raw = results.get("tariffs")
                if tariffs_raw:
                    tariff_windows = self._parse_tariff_windows(tariffs_raw)
                    if tariff_windows:
                        self._tariff_windows_by_ppid[ppid] = tariff_windows
                        self._tariff_windows_fetched_at[ppid] = now
                    self._smart_charging_supported_by_ppid[ppid] = (
                        self._parse_smart_charging_supported(tariffs_raw)
                    )

                manual_schedules_raw = results.get("manual_schedules")
                if manual_schedules_raw:
                    manual_schedule_windows = self._parse_manual_schedules(manual_schedules_raw)
                    if manual_schedule_windows:
                        self._manual_schedules_by_ppid[ppid] = manual_schedule_windows
                        self._manual_schedules_fetched_at[ppid] = now

                delegated_control_raw = results.get("delegated_control")
                if delegated_control_raw:
                    status_effective_from = _parse_dt(
                        delegated_control_raw.get("statusEffectiveFrom")
                    )
                    if status_effective_from:
                        self._status_effective_from_by_ppid[ppid] = status_effective_from
                    self._status_effective_from_fetched_at[ppid] = now

            month_energy, month_cost = self._month_stats_by_ppid.get(ppid, (None, None))

            # First time this ppid's been seen with no persisted running total yet - start
            # tracking from now, not a full account-history backfill (see
            # PodHomeTotalEnergySensor's docstring, sensor.py). Set once.
            if ppid not in self._total_started_at_by_ppid:
                self._total_started_at_by_ppid[ppid] = now

            last_seen_at = _parse_dt(status.get("lastSeenAt"))
            previous = self.data.get(ppid) if self.data else None
            if (
                ppid not in self._last_seen_changed_at
                or last_seen_at != (previous.last_seen_at if previous else None)
            ):
                self._last_seen_changed_at[ppid] = dt_util.utcnow()

            # Carry forward the last known charge if this poll's lookback window found none.
            latest_charge = latest_charge_by_ppid.get(ppid)
            if latest_charge is None and self.data and ppid in self.data:
                latest_charge = self.data[ppid].latest_charge

            # Refine the live in-progress charge's duration from "time since plug-in" (its
            # default, set in _async_refresh_api3_charges) to actual cumulative charging time,
            # using this poll's smart_schedule_windows if available - see
            # cumulative_charging_seconds()'s docstring in helpers.py.
            current_charge = current_charge_by_ppid.get(ppid)
            if current_charge is not None and current_charge.started_at is not None:
                charging_seconds = cumulative_charging_seconds(
                    smart_schedule_windows, current_charge.started_at, now
                )
                if charging_seconds is None:
                    # No schedule to refine against this poll (Basic mode, or a transient
                    # fetch gap) - fall back to a fresh naive "time since plug-in" estimate
                    # rather than leaving whatever duration carried forward from an earlier
                    # poll frozen in place until refinement becomes possible again.
                    charging_seconds = int((now - current_charge.started_at).total_seconds())
                current_charge = dataclasses.replace(current_charge, duration=charging_seconds)
                # Written back into the dict itself, not just this loop's local variable -
                # otherwise self._current_charge_by_ppid keeps a stale duration for the rest
                # of this poll and for any later poll that skips this refresh.
                self._current_charge_by_ppid[ppid] = current_charge

            result[ppid] = PodHomeCharger(
                ppid=ppid,
                unit_id=raw.get("unitId"),
                timezone=timezone_name,
                model_style=model_info.get("style"),
                model_colour=model_info.get("colour"),
                architecture=model_info.get("architecture"),
                connection_state=status.get("connectionState"),
                charging_state=charging_state,
                delegated_control_status=delegated_control_status,
                delegated_control_status_effective_from=self._status_effective_from_by_ppid.get(
                    ppid
                ),
                charging_started_at=self._charging_started_at_by_ppid.get(ppid),
                cable_unplugged_at=self._cable_unplugged_at_by_ppid.get(ppid),
                charge_finished_at=self._charge_finished_at_by_ppid.get(ppid),
                connection_quality=status.get("connectionQuality"),
                last_seen_at=last_seen_at,
                latest_charge=latest_charge,
                current_charge=current_charge,
                month_energy_kwh=month_energy,
                month_cost_amount=month_cost,
                total_energy_kwh=self._total_energy_kwh_by_ppid.get(ppid),
                total_started_at=self._total_started_at_by_ppid.get(ppid),
                firmware=self._firmware_by_ppid.get(ppid),
                tariff_windows=self._tariff_windows_by_ppid.get(ppid),
                manual_schedule_windows=self._manual_schedules_by_ppid.get(ppid),
                smart_schedule_windows=smart_schedule_windows,
                vehicle=vehicle,
                max_price=self._max_price_by_ppid.get(ppid),
                boost_end_at=self._boost_end_at_by_ppid.get(ppid),
                smart_charging_supported=self._smart_charging_supported_by_ppid.get(ppid),
                remote_lock_off_mode=self._remote_lock_off_mode_by_ppid.get(ppid),
            )

        # Coalesced, not one write per poll - the sticky dicts can change up to once a minute.
        self._sticky_store.async_delay_save(self._sticky_state_for_storage, STICKY_STATE_SAVE_DELAY)
        self._total_energy_store.async_delay_save(
            self._total_energy_state_for_storage, STICKY_STATE_SAVE_DELAY
        )

        self._async_adjust_poll_interval()
        return result

    def _async_adjust_poll_interval(self) -> None:
        """Speed up or slow down future polls based on how recently any charger's lastSeenAt
        changed. Takes effect from the next scheduled poll."""
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
    def _charge_entries_by_ppid(charges_raw: dict):
        """Yield (ppid, entry, charger) for every /charges entry that resolves to a known
        charger. Call sites materialize this once (`list(...)`) and pass the result to both
        _latest_charge_per_ppid() and _accumulate_total_energy() rather than each re-iterating
        the raw response independently."""
        entries = ((charges_raw or {}).get("data") or {}).get("charges") or []
        for entry in entries:
            charger = entry.get("charger") or {}
            ppid = charger.get("id")
            if not ppid:
                continue
            yield ppid, entry, charger

    @staticmethod
    def _latest_charge_per_ppid(charge_entries: list) -> dict[str, PodHomeCharge]:
        latest: dict[str, PodHomeCharge] = {}
        latest_started: dict[str, datetime.datetime] = {}

        for ppid, entry, charger in charge_entries:
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

    def _accumulate_total_energy(self, charge_entries: list) -> None:
        """Incrementally add newly-finalized charges to the persisted running total, per ppid -
        reuses the same already-parsed charge_entries _latest_charge_per_ppid() also consumes,
        not a second pass over the raw response. Only looks at entries with endedAt set
        (finalized); a still-open session is only ever shown live via current_charge (see
        PodHomeTotalEnergySensor's docstring, sensor.py), so a session's energy is counted
        exactly once, the moment it finalizes.

        Compares every entry against a snapshot of each ppid's watermark taken BEFORE this
        batch, not the live-updating value - otherwise an older-but-still-new entry processed
        after a newer one in the same batch would be wrongly skipped. The stored watermark only
        ever moves forward.

        seen_ids additionally guards against two entries in the SAME batch for the same ppid
        sharing an endedAt (a duplicate row, or two sessions finalizing simultaneously), which
        the watermark snapshot alone wouldn't catch."""
        watermarks_before = dict(self._total_watermark_by_ppid)
        seen_ids: set = set()

        for ppid, entry, _charger in charge_entries:
            ended_at = _parse_dt(entry.get("endedAt"))
            if ended_at is None:
                continue  # still open - counted live via current_charge instead, not here

            watermark = watermarks_before.get(ppid)
            if watermark is not None and ended_at <= watermark:
                continue  # already counted on an earlier poll

            charge_id = entry.get("id")
            if charge_id is not None:
                if charge_id in seen_ids:
                    continue  # duplicate entry within this same batch - already counted above
                seen_ids.add(charge_id)

            energy = entry.get("energyTotal")
            if energy is not None:
                self._total_energy_kwh_by_ppid[ppid] = (
                    self._total_energy_kwh_by_ppid.get(ppid, 0.0) + energy
                )

            current_watermark = self._total_watermark_by_ppid.get(ppid)
            if current_watermark is None or ended_at > current_watermark:
                self._total_watermark_by_ppid[ppid] = ended_at

    @staticmethod
    def _parse_firmware(firmware_raw) -> PodHomeFirmware | None:
        """firmware_raw is a bare list from GET /chargers/{ppid}/firmware - unlike the legacy
        api3/v5/units/{unitId}/firmware endpoint this replaced, it's not `data`-wrapped."""
        entries = firmware_raw if isinstance(firmware_raw, list) else []
        if not entries:
            return None
        entry = _safe_dict(entries[0])
        if not entry:
            return None
        version_info = _safe_dict(entry.get("versionInfo"))
        update_status = _safe_dict(entry.get("updateStatus"))
        return PodHomeFirmware(
            manifest_id=version_info.get("manifestId"),
            update_available=update_status.get("isUpdateAvailable"),
            serial_number=entry.get("serialNumber"),
        )

    @staticmethod
    def _parse_smart_charging_supported(tariffs_raw) -> bool | None:
        """Same source as _parse_tariff_windows (data[0]), parsed separately since the two are
        conceptually distinct (a list of windows vs. a single capability flag)."""
        entries = (tariffs_raw or {}).get("data") or []
        if not entries or not isinstance(entries, list):
            return None
        return _safe_dict(entries[0]).get("smartChargingSupported")

    @staticmethod
    def _parse_tariff_windows(tariffs_raw) -> list[PodHomeTariffWindow] | None:
        entries = (tariffs_raw or {}).get("data") or []
        if not entries or not isinstance(entries, list):
            return None
        entry = _safe_dict(entries[0])
        windows_raw = entry.get("tariffInfo") or []
        if not isinstance(windows_raw, list):
            return None
        windows = [
            PodHomeTariffWindow(
                days=w.get("days") or [],
                start=w.get("start"),
                end=w.get("end"),
                price=w.get("price"),
            )
            for w in (_safe_dict(raw_window) for raw_window in windows_raw)
            if w
        ]
        return windows or None

    @staticmethod
    def _parse_manual_schedules(manual_schedules_raw) -> list[PodHomeManualScheduleWindow] | None:
        entries = (manual_schedules_raw or {}).get("data") or []
        if not isinstance(entries, list):
            return None
        windows = [
            PodHomeManualScheduleWindow(
                uid=w.get("uid"),
                start_day=w.get("startDay"),
                start_time=w.get("startTime"),
                end_day=w.get("endDay"),
                end_time=w.get("endTime"),
                is_active=_safe_dict(w.get("status")).get("isActive"),
            )
            for w in (_safe_dict(raw_window) for raw_window in entries)
            if w
        ]
        return windows or None

    @staticmethod
    def _parse_smart_schedule(smart_schedule_raw) -> list[PodHomeSmartScheduleWindow] | None:
        entries = (smart_schedule_raw or {}).get("schedule") or []
        if not isinstance(entries, list):
            return None
        windows = [
            PodHomeSmartScheduleWindow(
                type=w.get("type"),
                timestamp=_parse_dt(w.get("timestamp")),
                from_timestamp=_parse_dt(w.get("fromTimestamp")),
                to_timestamp=_parse_dt(w.get("toTimestamp")),
                tariff_rate=w.get("tariffRate"),
            )
            for w in (_safe_dict(raw_window) for raw_window in entries)
            if w
        ]
        return windows or None

    @staticmethod
    def _vehicle_per_ppid(vehicles_raw) -> dict[str, PodHomeVehicle]:
        """vehicles_raw is normally a list (one entry per charger with linked vehicles);
        _safe_call's generic dict fallback on error becomes {} here, handled by the isinstance
        check. Every element is coerced via _safe_dict too, since a malformed list element must
        not crash the whole poll.
        """
        result: dict[str, PodHomeVehicle] = {}
        if not isinstance(vehicles_raw, list):
            return result

        for raw_entry in vehicles_raw:
            entry = _safe_dict(raw_entry)
            ppid = entry.get("ppid")
            vehicle_links = entry.get("vehicles")
            if not ppid or not isinstance(vehicle_links, list) or not vehicle_links:
                continue
            vehicle_links = [_safe_dict(v) for v in vehicle_links]
            vehicle_links = [v for v in vehicle_links if v]
            if not vehicle_links:
                continue

            chosen = next((v for v in vehicle_links if v.get("isPrimary")), vehicle_links[0])
            vehicle_raw = _safe_dict(chosen.get("vehicle"))
            vehicle_id = vehicle_raw.get("id")
            if not vehicle_id:
                continue

            info = _safe_dict(vehicle_raw.get("vehicleInformation"))
            charge_state = _safe_dict(vehicle_raw.get("chargeState"))
            odometer = _safe_dict(vehicle_raw.get("odometer"))
            current_intent = _safe_dict(chosen.get("currentIntent"))
            charge_detail = _safe_dict(current_intent.get("chargeDetail"))
            intent_details_raw = _safe_dict(chosen.get("intents")).get("details")
            if isinstance(intent_details_raw, list) and intent_details_raw:
                intent_details = _safe_dict(intent_details_raw[0])
            else:
                intent_details = {}

            result[ppid] = PodHomeVehicle(
                id=vehicle_id,
                display_name=info.get("displayName"),
                brand=info.get("brand"),
                model=info.get("model"),
                battery_capacity_kwh=charge_state.get("batteryCapacity"),
                battery_level_percent=charge_state.get("batteryLevelPercent"),
                range_km=charge_state.get("range"),
                is_charging=charge_state.get("isCharging"),
                odometer_km=odometer.get("distanceKm"),
                ready_by=_parse_dt(current_intent.get("readyByTime")),
                is_plugged_in_to_this_charger=chosen.get("isPluggedInToThisCharger"),
                charge_limit_percent=charge_state.get("chargeLimitPercent"),
                charge_limit_source=charge_state.get("chargeLimitSource"),
                expected_charge_percent=charge_detail.get("expectedChargeByTargetPercent"),
                can_meet_target=current_intent.get("canMeetTarget"),
                cannot_meet_target_reason=current_intent.get("cannotMeetTargetReason"),
                power_delivery_state=charge_state.get("powerDeliveryState"),
                is_fully_charged=charge_state.get("isFullyCharged"),
                charge_rate=charge_state.get("chargeRate"),
                max_current=charge_state.get("maxCurrent"),
                charge_time_remaining=charge_state.get("chargeTimeRemaining"),
                intent_charge_by_time=intent_details.get("chargeByTime"),
                intent_charge_kwh=intent_details.get("chargeKWh"),
                synced_at=_parse_dt(charge_state.get("lastUpdated")),
            )

        return result
