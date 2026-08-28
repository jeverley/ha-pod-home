"""Exceptions for the podpoint-mobile-api client."""
from __future__ import annotations


class PodHomeAuthError(Exception):
    """Raised when Firebase sign-in/token refresh fails (bad credentials, expired refresh
    token, MFA required, etc.) - callers should generally treat this as needing re-auth."""


class PodHomeApiError(Exception):
    """Raised when a mobile-api.pod-point.com call fails.

    Carries the HTTP status and (parsed, if JSON) response body so callers/logs have enough to
    debug against - this API's error shapes are still only partially mapped.
    """

    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        super().__init__(f"mobile-api error {status}: {body!r}")
