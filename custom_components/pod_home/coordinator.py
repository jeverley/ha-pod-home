"""DataUpdateCoordinator for the Pod Home integration."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterator
import dataclasses
from dataclasses import dataclass
import datetime
import logging
from typing import TYPE_CHECKING, Any, cast

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

# TEMPORARY: vendored copy, see __init__.py's import comment.
from .podpoint_mobile_api import PodHomeApiClient, PodHomeApiError, PodHomeAuthError
from .const import (
    CHARGING_STATE_CHARGING,
    CHARGING_STATE_OPTIONS,
    CHARGING_STATE_SUSPENDED_EV,
    DELEGATED_CONTROL_ACTIVE,
    DELEGATED_CONTROL_OPTIONS,
    DOMAIN,
)
from .helpers import (
    current_charging_seconds,
    datetime_to_schedule_date,
    expand_manual_schedule_events,
    is_momentarily_unplugged,
    resolve_timezone,
    smart_schedule_events,
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
# How far back _charging_state_transitions_by_ppid keeps entries - bounds an otherwise
# unbounded-over-uptime list; a real charging session never spans this long.
CHARGING_STATE_TRANSITION_RETENTION = datetime.timedelta(days=2)

# Firmware/tariffs rarely change; re-checked on this cadence.
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
# is decided from THIS poll's own freshly-fetched charger-side cable-connected state (a
# connectivity preflight in _async_fetch_data, run before this decision).
VEHICLE_REFRESH_INTERVAL = datetime.timedelta(minutes=5)

# A connection-level failure (couldn't reach mobile-api.pod-point.com at all - PodHomeApiError
# with status 0) on GET /chargers below, the one call whose failure is fatal to the whole poll,
# gets a few quick retries before giving up. NOT retried: a genuine HTTP error response (4xx/5xx)
# or PodHomeAuthError, a different exception type this doesn't catch at all.
CONNECTION_RETRY_ATTEMPTS = 3
CONNECTION_RETRY_DELAY_SECONDS = 2

_NEVER_FETCHED = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

# Status's sticky timestamps persist across HA restarts via HA's own Store helper - they
# change every poll (up to once a minute), so the save is delayed/coalesced.
STICKY_STATE_STORAGE_VERSION = 1
STICKY_STATE_SAVE_DELAY = 10  # seconds


def _safe_dict(value: object) -> dict[str, Any]:
    """Coerce a JSON value to a dict, discarding anything else. Guards nested .get() chains on
    response data that isn't guaranteed to match the expected shape at every level."""
    return value if isinstance(value, dict) else {}


@dataclass
class PodHomeCharge:
    """One charge session - a finished entry from mobile-api's /charges (latest_charge), or the
    live in-progress one from api3's charges endpoint (current_charge). ended_at is None on a
    current_charge; duration/energy_total/cost_amount are computed/read live rather than
    finalized.

    current_charge.duration is unset until _current_charge_duration() derives it (same poll)
    from this scheme's schedule/override data - frozen at the last known value on a poll where
    nothing can be refined against, or left None if nothing has ever been available to refine
    against for this session (see current_charging_seconds() in helpers.py)."""

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
    kept as sensor attributes. allowance_balance_gbp/
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
    is_plugged_in/is_plugged_in_to_this_charger are the only fields that reflect that."""

    id: str
    display_name: str | None
    brand: str | None
    model: str | None
    battery_capacity_kwh: float | None
    battery_level_percent: int | None
    range_km: float | None
    is_charging: bool | None
    odometer_km: float | None
    ready_by: datetime.datetime | None
    # The vehicle's own plug state (chargeState.isPluggedIn) - true whenever it's plugged into
    # any charger, not necessarily a Pod Point one this account knows about. Distinct from
    # is_plugged_in_to_this_charger below, which comes from the charger-side link instead.
    is_plugged_in: bool | None
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
    # confirmed live. charge_rate/max_current only ever observed null on this account - unit/shape
    # unconfirmed. charge_time_remaining has been observed non-null live, including in Basic
    # Charging - unit still unconfirmed (minutes assumed).
    power_delivery_state: str | None
    is_fully_charged: bool | None
    charge_rate: float | None
    max_current: float | None
    charge_time_remaining: int | None
    # The literal per-day Smart Charging config from intents.details[] - what the Target
    # Charge/Ready By write entities actually read and write. All 7 days are confirmed live to
    # always be identical - representative day picked from whichever entry is first.
    intent_charge_by_time: str | None
    intent_charge_kwh: float | None
    # When Enode itself last synced this vehicle's chargeState, not when we last polled it -
    # confirmed live to lag the real world by a variable amount, exposed as an attribute
    # (Battery sensor).
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
    # the API itself. Whichever timestamp is most recent wins: Finished is only reported while
    # charge_finished_at is more recent than the other two, letting it survive chargingState
    # later wandering through Finishing/Preparing/SuspendedEVSE. Persisted across HA restarts via
    # Store (see _sticky_store below).
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
    # GET /chargers/{ppid}/charge-overrides, see _parse_charge_overrides() above for how "current"
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
    # at all.
    remote_lock_off_mode: bool | None
    # Basic Charging's "Always on" mode - a non-deleted GET /chargers/{ppid}/charge-overrides
    # entry with no endAt at all (a Boost always has one - see _parse_charge_overrides() below).
    # Drives Charge Priority's Basic-mode read side (charge_priority_label_basic(), helpers.py) -
    # kept separate from boost_end_at, not folded in, since the two are different mechanisms.
    # None when charge-overrides has never successfully fetched for this ppid.
    always_on_active: bool | None


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


@dataclass
class PodHomeChargeOverrideState:
    """The account's currently-active boost or Always On override for one ppid, if any - a
    boost and Always On are mutually exclusive, classified together by _parse_charge_overrides()
    since both come from the same GET /chargers/{ppid}/charge-overrides list. Fetched every poll
    alongside preferences (not staleness-cached); only overwritten in the cache on a genuine
    list response, so a transient fetch failure doesn't flap an in-progress boost to None."""

    # The active boost's end time - whichever non-deleted, not-yet-ended entry has the latest
    # requestedAt (list order isn't trusted). An entry with no endAt is never a boost.
    boost_end_at: datetime.datetime | None
    # Whether any non-deleted entry has no endAt at all.
    always_on_active: bool
    # Every override entry's real coverage interval (requestedAt -> earliest of endAt/deletedAt/
    # now) that could overlap the current session - deleted (cancelled) entries included, since
    # a cancelled override still charged for real up to when it was cancelled. Feeds the live
    # charge duration (current_charging_seconds(), helpers.py).
    override_events: list[tuple[datetime.datetime, datetime.datetime, str]]


