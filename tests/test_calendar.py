"""Calendar entity tests, using pytest-homeassistant-custom-component's real `hass` fixture and
the shared factories in tests/_fixtures.py. The event-expansion logic itself is already covered
offline (tests/test_helpers.py) - these focus on PodHomeScheduleCalendar's own mode-branching and
current-or-next `event` selection.
"""
from __future__ import annotations

import datetime

import pytest
from homeassistant.core import HomeAssistant

import custom_components.pod_home.calendar as calendar
from tests._fixtures import (
    PPID,
    make_charger,
    make_coordinator,
    make_manual_window,
    make_smart_window,
)

pytestmark = pytest.mark.asyncio

UTC = datetime.timezone.utc


async def test_smart_mode_uses_smart_schedule_windows(hass: HomeAssistant) -> None:
    window = make_smart_window(
        from_timestamp=datetime.datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        to_timestamp=datetime.datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
    )
    coordinator = make_coordinator(
        hass,
        {PPID: make_charger(delegated_control_status="ACTIVE", smart_schedule_windows=[window])},
    )
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)
    events = await entity.async_get_events(
        hass, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert len(events) == 1
    assert events[0].start == window.from_timestamp


async def test_basic_mode_uses_manual_schedule_windows(hass: HomeAssistant) -> None:
    window = make_manual_window(start_day=1, start_time="00:30:00", end_day=1, end_time="05:30:00")
    coordinator = make_coordinator(
        hass,
        {
            PPID: make_charger(
                delegated_control_status="INACTIVE",
                manual_schedule_windows=[window],
                timezone=UTC,
            )
        },
    )
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)
    # 2026-01-05 is a Monday.
    events = await entity.async_get_events(
        hass, datetime.datetime(2026, 1, 5, tzinfo=UTC), datetime.datetime(2026, 1, 6, tzinfo=UTC)
    )
    assert len(events) == 1
    assert events[0].start == datetime.datetime(2026, 1, 5, 0, 30, tzinfo=UTC)


async def test_unrecognized_mode_returns_no_events(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(
        hass, {PPID: make_charger(delegated_control_status="PENDING")}
    )
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)
    events = await entity.async_get_events(
        hass, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert events == []


async def test_event_property_skips_past_events_returns_next(hass: HomeAssistant) -> None:
    past = make_smart_window(
        from_timestamp=datetime.datetime(2025, 1, 1, tzinfo=UTC),
        to_timestamp=datetime.datetime(2025, 1, 1, 1, 0, tzinfo=UTC),
    )
    coordinator = make_coordinator(
        hass,
        {PPID: make_charger(delegated_control_status="ACTIVE", smart_schedule_windows=[past])},
    )
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)
    assert entity.event is None  # only event is entirely in the past


async def test_no_charger_returns_no_events_and_no_current_event(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {})
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)
    assert entity.event is None
    events = await entity.async_get_events(
        hass, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )
    assert events == []


async def test_boost_shown_alongside_the_schedule(hass: HomeAssistant) -> None:
    """A scheduled boost appears as its own event, and is the next event before it starts."""
    window = make_smart_window(
        from_timestamp=datetime.datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        to_timestamp=datetime.datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
    )
    boost_start = datetime.datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    coordinator = make_coordinator(
        hass,
        {
            PPID: make_charger(
                delegated_control_status="ACTIVE",
                smart_schedule_windows=[window],
                boost_start_at=boost_start,
                boost_end_at=boost_start + datetime.timedelta(hours=1),
            )
        },
    )
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)

    events = await entity.async_get_events(
        hass, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert [e.start for e in events] == [window.from_timestamp, boost_start]
    assert events[1].summary == "Boost charging"
    assert events[1].end == boost_start + datetime.timedelta(hours=1)


async def test_planned_boost_window_merged_into_one_event(hass: HomeAssistant) -> None:
    """Smart Charging plans the boost as a schedule window; it shows once, named as a boost."""
    boost_start = datetime.datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
    boost_end = boost_start + datetime.timedelta(hours=1)
    window = make_smart_window(
        from_timestamp=boost_start, to_timestamp=boost_end, tariff_rate="PEAK"
    )
    coordinator = make_coordinator(
        hass,
        {
            PPID: make_charger(
                delegated_control_status="ACTIVE",
                smart_schedule_windows=[window],
                boost_start_at=boost_start,
                boost_end_at=boost_end,
            )
        },
    )
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)

    events = await entity.async_get_events(
        hass, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert [(e.summary, e.start, e.end) for e in events] == [
        ("Boost charging (Peak)", boost_start, boost_end)
    ]


async def test_no_boost_event_without_an_end(hass: HomeAssistant) -> None:
    """Always On has no end time, so it gets no event."""
    coordinator = make_coordinator(
        hass,
        {
            PPID: make_charger(
                delegated_control_status="INACTIVE",
                manual_schedule_windows=[],
                boost_start_at=datetime.datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
                boost_end_at=None,
            )
        },
    )
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)

    events = await entity.async_get_events(
        hass, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert events == []


async def test_planned_boost_split_across_rates_merged(hass: HomeAssistant) -> None:
    """A boost started "now" (with seconds) and planned as two windows shows as two boost
    windows, with no separate event."""
    boost_start = datetime.datetime(2026, 1, 1, 6, 59, 30, tzinfo=UTC)
    boost_end = boost_start + datetime.timedelta(hours=1)
    peak = make_smart_window(
        from_timestamp=datetime.datetime(2026, 1, 1, 7, 0, tzinfo=UTC),
        to_timestamp=datetime.datetime(2026, 1, 1, 7, 30, tzinfo=UTC),
        tariff_rate="PEAK",
    )
    off_peak = make_smart_window(
        from_timestamp=datetime.datetime(2026, 1, 1, 7, 30, tzinfo=UTC),
        to_timestamp=datetime.datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        tariff_rate="OFF_PEAK",
    )
    coordinator = make_coordinator(
        hass,
        {
            PPID: make_charger(
                delegated_control_status="ACTIVE",
                smart_schedule_windows=[peak, off_peak],
                boost_start_at=boost_start,
                boost_end_at=boost_end,
            )
        },
    )
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)

    events = await entity.async_get_events(
        hass, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert [e.summary for e in events] == ["Boost charging (Peak)", "Boost charging (Off-peak)"]


async def test_basic_schedule_window_not_renamed_by_a_boost(hass: HomeAssistant) -> None:
    """In Basic Charging the manual schedule stays as it is; the boost is its own event."""
    window = make_manual_window(start_day=4, start_time="00:30:00", end_day=4, end_time="04:30:00")
    boost_start = datetime.datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
    coordinator = make_coordinator(
        hass,
        {
            PPID: make_charger(
                delegated_control_status="INACTIVE",
                timezone=UTC,
                manual_schedule_windows=[window],
                boost_start_at=boost_start,
                boost_end_at=boost_start + datetime.timedelta(hours=4),
            )
        },
    )
    entity = calendar.PodHomeScheduleCalendar(coordinator, PPID)

    events = await entity.async_get_events(
        hass, datetime.datetime(2026, 1, 1, tzinfo=UTC), datetime.datetime(2026, 1, 2, tzinfo=UTC)
    )

    assert "Boost charging" in [e.summary for e in events]
    assert any(e.summary != "Boost charging" for e in events)
