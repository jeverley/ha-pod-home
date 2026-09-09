"""Shared fakes for podpoint_mobile_api's own tests - a minimal stand-in for
aiohttp.ClientSession that returns scripted responses without any real network I/O."""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import aiohttp


class FakeResponse:
    """Stands in for aiohttp.ClientResponse - only the surface client.py/auth.py touch."""

    def __init__(self, status: int, json_body: Any = None) -> None:
        self.status = status
        self._json_body = json_body

    async def json(self, content_type: str | None = None) -> Any:
        return self._json_body

    async def read(self) -> bytes:
        if self._json_body is None:
            return b""
        return json.dumps(self._json_body).encode()

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class RaisesClientError:
    """An async context manager that raises aiohttp.ClientError on entry - simulates a
    request that never got a response at all (DNS failure, connection refused, timeout)."""

    def __init__(self, message: str = "connection failed") -> None:
        self._message = message

    async def __aenter__(self) -> "RaisesClientError":
        raise aiohttp.ClientConnectionError(self._message)

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class FakeSession:
    """Scripted aiohttp.ClientSession - each call to get/post/request pops the next entry off
    `responses`, ignoring the actual URL/params/json/data/headers passed."""

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _next(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, args, kwargs))
        return self._responses.pop(0)

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._next("get", args, kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        return self._next("post", args, kwargs)

    def request(self, *args: Any, **kwargs: Any) -> Any:
        return self._next("request", args, kwargs)
