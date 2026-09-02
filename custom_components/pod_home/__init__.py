"""The Pod Home integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

# TEMPORARY: vendored, not an installed dependency - see PLAN.md.
from .podpoint_mobile_api import PodHomeApiClient, PodHomeAuth
from .const import AUTH_STORAGE_VERSION, CONF_EMAIL, CONF_PASSWORD, auth_store_key
from .coordinator import PodHomeDataUpdateCoordinator
from .entity import async_sync_mode_gated_entities, async_sync_tariff_gated_entities

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.UPDATE,
    Platform.NUMBER,
    Platform.TIME,
    Platform.SELECT,
    Platform.CALENDAR,
    Platform.BUTTON,
]

# Firebase refresh token, persisted across restarts so reload can silently refresh instead of
# doing a full sign-in each time (AUTH_STORAGE_VERSION/auth_store_key in const.py, shared with
# config_flow.py which must clear this Store on a successful reauth).
AUTH_SAVE_DELAY = 5  # seconds, coalesced

type PodHomeConfigEntry = ConfigEntry[PodHomeDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PodHomeConfigEntry) -> bool:
    """Set up Pod Home from a config entry."""
    session = async_get_clientsession(hass)

    auth_store: Store = Store(hass, AUTH_STORAGE_VERSION, auth_store_key(entry.entry_id))
    try:
        auth_data = await auth_store.async_load()
    except Exception:  # noqa: BLE001 - a corrupt/unreadable store file must not block setup
        _LOGGER.warning("Couldn't load saved auth tokens, signing in fresh", exc_info=True)
        auth_data = None

    def _save_auth_tokens() -> None:
        # Only called from within auth.async_get_id_token(), never during construction, so
        # `auth` (defined below) is always bound by the time this runs.
        auth_store.async_delay_save(auth.export_tokens, AUTH_SAVE_DELAY)

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

    # Initial reconciliation, then re-run on every coordinator update since mode/tariff can
    # change at any time. Two independent gating axes (see entity.py).
    async_sync_mode_gated_entities(hass, coordinator)
    async_sync_tariff_gated_entities(hass, coordinator)
    entry.async_on_unload(
        coordinator.async_add_listener(lambda: async_sync_mode_gated_entities(hass, coordinator))
    )
    entry.async_on_unload(
        coordinator.async_add_listener(lambda: async_sync_tariff_gated_entities(hass, coordinator))
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PodHomeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
