"""Number platform for pod_home - settable configuration entities."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PodHomeVehicleEntity, async_setup_dynamic_vehicles
from .helpers import smart_mode_available

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
        [PodHomeVehicleTargetChargeNumber],
    )


class PodHomeVehicleTargetChargeNumber(PodHomeVehicleEntity, NumberEntity):
    """Settable Target Charge - the percentage Smart Charging aims to reach by Ready By.
    Same unique_id/translation_key as the earlier read-only sensor it replaces. Confirmed working
    live - see DECISIONS.md.

    Writes chargeLimitPercent via async_set_vehicle_charge_limit() - a plain percentage, no
    unit conversion, independent of Ready By's own write."""

    _attr_translation_key = "vehicle_target_charge"
    _attr_name = "Target charge"
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    # A 0% target charge is meaningless (nothing for Smart Charging to charge toward).
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_target_charge"

    @property
    def available(self) -> bool:
        # Smart-Charging-only - see smart_mode_available() (helpers.py) and DECISIONS.md.
        charger = self._charger_for_vehicle()
        return super().available and smart_mode_available(
            charger.delegated_control_status if charger else None
        )

    @property
    def native_value(self) -> int | None:
        vehicle = self.vehicle
        return vehicle.charge_limit_percent if vehicle else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        vehicle = self.vehicle
        if not vehicle:
            return None
        return {"charge_limit_source": vehicle.charge_limit_source}

    async def async_set_native_value(self, value: float) -> None:
        vehicle = self.vehicle
        ppid = self.ppid
        if not vehicle or not ppid:
            raise HomeAssistantError("No linked vehicle to set Target Charge for")
        # native_step=1 is only a UI hint; a service call can still supply a fractional value.
        # chargeLimitPercent is a whole percentage, so round before sending.
        await self.coordinator.api.async_set_vehicle_charge_limit(ppid, vehicle.id, round(value))
        await self.coordinator.async_request_refresh()
