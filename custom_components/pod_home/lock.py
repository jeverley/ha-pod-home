"""Lock platform for pod_home - Remote Lock.

WRITE ENDPOINT with a real access-control effect on the charger, not the cable: locked means no
one can start a charging session on the charger until it's unlocked again - it does not
physically lock a plugged-in cable in place. Do not lock/unlock outside of the user explicitly
doing so live, knowing what it'll do.

NOT YET TESTED against a real account - Remote Lock is Solo 3S-only (per Pod Point's own app
guide); GET /remote-lock/{ppid} returns `{"offMode": null}` on hardware that doesn't support it.

The entity is only ever CREATED for a charger once it's confirmed to support Remote Lock
(`remote_lock_off_mode is not None`) - created only once hardware support is confirmed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import (
    PodHomeEntity,
    PodHomeOptimisticWriteMixin,
    async_handle_write_auth_error,
    async_setup_dynamic_chargers,
)
from .podpoint_mobile_api import PodHomeApiError, PodHomeAuthError

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
        [PodHomeRemoteLock],
        predicate=lambda charger: charger.remote_lock_off_mode is not None,
    )


class PodHomeRemoteLock(PodHomeOptimisticWriteMixin[bool], PodHomeEntity, LockEntity):
    """Remote Lock - prevents anyone from using the charger to start a new session until
    unlocked; doesn't lock a plugged-in cable in place. Per the app guide, a charger can only be
    locked while no car is currently plugged in; neither that nor being online is enforced
    client-side here (no `available` override). PodHomeOptimisticWriteMixin masks is_locked's
    read-your-own-write race - see its docstring."""

    _attr_translation_key = "remote_lock"
    _attr_name = "Remote lock"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_remote_lock"

    @property
    def is_locked(self) -> bool | None:
        optimistic = self._read_optimistic_value()
        if optimistic is not None:
            return optimistic
        charger = self.charger
        return charger.remote_lock_off_mode if charger else None

    async def async_lock(self, **kwargs: Any) -> None:
        await self._async_set_locked(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._async_set_locked(False)

    async def _async_set_locked(self, off_mode: bool) -> None:
        if not self.charger:
            raise HomeAssistantError("No charger to lock/unlock")
        try:
            await self.coordinator.api.async_set_remote_lock(self.ppid, off_mode)
        except PodHomeAuthError as exc:
            await async_handle_write_auth_error(self.coordinator, exc)
        except PodHomeApiError as exc:
            if exc.status == 501:
                raise HomeAssistantError(
                    "This charger doesn't support Remote Lock (Solo 3S only)"
                ) from exc
            raise
        self._set_optimistic_value(off_mode)
        await self.coordinator.async_request_refresh_after_write()
