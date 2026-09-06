"""The Pod Home integration."""
from __future__ import annotations

import logging
from typing import Callable

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
# actions) - no YAML configuration is supported, so hassfest requires this be stated explicitly
# rather than left implicit.
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

# Firebase refresh token, persisted across restarts so reload can silently refresh instead of
# doing a full sign-in each time (AUTH_STORAGE_VERSION/auth_store_key in const.py, shared with
# config_flow.py which must clear this Store on a successful reauth).
AUTH_SAVE_DELAY = 5  # seconds, coalesced

type PodHomeConfigEntry = ConfigEntry[PodHomeDataUpdateCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-wide services (start_boost/cancel_boost, services.py) once, hass-level -
    not gated on any config entry existing yet or being loaded. Called at most once regardless
    of how many config entries exist."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PodHomeConfigEntry) -> bool:
    """Set up Pod Home from a config entry."""
    session = async_get_clientsession(hass)

    auth_store: Store = Store(hass, AUTH_STORAGE_VERSION, auth_store_key(entry.entry_id))
    try:
        auth_data = await auth_store.async_load()
    except Exception:  # noqa: BLE001 - a corrupt/unreadable store file must not block setup
        _LOGGER.warning("Couldn't load saved auth tokens, signing in fresh", exc_info=True)
        auth_data = None

    # Captured now, compared against entry.data live at write time (below) - entry.data is
    # mutated in place by a reauth, not replaced, so this detects "this session's password is
    # now stale" even for a write that was already delayed/in-flight when reauth completed.
    signed_in_password = entry.data[CONF_PASSWORD]

    _cancel_delayed_save: Callable[[], None] | None = None

    def _save_auth_tokens() -> None:
        # Only called from within auth.async_get_id_token(), never during construction, so
        # `auth` (defined below) is always bound by the time this runs. Guards against a
        # delayed write landing after a reauth has since changed the password. Scheduled with
        # async_call_later, not Store.async_delay_save, so a stale write can be skipped outright
        # at fire time instead of writing a literal `data: null` (Store.async_delay_save always
        # writes its data_func's return value as-is). Re-checked inside the callback, not just
        # before scheduling, since reauth's Store.async_remove() runs on a separate Store
        # instance that can't cancel this one's pending write.
        nonlocal _cancel_delayed_save
        if _cancel_delayed_save is not None:
            _cancel_delayed_save()

        async def _save_if_still_current(_now) -> None:
            nonlocal _cancel_delayed_save
            _cancel_delayed_save = None
            if entry.data.get(CONF_PASSWORD) != signed_in_password:
                return
            await auth_store.async_save(auth.export_tokens())

        _cancel_delayed_save = async_call_later(hass, AUTH_SAVE_DELAY, _save_if_still_current)

    auth = PodHomeAuth(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        on_token_change=_save_auth_tokens,
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
