"""start_boost/cancel_boost services (services.py) - registered once, hass-level, in
__init__.py's async_setup (see tests/conftest.py's auto_enable_custom_integrations, which is
what actually lets HA find and load pod_home for these tests, same as tests/test_init.py).
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.pod_home.const import DOMAIN
from custom_components.pod_home.podpoint_mobile_api import PodHomeAuthError
from custom_components.pod_home.services import (
    SERVICE_CANCEL_BOOST,
    SERVICE_START_BOOST,
    async_setup_services,
)
from tests._fixtures import PPID, make_charger, make_coordinator

pytestmark = pytest.mark.asyncio



async def _setup(hass: HomeAssistant, **charger_overrides):
    """A coordinator with one charger, a real device registered against it (so a service call
    can target it by device_id, same as a real automation would), and the services actually
    registered - mirrors what __init__.py's async_setup/async_setup_entry do together in
    production, without going through full config-entry setup (tests/test_init.py already covers
    that layer)."""
    charger = make_charger(ppid=PPID, **charger_overrides)
    coordinator = make_coordinator(hass, {PPID: charger})
    coordinator.config_entry.runtime_data = coordinator
    coordinator.async_request_refresh = AsyncMock()

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, PPID)},
    )
    async_setup_services(hass)
    return coordinator, device.id


async def test_start_boost_full_charge_when_no_duration_given(hass: HomeAssistant) -> None:
    coordinator, device_id = await _setup(hass, charging_state="Charging")

    await hass.services.async_call(
        DOMAIN, SERVICE_START_BOOST, {"device_id": device_id}, blocking=True
    )

    coordinator.api.async_create_charge_override.assert_awaited_once()
    _, kwargs = coordinator.api.async_create_charge_override.call_args
    assert kwargs["end_at"] - kwargs["requested_at"] == datetime.timedelta(hours=12)
    coordinator.async_request_refresh.assert_awaited_once()


async def test_start_boost_auth_error_triggers_refresh_and_clean_failure(
    hass: HomeAssistant,
) -> None:
    coordinator, device_id = await _setup(hass, charging_state="Charging")
    coordinator.api.async_create_charge_override.side_effect = PodHomeAuthError("expired")

    with pytest.raises(HomeAssistantError, match="rejected"):
        await hass.services.async_call(
            DOMAIN, SERVICE_START_BOOST, {"device_id": device_id}, blocking=True
        )

    coordinator.async_request_refresh.assert_awaited_once()


async def test_start_boost_transient_auth_error_reports_connection_failure(
    hass: HomeAssistant,
) -> None:
    coordinator, device_id = await _setup(hass, charging_state="Charging")
    coordinator.api.async_create_charge_override.side_effect = PodHomeAuthError(
        "timed out", transient=True
    )

    with pytest.raises(HomeAssistantError, match="Couldn't reach Pod Point"):
        await hass.services.async_call(
            DOMAIN, SERVICE_START_BOOST, {"device_id": device_id}, blocking=True
        )


async def test_start_boost_with_duration(hass: HomeAssistant) -> None:
    coordinator, device_id = await _setup(hass, charging_state="Charging")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_START_BOOST,
        {"device_id": device_id, "duration": {"hours": 0, "minutes": 30}},
        blocking=True,
    )

    _, kwargs = coordinator.api.async_create_charge_override.call_args
    assert kwargs["end_at"] - kwargs["requested_at"] == datetime.timedelta(minutes=30)


async def test_start_boost_allowed_with_cable_unplugged(hass: HomeAssistant) -> None:
    """Lets an automation start a boost ahead of plug-in, e.g. for a supplier's free period."""
    coordinator, device_id = await _setup(hass, charging_state="Available")

    await hass.services.async_call(
        DOMAIN, SERVICE_START_BOOST, {"device_id": device_id}, blocking=True
    )

    coordinator.api.async_create_charge_override.assert_awaited_once()


