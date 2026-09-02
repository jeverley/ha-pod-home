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
from custom_components.pod_home.coordinator import (
    FAST_POLL_INTERVAL,
    SLOW_POLL_INTERVAL,
    PodHomeDataUpdateCoordinator,
)
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


async def test_firmware_and_tariffs_not_refetched_within_staleness_window(
    hass: HomeAssistant,
) -> None:
    """Firmware/tariffs/manual_schedules/delegated_control are cached on
    FIRMWARE_TARIFF_REFRESH_INTERVAL - a second poll immediately after the first must not
    re-fetch them."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_charger_firmware.return_value = [
        {"versionInfo": {"manifestId": "A"}, "updateStatus": {}, "serialNumber": "S"}
    ]

    coordinator = _make_coordinator(hass, api)
    await coordinator._async_fetch_data()
    assert api.async_charger_firmware.call_count == 1
    assert api.async_tariffs.call_count == 1
    assert api.async_manual_schedules.call_count == 1
    assert api.async_delegated_control.call_count == 1

    await coordinator._async_fetch_data()
    # Still cached - none of the four re-fetched on the very next poll.
    assert api.async_charger_firmware.call_count == 1
    assert api.async_tariffs.call_count == 1
    assert api.async_manual_schedules.call_count == 1
    assert api.async_delegated_control.call_count == 1
    # But per-charger data fetched every poll (preferences, charge_overrides, connectivity) did
    # get called again.
    assert api.async_smart_charging_preferences.call_count == 2


async def test_accumulate_total_energy_sums_finalized_charges_once_each(
    hass: HomeAssistant,
) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)

    entries = [
        (
            PPID,
            {"id": "c1", "endedAt": "2026-01-01T01:00:00Z", "energyTotal": 5.0},
            {},
        ),
        (
            PPID,
            {"id": "c2", "endedAt": "2026-01-01T02:00:00Z", "energyTotal": 3.0},
            {},
        ),
    ]
    coordinator._accumulate_total_energy(entries)
    assert coordinator._total_energy_kwh_by_ppid[PPID] == 8.0

    # Same batch replayed (e.g. a re-poll covering an overlapping lookback window) - already
    # counted (watermark has moved past both), must not double-add.
    coordinator._accumulate_total_energy(entries)
    assert coordinator._total_energy_kwh_by_ppid[PPID] == 8.0


async def test_accumulate_total_energy_skips_still_open_sessions(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    entries = [(PPID, {"id": "c1", "endedAt": None, "energyTotal": 5.0}, {})]
    coordinator._accumulate_total_energy(entries)
    assert PPID not in coordinator._total_energy_kwh_by_ppid


async def test_sticky_state_round_trips_through_store(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    coordinator._charging_started_at_by_ppid[PPID] = now
    coordinator._total_energy_kwh_by_ppid[PPID] = 42.0
    coordinator._total_started_at_by_ppid[PPID] = now

    await coordinator._sticky_store.async_save(coordinator._sticky_state_for_storage())
    await coordinator._total_energy_store.async_save(coordinator._total_energy_state_for_storage())

    # Simulate a restart: clear the in-memory state, then reload from the same Store the save
    # above wrote to - a second coordinator instance would use a different (randomly-generated)
    # MockConfigEntry.entry_id and so a different Store path, not actually testing persistence.
    coordinator._charging_started_at_by_ppid = {}
    coordinator._total_energy_kwh_by_ppid = {}
    coordinator._total_started_at_by_ppid = {}
    await coordinator.async_load_sticky_state()

    assert coordinator._charging_started_at_by_ppid[PPID] == now
    assert coordinator._total_energy_kwh_by_ppid[PPID] == 42.0
    assert coordinator._total_started_at_by_ppid[PPID] == now


async def test_adjust_poll_interval_speeds_up_after_recent_activity(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.update_interval = SLOW_POLL_INTERVAL
    coordinator._last_seen_changed_at[PPID] = datetime.datetime.now(datetime.timezone.utc)
    coordinator._async_adjust_poll_interval()
    assert coordinator.update_interval == FAST_POLL_INTERVAL


async def test_adjust_poll_interval_slows_down_without_recent_activity(
    hass: HomeAssistant,
) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.update_interval = FAST_POLL_INTERVAL
    coordinator._last_seen_changed_at[PPID] = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    coordinator._async_adjust_poll_interval()
    assert coordinator.update_interval == SLOW_POLL_INTERVAL


async def test_api3_charges_matched_to_ppid_via_pod_id(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_create_api3_session.return_value = {"sessions": {"user_id": 999}}
    api.async_api3_pods.return_value = {"pods": [{"ppid": PPID, "unit_id": 555}]}
    api.async_api3_charges.return_value = {
        "charges": [
            {
                "id": 1,
                "ends_at": None,
                "starts_at": "2026-01-01T10:00:00Z",
                "kwh_used": 2.5,
                "pod": {"id": 555},
                "billing_event": {"currency": "GBP"},
            }
        ]
    }

    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime(2026, 1, 1, 11, 0, tzinfo=datetime.timezone.utc)
    await coordinator._async_refresh_api3_account(now)
    await coordinator._async_refresh_api3_charges(now)

    assert coordinator._current_charge_by_ppid[PPID].energy_total == 2.5
    assert coordinator._current_charge_by_ppid[PPID].cost_currency == "GBP"
    assert coordinator._current_charge_by_ppid[PPID].duration == 3600  # 1 hour, from now - starts_at


async def test_api3_charges_unmatched_pod_id_warns_and_stays_empty(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_create_api3_session.return_value = {"sessions": {"user_id": 999}}
    api.async_api3_pods.return_value = {"pods": [{"ppid": PPID, "unit_id": 555}]}
    api.async_api3_charges.return_value = {
        "charges": [
            {
                "id": 1,
                "ends_at": None,
                "starts_at": "2026-01-01T10:00:00Z",
                "kwh_used": 2.5,
                "pod": {"id": 111},  # doesn't match any known unit_id
                "billing_event": {},
            }
        ]
    }

    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    await coordinator._async_refresh_api3_account(now)
    await coordinator._async_refresh_api3_charges(now)

    assert coordinator._current_charge_by_ppid == {}
