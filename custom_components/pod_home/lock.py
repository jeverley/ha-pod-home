"""Lock platform for pod_home - Remote Lock.

WRITE ENDPOINT with a real physical-access effect on the charger: locked means no one can start
a charging session without unlocking it first, matching the app's own Remote Lock feature. Do
not lock/unlock outside of the user explicitly doing so live, knowing what it'll do.

NOT YET TESTED against a real account - Remote Lock is Solo 3S-only (per Pod Point's own app
guide), and the account this integration is developed against has a Solo 3, which can't support
it at all (confirmed live: GET /remote-lock/{ppid} returns `{"offMode": null}`). Built anyway per
the user's explicit request, understanding that constraint.

The entity is only ever CREATED for a charger once it's confirmed to support Remote Lock
(`remote_lock_off_mode is not None`) - not created-then-disabled. Hardware support is a
permanent, one-time fact about a physical charger, unlike the mode/tariff-gated entities
elsewhere, so there's no benefit to a disabled-but-visible entity on every unsupported install.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PodHomeEntity, PodHomeOptimisticWriteMixin, async_setup_dynamic_chargers
from .podpoint_mobile_api import PodHomeApiError

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


class PodHomeRemoteLock(PodHomeOptimisticWriteMixin, PodHomeEntity, LockEntity):
    """Remote Lock - prevents a new charging session from starting until unlocked. Per the app
    guide, lock/unlock is only possible while the charger is online and unplugged; neither is
    enforced client-side here (no `available` override - unlike Boost, the app guide doesn't
    describe an app-side pre-check for this), so an offline or plugged-in lock/unlock attempt is
    left to the API's own response rather than guessed at. PodHomeOptimisticWriteMixin masks
    is_locked's read-your-own-write race - see its docstring."""

    _attr_translation_key = "remote_lock"
    _attr_name = "Remote lock"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_remote_lock"

    @property
    def is_locked(self) -> bool | None:
        optimistic = self._read_optimistic_value()
        if isinstance(optimistic, bool):
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
        except PodHomeApiError as exc:
            if exc.status == 501:
                raise HomeAssistantError(
                    "This charger doesn't support Remote Lock (Solo 3S only)"
                ) from exc
            raise
        self._set_optimistic_value(off_mode)
        await self.coordinator.async_request_refresh()
