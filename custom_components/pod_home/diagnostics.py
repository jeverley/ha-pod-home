"""Diagnostics support for Pod Home.

Must never reference entry.runtime_data.api or .api._auth: PodHomeAuth holds live Firebase
tokens as plain instance attributes, not covered by any redaction list here.
"""
from __future__ import annotations

import dataclasses
import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .coordinator import PodHomeDataUpdateCoordinator, _parse_dt
from .helpers import resolve_timezone
from .podpoint_mobile_api import PodHomeApiError

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
        "month_total_check": await _month_total_check(coordinator),
    }


def _month_starts(today: datetime.date, count: int) -> list[datetime.date]:
    starts = [today.replace(day=1)]
    while len(starts) < count:
        starts.append((starts[-1] - datetime.timedelta(days=1)).replace(day=1))
    return starts


async def _month_total_check(coordinator: PodHomeDataUpdateCoordinator) -> dict[str, Any]:
    """TEMPORARY: per charger, the last three local calendar months' charge-statistics totals
    next to the same months summed from /charges by session start - fetched only when
    diagnostics are downloaded."""
    api = coordinator.api
    result: dict[str, Any] = {}
    for ppid, charger in coordinator.data.items():
        tz = resolve_timezone(charger.timezone)
        today = dt_util.now(tz).date()
        month_starts = _month_starts(today, 3)
        try:
            charges_raw = await api.async_charges(
                month_starts[-1] - datetime.timedelta(days=1), today
            )
        except PodHomeApiError as exc:
            result[ppid] = {"error": f"/charges: {exc}"}
            continue
        sessions = [
            entry
            for entry_ppid, entry, _ in coordinator._charge_entries_by_ppid(charges_raw)
            if entry_ppid == ppid
        ]
        months: dict[str, Any] = {}
        for start in month_starts:
            next_start = (start + datetime.timedelta(days=32)).replace(day=1)
            end = min(next_start - datetime.timedelta(days=1), today)
            try:
                stats = await api.async_charge_statistics(ppid, start, end)
                energy = stats.get("energy") or {}
                statistics = {"energy_kwh": energy.get("totalUsage"), "cost": energy.get("cost")}
            except PodHomeApiError as exc:
                statistics = {"error": str(exc)}
            in_month = []
            for entry in sessions:
                started_at = _parse_dt(entry.get("startedAt"))
                if started_at is None:
                    continue
                if start <= started_at.astimezone(tz).date() < next_start:
                    in_month.append(entry)
            months[start.strftime("%Y-%m")] = {
                "charge_statistics": statistics,
                "summed_from_charges": {
                    "energy_kwh": sum(e.get("energyTotal") or 0 for e in in_month),
                    "cost": sum((e.get("cost") or {}).get("amount") or 0 for e in in_month),
                },
                "sessions": [
                    {
                        "started_at": e.get("startedAt"),
                        "ended_at": e.get("endedAt"),
                        "energy_kwh": e.get("energyTotal"),
                        "cost": (e.get("cost") or {}).get("amount"),
                    }
                    for e in in_month
                ],
            }
        result[ppid] = months
    return result
