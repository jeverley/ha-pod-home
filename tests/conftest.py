"""tests/ fixtures shared across the whole suite - one unified pytest session, no offline/
integration split. This dev environment has a known Windows-only local-repro gap; CI is the
source of truth for whether the suite actually passes."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """HA's test harness excludes custom_components from component discovery by default -
    this fixture (from pytest-homeassistant-custom-component) turns that back on so the `hass`
    fixture can actually find and set up the pod_home domain."""
    yield
