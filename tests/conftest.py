"""tests/ fixtures shared across the whole suite.

Everything under tests/ runs in one unified pytest session now - pytest-homeassistant-custom-
component auto-loads on its own once installed (no `-p no:homeassistant` suppression, no
subtree split). The earlier tests/test_*.py vs. tests/integration/ split existed only to route
around this plugin disrupting plain synchronous tests (an event_loop fixture swap + real-socket
blocking) when active for the whole session - that turned out to be a Windows-specific failure
mode (see DECISIONS.md for the confirmed local repro): Windows' asyncio.ProactorEventLoop needs a
real loopback socket just to construct itself, which pytest-socket's blocking then intercepts. On
Linux (this project's actual CI, and the authoritative place these tests are verified - see
.github/workflows/test.yml), that specific construction path doesn't arise, so one shared session
works cleanly.

Running the full suite locally on Windows may still hit that construction issue - if so, this is
a known, already-diagnosed platform gap (DECISIONS.md), not a regression to chase further; use
CI as the source of truth.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """HA's test harness excludes custom_components from component discovery by default -
    this fixture (from pytest-homeassistant-custom-component) turns that back on so the `hass`
    fixture can actually find and set up the pod_home domain."""
    yield
