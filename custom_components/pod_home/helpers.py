"""Small pure helper functions - no Home Assistant import, so they're independently testable."""
from __future__ import annotations

import datetime
import logging
import math
import re
from typing import TYPE_CHECKING, TypeVar
from zoneinfo import ZoneInfo

from .const import (
    CHARGE_PRIORITY_COMPLETE_CHARGE,
    CHARGE_PRIORITY_LOWEST_COST,
    CHARGER_STATUS_AVAILABLE,
    CHARGER_STATUS_CHARGING,
    CHARGER_STATUS_FAULT,
    CHARGER_STATUS_FINISHED,
    CHARGER_STATUS_FINISHING,
    CHARGER_STATUS_PAUSED,
    CHARGER_STATUS_PREPARING,
    CHARGER_STATUS_RESERVED,
    CHARGER_STATUS_UNAVAILABLE,
    CHARGING_STATE_AVAILABLE,
    CHARGING_STATE_CABLE_CONNECTED,
    CHARGING_STATE_CHARGING,
    CHARGING_STATE_FAULTED,
    CHARGING_STATE_FINISHING,
    CHARGING_STATE_PREPARING,
    CHARGING_STATE_RESERVED,
    CHARGING_STATE_SUSPENDED_EV,
    CHARGING_STATE_SUSPENDED_EVSE,
    CHARGING_STATE_UNAVAILABLE,
    DELEGATED_CONTROL_ACTIVE,
    DELEGATED_CONTROL_INACTIVE,
    SCHEDULE_MODE_BASIC_CHARGING,
    SCHEDULE_MODE_SMART_CHARGING,
    SMART_SCHEDULE_TYPE_CHARGING,
)

if TYPE_CHECKING:
    # Only for type hints - resolved as strings under `from __future__ import annotations`, so
    # this doesn't create a runtime import cycle with coordinator.py (which imports helpers.py).
    from .coordinator import (
        PodHomeCharge,
        PodHomeCharger,
        PodHomeManualScheduleWindow,
        PodHomeSmartScheduleWindow,
        PodHomeTariffWindow,
    )

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def resolve_timezone(timezone_name: str | None) -> ZoneInfo | None:
    """Resolve a charger's reported timezone, or None if missing/invalid."""
    if not timezone_name:
        return None
    try:
        return ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001 - genuinely any bad tz string should just fall back
        _LOGGER.debug("Unrecognized timezone %r, falling back to HA's default", timezone_name)
        return None


def humanize_model_style(style: str | None) -> str | None:
    """Turn a raw modelInfo.style value (e.g. "solo3") into a display-friendly model name
    (e.g. "Solo 3"), or None if style is missing."""
    if not style:
        return None
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", style)
    return spaced.title()


def known_or_none(value: T | None, known_values: list[T]) -> T | None:
    """Return value if it's a member of known_values, else None."""
    return value if value in known_values else None


def humanize_tariff_rate(tariff_rate: str | None) -> str | None:
    """Turn a raw tariffRate value (e.g. "OFF_PEAK") into a display-friendly label (e.g.
    "Off peak"), or None if missing. Generic SHOUTY_SNAKE_CASE -> sentence case transform, not a
    fixed enum lookup. Used as the fallback for values not in CALENDAR_TARIFF_RATE_LABELS below."""
    if not tariff_rate:
        return None
    return tariff_rate.replace("_", " ").capitalize()


# Matches the Pod Home app's own wording exactly, which doesn't match humanize_tariff_rate()'s
# generic transform ("Peak"/"Off-peak" vs "On peak"/"Off peak").
CALENDAR_TARIFF_RATE_LABELS = {
    "ON_PEAK": "Peak",
    "OFF_PEAK": "Off-peak",
}


def calendar_tariff_rate_label(tariff_rate: str | None) -> str | None:
    """The Schedule calendar's display label for a tariffRate value - see
    CALENDAR_TARIFF_RATE_LABELS above."""
    if not tariff_rate:
        return None
    return CALENDAR_TARIFF_RATE_LABELS.get(tariff_rate) or humanize_tariff_rate(tariff_rate)


# Pod Point only supports billing in these two currencies.
CURRENCY_ICONS = {
    "GBP": "mdi:currency-gbp",
    "EUR": "mdi:currency-eur",
}


def currency_icon(currency: str | None) -> str:
    """Icon for a monetary sensor showing an amount in `currency` - falls back to HA's generic
    "mdi:cash" device_class default for an unsupported or not-yet-known currency."""
    return CURRENCY_ICONS.get(currency or "", "mdi:cash")


