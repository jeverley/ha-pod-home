"""Async client for mobile-api.pod-point.com.

Every path/param shape here was confirmed live against a real account and a Solo 3, EXCEPT
where a method's docstring says otherwise. Deliberately read-only for now - charge-overrides
(charge now) and remote-lock (cable lock) have real physical side effects on the charger and
are intentionally not implemented here yet; add them once you're ready to test them live and
know what they'll do.
"""
from __future__ import annotations

import datetime

import aiohttp

from .auth import PodHomeAuth
from .const import MOBILE_API_BASE_URL
from .exceptions import PodHomeApiError, PodHomeAuthError


class PodHomeApiClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth: PodHomeAuth,
        base_url: str = MOBILE_API_BASE_URL,
    ) -> None:
        self._session = session
        self._auth = auth
        self._base_url = base_url

    async def _async_get(self, path: str, params: dict | None = None):
        token = await self._auth.async_get_id_token()
        url = self._base_url + path
        try:
            async with self._session.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as resp:
                raw = await resp.read()
                if not raw:
                    body = None
                else:
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        body = {"raw": raw.decode(errors="replace")}
                if resp.status in (401, 403):
                    # A real auth failure from mobile-api itself (session/token revoked
                    # server-side), not just a failed Firebase sign-in/refresh - route it
                    # through the same exception type so callers treat it as needing reauth
                    # rather than a generic (non-fatal, often swallowed) API error.
                    raise PodHomeAuthError(f"mobile-api rejected the request: {resp.status} {body}")
                if resp.status >= 400:
                    raise PodHomeApiError(resp.status, body)
                return body
        except aiohttp.ClientError as exc:
            raise PodHomeApiError(0, str(exc)) from exc

    # --- confirmed endpoints ---

    async def async_list_chargers(self) -> list[dict]:
        """GET /chargers - the account's chargers (ppid, unitId, modelInfo, delegatedControl,
        subscription). Does NOT include live status - see async_connectivity_status()."""
        return await self._async_get("/chargers")

    async def async_connectivity_status(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/connectivity-status-v2 - connectionState/chargingState/
        connectionQuality/lastSeenAt. This is the live status poll."""
        return await self._async_get(f"/chargers/{ppid}/connectivity-status-v2")

    async def async_manual_schedules(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/manual-schedules - one entry per day of week with
        startDay/startTime/endDay/endTime/status.isActive."""
        return await self._async_get(f"/chargers/{ppid}/manual-schedules")

    async def async_tariffs(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/tariffs."""
        return await self._async_get(f"/chargers/{ppid}/tariffs")

    async def async_charge_statistics(
        self, ppid: str, date_from: datetime.date, date_to: datetime.date
    ) -> dict:
        """GET /chargers/{ppid}/charge-statistics?from=&to= - 500s without the date range."""
        return await self._async_get(
            f"/chargers/{ppid}/charge-statistics",
            {"from": date_from.isoformat(), "to": date_to.isoformat()},
        )

    async def async_charges(
        self, date_from: datetime.date, date_to: datetime.date
    ) -> dict:
        """GET /charges?from=&to= - session history across all chargers on the account
        (charger.id in each entry is the ppid). 500s without the date range."""
        return await self._async_get(
            "/charges", {"from": date_from.isoformat(), "to": date_to.isoformat()}
        )

    async def async_charges_stats(
        self, date_from: datetime.date, date_to: datetime.date
    ) -> dict:
        """GET /charges/stats?from=&to= - aggregated energy/cost/duration summary."""
        return await self._async_get(
            "/charges/stats", {"from": date_from.isoformat(), "to": date_to.isoformat()}
        )

    # --- confirmed live, but not (yet) wired into any entity ---

    async def async_security_logs(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/security-logs - paginated event log. Confirmed live, but not
        wired into any entity yet (diagnostic-only, low priority)."""
        return await self._async_get(f"/chargers/{ppid}/security-logs")

    async def async_charger_detail_arch5(self, ppid: str) -> dict | None:
        """GET /chargers/arch5/{ppid} - returned an empty body for a Solo 3 (architecture
        "3.0"); likely only populated for newer "arch5" hardware. Confirmed live (as empty),
        semantics otherwise unconfirmed."""
        return await self._async_get(f"/chargers/arch5/{ppid}")

    async def async_get_users(self) -> dict:
        """GET /users - confirmed live. Includes `balance: {currency, amount}`, the source for
        the account's billing currency."""
        return await self._async_get("/users")
