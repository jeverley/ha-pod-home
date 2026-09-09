"""Button platform for pod_home - boost ("Charge Now") triggers.

WRITE ENDPOINT with a real physical effect on the charger. Matches the app's own two boost
options (Full charge / Set duration) plus a cancel action. Do not press these outside of the
user explicitly doing so live, knowing what it'll do.
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
from .entity import PodHomeEntity, async_handle_write_auth_error, async_setup_dynamic_chargers
from .helpers import parse_time_of_day
from .podpoint_mobile_api import PodHomeAuthError

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
    registry + state machine. Unset and 00:00 are both rejected before reaching the API."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("time", DOMAIN, f"{DOMAIN}_{ppid}_boost_duration")
    if entity_id is None:
        raise HomeAssistantError("Boost duration entity isn't registered yet")
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        raise HomeAssistantError("Enter a Boost duration before pressing this")
    duration = parse_time_of_day(state.state)
    if duration is None:
        raise HomeAssistantError(f"Couldn't parse Boost duration value {state.state!r}")
    if duration == datetime.time(0, 0):
        raise HomeAssistantError("Enter a Boost duration greater than zero before pressing this")
    return duration


_FULL_CHARGE_DURATION = datetime.timedelta(hours=12)


class PodHomeBoostFullChargeButton(PodHomeEntity, ButtonEntity):
    """Triggers a boost matching the app's "Full charge" option. A flat 12-hour override;
    endAt: null is rejected by the server (403)."""

    _attr_translation_key = "boost_full_charge"
    _attr_name = "Full charge"
    _attr_icon = "mdi:battery-charging-100"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_boost_full_charge"

    @property
    def available(self) -> bool:
        return self._boostable

    async def async_press(self) -> None:
        if not self.charger:
            raise HomeAssistantError("No charger to boost")
        requested_at = dt_util.utcnow()
        try:
            await self.coordinator.api.async_create_charge_override(
                self.ppid, requested_at=requested_at, end_at=requested_at + _FULL_CHARGE_DURATION
            )
        except PodHomeAuthError as exc:
            await async_handle_write_auth_error(self.coordinator, exc)
        await self.coordinator.async_request_refresh_after_write()


class PodHomeBoostDurationButton(PodHomeEntity, ButtonEntity):
    """Triggers a boost for the duration set on Boost duration (time.py), read at press time -
    matching the app's "Set duration" option. Boost duration is a one-shot input, not a sticky
    preference - resets to unset only after a successful press."""

    _attr_translation_key = "boost_duration_button"
    _attr_name = "Boost for duration"
    _attr_icon = "mdi:timer-play-outline"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_boost_duration_button"

    @property
    def available(self) -> bool:
        return self._boostable

    async def async_press(self) -> None:
        if not self.charger:
            raise HomeAssistantError("No charger to boost")
        duration = _read_boost_duration(self.hass, self.ppid)
        requested_at = dt_util.utcnow()
        end_at = requested_at + datetime.timedelta(
            hours=duration.hour, minutes=duration.minute
        )
        try:
            await self.coordinator.api.async_create_charge_override(
                self.ppid, requested_at=requested_at, end_at=end_at
            )
        except PodHomeAuthError as exc:
            await async_handle_write_auth_error(self.coordinator, exc)
        await self.coordinator.async_request_refresh_after_write()
        # pod_home owns both entities - see time.py's PodHomeBoostDurationTime.async_reset().
        duration_entity = self.coordinator.boost_duration_entities.get(self.ppid)
        if duration_entity is not None:
            await duration_entity.async_reset()


class PodHomeCancelBoostButton(PodHomeEntity, ButtonEntity):
    """Cancels the active boost, if any - `DELETE /chargers/{ppid}/charge-overrides`, also
    confirmed via the account's public OpenAPI schema."""

    _attr_translation_key = "boost_cancel"
    _attr_name = "Cancel boost"
    _attr_icon = "mdi:timer-off-outline"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_boost_cancel"

    @property
    def available(self) -> bool:
        # Greyed out when there's nothing to cancel, to prevent a no-op DELETE.
        charger = self._available_charger
        return charger is not None and charger.boost_end_at is not None

    async def async_press(self) -> None:
        if not self.charger:
            raise HomeAssistantError("No charger to cancel a boost on")
        try:
            await self.coordinator.api.async_delete_charge_override(self.ppid)
        except PodHomeAuthError as exc:
            await async_handle_write_auth_error(self.coordinator, exc)
        await self.coordinator.async_request_refresh_after_write()
