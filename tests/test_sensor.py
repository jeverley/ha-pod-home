"""Sensor entity tests, using pytest-homeassistant-custom-component's real `hass` fixture and
the shared dataclass factories in tests/_fixtures.py. Focuses on entities with genuine logic
(derived values, conditional attributes, unit/currency handling) - a sensor that's a pure
passthrough of one charger/vehicle field gets one assertion, not exhaustive coverage.
"""
from __future__ import annotations

import datetime

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.pod_home.const import (
    CHARGER_STATUS_AVAILABLE,
    CHARGER_STATUS_CHARGING,
)
import custom_components.pod_home.sensor as sensor
from tests._fixtures import make_charge, make_charger, make_coordinator, make_tariff_window, make_vehicle

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"
UTC = datetime.timezone.utc


async def test_charging_state_sensor_known_and_unrecognized(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(charging_state="Available")})
    entity = sensor.PodHomeChargingStateSensor(coordinator, PPID)
    assert entity.native_value == "Available"
    assert entity.extra_state_attributes is None

    coordinator.data = {PPID: make_charger(charging_state="SomethingNew")}
    assert entity.native_value is None  # not in CHARGING_STATE_OPTIONS
    assert entity.extra_state_attributes == {"raw_charging_state": "SomethingNew"}


async def test_charging_mode_sensor(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(
        hass,
        {
            PPID: make_charger(
                delegated_control_status="ACTIVE",
                delegated_control_status_effective_from=datetime.datetime(2026, 1, 1, tzinfo=UTC),
                smart_charging_supported=True,
            )
        },
    )
    entity = sensor.PodHomeChargingModeSensor(coordinator, PPID)
    assert entity.native_value == "smart"
    assert entity.extra_state_attributes == {
        "raw_delegated_control_status": "ACTIVE",
        "status_effective_from": "2026-01-01T00:00:00+00:00",
        "smart_charging_supported": True,
    }


async def test_status_sensor_icon_mapping(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(charging_state="Charging")})
    entity = sensor.PodHomeStatusSensor(coordinator, PPID)
    assert entity.native_value == CHARGER_STATUS_CHARGING
    assert entity.icon == "mdi:ev-station"

    coordinator.data = {PPID: make_charger(charging_state="Available")}
    assert entity.native_value == CHARGER_STATUS_AVAILABLE
    assert entity.icon == "mdi:power-plug-off"


async def test_last_charge_sensors_prefer_current_over_latest(hass: HomeAssistant) -> None:
    # Different started_at values - two genuinely different sessions (current_charge is a new,
    # still-open one mobile-api hasn't reported back yet), not the "same session, latest already
    # finalized" case select_last_charge() specifically prefers latest_charge for.
    current = make_charge(
        id="c-current", started_at=datetime.datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        energy_total=1.5, duration=600, cost_amount=None,
    )
    latest = make_charge(
        id="c-latest", started_at=datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        energy_total=9.9, duration=9999, cost_amount=999,
    )
    coordinator = make_coordinator(
        hass, {PPID: make_charger(current_charge=current, latest_charge=latest)}
    )
    duration = sensor.PodHomeLastChargeDurationSensor(coordinator, PPID)
    energy = sensor.PodHomeLastChargeEnergySensor(coordinator, PPID)
    cost = sensor.PodHomeLastChargeCostSensor(coordinator, PPID)

    assert duration.native_value == 600
    assert energy.native_value == 1.5
    # current_charge.cost_amount is None while live - cost stays None, not misleadingly 0.
    assert cost.native_value is None


async def test_last_charge_cost_converts_minor_units_and_currency(hass: HomeAssistant) -> None:
    finished = make_charge(cost_amount=1234, cost_currency="GBP", ended_at=datetime.datetime(2026, 1, 1, tzinfo=UTC))
    coordinator = make_coordinator(
        hass, {PPID: make_charger(current_charge=None, latest_charge=finished)}
    )
    cost = sensor.PodHomeLastChargeCostSensor(coordinator, PPID)
    assert cost.native_value == 12.34
    assert cost.native_unit_of_measurement == "GBP"