async def test_start_boost_rejects_during_always_on(hass: HomeAssistant) -> None:
    coordinator, device_id = await _setup(
        hass, charging_state="Charging", always_on_active=True
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_START_BOOST, {"device_id": device_id}, blocking=True
        )
    coordinator.api.async_create_charge_override.assert_not_awaited()


@freeze_time("2026-11-15T12:00:00Z")
@pytest.mark.parametrize(
    ("start", "expected_requested", "expected_end"),
    [
        # Later today: requested then, ending a duration after.
        ("2026-11-15T13:00:00+00:00", "2026-11-15T13:00:00+00:00", "2026-11-15T13:30:00+00:00"),
        # Already started: sent as now, same end.
        ("2026-11-15T11:50:00+00:00", "2026-11-15T12:00:00+00:00", "2026-11-15T12:20:00+00:00"),
    ],
)
async def test_start_boost_with_start(
    hass: HomeAssistant, start: str, expected_requested: str, expected_end: str
) -> None:
    coordinator, device_id = await _setup(hass, charging_state="Available")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_START_BOOST,
        {"device_id": device_id, "start": start, "duration": {"minutes": 30}},
        blocking=True,
    )

    _, kwargs = coordinator.api.async_create_charge_override.call_args
    assert kwargs["requested_at"] == datetime.datetime.fromisoformat(expected_requested)
    assert kwargs["end_at"] == datetime.datetime.fromisoformat(expected_end)


@freeze_time("2026-11-15T12:00:00Z")
async def test_start_boost_rejects_a_window_already_over(hass: HomeAssistant) -> None:
    coordinator, device_id = await _setup(hass, charging_state="Available")

    with pytest.raises(ServiceValidationError, match="already have ended"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_START_BOOST,
            {"device_id": device_id, "start": "2026-11-15T11:00:00+00:00",
             "duration": {"minutes": 30}},
            blocking=True,
        )
    coordinator.api.async_create_charge_override.assert_not_awaited()


@pytest.mark.parametrize("duration", ["00:00:00", "-01:00:00"])
async def test_start_boost_rejects_a_duration_of_zero_or_less(
    hass: HomeAssistant, duration: str
) -> None:
    coordinator, device_id = await _setup(hass, charging_state="Charging")

    with pytest.raises(ServiceValidationError, match="more than zero"):
        await hass.services.async_call(
            DOMAIN, SERVICE_START_BOOST, {"device_id": device_id, "duration": duration},
            blocking=True,
        )
    coordinator.api.async_create_charge_override.assert_not_awaited()


async def test_start_boost_rejects_unknown_always_on_state(hass: HomeAssistant) -> None:
    coordinator, device_id = await _setup(hass, charging_state="Charging", always_on_active=None)

    with pytest.raises(ServiceValidationError, match="can't tell yet"):
        await hass.services.async_call(
            DOMAIN, SERVICE_START_BOOST, {"device_id": device_id}, blocking=True
        )
    coordinator.api.async_create_charge_override.assert_not_awaited()


async def test_start_boost_rejects_a_non_charger_device(hass: HomeAssistant) -> None:
    coordinator, _ = await _setup(hass, charging_state="Charging")
    other_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id,
        identifiers={(DOMAIN, "some-vehicle-id")},
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_START_BOOST, {"device_id": other_device.id}, blocking=True
        )


async def test_cancel_boost_deletes_the_active_override(hass: HomeAssistant) -> None:
    coordinator, device_id = await _setup(
        hass, boost_end_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    )

    await hass.services.async_call(
        DOMAIN, SERVICE_CANCEL_BOOST, {"device_id": device_id}, blocking=True
    )

    coordinator.api.async_delete_charge_override.assert_awaited_once_with(PPID)
    coordinator.async_request_refresh.assert_awaited_once()


async def test_cancel_boost_rejects_when_nothing_active(hass: HomeAssistant) -> None:
    coordinator, device_id = await _setup(hass, boost_end_at=None)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_CANCEL_BOOST, {"device_id": device_id}, blocking=True
        )
    coordinator.api.async_delete_charge_override.assert_not_awaited()
