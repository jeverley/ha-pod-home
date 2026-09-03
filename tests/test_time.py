"""Time entity tests, using pytest-homeassistant-custom-component's real `hass` fixture and the
shared factories in tests/_fixtures.py. PodHomeBoostDurationTime's async_write_ha_state() calls
are neutralized (the entity is constructed directly, not through a real platform add, so it has
no entity_id for HA to write state against) - these tests exercise the entity's own value/reset
logic, not the full add-to-hass/RestoreEntity lifecycle.
"""
from __future__ import annotations

import datetime

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

import custom_components.pod_home.time as time_platform
from tests._fixtures import make_charger, make_coordinator, make_vehicle

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


async def test_ready_by_native_value_from_intent(hass: HomeAssistant) -> None:
    vehicle = make_vehicle(id="v1", intent_charge_by_time="07:30:00")
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    entity = time_platform.PodHomeVehicleReadyByTime(coordinator, "v1")
    assert entity.native_value == datetime.time(7, 30, 0)


async def test_ready_by_set_value_writes_intents_with_echoed_kwh(hass: HomeAssistant) -> None:
    vehicle = make_vehicle(id="v1", intent_charge_kwh=42.344)
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    coordinator.async_request_refresh = AsyncMock()
    entity = time_platform.PodHomeVehicleReadyByTime(coordinator, "v1")

    await entity.async_set_value(datetime.time(8, 0, 0))

    coordinator.api.async_set_vehicle_intents.assert_called_once()
    call_ppid, call_vehicle_id, call_details = coordinator.api.async_set_vehicle_intents.call_args[0]
    assert call_ppid == PPID
    assert call_vehicle_id == "v1"
    assert len(call_details) == 7  # fanned across all 7 days
    assert all(d["chargeByTime"] == "08:00:00" and d["chargeKWh"] == 42.34 for d in call_details)
    coordinator.async_request_refresh.assert_called_once()


async def test_ready_by_set_value_without_known_kwh_raises(hass: HomeAssistant) -> None:
    vehicle = make_vehicle(id="v1", intent_charge_kwh=None)
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    entity = time_platform.PodHomeVehicleReadyByTime(coordinator, "v1")
    with pytest.raises(HomeAssistantError):
        await entity.async_set_value(datetime.time(8, 0, 0))


async def test_ready_by_available_only_in_smart_charging_mode(hass: HomeAssistant) -> None:
    vehicle = make_vehicle(id="v1")
    coordinator = make_coordinator(
        hass, {PPID: make_charger(vehicle=vehicle, delegated_control_status="ACTIVE")}
    )
    entity = time_platform.PodHomeVehicleReadyByTime(coordinator, "v1")
    assert entity.available is True

    coordinator.data = {
        PPID: make_charger(vehicle=vehicle, delegated_control_status="INACTIVE")
    }
    assert entity.available is False

    # Unrecognized/unresolved status isn't guessed away - stays available.
    coordinator.data = {PPID: make_charger(vehicle=vehicle, delegated_control_status="PENDING")}
    assert entity.available is True


async def test_boost_duration_defaults_unset(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    entity = time_platform.PodHomeBoostDurationTime(coordinator, PPID)
    assert entity.native_value is None


async def test_boost_duration_available_only_with_cable_connected(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(charging_state="Charging")})
    entity = time_platform.PodHomeBoostDurationTime(coordinator, PPID)
    assert entity.available is True

    coordinator.data = {PPID: make_charger(charging_state="Available")}  # cable unplugged
    assert entity.available is False


async def test_boost_duration_set_value_then_reset(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    entity = time_platform.PodHomeBoostDurationTime(coordinator, PPID)
    entity.async_write_ha_state = lambda: None  # not added to hass in this test

    await entity.async_set_value(datetime.time(0, 30, 0))
    assert entity.native_value == datetime.time(0, 30, 0)

    await entity.async_reset()
    assert entity.native_value is None
