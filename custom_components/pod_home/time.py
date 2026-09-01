"""Time platform for pod_home - the settable Ready By entity."""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import (
    PodHomeVehicleEntity,
    PodHomeVehicleIntentsWriteMixin,
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
