"""Config flow tests, using pytest-homeassistant-custom-component's real `hass` fixture (see
tests/homeassistant/conftest.py for why this lives in its own subtree).

`custom_components.pod_home.async_setup_entry` is patched to a no-op success in every test here -
these tests are about the config flow's own behaviour (unique_id/abort/error/reauth handling),
not about the coordinator's first refresh actually succeeding, which is a separate, not-yet-
written piece of coverage.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pod_home.const import CONF_EMAIL, CONF_PASSWORD, DOMAIN
from custom_components.pod_home.podpoint_mobile_api import PodHomeAuthError

pytestmark = pytest.mark.asyncio

USER_INPUT = {CONF_EMAIL: "driver@example.com", CONF_PASSWORD: "hunter2"}


@pytest.fixture(autouse=True)
def no_real_setup():
    """Never actually stand up the coordinator - only the flow itself is under test here. Also
    patches async_get_clientsession: building a *real* aiohttp connector pulls in aiodns'
    AsyncResolver, which requires either a SelectorEventLoop or the exact winloop.Loop instance -
    neither of which this test's ProactorEventLoop-based `hass` fixture provides on Windows. Not
    a concern for what's under test here: PodHomeAuth.async_get_id_token is independently mocked
    in every test below and never actually touches the session object."""
    with (
        patch("custom_components.pod_home.async_setup_entry", AsyncMock(return_value=True)),
        patch("custom_components.pod_home.config_flow.async_get_clientsession"),
    ):
        yield


async def test_user_flow_success_creates_entry(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.pod_home.config_flow.PodHomeAuth.async_get_id_token",
        AsyncMock(return_value="id-token"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == USER_INPUT[CONF_EMAIL]
    assert result["data"] == USER_INPUT


async def test_user_flow_invalid_auth_shows_error(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.pod_home.config_flow.PodHomeAuth.async_get_id_token",
        AsyncMock(side_effect=PodHomeAuthError("bad credentials")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_duplicate_email_aborts(hass: HomeAssistant) -> None:
    existing = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    existing.add_to_hass(hass)

    with patch(
        "custom_components.pod_home.config_flow.PodHomeAuth.async_get_id_token",
        AsyncMock(return_value="id-token"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_duplicate_email_is_case_insensitive(hass: HomeAssistant) -> None:
    """async_set_unique_id lowercases the email - a re-add with different casing should still
    match the existing entry rather than creating a second one."""
    existing = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    existing.add_to_hass(hass)
    shouty_input = {**USER_INPUT, CONF_EMAIL: USER_INPUT[CONF_EMAIL].upper()}

    with patch(
        "custom_components.pod_home.config_flow.PodHomeAuth.async_get_id_token",
        AsyncMock(return_value="id-token"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], shouty_input
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow_success_updates_password(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pod_home.config_flow.PodHomeAuth.async_get_id_token",
        AsyncMock(return_value="id-token"),
    ):
        result = entry.start_reauth_flow(hass)
        result = await result
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert entry.data[CONF_EMAIL] == USER_INPUT[CONF_EMAIL]  # unchanged


async def test_reauth_flow_invalid_auth_shows_error(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pod_home.config_flow.PodHomeAuth.async_get_id_token",
        AsyncMock(side_effect=PodHomeAuthError("bad credentials")),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "wrong-password"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_PASSWORD] == USER_INPUT[CONF_PASSWORD]  # unchanged
