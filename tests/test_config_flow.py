"""Config flow tests, using pytest-homeassistant-custom-component's real `hass` fixture (see
tests/conftest.py for how the plugin is registered for the whole tests/ tree).

`custom_components.pod_home.async_setup_entry` is patched to a no-op success in every test here -
these tests are about the config flow's own behaviour (unique_id/abort/error/reauth handling),
not about the coordinator's first refresh actually succeeding - see tests/test_init.py for that.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pod_home.const import (
    AUTH_STORAGE_VERSION,
    CONF_EMAIL,
    CONF_PASSWORD,
    DOMAIN,
    auth_store_key,
)
from podpoint_mobile_api import (
    PodPointMobileAuthError,
    PodPointMobileConnectionError,
)

pytestmark = pytest.mark.asyncio

USER_INPUT = {CONF_EMAIL: "driver@example.com", CONF_PASSWORD: "hunter2"}


@pytest.fixture(autouse=True)
def no_real_setup():
    """Never actually stand up the coordinator - only the flow itself is under test here. Also
    patches async_get_clientsession: building a *real* aiohttp connector pulls in aiodns'
    AsyncResolver, which requires either a SelectorEventLoop or the exact winloop.Loop instance -
    neither of which this test's ProactorEventLoop-based `hass` fixture provides on Windows. Not
    a concern for what's under test here: PodPointMobileAuth.async_get_id_token is independently mocked
    in every test below and never actually touches the session object."""
    with (
        patch("custom_components.pod_home.async_setup_entry", AsyncMock(return_value=True)),
        patch("custom_components.pod_home.config_flow.async_get_clientsession"),
    ):
        yield


async def test_user_flow_success_creates_entry(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
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


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PodPointMobileAuthError("bad credentials"), "invalid_auth"),
        (PodPointMobileConnectionError("timed out"), "cannot_connect"),
    ],
)
async def test_user_flow_sign_in_error_shows_error(
    hass: HomeAssistant, error: Exception, expected: str
) -> None:
    with patch(
        "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}


@pytest.mark.parametrize(
    "email",
    [USER_INPUT[CONF_EMAIL], USER_INPUT[CONF_EMAIL].upper()],
    ids=["same-case", "different-case"],
)
async def test_user_flow_duplicate_email_aborts(hass: HomeAssistant, email: str) -> None:
    """async_set_unique_id lowercases the email - a re-add with different casing should still
    match the existing entry rather than creating a second one."""
    existing = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    existing.add_to_hass(hass)

    with patch(
        "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
        AsyncMock(return_value="id-token"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_EMAIL: email}
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow_success_updates_password(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    entry.add_to_hass(hass)

    # A token persisted under the *old* password must not survive reauth - this is the
    # regression test for that exact bug: reauth appearing to succeed, then immediately
    # re-failing because a stale refresh token was restored on reload.
    auth_store = Store(hass, AUTH_STORAGE_VERSION, auth_store_key(entry.entry_id))
    await auth_store.async_save(
        {"id_token": "stale", "refresh_token": "stale-refresh", "expires_at": None}
    )

    with patch(
        "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
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
    assert await auth_store.async_load() is None  # stale token cleared, not just entry.data
    assert entry.data[CONF_EMAIL] == USER_INPUT[CONF_EMAIL]  # unchanged


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PodPointMobileAuthError("bad credentials"), "invalid_auth"),
        (PodPointMobileConnectionError("timed out"), "cannot_connect"),
    ],
)
async def test_reauth_flow_sign_in_error_shows_error(
    hass: HomeAssistant, error: Exception, expected: str
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
        AsyncMock(side_effect=error),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "wrong-password"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": expected}
    assert entry.data[CONF_PASSWORD] == USER_INPUT[CONF_PASSWORD]  # unchanged


async def test_reauth_flow_completes_when_clearing_saved_tokens_fails(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
            AsyncMock(return_value="id-token"),
        ),
        patch.object(Store, "async_remove", AsyncMock(side_effect=OSError("disk full"))),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "new-password"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reconfigure_flow_updates_password(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    entry.add_to_hass(hass)
    auth_store = Store(hass, AUTH_STORAGE_VERSION, auth_store_key(entry.entry_id))
    await auth_store.async_save(
        {"id_token": "stale", "refresh_token": "stale-refresh", "expires_at": None}
    )

    with patch(
        "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
        AsyncMock(return_value="id-token"),
    ):
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reconfigure"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_PASSWORD: "new-password"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {**USER_INPUT, CONF_PASSWORD: "new-password"}
    assert entry.unique_id == USER_INPUT[CONF_EMAIL].lower()
    assert await auth_store.async_load() is None


async def test_reconfigure_flow_changes_email(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_INPUT[CONF_EMAIL].lower(),
        title=USER_INPUT[CONF_EMAIL],
        data=USER_INPUT,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
        AsyncMock(return_value="id-token"),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "New.Driver@example.com", CONF_PASSWORD: "pw2"}
        )

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_EMAIL] == "New.Driver@example.com"
    assert entry.unique_id == "new.driver@example.com"
    assert entry.title == "New.Driver@example.com"


async def test_reconfigure_flow_rejects_email_of_another_entry(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="other@example.com",
        data={CONF_EMAIL: "other@example.com", CONF_PASSWORD: "x"},
    )
    other.add_to_hass(hass)

    with patch(
        "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
        AsyncMock(return_value="id-token"),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "Other@example.com", CONF_PASSWORD: "pw"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data == USER_INPUT


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (PodPointMobileAuthError("bad credentials"), "invalid_auth"),
        (PodPointMobileConnectionError("timed out"), "cannot_connect"),
    ],
)
async def test_reconfigure_flow_sign_in_error_shows_error(
    hass: HomeAssistant, error: Exception, expected: str
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=USER_INPUT[CONF_EMAIL].lower(), data=USER_INPUT
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.pod_home.config_flow.PodPointMobileAuth.async_get_id_token",
        AsyncMock(side_effect=error),
    ):
        result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_PASSWORD: "wrong"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": expected}
    assert entry.data == USER_INPUT
