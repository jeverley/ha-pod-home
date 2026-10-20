"""Coordinator tests, using pytest-homeassistant-custom-component's real `hass` fixture (see
tests/conftest.py). Exercises `_async_fetch_data`/`_async_update_data` directly against a fully
mocked `PodHomeApiClient` (`create_autospec`, so a renamed/removed client method fails loudly
here rather than silently mocking a typo) with realistic response shapes - focused on the
coordinator's own parsing/staleness/error-handling logic, in isolation from config-entry setup.
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
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
from custom_components.pod_home.helpers import select_last_charge
import custom_components.pod_home.sensor as sensor
from tests._fixtures import PPID, charger_raw, make_charge, make_charger, make_stub_api

pytestmark = pytest.mark.asyncio



@pytest.fixture
def api() -> PodHomeApiClient:
    return make_stub_api()


@pytest.fixture
def coordinator(hass: HomeAssistant, api: PodHomeApiClient) -> PodHomeDataUpdateCoordinator:
    return _make_coordinator(hass, api)


def _make_coordinator(hass: HomeAssistant, api: PodHomeApiClient) -> PodHomeDataUpdateCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"email": "driver@example.com", "password": "x"})
    entry.add_to_hass(hass)
    return PodHomeDataUpdateCoordinator(
        hass, entry, api, email="driver@example.com", password="x"
    )


async def test_first_refresh_parses_charger_basic_mode(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
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
    started_now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    api.async_charges.return_value = {
        "data": {
            "charges": [
                {"id": "m1", "startedAt": started_now, "endedAt": started_now,
                 "energyTotal": 12.3, "cost": {"amount": 456}, "charger": {"id": PPID}},
            ]
        }
    }

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


async def test_smart_mode_fetches_smart_schedule(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_list_chargers.return_value = [
        charger_raw(delegatedControl={"status": "ACTIVE"})
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

    result = await coordinator._async_fetch_data()

    api.async_smart_schedule_active.assert_called_once_with(PPID)
    windows = result[PPID].smart_schedule_windows
    assert windows is not None
    assert windows[0].type == "CHARGING"
    assert windows[0].tariff_rate == "OFF_PEAK"


async def test_empty_chargers_first_poll_returns_empty(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_list_chargers.return_value = []
    result = await coordinator._async_fetch_data()
    assert result == {}


async def test_empty_chargers_keeps_previous_data(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    first = await coordinator._async_fetch_data()
    coordinator.data = first  # DataUpdateCoordinator normally does this after a successful poll

    api.async_list_chargers.return_value = []
    second = await coordinator._async_fetch_data()

    assert second == first  # previous data kept, not wiped to {}


async def test_empty_chargers_still_saves_state_and_adjusts_poll_interval(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
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


@pytest.mark.parametrize(
    ("changed_ago", "expected"),
    [(datetime.timedelta(minutes=1), FAST_POLL_INTERVAL), (None, SLOW_POLL_INTERVAL)],
)
async def test_failed_poll_retries_at_base_interval(
    api: PodHomeApiClient,
    coordinator: PodHomeDataUpdateCoordinator,
    changed_ago: datetime.timedelta | None,
    expected: datetime.timedelta,
) -> None:
    """A check-in-aligned delay of a few seconds isn't reused to retry a failing API, and an
    idle outage retries at the slow interval."""
    api.async_list_chargers.side_effect = PodHomeApiError(500, {})
    coordinator.update_interval = datetime.timedelta(seconds=7)
    if changed_ago is not None:
        coordinator._plugged_in_at[PPID] = dt_util.utcnow() - changed_ago

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.update_interval == expected


async def test_auth_error_raises_config_entry_auth_failed(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_list_chargers.side_effect = PodHomeAuthError("token expired")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_connection_error_retries_then_raises_update_failed(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_list_chargers.side_effect = PodHomeApiError(0, "connection refused")
    with patch("custom_components.pod_home.coordinator.asyncio.sleep", AsyncMock()):
        with pytest.raises(UpdateFailed):
            await coordinator._async_fetch_data()
    assert api.async_list_chargers.call_count == 3  # CONNECTION_RETRY_ATTEMPTS


async def test_http_error_raises_immediately_without_retry(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_list_chargers.side_effect = PodHomeApiError(500, "server error")
    with pytest.raises(UpdateFailed):
        await coordinator._async_fetch_data()
    assert api.async_list_chargers.call_count == 1  # a genuine HTTP error is not retried


async def test_boost_end_at_parsed_from_charge_overrides(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
    api.async_get_charge_overrides.return_value = [
        {"requestedAt": "2026-01-01T00:00:00Z", "endAt": future.isoformat(), "deletedAt": None}
    ]

    result = await coordinator._async_fetch_data()

    assert result[PPID].boost_end_at == future


async def test_override_requested_at_is_newest_active_entry(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Expired and deleted entries don't count, however recent."""
    now = datetime.datetime.now(datetime.timezone.utc)
    active = now - datetime.timedelta(minutes=2)
    api.async_get_charge_overrides.return_value = [
        {"requestedAt": active.isoformat(), "endAt": (now + datetime.timedelta(hours=1)).isoformat()},
        {"requestedAt": (now - datetime.timedelta(minutes=1)).isoformat(),
         "endAt": (now + datetime.timedelta(hours=1)).isoformat(),
         "deletedAt": now.isoformat()},
        {"requestedAt": now.isoformat(), "endAt": (now - datetime.timedelta(seconds=1)).isoformat()},
    ]

    result = await coordinator._async_fetch_data()

    assert result[PPID].override_requested_at == active


