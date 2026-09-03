"""Tests for entity.py's tariff-shape gating reconciliation (async_sync_tariff_gated_entities) -
enables/disables Charge Priority based on whether the account's tariff has more than one rate.
This is the one remaining entity-registry-gated axis - Charging-Mode gating moved to `available`
and Remote Lock's hardware-support gating moved to conditional entity creation (see DECISIONS.md
for why), so mode-gating's own reconciliation function no longer exists to test. Mode-gating's
`available`-based replacement is covered instead in test_time.py/test_number.py/test_sensor.py.
"""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.pod_home.const import DOMAIN
from custom_components.pod_home.entity import async_sync_tariff_gated_entities
from tests._fixtures import make_charger, make_coordinator, make_tariff_window

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


def _register_charge_priority(hass: HomeAssistant, ppid: str) -> str:
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "select",
        DOMAIN,
        f"{DOMAIN}_{ppid}_charging_strategy",
        suggested_object_id=f"{ppid}_charging_strategy",
    )
    return entry.entity_id


async def test_disabled_on_single_rate_tariff(hass: HomeAssistant) -> None:
    entity_id = _register_charge_priority(hass, PPID)
    windows = [make_tariff_window(price=0.15), make_tariff_window(price=0.15)]
    coordinator = make_coordinator(hass, {PPID: make_charger(tariff_windows=windows)})

    async_sync_tariff_gated_entities(hass, coordinator)

    registry = er.async_get(hass)
    assert registry.entities[entity_id].disabled_by == er.RegistryEntryDisabler.INTEGRATION


async def test_enabled_on_two_rate_tariff(hass: HomeAssistant) -> None:
    entity_id = _register_charge_priority(hass, PPID)
    registry = er.async_get(hass)
    registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    windows = [make_tariff_window(price=0.10), make_tariff_window(price=0.25)]
    coordinator = make_coordinator(hass, {PPID: make_charger(tariff_windows=windows)})

    async_sync_tariff_gated_entities(hass, coordinator)

    assert registry.entities[entity_id].disabled_by is None


async def test_leaves_a_user_disabled_entity_alone(hass: HomeAssistant) -> None:
    """Only re-enables an entity WE disabled - a user's own manual disable must survive."""
    entity_id = _register_charge_priority(hass, PPID)
    registry = er.async_get(hass)
    registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    windows = [make_tariff_window(price=0.10), make_tariff_window(price=0.25)]
    coordinator = make_coordinator(hass, {PPID: make_charger(tariff_windows=windows)})

    async_sync_tariff_gated_entities(hass, coordinator)

    assert registry.entities[entity_id].disabled_by == er.RegistryEntryDisabler.USER


async def test_unknown_tariff_shape_leaves_existing_state_alone(hass: HomeAssistant) -> None:
    entity_id = _register_charge_priority(hass, PPID)
    coordinator = make_coordinator(hass, {PPID: make_charger(tariff_windows=None)})

    async_sync_tariff_gated_entities(hass, coordinator)

    registry = er.async_get(hass)
    assert registry.entities[entity_id].disabled_by is None


async def test_a_manual_enable_of_our_own_disable_survives_the_next_sync(
    hass: HomeAssistant,
) -> None:
    """Regression test for a real bug: HA's UI "enable" action sets disabled_by=None regardless
    of who disabled it, so a user re-enabling an entity WE'D disabled looks identical to "never
    touched" on the next sync - which used to disable it right back. entity_gate_applied_state
    (coordinator.py) tracks what WE last wrote, so a mismatch (disabled_by=None when we expected
    our own INTEGRATION disable to still be there) is recognised as a user override and left
    alone, not re-applied. Originally caught via Remote Lock (support-gating); reproduced here
    against tariff-gating, the mechanism's one remaining user."""
    entity_id = _register_charge_priority(hass, PPID)
    windows = [make_tariff_window(price=0.15), make_tariff_window(price=0.15)]
    coordinator = make_coordinator(hass, {PPID: make_charger(tariff_windows=windows)})

    # First sync: single-rate tariff, so we disable it ourselves.
    async_sync_tariff_gated_entities(hass, coordinator)
    registry = er.async_get(hass)
    assert registry.entities[entity_id].disabled_by == er.RegistryEntryDisabler.INTEGRATION

    # User manually re-enables it via the UI - HA sets disabled_by back to None, same as if it
    # had simply never been disabled.
    registry.async_update_entity(entity_id, disabled_by=None)

    # Still single-rate - a naive re-sync would disable it again. It must not.
    async_sync_tariff_gated_entities(hass, coordinator)
    assert registry.entities[entity_id].disabled_by is None
