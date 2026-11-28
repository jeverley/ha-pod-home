"""Config flow for the Pod Home integration."""
from __future__ import annotations

import logging
from typing import Any

import probatio as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.storage import Store

# TEMPORARY: vendored copy, see __init__.py's import comment.
from .podpoint_mobile_api import PodHomeAuth, PodHomeAuthError, PodHomeConnectionError
from .const import AUTH_STORAGE_VERSION, CONF_EMAIL, CONF_PASSWORD, DOMAIN, auth_store_key

_LOGGER = logging.getLogger(__name__)

EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email")
)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR})


class PodHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pod Home."""

    VERSION = 1

    async def _async_check_sign_in(self, email: str, password: str) -> str | None:
        """Sign in with the credentials; returns the error key on failure."""
        auth = PodHomeAuth(async_get_clientsession(self.hass), email, password)
        try:
            await auth.async_get_id_token()
        except PodHomeConnectionError as exc:
            _LOGGER.debug("Couldn't reach Pod Point during sign-in check: %s", exc)
            return "cannot_connect"
        except PodHomeAuthError as exc:
            _LOGGER.debug("Sign-in check failed: %s", exc)
            return "invalid_auth"
        return None

    async def _async_clear_saved_tokens(self, entry: ConfigEntry) -> None:
        """Drop the persisted refresh token so a session from the old credentials isn't
        restored. A failed clear doesn't block the flow."""
        try:
            await Store(
                self.hass, AUTH_STORAGE_VERSION, auth_store_key(entry.entry_id)
            ).async_remove()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Couldn't clear saved auth tokens", exc_info=True)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()

            error = await self._async_check_sign_in(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if error is None:
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL], data=user_input
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start a reauth flow (e.g. password changed) - reuses the user step's form."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            error = await self._async_check_sign_in(
                reauth_entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if error is None:
                await self._async_clear_saved_tokens(reauth_entry)
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the account's email or password."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            existing = self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, email.lower()
            )
            if existing is not None and existing.entry_id != entry.entry_id:
                return self.async_abort(reason="already_configured")

            error = await self._async_check_sign_in(email, user_input[CONF_PASSWORD])
            if error is None:
                await self._async_clear_saved_tokens(entry)
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=email.lower(),
                    title=email,
                    data_updates=user_input,
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {CONF_EMAIL: (user_input or entry.data)[CONF_EMAIL]},
            ),
            errors=errors,
        )