async def test_deleted_charge_override_not_current_boost(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
    api.async_get_charge_overrides.return_value = [
        {
            "requestedAt": "2026-01-01T00:00:00Z",
            "endAt": future.isoformat(),
            "deletedAt": "2026-01-01T00:05:00Z",
        }
    ]

    result = await coordinator._async_fetch_data()

    assert result[PPID].boost_end_at is None


async def test_always_on_active_from_endat_less_charge_override(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """An indefinite override (no endAt at all) is Basic Charging's "Always on" mode, not a
    boost - confirmed live, distinct from test_boost_end_at_parsed_from_charge_overrides above,
    which always carries a real endAt."""
    api.async_get_charge_overrides.return_value = [
        {"requestedAt": "2026-01-01T00:00:00Z"}
    ]

    result = await coordinator._async_fetch_data()

    assert result[PPID].always_on_active is True
    assert result[PPID].boost_end_at is None  # not treated as a cancellable boost


async def test_always_on_active_none_when_charge_overrides_never_fetched(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Distinct from confirmed True/False - a charge-overrides fetch that's never succeeded
    leaves always_on_active unknown, not silently False."""
    api.async_get_charge_overrides.side_effect = PodHomeApiError(500, "server error")

    result = await coordinator._async_fetch_data()

    assert result[PPID].always_on_active is None


async def test_deleted_endat_less_override_is_not_always_on(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_get_charge_overrides.return_value = [
        {"requestedAt": "2026-01-01T00:00:00Z", "deletedAt": "2026-01-01T00:05:00Z"}
    ]

    result = await coordinator._async_fetch_data()

    assert result[PPID].always_on_active is False


async def test_firmware_and_tariffs_not_refetched_within_staleness_window(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Firmware/tariffs/manual_schedules/delegated_control are cached on
    FIRMWARE_TARIFF_REFRESH_INTERVAL - a second poll immediately after the first must not
    re-fetch them."""
    # Each needs a genuinely parseable (non-empty) response for its _fetched_at to actually get
    # stamped - the coordinator deliberately does NOT cache an empty/unparseable response (see
    # its own comment: "a bad/unexpected response gets retried next poll"), so an empty-dict
    # default (as most make_stub_api() calls use) would legitimately keep re-fetching every poll.
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
    # get called again - remote_lock too, since no successful response has been stored yet.
    assert api.async_smart_charging_preferences.call_count == 2
    assert api.async_get_remote_lock_status.call_count == 2


async def test_accumulate_total_energy_sums_finalized_charges_once_each(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    now = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)
    coordinator._total_started_at_by_ppid[PPID] = datetime.datetime(
        2025, 12, 31, tzinfo=datetime.timezone.utc
    )

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
    coordinator._accumulate_total_energy(entries, now)
    assert coordinator._total_energy_kwh_by_ppid[PPID] == 8.0

    # Same batch replayed (e.g. a re-poll covering an overlapping lookback window) - already
    # counted (watermark has moved past both), must not double-add.
    coordinator._accumulate_total_energy(entries, now)
    assert coordinator._total_energy_kwh_by_ppid[PPID] == 8.0


async def test_accumulate_total_energy_skips_still_open_sessions(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    entries = [(PPID, {"id": "c1", "endedAt": None, "energyTotal": 5.0}, {})]
    coordinator._accumulate_total_energy(entries, datetime.datetime.now(datetime.timezone.utc))
    assert PPID not in coordinator._total_energy_kwh_by_ppid


async def test_accumulate_total_energy_starts_at_tracking_start(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Sessions that ended before this install started tracking a charger aren't added - a
    first fetch covering past weeks of history counts nothing until a session ends after it."""
    first_fetch = datetime.datetime(2026, 1, 10, tzinfo=datetime.timezone.utc)
    history = [(PPID, {"id": "c1", "endedAt": "2026-01-05T01:00:00Z", "energyTotal": 5.0}, {})]

    coordinator._accumulate_total_energy(history, first_fetch)
    assert PPID not in coordinator._total_energy_kwh_by_ppid

    coordinator._total_started_at_by_ppid[PPID] = first_fetch
    new_session = (PPID, {"id": "c2", "endedAt": "2026-01-11T01:00:00Z", "energyTotal": 3.0}, {})
    coordinator._accumulate_total_energy([*history, new_session], first_fetch)
    assert coordinator._total_energy_kwh_by_ppid[PPID] == 3.0


async def test_sticky_state_round_trips_through_store(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    coordinator._charging_started_at_by_ppid[PPID] = now
    coordinator._total_energy_kwh_by_ppid[PPID] = 42.0
    coordinator._total_started_at_by_ppid[PPID] = now
    coordinator._latest_charge_by_ppid[PPID] = make_charge()

    await coordinator._sticky_store.async_save(coordinator._sticky_state_for_storage())
    await coordinator._total_energy_store.async_save(coordinator._total_energy_state_for_storage())

    # Simulate a restart: clear the in-memory state, then reload from the same Store the save
    # above wrote to - a second coordinator instance would use a different (randomly-generated)
    # MockConfigEntry.entry_id and so a different Store path, not actually testing persistence.
    coordinator._charging_started_at_by_ppid = {}
    coordinator._total_energy_kwh_by_ppid = {}
    coordinator._total_started_at_by_ppid = {}
    coordinator._latest_charge_by_ppid = {}
    await coordinator.async_load_sticky_state()

    assert coordinator._charging_started_at_by_ppid[PPID] == now
    assert coordinator._total_energy_kwh_by_ppid[PPID] == 42.0
    assert coordinator._total_started_at_by_ppid[PPID] == now
    assert coordinator._latest_charge_by_ppid[PPID] == make_charge()


async def test_latest_charge_kept_when_outside_lookback_window(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """A restored latest charge older than RECENT_CHARGES_LOOKBACK stays reported when the
    /charges window has no entries for that charger."""
    coordinator._latest_charge_by_ppid[PPID] = make_charge()

    result = await coordinator._async_fetch_data()

    api.async_charges.assert_awaited_once()
    assert result[PPID].latest_charge == make_charge()


async def test_firmware_version_synced_to_device_registry(
    hass: HomeAssistant, api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_charger_firmware.return_value = [
        {
            "versionInfo": {"manifestId": "A30P-2.0"},
            "updateStatus": {"isUpdateAvailable": False},
            "serialNumber": "SN123",
        }
    ]
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, PPID)},
        sw_version="A30P-1.0",
    )

    await coordinator._async_fetch_data()

    assert registry.async_get(device.id).sw_version == "A30P-2.0"


async def test_adjust_poll_interval_speeds_up_after_plug_in(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    coordinator.update_interval = SLOW_POLL_INTERVAL
    coordinator._plugged_in_at[PPID] = datetime.datetime.now(datetime.timezone.utc)
    coordinator._async_adjust_poll_interval({})
    assert coordinator.update_interval == FAST_POLL_INTERVAL


async def test_adjust_poll_interval_slows_down_after_window(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    coordinator.update_interval = FAST_POLL_INTERVAL
    coordinator._plugged_in_at[PPID] = datetime.datetime(
        2020, 1, 1, tzinfo=datetime.timezone.utc
    )
    coordinator._async_adjust_poll_interval({})
    assert coordinator.update_interval == SLOW_POLL_INTERVAL


async def test_api3_charges_matched_to_ppid_via_pod_id(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
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

    now = datetime.datetime(2026, 1, 1, 11, 0, tzinfo=datetime.timezone.utc)
    await coordinator._async_refresh_api3_account(now)
    await coordinator._async_refresh_api3_charges(coordinator._api3_user_id, now)

    assert coordinator._current_charge_by_ppid[PPID].energy_total == 2.5
    assert coordinator._current_charge_by_ppid[PPID].cost_currency == "GBP"
    assert coordinator._current_charge_by_ppid[PPID].duration is None


async def test_api3_charges_unmatched_pod_id_warns_and_stays_empty(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
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

    now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    await coordinator._async_refresh_api3_account(now)
    await coordinator._async_refresh_api3_charges(coordinator._api3_user_id, now)

    assert "api3_charges_unmatched" in coordinator._warned_keys
    assert coordinator._current_charge_by_ppid == {}


def _api3_session_stubs(api, *, started_at: datetime.datetime, kwh_used: float = 1.0) -> None:
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
                "kwh_used": kwh_used,
                "pod": {"id": 555},
                "billing_event": {},
            }
        ]
    }


@pytest.mark.parametrize(
    ("status", "error", "expect_warning"),
    [
        (404, "NO_ACTIVE_CHARGING_SESSION", False),
        (404, "AFTER_INTENT_TIME", False),
        (412, "NO_READY_BY_TIME", False),
        (412, "SOMETHING_ELSE", True),
    ],
)
async def test_expected_smart_schedule_errors_not_warned(
    api: PodHomeApiClient,
    coordinator: PodHomeDataUpdateCoordinator,
    status: int,
    error: str,
    expect_warning: bool,
) -> None:
    api.async_smart_schedule_active.side_effect = PodHomeApiError(status, {"error": error})

    assert await coordinator._fetch_smart_schedule(PPID) == {}
    assert (f"smart_schedule:{PPID}" in coordinator._warned_keys) is expect_warning


async def test_current_charge_duration_refined_from_smart_schedule(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Smart Charging: duration comes from the CHARGING window(s) actually in the schedule, not
    the full time-since-session-start (which would include any paused time)."""
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "ACTIVE"})]
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

    result = await coordinator._async_fetch_data()

    # Only the 1-hour CHARGING window counts, not the full 2h since session start.
    assert result[PPID].current_charge.duration == 3600


async def test_current_charge_duration_unknown_on_first_poll_when_schedule_fetch_fails(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Smart Charging: a schedule is known to exist for this scheme (delegated control active),
    but this poll's schedule fetch itself comes back empty and it's the first poll of this
    session - nothing yet to freeze at, so duration is None rather than a guessed value."""
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "ACTIVE"})]
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    api.async_smart_schedule_active.return_value = {}

    result = await coordinator._async_fetch_data()

    assert result[PPID].current_charge.duration is None


async def test_current_charge_duration_freezes_when_schedule_unavailable(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Smart Charging: a poll where the schedule can't be refined against (charger paused
    between windows, or a transient fetch gap) freezes the last known duration rather than
    recomputing time-since-session-start, which would overcount by the paused time and keep
    growing every poll while genuinely not charging."""
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "ACTIVE"})]
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

    coordinator.data = await coordinator._async_fetch_data()  # commit, as a real refresh would
    assert coordinator._current_charge_by_ppid[PPID].duration == 3600

    # Now paused between windows - the schedule endpoint has nothing to offer this poll, and
    # the api3-charges refetch (still forced here) would reset duration to None if not for the
    # freeze at the previous poll's committed value.
    api.async_connectivity_status.return_value = {"chargingState": "SuspendedEVSE"}
    api.async_smart_schedule_active.return_value = {}
    coordinator._charges_fetched_at = None  # session data due - refetches api3

    result = await coordinator._async_fetch_data()

    assert result[PPID].current_charge.duration == 3600


