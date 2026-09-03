"""Tests for entity.py's support-gating reconciliation (async_sync_support_gated_entities) -
enables/disables Remote Lock based on whether its charger has ever reported a real
remote_lock_off_mode value. Mode-gating and tariff-gating reconciliation have no equivalent
coverage yet - see QUALITY_SCALE.md's test-coverage entry for that open gap; not addressed here.
"""
from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.pod_home.const import DOMAIN
from custom_components.pod_home.entity import async_sync_support_gated_entities
from tests._fixtures import make_charger, make_coordinator

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


def _register_remote_lock(hass: HomeAssistant, ppid: str) -> str:
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "lock", DOMAIN, f"{DOMAIN}_{ppid}_remote_lock", suggested_object_id=f"{ppid}_remote_lock"
    )
    return entry.entity_id


async def test_disabled_when_off_mode_never_seen(hass: HomeAssistant) -> None:
    entity_id = _register_remote_lock(hass, PPID)
    coordinator = make_coordinator(hass, {PPID: make_charger(remote_lock_off_mode=None)})

    async_sync_support_gated_entities(hass, coordinator)

    registry = er.async_get(hass)
    assert registry.entities[entity_id].disabled_by == er.RegistryEntryDisabler.INTEGRATION


async def test_enabled_once_a_real_value_is_seen(hass: HomeAssistant) -> None:
    entity_id = _register_remote_lock(hass, PPID)
    registry = er.async_get(hass)
    registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION)
    coordinator = make_coordinator(hass, {PPID: make_charger(remote_lock_off_mode=False)})

    async_sync_support_gated_entities(hass, coordinator)

    assert registry.entities[entity_id].disabled_by is None


async def test_leaves_a_user_disabled_entity_alone(hass: HomeAssistant) -> None:
    """Only re-enables an entity WE disabled - a user's own manual disable must survive."""
    entity_id = _register_remote_lock(hass, PPID)
    registry = er.async_get(hass)
    registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    coordinator = make_coordinator(hass, {PPID: make_charger(remote_lock_off_mode=True)})

    async_sync_support_gated_entities(hass, coordinator)

    assert registry.entities[entity_id].disabled_by == er.RegistryEntryDisabler.USER


async def test_a_manual_enable_of_our_own_disable_survives_the_next_sync(
    hass: HomeAssistant,
) -> None:
    """Regression test for a real bug: HA's UI "enable" action sets disabled_by=None regardless
    of who disabled it, so a user re-enabling an entity WE'D disabled looks identical to "never
    touched" on the next sync - which used to disable it right back. entity_gate_applied_state
    (coordinator.py) tracks what WE last wrote, so a mismatch (disabled_by=None when we expected
    our own INTEGRATION disable to still be there) is recognised as a user override and left
    alone, not re-applied."""
    entity_id = _register_remote_lock(hass, PPID)
    coordinator = make_coordinator(hass, {PPID: make_charger(remote_lock_off_mode=None)})

    # First sync: unsupported, so we disable it ourselves.
    async_sync_support_gated_entities(hass, coordinator)
    registry = er.async_get(hass)
    assert registry.entities[entity_id].disabled_by == er.RegistryEntryDisabler.INTEGRATION

    # User manually re-enables it via the UI - HA sets disabled_by back to None, same as if it
    # had simply never been disabled.
    registry.async_update_entity(entity_id, disabled_by=None)

    # Still unsupported - a naive re-sync would disable it again. It must not.
    async_sync_support_gated_entities(hass, coordinator)
    assert registry.entities[entity_id].disabled_by is None