def _parse_charge_overrides(
    charge_overrides_raw: list[Any],
    now: datetime.datetime,
    session_start: datetime.datetime | None,
) -> PodHomeChargeOverrideState:
    """Single pass over GET /chargers/{ppid}/charge-overrides - see PodHomeChargeOverrideState's
    docstring for what each field means. `session_start`, if known, skips building an event for
    an entry that couldn't possibly overlap the current session - a plain optimization (the
    account's full override history is returned every time, unbounded, confirmed live), not a
    correctness filter - current_charging_seconds() clips every event to [session_start, now]
    regardless."""
    best: tuple[datetime.datetime, datetime.datetime] | None = None
    always_on_active = False
    override_events: list[tuple[datetime.datetime, datetime.datetime, str]] = []
    for raw_entry in charge_overrides_raw:
        entry = _safe_dict(raw_entry)
        requested_at = _parse_dt(entry.get("requestedAt"))
        end_at = _parse_dt(entry.get("endAt"))
        deleted_at = _parse_dt(entry.get("deletedAt"))

        if requested_at is not None:
            candidates = [t for t in (end_at, deleted_at) if t is not None]
            event_end = min(candidates) if candidates else now
            if event_end > requested_at and (session_start is None or event_end >= session_start):
                override_events.append(
                    (requested_at, event_end, "Always on" if end_at is None else "Boost")
                )

        # Current override state - deleted entries never count towards this.
        if deleted_at is not None:
            continue
        if end_at is None:
            always_on_active = True
            continue
        if end_at <= now:
            continue
        resolved_requested_at = requested_at or end_at
        if best is None or resolved_requested_at > best[0]:
            best = (resolved_requested_at, end_at)

    return PodHomeChargeOverrideState(
        boost_end_at=best[1] if best else None,
        always_on_active=always_on_active,
        override_events=override_events,
    )


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
        # (sensor.py). None until the first successful fetch.
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
        # Set by a write whose read-back depends on vehicle data (Target Charge, Ready By) so
        # the refresh it triggers actually re-fetches vehicles instead of skipping a tier that
        # isn't due yet - see request_vehicles_fetch() and PodHomeOptimisticWriteMixin.
        self._force_vehicles_fetch: bool = False
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
        # unsupported by this charger model. Fetched every poll alongside preferences/
        # charge_overrides below.
        self._remote_lock_off_mode_by_ppid: dict[str, bool | None] = {}
        # Fetched every poll alongside connectivity.
        self._max_price_by_ppid: dict[str, float | None] = {}
        # The account's active boost/Always On override, if any - also fetched every poll, same
        # reasoning as max_price above. See PodHomeChargeOverrideState's docstring.
        self._charge_override_by_ppid: dict[str, PodHomeChargeOverrideState] = {}
        # No separate staleness tracking - piggybacks on the tariffs fetch/cache below, since
        # it's parsed from that same already-fetched response, not a second API call.
        self._smart_charging_supported_by_ppid: dict[str, bool | None] = {}
        # Month-to-date charge-statistics - fetched every poll while that charger was charging as
        # of the previous poll, otherwise on CHARGE_STATS_REFRESH_INTERVAL.
        self._month_stats_by_ppid: dict[str, tuple[float | None, int | None]] = {}
        self._month_stats_fetched_at: dict[str, datetime.datetime] = {}
        # /charges (latest_charge) is one account-wide call, not per-ppid, so its own staleness
        # is tracked as a single timestamp - fetched every poll while ANY charger was charging as
        # of the previous poll, otherwise on CHARGE_STATS_REFRESH_INTERVAL.
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
        # Wall-clock time of the most recent control-entity write, any ppid - also drives
        # adaptive polling, so whatever changed gets detected sooner regardless of prior charger
        # activity. See mark_recent_write()/async_request_refresh_after_write().
        self._last_write_at: datetime.datetime | None = None
        # Per-ppid (lastSeenAt, chargingState) pairs, recorded on genuine change - see
        # _record_charging_state_transition()/_confirmed_override_events()/_effective_now().
        self._charging_state_transitions_by_ppid: dict[
            str, list[tuple[datetime.datetime, str]]
        ] = {}
        # Sticky signals for Status - see PodHomeCharger's docstring on these three fields.
        # Updated every poll.
        self._charging_started_at_by_ppid: dict[str, datetime.datetime] = {}
        self._cable_unplugged_at_by_ppid: dict[str, datetime.datetime] = {}
        self._charge_finished_at_by_ppid: dict[str, datetime.datetime] = {}
        # Scoped per config entry so a second Pod Home account gets its own file.
        self._sticky_store: Store[dict[str, Any]] = Store(
            hass, STICKY_STATE_STORAGE_VERSION, f"{DOMAIN}_{config_entry.entry_id}_status"
        )
        # Separate Store (and separate load/save try/except below) from the sticky Charger
        # Status signals above.
        self._total_energy_store: Store[dict[str, Any]] = Store(
            hass, STICKY_STATE_STORAGE_VERSION, f"{DOMAIN}_{config_entry.entry_id}_total_energy"
        )
        # Live PodHomeBoostDurationTime instances, keyed by ppid, used by button.py to reset the
        # entity after a boost.
        self.boost_duration_entities: dict[str, Any] = {}

    def request_vehicles_fetch(self) -> None:
        """Bypass the vehicle-fetch staleness tier on the very next poll - called by write paths
        (Target Charge, Ready By) whose read-back depends on vehicle data, so the refresh they
        trigger right after actually re-fetches it instead of waiting out the current tier. One
        forced attempt only - reset once _async_fetch_data has acted on it, whether or not that
        fetch itself succeeds."""
        self._force_vehicles_fetch = True

    def mark_recent_write(self) -> None:
        """Speeds up polling immediately after any control entity's write, regardless of prior
        charger activity - see _async_adjust_poll_interval()."""
        self._last_write_at = dt_util.utcnow()

    async def async_request_refresh_after_write(self) -> None:
        """Every write call site uses this instead of async_request_refresh() directly, so
        marking the write as recent can't be forgotten at an individual call site."""
        self.mark_recent_write()
        await self.async_request_refresh()

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
    def _parse_number_dict(raw: object) -> dict[str, float]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, float] = {}
        for ppid, value in raw.items():
            if isinstance(value, (int, float)):
                result[ppid] = value
        return result

    @staticmethod
    def _parse_sticky_dict(raw: object) -> dict[str, datetime.datetime]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, datetime.datetime] = {}
        for ppid, value in raw.items():
            parsed = _parse_dt(value)
            if parsed:
                result[ppid] = parsed
        return result

    def _sticky_state_for_storage(self) -> dict[str, Any]:
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

    def _total_energy_state_for_storage(self) -> dict[str, Any]:
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
        return PodHomeDataUpdateCoordinator._value_stale(
            fetched_at.get(ppid, _NEVER_FETCHED), now, interval
        )

    @staticmethod
    def _value_stale(
        fetched_at: datetime.datetime | None,
        now: datetime.datetime,
        interval: datetime.timedelta = FIRMWARE_TARIFF_REFRESH_INTERVAL,
    ) -> bool:
        """Same staleness test as _stale(), for a single fetched_at value rather than a
        per-ppid dict."""
        return fetched_at is None or now - fetched_at >= interval

    async def _safe_call(
        self, key: str, message: str, coro: Coroutine[Any, Any, dict[str, Any] | list[Any]]
    ) -> dict[str, Any] | list[Any]:
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

    async def _fetch_smart_schedule(self, ppid: str) -> dict[str, Any]:
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
        known alone on a partial/failed response. Stamps _account_preferences_fetched_at on any
        successful response, even one where a field is legitimately absent."""
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
        unless both calls succeed, so a partial failure gets retried next poll."""
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

    async def _async_refresh_api3_charges(self, api3_user_id: int, now: datetime.datetime) -> None:
        """Refresh the live in-progress charge per ppid, from api3's charges endpoint - filtered
        by _api3_pod_id_by_ppid, since the endpoint returns every one of the account's pods'
        charges together, not scoped to one charger. Non-fatal. Entries come back newest-first,
        so the first open (ends_at is None) entry seen for a given ppid is the current one -
        duration/cost aren't populated live by the API on an open entry (both 0) - both left None
        here rather than surfacing the API's misleading 0 as if it were real; duration is derived
        separately, see _current_charge_duration().

        `api3_user_id` is passed in already-narrowed by the caller (self._api3_user_id, only
        called once it's confirmed set)."""
        try:
            charges_resp = await self.api.async_api3_charges(api3_user_id)
        except PodHomeApiError as exc:
            self._warn_once("api3_charges", f"Couldn't fetch api3 charges (non-fatal): {exc}")
            return
        self._clear_warning("api3_charges")
        self._api3_charges_fetched_at = now

        pod_id_to_ppid = {pod_id: ppid for ppid, pod_id in self._api3_pod_id_by_ppid.items()}
        current_by_ppid: dict[str, PodHomeCharge] = {}
        open_entry_seen = False
        unmatched_pod_ids: set[Any] = set()
        for entry in (charges_resp or {}).get("charges") or []:
            if entry.get("ends_at") is not None:
                continue  # finished - mobile-api's own /charges (latest_charge) covers this
            open_entry_seen = True
            raw_pod_id = (entry.get("pod") or {}).get("id")
            ppid = pod_id_to_ppid.get(raw_pod_id) if raw_pod_id is not None else None
            if not ppid:
                unmatched_pod_ids.add(raw_pod_id)
                continue  # unknown pod - see the warning below if this happens for every entry
            if ppid in current_by_ppid:
                continue  # this ppid's current charge was already found
            started_at = _parse_dt(entry.get("starts_at"))
            entry_id = entry.get("id")
            if started_at is None or entry_id is None:
                continue
            billing = entry.get("billing_event") or {}
            current_by_ppid[ppid] = PodHomeCharge(
                id=str(entry_id),
                started_at=started_at,
                ended_at=None,
                duration=None,
                energy_total=entry.get("kwh_used"),
                cost_amount=None,
                cost_currency=billing.get("currency")
                or (entry.get("billing_account") or {}).get("currency"),
                plugged_in_at=None,
                unplugged_at=None,
            )
        # At least one open session and a real pod-id mapping to check it against, but none
        # matched - warn once.
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
            raise ConfigEntryAuthFailed(
                str(exc),
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                translation_placeholders={"error": str(exc)},
            ) from exc

    async def _async_with_connection_retry(
        self, attempt: Callable[[], Coroutine[Any, Any, list[dict[str, Any]]]]
    ) -> list[dict[str, Any]]:
        """Retry `attempt` (a zero-arg async callable performing one API call) up to
        CONNECTION_RETRY_ATTEMPTS times, but only for connection-level failures - see
        CONNECTION_RETRY_ATTEMPTS' comment above. Concretely typed for its one call site
        (async_list_chargers)."""
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
        # Reached only once the loop above has exhausted CONNECTION_RETRY_ATTEMPTS (always >= 1)
        # iterations, each of which either returns or sets last_exc before continuing.
        assert last_exc is not None
        raise last_exc

    async def _async_fetch_connectivity(
        self, chargers_raw: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Connectivity fetched for every charger up front, before deciding the vehicles/
        `/charges` cadence tiers below, so a charging-state transition promotes those tiers
        THIS poll. Missing-ppid entries are silently skipped here; the per-charger loop still
        logs its own warning for them once."""
        ppids: list[str] = []
        for raw in chargers_raw:
            raw_ppid = raw.get("ppid")
            if isinstance(raw_ppid, str) and raw_ppid:
                ppids.append(raw_ppid)
        if not ppids:
            return {}
        connectivity_results = await asyncio.gather(
            *(
                self._safe_call(
                    f"connectivity:{ppid}",
                    f"Couldn't fetch connectivity status for {ppid} (non-fatal)",
                    self.api.async_connectivity_status(ppid),
                )
                for ppid in ppids
            )
        )
        # connectivity-status-v2 is always dict-shaped on success; _safe_call's {} fallback
        # on error is a dict too - _safe_call's return type is only a union because it's
        # shared with list-returning endpoints elsewhere, not because this call can produce
        # one.
        return dict(zip(ppids, cast("list[dict[str, Any]]", connectivity_results)))

    def _vehicles_stale(
        self, connectivity_by_ppid: dict[str, dict[str, Any]], any_charging_this_poll: bool, now: datetime.datetime
    ) -> bool:
        """Picks the linked-vehicle fetch's staleness-tier cadence from this poll's connectivity
        plus last poll's vehicle-side charging state, then applies any one-shot forced fetch
        (a write's own request_vehicles_fetch(), consumed here regardless of what it decides)."""
        # Cable-connected covers every state meaning a car is physically plugged in, not just
        # Charging - an unrecognized chargingState is treated as connected here.
        any_cable_connected_this_poll = any(
            not is_momentarily_unplugged(connectivity.get("chargingState"))
            for connectivity in connectivity_by_ppid.values()
        )
        # Vehicle-side is_charging from last poll also promotes the fast tier.
        any_vehicle_charging_last_poll = any(
            charger.vehicle is not None and charger.vehicle.is_charging
            for charger in (self.data or {}).values()
        )
        if any_charging_this_poll or any_vehicle_charging_last_poll:
            vehicles_stale = True
        elif any_cable_connected_this_poll:
            vehicles_stale = self._value_stale(
                self._vehicles_fetched_at, now, CHARGE_STATS_REFRESH_INTERVAL
            )
        else:
            vehicles_stale = self._value_stale(
                self._vehicles_fetched_at, now, VEHICLE_REFRESH_INTERVAL
            )
        vehicles_stale = vehicles_stale or self._force_vehicles_fetch
        self._force_vehicles_fetch = False
        return vehicles_stale

    async def _async_preflight_connectivity_and_vehicles(
        self, chargers_raw: list[dict[str, Any]], now: datetime.datetime
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Also returns any_charging_this_poll, needed by _async_refresh_account_level_data
        too."""
        connectivity_by_ppid = await self._async_fetch_connectivity(chargers_raw)
        # Used by both /charges' and the linked-vehicle fetch's tiered cadence below.
        any_charging_this_poll = any(
            connectivity.get("chargingState") == CHARGING_STATE_CHARGING
            for connectivity in connectivity_by_ppid.values()
        )
        account_preferences_stale = self._value_stale(self._account_preferences_fetched_at, now)
        vehicles_stale = self._vehicles_stale(connectivity_by_ppid, any_charging_this_poll, now)

        # account_preferences and vehicles are independent of each other - gathered together.
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

        return connectivity_by_ppid, any_charging_this_poll

    async def _async_refresh_account_level_data(
        self, now: datetime.datetime, any_charging_this_poll: bool
    ) -> None:
        """Refreshes whichever of charges/api3-account-mapping/rewards/the live api3 charge are
        due this poll - account-level, fetched once regardless of charger count. Mutates
        self state directly, matching _async_refresh_rewards() etc."""
        today_utc = dt_util.now(datetime.timezone.utc).date()
        lookback_start = today_utc - RECENT_CHARGES_LOOKBACK
        charges_stale = self._value_stale(self._charges_fetched_at, now, CHARGE_STATS_REFRESH_INTERVAL)
        # api3 account mapping (user_id, ppid->pod_id) - own conservative cadence (it changes
        # essentially never).
        api3_account_stale = self._value_stale(
            self._api3_account_fetched_at, now, API3_ACCOUNT_REFRESH_INTERVAL
        )
        # Rewards balance - account-wide, no per-charger dimension, fetched once per account on
        # the same conservative cadence as firmware/tariffs/api3 account mapping.
        rewards_stale = self._value_stale(self._rewards_fetched_at, now)
        # charges/api3_account/rewards are independent of each other (api3_charges needs
        # api3_account's user_id, but that's awaited separately below) - gathered together.
        account_calls: dict[str, Any] = {}
        if any_charging_this_poll or charges_stale:
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

        # The live in-progress charge - only worth asking for once there's an api3 user_id,
        # then the same charging-aware/slow-fallback gating as /charges above.
        api3_user_id = self._api3_user_id
        if api3_user_id is not None:
            api3_charges_stale = self._value_stale(
                self._api3_charges_fetched_at, now, CHARGE_STATS_REFRESH_INTERVAL
            )
            if any_charging_this_poll or api3_charges_stale:
                await self._async_refresh_api3_charges(api3_user_id, now)

    async def _async_fetch_data(self) -> dict[str, PodHomeCharger]:
        try:
            chargers_raw = await self._async_with_connection_retry(self.api.async_list_chargers)
        except PodHomeApiError as exc:
            raise UpdateFailed(
                str(exc),
                translation_domain=DOMAIN,
                translation_key="api_failed",
                translation_placeholders={"error": str(exc)},
            ) from exc

        # Captured once and reused for every staleness check this poll, so every check stays
        # consistent with the others.
        now = dt_util.utcnow()

        connectivity_by_ppid, any_charging_this_poll = (
            await self._async_preflight_connectivity_and_vehicles(chargers_raw, now)
        )

        if not chargers_raw:
            if self.data:
                self._warn_once(
                    "empty_chargers",
                    "GET /chargers returned no chargers this poll; keeping previous data",
                )
                self._sticky_store.async_delay_save(
                    self._sticky_state_for_storage, STICKY_STATE_SAVE_DELAY
                )
                self._total_energy_store.async_delay_save(
                    self._total_energy_state_for_storage, STICKY_STATE_SAVE_DELAY
                )
                self._async_adjust_poll_interval()
                return self.data
            return {}
        self._clear_warning("empty_chargers")

        await self._async_refresh_account_level_data(now, any_charging_this_poll)

        result: dict[str, PodHomeCharger] = {}
        for raw in chargers_raw:
            charger = await self._async_build_charger(raw, now, connectivity_by_ppid)
            if charger is not None:
                result[charger.ppid] = charger

        # Coalesced - the sticky dicts can change up to once a minute.
        self._sticky_store.async_delay_save(self._sticky_state_for_storage, STICKY_STATE_SAVE_DELAY)
        self._total_energy_store.async_delay_save(
            self._total_energy_state_for_storage, STICKY_STATE_SAVE_DELAY
        )

        self._async_adjust_poll_interval()
        return result

    async def _async_fetch_stale_per_charger_data(
        self,
        ppid: str,
        now: datetime.datetime,
        month_start_local: datetime.date,
        today_local: datetime.date,
        was_charging_last_poll: bool,
    ) -> None:
        """Fetches whichever of month-to-date stats/firmware/tariffs/manual-schedules/
        delegated-control-detail are due this poll for this ppid, gathered together. Each
        cached independently, so a partial-failure poll still keeps whichever succeeded."""
        stale_calls: dict[str, Any] = {}
        # Fetched every poll while this charger was charging as of the previous poll,
        # otherwise on the slower CHARGE_STATS_REFRESH_INTERVAL cadence.
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

        if not stale_calls:
            return
        results = dict(zip(stale_calls.keys(), await asyncio.gather(*stale_calls.values())))

        # Gate "fetched" on the raw response - a genuinely empty response is still a successful
        # fetch and must not be retried every poll.
        charge_stats_raw = results.get("charge_stats")
        if charge_stats_raw:
            energy = charge_stats_raw.get("energy") or {}
            self._month_stats_by_ppid[ppid] = (energy.get("totalUsage"), energy.get("cost"))
            self._month_stats_fetched_at[ppid] = now

        # Only stamp fetched_at once parsing actually produced something, so a
        # bad/unexpected response gets retried next poll.
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
            status_effective_from = _parse_dt(delegated_control_raw.get("statusEffectiveFrom"))
            if status_effective_from:
                self._status_effective_from_by_ppid[ppid] = status_effective_from
            self._status_effective_from_fetched_at[ppid] = now

    def _record_charging_state_transition(
        self, ppid: str, charging_state: str | None, last_seen_at: datetime.datetime | None
    ) -> None:
        """Records (lastSeenAt, chargingState) whenever chargingState differs from the last
        recorded entry - anchored on the charger's own lastSeenAt, not our poll time
        (chargingState has no timestamp of its own). Feeds _confirmed_override_events()/
        _effective_now() below."""
        if charging_state is None or last_seen_at is None:
            return
        transitions = self._charging_state_transitions_by_ppid.setdefault(ppid, [])
        if not transitions or transitions[-1][1] != charging_state:
            transitions.append((last_seen_at, charging_state))
            cutoff = last_seen_at - CHARGING_STATE_TRANSITION_RETENTION
            while transitions and transitions[0][0] < cutoff:
                transitions.pop(0)

    @staticmethod
    def _has_confirmed_energy(current_charge: PodHomeCharge) -> bool:
        """Real, measured proof this session has delivered at least some energy - energy_total
        (api3's kwh_used) is a direct measurement: if it's grown, power definitely flowed.
        Session-wide, not per-window - a rare multi-window single session would have a later
        window inherit an earlier one's confirmation, accepted rather than solved here."""
        return current_charge.energy_total is not None and current_charge.energy_total > 0

    def _confirmed_override_events(
        self,
        ppid: str,
        override_events: list[tuple[datetime.datetime, datetime.datetime, str]],
        session_start: datetime.datetime,
    ) -> list[tuple[datetime.datetime, datetime.datetime, str]]:
        """A fresh override's own creation can't be trusted in advance - nothing could have
        synced before it existed - so its credited start waits for a confirmed Charging
        transition at/after requestedAt, dropped entirely if not yet confirmed. An override that
        already existed before this session began (requestedAt < session_start - e.g. Always On
        surviving a cable unplug/replug) is trusted from session_start directly, same reasoning
        as a long-standing schedule. Each event's own end is left untouched - already correctly
        bounded by the existing interval-clipping, and covered by _effective_now() below for the
        cases (early stop, cancellation lag) where it needs to be."""
        transitions = self._charging_state_transitions_by_ppid.get(ppid, [])
        confirmed: list[tuple[datetime.datetime, datetime.datetime, str]] = []
        for start, end, summary in override_events:
            if start < session_start:
                confirmed.append((session_start, end, summary))
                continue
            # Already confirmed Charging as of `start` (e.g. a boost extending an already
            # in-progress charge) - trusted from `start` directly. chargingState only records a
            # transition on genuine change, so it would never record a fresh confirmation here.
            state_at_start = next(
                (state for ts, state in reversed(transitions) if ts <= start), None
            )
            if state_at_start == CHARGING_STATE_CHARGING:
                confirmed.append((start, end, summary))
                continue
            confirmed_start = next(
                (
                    ts
                    for ts, state in transitions
                    if state == CHARGING_STATE_CHARGING and ts >= start
                ),
                None,
            )
            if confirmed_start is not None:
                confirmed.append((confirmed_start, end, summary))
        return confirmed

    def _effective_now(self, ppid: str, now: datetime.datetime) -> datetime.datetime:
        """Caps `now` back to the last confirmed moment charging was actually observed, if the
        charger's most recently known chargingState isn't Charging - so duration stops
        accumulating once charging has genuinely stopped (the vehicle finishing, or the charger
        not yet having caught up with a cancellation), rather than continuing for as long as a
        schedule window or override nominally stays open. Applies uniformly to schedule and
        override events alike - tracks whether the vehicle is still drawing power."""
        transitions = self._charging_state_transitions_by_ppid.get(ppid, [])
        if transitions and transitions[-1][1] != CHARGING_STATE_CHARGING:
            return transitions[-1][0]
        return now

    def _resolve_schedule_events(
        self,
        ppid: str,
        now: datetime.datetime,
        tz: datetime.tzinfo | None,
        delegated_control_status: str | None,
        smart_schedule_windows: list[PodHomeSmartScheduleWindow] | None,
        session_start: datetime.datetime,
    ) -> tuple[list[tuple[datetime.datetime, datetime.datetime, str]] | None, bool]:
        """Builds this poll's schedule_events for whichever Charging Scheme is active, plus
        session_has_schedule (whether a schedule has ever been available to refine against for
        this session, not just this poll)."""
        if delegated_control_status == DELEGATED_CONTROL_ACTIVE:
            # Smart Charging always attempts this fetch fresh every poll (no persistent
            # cache). None (not []) when this poll's fetch came back empty - distinct from a
            # real, possibly-empty events list, so current_charging_seconds() can tell "nothing
            # to work with" from "schedule fetched, nothing overlapped" (a real 0).
            schedule_events = (
                smart_schedule_events(smart_schedule_windows, session_start, now)
                if smart_schedule_windows else None
            )
            # Smart mode has no persistent cache to check, so this is unconditional; both
            # feed the same discriminator below regardless.
            session_has_schedule = True
        else:
            manual_schedule_windows = self._manual_schedules_by_ppid.get(ppid)
            schedule_events = (
                expand_manual_schedule_events(
                    manual_schedule_windows,
                    datetime_to_schedule_date(session_start, tz),
                    # +1 day: expand_manual_schedule_events treats range_end as
                    # exclusive, but "now" itself must be covered.
                    datetime_to_schedule_date(now, tz) + datetime.timedelta(days=1),
                    tz,
                )
                if manual_schedule_windows and tz is not None else None
            )
            # Cached (self._manual_schedules_by_ppid), so this reflects whether a
            # schedule has EVER been fetched for this ppid, not just this poll -
            # `schedule_events` above can legitimately be None this poll (e.g. tz momentarily
            # unresolvable) even once a schedule is known.
            session_has_schedule = manual_schedule_windows is not None
        return schedule_events, session_has_schedule

    def _derive_charging_seconds(
        self,
        ppid: str,
        now: datetime.datetime,
        current_charge: PodHomeCharge,
        session_start: datetime.datetime,
        schedule_events: list[tuple[datetime.datetime, datetime.datetime, str]] | None,
        session_has_schedule: bool,
    ) -> int | None:
        """The energy gate, override confirmation, and freeze/fallback logic behind
        _current_charge_duration()'s returned duration."""
        if not self._has_confirmed_energy(current_charge):
            # Genuinely unknown - e.g. a vehicle that's already full may never draw any power
            # this session despite a schedule window/override being open.
            # Bypasses the freeze/fallback logic below entirely - that's for a schedule/override
            # signal that's momentarily unavailable THIS poll, a different situation from "no
            # energy has been confirmed yet" (energy_total is monotonic, so once confirmed it
            # stays confirmed - this branch only ever applies before the very first confirmation).
            return None
        override_state = self._charge_override_by_ppid.get(ppid)
        override_events = self._confirmed_override_events(
            ppid,
            override_state.override_events if override_state else [],
            session_start,
        )
        effective_now = self._effective_now(ppid, now)
        charging_seconds = current_charging_seconds(
            schedule_events, session_start, effective_now,
            override_events=override_events,
        )
        if charging_seconds is not None:
            return charging_seconds
        # current_charging_seconds() only returns None when schedule_events is None AND
        # override_events is empty, so override_events is empty here too -
        # session_has_schedule alone decides freeze vs. fallback below.
        if not session_has_schedule:
            # Duration stays unknown when nothing has ever been available to refine against.
            return None
        previous_charger = self.data.get(ppid) if self.data else None
        previous_charge = previous_charger.current_charge if previous_charger else None
        if previous_charge is not None and previous_charge.id == current_charge.id:
            return previous_charge.duration
        # First poll of this session with no refinement yet - unknown.
        return None

    def _current_charge_duration(
        self,
        ppid: str,
        now: datetime.datetime,
        tz: datetime.tzinfo | None,
        delegated_control_status: str | None,
        smart_schedule_windows: list[PodHomeSmartScheduleWindow] | None,
    ) -> PodHomeCharge | None:
        """Derives the live in-progress charge's actual cumulative charging time from this
        scheme's own schedule (+ any active override) if available - see
        current_charging_seconds() in helpers.py. Returns self._current_charge_by_ppid's
        current value for this ppid unchanged (including None) if there's no live session or
        its started_at isn't known yet. Also writes the result back into that same dict, not
        just the returned value."""
        current_charge = self._current_charge_by_ppid.get(ppid)
        if current_charge is None or current_charge.started_at is None:
            return current_charge
        session_start = current_charge.started_at
        schedule_events, session_has_schedule = self._resolve_schedule_events(
            ppid, now, tz, delegated_control_status, smart_schedule_windows, session_start,
        )
        charging_seconds = self._derive_charging_seconds(
            ppid, now, current_charge, session_start, schedule_events, session_has_schedule
        )
        current_charge = dataclasses.replace(current_charge, duration=charging_seconds)
        # Also written back into _current_charge_by_ppid.
        self._current_charge_by_ppid[ppid] = current_charge
        return current_charge

    async def _async_fetch_live_per_charger_data(
        self,
        ppid: str,
        now: datetime.datetime,
        delegated_control_status: str | None,
        charging_state: str | None,
    ) -> list[PodHomeSmartScheduleWindow] | None:
        """Fetches preferences/charge-overrides/remote-lock/smart-schedule for this ppid -
        every poll, not staleness-cached, since each reflects something the user (or an
        active session) may have just changed. Caches each into its own self._x_by_ppid
        dict, updates the sticky Status timestamps, and warns on unrecognized enum values.
        Returns the parsed smart-schedule windows."""
        # Charge Priority (chargingStrategy/maxPrice) is fetched every poll alongside
        # connectivity - a setting the user may change live. Relevant in both charging
        # modes: Charge Priority stays viewable/changeable regardless of Smart/Basic mode.
        preferences_call = self._safe_call(
            f"preferences:{ppid}",
            f"Couldn't fetch smart charging preferences for {ppid} (non-fatal, will retry)",
            self.api.async_smart_charging_preferences(ppid),
        )
        # A boost ("Charge Now") is short-lived and something the user just did in the app -
        # fetched every poll alongside Charge Priority above, in both branches below like
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
            # worth calling in Smart Charging mode. Re-fetched every poll - it reflects the
            # live session's own plan.
            smart_schedule_raw, preferences_raw, charge_overrides_raw, remote_lock_raw = await asyncio.gather(
                self._fetch_smart_schedule(ppid),
                preferences_call,
                charge_overrides_call,
                remote_lock_call,
            )
        else:
            preferences_raw, charge_overrides_raw, remote_lock_raw = await asyncio.gather(
                preferences_call,
                charge_overrides_call,
                remote_lock_call,
            )
            smart_schedule_raw = {}
        smart_schedule_windows = self._parse_smart_schedule(smart_schedule_raw)
        if preferences_raw:
            self._max_price_by_ppid[ppid] = preferences_raw.get("maxPrice")
        # Only overwrite on a genuine list response (a failed fetch falls back to {} via
        # _safe_call, not a list) - leaves the last known boost end time in place.
        if isinstance(charge_overrides_raw, list):
            current_charge = self._current_charge_by_ppid.get(ppid)
            self._charge_override_by_ppid[ppid] = _parse_charge_overrides(
                charge_overrides_raw, now, current_charge.started_at if current_charge else None
            )
        # remote_lock_raw is {"offMode": bool | None} - a genuine `null` (unset, or this
        # charger model doesn't support Remote Lock at all) is still a successful fetch, not
        # left unset.
        if remote_lock_raw:
            self._remote_lock_off_mode_by_ppid[ppid] = remote_lock_raw.get("offMode")

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

        # Refresh whichever of the three Status sticky signals is true THIS poll - see
        # PodHomeCharger's docstring on charging_started_at/cable_unplugged_at/
        # charge_finished_at.
        if charging_state == CHARGING_STATE_CHARGING:
            self._charging_started_at_by_ppid[ppid] = now
        if is_momentarily_unplugged(charging_state):
            self._cable_unplugged_at_by_ppid[ppid] = now
        if charging_state == CHARGING_STATE_SUSPENDED_EV:
            self._charge_finished_at_by_ppid[ppid] = now

        return smart_schedule_windows

    async def _async_build_charger(
        self,
        raw: dict[str, Any],
        now: datetime.datetime,
        connectivity_by_ppid: dict[str, dict[str, Any]],
    ) -> PodHomeCharger | None:
        """Builds one charger's PodHomeCharger, fetching whichever of its per-ppid data is due
        this poll. Returns None (already logged) for a /chargers entry with no ppid."""
        ppid = raw.get("ppid")
        if not isinstance(ppid, str) or not ppid:
            _LOGGER.warning("Skipping a /chargers entry with no ppid: %r", raw)
            return None

        model_info = raw.get("modelInfo") or {}
        timezone_name = raw.get("timezone")
        tz = resolve_timezone(timezone_name)
        today_local = dt_util.now(tz).date()
        month_start_local = today_local.replace(day=1)

        # Known before any request this poll - drives whether smart-schedules/active is
        # worth calling at all (see _async_fetch_live_per_charger_data).
        delegated_control_status = (raw.get("delegatedControl") or {}).get("status")
        # Already fetched in the connectivity preflight above (this poll, not staleness
        # cached) - not re-fetched here.
        status = connectivity_by_ppid.get(ppid, {})
        charging_state = status.get("chargingState")

        smart_schedule_windows = await self._async_fetch_live_per_charger_data(
            ppid, now, delegated_control_status, charging_state
        )

        # Fetched every poll while this charger was charging as of the previous poll,
        # otherwise on the slower CHARGE_STATS_REFRESH_INTERVAL cadence.
        was_charging_last_poll = (
            self.data[ppid].charging_state if self.data and ppid in self.data else None
        ) == CHARGING_STATE_CHARGING
        await self._async_fetch_stale_per_charger_data(
            ppid, now, month_start_local, today_local, was_charging_last_poll
        )

        month_energy, month_cost = self._month_stats_by_ppid.get(ppid, (None, None))

        # First time this ppid's been seen with no persisted running total yet - start
        # tracking from now (see PodHomeTotalEnergySensor's docstring, sensor.py). Set once.
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
        latest_charge = self._latest_charge_by_ppid.get(ppid)
        if latest_charge is None and self.data and ppid in self.data:
            latest_charge = self.data[ppid].latest_charge

        self._record_charging_state_transition(ppid, charging_state, last_seen_at)
        current_charge = self._current_charge_duration(
            ppid, now, tz, delegated_control_status, smart_schedule_windows
        )
        override_state = self._charge_override_by_ppid.get(ppid)

        return PodHomeCharger(
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
            vehicle=self._vehicle_by_ppid.get(ppid),
            max_price=self._max_price_by_ppid.get(ppid),
            boost_end_at=override_state.boost_end_at if override_state else None,
            smart_charging_supported=self._smart_charging_supported_by_ppid.get(ppid),
            remote_lock_off_mode=self._remote_lock_off_mode_by_ppid.get(ppid),
            always_on_active=override_state.always_on_active if override_state else None,
        )

    def _async_adjust_poll_interval(self) -> None:
        """Speed up or slow down future polls based on how recently any charger's lastSeenAt
        changed, or a control-entity write happened. Takes effect from the next scheduled poll."""
        now = dt_util.utcnow()
        recent = any(
            now - changed_at <= RECENT_CHANGE_WINDOW
            for changed_at in self._last_seen_changed_at.values()
        ) or (
            self._last_write_at is not None and now - self._last_write_at <= RECENT_CHANGE_WINDOW
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
    def _charge_entries_by_ppid(
        charges_raw: dict[str, Any],
    ) -> Iterator[tuple[str, dict[str, Any], dict[str, Any]]]:
        """Yield (ppid, entry, charger) for every /charges entry that resolves to a known
        charger. Call sites materialize this once (`list(...)`) and pass the result to both
        _latest_charge_per_ppid() and _accumulate_total_energy()."""
        entries = ((charges_raw or {}).get("data") or {}).get("charges") or []
        for entry in entries:
            charger = entry.get("charger") or {}
            ppid = charger.get("id")
            if not ppid:
                continue
            yield ppid, entry, charger

    @staticmethod
    def _latest_charge_per_ppid(
        charge_entries: list[tuple[str, dict[str, Any], dict[str, Any]]],
    ) -> dict[str, PodHomeCharge]:
        latest: dict[str, PodHomeCharge] = {}
        latest_started: dict[str, datetime.datetime] = {}

        for ppid, entry, charger in charge_entries:
            started_at = _parse_dt(entry.get("startedAt"))
            entry_id = entry.get("id")
            if started_at is None or entry_id is None:
                continue

            if ppid in latest_started and started_at <= latest_started[ppid]:
                continue

            cost = entry.get("cost") or {}
            latest_started[ppid] = started_at
            latest[ppid] = PodHomeCharge(
                id=str(entry_id),
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

    def _accumulate_total_energy(
        self, charge_entries: list[tuple[str, dict[str, Any], dict[str, Any]]]
    ) -> None:
        """Incrementally add newly-finalized charges to the persisted running total, per ppid -
        reuses the already-parsed charge_entries _latest_charge_per_ppid() also consumes. Only
        entries with endedAt set (finalized) count, so a session's energy is added exactly once,
        the moment it finalizes.

        Compares against a snapshot of each ppid's watermark taken before this batch, so an
        older-but-still-new entry processed after a newer one in the same batch isn't wrongly
        skipped - the stored watermark only ever moves forward.

        seen_ids additionally guards two entries in the SAME batch sharing an endedAt, which
        the watermark snapshot alone wouldn't catch."""
        watermarks_before = dict(self._total_watermark_by_ppid)
        seen_ids: set[Any] = set()

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
    def _parse_firmware(firmware_raw: object) -> PodHomeFirmware | None:
        """GET /chargers/{ppid}/firmware returns a bare list, not data-wrapped."""
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
    def _parse_smart_charging_supported(tariffs_raw: object) -> bool | None:
        """Same source as _parse_tariff_windows (data[0]), parsed separately since the two are
        conceptually distinct (a list of windows vs. a single capability flag)."""
        entries = _safe_dict(tariffs_raw).get("data") or []
        if not entries or not isinstance(entries, list):
            return None
        return _safe_dict(entries[0]).get("smartChargingSupported")

    @staticmethod
    def _parse_tariff_windows(tariffs_raw: object) -> list[PodHomeTariffWindow] | None:
        entries = _safe_dict(tariffs_raw).get("data") or []
        if not entries or not isinstance(entries, list):
            return None
        entry = _safe_dict(entries[0])
        windows_raw = entry.get("tariffInfo") or []
        if not isinstance(windows_raw, list):
            return None
        windows = []
        for raw_window in windows_raw:
            w = _safe_dict(raw_window)
            start, end = w.get("start"), w.get("end")
            if not w or start is None or end is None:
                continue  # a window needs both bounds to mean anything
            windows.append(
                PodHomeTariffWindow(
                    days=w.get("days") or [], start=start, end=end, price=w.get("price")
                )
            )
        return windows or None

    @staticmethod
    def _parse_manual_schedules(
        manual_schedules_raw: object,
    ) -> list[PodHomeManualScheduleWindow] | None:
        entries = _safe_dict(manual_schedules_raw).get("data") or []
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
    def _parse_smart_schedule(smart_schedule_raw: object) -> list[PodHomeSmartScheduleWindow] | None:
        entries = _safe_dict(smart_schedule_raw).get("schedule") or []
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
    def _vehicle_per_ppid(vehicles_raw: object) -> dict[str, PodHomeVehicle]:
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
                is_plugged_in=charge_state.get("isPluggedIn"),
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
