"""Helpers for the data the integration saves."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import AUTH_STORAGE_VERSION, auth_store_key

_LOGGER = logging.getLogger(__name__)


async def async_remove_store(hass: HomeAssistant, version: int, key: str) -> None:
    """Delete a saved Store; a failure is logged and doesn't block the caller."""
    try:
        await Store(hass, version, key).async_remove()
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Couldn't delete saved data %s", key, exc_info=True)


async def async_clear_auth_tokens(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the persisted refresh token so a session from the old credentials isn't restored."""
    await async_remove_store(hass, AUTH_STORAGE_VERSION, auth_store_key(entry_id))