def schedule_mode(delegated_control_status: str | None) -> str | None:
    """Map delegatedControl.status to "Smart Charging" (ACTIVE) or "Basic Charging" (INACTIVE).
    UNKNOWN and PENDING resolve to unknown (None) rather than "Basic Charging", same as a missing
    or unrecognized status."""
    if delegated_control_status == DELEGATED_CONTROL_ACTIVE:
        return SCHEDULE_MODE_SMART_CHARGING
    if delegated_control_status == DELEGATED_CONTROL_INACTIVE:
        return SCHEDULE_MODE_BASIC_CHARGING
    return None


def smart_mode_available(delegated_control_status: str | None) -> bool:
    """Whether a Smart-Charging-only entity (Ready By, Target Charge, Expected Charge,
    Electricity Rate) should report itself available - unlike Remote Lock's hardware-support
    gating (a permanent, one-time fact - see lock.py), Charging Mode genuinely toggles at any
    time from the app, so this is `available`/`unavailable`, not entity-registry disable: the
    functionality really is temporarily unavailable, not a capability this account will never
    have. An unresolved/unrecognized status is treated as available rather than guessed
    unavailable, matching schedule_mode()'s own "don't guess" handling."""
    mode = schedule_mode(delegated_control_status)
    return mode is None or mode == SCHEDULE_MODE_SMART_CHARGING


def parse_time_of_day(time_str: str | None) -> datetime.time | None:
    """Parse a plain "HH:MM:SS" local-time string, or None if missing/unparseable."""
    if not time_str:
        return None
    try:
        return datetime.time.fromisoformat(time_str)
    except ValueError:
        return None


def is_momentarily_unplugged(charging_state: str | None) -> bool:
    """Whether chargingState currently implies no cable connected."""
    return CHARGING_STATE_CABLE_CONNECTED.get(charging_state) is False


def charger_status(charger: "PodHomeCharger", now_utc: datetime.datetime) -> str | None:
    """Derive a small, user-meaningful Status from chargingState and the sticky
    charging_started_at/cable_unplugged_at/charge_finished_at timestamps the coordinator
    maintains. Every chargingState passes through as its own value (see CHARGER_STATUS_* in
    const.py), except: Finished overrides SuspendedEVSE/SuspendedEV/Preparing/Finishing while
    _is_finished_sticky() holds (chargingState can wander through several of these after a charge
    actually finishes before settling); SuspendedEVSE otherwise defaults to Paused; an
    unrecognized/Unknown chargingState resolves to unknown (the final `else`).

    chargingState alone decides Charging - the vehicle's isCharging/is_fully_charged flags are
    not consulted, since they're Enode-reported and not scoped to this specific charger (can read
    true from a different charger or another plug-in entirely)."""
    charging_state = charger.charging_state

    if charging_state == CHARGING_STATE_FAULTED:
        result = CHARGER_STATUS_FAULT
    elif charging_state == CHARGING_STATE_CHARGING:
        result = CHARGER_STATUS_CHARGING
    elif charging_state == CHARGING_STATE_AVAILABLE:
        result = CHARGER_STATUS_AVAILABLE
    elif charging_state == CHARGING_STATE_RESERVED:
        result = CHARGER_STATUS_RESERVED
    elif charging_state == CHARGING_STATE_UNAVAILABLE:
        result = CHARGER_STATUS_UNAVAILABLE
    elif charging_state == CHARGING_STATE_SUSPENDED_EV or _is_finished_sticky(charger, now_utc):
        result = CHARGER_STATUS_FINISHED
    elif charging_state == CHARGING_STATE_SUSPENDED_EVSE:
        result = CHARGER_STATUS_PAUSED
    elif charging_state == CHARGING_STATE_PREPARING:
        result = CHARGER_STATUS_PREPARING
    elif charging_state == CHARGING_STATE_FINISHING:
        result = CHARGER_STATUS_FINISHING
    else:
        result = None

    return result


def _is_finished_sticky(charger: "PodHomeCharger", now_utc: datetime.datetime) -> bool:
    """True only while charge_finished_at is the most recent of the three sticky timestamps -
    i.e. nothing that looks like a new charging session or a fresh unplug has happened since."""
    finished_at = charger.charge_finished_at
    if finished_at is None:
        return False
    started_at = charger.charging_started_at
    if started_at is not None and started_at > finished_at:
        return False
    unplugged_at = charger.cable_unplugged_at
    if unplugged_at is not None and unplugged_at > finished_at:
        return False
    return True


