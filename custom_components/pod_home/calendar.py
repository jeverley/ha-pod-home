"""Calendar platform for pod_home - one mode-aware Schedule calendar."""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SCHEDULE_MODE_BASIC_CHARGING, SCHEDULE_MODE_SMART_CHARGING
from .entity import PodHomeEntity, async_setup_dynamic_chargers
from .helpers import expand_manual_schedule_events, resolve_timezone, schedule_mode, smart_schedule_events

if TYPE_CHECKING:
    from . import PodHomeConfigEntry
    from .coordinator import PodHomeCharger

PARALLEL_UPDATES = 0

# How far ahead to look for the "next" event when nothing is currently in progress - the
# `event` property has no natural range to work with the way async_get_events() does.
_UPCOMING_WINDOW = datetime.timedelta(days=8)


async def async_setup_entry(
    hass: HomeAssistant, entry: PodHomeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_dynamic_chargers(
        entry,
        entry.runtime_data,
        async_add_entities,
        [PodHomeScheduleCalendar],
    )


class PodHomeScheduleCalendar(PodHomeEntity, CalendarEntity):
    """The mode-appropriate charge schedule as real calendar events - not mode-gated; branches on
    `schedule_mode()` internally instead of splitting into two mode-specific entities (see
    expand_manual_schedule_events()/smart_schedule_events() in helpers.py for the two branches).
    Empty outside an active/recent Smart Charging session, or on an unrecognized status - a
    correct empty result in both cases, not an error."""

    _attr_translation_key = "schedule"
    _attr_name = "Schedule"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_schedule_calendar"

    @property
    def event(self) -> CalendarEvent | None:
        charger = self.charger
        if not charger:
            return None
        now = dt_util.utcnow()
        for start, end, summary in self._events_for_range(charger, now, now + _UPCOMING_WINDOW):
            if end > now:
                return CalendarEvent(start=start, end=end, summary=summary)
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime.datetime, end_date: datetime.datetime
    ) -> list[CalendarEvent]:
        charger = self.charger
        if not charger:
            return []
        return [
            CalendarEvent(start=start, end=end, summary=summary)
            for start, end, summary in self._events_for_range(charger, start_date, end_date)
        ]

    def _events_for_range(
        self, charger: "PodHomeCharger", start_date: datetime.datetime, end_date: datetime.datetime
    ) -> list[tuple[datetime.datetime, datetime.datetime, str]]:
        mode = schedule_mode(charger.delegated_control_status)
        if mode == SCHEDULE_MODE_SMART_CHARGING:
            return smart_schedule_events(charger.smart_schedule_windows, start_date, end_date)
        if mode == SCHEDULE_MODE_BASIC_CHARGING:
            tz = resolve_timezone(charger.timezone) or dt_util.DEFAULT_TIME_ZONE
            return expand_manual_schedule_events(
                charger.manual_schedule_windows,
                start_date.astimezone(tz).date(),
                end_date.astimezone(tz).date(),
                tz,
            )
        return []
