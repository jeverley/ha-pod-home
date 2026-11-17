"""Exceptions for the podpoint-mobile-api client."""
from __future__ import annotations


class PodHomeAuthError(Exception):
    """Raised when Firebase sign-in or token refresh fails. `transient=True` marks a request-level
    failure (e.g. a network error) to retry; otherwise Firebase rejected the credentials."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class PodHomeApiError(Exception):
    """Raised when a mobile-api.pod-point.com call fails. Carries the HTTP status and (parsed,
    if JSON) response body."""

    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body
        super().__init__(f"mobile-api error {status}: {body!r}")
