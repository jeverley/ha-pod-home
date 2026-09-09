"""Tests for PodHomeAuth against the real implementation (see _fakes.py for the scripted
aiohttp.ClientSession stand-in) - no network I/O."""
from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import aiohttp
import pytest

from podpoint_mobile_api.auth import PodHomeAuth
from podpoint_mobile_api.exceptions import PodHomeAuthError

from ._fakes import FakeResponse, FakeSession, RaisesClientError


def _auth(session: FakeSession) -> PodHomeAuth:
    return PodHomeAuth(cast(aiohttp.ClientSession, session), "driver@example.com", "hunter2")


async def test_sign_in_on_first_call() -> None:
    session = FakeSession(
        [FakeResponse(200, {"idToken": "id1", "refreshToken": "ref1", "expiresIn": "3600"})]
    )
    auth = _auth(session)

    token = await auth.async_get_id_token()

    assert token == "id1"
    assert session.calls[0][0] == "post"


async def test_refresh_used_once_a_refresh_token_is_held() -> None:
    session = FakeSession(
        [FakeResponse(200, {"id_token": "id2", "expires_in": "3600"})]
    )
    auth = _auth(session)
    auth.import_tokens({"refresh_token": "ref1", "expires_at": "2000-01-01T00:00:00+00:00"})

    token = await auth.async_get_id_token()

    assert token == "id2"


async def test_rejected_refresh_token_falls_back_to_sign_in() -> None:
    session = FakeSession(
        [
            FakeResponse(400, {"error": "invalid_grant"}),
            FakeResponse(200, {"idToken": "id3", "refreshToken": "ref3", "expiresIn": "3600"}),
        ]
    )
    auth = _auth(session)
    auth.import_tokens({"refresh_token": "expired", "expires_at": "2000-01-01T00:00:00+00:00"})

    token = await auth.async_get_id_token()

    assert token == "id3"
    assert len(session.calls) == 2  # refresh attempted, then a real sign-in


async def test_transient_network_error_during_refresh_does_not_fall_back_to_sign_in() -> None:
    """A network-level failure isn't the same as Firebase actually rejecting the refresh
    token - falling back to a full password sign-in on every network blip would be wasteful
    and would mask the real (transient) cause."""
    session = FakeSession([RaisesClientError()])
    auth = _auth(session)
    auth.import_tokens({"refresh_token": "ref1", "expires_at": "2000-01-01T00:00:00+00:00"})

    with pytest.raises(PodHomeAuthError) as exc_info:
        await auth.async_get_id_token()

    assert exc_info.value.transient is True
    assert len(session.calls) == 1  # no sign-in attempt made


async def test_malformed_refresh_response_raises_auth_error_not_key_error() -> None:
    """A malformed 200 body from the refresh endpoint must surface as PodHomeAuthError, not
    propagate a raw KeyError past async_get_id_token's own exception handling - falls through
    to a real sign-in, same as an explicitly-rejected refresh token would."""
    session = FakeSession(
        [
            FakeResponse(200, {"unexpected": "shape"}),
            FakeResponse(200, {"idToken": "id4", "refreshToken": "ref4", "expiresIn": "3600"}),
        ]
    )
    auth = _auth(session)
    auth.import_tokens({"refresh_token": "ref1", "expires_at": "2000-01-01T00:00:00+00:00"})

    token = await auth.async_get_id_token()

    assert token == "id4"


async def test_rejected_sign_in_raises_auth_error() -> None:
    session = FakeSession([FakeResponse(400, {"error": {"message": "INVALID_PASSWORD"}})])
    auth = _auth(session)

    with pytest.raises(PodHomeAuthError):
        await auth.async_get_id_token()


async def test_transient_network_error_during_sign_in_is_marked_transient() -> None:
    session = FakeSession([RaisesClientError()])
    auth = _auth(session)

    with pytest.raises(PodHomeAuthError) as exc_info:
        await auth.async_get_id_token()

    assert exc_info.value.transient is True


async def test_sign_in_missing_expires_in_does_not_crash_the_change_callback() -> None:
    """A 200 sign-in response missing expiresIn leaves expires_at unset (not a guessed 3600s) -
    export_tokens() then returns None, and the on_token_change callback must be skipped
    gracefully rather than crashing on an assumption that tokens are always exportable after a
    successful sign-in."""
    session = FakeSession([FakeResponse(200, {"idToken": "id5", "refreshToken": "ref5"})])
    on_token_change = MagicMock()
    auth = PodHomeAuth(
        cast(aiohttp.ClientSession, session),
        "driver@example.com",
        "hunter2",
        on_token_change=on_token_change,
    )

    token = await auth.async_get_id_token()

    assert token == "id5"
    on_token_change.assert_not_called()
