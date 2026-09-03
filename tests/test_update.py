"""Update entity tests, using pytest-homeassistant-custom-component's real `hass` fixture and
the shared factories in tests/_fixtures.py."""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

import custom_components.pod_home.update as update
from tests._fixtures import make_charger, make_coordinator, make_firmware

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


async def test_no_update_available_latest_matches_installed(hass: HomeAssistant) -> None:
    firmware = make_firmware(manifest_id="A30P-1.0", update_available=False)
    coordinator = make_coordinator(hass, {PPID: make_charger(firmware=firmware)})
    entity = update.PodHomeFirmwareUpdateEntity(coordinator, PPID)
    assert entity.installed_version == "A30P-1.0"
    assert entity.latest_version == "A30P-1.0"  # matches installed - HA shows "up to date"
    assert entity.extra_state_attributes == {
        "update_available": False,
        "serial_number": firmware.serial_number,
    }


async def test_update_available_latest_differs_from_installed(hass: HomeAssistant) -> None:
    firmware = make_firmware(manifest_id="A30P-1.0", update_available=True)
    coordinator = make_coordinator(hass, {PPID: make_charger(firmware=firmware)})
    entity = update.PodHomeFirmwareUpdateEntity(coordinator, PPID)
    assert entity.latest_version == "A30P-1.0 (update available)"
    assert entity.latest_version != entity.installed_version  # HA shows "update available"


async def test_update_available_with_no_installed_version_known(hass: HomeAssistant) -> None:
    firmware = make_firmware(manifest_id=None, update_available=True)
    coordinator = make_coordinator(hass, {PPID: make_charger(firmware=firmware)})
    entity = update.PodHomeFirmwareUpdateEntity(coordinator, PPID)
    assert entity.installed_version is None
    assert entity.latest_version == "update available"


async def test_no_firmware_data_yet(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(firmware=None)})
    entity = update.PodHomeFirmwareUpdateEntity(coordinator, PPID)
    assert entity.installed_version is None
    assert entity.latest_version is None
    assert entity.extra_state_attributes is None
