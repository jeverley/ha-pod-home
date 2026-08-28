"""The Pod Home integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from podpoint_mobile_api import PodHomeApiClient, PodHomeAuth
from .const import CONF_EMAIL, CONF_PASSWORD
from .coordinator import PodHomeDataUpdateCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

# Typed config entry alias - current HA best practice (the "runtime-data" quality scale rule)
# for storing per-entry runtime state, in place of the older hass.data[DOMAIN][entry_id]
# pattern. Used throughout (coordinator.py, sensor.py, binary_sensor.py, diagnostics.py).
type PodHomeConfigEntry = ConfigEntry[PodHomeDataUpdateCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PodHomeConfigEntry) -> bool:
    """Set up Pod Home from a config entry."""
    session = async_get_clientsession(hass)
    auth = PodHomeAuth(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    api = PodHomeApiClient(session, auth)

    coordinator = PodHomeDataUpdateCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PodHomeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
