"""Time platform for pod_home - the settable Ready By entity."""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import (
    PodHomeEntity,
    PodHomeVehicleEntity,
    PodHomeVehicleIntentsWriteMixin,
    async_setup_dynamic_chargers,
    async_setup_dynamic_vehicles,
)
from .helpers import parse_time_of_day

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


class PodHomeVehicleReadyByTime(PodHomeVehicleEntity, PodHomeVehicleIntentsWriteMixin, TimeEntity):
    """Settable Ready By - the local wall-clock time Smart Charging aims to reach Target Charge
    by. NOT YET TESTED against a real account - see DECISIONS.md and
    PodHomeVehicleIntentsWriteMixin's docstring.

    Reads/writes intent_charge_by_time (intents.details[].chargeByTime, a plain "HH:MM:SS"
    local string), not the old sensor's ready_by (currentIntent.readyByTime, which can lag a
    just-written change and carries a date).

    Writes go through the shared per-day intents endpoint, whose entries also require
    chargeKWh even though this entity doesn't change it - echoes back the last-read
    vehicle.intent_charge_kwh rather than recomputing one, and refuses to write (raises) if
    that value isn't known yet."""

    _attr_translation_key = "vehicle_ready_by"
    _attr_name = "Ready by"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-time-four-outline"
    # Smart-Charging-only (see _MODE_GATED_ENTITIES in entity.py).
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_ready_by"

    @property
    def native_value(self) -> datetime.time | None:
        vehicle = self.vehicle
        return parse_time_of_day(vehicle.intent_charge_by_time) if vehicle else None

    async def async_set_value(self, value: datetime.time) -> None:
        vehicle = self.vehicle
        if not vehicle:
            raise HomeAssistantError("No linked vehicle to set Ready By for")
        if vehicle.intent_charge_kwh is None:
            raise HomeAssistantError(
                "Current chargeKWh isn't known yet - required for this write (see docstring)"
            )
        await self._async_write_intents(
            charge_by_time=value.strftime("%H:%M:%S"), charge_kwh=vehicle.intent_charge_kwh
        )


class PodHomeBoostDurationTime(PodHomeEntity, RestoreEntity, TimeEntity):
    """One-shot input for the "Boost for duration" button (button.py) - not derived from the
    API, since there's no "configured boost duration" field to read back; this is purely a
    parameter the button reads at press time. Reuses TimeEntity's hh:mm picker to represent a
    *duration* (H hours M minutes), not a wall-clock time - per the user directly, HA has no
    dedicated duration entity domain.

    Deliberately unset (None) until explicitly given a value - no default, matching the app's
    own boost-duration prompt rather than assuming one. Persists across restarts via
    RestoreEntity (so a pending value survives a restart before it's used), but button.py resets
    it back to unset via async_reset() after each successful press - per the user directly, this
    represents "execute this duration" rather than a sticky preference to remember between
    boosts. Registers itself on the coordinator (see PodHomeDataUpdateCoordinator.
    boost_duration_entities) so button.py can call async_reset() directly - pod_home owns both
    entities, so a direct call is the right tool here, not a generic cross-integration service
    (HA's time.set_value requires a real time value, can't clear one)."""

    _attr_translation_key = "boost_duration"
    _attr_name = "Boost duration"
    _attr_icon = "mdi:timer-cog-outline"

    def __init__(self, coordinator, ppid: str) -> None:
        super().__init__(coordinator, ppid)
        self._value: datetime.time | None = None

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_boost_duration"

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
