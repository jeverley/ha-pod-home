"""Firebase Identity Toolkit auth for Pod Point's mobile-api.

Confirmed working against a real account: signs in the same way the Pod Home app's Firebase
SDK does under the hood for email/password sign-in - talks to Google's public Identity Toolkit
REST API directly, not anything Pod Point-hosted.
"""
from __future__ import annotations

import asyncio
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
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._api_key = api_key

        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: datetime | None = None
        # Callers may fire concurrently (e.g. a Home Assistant coordinator polling several
        # chargers via asyncio.gather), all sharing this one PodHomeAuth instance - without a
        # lock, two callers can both see an expiring/missing token at once and each
        # independently fire a refresh/sign-in request, racing to set
        # self._id_token/_refresh_token with last-write-wins.
        self._token_lock = asyncio.Lock()

    async def async_get_id_token(self) -> str:
        """Return a valid id token, signing in or refreshing first if needed."""
        async with self._token_lock:
            if self._id_token is None or self._is_expiring():
                if self._refresh_token:
                    try:
                        await self._async_refresh()
                        return self._id_token
                    except PodHomeAuthError:
                        # Refresh token may itself have expired/been revoked - fall back to a
                        # full sign-in rather than failing outright.
                        pass
                await self._async_sign_in()
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

        # The refresh endpoint uses different (snake_case, short) key names than sign-in.
        self._id_token = body["id_token"]
        self._refresh_token = body.get("refresh_token", self._refresh_token)
        self._expires_at = datetime.utcnow() + timedelta(seconds=int(body["expires_in"]))

    def _apply_token_response(self, body: dict) -> None:
        self._id_token = body["idToken"]
        self._refresh_token = body.get("refreshToken")
        self._expires_at = datetime.utcnow() + timedelta(
            seconds=int(body.get("expiresIn", 3600))
        )
