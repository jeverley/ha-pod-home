"""Button platform for pod_home - boost ("Charge Now") triggers.

WRITE ENDPOINT with a real physical effect on the charger (see CLAUDE.md's "Write endpoints"
section). Built to match the app's own two boost options (Full charge / Set duration) plus a
cancel action, per the user directly - NOT YET TESTED against a real account. Do not press these
outside of the user explicitly doing so live, knowing what it'll do.
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import PodHomeEntity, async_setup_dynamic_chargers
from .helpers import parse_time_of_day

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: PodHomeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_dynamic_chargers(
        entry,
        entry.runtime_data,
        async_add_entities,
        [PodHomeBoostFullChargeButton, PodHomeBoostDurationButton, PodHomeCancelBoostButton],
    )


def _read_boost_duration(hass: HomeAssistant, ppid: str) -> datetime.time:
    """Reads the current value of the Boost duration time entity (time.py) via the entity
    registry + state machine, rather than a direct object reference - the two entities are set
    up independently by separate platforms, so this is the standard cross-entity read pattern
    rather than a fragile string-built entity_id guess."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("time", DOMAIN, f"{DOMAIN}_{ppid}_boost_duration")
    if entity_id is None:
        raise HomeAssistantError("Boost duration entity isn't registered yet")
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        raise HomeAssistantError("Boost duration isn't set yet")
    duration = parse_time_of_day(state.state)
    if duration is None:
        raise HomeAssistantError(f"Couldn't parse Boost duration value {state.state!r}")
    return duration


_FULL_CHARGE_DURATION = datetime.timedelta(hours=12)


class PodHomeBoostFullChargeButton(PodHomeEntity, ButtonEntity):
    """Triggers a boost matching the app's "Full charge" option - a flat 12-hour override, NOT
    an indefinite/"Always On" one. `endAt: null` looked valid per the account's public OpenAPI
    schema (ChargeOverrideRequestDTO's own description), but confirmed live to both be rejected
    by the server (403) and to not match the app's real behavior anyway - triggering "Full
    charge" from the app itself showed an end time exactly 12 hours out, not indefinite. The
    schema documenting a value as accepted doesn't guarantee the server actually honours it -
    see DECISIONS.md."""

    _attr_translation_key = "boost_full_charge"
    _attr_name = "Full charge"
    _attr_icon = "mdi:battery-charging-100"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_boost_full_charge"

    async def async_press(self) -> None:
        if not self.charger:
            raise HomeAssistantError("No charger to boost")
        requested_at = dt_util.utcnow()
        await self.coordinator.api.async_create_charge_override(
            self.ppid, requested_at=requested_at, end_at=requested_at + _FULL_CHARGE_DURATION
        )
        await self.coordinator.async_request_refresh()


class PodHomeBoostDurationButton(PodHomeEntity, ButtonEntity):
    """Triggers a boost for the duration set on Boost duration (time.py), read at press time -
    matching the app's "Set duration" option."""

    _attr_translation_key = "boost_duration_button"
    _attr_name = "Boost for duration"
    _attr_icon = "mdi:timer-play-outline"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_boost_duration_button"

    async def async_press(self) -> None:
        if not self.charger:
            raise HomeAssistantError("No charger to boost")
        duration = _read_boost_duration(self.hass, self.ppid)
        requested_at = dt_util.utcnow()
        end_at = requested_at + datetime.timedelta(
            hours=duration.hour, minutes=duration.minute
        )
        await self.coordinator.api.async_create_charge_override(
            self.ppid, requested_at=requested_at, end_at=end_at
        )
        await self.coordinator.async_request_refresh()


class PodHomeCancelBoostButton(PodHomeEntity, ButtonEntity):
    """Cancels the active boost, if any - `DELETE /chargers/{ppid}/charge-overrides`, also
    confirmed via the account's public OpenAPI schema."""

    _attr_translation_key = "boost_cancel"
    _attr_name = "Cancel boost"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_boost_cancel"

    @property
    def available(self) -> bool:
        # Unlike the entity-availability convention for sensors (unavailable = can't fetch data),
        # a button's availability controls whether it's pressable at all - greying this out when
        # there's nothing to cancel prevents a no-op DELETE, matching HA's own convention for
        # action entities that don't currently apply (e.g. a media player's "next track").
        return super().available and self.charger.boost_end_at is not None

    @property
    def icon(self) -> str:
        # ButtonEntity has no on/off or device_class-driven state, so HA's frontend doesn't
        # apply any automatic colour here regardless of icon choice - only *which* icon shows
        # is under this integration's control. Swap to an alert-styled icon while a boost is
        # actually running, so there's something real to cancel.
        charger = self.charger
        if charger and charger.boost_end_at is not None:
            return "mdi:timer-alert-outline"
        return "mdi:timer-off-outline"

    async def async_press(self) -> None:
        if not self.charger:
            raise HomeAssistantError("No charger to cancel a boost on")
        await self.coordinator.api.async_delete_charge_override(self.ppid)
        await self.coordinator.async_request_refresh()
