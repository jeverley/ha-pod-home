"""Firebase Identity Toolkit auth for Pod Point's mobile-api."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

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
        on_token_change: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._api_key = api_key
        # Called (sync, no args) after a sign-in or refresh changes the tokens, so a caller can
        # persist them. Not called from import_tokens().
        self._on_token_change = on_token_change

        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: datetime | None = None
        # Guards against concurrent callers racing to refresh/sign in at once.
        self._token_lock = asyncio.Lock()

    def export_tokens(self) -> dict | None:
        """Return the current tokens as a plain, JSON-serializable dict, or None if nothing's
        been obtained yet."""
        if self._refresh_token is None or self._expires_at is None:
            return None
        return {
            "id_token": self._id_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at.isoformat(),
        }

    def import_tokens(self, data: dict | None) -> None:
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
                        if self._on_token_change:
                            self._on_token_change()
                        return self._id_token
                    except PodHomeAuthError:
                        pass  # refresh token itself may have expired - fall back to sign-in
                await self._async_sign_in()
                if self._on_token_change:
                    self._on_token_change()
            return self._id_token

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
            raise PodHomeAuthError(f"Firebase sign-in request failed: {exc}") from exc

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
            raise PodHomeAuthError(f"Firebase token refresh request failed: {exc}") from exc

        # Refresh response uses snake_case keys, unlike sign-in.
        self._id_token = body["id_token"]
        self._refresh_token = body.get("refresh_token", self._refresh_token)
        self._expires_at = datetime.utcnow() + timedelta(seconds=int(body["expires_in"]))

    def _apply_token_response(self, body: dict) -> None:
        self._id_token = body["idToken"]
        self._refresh_token = body.get("refreshToken")
        self._expires_at = datetime.utcnow() + timedelta(
            seconds=int(body.get("expiresIn", 3600))
        )
