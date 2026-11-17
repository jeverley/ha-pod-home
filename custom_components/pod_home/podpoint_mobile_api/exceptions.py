"""Exceptions for the podpoint-mobile-api client."""
from __future__ import annotations


class PodHomeError(Exception):
    """Base class for every error this client raises."""


class PodHomeAuthError(PodHomeError):
    """Firebase or mobile-api rejected the account's credentials."""


class PodHomeConnectionError(PodHomeError):
    """Pod Point or Firebase couldn't be reached, timed out, or failed server-side."""


class PodHomeApiError(PodHomeError):
    """An HTTP error response from a Pod Point API. Carries the HTTP status and (parsed, if JSON)
    response body."""

    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body
        super().__init__(f"mobile-api error {status}: {body!r}")