def _smart_session_api(now: datetime.datetime) -> PodHomeApiClient:
    """Smart Charging, an api3 session that started 2 hours ago, and a schedule whose one
    charging window ended an hour ago."""
    api = make_stub_api()
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "ACTIVE"})]
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    api.async_smart_schedule_active.return_value = {
        "schedule": [
            {
                "type": "CHARGING",
                "fromTimestamp": session_start.isoformat(),
                "toTimestamp": (now - datetime.timedelta(hours=1)).isoformat(),
            }
        ]
    }
    return api


_SCHEDULE_OVER: dict = {}


async def _restart(coordinator: PodHomeDataUpdateCoordinator) -> PodHomeDataUpdateCoordinator:
    """Save the sticky state and load it into a fresh coordinator on the same config entry."""
    await coordinator._sticky_store.async_save(coordinator._sticky_state_for_storage())
    restarted = PodHomeDataUpdateCoordinator(
        hass=coordinator.hass, config_entry=coordinator.config_entry, api=coordinator.api,
        email="driver@example.com", password="x",
    )
    await restarted.async_load_sticky_state()
    return restarted


@pytest.mark.parametrize(
    ("state_after", "seen_after", "kwh_after", "schedule_after", "expected"),
    [
        # B: not charging, energy unchanged - nothing happened while down.
        ("SuspendedEV", datetime.timedelta(hours=2), 1.0, False, 3600),
        # C/E: energy rose, not charging now, check-ins missed - stopped unseen.
        ("SuspendedEV", datetime.timedelta(hours=2), 2.0, True, None),
        # G: energy rose but no check-in was missed - the stop is as precise as ever.
        ("SuspendedEV", datetime.timedelta(minutes=3), 2.0, True, 3600),
        # F: still charging - counted from the schedule.
        ("Charging", datetime.timedelta(hours=2), 2.0, True, 3600),
    ],
)
async def test_live_charge_duration_after_restart(
    hass: HomeAssistant,
    state_after: str,
    seen_after: datetime.timedelta,
    kwh_after: float,
    schedule_after: bool,
    expected: int | None,
) -> None:
    """Smart Charging session, its one window ended an hour ago, charger last seen stopped now:
    duration 3600 before a restart. After it, depending on what happened while down."""
    now = datetime.datetime.now(datetime.timezone.utc)
    api = _smart_session_api(now)
    schedule = api.async_smart_schedule_active.return_value
    api.async_connectivity_status.return_value = {
        "chargingState": "SuspendedEV", "lastSeenAt": now.isoformat(),
    }
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_fetch_data()
    assert coordinator.data[PPID].current_charge.duration == 3600

    restarted = await _restart(coordinator)
    api.async_connectivity_status.return_value = {
        "chargingState": state_after, "lastSeenAt": (now + seen_after).isoformat(),
    }
    _api3_session_stubs(api, started_at=now - datetime.timedelta(hours=2), kwh_used=kwh_after)
    api.async_smart_schedule_active.return_value = schedule if schedule_after else _SCHEDULE_OVER

    result = await restarted._async_fetch_data()

    assert result[PPID].current_charge.duration == expected


async def test_observed_stop_survives_restart(hass: HomeAssistant) -> None:
    """D: a stop seen before the restart still caps the duration recomputed after it, and the
    energy delivered right up to the stop isn't mistaken for charging while down."""
    now = datetime.datetime.now(datetime.timezone.utc)
    api = _smart_session_api(now)
    api.async_smart_schedule_active.return_value = {
        "schedule": [
            {
                "type": "CHARGING",
                "fromTimestamp": (now - datetime.timedelta(hours=2)).isoformat(),
                "toTimestamp": (now + datetime.timedelta(hours=1)).isoformat(),
            }
        ]
    }
    stopped_at = now - datetime.timedelta(hours=1)
    api.async_connectivity_status.return_value = {
        "chargingState": "Charging",
        "lastSeenAt": (stopped_at - datetime.timedelta(minutes=5)).isoformat(),
    }
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_fetch_data()
    api.async_connectivity_status.return_value = {
        "chargingState": "SuspendedEV", "lastSeenAt": stopped_at.isoformat(),
    }
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start, kwh_used=1.2)  # final energy at the stop
    coordinator.data = await coordinator._async_fetch_data()

    restarted = await _restart(coordinator)
    api.async_connectivity_status.return_value = {  # a later check-in, still stopped
        "chargingState": "SuspendedEV", "lastSeenAt": now.isoformat(),
    }
    result = await restarted._async_fetch_data()

    assert result[PPID].current_charge.duration == 3600


