"""tests/ fixtures shared across the whole suite - one unified pytest session, no offline/
integration split. See DECISIONS.md for why the earlier split was abandoned and for this
session's known Windows-only local-repro gap (CI is the source of truth, per CLAUDE.md)."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """HA's test harness excludes custom_components from component discovery by default -
    this fixture (from pytest-homeassistant-custom-component) turns that back on so the `hass`
    fixture can actually find and set up the pod_home domain."""
    yield
