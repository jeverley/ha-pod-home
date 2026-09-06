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
from tests._fixtures import make_charger, make_coordinator, make_manual_window, make_smart_window

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"
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
                timezone="UTC",
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
