"""The Pod Home integration."""
from __future__ import annotations

import datetime
from collections.abc import Callable
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

# TEMPORARY: vendored, not an installed dependency - undo once podpoint-mobile-api has a real
# installable release (revert this import and the two others below back to a plain
# `import podpoint_mobile_api`, delete the vendored copy, populate manifest.json's requirements).
from .podpoint_mobile_api import PodHomeApiClient, PodHomeAuth
from .const import AUTH_STORAGE_VERSION, CONF_EMAIL, CONF_PASSWORD, DOMAIN, auth_store_key
from .coordinator import PodHomeDataUpdateCoordinator
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

# pod_home is config-entry-only (async_setup here just registers services.py's domain-wide
# actions) - no YAML configuration is supported; hassfest requires this stated explicitly.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.UPDATE,
    Platform.NUMBER,
    Platform.TIME,
    Platform.SELECT,
    Platform.CALENDAR,
    Platform.BUTTON,
    Platform.LOCK,
]

# Firebase refresh token, persisted across restarts (AUTH_STORAGE_VERSION/auth_store_key in
# const.py, shared with config_flow.py which must clear this Store on a successful reauth).
AUTH_SAVE_DELAY = 5  # seconds, coalesced

type PodHomeConfigEntry = ConfigEntry[PodHomeDataUpdateCoordinator]


class _AuthTokenSaver:
    """Persists Firebase auth tokens on change, coalesced via async_call_later and guarded
    against a delayed write landing after reauth has since changed the password."""

    def __init__(
        self,
        hass: HomeAssistant,
        auth_store: Store[dict[str, Any]],
        entry: PodHomeConfigEntry,
        signed_in_password: str,
    ) -> None:
        self._hass = hass
        self._auth_store = auth_store
        self._entry = entry
        self._signed_in_password = signed_in_password
        self._cancel_delayed_save: Callable[[], None] | None = None
        self._pending_tokens: dict[str, Any] | None = None

    def save(self, tokens: dict[str, Any]) -> None:
        """on_token_change callback passed to PodHomeAuth."""
        if self._cancel_delayed_save is not None:
            self._cancel_delayed_save()
        self._pending_tokens = tokens
        self._cancel_delayed_save = async_call_later(
            self._hass, AUTH_SAVE_DELAY, self._save_if_still_current
        )

    async def _save_if_still_current(self, _now: datetime.datetime) -> None:
        self._cancel_delayed_save = None
        if self._entry.data.get(CONF_PASSWORD) != self._signed_in_password:
            return
        assert self._pending_tokens is not None
        await self._auth_store.async_save(self._pending_tokens)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-wide services (start_boost/cancel_boost, services.py) once, hass-level -
    not gated on any config entry existing yet or being loaded. Called at most once regardless
    of how many config entries exist."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PodHomeConfigEntry) -> bool:
    """Set up Pod Home from a config entry."""
    session = async_get_clientsession(hass)

    auth_store: Store[dict[str, Any]] = Store(
        hass, AUTH_STORAGE_VERSION, auth_store_key(entry.entry_id)
    )
    try:
        auth_data = await auth_store.async_load()
    except Exception:  # noqa: BLE001 - a corrupt/unreadable store file must not block setup
        _LOGGER.warning("Couldn't load saved auth tokens, signing in fresh", exc_info=True)
        auth_data = None

    # Captured now; compared against entry.data live at write time to detect a reauth that
    # changed the password mid-flight.
    signed_in_password = entry.data[CONF_PASSWORD]

    token_saver = _AuthTokenSaver(hass, auth_store, entry, signed_in_password)
    auth = PodHomeAuth(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        on_token_change=token_saver.save,
    )
    auth.import_tokens(auth_data)
    api = PodHomeApiClient(session, auth)

    coordinator = PodHomeDataUpdateCoordinator(
        hass, entry, api, email=entry.data[CONF_EMAIL], password=entry.data[CONF_PASSWORD]
    )
    await coordinator.async_load_sticky_state()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: PodHomeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
