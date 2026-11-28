"""Domain-wide services ("actions") for pod_home - registered once in async_setup, not per
config entry. start_boost/cancel_boost make the same charge-overrides calls as button.py's Boost
full charge/Boost for duration/Cancel boost buttons; start_boost also accepts the cable unplugged
and a future `start`.

device_id is a plain required field, not HA's own "target" mechanism - hassfest rejects a
services.yaml `target.device` key entirely.

WRITE ENDPOINTS with a real physical effect on the charger.
"""
from __future__ import annotations

import datetime

import probatio as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import PodHomeCharger, PodHomeDataUpdateCoordinator
from .entity import async_handle_write_error
from .errors import translated_validation_error
from podpoint_mobile_api import PodPointMobileError

SERVICE_START_BOOST = "start_boost"
SERVICE_CANCEL_BOOST = "cancel_boost"

ATTR_DEVICE_ID = "device_id"
ATTR_DURATION = "duration"
ATTR_START = "start"

# Matches PodHomeBoostFullChargeButton's own flat duration (button.py) - see its docstring for
# why 12h, not indefinite.
_FULL_CHARGE_DURATION = datetime.timedelta(hours=12)

START_BOOST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Optional(ATTR_DURATION): cv.time_period,
        vol.Optional(ATTR_START): cv.datetime,
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
        raise translated_validation_error("unknown_device", device_id=device_id)
    # DeviceEntry.config_entry_id replaces the deprecated config_entries set.
    entry_ids = (
        [device.config_entry_id] if hasattr(device, "config_entry_id") else device.config_entries
    )
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            continue
        # runtime_data is unset (raises AttributeError, not None) until async_setup_entry
        # finishes, even though the device registry can already list this entry against a device.
        coordinator: PodHomeDataUpdateCoordinator | None = getattr(entry, "runtime_data", None)
        if coordinator is not None:
            for ppid in coordinator.data:
                if (DOMAIN, ppid) in device.identifiers:
                    return coordinator, ppid
    raise translated_validation_error("not_a_charger_device", device=device.name or device_id)


def _require_boostable(charger: PodHomeCharger, device_label: str) -> None:
    """A boost can't be started during Always On, or while that's unknown. The cable may be
    unplugged."""
    if charger.always_on_active is None:
        raise translated_validation_error("always_on_unknown", device=device_label)
    if charger.always_on_active:
        raise translated_validation_error("boost_blocked_by_always_on", device=device_label)


def _boost_window(
    start: datetime.datetime | None, duration: datetime.timedelta, device_label: str
) -> tuple[datetime.datetime, datetime.datetime]:
    """(requestedAt, endAt) for a boost lasting `duration` from `start` (local time if naive). A
    start already passed is sent as now, keeping the same end."""
    if duration <= datetime.timedelta(0):
        raise translated_validation_error("boost_duration_not_positive", device=device_label)
    now = dt_util.utcnow()
    start_utc = dt_util.as_utc(start) if start is not None else now
    end_at = start_utc + duration
    if end_at <= now:
        raise translated_validation_error("boost_already_ended", device=device_label)
    return max(start_utc, now), end_at


def async_setup_services(hass: HomeAssistant) -> None:
    """Registered once, hass-level, in __init__.py's async_setup - not gated on any one config
    entry's state. Each handler validates its own target/preconditions via
    ServiceValidationError instead."""

    async def _async_start_boost(call: ServiceCall) -> None:
        coordinator, ppid = _resolve_charger(hass, call.data[ATTR_DEVICE_ID])
        charger = coordinator.data[ppid]
        _require_boostable(charger, ppid)
        duration: datetime.timedelta | None = call.data.get(ATTR_DURATION)
        requested_at, end_at = _boost_window(
            call.data.get(ATTR_START),
            duration if duration is not None else _FULL_CHARGE_DURATION,
            ppid,
        )
        try:
            await coordinator.api.async_create_charge_override(
                ppid, requested_at=requested_at, end_at=end_at
            )
        except PodPointMobileError as exc:
            await async_handle_write_error(coordinator, exc)
        await coordinator.async_request_refresh()

    async def _async_cancel_boost(call: ServiceCall) -> None:
        coordinator, ppid = _resolve_charger(hass, call.data[ATTR_DEVICE_ID])
        charger = coordinator.data[ppid]
        if charger.boost_end_at is None:
            raise translated_validation_error("no_active_boost", device=ppid)
        try:
            await coordinator.api.async_delete_charge_override(ppid)
        except PodPointMobileError as exc:
            await async_handle_write_error(coordinator, exc)
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_START_BOOST, _async_start_boost, schema=START_BOOST_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CANCEL_BOOST, _async_cancel_boost, schema=CANCEL_BOOST_SCHEMA
    )
