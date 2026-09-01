"""Update platform for pod_home."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PodHomeEntity, async_setup_dynamic_chargers

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
        [PodHomeFirmwareUpdateEntity],
    )


class PodHomeFirmwareUpdateEntity(PodHomeEntity, UpdateEntity):
    """Firmware update status.

    The API's update_available is a plain boolean; no real target version string is known, so
    when true, latest_version is set to a placeholder marker distinct from installed_version
    purely so HA's comparison shows "update available".
    """

    _attr_translation_key = "firmware"
    _attr_name = "Firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_firmware"

    @property
    def installed_version(self) -> str | None:
        charger = self.charger
        return charger.firmware.manifest_id if charger and charger.firmware else None

    @property
    def latest_version(self) -> str | None:
        charger = self.charger
        if not charger or not charger.firmware:
            return None
        installed = charger.firmware.manifest_id
        if not charger.firmware.update_available:
            return installed
        return f"{installed} (update available)" if installed else "update available"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        charger = self.charger
        if not charger or not charger.firmware:
            return None
        return {
            "update_available": charger.firmware.update_available,
            # Distinct from the device's own serial_number (the ppid/"PSL number").
            "serial_number": charger.firmware.serial_number,
        }
