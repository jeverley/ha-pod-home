"""Binary sensor entity tests, using pytest-homeassistant-custom-component's real `hass` fixture
and the shared factories in tests/_fixtures.py."""
from __future__ import annotations

import datetime

import pytest
from homeassistant.core import HomeAssistant

import custom_components.pod_home.binary_sensor as binary_sensor
from tests._fixtures import make_charger, make_coordinator, make_vehicle

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


async def test_connectivity_sensor_on_off_and_icon(hass: HomeAssistant) -> None:
    last_seen = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    coordinator = make_coordinator(
        hass, {PPID: make_charger(connection_state="Online", last_seen_at=last_seen)}
    )
    entity = binary_sensor.PodHomeConnectivitySensor(coordinator, PPID)
    assert entity.is_on is True
    assert entity.icon == "mdi:cloud-check-variant"
    assert entity.extra_state_attributes == {"last_seen": last_seen}

    coordinator.data = {PPID: make_charger(connection_state="Offline")}
    assert entity.is_on is False
    assert entity.icon == "mdi:cloud-off"


async def test_cable_connected_sensor_maps_charging_state(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(charging_state="Charging")})
    entity = binary_sensor.PodHomeCableConnectedSensor(coordinator, PPID)
    assert entity.is_on is True  # Charging implies a cable is connected

    coordinator.data = {PPID: make_charger(charging_state="Available")}
    assert entity.is_on is False  # Available implies no cable

    coordinator.data = {PPID: make_charger(charging_state="Faulted")}
    assert entity.is_on is None  # ambiguous - not False/True, per CHARGING_STATE_CABLE_CONNECTED

    coordinator.data = {PPID: make_charger(charging_state=None)}
    assert entity.is_on is None


async def test_vehicle_charging_sensor(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(
        hass, {PPID: make_charger(vehicle=make_vehicle(id="v1", is_charging=True))}
    )
    entity = binary_sensor.PodHomeVehicleChargingSensor(coordinator, "v1")
    assert entity.is_on is True

    coordinator.data = {}
    assert entity.is_on is None  # no linked vehicle found
