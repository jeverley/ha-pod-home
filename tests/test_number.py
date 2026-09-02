"""Number entity tests, using pytest-homeassistant-custom-component's real `hass` fixture and
the shared factories in tests/_fixtures.py."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

import custom_components.pod_home.number as number
from tests._fixtures import make_charger, make_coordinator, make_vehicle

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


async def test_target_charge_native_value_and_attributes(hass: HomeAssistant) -> None:
    vehicle = make_vehicle(id="v1", charge_limit_percent=80, charge_limit_source="user")
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    entity = number.PodHomeVehicleTargetChargeNumber(coordinator, "v1")
    assert entity.native_value == 80
    assert entity.extra_state_attributes == {"charge_limit_source": "user"}


async def test_target_charge_set_value_rounds_and_refreshes(hass: HomeAssistant) -> None:
    vehicle = make_vehicle(id="v1")
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    coordinator.async_request_refresh = AsyncMock()
    entity = number.PodHomeVehicleTargetChargeNumber(coordinator, "v1")

    await entity.async_set_native_value(72.6)

    coordinator.api.async_set_vehicle_charge_limit.assert_called_once_with(PPID, "v1", 73)
    coordinator.async_request_refresh.assert_called_once()


async def test_target_charge_set_value_without_vehicle_raises(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {})
    entity = number.PodHomeVehicleTargetChargeNumber(coordinator, "v1")
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(50)