# Tolerance for matching current_charge and latest_charge's started_at as the same session -
# the two APIs aren't guaranteed to report the same instant bit-for-bit.
_SAME_SESSION_TOLERANCE = datetime.timedelta(seconds=5)


def select_last_charge(
    current_charge: "PodHomeCharge | None", latest_charge: "PodHomeCharge | None"
) -> "PodHomeCharge | None":
    """Which of current_charge (api3's live in-progress entry) and latest_charge (mobile-api's
    own finalized one) is the accurate source for Last Charge Duration/Energy/Cost right now.

    api3's current_charge stays "open" (ended_at None) until the cable is physically unplugged,
    not until charging itself finishes, so its duration can overrun the true charging time by
    hours. mobile-api's endedAt is set correctly the moment charging concludes.

    Once both describe the same physical session (matching started_at, within
    _SAME_SESSION_TOLERANCE) and mobile-api's version already shows it ended, prefer that
    finalized snapshot over api3's still-open one. Otherwise current_charge wins - including
    while the two refer to different sessions (latest_charge is the previous finished one;
    current_charge is a new session mobile-api hasn't reported back yet), where current_charge is
    correctly the more recent, relevant one.

    Deliberately does not guard against charging resuming after mobile-api reports ended by
    comparing energy totals between the two sources - the two APIs' energy figures have no
    guaranteed tolerance for matching exactly, so such a guard can misfire and block the switch to
    mobile-api's cost-populated snapshot indefinitely."""
    if not current_charge or not latest_charge:
        return current_charge or latest_charge
    same_session = (
        abs((latest_charge.started_at - current_charge.started_at).total_seconds())
        <= _SAME_SESSION_TOLERANCE.total_seconds()
    )
    if same_session and latest_charge.ended_at is not None:
        return latest_charge
    return current_charge


def is_single_rate_tariff(tariff_windows: list["PodHomeTariffWindow"] | None) -> bool | None:
    """Whether every priced window in the account's tariff shares the same rate. Used to gate the
    Charge Priority select's entity-registry enabled state (see
    async_sync_tariff_gated_entities() in entity.py). Returns None, not True/False, when there's
    not enough data to tell (no tariff fetched yet, or no window has a known price) - the caller
    leaves the entity's existing enabled state alone in that case."""
    if not tariff_windows:
        return None
    prices = [w.price for w in tariff_windows if w.price is not None]
    if not prices:
        return None
    return math.isclose(min(prices), max(prices), abs_tol=0.0005)


def charging_priority_label(
    max_price: float | None, tariff_windows: list["PodHomeTariffWindow"] | None
) -> str | None:
    """Derive the Charge Priority select's current label from maxPrice (GET .../preferences),
    compared against the account's own tariff rates - not read from chargingStrategy (see
    DECISIONS.md). maxPrice matches the cheapest tariff rate for "Lowest cost", the priciest for
    "Complete charge". Uses math.isclose() since float round-tripping through JSON isn't
    guaranteed bit-exact. On a single-rate tariff (min(prices) == max(prices)), "Lowest cost" and
    "Complete charge" can't be told apart from maxPrice alone, so this resolves to unknown."""
    if max_price is None or not tariff_windows:
        return None
    prices = [w.price for w in tariff_windows if w.price is not None]
    if not prices:
        return None
    lowest, highest = min(prices), max(prices)
    if math.isclose(lowest, highest, abs_tol=0.0005):
        return None
    if math.isclose(max_price, lowest, abs_tol=0.0005):
        return CHARGE_PRIORITY_LOWEST_COST
    if math.isclose(max_price, highest, abs_tol=0.0005):
        return CHARGE_PRIORITY_COMPLETE_CHARGE
    return None


def max_price_for_charging_priority(
    label: str, tariff_windows: list["PodHomeTariffWindow"] | None
) -> float | None:
    """Charge Priority's write side - label to the maxPrice value to PATCH to
    .../delegated-controls/{ppid}/preferences: the cheapest tariff rate for "Lowest cost", the
    priciest for "Complete charge" (mirrors charging_priority_label()'s read-side lookup, so
    write and read agree). None if there's no tariff data yet, or the label isn't recognized -
    the caller should refuse to write rather than guess at a price."""
    if not tariff_windows:
        return None
    prices = [w.price for w in tariff_windows if w.price is not None]
    if not prices:
        return None
    if label == CHARGE_PRIORITY_LOWEST_COST:
        return min(prices)
    if label == CHARGE_PRIORITY_COMPLETE_CHARGE:
        return max(prices)
    return None


