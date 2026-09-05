"""Offline coverage for helpers.py's pure functions (no Home Assistant needed - see
_pod_home_loader.py). Uses SimpleNamespace stand-ins for PodHomeCharger/PodHomeCharge/etc.
(defined in coordinator.py, which does import Home Assistant) rather than the real dataclasses -
every function under test only does attribute access, so a duck-typed namespace with just the
fields each function reads is enough, and keeps this file free of the same HA-import problem
_pod_home_loader.py works around for helpers.py itself.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _pod_home_loader import const, helpers  # noqa: E402 - path insert must happen first


def _charger(**overrides):
    defaults = dict(
        charging_state=None,
        charging_started_at=None,
        cable_unplugged_at=None,
        charge_finished_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _charge(**overrides):
    defaults = dict(started_at=None, ended_at=None)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _tariff_window(price):
    return SimpleNamespace(price=price)


def _manual_window(**overrides):
    defaults = dict(
        uid="w1", start_day=1, start_time="00:30:00", end_day=1, end_time="05:30:00",
        is_active=True,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _smart_window(**overrides):
    defaults = dict(
        type=const.SMART_SCHEDULE_TYPE_CHARGING,
        timestamp=None,
        from_timestamp=None,
        to_timestamp=None,
        tariff_rate=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# resolve_timezone / humanize_model_style / known_or_none / currency_icon
# ---------------------------------------------------------------------------


def test_resolve_timezone_valid():
    tz = helpers.resolve_timezone("Europe/London")
    assert tz is not None
    assert str(tz) == "Europe/London"


@pytest.mark.parametrize("bad", [None, "", "Not/A/Real/Zone"])
def test_resolve_timezone_invalid_or_missing(bad):
    assert helpers.resolve_timezone(bad) is None


@pytest.mark.parametrize(
    "style,expected",
    [
        ("solo3", "Solo 3"),
        ("twin", "Twin"),
        (None, None),
        ("", None),
    ],
)
def test_humanize_model_style(style, expected):
    assert helpers.humanize_model_style(style) == expected


def test_known_or_none():
    assert helpers.known_or_none("a", ["a", "b"]) == "a"
    assert helpers.known_or_none("c", ["a", "b"]) is None
    assert helpers.known_or_none(None, ["a", "b"]) is None


@pytest.mark.parametrize(
    "currency,expected_icon",
    [("GBP", "mdi:currency-gbp"), ("EUR", "mdi:currency-eur"), ("USD", "mdi:cash"), (None, "mdi:cash")],
)
def test_currency_icon(currency, expected_icon):
    assert helpers.currency_icon(currency) == expected_icon


# ---------------------------------------------------------------------------
# humanize_tariff_rate / calendar_tariff_rate_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("OFF_PEAK", "Off peak"), ("ON_PEAK", "On peak"), (None, None), ("", None)],
)
def test_humanize_tariff_rate(raw, expected):
    assert helpers.humanize_tariff_rate(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ON_PEAK", "Peak"),  # app-matching override, not the generic transform
        ("OFF_PEAK", "Off-peak"),
        ("SOME_OTHER_RATE", "Some other rate"),  # falls back to humanize_tariff_rate
        (None, None),
    ],
)
def test_calendar_tariff_rate_label(raw, expected):
    assert helpers.calendar_tariff_rate_label(raw) == expected


# ---------------------------------------------------------------------------
# schedule_mode / parse_time_of_day / is_momentarily_unplugged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (const.DELEGATED_CONTROL_ACTIVE, const.SCHEDULE_MODE_SMART_CHARGING),
        (const.DELEGATED_CONTROL_INACTIVE, const.SCHEDULE_MODE_BASIC_CHARGING),
        ("PENDING", None),
        ("UNKNOWN", None),
        (None, None),
    ],
)
def test_schedule_mode(status, expected):
    assert helpers.schedule_mode(status) == expected


@pytest.mark.parametrize(
    "status,expected",
    [
        (const.DELEGATED_CONTROL_ACTIVE, True),
        (const.DELEGATED_CONTROL_INACTIVE, False),
        ("PENDING", True),  # unresolved - not guessed unavailable
        ("UNKNOWN", True),
        (None, True),
    ],
)
def test_smart_mode_available(status, expected):
    assert helpers.smart_mode_available(status) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("14:30:00", datetime.time(14, 30, 0)),
        ("00:00:00", datetime.time(0, 0, 0)),
        (None, None),
        ("", None),
        ("not-a-time", None),
    ],
)
def test_parse_time_of_day(raw, expected):
    assert helpers.parse_time_of_day(raw) == expected


def test_is_momentarily_unplugged():
    assert helpers.is_momentarily_unplugged(const.CHARGING_STATE_AVAILABLE) is True
    assert helpers.is_momentarily_unplugged(const.CHARGING_STATE_CHARGING) is False
    # Unrecognized/missing value: CHARGING_STATE_CABLE_CONNECTED.get(...) is None, not False,
    # and `None is False` is False - confirms the function doesn't silently treat unknown as
    # "cable connected".
    assert helpers.is_momentarily_unplugged("something-unrecognized") is False
    assert helpers.is_momentarily_unplugged(None) is False


# ---------------------------------------------------------------------------
# charger_status - every CHARGING_STATE_* plus the Finished-sticky logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "charging_state,expected",
    [
        (const.CHARGING_STATE_FAULTED, const.CHARGER_STATUS_FAULT),
        (const.CHARGING_STATE_CHARGING, const.CHARGER_STATUS_CHARGING),
        (const.CHARGING_STATE_AVAILABLE, const.CHARGER_STATUS_AVAILABLE),
        (const.CHARGING_STATE_RESERVED, const.CHARGER_STATUS_RESERVED),
        (const.CHARGING_STATE_UNAVAILABLE, const.CHARGER_STATUS_UNAVAILABLE),
        (const.CHARGING_STATE_SUSPENDED_EV, const.CHARGER_STATUS_FINISHED),
        (const.CHARGING_STATE_SUSPENDED_EVSE, const.CHARGER_STATUS_PAUSED),
        (const.CHARGING_STATE_PREPARING, const.CHARGER_STATUS_PREPARING),
        (const.CHARGING_STATE_FINISHING, const.CHARGER_STATUS_FINISHING),
        (const.CHARGING_STATE_UNKNOWN, None),
        ("totally-unrecognized", None),
        (None, None),
    ],
)
def test_charger_status_plain_mapping(charging_state, expected):
    now = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    charger = _charger(charging_state=charging_state)
    assert helpers.charger_status(charger, now) == expected


def test_charger_status_finished_sticky_overrides_suspended_evse():
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    finished_at = now - datetime.timedelta(minutes=5)
    charger = _charger(
        charging_state=const.CHARGING_STATE_SUSPENDED_EVSE,
        charge_finished_at=finished_at,
        charging_started_at=None,
        cable_unplugged_at=None,
    )
    assert helpers.charger_status(charger, now) == const.CHARGER_STATUS_FINISHED


def test_charger_status_finished_sticky_overrides_preparing_and_finishing():
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    finished_at = now - datetime.timedelta(minutes=1)
    for wandering_state in (const.CHARGING_STATE_PREPARING, const.CHARGING_STATE_FINISHING):
        charger = _charger(charging_state=wandering_state, charge_finished_at=finished_at)
        assert helpers.charger_status(charger, now) == const.CHARGER_STATUS_FINISHED


def test_charger_status_finished_sticky_cleared_by_new_charging_session():
    """A charging_started_at after charge_finished_at means a new session has begun - Finished
    should no longer be sticky, even though chargingState is still SuspendedEVSE."""
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    finished_at = now - datetime.timedelta(minutes=10)
    started_at = now - datetime.timedelta(minutes=5)  # newer than finished_at
    charger = _charger(
        charging_state=const.CHARGING_STATE_SUSPENDED_EVSE,
        charge_finished_at=finished_at,
        charging_started_at=started_at,
    )
    assert helpers.charger_status(charger, now) == const.CHARGER_STATUS_PAUSED


def test_charger_status_finished_sticky_cleared_by_unplug():
    """A cable_unplugged_at after charge_finished_at means the sticky Finished window has ended -
    same reasoning as the new-session case above."""
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    finished_at = now - datetime.timedelta(minutes=10)
    unplugged_at = now - datetime.timedelta(minutes=5)
    charger = _charger(
        charging_state=const.CHARGING_STATE_SUSPENDED_EVSE,
        charge_finished_at=finished_at,
        cable_unplugged_at=unplugged_at,
    )
    assert helpers.charger_status(charger, now) == const.CHARGER_STATUS_PAUSED


def test_charger_status_no_finished_at_never_sticky():
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    charger = _charger(charging_state=const.CHARGING_STATE_SUSPENDED_EVSE, charge_finished_at=None)
    assert helpers.charger_status(charger, now) == const.CHARGER_STATUS_PAUSED


# ---------------------------------------------------------------------------
# select_last_charge
# ---------------------------------------------------------------------------


def test_select_last_charge_neither_present():
    assert helpers.select_last_charge(None, None) is None


def test_select_last_charge_only_current():
    current = _charge(started_at=datetime.datetime(2026, 1, 1, tzinfo=UTC))
    assert helpers.select_last_charge(current, None) is current


def test_select_last_charge_only_latest():
    latest = _charge(started_at=datetime.datetime(2026, 1, 1, tzinfo=UTC))
    assert helpers.select_last_charge(None, latest) is latest


def test_select_last_charge_different_sessions_prefers_current():
    started = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    current = _charge(started_at=started + datetime.timedelta(hours=1))
    latest = _charge(started_at=started, ended_at=started + datetime.timedelta(minutes=30))
    assert helpers.select_last_charge(current, latest) is current


def test_select_last_charge_same_session_latest_finalized_wins():
    started = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    current = _charge(started_at=started, ended_at=None)
    latest = _charge(started_at=started, ended_at=started + datetime.timedelta(hours=2))
    assert helpers.select_last_charge(current, latest) is latest


def test_select_last_charge_same_session_but_latest_not_yet_ended_current_wins():
    started = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    current = _charge(started_at=started, ended_at=None)
    latest = _charge(started_at=started, ended_at=None)
    assert helpers.select_last_charge(current, latest) is current


def test_select_last_charge_within_tolerance_counts_as_same_session():
    started = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    current = _charge(started_at=started, ended_at=None)
    latest = _charge(
        started_at=started + datetime.timedelta(seconds=3), ended_at=started + datetime.timedelta(hours=1)
    )
    assert helpers.select_last_charge(current, latest) is latest


# ---------------------------------------------------------------------------
# is_single_rate_tariff / charging_priority_label / max_price_for_charging_priority
# ---------------------------------------------------------------------------


def test_is_single_rate_tariff_true():
    windows = [_tariff_window(0.10), _tariff_window(0.10)]
    assert helpers.is_single_rate_tariff(windows) is True


def test_is_single_rate_tariff_false():
    windows = [_tariff_window(0.10), _tariff_window(0.30)]
    assert helpers.is_single_rate_tariff(windows) is False


@pytest.mark.parametrize("windows", [None, [], [_tariff_window(None)]])
def test_is_single_rate_tariff_unknown(windows):
    assert helpers.is_single_rate_tariff(windows) is None


def test_charge_priority_available_false_on_confirmed_single_rate():
    windows = [_tariff_window(0.10), _tariff_window(0.10)]
    assert helpers.charge_priority_available(windows) is False


def test_charge_priority_available_true_on_multi_rate():
    windows = [_tariff_window(0.10), _tariff_window(0.30)]
    assert helpers.charge_priority_available(windows) is True


@pytest.mark.parametrize("windows", [None, [], [_tariff_window(None)]])
def test_charge_priority_available_true_when_unknown(windows):
    # Not yet known - defaults available, not guessed unavailable.
    assert helpers.charge_priority_available(windows) is True


def test_charge_priority_label_basic():
    assert helpers.charge_priority_label_basic(False) == const.CHARGE_PRIORITY_SCHEDULE
    assert helpers.charge_priority_label_basic(True) == const.CHARGE_PRIORITY_ALWAYS_ON


def test_charging_priority_label_lowest_and_highest():
    windows = [_tariff_window(0.10), _tariff_window(0.30)]
    assert helpers.charging_priority_label(0.10, windows) == const.CHARGE_PRIORITY_LOWEST_COST
    assert helpers.charging_priority_label(0.30, windows) == const.CHARGE_PRIORITY_COMPLETE_CHARGE


def test_charging_priority_label_mid_price_unknown():
    windows = [_tariff_window(0.10), _tariff_window(0.30)]
    assert helpers.charging_priority_label(0.20, windows) is None


def test_charging_priority_label_single_rate_cant_disambiguate():
    windows = [_tariff_window(0.15), _tariff_window(0.15)]
    assert helpers.charging_priority_label(0.15, windows) is None


@pytest.mark.parametrize("max_price,windows", [(None, [_tariff_window(0.1)]), (0.1, None), (0.1, [])])
def test_charging_priority_label_missing_data(max_price, windows):
    assert helpers.charging_priority_label(max_price, windows) is None


def test_max_price_for_charging_priority_round_trips_charging_priority_label():
    windows = [_tariff_window(0.10), _tariff_window(0.30)]
    lowest = helpers.max_price_for_charging_priority(const.CHARGE_PRIORITY_LOWEST_COST, windows)
    highest = helpers.max_price_for_charging_priority(const.CHARGE_PRIORITY_COMPLETE_CHARGE, windows)
    assert lowest == 0.10
    assert highest == 0.30
    assert helpers.charging_priority_label(lowest, windows) == const.CHARGE_PRIORITY_LOWEST_COST
    assert helpers.charging_priority_label(highest, windows) == const.CHARGE_PRIORITY_COMPLETE_CHARGE


def test_max_price_for_charging_priority_unrecognized_label_or_no_data():
    windows = [_tariff_window(0.10)]
    assert helpers.max_price_for_charging_priority("not-a-real-label", windows) is None
    assert helpers.max_price_for_charging_priority(const.CHARGE_PRIORITY_LOWEST_COST, None) is None


# ---------------------------------------------------------------------------
# expand_manual_schedule_events
# ---------------------------------------------------------------------------


def test_expand_manual_schedule_events_simple_same_day_window():
    windows = [_manual_window(start_day=1, start_time="00:30:00", end_day=1, end_time="05:30:00")]
    # 2026-01-05 is a Monday (isoweekday 1).
    events = helpers.expand_manual_schedule_events(
        windows, datetime.date(2026, 1, 5), datetime.date(2026, 1, 6), UTC
    )
    assert len(events) == 1
    start, end, summary = events[0]
    assert start == datetime.datetime(2026, 1, 5, 0, 30, tzinfo=UTC)
    assert end == datetime.datetime(2026, 1, 5, 5, 30, tzinfo=UTC)
    assert summary == "Manual schedule"


def test_expand_manual_schedule_events_inactive_window_produces_nothing():
    windows = [_manual_window(is_active=False)]
    events = helpers.expand_manual_schedule_events(
        windows, datetime.date(2026, 1, 5), datetime.date(2026, 1, 6), UTC
    )
    assert events == []


def test_expand_manual_schedule_events_crosses_midnight():
    # Starts Monday 22:00, ends Tuesday 02:00 (same start_day/end_day, end_time <= start_time).
    windows = [_manual_window(start_day=1, start_time="22:00:00", end_day=1, end_time="02:00:00")]
    events = helpers.expand_manual_schedule_events(
        windows, datetime.date(2026, 1, 5), datetime.date(2026, 1, 6), UTC
    )
    assert len(events) == 1
    start, end, _ = events[0]
    assert start == datetime.datetime(2026, 1, 5, 22, 0, tzinfo=UTC)
    assert end == datetime.datetime(2026, 1, 6, 2, 0, tzinfo=UTC)


def test_expand_manual_schedule_events_week_boundary_wrap_caught():
    """A window starting the day before range_start should still produce an event if it overlaps
    the range (the "start a day early" cursor logic)."""
    windows = [_manual_window(start_day=7, start_time="23:00:00", end_day=1, end_time="01:00:00")]
    # 2026-01-04 is a Sunday (isoweekday 7); range starts the next day (Monday).
    events = helpers.expand_manual_schedule_events(
        windows, datetime.date(2026, 1, 5), datetime.date(2026, 1, 6), UTC
    )
    assert len(events) == 1
    start, end, _ = events[0]
    assert start == datetime.datetime(2026, 1, 4, 23, 0, tzinfo=UTC)
    assert end == datetime.datetime(2026, 1, 5, 1, 0, tzinfo=UTC)


def test_expand_manual_schedule_events_missing_fields_skipped():
    windows = [_manual_window(start_day=None), _manual_window(start_time=None)]
    events = helpers.expand_manual_schedule_events(
        windows, datetime.date(2026, 1, 5), datetime.date(2026, 1, 6), UTC
    )
    assert events == []


def test_expand_manual_schedule_events_empty_input():
    assert helpers.expand_manual_schedule_events(None, datetime.date(2026, 1, 5), datetime.date(2026, 1, 6), UTC) == []


# ---------------------------------------------------------------------------
# smart_schedule_events
# ---------------------------------------------------------------------------


def test_smart_schedule_events_charging_window_included():
    start = datetime.datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    end = datetime.datetime(2026, 1, 1, 4, 0, tzinfo=UTC)
    windows = [
        _smart_window(type=const.SMART_SCHEDULE_TYPE_CHARGING, from_timestamp=start, to_timestamp=end, tariff_rate="OFF_PEAK")
    ]
    events = helpers.smart_schedule_events(
        windows, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert len(events) == 1
    assert events[0] == (start, end, "Charging (Off-peak)")


def test_smart_schedule_events_paused_and_plugged_in_excluded():
    windows = [
        _smart_window(type="PAUSED", from_timestamp=datetime.datetime(2026, 1, 1, tzinfo=UTC), to_timestamp=datetime.datetime(2026, 1, 1, 1, tzinfo=UTC)),
        _smart_window(type="PLUGGED_IN", timestamp=datetime.datetime(2026, 1, 1, tzinfo=UTC)),
    ]
    events = helpers.smart_schedule_events(
        windows, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert events == []


def test_smart_schedule_events_outside_range_excluded():
    windows = [
        _smart_window(
            type=const.SMART_SCHEDULE_TYPE_CHARGING,
            from_timestamp=datetime.datetime(2025, 12, 31, tzinfo=UTC),
            to_timestamp=datetime.datetime(2025, 12, 31, 1, tzinfo=UTC),
        )
    ]
    events = helpers.smart_schedule_events(
        windows, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert events == []


def test_smart_schedule_events_no_tariff_rate_no_suffix():
    start = datetime.datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    end = datetime.datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
    windows = [_smart_window(type=const.SMART_SCHEDULE_TYPE_CHARGING, from_timestamp=start, to_timestamp=end)]
    events = helpers.smart_schedule_events(
        windows, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert events[0][2] == "Charging"


# ---------------------------------------------------------------------------
# cumulative_charging_seconds
# ---------------------------------------------------------------------------


def test_cumulative_charging_seconds_sums_only_charging_windows():
    session_start = datetime.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    now = datetime.datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
    windows = [
        _smart_window(
            type=const.SMART_SCHEDULE_TYPE_CHARGING,
            from_timestamp=datetime.datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            to_timestamp=datetime.datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        ),
        _smart_window(
            type="PAUSED",
            from_timestamp=datetime.datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
            to_timestamp=datetime.datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
        ),
        _smart_window(
            type=const.SMART_SCHEDULE_TYPE_CHARGING,
            from_timestamp=datetime.datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
            to_timestamp=datetime.datetime(2026, 1, 1, 3, 0, tzinfo=UTC),
        ),
    ]
    assert helpers.cumulative_charging_seconds(windows, session_start, now) == 2 * 3600


def test_cumulative_charging_seconds_clips_to_session_and_now():
    session_start = datetime.datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    now = datetime.datetime(2026, 1, 1, 0, 45, tzinfo=UTC)
    windows = [
        _smart_window(
            type=const.SMART_SCHEDULE_TYPE_CHARGING,
            from_timestamp=datetime.datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            to_timestamp=datetime.datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        ),
    ]
    assert helpers.cumulative_charging_seconds(windows, session_start, now) == 15 * 60


@pytest.mark.parametrize("windows,session_start", [(None, datetime.datetime(2026, 1, 1, tzinfo=UTC)), ([], datetime.datetime(2026, 1, 1, tzinfo=UTC)), ([_smart_window()], None)])
def test_cumulative_charging_seconds_none_when_no_data(windows, session_start):
    assert helpers.cumulative_charging_seconds(windows, session_start, datetime.datetime(2026, 1, 1, tzinfo=UTC)) is None


def test_cumulative_charging_seconds_zero_is_a_real_answer_not_none():
    """No CHARGING window has started yet within the session - a real 0, distinct from the
    None returned when there's no schedule at all."""
    session_start = datetime.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    now = datetime.datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
    windows = [
        _smart_window(
            type="PAUSED",
            from_timestamp=datetime.datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
            to_timestamp=datetime.datetime(2026, 1, 1, 0, 10, tzinfo=UTC),
        )
    ]
    assert helpers.cumulative_charging_seconds(windows, session_start, now) == 0
