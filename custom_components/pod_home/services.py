"""Domain-wide services ("actions") for pod_home - registered once in async_setup, not per
config entry. start_boost/cancel_boost mirror button.py's Boost full charge/Boost for duration/
Cancel boost buttons exactly (same underlying charge-overrides calls), as a duration-
parameterized pair automations can call directly instead of sequencing a time-entity write plus
a button press.

A plain required `device_id` field (a device selector), not HA's own "target" mechanism -
hassfest rejects a services.yaml `target.device` key entirely, filtered or not ("Services do not
support device filters on target, use a device selector instead"). A plain field sidesteps that
and still gives the UI a device picker; the tradeoff is exactly one device per call instead of a
multi-select target, which both services only ever need anyway.

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
from .helpers import is_momentarily_unplugged

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
        coordinator: PodHomeDataUpdateCoordinator = entry.runtime_data
        for ppid in coordinator.data:
            if (DOMAIN, ppid) in device.identifiers:
                return coordinator, ppid
    raise ServiceValidationError(
        f"{device.name or device_id} isn't a Pod Home charger device - pick the charger, not a "
        "linked vehicle or the Pod Point account device"
    )


def _require_boostable(charger: PodHomeCharger, device_label: str) -> None:
    """Same guard button.py's boost-start buttons apply via `available` - kept in sync
    deliberately rather than shared code, since one raises to fail a service call and the other
    just greys out a button."""
    if is_momentarily_unplugged(charger.charging_state):
        raise ServiceValidationError(f"{device_label}: plug in the cable before boosting")
    if charger.always_on_active:
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
        await coordinator.api.async_create_charge_override(
            ppid, requested_at=requested_at, end_at=end_at
        )
        await coordinator.async_request_refresh()

    async def _async_cancel_boost(call: ServiceCall) -> None:
        coordinator, ppid = _resolve_charger(hass, call.data[ATTR_DEVICE_ID])
        charger = coordinator.data[ppid]
        if charger.boost_end_at is None:
            raise ServiceValidationError(f"{ppid}: no active boost to cancel")
        await coordinator.api.async_delete_charge_override(ppid)
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_START_BOOST, _async_start_boost, schema=START_BOOST_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_BOOST, _async_cancel_boost, schema=CANCEL_BOOST_SCHEMA
    )
