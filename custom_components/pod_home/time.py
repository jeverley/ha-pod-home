"""Time platform for pod_home - the settable Ready By entity."""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.time import TimeEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DAY_OF_WEEK_OPTIONS, DOMAIN
from .coordinator import PodHomeDataUpdateCoordinator
from .entity import (
    PodHomeEntity,
    PodHomeOptimisticWriteMixin,
    PodHomeVehicleEntity,
    async_handle_write_auth_error,
    async_setup_dynamic_chargers,
    async_setup_dynamic_vehicles,
)
from .helpers import parse_time_of_day
from .podpoint_mobile_api import PodHomeAuthError

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: PodHomeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_dynamic_vehicles(
        entry,
        entry.runtime_data,
        async_add_entities,
        [PodHomeVehicleReadyByTime],
    )
    async_setup_dynamic_chargers(
        entry,
        entry.runtime_data,
        async_add_entities,
        [PodHomeBoostDurationTime],
    )


def _build_intent_details(charge_by_time: str, charge_kwh: float) -> list[dict[str, Any]]:
    """Builds the intents payload, fanning one chargeByTime/chargeKWh pair across all 7 days."""
    return [
        {
            "dayOfWeek": day,
            "chargeByTime": charge_by_time,
            "chargeKWh": round(charge_kwh, 2),
        }
        for day in DAY_OF_WEEK_OPTIONS
    ]


class PodHomeVehicleReadyByTime(
    PodHomeOptimisticWriteMixin[datetime.time], PodHomeVehicleEntity, TimeEntity
):
    """Settable Ready By - the local wall-clock time Smart Charging aims to reach Target Charge
    by. Confirmed working live.

    Reads/writes intent_charge_by_time (intents.details[].chargeByTime, a plain "HH:MM:SS"
    local string).

    Writes go through the shared per-day intents endpoint (PUT .../intents, requiring both
    chargeByTime and chargeKWh on every entry), fanned identically across all 7 days - see
    DAY_OF_WEEK_OPTIONS in const.py. Requires vehicle.intent_charge_kwh already known; refuses
    to write (raises) if that value isn't known yet. PodHomeOptimisticWriteMixin masks
    native_value's read-your-own-write race."""

    _attr_translation_key = "vehicle_ready_by"
    _attr_name = "Ready by"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-time-four-outline"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_ready_by"

    @property
    def available(self) -> bool:
        # Smart-Charging-only.
        return super().available and self._smart_mode_available

    @property
    def native_value(self) -> datetime.time | None:
        optimistic = self._read_optimistic_value()
        if optimistic is not None:
            return optimistic
        vehicle = self.vehicle
        return parse_time_of_day(vehicle.intent_charge_by_time) if vehicle else None

    async def async_set_value(self, value: datetime.time) -> None:
        vehicle = self.vehicle
        ppid = self.ppid
        if not vehicle or not ppid:
            raise HomeAssistantError("No linked vehicle to set Ready By for")
        if vehicle.intent_charge_kwh is None:
            raise HomeAssistantError(
                "Current chargeKWh isn't known yet - required for this write (see docstring)"
            )
        intent_details = _build_intent_details(
            value.strftime("%H:%M:%S"), vehicle.intent_charge_kwh
        )
        try:
            await self.coordinator.api.async_set_vehicle_intents(ppid, vehicle.id, intent_details)
        except PodHomeAuthError as exc:
            await async_handle_write_auth_error(self.coordinator, exc)
        # Read-back for this write is the staleness-tiered vehicles fetch - force it so the
        # refresh below actually has a chance to confirm the write.
        self.coordinator.request_vehicles_fetch()
        self._set_optimistic_value(value)
        await self.coordinator.async_request_refresh_after_write()


class PodHomeBoostDurationTime(PodHomeEntity, RestoreEntity, TimeEntity):
    """One-shot input for the "Boost for duration" button (button.py) - not derived from the
    API; this is purely a parameter the button reads at press time. Reuses TimeEntity's hh:mm
    picker to represent a *duration*, not a wall-clock time - HA has no dedicated duration
    domain.

    Unset (None) until explicitly given a value - no default. Persists across restarts via
    RestoreEntity, but button.py resets it back to unset via async_reset() after each successful
    press - "execute this duration" once, not a sticky preference. Registers itself on
    `PodHomeDataUpdateCoordinator.boost_duration_entities` so button.py can call async_reset()
    directly."""

    _attr_translation_key = "boost_duration"
    _attr_name = "Boost duration"
    _attr_icon = "mdi:timer-cog-outline"

    def __init__(self, coordinator: PodHomeDataUpdateCoordinator, ppid: str) -> None:
        super().__init__(coordinator, ppid)
        self._value: datetime.time | None = None

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_boost_duration"

    @property
    def available(self) -> bool:
        # Same cable-unplugged gate as the boost buttons themselves (button.py) - there's
        # nothing to prepare a boost duration for with no cable connected.
        return super().available and self._cable_connected

    @property
    def native_value(self) -> datetime.time | None:
        return self._value

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.boost_duration_entities[self.ppid] = self
        last_state = await self.async_get_last_state()
        if last_state is not None:
            restored = parse_time_of_day(last_state.state)
            if restored is not None:
                self._value = restored

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.boost_duration_entities.pop(self.ppid, None)
        await super().async_will_remove_from_hass()

    async def async_set_value(self, value: datetime.time) -> None:
        self._value = value
        self.async_write_ha_state()

    async def async_reset(self) -> None:
        """Back to unset - called by button.py's Boost for duration after a successful press."""
        self._value = None
        self.async_write_ha_state()
