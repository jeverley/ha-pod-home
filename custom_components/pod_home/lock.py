"""Lock platform for pod_home - Remote Lock.

WRITE ENDPOINT with a real physical-access effect on the charger (see CLAUDE.md's "Write
endpoints" section): locked means no one can start a charging session at this charger without
unlocking it first, matching the app's own Remote Lock feature exactly (see DECISIONS.md for the
Pod Home app guide quote this is built from). Do not lock/unlock outside of the user explicitly
doing so live, knowing what it'll do.

NOT YET TESTED against a real account - and, per Pod Point's own app guide, Remote Lock is
Solo 3S-only. The account this integration has been live-tested against has a Solo 3, which
can't support it at all (confirmed live: GET /remote-lock/{ppid} returns `{"offMode": null}` for
it) - so unlike every other write entity in this integration, this one may never be live-verified
on this account. Built anyway per the user's explicit request, understanding that constraint.

The entity itself is only ever CREATED for a charger once it's confirmed to support Remote Lock
(`remote_lock_off_mode is not None`) - not created-then-disabled. This is deliberately different
from the mode/tariff-gated entities elsewhere in this integration (see entity.py): hardware
support is a permanent, one-time fact about a specific physical charger, never a live-fluctuating
one like Charging Mode or tariff shape, so there's no ongoing "re-disable" risk to guard against
and no benefit to a disabled-but-visible entity sitting in every unsupported install's registry
forever. See DECISIONS.md.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PodHomeEntity, async_setup_dynamic_chargers
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


class PodHomeRemoteLock(PodHomeEntity, LockEntity):
    """Remote Lock - prevents a new charging session from starting until unlocked. Per the app
    guide, lock/unlock is only possible while the charger is online and unplugged; neither is
    enforced client-side here (no `available` override - unlike Boost, the app guide doesn't
    describe an app-side pre-check for this), so an offline or plugged-in lock/unlock attempt is
    left to the API's own response rather than guessed at."""

    _attr_translation_key = "remote_lock"
    _attr_name = "Remote lock"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_remote_lock"

    @property
    def is_locked(self) -> bool | None:
        charger = self.charger
        return charger.remote_lock_off_mode if charger else None

    async def async_lock(self, **kwargs) -> None:
        await self._async_set_locked(True)

    async def async_unlock(self, **kwargs) -> None:
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
        await self.coordinator.async_request_refresh()
