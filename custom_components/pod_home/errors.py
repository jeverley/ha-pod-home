"""Errors raised to the user, with their messages in strings.json's `exceptions` block."""
from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN


def translated_error(key: str, **placeholders: str) -> HomeAssistantError:
    """A HomeAssistantError whose message is the `exceptions` entry for `key`."""
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


def translated_validation_error(key: str, **placeholders: str) -> ServiceValidationError:
    """A ServiceValidationError whose message is the `exceptions` entry for `key`."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )
