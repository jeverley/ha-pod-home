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

from .const import CONNECTION_STATE_ONLINE, DOMAIN
from .entity import PodHomeEntity, async_setup_dynamic_chargers

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

# Coordinator-backed, read-only platform - see sensor.py's comment on this same line.
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


class PodHomeConnectivitySensor(PodHomeEntity, BinarySensorEntity):
    """Confirmed live: connectivity-status-v2.connectionState == "Online". lastSeenAt is
    surfaced as an attribute here rather than a standalone sensor - it's diagnostic detail
    about connectivity specifically, not something independently dashboard-worthy. (No prior
    entities have ever run in real HA, so there's no migration cost to this shape.)
    """

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
    """HEURISTIC, not confirmed: on when the most recent /charges entry has a pluggedInAt but
    no unpluggedAt yet. connectivity-status-v2 has no direct cable-present field (this is the
    same problem the old integration's cable-state-when-status-pending branch was working
    around, just with a different, hopefully more reliable, signal). Verify this the next time
    a cable is actually plugged in before trusting it for automations.
    """

    _attr_translation_key = "cable_connected"
    _attr_name = "Cable Status"
    _attr_device_class = BinarySensorDeviceClass.PLUG

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_cable_connected"

    @property
    def is_on(self) -> bool | None:
        charger = self.charger
        if not charger or not charger.latest_charge:
            return None
        return charger.latest_charge.cable_connected
