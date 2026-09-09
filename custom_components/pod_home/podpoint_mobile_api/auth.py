"""Firebase Identity Toolkit auth for Pod Point's mobile-api."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from .const import (
    FIREBASE_API_KEY,
    FIREBASE_REFRESH_URL,
    FIREBASE_SIGN_IN_URL,
    TOKEN_REFRESH_MARGIN,
)
from .exceptions import PodHomeAuthError


class PodHomeAuth:
    """Holds a Firebase session for one Pod Point account and keeps the id token fresh."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        api_key: str = FIREBASE_API_KEY,
        on_token_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._api_key = api_key
        # Called (sync) with the freshly-exported tokens after a sign-in or refresh changes
        # them, so a caller can persist them. Not called from import_tokens().
        self._on_token_change = on_token_change

        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: datetime | None = None
        # Guards against concurrent callers racing to refresh/sign in at once.
        self._token_lock = asyncio.Lock()

    def export_tokens(self) -> dict[str, Any] | None:
        """Return the current tokens as a plain, JSON-serializable dict, or None if nothing's
        been obtained yet."""
        if self._refresh_token is None or self._expires_at is None:
            return None
        return {
            "id_token": self._id_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at.isoformat(),
        }

    def import_tokens(self, data: dict[str, Any] | None) -> None:
        """Restore previously-exported tokens. Call once, right after construction, before the
        first async_get_id_token() - lets that first call refresh instead of doing a full
        sign-in. Malformed/partial data is ignored, not an error."""
        if not isinstance(data, dict):
            return
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            return
        self._refresh_token = refresh_token
        self._id_token = data.get("id_token")
        expires_at = data.get("expires_at")
        try:
            self._expires_at = datetime.fromisoformat(expires_at) if expires_at else None
        except ValueError:
            self._expires_at = None

    async def async_get_id_token(self) -> str:
        """Return a valid id token, signing in or refreshing first if needed."""
        async with self._token_lock:
            if self._id_token is None or self._is_expiring():
                if self._refresh_token:
                    try:
                        await self._async_refresh()
                        self._notify_token_change()
                        # _async_refresh() always sets a real token or raises - never leaves
                        # self._id_token None on success.
                        assert self._id_token is not None
                        return self._id_token
                    except PodHomeAuthError as exc:
                        if exc.transient:
                            raise
                        pass  # refresh token itself may have expired - fall back to sign-in
                await self._async_sign_in()
                self._notify_token_change()
            # Same guarantee as above - _async_sign_in() always sets a real token or raises.
            assert self._id_token is not None
            return self._id_token

    def _notify_token_change(self) -> None:
        if self._on_token_change is None:
            return
        tokens = self.export_tokens()
        # None when expires_at is unknown (e.g. a sign-in response missing expiresIn) - nothing
        # exportable yet, skip the callback rather than notifying with incomplete data.
        if tokens is None:
            return
        self._on_token_change(tokens)

    def _is_expiring(self) -> bool:
        if self._expires_at is None:
            return True
        return datetime.utcnow() >= (self._expires_at - TOKEN_REFRESH_MARGIN)

    async def _async_sign_in(self) -> None:
        try:
            async with self._session.post(
                FIREBASE_SIGN_IN_URL,
                params={"key": self._api_key},
                json={
                    "email": self._email,
                    "password": self._password,
                    "returnSecureToken": True,
                },
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    err = (body or {}).get("error", body)
                    raise PodHomeAuthError(f"Firebase sign-in failed: {err}")
        except aiohttp.ClientError as exc:
            raise PodHomeAuthError(
                f"Firebase sign-in request failed: {exc}", transient=True
            ) from exc

        self._apply_token_response(body)

    async def _async_refresh(self) -> None:
        try:
            async with self._session.post(
                FIREBASE_REFRESH_URL,
                params={"key": self._api_key},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    err = (body or {}).get("error", body)
                    raise PodHomeAuthError(f"Firebase token refresh failed: {err}")
        except aiohttp.ClientError as exc:
            raise PodHomeAuthError(
                f"Firebase token refresh request failed: {exc}", transient=True
            ) from exc

        # Refresh response uses snake_case keys, unlike sign-in.
        try:
            self._id_token = body["id_token"]
            self._refresh_token = body.get("refresh_token", self._refresh_token)
            self._expires_at = datetime.utcnow() + timedelta(seconds=int(body["expires_in"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PodHomeAuthError(f"Firebase token refresh returned an unexpected body: {exc}") from exc

    def _apply_token_response(self, body: dict[str, Any]) -> None:
        self._id_token = body["idToken"]
        self._refresh_token = body.get("refreshToken")
        expires_in = body.get("expiresIn")
        # None (rather than a guessed lifetime) makes _is_expiring() treat the token as already
        # expiring, forcing a refresh/re-sign-in on the next call instead of trusting a guess.
        self._expires_at = (
            datetime.utcnow() + timedelta(seconds=int(expires_in)) if expires_in is not None
            else None
        )
