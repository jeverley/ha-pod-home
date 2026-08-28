"""Diagnostics support for Pod Home.

IMPORTANT: this must never reference entry.runtime_data.api or .api._auth in any form.
PodHomeAuth (from the podpoint_mobile_api package) holds live Firebase _id_token/_refresh_token
as plain instance attributes - there is no redaction list here that catches them, because they
should simply never be reachable from this file at all.

entry.data (email/password) is deliberately left out entirely rather than included-then-
redacted - it adds nothing for debugging beyond "which account", which is already visible
elsewhere in HA's own UI, so there's no reason to give it a chance to leak. Only the
coordinator's PodHomeCharger/PodHomeCharge dataclasses (which contain no token fields -
checked, see coordinator.py) are included.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from . import PodHomeConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: "PodHomeConfigEntry"
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    return {
        "currency": coordinator.currency,
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(coordinator.last_exception)
        if coordinator.last_exception
        else None,
        "chargers": {
            ppid: dataclasses.asdict(charger) for ppid, charger in coordinator.data.items()
        },
    }
