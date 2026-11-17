"""Tests for diagnostics.py: output is JSON-serializable and redacts personal data."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import json_dumps

from custom_components.pod_home.diagnostics import async_get_config_entry_diagnostics
from tests._fixtures import PPID, make_charger, make_coordinator, make_vehicle


async def test_diagnostics_serializable_and_redacted(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(vehicle=make_vehicle())})
    entry = coordinator.config_entry
    entry.runtime_data = coordinator

    result = await async_get_config_entry_diagnostics(hass, entry)

    json_dumps(result)
    charger = result["chargers"][PPID]
    assert charger["timezone"] == "Europe/London"
    assert charger["vehicle"] == "**REDACTED**"
