"""Coordinator tests, using pytest-homeassistant-custom-component's real `hass` fixture (see
tests/conftest.py). Exercises `_async_fetch_data`/`_async_update_data` directly against a fully
mocked `PodHomeApiClient` (`create_autospec`, so a renamed/removed client method fails loudly
here rather than silently mocking a typo) with realistic response shapes - focused on the
coordinator's own parsing/staleness/error-handling logic, in isolation from config-entry setup.
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, create_autospec, patch
from zoneinfo import ZoneInfo

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
from tests._fixtures import make_charge, make_charger

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
    api.async_get_remote_lock_status.return_value = {}
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


async def test_empty_chargers_still_saves_state_and_adjusts_poll_interval(
    hass: HomeAssistant,
) -> None:
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_fetch_data()

    api.async_list_chargers.return_value = []
    with (
        patch.object(coordinator, "_async_adjust_poll_interval") as mock_adjust,
        patch.object(coordinator._sticky_store, "async_delay_save") as mock_sticky_save,
        patch.object(coordinator._total_energy_store, "async_delay_save") as mock_energy_save,
    ):
        await coordinator._async_fetch_data()

    mock_adjust.assert_called_once()
    mock_sticky_save.assert_called_once()
    mock_energy_save.assert_called_once()


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


async def test_always_on_active_from_endat_less_charge_override(hass: HomeAssistant) -> None:
    """An indefinite override (no endAt at all) is Basic Charging's "Always on" mode, not a
    boost - confirmed live, distinct from test_boost_end_at_parsed_from_charge_overrides above,
    which always carries a real endAt."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_get_charge_overrides.return_value = [
        {"requestedAt": "2026-01-01T00:00:00Z"}
    ]

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    assert result[PPID].always_on_active is True
    assert result[PPID].boost_end_at is None  # not treated as a cancellable boost


async def test_always_on_active_none_when_charge_overrides_never_fetched(
    hass: HomeAssistant,
) -> None:
    """Distinct from confirmed True/False - a charge-overrides fetch that's never succeeded
    leaves always_on_active unknown, not silently False."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_get_charge_overrides.side_effect = PodHomeApiError(500, "server error")

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    assert result[PPID].always_on_active is None


async def test_deleted_endat_less_override_is_not_always_on(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_get_charge_overrides.return_value = [
        {"requestedAt": "2026-01-01T00:00:00Z", "deletedAt": "2026-01-01T00:05:00Z"}
    ]

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    assert result[PPID].always_on_active is False


async def test_firmware_and_tariffs_not_refetched_within_staleness_window(
    hass: HomeAssistant,
) -> None:
    """Firmware/tariffs/manual_schedules/delegated_control are cached on
    FIRMWARE_TARIFF_REFRESH_INTERVAL - a second poll immediately after the first must not
    re-fetch them."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    # Each needs a genuinely parseable (non-empty) response for its _fetched_at to actually get
    # stamped - the coordinator deliberately does NOT cache an empty/unparseable response (see
    # its own comment: "a bad/unexpected response gets retried next poll"), so an empty-dict
    # default (as most _stub_api() calls use) would legitimately keep re-fetching every poll.
    api.async_charger_firmware.return_value = [
        {"versionInfo": {"manifestId": "A"}, "updateStatus": {}, "serialNumber": "S"}
    ]
    api.async_tariffs.return_value = {
        "data": [{"tariffInfo": [{"days": ["MONDAY"], "start": "00:00", "end": "05:00", "price": 0.1}]}]
    }
    api.async_manual_schedules.return_value = {
        "data": [{"uid": "w1", "startDay": 1, "startTime": "00:30:00", "endDay": 1,
                   "endTime": "05:30:00", "status": {"isActive": True}}]
    }
    api.async_delegated_control.return_value = {"statusEffectiveFrom": "2026-01-01T00:00:00Z"}

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
    # But per-charger data fetched every poll (preferences, charge_overrides, connectivity,
    # remote_lock) did get called again.
    assert api.async_smart_charging_preferences.call_count == 2
    assert api.async_get_remote_lock_status.call_count == 2


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