async def test_month_cost_sensor_last_reset_and_currency_fallback(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(
        hass, {PPID: make_charger(month_cost_amount=500, timezone="Europe/London")}
    )
    entity = sensor.PodHomeMonthCostSensor(coordinator, PPID)
    assert entity.native_value == 5.0
    assert entity.native_unit_of_measurement == "GBP"  # DEFAULT_CURRENCY, coordinator.currency unset
    reset = entity.last_reset
    assert reset.day == 1 and reset.hour == 0 and reset.minute == 0

    coordinator.currency = "EUR"
    assert entity.native_unit_of_measurement == "EUR"


async def test_total_energy_sensor_adds_live_session_only_when_current(hass: HomeAssistant) -> None:
    current = make_charge(energy_total=3.0, ended_at=None)
    coordinator = make_coordinator(
        hass,
        {
            PPID: make_charger(
                total_energy_kwh=50.0,
                current_charge=current,
                latest_charge=None,
                total_started_at=datetime.datetime(2026, 1, 1, tzinfo=UTC),
            )
        },
    )
    entity = sensor.PodHomeTotalEnergySensor(coordinator, PPID)
    assert entity.native_value == 53.0  # 50 running total + 3 live session on top
    assert entity.extra_state_attributes == {"tracking_started_at": "2026-01-01T00:00:00+00:00"}

    # Once mobile-api reports the SAME session finished (latest_charge takes over per
    # select_last_charge), it must not be double-counted on top of total_energy_kwh.
    finished = make_charge(
        id=current.id, started_at=current.started_at, energy_total=3.0,
        ended_at=datetime.datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
    )
    coordinator.data = {
        PPID: make_charger(total_energy_kwh=53.0, current_charge=current, latest_charge=finished)
    }
    assert entity.native_value == 53.0


async def test_electricity_rate_sensor_picks_current_window_and_cheapest_flag(
    hass: HomeAssistant,
) -> None:
    cheap = make_tariff_window(days=["MONDAY"], start="00:30:00", end="05:30:00", price=0.0863)
    expensive = make_tariff_window(days=["MONDAY"], start="05:30:00", end="00:30:00", price=0.2942)
    coordinator = make_coordinator(
        hass, {PPID: make_charger(tariff_windows=[cheap, expensive], timezone="UTC")}
    )
    entity = sensor.PodHomeElectricityRateSensor(coordinator, PPID)

    with freeze_time("2026-01-05 02:00:00"):  # a Monday, inside the cheap window
        assert entity.native_value == 0.0863
        assert entity.extra_state_attributes == {"is_cheapest_rate": True}

    with freeze_time("2026-01-05 10:00:00"):  # inside the expensive (wrapping) window
        assert entity.native_value == 0.2942
        assert entity.extra_state_attributes == {"is_cheapest_rate": False}


async def test_electricity_rate_available_regardless_of_charging_mode(hass: HomeAssistant) -> None:
    """NOT mode-gated - the rate is a tariff property, applicable in Basic mode too."""
    coordinator = make_coordinator(
        hass, {PPID: make_charger(delegated_control_status="INACTIVE")}
    )
    entity = sensor.PodHomeElectricityRateSensor(coordinator, PPID)
    assert entity.available is True


async def test_vehicle_range_odometer_suggested_unit_from_account_preference(
    hass: HomeAssistant,
) -> None:
    vehicle = make_vehicle(id="v1", range_km=100.0, odometer_km=5000.0)
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    range_sensor = sensor.PodHomeVehicleRangeSensor(coordinator, "v1")
    odometer_sensor = sensor.PodHomeVehicleOdometerSensor(coordinator, "v1")

    assert range_sensor.native_value == 100.0
    assert range_sensor.suggested_unit_of_measurement is None  # unit_of_distance unset

    coordinator.unit_of_distance = "mi"
    assert range_sensor.suggested_unit_of_measurement == "mi"
    assert odometer_sensor.suggested_unit_of_measurement == "mi"


async def test_vehicle_battery_synced_at_attribute(hass: HomeAssistant) -> None:
    synced = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    vehicle = make_vehicle(id="v1", battery_level_percent=42, synced_at=synced)
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    entity = sensor.PodHomeVehicleBatteryLevelSensor(coordinator, "v1")
    assert entity.native_value == 42
    assert entity.extra_state_attributes == {"synced_at": "2026-01-01T12:00:00+00:00"}


async def test_vehicle_expected_charge_attributes(hass: HomeAssistant) -> None:
    vehicle = make_vehicle(
        id="v1", expected_charge_percent=70, can_meet_target=False,
        cannot_meet_target_reason="PRICE",
    )
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    entity = sensor.PodHomeVehicleExpectedChargeSensor(coordinator, "v1")
    assert entity.native_value == 70
    assert entity.extra_state_attributes == {
        "can_meet_target": False,
        "cannot_meet_target_reason": "PRICE",
    }


async def test_expected_charge_available_only_in_smart_charging_mode(hass: HomeAssistant) -> None:
    vehicle = make_vehicle(id="v1")
    coordinator = make_coordinator(
        hass, {PPID: make_charger(vehicle=vehicle, delegated_control_status="ACTIVE")}
    )
    entity = sensor.PodHomeVehicleExpectedChargeSensor(coordinator, "v1")
    assert entity.available is True

    coordinator.data = {
        PPID: make_charger(vehicle=vehicle, delegated_control_status="INACTIVE")
    }
    assert entity.available is False


async def test_vehicle_power_delivery_state_known_or_none(hass: HomeAssistant) -> None:
    vehicle = make_vehicle(id="v1", power_delivery_state="PLUGGED_IN:STOPPED")
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    entity = sensor.PodHomeVehiclePowerDeliveryStateSensor(coordinator, "v1")
    assert entity.native_value == "PLUGGED_IN:STOPPED"

    coordinator.data = {PPID: make_charger(vehicle=make_vehicle(id="v1", power_delivery_state="NOT_SEEN_BEFORE"))}
    assert entity.native_value is None


async def test_rewards_balance_sensor_unavailable_until_first_fetch(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {})
    entity = sensor.PodHomeRewardsBalanceSensor(coordinator)
    assert entity.available is False
    assert entity.native_value is None

    from custom_components.pod_home.coordinator import PodHomeRewards

    coordinator.rewards = PodHomeRewards(
        balance_gbp=12.5, balance_miles=100, balance_points=50,
        allowance_balance_gbp=5.0, annual_allowance_gbp=50.0, payout_threshold_gbp=10.0,
    )
    assert entity.available is True
    assert entity.native_value == 12.5
    assert entity.extra_state_attributes == {
        "balance_miles": 100,
        "balance_points": 50,
        "allowance_balance_gbp": 5.0,
        "annual_allowance_gbp": 50.0,
        "payout_threshold_gbp": 10.0,
    }
