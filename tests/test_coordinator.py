"""Coordinator tests, using pytest-homeassistant-custom-component's real `hass` fixture (see
tests/conftest.py). Exercises `_async_fetch_data`/`_async_update_data` directly against a fully
mocked `PodHomeApiClient` (`create_autospec`, so a renamed/removed client method fails loudly
here rather than silently mocking a typo) with realistic response shapes, rather than going
through `async_setup_entry` - keeps focus on the coordinator's own parsing/staleness/error-
handling logic in isolation from config-entry setup (already covered by test_config_flow.py).
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pod_home.const import DOMAIN
from custom_components.pod_home.coordinator import PodHomeDataUpdateCoordinator
from custom_components.pod_home.podpoint_mobile_api import (
    PodHomeApiClient,
    PodHomeApiError,
    PodHomeAuthError,
)

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


def _stub_api() -> PodHomeApiClient:
    """A fully-autospecced client with benign defaults for every call the coordinator might
    make on any given poll - individual tests override only what they care about."""
    api = create_autospec(PodHomeApiClient, instance=True)
    api.async_list_chargers.return_value = []
    api.async_get_users.return_value = {}
    api.async_smart_charging_chargers_and_vehicles.return_value = []
    api.async_charges.return_value = {}
    api.async_create_api3_session.return_value = {}
    api.async_api3_pods.return_value = {}
    api.async_api3_charges.return_value = {}
    api.async_reward_wallet.return_value = {"rewards": {}, "allowance": {}, "payments": {}}
    api.async_smart_charging_preferences.return_value = {}
    api.async_get_charge_overrides.return_value = []
    api.async_connectivity_status.return_value = {}
    api.async_smart_schedule_active.return_value = {}
    api.async_charge_statistics.return_value = {}
    api.async_charger_firmware.return_value = []
    api.async_tariffs.return_value = {}
    api.async_manual_schedules.return_value = {}
    api.async_delegated_control.return_value = {}
    return api


def _make_coordinator(hass: HomeAssistant, api: PodHomeApiClient) -> PodHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "driver@example.com", "password": "x"})
    entry.add_to_hass(hass)
    return PodHomeDataUpdateCoordinator(
        hass, entry, api, email="driver@example.com", password="x"
    )


def _charger_raw(**overrides) -> dict:
    raw = {
        "ppid": PPID,
        "unitId": 12345,
        "timezone": "Europe/London",
        "modelInfo": {"style": "solo3", "colour": "black", "architecture": "3.0"},
        "delegatedControl": {"status": "INACTIVE"},
    }
    raw.update(overrides)
    return raw


async def test_first_refresh_parses_charger_basic_mode(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_connectivity_status.return_value = {
        "chargingState": "Available",
        "connectionState": "Online",
        "connectionQuality": 5,
        "lastSeenAt": "2026-01-01T00:00:00Z",
    }
    api.async_smart_charging_preferences.return_value = {"maxPrice": 0.15}
    api.async_charger_firmware.return_value = [
        {
            "versionInfo": {"manifestId": "A30P-1.0"},
            "updateStatus": {"isUpdateAvailable": False},
            "serialNumber": "SN123",
        }
    ]
    api.async_tariffs.return_value = {
        "data": [
            {
                "smartChargingSupported": True,
                "tariffInfo": [
                    {"days": ["MONDAY"], "start": "00:30:00", "end": "05:30:00", "price": 0.0863}
                ],
            }
        ]
    }
    api.async_manual_schedules.return_value = {
        "data": [
            {
                "uid": "w1",
                "startDay": 1,
                "startTime": "00:30:00",
                "endDay": 1,
                "endTime": "05:30:00",
                "status": {"isActive": True},
            }
        ]
    }
    api.async_charge_statistics.return_value = {"energy": {"totalUsage": 12.3, "cost": 456}}

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    assert set(result) == {PPID}
    charger = result[PPID]
    assert charger.unit_id == 12345
    assert charger.timezone == "Europe/London"
    assert charger.model_style == "solo3"
    assert charger.model_colour == "black"
    assert charger.architecture == "3.0"
    assert charger.connection_state == "Online"
    assert charger.charging_state == "Available"
    assert charger.delegated_control_status == "INACTIVE"
    assert charger.max_price == 0.15
    assert charger.firmware.manifest_id == "A30P-1.0"
    assert charger.firmware.serial_number == "SN123"
    assert charger.tariff_windows[0].price == 0.0863
    assert charger.smart_charging_supported is True
    assert charger.manual_schedule_windows[0].uid == "w1"
    assert charger.month_energy_kwh == 12.3
    assert charger.month_cost_amount == 456
    # Basic Charging mode - smart-schedules/active is meaningless here and must not be called.
    api.async_smart_schedule_active.assert_not_called()
    assert charger.smart_schedule_windows is None


async def test_smart_mode_fetches_smart_schedule(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.return_value = [
        _charger_raw(delegatedControl={"status": "ACTIVE"})
    ]
    api.async_smart_schedule_active.return_value = {
        "schedule": [
            {
                "type": "CHARGING",
                "fromTimestamp": "2026-01-01T01:00:00Z",
                "toTimestamp": "2026-01-01T02:00:00Z",
                "tariffRate": "OFF_PEAK",
            }
        ]
    }

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    api.async_smart_schedule_active.assert_called_once_with(PPID)
    windows = result[PPID].smart_schedule_windows
    assert windows is not None
    assert windows[0].type == "CHARGING"
    assert windows[0].tariff_rate == "OFF_PEAK"


async def test_empty_chargers_first_poll_returns_empty(hass: HomeAssistant) -> None:
    api = _stub_api()  # async_list_chargers already defaults to []
    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()
    assert result == {}


async def test_empty_chargers_keeps_previous_data(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    coordinator = _make_coordinator(hass, api)
    first = await coordinator._async_fetch_data()
    coordinator.data = first  # DataUpdateCoordinator normally does this after a successful poll

    api.async_list_chargers.return_value = []
    second = await coordinator._async_fetch_data()

    assert second == first  # previous data kept, not wiped to {}


async def test_auth_error_raises_config_entry_auth_failed(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.side_effect = PodHomeAuthError("token expired")
    coordinator = _make_coordinator(hass, api)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_connection_error_retries_then_raises_update_failed(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.side_effect = PodHomeApiError(0, "connection refused")
    coordinator = _make_coordinator(hass, api)
    with patch("custom_components.pod_home.coordinator.asyncio.sleep", AsyncMock()):
        with pytest.raises(UpdateFailed):
            await coordinator._async_fetch_data()
    assert api.async_list_chargers.call_count == 3  # CONNECTION_RETRY_ATTEMPTS


async def test_http_error_raises_immediately_without_retry(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.side_effect = PodHomeApiError(500, "server error")
    coordinator = _make_coordinator(hass, api)
    with pytest.raises(UpdateFailed):
        await coordinator._async_fetch_data()
    assert api.async_list_chargers.call_count == 1  # a genuine HTTP error is not retried


async def test_boost_end_at_parsed_from_charge_overrides(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
    api.async_get_charge_overrides.return_value = [
        {"requestedAt": "2026-01-01T00:00:00Z", "endAt": future.isoformat(), "deletedAt": None}
    ]

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    assert result[PPID].boost_end_at == future


async def test_deleted_charge_override_not_current_boost(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
    api.async_get_charge_overrides.return_value = [
        {
            "requestedAt": "2026-01-01T00:00:00Z",
            "endAt": future.isoformat(),
            "deletedAt": "2026-01-01T00:05:00Z",
        }
    ]

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    assert result[PPID].boost_end_at is None
