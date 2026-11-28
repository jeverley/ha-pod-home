"""Device lifecycle: devices of chargers that leave the account are removed, devices can only be
removed by hand once they're gone from the account, and a charger that returns gets entities
again."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pod_home import async_remove_config_entry_device
from custom_components.pod_home.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from custom_components.pod_home.entity import (
    async_setup_dynamic_chargers,
    async_setup_dynamic_vehicles,
)
from tests._fixtures import PPID, charger_raw, make_charger, make_coordinator, make_stub_api, make_vehicle

pytestmark = pytest.mark.asyncio

USER_INPUT = {CONF_EMAIL: "driver@example.com", CONF_PASSWORD: "hunter2"}
OTHER_PPID = "PSL-000002"


@pytest.fixture(autouse=True)
def no_real_network():
    with patch("custom_components.pod_home.async_get_clientsession"):
        yield


async def _setup_two_chargers(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)
    api = make_stub_api()
    api.async_list_chargers.return_value = [charger_raw(), charger_raw(ppid=OTHER_PPID)]
    with patch("custom_components.pod_home.PodHomeApiClient", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, api


def _device(hass: HomeAssistant, entry: MockConfigEntry, identifier: str):
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id
    )


async def test_device_of_a_charger_that_left_the_account_is_removed(hass: HomeAssistant) -> None:
    entry, api = await _setup_two_chargers(hass)
    assert _device(hass, entry, PPID) is not None
    assert _device(hass, entry, OTHER_PPID) is not None

    api.async_list_chargers.return_value = [charger_raw()]
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _device(hass, entry, OTHER_PPID) is None
    assert _device(hass, entry, PPID) is not None


async def test_devices_kept_when_the_charger_list_comes_back_empty(hass: HomeAssistant) -> None:
    entry, api = await _setup_two_chargers(hass)

    api.async_list_chargers.return_value = []
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _device(hass, entry, PPID) is not None
    assert _device(hass, entry, OTHER_PPID) is not None


async def test_vehicle_and_account_devices_are_not_removed_automatically(
    hass: HomeAssistant,
) -> None:
    entry, api = await _setup_two_chargers(hass)
    registry = dr.async_get(hass)
    registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "vehicle-1")}
    )
    registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )

    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert _device(hass, entry, "vehicle-1") is not None
    assert _device(hass, entry, entry.entry_id) is not None


async def test_manual_removal_only_allowed_for_devices_gone_from_the_account(
    hass: HomeAssistant,
) -> None:
    vehicle = make_vehicle()
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=vehicle)})
    entry = coordinator.config_entry
    entry.runtime_data = coordinator
    registry = dr.async_get(hass)

    def device_for(identifier: str) -> dr.DeviceEntry:
        return registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, identifier)}
        )

    assert not await async_remove_config_entry_device(hass, entry, device_for(PPID))
    assert not await async_remove_config_entry_device(hass, entry, device_for(vehicle.id))
    assert not await async_remove_config_entry_device(hass, entry, device_for(entry.entry_id))
    assert await async_remove_config_entry_device(hass, entry, device_for(OTHER_PPID))
    assert await async_remove_config_entry_device(hass, entry, device_for("old-vehicle"))


class _Entity:
    def __init__(self, coordinator, key: str) -> None:
        self.key = key


async def test_charger_that_returns_gets_entities_again(hass: HomeAssistant) -> None:
    charger = make_charger()
    coordinator = make_coordinator(hass, {PPID: charger})
    add_entities = MagicMock()

    async_setup_dynamic_chargers(coordinator.config_entry, coordinator, add_entities, [_Entity])
    assert add_entities.call_count == 1

    coordinator.data = {}
    coordinator.async_update_listeners()
    assert add_entities.call_count == 1

    coordinator.data = {PPID: charger}
    coordinator.async_update_listeners()
    assert add_entities.call_count == 2


async def test_vehicle_that_returns_gets_entities_again(hass: HomeAssistant) -> None:
    charger = make_charger(vehicle=make_vehicle())
    coordinator = make_coordinator(hass, {PPID: charger})
    add_entities = MagicMock()

    async_setup_dynamic_vehicles(coordinator.config_entry, coordinator, add_entities, [_Entity])
    assert add_entities.call_count == 1

    coordinator.data = {PPID: make_charger()}
    coordinator.async_update_listeners()
    assert add_entities.call_count == 1

    coordinator.data = {PPID: charger}
    coordinator.async_update_listeners()
    assert add_entities.call_count == 2