async def test_api3_fetched_on_the_poll_after_charging_stops(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    _api3_session_stubs(api, started_at=datetime.datetime.now(datetime.timezone.utc))
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    coordinator.data = await coordinator._async_fetch_data()
    fetched = api.async_api3_charges.await_count

    api.async_connectivity_status.return_value = {"chargingState": "SuspendedEV"}
    coordinator.data = await coordinator._async_fetch_data()
    assert api.async_api3_charges.await_count == fetched + 1
    coordinator.data = await coordinator._async_fetch_data()
    assert api.async_api3_charges.await_count == fetched + 1


async def test_corrupt_saved_timestamps_dont_block_setup(hass: HomeAssistant) -> None:
    coordinator = _make_coordinator(hass, make_stub_api())
    await coordinator._sticky_store.async_save(
        {
            "charging_state_transitions": {PPID: [[1700000000, "Charging"]]},
            "last_seen_at": {PPID: 1700000000},
            "duration_unknown": {PPID: 5},
        }
    )

    await coordinator.async_load_sticky_state()

    assert coordinator._charging_state_transitions_by_ppid == {}
    assert coordinator._last_seen_at_by_ppid == {}
    assert coordinator._duration_unknown_by_ppid == {}


async def test_restored_charge_rejects_non_integer_duration(hass: HomeAssistant) -> None:
    coordinator = _make_coordinator(hass, make_stub_api())
    restored = coordinator._parse_charge_dict({PPID: {"id": "c1", "duration": "3600"}})
    snapshots = coordinator._parse_duration_snapshots({PPID: {"id": "c1", "duration": "3600"}})

    assert restored[PPID].duration is None
    assert snapshots == {}


async def test_current_charge_duration_unknown_when_nothing_to_refine_against_in_basic_mode(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Basic Charging with genuinely nothing to refine against (no manual schedule ever fetched,
    no active override - make_stub_api()'s defaults) leaves duration None. See the two tests below
    for Basic Charging WITH schedule/override data, which refines properly."""
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "INACTIVE"})]
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)

    result = await coordinator._async_fetch_data()

    assert result[PPID].current_charge.duration is None


async def test_current_charge_duration_refined_from_manual_schedule_in_basic_mode(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Basic Charging: duration comes from the manual schedule's own active window(s), not the
    full time-since-session-start (which would include any off-schedule time)."""
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "INACTIVE"})]
    tz = ZoneInfo("Europe/London")  # matches charger_raw()'s default timezone
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

    result = await coordinator._async_fetch_data()

    duration = result[PPID].current_charge.duration
    assert 3595 <= duration <= 3610  # ~1h, not the naive ~2h


async def test_current_charge_duration_refined_from_override_in_basic_mode(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Basic Charging: an Always On override that already existed before this session began
    (survived a cable unplug/replug, say) is trusted from session_start directly - not gated by
    the manual schedule, and not waiting on a fresh confirmation since it predates the session."""
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "INACTIVE"})]
    now = datetime.datetime.now(datetime.timezone.utc)
    session_start = now - datetime.timedelta(hours=2)
    _api3_session_stubs(api, started_at=session_start)
    override_start = session_start - datetime.timedelta(hours=1)  # predates this session
    api.async_get_charge_overrides.return_value = [
        {"requestedAt": override_start.isoformat()}  # Always On - no endAt at all
    ]

    result = await coordinator._async_fetch_data()

    duration = result[PPID].current_charge.duration
    # The whole 2h session, trusted from session_start - not gated by the manual schedule, and
    # not clipped back to the override's own (pre-session) requestedAt.
    assert 7195 <= duration <= 7210


async def test_current_charge_duration_keeps_a_cancelled_boosts_contribution(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Basic Charging, no manual schedule: a boost created and confirmed this session, then
    cancelled (deletedAt set), still contributes its real ~30-minute confirmed-to-cancelled span
    to duration - not 0 (nothing currently active) and not extended to endAt (never reached)."""
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "INACTIVE"})]
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

    result = await coordinator._async_fetch_data()

    duration = result[PPID].current_charge.duration
    # Exactly the boost's real 30-minute contribution (confirmed start -> deletedAt).
    assert 1795 <= duration <= 1810