def expand_manual_schedule_events(
    windows: list["PodHomeManualScheduleWindow"] | None,
    range_start: datetime.date,
    range_end: datetime.date,
    tz: datetime.tzinfo,
) -> list[tuple[datetime.datetime, datetime.datetime, str]]:
    """Expand manual_schedule_windows' weekly recurrence into concrete (start, end, summary)
    occurrences overlapping [range_start, range_end) - only for active windows. A window that
    crosses midnight (end_day differs from start_day, or end_time not after start_time on the
    same day) is handled but not confirmed to occur live."""
    events: list[tuple[datetime.datetime, datetime.datetime, str]] = []
    if not windows:
        return events
    range_start_dt = datetime.datetime.combine(range_start, datetime.time.min, tzinfo=tz)
    for window in windows:
        if not window.is_active:
            continue
        start_t = parse_time_of_day(window.start_time)
        end_t = parse_time_of_day(window.end_time)
        if window.start_day is None or start_t is None or end_t is None:
            continue
        if window.end_day is not None:
            day_span = (window.end_day - window.start_day) % 7
        else:
            day_span = 0
        if day_span == 0 and end_t <= start_t:
            day_span = 1  # crosses midnight
        # Start a day early to catch a wrapping occurrence that begins before range_start.
        cursor = range_start - datetime.timedelta(days=1)
        while cursor < range_end:
            if cursor.isoweekday() == window.start_day:
                start_dt = datetime.datetime.combine(cursor, start_t, tzinfo=tz)
                end_dt = datetime.datetime.combine(
                    cursor + datetime.timedelta(days=day_span), end_t, tzinfo=tz
                )
                if end_dt > range_start_dt:
                    events.append((start_dt, end_dt, "Manual schedule"))
            cursor += datetime.timedelta(days=1)
    events.sort(key=lambda e: e[0])
    return events


def smart_schedule_events(
    windows: list["PodHomeSmartScheduleWindow"] | None,
    range_start_utc: datetime.datetime,
    range_end_utc: datetime.datetime,
) -> list[tuple[datetime.datetime, datetime.datetime, str]]:
    """Build (start, end, summary) events directly from smart_schedule_windows' own absolute
    timestamps, clamped to the requested range. Only CHARGING windows produce an event -
    PLUGGED_IN is a point-in-time marker (no range) and PAUSED windows are deliberately excluded
    (see DECISIONS.md). Each event shows its tariff rate via calendar_tariff_rate_label()."""
    events: list[tuple[datetime.datetime, datetime.datetime, str]] = []
    for window in windows or []:
        if window.type != SMART_SCHEDULE_TYPE_CHARGING:
            continue
        if window.from_timestamp is None or window.to_timestamp is None:
            continue
        if window.to_timestamp <= range_start_utc or window.from_timestamp >= range_end_utc:
            continue  # no overlap with the requested range
        summary = "Charging"
        tariff_rate = calendar_tariff_rate_label(window.tariff_rate)
        if tariff_rate:
            summary = f"{summary} ({tariff_rate})"
        events.append((window.from_timestamp, window.to_timestamp, summary))
    events.sort(key=lambda e: e[0])
    return events


def cumulative_charging_seconds(
    windows: list["PodHomeSmartScheduleWindow"] | None,
    session_start: datetime.datetime | None,
    now: datetime.datetime,
) -> int | None:
    """Sum of actual CHARGING time (not Paused/Plugged-in) within [session_start, now], from
    smart_schedule_windows. More accurate than current_charge.duration's "time since plug-in",
    which overcounts by however long the session spent Paused before charging began.

    Returns None if there's no schedule to work with (Basic Charging mode, or not yet fetched) -
    the caller falls back to the naive plug-in-elapsed estimate in that case, not to zero; a real
    0 (no CHARGING window has started yet) is a different, correct answer from "we don't know."

    Each CHARGING window is clipped to [session_start, now] before being counted. If the schedule
    doesn't go back far enough to cover the whole session (untested for a long-running multi-day
    one), this undercounts rather than overcounts."""
    if not windows or session_start is None:
        return None
    total = datetime.timedelta()
    for window in windows:
        if window.type != SMART_SCHEDULE_TYPE_CHARGING:
            continue
        if window.from_timestamp is None or window.to_timestamp is None:
            continue
        start = max(window.from_timestamp, session_start)
        end = min(window.to_timestamp, now)
        if end > start:
            total += end - start
    return int(total.total_seconds())
