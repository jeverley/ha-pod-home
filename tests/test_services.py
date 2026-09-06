"""start_boost/cancel_boost services (services.py) - registered once, hass-level, in
__init__.py's async_setup (see tests/conftest.py's auto_enable_custom_integrations, which is
what actually lets HA find and load pod_home for these tests, same as tests/test_init.py).
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.pod_home.const import DOMAIN
from custom_components.pod_home.services import (
    SERVICE_CANCEL_BOOST,
    SERVICE_START_BOOST,
    async_setup_services,
)
from tests._fixtures import make_charger, make_coordinator

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


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


async def test_start_boost_rejects_unplugged_cable(hass: HomeAssistant) -> None:
    coordinator, device_id = await _setup(hass, charging_state="Available")

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_START_BOOST, {"device_id": device_id}, blocking=True
        )
    coordinator.api.async_create_charge_override.assert_not_awaited()


async def test_start_boost_rejects_during_always_on(hass: HomeAssistant) -> None:
    coordinator, device_id = await _setup(
        hass, charging_state="Charging", always_on_active=True
    )

    with pytest.raises(ServiceValidationError):
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
