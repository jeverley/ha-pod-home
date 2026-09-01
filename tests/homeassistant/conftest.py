"""Fixtures for the HA-dependent test subtree.

pytest.ini disables pytest-homeassistant-custom-component repo-wide (`-p no:homeassistant`)
because it's disruptive when active for the whole session - it swaps pytest-asyncio's
`event_loop` fixture for HA's own and blocks real sockets, which breaks the plain offline tests
in tests/test_helpers.py and tests/test_translation_keys.py even though they never touch it.
`pytest_plugins` here re-registers it for this subtree specifically.

IMPORTANT - this must be invoked as its own separate pytest run, never together with the parent
tests/ directory in the same session, and always via `python -m pytest` (not bare `pytest` - only
`-m` puts the repo root, and so custom_components/, on sys.path):

    python -m pytest tests/homeassistant/ --force-enable-socket -o asyncio_mode=auto

(root pytest.ini's `--ignore=tests/homeassistant` already keeps a bare `pytest`/`pytest tests/`
from touching this directory, so the two suites can't accidentally collide). Collecting this
conftest.py as part of a larger walk starting above tests/homeassistant/ trips pytest's "nested
conftest declaring pytest_plugins" restriction (confirmed by trying `pytest tests/` with this
file unchanged - fails at collection); targeting this directory directly does not.

--force-enable-socket/asyncio_mode are passed as CLI flags above rather than living in a
tests/homeassistant/pytest.ini of their own - both are options owned by plugins the offline-only
`pytest tests/` run never installs, and pytest hard-errors on an unrecognized CLI option (unlike
an unknown ini key, which only warns) - see pytest.ini and DECISIONS.md.
"""
from __future__ import annotations

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component.plugins"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """HA's test harness excludes custom_components from component discovery by default -
    this fixture (from pytest-homeassistant-custom-component) turns that back on so the `hass`
    fixture can actually find and set up the pod_home domain."""
    yield
