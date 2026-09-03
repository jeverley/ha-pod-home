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
