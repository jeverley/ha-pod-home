"""Button entity tests, using pytest-homeassistant-custom-component's real `hass` fixture and the
shared factories in tests/_fixtures.py.
"""
from __future__ import annotations

import datetime
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

import custom_components.pod_home.button as button
from custom_components.pod_home.const import DOMAIN
from tests._fixtures import make_charger, make_coordinator

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"
UTC = datetime.timezone.utc


def _register_boost_duration(hass: HomeAssistant, ppid: str, state: str) -> str:
    """Registers the Boost duration time entity in the real entity registry + state machine -
    _read_boost_duration() (button.py) reads it via that path, not a direct object reference."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "time", DOMAIN, f"{DOMAIN}_{ppid}_boost_duration", suggested_object_id=f"{ppid}_boost_duration"
    )
    hass.states.async_set(entry.entity_id, state)
    return entry.entity_id


# --- Full charge ---


async def test_full_charge_available_only_with_cable_connected(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(charging_state="Charging")})
    entity = button.PodHomeBoostFullChargeButton(coordinator, PPID)
    assert entity.available is True

    coordinator.data = {PPID: make_charger(charging_state="Available")}  # cable unplugged
    assert entity.available is False


async def test_full_charge_press_sends_flat_12h_end_at(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    coordinator.async_request_refresh = AsyncMock()
    entity = button.PodHomeBoostFullChargeButton(coordinator, PPID)

    await entity.async_press()

    coordinator.api.async_create_charge_override.assert_called_once()
    call_kwargs = coordinator.api.async_create_charge_override.call_args
    assert call_kwargs.args[0] == PPID
    requested_at = call_kwargs.kwargs["requested_at"]
    end_at = call_kwargs.kwargs["end_at"]
    assert end_at - requested_at == datetime.timedelta(hours=12)
    coordinator.async_request_refresh.assert_called_once()


# --- Boost for duration ---


async def test_boost_duration_press_uses_registered_time_value_and_resets(
    hass: HomeAssistant,
) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    coordinator.async_request_refresh = AsyncMock()
    _register_boost_duration(hass, PPID, "00:30:00")
    reset_entity = AsyncMock()
    coordinator.boost_duration_entities[PPID] = reset_entity

    entity = button.PodHomeBoostDurationButton(coordinator, PPID)
    await entity.async_press()

    call_kwargs = coordinator.api.async_create_charge_override.call_args
    requested_at = call_kwargs.kwargs["requested_at"]
    end_at = call_kwargs.kwargs["end_at"]
    assert end_at - requested_at == datetime.timedelta(minutes=30)
    coordinator.async_request_refresh.assert_called_once()
    reset_entity.async_reset.assert_called_once()


async def test_boost_duration_press_without_reset_entity_registered_does_not_crash(
    hass: HomeAssistant,
) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    coordinator.async_request_refresh = AsyncMock()
    _register_boost_duration(hass, PPID, "00:15:00")
    # No entry in coordinator.boost_duration_entities - press must still succeed.

    entity = button.PodHomeBoostDurationButton(coordinator, PPID)
    await entity.async_press()
    coordinator.api.async_create_charge_override.assert_called_once()


async def test_boost_duration_press_unset_raises_clean_error(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    _register_boost_duration(hass, PPID, "unknown")
    entity = button.PodHomeBoostDurationButton(coordinator, PPID)
    with pytest.raises(HomeAssistantError, match="Enter a Boost duration"):
        await entity.async_press()
    coordinator.api.async_create_charge_override.assert_not_called()


async def test_boost_duration_press_zero_raises_clean_error(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    _register_boost_duration(hass, PPID, "00:00:00")
    entity = button.PodHomeBoostDurationButton(coordinator, PPID)
    with pytest.raises(HomeAssistantError, match="greater than zero"):
        await entity.async_press()
    coordinator.api.async_create_charge_override.assert_not_called()


async def test_boost_duration_available_only_with_cable_connected(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(charging_state="Charging")})
    entity = button.PodHomeBoostDurationButton(coordinator, PPID)
    assert entity.available is True

    coordinator.data = {PPID: make_charger(charging_state="Available")}
    assert entity.available is False


# --- Cancel boost ---


async def test_cancel_boost_available_only_when_boost_active(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(
        hass, {PPID: make_charger(boost_end_at=datetime.datetime(2026, 1, 1, tzinfo=UTC))}
    )
    entity = button.PodHomeCancelBoostButton(coordinator, PPID)
    assert entity.available is True

    coordinator.data = {PPID: make_charger(boost_end_at=None)}
    assert entity.available is False


async def test_cancel_boost_press_deletes_and_refreshes(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    coordinator.async_request_refresh = AsyncMock()
    entity = button.PodHomeCancelBoostButton(coordinator, PPID)

    await entity.async_press()

    coordinator.api.async_delete_charge_override.assert_called_once_with(PPID)
    coordinator.async_request_refresh.assert_called_once()
