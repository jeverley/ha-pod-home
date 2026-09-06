"""Binary sensor platform for pod_home."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHARGING_STATE_CABLE_CONNECTED, CONNECTION_STATE_ONLINE, DOMAIN
from .entity import (
    PodHomeEntity,
    PodHomeVehicleEntity,
    async_setup_dynamic_chargers,
    async_setup_dynamic_vehicles,
)

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: PodHomeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_dynamic_chargers(
        entry,
        entry.runtime_data,
        async_add_entities,
        [PodHomeConnectivitySensor, PodHomeCableConnectedSensor],
    )
    async_setup_dynamic_vehicles(
        entry,
        entry.runtime_data,
        async_add_entities,
        [PodHomeVehicleChargingSensor],
    )


class PodHomeConnectivitySensor(PodHomeEntity, BinarySensorEntity):
    """Whether the charger is currently reachable via Pod Point's cloud."""

    _attr_translation_key = "connectivity"
    _attr_name = "Connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_connectivity"

    @property
    def is_on(self) -> bool | None:
        charger = self.charger
        if not charger:
            return None
        return charger.connection_state == CONNECTION_STATE_ONLINE

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        charger = self.charger
        if not charger:
            return None
        return {"last_seen": charger.last_seen_at}

    @property
    def icon(self) -> str:
        return "mdi:cloud-check-variant" if self.is_on else "mdi:cloud-off"


class PodHomeCableConnectedSensor(PodHomeEntity, BinarySensorEntity):
    """Whether a cable is plugged in, derived from chargingState."""

    _attr_translation_key = "cable_connected"
    _attr_name = "Cable status"
    _attr_device_class = BinarySensorDeviceClass.PLUG

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_cable_connected"

    @property
    def is_on(self) -> bool | None:
        charger = self.charger
        if not charger or charger.charging_state is None:
            return None
        return CHARGING_STATE_CABLE_CONNECTED.get(charger.charging_state)


class PodHomeVehicleChargingSensor(PodHomeVehicleEntity, BinarySensorEntity):
    """Whether the vehicle itself reports as charging (via Enode), independent of this
    charger's own chargingState."""

    _attr_translation_key = "vehicle_charging"
    _attr_name = "Charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_charging"

    @property
    def is_on(self) -> bool | None:
        vehicle = self.vehicle
        return vehicle.is_charging if vehicle else None
