"""storage.py: deleting saved data never raises into the caller."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.pod_home.const import AUTH_STORAGE_VERSION, auth_store_key
from custom_components.pod_home.storage import async_clear_auth_tokens, async_remove_store

pytestmark = pytest.mark.asyncio


async def test_remove_store_deletes_saved_data(hass: HomeAssistant) -> None:
    store = Store(hass, 1, "pod_home_test_store")
    await store.async_save({"saved": True})

    await async_remove_store(hass, 1, "pod_home_test_store")

    assert await store.async_load() is None


async def test_remove_store_failure_is_logged_not_raised(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    with patch.object(Store, "async_remove", AsyncMock(side_effect=OSError("denied"))):
        await async_remove_store(hass, 1, "pod_home_test_store")

    assert "Couldn't delete saved data pod_home_test_store" in caplog.text


async def test_clear_auth_tokens_removes_the_entrys_tokens(hass: HomeAssistant) -> None:
    store = Store(hass, AUTH_STORAGE_VERSION, auth_store_key("entry-1"))
    await store.async_save({"refresh_token": "x"})

    await async_clear_auth_tokens(hass, "entry-1")

    assert await store.async_load() is None
