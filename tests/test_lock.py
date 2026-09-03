"""Lock entity tests, using pytest-homeassistant-custom-component's real `hass` fixture and the
shared factories in tests/_fixtures.py."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

import custom_components.pod_home.lock as lock
from custom_components.pod_home.podpoint_mobile_api import PodHomeApiError
from tests._fixtures import make_charger, make_coordinator

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


async def test_is_locked_reflects_off_mode(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(remote_lock_off_mode=True)})
    entity = lock.PodHomeRemoteLock(coordinator, PPID)
    assert entity.is_locked is True

    coordinator.data = {PPID: make_charger(remote_lock_off_mode=False)}
    assert entity.is_locked is False


async def test_is_locked_none_when_unsupported_or_unset(hass: HomeAssistant) -> None:
    """offMode: null - confirmed live for a charger model that doesn't support Remote Lock."""
    coordinator = make_coordinator(hass, {PPID: make_charger(remote_lock_off_mode=None)})
    entity = lock.PodHomeRemoteLock(coordinator, PPID)
    assert entity.is_locked is None


async def test_async_lock_sends_off_mode_true_and_refreshes(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    coordinator.async_request_refresh = AsyncMock()
    entity = lock.PodHomeRemoteLock(coordinator, PPID)

    await entity.async_lock()

    coordinator.api.async_set_remote_lock.assert_called_once_with(PPID, True)
    coordinator.async_request_refresh.assert_called_once()


async def test_async_unlock_sends_off_mode_false_and_refreshes(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    coordinator.async_request_refresh = AsyncMock()
    entity = lock.PodHomeRemoteLock(coordinator, PPID)

    await entity.async_unlock()

    coordinator.api.async_set_remote_lock.assert_called_once_with(PPID, False)
    coordinator.async_request_refresh.assert_called_once()


async def test_async_lock_raises_clean_error_when_charger_unsupported(
    hass: HomeAssistant,
) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    coordinator.api.async_set_remote_lock.side_effect = PodHomeApiError(501, {})
    entity = lock.PodHomeRemoteLock(coordinator, PPID)

    with pytest.raises(HomeAssistantError, match="doesn't support Remote Lock"):
        await entity.async_lock()


async def test_async_lock_reraises_other_api_errors(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger()})
    coordinator.api.async_set_remote_lock.side_effect = PodHomeApiError(500, {})
    entity = lock.PodHomeRemoteLock(coordinator, PPID)

    with pytest.raises(PodHomeApiError):
        await entity.async_lock()


async def test_async_lock_raises_without_charger(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {})
    entity = lock.PodHomeRemoteLock(coordinator, PPID)

    with pytest.raises(HomeAssistantError, match="No charger"):
        await entity.async_lock()
