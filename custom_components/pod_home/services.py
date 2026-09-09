"""Domain-wide services ("actions") for pod_home - registered once in async_setup, not per
config entry. start_boost/cancel_boost mirror button.py's Boost full charge/Boost for duration/
Cancel boost buttons exactly (same underlying charge-overrides calls).

device_id is a plain required field, not HA's own "target" mechanism - hassfest rejects a
services.yaml `target.device` key entirely.

WRITE ENDPOINTS with a real physical effect on the charger. Confirmed working live.
"""
from __future__ import annotations

import datetime

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import PodHomeCharger, PodHomeDataUpdateCoordinator
from .entity import async_handle_write_auth_error
from .helpers import boostable, is_momentarily_unplugged
from .podpoint_mobile_api import PodHomeAuthError

SERVICE_START_BOOST = "start_boost"
SERVICE_CANCEL_BOOST = "cancel_boost"

ATTR_DEVICE_ID = "device_id"
ATTR_DURATION = "duration"

# Matches PodHomeBoostFullChargeButton's own flat duration (button.py) - see its docstring for
# why 12h, not indefinite.
_FULL_CHARGE_DURATION = datetime.timedelta(hours=12)

START_BOOST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_DURATION): cv.time_period,
    }
)
CANCEL_BOOST_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): cv.string})


def _resolve_charger(hass: HomeAssistant, device_id: str) -> tuple[PodHomeDataUpdateCoordinator, str]:
    """One targeted device_id -> (coordinator, ppid). Raises ServiceValidationError for anything
    that isn't a currently-known Pod Home charger device - a linked vehicle or the Pod Point
    account device (both also (DOMAIN, ...)-identified, see entity.py), an unknown/removed
    device, or a device from an unrelated integration."""
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Unknown device: {device_id}")
    for config_entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(config_entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        # runtime_data is unset (raises AttributeError, not None) until async_setup_entry
        # finishes, even though the device registry can already list this entry against a device.
        coordinator: PodHomeDataUpdateCoordinator | None = getattr(entry, "runtime_data", None)
        if coordinator is None:
            continue
        for ppid in coordinator.data:
            if (DOMAIN, ppid) in device.identifiers:
                return coordinator, ppid
    raise ServiceValidationError(
        f"{device.name or device_id} isn't a Pod Home charger device - pick the charger, not a "
        "linked vehicle or the Pod Point account device"
    )


def _require_boostable(charger: PodHomeCharger, device_label: str) -> None:
    """Same underlying check as entity.py's _boostable (helpers.boostable()), split into two
    messages so the user knows which precondition failed."""
    if is_momentarily_unplugged(charger.charging_state):
        raise ServiceValidationError(f"{device_label}: plug in the cable before boosting")
    if not boostable(charger.charging_state, charger.always_on_active):
        raise ServiceValidationError(f"{device_label}: can't boost while Always On is active")


def async_setup_services(hass: HomeAssistant) -> None:
    """Registered once, hass-level, in __init__.py's async_setup - not gated on any one config
    entry's state. Each handler validates its own target/preconditions via
    ServiceValidationError instead."""

    async def _async_start_boost(call: ServiceCall) -> None:
        coordinator, ppid = _resolve_charger(hass, call.data[ATTR_DEVICE_ID])
        charger = coordinator.data[ppid]
        _require_boostable(charger, ppid)
        duration: datetime.timedelta | None = call.data.get(ATTR_DURATION)
        requested_at = dt_util.utcnow()
        end_at = requested_at + (duration if duration is not None else _FULL_CHARGE_DURATION)
        try:
            await coordinator.api.async_create_charge_override(
                ppid, requested_at=requested_at, end_at=end_at
            )
        except PodHomeAuthError as exc:
            await async_handle_write_auth_error(coordinator, exc)
        await coordinator.async_request_refresh_after_write()

    async def _async_cancel_boost(call: ServiceCall) -> None:
        coordinator, ppid = _resolve_charger(hass, call.data[ATTR_DEVICE_ID])
        charger = coordinator.data[ppid]
        if charger.boost_end_at is None:
            raise ServiceValidationError(f"{ppid}: no active boost to cancel")
        try:
            await coordinator.api.async_delete_charge_override(ppid)
        except PodHomeAuthError as exc:
            await async_handle_write_auth_error(coordinator, exc)
        await coordinator.async_request_refresh_after_write()

    hass.services.async_register(
        DOMAIN, SERVICE_START_BOOST, _async_start_boost, schema=START_BOOST_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_BOOST, _async_cancel_boost, schema=CANCEL_BOOST_SCHEMA
    )
