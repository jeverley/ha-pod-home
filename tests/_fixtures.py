"""Shared factory functions for constructing real coordinator.py dataclasses with sensible
defaults, for entity/coordinator tests in the HA-enabled tests/ tree. Prefer these over
SimpleNamespace (used only by tests/test_helpers.py, which can't import coordinator.py at all -
see that file's own docstring) since the real dataclasses are importable here and give
type-accurate fixtures that won't silently drift from a renamed/added field.
"""
from __future__ import annotations

import datetime
from unittest.mock import create_autospec

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pod_home.const import DOMAIN
from custom_components.pod_home.coordinator import (
    PodHomeCharge,
    PodHomeCharger,
    PodHomeDataUpdateCoordinator,
    PodHomeFirmware,
    PodHomeManualScheduleWindow,
    PodHomeSmartScheduleWindow,
    PodHomeTariffWindow,
    PodHomeVehicle,
)
from custom_components.pod_home.podpoint_mobile_api import PodHomeApiClient

UTC = datetime.timezone.utc


def make_coordinator(
    hass: HomeAssistant, data: dict[str, PodHomeCharger] | None = None
) -> PodHomeDataUpdateCoordinator:
    """A coordinator with a fully-autospecced (unused) API client and `.data` preset directly -
    for entity tests that only care about reading already-fetched data, not the fetch itself
    (see tests/test_coordinator.py for testing the fetch/parse logic)."""
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "driver@example.com", "password": "x"})
    entry.add_to_hass(hass)
    api = create_autospec(PodHomeApiClient, instance=True)
    coordinator = PodHomeDataUpdateCoordinator(
        hass, entry, api, email="driver@example.com", password="x"
    )
    coordinator.data = data if data is not None else {}
    return coordinator


def make_charge(**overrides) -> PodHomeCharge:
    defaults = dict(
        id="charge-1",
        started_at=datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        ended_at=datetime.datetime(2026, 1, 1, 13, 0, tzinfo=UTC),
        duration=3600,
        energy_total=5.0,
        cost_amount=150,
        cost_currency="GBP",
        plugged_in_at=datetime.datetime(2026, 1, 1, 11, 55, tzinfo=UTC),
        unplugged_at=None,
    )
    defaults.update(overrides)
    return PodHomeCharge(**defaults)


def make_firmware(**overrides) -> PodHomeFirmware:
    defaults = dict(manifest_id="A30P-1.0", update_available=False, serial_number="SN123")
    defaults.update(overrides)
    return PodHomeFirmware(**defaults)


def make_tariff_window(**overrides) -> PodHomeTariffWindow:
    defaults = dict(days=["MONDAY"], start="00:30:00", end="05:30:00", price=0.0863)
    defaults.update(overrides)
    return PodHomeTariffWindow(**defaults)


def make_manual_window(**overrides) -> PodHomeManualScheduleWindow:
    defaults = dict(
        uid="w1", start_day=1, start_time="00:30:00", end_day=1, end_time="05:30:00",
        is_active=True,
    )
    defaults.update(overrides)
    return PodHomeManualScheduleWindow(**defaults)


def make_smart_window(**overrides) -> PodHomeSmartScheduleWindow:
    defaults = dict(
        type="CHARGING",
        timestamp=None,
        from_timestamp=datetime.datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        to_timestamp=datetime.datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
        tariff_rate="OFF_PEAK",
    )
    defaults.update(overrides)
    return PodHomeSmartScheduleWindow(**defaults)


def make_vehicle(**overrides) -> PodHomeVehicle:
    defaults = dict(
        id="vehicle-1",
        display_name="My EV",
        brand="Tesla",
        model="Model 3",
        battery_capacity_kwh=75.0,
        battery_level_percent=60,
        range_km=200.0,
        is_charging=False,
        odometer_km=12345.0,
        ready_by=None,
        is_plugged_in_to_this_charger=True,
        charge_limit_percent=80,
        charge_limit_source="user",
        expected_charge_percent=80,
        can_meet_target=True,
        cannot_meet_target_reason=None,
        power_delivery_state="PLUGGED_IN:CHARGING",
        is_fully_charged=False,
        charge_rate=7.4,
        max_current=32.0,
        charge_time_remaining=60,
        intent_charge_by_time="07:00:00",
        intent_charge_kwh=45.0,
        synced_at=datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
    )
    defaults.update(overrides)
    return PodHomeVehicle(**defaults)


def make_charger(**overrides) -> PodHomeCharger:
    defaults = dict(
        ppid="PSL-000001",
        unit_id=12345,
        timezone="Europe/London",
        model_style="solo3",
        model_colour="black",
        architecture="3.0",
        connection_state="Online",
        charging_state="Available",
        delegated_control_status="ACTIVE",
        delegated_control_status_effective_from=None,
        charging_started_at=None,
        cable_unplugged_at=None,
        charge_finished_at=None,
        connection_quality=5,
        last_seen_at=datetime.datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        latest_charge=None,
        current_charge=None,
        month_energy_kwh=10.0,
        month_cost_amount=300,
        total_energy_kwh=100.0,
        total_started_at=datetime.datetime(2025, 12, 1, tzinfo=UTC),
        firmware=None,
        tariff_windows=None,
        manual_schedule_windows=None,
        smart_schedule_windows=None,
        vehicle=None,
        max_price=None,
        boost_end_at=None,
        smart_charging_supported=None,
        remote_lock_off_mode=None,
    )
    defaults.update(overrides)
    return PodHomeCharger(**defaults)
