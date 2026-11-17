"""Tests for PodHomeApiClient's HTTP status/error-body classification against the real
implementation (see _fakes.py for the scripted aiohttp.ClientSession stand-in) - no network I/O."""
from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from podpoint_mobile_api.auth import PodHomeAuth
from podpoint_mobile_api.client import PodHomeApiClient
from podpoint_mobile_api.exceptions import PodHomeApiError, PodHomeAuthError, PodHomeConnectionError

from ._fakes import FakeResponse, FakeSession, RaisesClientError, RaisesTimeout


def _client(session: FakeSession) -> PodHomeApiClient:
    auth = cast(PodHomeAuth, AsyncMock())
    auth.async_get_id_token = AsyncMock(return_value="a-token")
    auth.invalidate_id_token = MagicMock()
    return PodHomeApiClient(cast(aiohttp.ClientSession, session), auth)


async def test_get_returns_parsed_body_on_success() -> None:
    client = _client(FakeSession([FakeResponse(200, [{"ppid": "PSL-000001"}])]))

    result = await client.async_list_chargers()

    assert result == [{"ppid": "PSL-000001"}]


@pytest.mark.parametrize("status", [401, 403])
async def test_get_rejected_twice_raises_auth_error(status: int) -> None:
    session = FakeSession([FakeResponse(status, {"error": "x"}), FakeResponse(status, {"error": "x"})])
    client = _client(session)

    with pytest.raises(PodHomeAuthError):
        await client.async_list_chargers()

    assert len(session.calls) == 2


async def test_get_rejected_once_retries_with_fresh_token() -> None:
    session = FakeSession([FakeResponse(401, {"error": "expired"}), FakeResponse(200, [])])
    client = _client(session)

    assert await client.async_list_chargers() == []
    client._auth.invalidate_id_token.assert_called_once()  # type: ignore[attr-defined]


async def test_get_500_raises_api_error_with_status() -> None:
    client = _client(FakeSession([FakeResponse(500, {"error": "server error"})]))

    with pytest.raises(PodHomeApiError) as exc_info:
        await client.async_list_chargers()

    assert exc_info.value.status == 500


async def test_get_connection_failure_raises_connection_error() -> None:
    client = _client(FakeSession([RaisesClientError()]))

    with pytest.raises(PodHomeConnectionError):
        await client.async_list_chargers()


async def test_get_timeout_raises_connection_error() -> None:
    client = _client(FakeSession([RaisesTimeout()]))

    with pytest.raises(PodHomeConnectionError):
        await client.async_list_chargers()


async def test_requests_carry_a_total_timeout() -> None:
    session = FakeSession([FakeResponse(200, [])])

    await _client(session).async_list_chargers()

    _method, _args, kwargs = session.calls[0]
    assert kwargs["timeout"].total == 30


async def test_write_401_raises_auth_error() -> None:
    client = _client(FakeSession([FakeResponse(401, None), FakeResponse(401, None)]))

    with pytest.raises(PodHomeAuthError):
        await client.async_delete_charge_override("PSL-000001")


async def test_write_success_returns_none() -> None:
    client = _client(FakeSession([FakeResponse(204, None)]))

    assert await client.async_delete_charge_override("PSL-000001") is None


async def test_post_for_response_returns_parsed_body() -> None:
    client = _client(
        FakeSession([FakeResponse(200, {"sessions": {"user_id": 1, "id": "s1"}})])
    )

    result = await client.async_create_api3_session("driver@example.com", "hunter2")

    assert result == {"sessions": {"user_id": 1, "id": "s1"}}


async def test_post_for_response_non_dict_body_raises_api_error() -> None:
    client = _client(FakeSession([FakeResponse(200, ["not", "a", "dict"])]))

    with pytest.raises(PodHomeApiError):
        await client.async_create_api3_session("driver@example.com", "hunter2")