async def test_record_charging_state_transition_dedups_consecutive_same_state(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
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
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator._record_charging_state_transition(PPID, None, now)
    coordinator._record_charging_state_transition(PPID, "Charging", None)
    assert coordinator._charging_state_transitions_by_ppid.get(PPID, []) == []


async def test_has_confirmed_energy(coordinator: PodHomeDataUpdateCoordinator) -> None:
    assert coordinator._has_confirmed_energy(make_charge(energy_total=1.2)) is True
    assert coordinator._has_confirmed_energy(make_charge(energy_total=0)) is False
    assert coordinator._has_confirmed_energy(make_charge(energy_total=None)) is False


async def test_confirmed_override_events_drops_unconfirmed_fresh_override(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    session_start = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    start = session_start + datetime.timedelta(minutes=10)
    end = start + datetime.timedelta(hours=1)
    # No transitions recorded at all - nothing confirms this override started.
    assert coordinator._confirmed_override_events(
        PPID, [(start, end, "Boost")], session_start
    ) == []


async def test_confirmed_override_events_uses_confirmed_transition_as_start(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    session_start = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    start = session_start + datetime.timedelta(minutes=10)
    end = start + datetime.timedelta(hours=1)
    confirmed_at = start + datetime.timedelta(minutes=3)
    coordinator._charging_state_transitions_by_ppid[PPID] = [(confirmed_at, "Charging")]
    assert coordinator._confirmed_override_events(
        PPID, [(start, end, "Boost")], session_start
    ) == [(confirmed_at, end, "Boost")]


async def test_confirmed_override_events_trusts_pre_session_override(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    session_start = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.timezone.utc)
    start = session_start - datetime.timedelta(hours=2)  # predates the session
    end = session_start + datetime.timedelta(hours=1)
    # No transitions recorded at all - still trusted, since it predates session_start.
    assert coordinator._confirmed_override_events(
        PPID, [(start, end, "Always on")], session_start
    ) == [(session_start, end, "Always on")]


async def test_confirmed_override_events_start_exactly_at_session_start(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """The `start < session_start` boundary is exclusive - an override starting exactly at
    session_start is NOT pre-existing, so it still needs a confirmed Charging transition (unlike
    one that predates session_start, see test_confirmed_override_events_trusts_pre_session_override
    above)."""
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
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """An override created while the charger is already Charging (e.g. a boost extending an
    in-progress charge) is trusted from its own start immediately - chargingState won't record a
    fresh transition if it was already Charging and stays that way."""
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


async def test_effective_now_uncapped_when_currently_charging(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator._charging_state_transitions_by_ppid[PPID] = [
        (now - datetime.timedelta(minutes=5), "Charging"),
    ]
    assert coordinator._effective_now(PPID, now) == now


async def test_effective_now_capped_when_not_currently_charging(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    stopped_at = now - datetime.timedelta(minutes=10)
    coordinator._charging_state_transitions_by_ppid[PPID] = [
        (now - datetime.timedelta(minutes=20), "Charging"),
        (stopped_at, "SuspendedEV"),
    ]
    assert coordinator._effective_now(PPID, now) == stopped_at


async def test_effective_now_uncapped_with_no_transitions_yet(
    coordinator: PodHomeDataUpdateCoordinator
) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    assert coordinator._effective_now(PPID, now) == now


async def test_current_charge_duration_schedule_start_trusted_end_freezes_on_observed_stop(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Reconstructs the scenario proven from a real HA history export: a schedule window opens
    well before chargingState actually reaches Charging - the front-edge gap is NOT suppressed
    (schedule starts are trusted, unlike a fresh override) - then once chargingState is observed
    leaving Charging (the vehicle finishing), duration freezes at that point instead of
    continuing to climb for as long as the window nominally stays open."""
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "INACTIVE"})]
    tz = ZoneInfo("Europe/London")  # matches charger_raw()'s default timezone
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

    coordinator.data = await coordinator._async_fetch_data()  # commit, as a real refresh would
    # The whole 2h session - trusted from session_start, not clipped to when Charging was
    # actually confirmed (11 minutes in).
    assert 7195 <= coordinator._current_charge_by_ppid[PPID].duration <= 7210

    # The vehicle finishes - chargingState leaves Charging.
    stopped_at = now - datetime.timedelta(minutes=30)
    api.async_connectivity_status.return_value = {
        "chargingState": "SuspendedEV", "lastSeenAt": stopped_at.isoformat(),
    }
    coordinator._charges_fetched_at = None  # session data due - refetches api3

    result = await coordinator._async_fetch_data()

    # Frozen at the observed stop, not still climbing toward the window's own (much later) end.
    frozen_duration = (stopped_at - session_start).total_seconds()
    assert frozen_duration - 5 <= result[PPID].current_charge.duration <= frozen_duration + 5


async def test_current_charge_duration_unknown_while_never_confirmed_energy(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """A vehicle that never actually draws power this session (already fully charged, say)
    stays at an honest None throughout, even with a schedule window open the whole time -
    not a guessed 0 or a value derived purely from the window being open."""
    api.async_list_chargers.return_value = [charger_raw(delegatedControl={"status": "INACTIVE"})]
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

    coordinator.data = await coordinator._async_fetch_data()
    assert coordinator._current_charge_by_ppid[PPID].duration is None

    coordinator._charges_fetched_at = None  # session data due - refetches api3
    result = await coordinator._async_fetch_data()
    assert result[PPID].current_charge.duration is None


async def test_vehicle_parsed_from_smart_charging_chargers_and_vehicles(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Shape below matches a real captured response (vehicles/currentIntent/intents.details
    nesting) - cross-checked against scratch/output locally, not copied from it."""
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


@pytest.mark.parametrize(
    ("previous_state", "current_state", "expect_fetch"),
    [
        ("SuspendedEV", "Available", True),  # unplugged after charging finished
        ("Charging", "Available", True),  # unplugged mid-charge
        ("Charging", "SuspendedEV", False),  # finished but still plugged in
        ("Charging", "Charging", False),
        ("Available", "Preparing", False),  # plugged in
        ("Unknown", "Available", True),  # previous state not known to be unplugged
    ],
)
async def test_finalized_charge_data_fetched_when_cable_unplugged(
    api: PodHomeApiClient,
    coordinator: PodHomeDataUpdateCoordinator,
    previous_state: str,
    current_state: str,
    expect_fetch: bool,
) -> None:
    """/charges lists a session once its cable is unplugged."""
    api.async_connectivity_status.return_value = {"chargingState": current_state}
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator.data = {PPID: make_charger(charging_state=previous_state)}
    coordinator._charges_fetched_at = now  # not otherwise stale

    await coordinator._async_fetch_data()

    assert api.async_charges.called is expect_fetch


async def test_api3_charges_fetched_same_poll_as_charging_transition(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator.data = {PPID: make_charger(charging_state="Available")}
    coordinator._api3_user_id = 123  # only fetched at all once this is known
    coordinator._charges_fetched_at = now  # session data not otherwise due

    await coordinator._async_fetch_data()

    api.async_api3_charges.assert_called_once()
    api.async_charges.assert_not_called()


@pytest.mark.parametrize("charges_fetched_ago", [None, datetime.timedelta(minutes=5)])
async def test_api3_and_charges_fetched_together_outside_charging(
    api: PodHomeApiClient,
    coordinator: PodHomeDataUpdateCoordinator,
    charges_fetched_ago: datetime.timedelta | None,
) -> None:
    """Outside charging, api3 is fetched only alongside /charges, and first - so a session api3
    already shows closed is never dropped from Total energy before /charges lists it."""
    api.async_connectivity_status.return_value = {"chargingState": "Available"}
    calls: list[str] = []
    api.async_api3_charges.side_effect = lambda *a, **k: calls.append("api3") or {}
    api.async_charges.side_effect = lambda *a, **k: calls.append("charges") or {}
    now = datetime.datetime.now(datetime.timezone.utc)
    coordinator.data = {PPID: make_charger(charging_state="Available")}
    coordinator._api3_user_id = 123
    coordinator._charges_fetched_at = (
        now - charges_fetched_ago if charges_fetched_ago is not None else None
    )

    await coordinator._async_fetch_data()

    assert calls == (["api3", "charges"] if charges_fetched_ago is None else [])


async def test_connectivity_fetched_once_per_ppid_per_poll(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Guards against the connectivity preflight and the per-charger loop both fetching it -
    same total call count as before the same-poll restructuring, just resolved earlier."""

    await coordinator._async_fetch_data()

    assert api.async_connectivity_status.call_count == 1


async def test_remote_lock_status_parsed(hass: HomeAssistant) -> None:
    """offMode: null is a genuine, confirmed-live response shape (unsupported charger model) - a
    successful fetch, not treated as missing data. Two separate coordinators here purely for a
    clean before/after comparison."""
    api = make_stub_api()
    api.async_get_remote_lock_status.return_value = {"offMode": None}
    result = await _make_coordinator(hass, api)._async_fetch_data()
    assert result[PPID].remote_lock_off_mode is None

    api = make_stub_api()
    api.async_get_remote_lock_status.return_value = {"offMode": True}
    result = await _make_coordinator(hass, api)._async_fetch_data()
    assert result[PPID].remote_lock_off_mode is True


@pytest.mark.parametrize(("off_mode", "expected_calls"), [(None, 1), (True, 2), (False, 2)])
async def test_remote_lock_polled_every_poll_only_once_set(
    api: PodHomeApiClient,
    coordinator: PodHomeDataUpdateCoordinator,
    off_mode: bool | None,
    expected_calls: int,
) -> None:
    """A charger reporting offMode: null (unsupported) is re-checked on the slow
    cadence; one with a real lock state is polled every poll so app-side changes show up."""
    api.async_get_remote_lock_status.return_value = {"offMode": off_mode}

    await coordinator._async_fetch_data()
    await coordinator._async_fetch_data()

    assert api.async_get_remote_lock_status.call_count == expected_calls


_PREVIOUS_SESSION = {
    "id": "c0",
    "startedAt": "2026-09-20T23:30:00Z",
    "endedAt": "2026-09-21T04:30:00Z",
    "energyTotal": 29.5,
    "charger": {"id": PPID},
}
_SESSION = {
    "id": "c1",
    "startedAt": "2026-09-23T19:25:34Z",
    "endedAt": "2026-09-24T05:17:35Z",
    "energyTotal": 64,
    "charger": {"id": PPID},
}
_API3_OPEN = {
    "id": 1, "starts_at": "2026-09-23T19:25:34Z", "ends_at": None, "kwh_used": 63.7,
    "pod": {"id": 555},
}
_API3_CLOSED = {
    **_API3_OPEN, "ends_at": "2026-09-24T05:25:39Z", "kwh_used": 64,
    "charging_duration": {"raw": 35521},
}


def _session_end_scenario(hass: HomeAssistant):
    """A coordinator plus a poll(charging_state, api3_entry, charges) helper that records Total
    energy after each poll - for walking one session through its end."""
    api = make_stub_api()
    api.async_create_api3_session.return_value = {"sessions": {"user_id": 999}}
    api.async_api3_pods.return_value = {"pods": [{"ppid": PPID, "unit_id": 555}]}
    coordinator = _make_coordinator(hass, api)
    totals: list[float | None] = []

    async def poll(charging_state: str, api3_entry: dict, charges: list[dict]) -> None:
        api.async_connectivity_status.return_value = {"chargingState": charging_state}
        api.async_api3_charges.return_value = {"charges": [api3_entry]}
        api.async_charges.return_value = {"data": {"charges": charges}}
        coordinator.data = await coordinator._async_fetch_data()
        totals.append(sensor.PodHomeTotalEnergySensor(coordinator, PPID).native_value)

    return coordinator, poll, totals


async def test_total_energy_never_drops_across_session_end(hass: HomeAssistant) -> None:
    """Charging, then finished with the cable still in, then unplugged: api3 closes the session
    and /charges lists it at unplug, and Total energy swaps live for finalized in one poll."""
    _, poll, totals = _session_end_scenario(hass)
    with freeze_time("2026-09-24T05:00:00Z"):
        await poll("Charging", _API3_OPEN, [_PREVIOUS_SESSION])
    with freeze_time("2026-09-24T05:18:00Z"):
        await poll("SuspendedEV", {**_API3_OPEN, "kwh_used": 64}, [_PREVIOUS_SESSION])
    with freeze_time("2026-09-24T05:26:00Z"):
        await poll("Available", _API3_CLOSED, [_SESSION, _PREVIOUS_SESSION])

    assert totals == sorted(totals)
    assert totals[-1] == 64  # the earlier session ended before tracking started


async def test_total_energy_held_while_charges_lags_api3(hass: HomeAssistant) -> None:
    """api3 already shows the session closed at unplug but /charges lists it a poll later: the
    finished api3 session stays current (matched to /charges by started_at) meanwhile."""
    coordinator, poll, totals = _session_end_scenario(hass)
    with freeze_time("2026-09-24T05:00:00Z"):
        await poll("Charging", _API3_OPEN, [_PREVIOUS_SESSION])
    with freeze_time("2026-09-24T05:26:00Z"):
        await poll("Available", _API3_CLOSED, [_PREVIOUS_SESSION])  # /charges not listed yet
        held = coordinator.data[PPID].current_charge
        assert held is not None and held.energy_total == 64 and held.duration == 35521
    with freeze_time("2026-09-24T05:51:00Z"):
        await poll("Available", _API3_CLOSED, [_SESSION, _PREVIOUS_SESSION])
        charger = coordinator.data[PPID]
        assert select_last_charge(charger.current_charge, charger.latest_charge) is (
            charger.latest_charge
        )

    assert totals == [63.7, 64, 64]


async def test_api3_charges_failure_retried_without_charges(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """A failed api3 charges fetch is retried next poll on its own; /charges stays on its
    schedule."""
    api.async_connectivity_status.return_value = {"chargingState": "Available"}
    api.async_api3_charges.side_effect = [PodHomeApiError(500, {}), {"charges": []}]
    coordinator.data = {PPID: make_charger(charging_state="Available")}
    coordinator._api3_user_id = 123

    for _ in range(3):  # fails, retried and succeeds, then nothing due
        await coordinator._async_fetch_data()

    assert api.async_api3_charges.await_count == 2
    assert api.async_charges.await_count == 1


@pytest.mark.parametrize(
    ("latest_started_at", "ended_ago", "expect_held"),
    [
        (datetime.datetime(2026, 9, 20, 23, 30, tzinfo=datetime.timezone.utc), 1, True),
        (datetime.datetime(2026, 9, 23, 19, 25, 34, tzinfo=datetime.timezone.utc), 1, False),
        (None, 1, True),  # nothing listed yet
        (datetime.datetime(2026, 9, 20, 23, 30, tzinfo=datetime.timezone.utc), 25 * 60, False),
    ],
)
async def test_finished_api3_session_held_only_until_listed(
    api: PodHomeApiClient,
    coordinator: PodHomeDataUpdateCoordinator,
    latest_started_at: datetime.datetime | None,
    ended_ago: int,
    expect_held: bool,
) -> None:
    ended_at = datetime.datetime(2026, 9, 24, 5, 25, 39, tzinfo=datetime.timezone.utc)
    api.async_api3_charges.return_value = {
        "charges": [
            {"id": 1, "starts_at": "2026-09-23T19:25:34Z", "ends_at": ended_at.isoformat(),
             "kwh_used": 64, "pod": {"id": 555}},
        ]
    }
    coordinator._api3_pod_id_by_ppid = {PPID: 555}
    coordinator._total_started_at_by_ppid[PPID] = ended_at - datetime.timedelta(days=30)
    if latest_started_at is not None:
        coordinator._latest_charge_by_ppid[PPID] = make_charge(started_at=latest_started_at)

    await coordinator._async_refresh_api3_charges(
        999, ended_at + datetime.timedelta(minutes=ended_ago)
    )

    assert (PPID in coordinator._current_charge_by_ppid) is expect_held


def _charge_entry(started_at: str, energy: float, cost: int, ppid: str = PPID) -> dict:
    return {"startedAt": started_at, "endedAt": started_at, "energyTotal": energy,
            "cost": {"amount": cost}, "charger": {"id": ppid}}


@freeze_time("2026-10-18T12:00:00Z")
async def test_month_totals_summed_from_charges_by_local_start(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Month energy/cost sum the sessions that started this charger-local calendar month - the
    attribution the app uses."""
    api.async_charges.return_value = {
        "data": {
            "charges": [
                _charge_entry("2026-09-30T23:30:00Z", 10, 100),  # 00:30 BST on 1 October
                _charge_entry("2026-10-12T20:00:00Z", 5.2, 60),
                _charge_entry("2026-09-23T19:25:34Z", 64, 1173),  # September
                _charge_entry("2026-10-05T20:00:00Z", 7, 70, ppid="PSL-OTHER"),
                {**_charge_entry("2026-10-09T17:32:24Z", 4, 0), "endedAt": None, "cost": None},
            ]
        }
    }

    result = await coordinator._async_fetch_data()

    assert result[PPID].month_energy_kwh == 15.2
    assert result[PPID].month_cost_amount == 160
    api.async_charges.assert_awaited_once_with(
        datetime.date(2026, 9, 18), datetime.date(2026, 10, 18)
    )


@freeze_time("2026-10-18T12:00:00Z")
async def test_month_cost_unknown_when_an_entry_has_no_cost(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_charges.return_value = {
        "data": {
            "charges": [
                _charge_entry("2026-10-12T20:00:00Z", 5.2, 60),
                {**_charge_entry("2026-10-14T20:00:00Z", 3, 0), "cost": None},
            ]
        }
    }

    result = await coordinator._async_fetch_data()

    assert result[PPID].month_energy_kwh == 8.2
    assert result[PPID].month_cost_amount is None


async def test_month_totals_unknown_until_charges_fetched(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_charges.side_effect = PodHomeApiError(500, {})

    result = await coordinator._async_fetch_data()

    assert result[PPID].month_energy_kwh is None
    assert result[PPID].month_cost_amount is None


@freeze_time("2026-10-31T12:00:00Z")
async def test_charges_window_reaches_before_local_month_start(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """On the 31st, today - 30 is the 1st itself - the window still starts the day before."""

    await coordinator._async_fetch_data()

    api.async_charges.assert_awaited_once_with(
        datetime.date(2026, 9, 30), datetime.date(2026, 10, 31)
    )


@freeze_time("2026-11-01T00:30:00Z")
async def test_month_totals_reset_at_local_midnight_without_refetch(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """Totals are recomputed each poll from the stored sessions, so the 1st's local midnight
    resets them even when /charges isn't due."""
    coordinator._charge_entries = [
        (PPID, _charge_entry("2026-10-20T20:00:00Z", 30, 300), {"id": PPID})
    ]
    coordinator._charges_fetched_at = datetime.datetime.now(datetime.timezone.utc)

    result = await coordinator._async_fetch_data()

    api.async_charges.assert_not_called()
    assert result[PPID].month_energy_kwh == 0
    assert result[PPID].month_cost_amount == 0


@pytest.mark.parametrize(
    ("fetched_ago", "expect_fetch"),
    [(datetime.timedelta(minutes=45), False), (datetime.timedelta(hours=6), True)],
)
async def test_charges_safety_refresh_every_six_hours(
    api: PodHomeApiClient,
    coordinator: PodHomeDataUpdateCoordinator,
    fetched_ago: datetime.timedelta,
    expect_fetch: bool,
) -> None:
    """With no unplug and nothing awaiting listing, /charges and api3 are only re-fetched on
    the 6-hour safety net."""
    api.async_connectivity_status.return_value = {"chargingState": "Available"}
    coordinator.data = {PPID: make_charger(charging_state="Available")}
    coordinator._api3_user_id = 123
    coordinator._charges_fetched_at = datetime.datetime.now(datetime.timezone.utc) - fetched_ago

    await coordinator._async_fetch_data()

    assert api.async_charges.called is expect_fetch
    assert api.async_api3_charges.called is expect_fetch


async def test_charges_fetched_after_unplug_until_session_listed(hass: HomeAssistant) -> None:
    """After an unplug /charges is fetched every poll until it lists the session api3 closed,
    then back to the 6-hour safety net."""
    coordinator, poll, _ = _session_end_scenario(hass)
    api = coordinator.api
    fetched: list[bool] = []

    async def poll_at(moment: str, charges: list[dict]) -> None:
        before = api.async_charges.await_count
        with freeze_time(moment):
            await poll("Available", _API3_CLOSED, charges)
        fetched.append(api.async_charges.await_count > before)

    coordinator._total_started_at_by_ppid[PPID] = datetime.datetime(
        2026, 9, 1, tzinfo=datetime.timezone.utc
    )
    with freeze_time("2026-09-24T05:00:00Z"):
        await poll("Charging", _API3_OPEN, [_PREVIOUS_SESSION])
    await poll_at("2026-09-24T05:26:00Z", [_PREVIOUS_SESSION])  # unplug, not listed yet
    await poll_at("2026-09-24T05:28:00Z", [_PREVIOUS_SESSION])
    await poll_at("2026-09-24T05:31:00Z", [_PREVIOUS_SESSION])
    await poll_at("2026-09-24T05:36:00Z", [_SESSION, _PREVIOUS_SESSION])  # listed
    await poll_at("2026-09-24T05:41:00Z", [_SESSION, _PREVIOUS_SESSION])  # stopped

    assert fetched == [True, True, True, True, False]
    assert coordinator.data[PPID].latest_charge.energy_total == 64


@pytest.mark.parametrize(
    ("api3_after_unplug", "charges", "api3_mapping"),
    [
        (_API3_OPEN, [_SESSION, _PREVIOUS_SESSION], "ok"),  # api3 not closed yet
        (_API3_CLOSED, [_PREVIOUS_SESSION], "ok"),  # closed but never listed
        (_API3_CLOSED, [_SESSION, _PREVIOUS_SESSION], "no_session"),  # no api3 at all
        (_API3_CLOSED, [_SESSION, _PREVIOUS_SESSION], "no_pods"),  # api3 can't match the charger
    ],
)
async def test_charges_fetched_for_whole_window_until_listing_confirmed(
    hass: HomeAssistant, api3_after_unplug: dict, charges: list[dict], api3_mapping: str
) -> None:
    """Until /charges is seen listing the charger's closed api3 session, it keeps refreshing
    for the full 30 minutes after an unplug."""
    coordinator, poll, _ = _session_end_scenario(hass)
    api = coordinator.api
    if api3_mapping == "no_session":
        api.async_create_api3_session.side_effect = PodHomeApiError(500, {})
    elif api3_mapping == "no_pods":
        api.async_api3_pods.return_value = {"pods": []}
    coordinator._total_started_at_by_ppid[PPID] = datetime.datetime(
        2026, 9, 1, tzinfo=datetime.timezone.utc
    )
    with freeze_time("2026-09-24T05:00:00Z"):
        await poll("Charging", _API3_OPEN, [_PREVIOUS_SESSION])
    fetched: list[bool] = []
    for moment in ("05:26", "05:31", "05:36", "06:10"):
        before = api.async_charges.await_count
        with freeze_time(f"2026-09-24T{moment}:00Z"):
            await poll("Available", api3_after_unplug, charges)
        fetched.append(api.async_charges.await_count > before)

    assert fetched == [True, True, True, False]


async def test_one_chargers_open_session_doesnt_extend_anothers_window(
    hass: HomeAssistant,
) -> None:
    """Each charger's unplug window ends when its own session is listed."""
    coordinator = _make_coordinator(hass, make_stub_api())
    now = datetime.datetime(2026, 9, 24, 5, 30, tzinfo=datetime.timezone.utc)
    started = datetime.datetime(2026, 9, 23, 19, 25, 34, tzinfo=datetime.timezone.utc)
    coordinator._api3_newest_finished_by_ppid = {
        PPID: make_charge(started_at=started, ended_at=now)
    }
    coordinator._latest_charge_by_ppid[PPID] = make_charge(started_at=started)
    coordinator._current_charge_by_ppid = {"PSL-OTHER": make_charge(ended_at=None)}

    assert coordinator._session_listed(PPID) is True
    assert coordinator._session_listed("PSL-OTHER") is False


async def test_session_ended_before_tracking_start_not_held(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """A fresh install whose api3 shows a session closed before tracking started: holding it
    would count it live, then drop it once /charges lists it (the baseline skips it)."""
    ended_at = datetime.datetime(2026, 9, 24, 5, 25, 39, tzinfo=datetime.timezone.utc)
    api.async_api3_charges.return_value = {"charges": [_API3_CLOSED]}
    coordinator._api3_pod_id_by_ppid = {PPID: 555}

    await coordinator._async_refresh_api3_charges(999, ended_at + datetime.timedelta(minutes=30))

    assert coordinator._current_charge_by_ppid == {}


async def test_month_totals_fall_back_to_ha_time_zone(hass: HomeAssistant) -> None:
    """A charger with no usable timezone buckets sessions in HA's configured zone, the same
    zone its month start comes from - not the host system's."""
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("America/Los_Angeles"))
    try:
        coordinator = _make_coordinator(hass, make_stub_api())
        coordinator._charge_entries = [
            # 23:30 on 30 September in Los Angeles - still 1 October in UTC.
            (PPID, _charge_entry("2026-10-01T06:30:00Z", 10, 100), {"id": PPID}),
            (PPID, _charge_entry("2026-10-02T06:30:00Z", 5, 50), {"id": PPID}),
        ]
        totals = coordinator._month_to_date_totals(PPID, None, datetime.date(2026, 10, 1))
    finally:
        dt_util.set_default_time_zone(previous)

    assert totals == (5, 50)


async def test_api3_failure_while_charging_retries_after_charging_stops(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """A failed api3 fetch on the last charging poll still gets retried once charging stops."""
    api.async_api3_charges.side_effect = [PodHomeApiError(500, {}), {"charges": []}]
    coordinator._api3_user_id = 123
    coordinator._charges_fetched_at = datetime.datetime.now(datetime.timezone.utc)
    coordinator.data = {PPID: make_charger(charging_state="Charging")}

    api.async_connectivity_status.return_value = {"chargingState": "Charging"}
    coordinator.data = await coordinator._async_fetch_data()
    api.async_connectivity_status.return_value = {"chargingState": "SuspendedEV"}
    await coordinator._async_fetch_data()

    assert api.async_api3_charges.await_count == 2


async def test_vehicles_fetched_every_poll(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    api.async_connectivity_status.return_value = {"chargingState": "Available"}

    coordinator.data = await coordinator._async_fetch_data()
    await coordinator._async_fetch_data()

    assert api.async_smart_charging_chargers_and_vehicles.await_count == 2


@freeze_time("2026-10-18T12:00:00Z")
@pytest.mark.parametrize(
    ("last_seen_ago", "plugged_in_ago", "expected_delay"),
    [
        # Idle: next poll 5 minutes + 5 seconds after the latest check-in.
        (datetime.timedelta(minutes=2), None, datetime.timedelta(minutes=3, seconds=5)),
        # Check-in overdue - poll again in a full slow interval from now.
        (datetime.timedelta(minutes=6), None, SLOW_POLL_INTERVAL),
        # Recent plug-in: 60 seconds + 5 seconds after the latest check-in...
        (datetime.timedelta(seconds=30), datetime.timedelta(minutes=1),
         datetime.timedelta(seconds=35)),
        # ...or a full fast interval from now once that's passed.
        (datetime.timedelta(minutes=2), datetime.timedelta(minutes=1), FAST_POLL_INTERVAL),
        # Check-in due within the buffer - never less than the buffer.
        (datetime.timedelta(minutes=5, seconds=4.5), None, datetime.timedelta(seconds=5)),
        # Charger clock ahead of ours - never more than one interval plus the buffer.
        (-datetime.timedelta(minutes=1), None, SLOW_POLL_INTERVAL + datetime.timedelta(seconds=5)),
    ],
)
async def test_poll_timed_from_latest_check_in(
    hass: HomeAssistant,
    last_seen_ago: datetime.timedelta,
    plugged_in_ago: datetime.timedelta | None,
    expected_delay: datetime.timedelta,
) -> None:
    coordinator = _make_coordinator(hass, make_stub_api())
    now = datetime.datetime(2026, 10, 18, 12, 0, tzinfo=datetime.timezone.utc)
    if plugged_in_ago is not None:
        coordinator._plugged_in_at[PPID] = now - plugged_in_ago

    coordinator._async_adjust_poll_interval({PPID: make_charger(last_seen_at=now - last_seen_ago)})

    assert coordinator.update_interval == expected_delay


async def test_missing_charging_state_doesnt_trigger_fast_polling(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """A poll whose connectivity call failed (no chargingState) doesn't make the next reading
    look like a plug-in."""
    api.async_connectivity_status.return_value = {
        "chargingState": "Available", "lastSeenAt": "2026-10-18T11:00:00Z",
    }
    with freeze_time("2026-10-18T11:01:00Z"):
        coordinator.data = await coordinator._async_fetch_data()  # first sight
    api.async_connectivity_status.side_effect = PodHomeApiError(500, {})
    with freeze_time("2026-10-18T11:50:00Z"):
        coordinator.data = await coordinator._async_fetch_data()
    api.async_connectivity_status.side_effect = None
    api.async_connectivity_status.return_value = {
        "chargingState": "Available", "lastSeenAt": "2026-10-18T11:54:00Z",
    }
    with freeze_time("2026-10-18T11:55:00Z"):
        coordinator.data = await coordinator._async_fetch_data()

    assert coordinator.update_interval == datetime.timedelta(minutes=4, seconds=5)


@pytest.mark.parametrize(
    ("states", "expect_fast"),
    [
        (["Available", "SuspendedEV"], True),  # plugged in, not charging yet
        (["Available", "SuspendedEV", "Charging"], False),  # charging started
        (["Available", "Charging"], False),  # charging straight away
        (["Charging", "SuspendedEV"], False),  # charging finished
        (["SuspendedEV", "Available"], False),  # unplugged
        (["SuspendedEV"], False),  # first sight
    ],
)
async def test_fast_polling_only_while_awaiting_charge_after_plug_in(
    api: PodHomeApiClient,
    coordinator: PodHomeDataUpdateCoordinator,
    states: list[str],
    expect_fast: bool,
) -> None:
    for minute, state in enumerate(states):
        api.async_connectivity_status.return_value = {
            "chargingState": state, "lastSeenAt": f"2026-10-18T12:0{minute}:00Z",
        }
        with freeze_time(f"2026-10-18T12:0{minute}:30Z"):
            coordinator.data = await coordinator._async_fetch_data()

    expected = datetime.timedelta(seconds=35) if expect_fast else datetime.timedelta(
        minutes=4, seconds=35
    )
    assert coordinator.update_interval == expected


async def test_routine_check_ins_alone_dont_keep_polling_fast(
    api: PodHomeApiClient, coordinator: PodHomeDataUpdateCoordinator
) -> None:
    """lastSeenAt advancing every few minutes is routine - only a plug-in switches to fast
    polling."""
    api.async_connectivity_status.return_value = {
        "chargingState": "Available", "lastSeenAt": "2026-10-18T11:50:00Z",
    }
    with freeze_time("2026-10-18T11:51:00Z"):
        coordinator.data = await coordinator._async_fetch_data()  # first sight
    api.async_connectivity_status.return_value = {
        "chargingState": "Available", "lastSeenAt": "2026-10-18T11:59:00Z",
    }
    with freeze_time("2026-10-18T12:00:00Z"):
        coordinator.data = await coordinator._async_fetch_data()

    assert coordinator.update_interval == datetime.timedelta(minutes=4, seconds=5)
