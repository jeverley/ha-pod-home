"""Diagnostics support for Pod Home.

Must never reference entry.runtime_data.api or .api._auth: PodHomeAuth holds live Firebase
tokens as plain instance attributes, not covered by any redaction list here.
"""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

# vehicle (PII: name, brand/model, odometer, battery level) and firmware.serial_number are
# redacted so diagnostics stay shareable (e.g. attached to a GitHub issue).
_REDACTED = "**REDACTED**"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: "PodHomeConfigEntry"
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    chargers = {}
    for ppid, charger in coordinator.data.items():
        charger_data = dataclasses.asdict(charger)
        if charger_data.get("vehicle") is not None:
            charger_data["vehicle"] = _REDACTED
        if (charger_data.get("firmware") or {}).get("serial_number") is not None:
            charger_data["firmware"]["serial_number"] = _REDACTED
        chargers[ppid] = charger_data

    return {
        "currency": coordinator.currency,
        "last_update_success": coordinator.last_update_success,
        "last_exception": repr(coordinator.last_exception)
        if coordinator.last_exception
        else None,
        "chargers": chargers,
    }