async def test_adjust_poll_interval_speeds_up_after_recent_write(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.update_interval = SLOW_POLL_INTERVAL
    coordinator.mark_recent_write()
    coordinator._async_adjust_poll_interval()
    assert coordinator.update_interval == FAST_POLL_INTERVAL


async def test_adjust_poll_interval_slows_down_without_recent_write(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.update_interval = FAST_POLL_INTERVAL
    coordinator._last_write_at = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    coordinator._async_adjust_poll_interval()
    assert coordinator.update_interval == SLOW_POLL_INTERVAL


async def test_async_request_refresh_after_write_marks_recent_write(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    assert coordinator._last_write_at is None
    with patch.object(coordinator, "async_request_refresh", new=AsyncMock()) as mock_refresh:
        await coordinator.async_request_refresh_after_write()
    assert coordinator._last_write_at is not None
    mock_refresh.assert_awaited_once()


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
    await coordinator._async_refresh_api3_charges(coordinator._api3_user_id, now)

    assert coordinator._current_charge_by_ppid[PPID].energy_total == 2.5
    assert coordinator._current_charge_by_ppid[PPID].cost_currency == "GBP"
    assert coordinator._current_charge_by_ppid[PPID].duration is None


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
    await coordinator._async_refresh_api3_charges(coordinator._api3_user_id, now)

    assert "api3_charges_unmatched" in coordinator._warned_keys
    assert coordinator._current_charge_by_ppid == {}


def _api3_session_stubs(api, *, started_at: datetime.datetime) -> None:
    """Shared setup for the current_charge duration-refinement tests below - a single open
    api3 charge for PPID, started at `started_at`."""
    api.async_create_api3_session.return_value = {"sessions": {"user_id": 999}}
    api.async_api3_pods.return_value = {"pods": [{"ppid": PPID, "unit_id": 555}]}
    api.async_api3_charges.return_value = {
        "charges": [
            {
                "id": 1,
                "ends_at": None,
                "starts_at": started_at.isoformat(),
                "kwh_used": 1.0,
                "pod": {"id": 555},
                "billing_event": {},
            }
        ]
    }


async def test_current_charge_duration_refined_from_smart_schedule(hass: HomeAssistant) -> None:
    """Smart Charging: duration comes from the CHARGING window(s) actually in the schedule, not
    the full time-since-session-start (which would include any paused time)."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw(delegatedControl={"status": "ACTIVE"})]
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    window_end = now - datetime.timedelta(hours=1)
    api.async_smart_schedule_active.return_value = {
        "schedule": [
            {
                "type": "CHARGING",
                "fromTimestamp": session_start.isoformat(),
                "toTimestamp": window_end.isoformat(),
            }
        ]
    }

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    # Only the 1-hour CHARGING window counts, not the full 2h since session start.
    assert result[PPID].current_charge.duration == 3600


async def test_current_charge_duration_unknown_on_first_poll_when_schedule_fetch_fails(
    hass: HomeAssistant,
) -> None:
    """Smart Charging: a schedule is known to exist for this scheme (delegated control active),
    but this poll's schedule fetch itself comes back empty and it's the first poll of this
    session - nothing yet to freeze at, so duration is None rather than a guessed value."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw(delegatedControl={"status": "ACTIVE"})]
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    api.async_smart_schedule_active.return_value = {}

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    assert result[PPID].current_charge.duration is None


async def test_current_charge_duration_freezes_when_schedule_unavailable(
    hass: HomeAssistant,
) -> None:
    """Smart Charging: a poll where the schedule can't be refined against (charger paused
    between windows, or a transient fetch gap) freezes the last known duration rather than
    recomputing time-since-session-start, which would overcount by the paused time and keep
    growing every poll while genuinely not charging."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw(delegatedControl={"status": "ACTIVE"})]
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    window_end = now - datetime.timedelta(hours=1)
    api.async_smart_schedule_active.return_value = {
        "schedule": [
            {
                "type": "CHARGING",
                "fromTimestamp": session_start.isoformat(),
                "toTimestamp": window_end.isoformat(),
            }
        ]
    }

    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_fetch_data()  # commit, as a real refresh would
    assert coordinator._current_charge_by_ppid[PPID].duration == 3600

    # Now paused between windows - the schedule endpoint has nothing to offer this poll, and
    # the api3-charges refetch (still forced here) would reset duration to None if not for the
    # freeze at the previous poll's committed value.
    api.async_connectivity_status.return_value = {"chargingState": "SuspendedEVSE"}
    api.async_smart_schedule_active.return_value = {}
    coordinator._api3_charges_fetched_at = None

    result = await coordinator._async_fetch_data()

    assert result[PPID].current_charge.duration == 3600


async def test_current_charge_duration_unknown_when_nothing_to_refine_against_in_basic_mode(
    hass: HomeAssistant,
) -> None:
    """Basic Charging with genuinely nothing to refine against (no manual schedule ever fetched,
    no active override - _stub_api()'s defaults) leaves duration None. See the two tests below
    for Basic Charging WITH schedule/override data, which refines properly."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw(delegatedControl={"status": "INACTIVE"})]
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    assert result[PPID].current_charge.duration is None


async def test_current_charge_duration_refined_from_manual_schedule_in_basic_mode(
    hass: HomeAssistant,
) -> None:
    """Basic Charging: duration comes from the manual schedule's own active window(s), not the
    full time-since-session-start (which would include any off-schedule time)."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw(delegatedControl={"status": "INACTIVE"})]
    tz = ZoneInfo("Europe/London")  # matches _charger_raw()'s default timezone
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)

    # A schedule window covering only the last hour of the 2h session, local time.
    window_start_local = (now - datetime.timedelta(hours=1)).astimezone(tz)
    window_end_local = now.astimezone(tz)
    api.async_manual_schedules.return_value = {
        "data": [
            {
                "uid": "w1",
                "startDay": window_start_local.isoweekday(),
                "startTime": window_start_local.strftime("%H:%M:%S"),
                "endDay": window_end_local.isoweekday(),
                "endTime": window_end_local.strftime("%H:%M:%S"),
                "status": {"isActive": True},
            }
        ]
    }

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    duration = result[PPID].current_charge.duration
    assert 3595 <= duration <= 3610  # ~1h, not the naive ~2h


async def test_current_charge_duration_refined_from_override_in_basic_mode(
    hass: HomeAssistant,
) -> None:
    """Basic Charging: an Always On override that already existed before this session began
    (survived a cable unplug/replug, say) is trusted from session_start directly - not gated by
    the manual schedule, and not waiting on a fresh confirmation since it predates the session."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw(delegatedControl={"status": "INACTIVE"})]
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    override_start = session_start - datetime.timedelta(hours=1)  # predates this session
    api.async_get_charge_overrides.return_value = [
        {"requestedAt": override_start.isoformat()}  # Always On - no endAt at all
    ]

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    duration = result[PPID].current_charge.duration
    # The whole 2h session, trusted from session_start - not gated by the manual schedule, and
    # not clipped back to the override's own (pre-session) requestedAt.
    assert 7195 <= duration <= 7210


async def test_current_charge_duration_keeps_a_cancelled_boosts_contribution(
    hass: HomeAssistant,
) -> None:
    """Basic Charging, no manual schedule: a boost created and confirmed this session, then
    cancelled (deletedAt set), still contributes its real ~30-minute confirmed-to-cancelled span
    to duration - not 0 (nothing currently active) and not extended to endAt (never reached)."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw(delegatedControl={"status": "INACTIVE"})]
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    boost_start = session_start + datetime.timedelta(minutes=30)
    boost_cancelled_at = boost_start + datetime.timedelta(minutes=30)
    # Confirms the boost's start - lastSeenAt at/after requestedAt with chargingState Charging.
    api.async_connectivity_status.return_value = {
        "chargingState": "Charging", "lastSeenAt": boost_start.isoformat(),
    }
    api.async_get_charge_overrides.return_value = [
        {
            "requestedAt": boost_start.isoformat(),
            "endAt": (boost_start + datetime.timedelta(hours=1)).isoformat(),
            "deletedAt": boost_cancelled_at.isoformat(),
        }
    ]

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    duration = result[PPID].current_charge.duration
    # Exactly the boost's real 30-minute contribution (confirmed start -> deletedAt).
    assert 1795 <= duration <= 1810


async def test_record_charging_state_transition_dedups_consecutive_same_state(
    hass: HomeAssistant,
) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    t1 = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 1, 1, 10, 1, tzinfo=datetime.timezone.utc)
    coordinator._record_charging_state_transition(PPID, "Charging", t1)
    coordinator._record_charging_state_transition(PPID, "Charging", t2)  # same state - no-op
    assert coordinator._charging_state_transitions_by_ppid[PPID] == [(t1, "Charging")]

    t3 = datetime.datetime(2026, 1, 1, 10, 2, tzinfo=datetime.timezone.utc)
    coordinator._record_charging_state_transition(PPID, "SuspendedEV", t3)
    assert coordinator._charging_state_transitions_by_ppid[PPID] == [
        (t1, "Charging"), (t3, "SuspendedEV"),
    ]


async def test_record_charging_state_transition_ignores_missing_data(
    hass: HomeAssistant,
) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator._record_charging_state_transition(PPID, None, now)
    coordinator._record_charging_state_transition(PPID, "Charging", None)
    assert coordinator._charging_state_transitions_by_ppid.get(PPID, []) == []


async def test_has_confirmed_energy(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    assert coordinator._has_confirmed_energy(make_charge(energy_total=1.2)) is True
    assert coordinator._has_confirmed_energy(make_charge(energy_total=0)) is False
    assert coordinator._has_confirmed_energy(make_charge(energy_total=None)) is False


async def test_confirmed_override_events_drops_unconfirmed_fresh_override(
    hass: HomeAssistant,
) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    session_start = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    start = session_start + datetime.timedelta(minutes=10)
    end = start + datetime.timedelta(hours=1)
    # No transitions recorded at all - nothing confirms this override started.
    assert coordinator._confirmed_override_events(
        PPID, [(start, end, "Boost")], session_start
    ) == []


async def test_confirmed_override_events_uses_confirmed_transition_as_start(
    hass: HomeAssistant,
) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    session_start = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    start = session_start + datetime.timedelta(minutes=10)
    end = start + datetime.timedelta(hours=1)
    confirmed_at = start + datetime.timedelta(minutes=3)
    coordinator._charging_state_transitions_by_ppid[PPID] = [(confirmed_at, "Charging")]
    assert coordinator._confirmed_override_events(
        PPID, [(start, end, "Boost")], session_start
    ) == [(confirmed_at, end, "Boost")]


async def test_confirmed_override_events_trusts_pre_session_override(
    hass: HomeAssistant,
) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    session_start = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    start = session_start - datetime.timedelta(hours=2)  # predates the session
    end = session_start + datetime.timedelta(hours=1)
    # No transitions recorded at all - still trusted, since it predates session_start.
    assert coordinator._confirmed_override_events(
        PPID, [(start, end, "Always on")], session_start
    ) == [(session_start, end, "Always on")]


async def test_confirmed_override_events_start_exactly_at_session_start(
    hass: HomeAssistant,
) -> None:
    """The `start < session_start` boundary is exclusive - an override starting exactly at
    session_start is NOT pre-existing, so it still needs a confirmed Charging transition (unlike
    one that predates session_start, see test_confirmed_override_events_trusts_pre_session_override
    above)."""
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    session_start = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    end = session_start + datetime.timedelta(hours=1)
    # No transitions recorded - not trusted, so dropped.
    assert coordinator._confirmed_override_events(
        PPID, [(session_start, end, "Always on")], session_start
    ) == []

    # Confirmed via a transition at exactly session_start - included from that point.
    coordinator._charging_state_transitions_by_ppid[PPID] = [(session_start, "Charging")]
    assert coordinator._confirmed_override_events(
        PPID, [(session_start, end, "Always on")], session_start
    ) == [(session_start, end, "Always on")]


async def test_confirmed_override_events_trusts_when_already_charging_at_start(
    hass: HomeAssistant,
) -> None:
    """An override created while the charger is already Charging (e.g. a boost extending an
    in-progress charge) is trusted from its own start immediately - chargingState won't record a
    fresh transition if it was already Charging and stays that way."""
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    session_start = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    already_charging_at = session_start + datetime.timedelta(minutes=5)
    start = session_start + datetime.timedelta(minutes=20)
    end = start + datetime.timedelta(hours=1)
    coordinator._charging_state_transitions_by_ppid[PPID] = [
        (already_charging_at, "Charging"),
    ]
    assert coordinator._confirmed_override_events(
        PPID, [(start, end, "Boost")], session_start
    ) == [(start, end, "Boost")]


async def test_effective_now_uncapped_when_currently_charging(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator._charging_state_transitions_by_ppid[PPID] = [
        (now - datetime.timedelta(minutes=5), "Charging"),
    ]
    assert coordinator._effective_now(PPID, now) == now


async def test_effective_now_capped_when_not_currently_charging(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime.now(datetime.timezone.utc)
    stopped_at = now - datetime.timedelta(minutes=10)
    coordinator._charging_state_transitions_by_ppid[PPID] = [
        (now - datetime.timedelta(minutes=20), "Charging"),
        (stopped_at, "SuspendedEV"),
    ]
    assert coordinator._effective_now(PPID, now) == stopped_at


async def test_effective_now_uncapped_with_no_transitions_yet(hass: HomeAssistant) -> None:
    api = _stub_api()
    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime.now(datetime.timezone.utc)
    assert coordinator._effective_now(PPID, now) == now


async def test_current_charge_duration_schedule_start_trusted_end_freezes_on_observed_stop(
    hass: HomeAssistant,
) -> None:
    """Reconstructs the scenario proven from a real HA history export: a schedule window opens
    well before chargingState actually reaches Charging - the front-edge gap is NOT suppressed
    (schedule starts are trusted, unlike a fresh override) - then once chargingState is observed
    leaving Charging (the vehicle finishing), duration freezes at that point instead of
    continuing to climb for as long as the window nominally stays open."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw(delegatedControl={"status": "INACTIVE"})]
    tz = ZoneInfo("Europe/London")  # matches _charger_raw()'s default timezone
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    # chargingState only reaches Charging 11 minutes in - still trusted from session_start.
    api.async_connectivity_status.return_value = {
        "chargingState": "Charging",
        "lastSeenAt": (session_start + datetime.timedelta(minutes=11)).isoformat(),
    }
    # A schedule window covering the whole session and beyond, well past `now`.
    window_start_local = session_start.astimezone(tz)
    window_end_local = (now + datetime.timedelta(hours=2)).astimezone(tz)
    api.async_manual_schedules.return_value = {
        "data": [
            {
                "uid": "w1",
                "startDay": window_start_local.isoweekday(),
                "startTime": window_start_local.strftime("%H:%M:%S"),
                "endDay": window_end_local.isoweekday(),
                "endTime": window_end_local.strftime("%H:%M:%S"),
                "status": {"isActive": True},
            }
        ]
    }

    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_fetch_data()  # commit, as a real refresh would
    # The whole 2h session - trusted from session_start, not clipped to when Charging was
    # actually confirmed (11 minutes in).
    assert 7195 <= coordinator._current_charge_by_ppid[PPID].duration <= 7210

    # The vehicle finishes - chargingState leaves Charging.
    stopped_at = now - datetime.timedelta(minutes=30)
    api.async_connectivity_status.return_value = {
        "chargingState": "SuspendedEV", "lastSeenAt": stopped_at.isoformat(),
    }
    coordinator._api3_charges_fetched_at = None

    result = await coordinator._async_fetch_data()

    # Frozen at the observed stop, not still climbing toward the window's own (much later) end.
    frozen_duration = (stopped_at - session_start).total_seconds()
    assert frozen_duration - 5 <= result[PPID].current_charge.duration <= frozen_duration + 5


async def test_current_charge_duration_unknown_while_never_confirmed_energy(
    hass: HomeAssistant,
) -> None:
    """A vehicle that never actually draws power this session (already fully charged, say)
    stays at an honest None throughout, even with a schedule window open the whole time -
    not a guessed 0 or a value derived purely from the window being open."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw(delegatedControl={"status": "INACTIVE"})]
    tz = ZoneInfo("Europe/London")
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    api.async_api3_charges.return_value["charges"][0]["kwh_used"] = 0
    window_start_local = session_start.astimezone(tz)
    window_end_local = (now + datetime.timedelta(hours=2)).astimezone(tz)
    api.async_manual_schedules.return_value = {
        "data": [
            {
                "uid": "w1",
                "startDay": window_start_local.isoweekday(),
                "startTime": window_start_local.strftime("%H:%M:%S"),
                "endDay": window_end_local.isoweekday(),
                "endTime": window_end_local.strftime("%H:%M:%S"),
                "status": {"isActive": True},
            }
        ]
    }

    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_fetch_data()
    assert coordinator._current_charge_by_ppid[PPID].duration is None

    coordinator._api3_charges_fetched_at = None
    result = await coordinator._async_fetch_data()
    assert result[PPID].current_charge.duration is None


async def test_vehicle_parsed_from_smart_charging_chargers_and_vehicles(
    hass: HomeAssistant,
) -> None:
    """Shape below matches a real captured response (vehicles/currentIntent/intents.details
    nesting) - cross-checked against scratch/output locally, not copied from it."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_smart_charging_chargers_and_vehicles.return_value = [
        {
            "ppid": PPID,
            "vehicles": [
                {
                    "id": "vehicle-link-1",
                    "isPluggedInToThisCharger": True,
                    "isPrimary": True,
                    "vehicle": {
                        "id": "vehicle-1",
                        "vehicleInformation": {
                            "brand": "TestMake",
                            "model": "TestModel",
                            "displayName": "My Test Car",
                        },
                        "chargeState": {
                            "batteryCapacity": 64,
                            "batteryLevelPercent": 55,
                            "range": 210,
                            "isCharging": True,
                            "isFullyCharged": False,
                            "chargeLimitPercent": 80,
                            "chargeLimitSource": "vehicle",
                            "powerDeliveryState": "PLUGGED_IN:CHARGING",
                            "chargeRate": None,
                            "maxCurrent": None,
                            "chargeTimeRemaining": None,
                            "lastUpdated": "2026-01-01T09:00:00Z",
                        },
                        "odometer": {"distanceKm": 12000},
                    },
                    "currentIntent": {
                        "canMeetTarget": True,
                        "cannotMeetTargetReason": None,
                        "readyByTime": "2026-01-02T07:00:00Z",
                        "chargeDetail": {"expectedChargeByTargetPercent": 80.0},
                    },
                    "intents": {
                        "details": [{"dayOfWeek": "MONDAY", "chargeByTime": "07:00:00", "chargeKWh": 20}]
                    },
                }
            ],
        }
    ]

    coordinator = _make_coordinator(hass, api)
    result = await coordinator._async_fetch_data()

    vehicle = result[PPID].vehicle
    assert vehicle is not None
    assert vehicle.id == "vehicle-1"
    assert vehicle.display_name == "My Test Car"
    assert vehicle.battery_level_percent == 55
    assert vehicle.is_plugged_in_to_this_charger is True
    assert vehicle.can_meet_target is True
    assert vehicle.intent_charge_kwh == 20
    assert vehicle.ready_by == datetime.datetime(2026, 1, 2, 7, 0, tzinfo=datetime.timezone.utc)

    assert coordinator._current_charge_by_ppid == {}


async def test_vehicles_fetched_same_poll_as_charging_transition(hass: HomeAssistant) -> None:
    """The vehicles fetch's fast tier used to only promote on the poll AFTER a charging
    transition (sourced from self.data, last poll's already-committed state) - it now promotes
    the same poll the transition is first observed, from this poll's own freshly-fetched
    connectivity."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime.now(datetime.timezone.utc)
    # Simulate the previous poll's committed state (not charging) and a vehicle fetch recent
    # enough that neither the cable-connected nor idle tier would be due on its own.
    coordinator.data = {PPID: make_charger(charging_state="Available")}
    coordinator._vehicles_fetched_at = now

    await coordinator._async_fetch_data()

    api.async_smart_charging_chargers_and_vehicles.assert_called_once()


async def test_vehicles_not_refetched_when_still_idle_and_fresh(hass: HomeAssistant) -> None:
    """Negative control for the above - proves the tiering itself still holds, this isn't
    "always fetch vehicles now"."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_connectivity_status.return_value = {"chargingState": "Available"}
    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator.data = {PPID: make_charger(charging_state="Available")}
    coordinator._vehicles_fetched_at = now

    await coordinator._async_fetch_data()

    api.async_smart_charging_chargers_and_vehicles.assert_not_called()


async def test_charges_fetched_same_poll_as_charging_transition(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator.data = {PPID: make_charger(charging_state="Available")}
    coordinator._charges_fetched_at = now  # not otherwise stale

    await coordinator._async_fetch_data()

    api.async_charges.assert_called_once()


async def test_api3_charges_fetched_same_poll_as_charging_transition(hass: HomeAssistant) -> None:
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    coordinator = _make_coordinator(hass, api)
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator.data = {PPID: make_charger(charging_state="Available")}
    coordinator._api3_user_id = 123  # only fetched at all once this is known
    coordinator._api3_charges_fetched_at = now  # not otherwise stale

    await coordinator._async_fetch_data()

    api.async_api3_charges.assert_called_once()


async def test_connectivity_fetched_once_per_ppid_per_poll(hass: HomeAssistant) -> None:
    """Guards against the connectivity preflight and the per-charger loop both fetching it -
    same total call count as before the same-poll restructuring, just resolved earlier."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    coordinator = _make_coordinator(hass, api)

    await coordinator._async_fetch_data()

    assert api.async_connectivity_status.call_count == 1


async def test_force_vehicles_fetch_bypasses_staleness_tier(hass: HomeAssistant) -> None:
    """request_vehicles_fetch() (called by Target Charge/Ready By's write paths) bypasses
    whichever tier is currently active, one-shot."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    coordinator = _make_coordinator(hass, api)
    coordinator.data = {PPID: make_charger(charging_state="Available")}
    coordinator._vehicles_fetched_at = datetime.datetime.now(datetime.timezone.utc)  # fresh

    coordinator.request_vehicles_fetch()
    assert coordinator._force_vehicles_fetch is True
    await coordinator._async_fetch_data()

    api.async_smart_charging_chargers_and_vehicles.assert_called_once()
    assert coordinator._force_vehicles_fetch is False  # one-shot, consumed regardless of outcome


async def test_remote_lock_status_parsed(hass: HomeAssistant) -> None:
    """offMode: null is a genuine, confirmed-live response shape (unsupported charger model, or
    unset) - a successful fetch, not treated as missing data. Two separate coordinators here
    purely for a clean before/after comparison; unlike firmware/tariffs, remote_lock is fetched
    every poll, not staleness-cached (see test_firmware_and_tariffs_not_refetched_within_
    staleness_window's assertion on async_get_remote_lock_status.call_count) - a lock/unlock
    write should be reflected the moment the next poll runs."""
    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_get_remote_lock_status.return_value = {"offMode": None}
    result = await _make_coordinator(hass, api)._async_fetch_data()
    assert result[PPID].remote_lock_off_mode is None

    api = _stub_api()
    api.async_list_chargers.return_value = [_charger_raw()]
    api.async_get_remote_lock_status.return_value = {"offMode": True}
    result = await _make_coordinator(hass, api)._async_fetch_data()
    assert result[PPID].remote_lock_off_mode is True
