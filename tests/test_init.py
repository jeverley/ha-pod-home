"""__init__.py's async_setup_entry - the real first-refresh path: auth token load, coordinator
construction, a real first refresh, and every platform's real async_setup_entry, all run
end-to-end against a mocked API client rather than a live account.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

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


async def test_pre_0_3_7_charge_sensor_unique_ids_migrated(hass: HomeAssistant) -> None:
    """The renamed Charge sensors keep their entity IDs (and so their history)."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, f"{DOMAIN}_{PPID}_last_charge_energy", config_entry=entry,
        suggested_object_id="driveway_last_charge_energy",
    )

    with patch("custom_components.pod_home.PodHomeApiClient", return_value=make_stub_api()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{PPID}_charge_energy"
    ) == old.entity_id
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{PPID}_last_charge_energy"
    ) is None


async def test_charge_unique_id_migration_skips_a_taken_id(hass: HomeAssistant) -> None:
    """An entry already holding the new unique ID doesn't block setup."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for unique_id in (f"{DOMAIN}_{PPID}_last_charge_energy", f"{DOMAIN}_{PPID}_charge_energy"):
        registry.async_get_or_create("sensor", DOMAIN, unique_id, config_entry=entry)

    with patch("custom_components.pod_home.PodHomeApiClient", return_value=make_stub_api()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


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


async def test_auth_token_saver_coalesces_and_saves_after_the_delay(hass: HomeAssistant) -> None:
    import datetime

    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.pod_home import AUTH_SAVE_DELAY, _AuthTokenSaver

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    store = Store(hass, 1, "test_auth_saver")
    saver = _AuthTokenSaver(hass, store, entry, USER_INPUT[CONF_PASSWORD])

    saver.save({"id_token": "first"})
    saver.save({"id_token": "second"})
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(seconds=AUTH_SAVE_DELAY + 1))
    await hass.async_block_till_done()

    assert await store.async_load() == {"id_token": "second"}


async def test_auth_token_saver_skips_a_save_after_the_password_changed(hass: HomeAssistant) -> None:
    import datetime

    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.pod_home import AUTH_SAVE_DELAY, _AuthTokenSaver

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    store = Store(hass, 1, "test_auth_saver_skipped")
    saver = _AuthTokenSaver(hass, store, entry, "the-old-password")

    saver.save({"id_token": "stale"})
    async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(seconds=AUTH_SAVE_DELAY + 1))
    await hass.async_block_till_done()

    assert await store.async_load() is None


async def test_setup_signs_in_fresh_when_the_saved_tokens_cannot_be_read(hass: HomeAssistant) -> None:
    from homeassistant.helpers.storage import Store

    from custom_components.pod_home.const import auth_store_key

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    async def _load(self):
        if self.key == auth_store_key(entry.entry_id):
            raise OSError("corrupt")
        return None

    with (
        patch("custom_components.pod_home.PodHomeApiClient", return_value=make_stub_api()),
        patch.object(Store, "async_load", autospec=True, side_effect=_load),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


async def test_removing_the_entry_deletes_its_saved_data(hass: HomeAssistant) -> None:
    import datetime

    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from custom_components.pod_home.const import (
        AUTH_STORAGE_VERSION,
        STICKY_STATE_STORAGE_VERSION,
        auth_store_key,
        status_store_key,
        total_energy_store_key,
    )

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)
    stores = [
        Store(hass, AUTH_STORAGE_VERSION, auth_store_key(entry.entry_id)),
        Store(hass, STICKY_STATE_STORAGE_VERSION, status_store_key(entry.entry_id)),
        Store(hass, STICKY_STATE_STORAGE_VERSION, total_energy_store_key(entry.entry_id)),
    ]
    for store in stores:
        await store.async_save({"saved": True})

    with patch("custom_components.pod_home.PodHomeApiClient", return_value=make_stub_api()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        # A save the coordinator had already scheduled must not bring the data back.
        async_fire_time_changed(hass, dt_util.utcnow() + datetime.timedelta(seconds=60))
        await hass.async_block_till_done()

    for store in stores:
        assert await store.async_load() is None


async def test_unloading_the_entry_saves_pending_state(hass: HomeAssistant) -> None:
    from homeassistant.helpers.storage import Store

    from custom_components.pod_home.const import (
        STICKY_STATE_STORAGE_VERSION,
        total_energy_store_key,
    )

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    entry.add_to_hass(hass)

    with patch("custom_components.pod_home.PodHomeApiClient", return_value=make_stub_api()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data._total_energy_kwh_by_ppid = {PPID: 5.0}
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    store = Store(hass, STICKY_STATE_STORAGE_VERSION, total_energy_store_key(entry.entry_id))
    saved = await store.async_load()
    assert saved is not None
    assert saved["total_energy_kwh"] == {PPID: 5.0}


async def test_failed_deletion_of_one_store_does_not_stop_the_rest(hass: HomeAssistant) -> None:
    from homeassistant.helpers.storage import Store

    from custom_components.pod_home import async_remove_entry

    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    # The auth store is deleted first, then Status and Total energy.
    failing_first = AsyncMock(side_effect=[OSError("denied"), None, None])

    with patch.object(Store, "async_remove", failing_first):
        await async_remove_entry(hass, entry)

    assert failing_first.await_count == 3
