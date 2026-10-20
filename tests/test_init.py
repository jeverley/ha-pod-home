"""__init__.py's async_setup_entry - the real first-refresh path: auth token load, coordinator
construction, a real first refresh, and every platform's real async_setup_entry, all run
end-to-end against a mocked API client rather than a live account.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pod_home.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from custom_components.pod_home.coordinator import PodHomeDataUpdateCoordinator
from custom_components.pod_home.podpoint_mobile_api import PodHomeAuthError
from tests._fixtures import PPID, make_stub_api

pytestmark = pytest.mark.asyncio

USER_INPUT = {CONF_EMAIL: "driver@example.com", CONF_PASSWORD: "hunter2"}


@pytest.fixture(autouse=True)
def no_real_network():
    """Same reasoning as test_config_flow.py's no_real_setup fixture: a real aiohttp connector
    pulls in aiodns' AsyncResolver, incompatible with this test harness's ProactorEventLoop on
    Windows - not a concern here since the API client itself is fully mocked below."""
    with patch("custom_components.pod_home.async_get_clientsession"):
        yield


async def test_async_setup_entry_real_first_refresh_succeeds(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    api = make_stub_api()
    with patch("custom_components.pod_home.PodHomeApiClient", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    coordinator = entry.runtime_data
    assert isinstance(coordinator, PodHomeDataUpdateCoordinator)
    assert PPID in coordinator.data

    # A real entity from a real platform actually got created off the real first refresh - not
    # just that the coordinator itself has data. Looked up by unique_id via the entity registry
    # rather than a guessed entity_id, since display-name-derived IDs are an implementation
    # detail this test shouldn't depend on.
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_{PPID}_status") is not None


async def test_async_setup_entry_auth_failure_does_not_load(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    api = make_stub_api()
    api.async_list_chargers.side_effect = PodHomeAuthError("token expired")
    with patch("custom_components.pod_home.PodHomeApiClient", return_value=api):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # ConfigEntryAuthFailed during the first refresh must not leave the entry looking loaded -
    # HA routes this into SETUP_ERROR and queues a reauth flow rather than raising past setup.
    assert entry.state is ConfigEntryState.SETUP_ERROR
