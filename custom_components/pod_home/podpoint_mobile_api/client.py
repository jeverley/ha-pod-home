"""Async client for mobile-api.pod-point.com. Mostly read-only - charge-overrides and
remote-lock have real physical side effects on the charger and aren't implemented here.
async_set_vehicle_intents(), async_set_vehicle_charge_limit(), and
async_set_charge_priority_max_price() are writes with real effects on a vehicle/charger -
callers must treat them with the same explicit-authorization discipline as any other write path.
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
                    raise PodHomeAuthError(f"mobile-api rejected the request: {resp.status} {body}")
                if resp.status >= 400:
                    raise PodHomeApiError(resp.status, body)
                return body
        except aiohttp.ClientError as exc:
            raise PodHomeApiError(0, str(exc)) from exc

    async def _async_write(self, method: str, path: str, json_body: dict | None) -> None:
        """Shared by _async_put/_async_patch - same request/error handling, verb differs."""
        token = await self._auth.async_get_id_token()
        url = self._base_url + path
        try:
            async with self._session.request(
                method,
                url,
                json=json_body,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            ) as resp:
                if resp.status in (401, 403):
                    raise PodHomeAuthError(f"mobile-api rejected the request: {resp.status}")
                if resp.status >= 400:
                    raw = await resp.read()
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        body = {"raw": raw.decode(errors="replace")}
                    raise PodHomeApiError(resp.status, body)
        except aiohttp.ClientError as exc:
            raise PodHomeApiError(0, str(exc)) from exc

    async def _async_put(self, path: str, json_body: dict) -> None:
        await self._async_write("PUT", path, json_body)

    async def _async_patch(self, path: str, json_body: dict) -> None:
        await self._async_write("PATCH", path, json_body)

    async def _async_post(self, path: str, json_body: dict | None) -> None:
        await self._async_write("POST", path, json_body)

    async def _async_delete(self, path: str) -> None:
        await self._async_write("DELETE", path, None)

    async def _async_post_for_response(self, path: str, json_body: dict) -> dict:
        """Like _async_write, but returns the parsed response body (needed by the api3 session
        endpoint, whose response body is the entire point of calling it)."""
        token = await self._auth.async_get_id_token()
        url = self._base_url + path
        try:
            async with self._session.post(
                url,
                json=json_body,
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
                    raise PodHomeAuthError(f"mobile-api rejected the request: {resp.status} {body}")
                if resp.status >= 400:
                    raise PodHomeApiError(resp.status, body)
                return body
        except aiohttp.ClientError as exc:
            raise PodHomeApiError(0, str(exc)) from exc

    # --- confirmed endpoints ---

    async def async_list_chargers(self) -> list[dict]:
        """GET /chargers - the account's chargers. Does not include live status."""
        return await self._async_get("/chargers")

    async def async_connectivity_status(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/connectivity-status-v2 - the live status poll."""
        return await self._async_get(f"/chargers/{ppid}/connectivity-status-v2")

    async def async_manual_schedules(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/manual-schedules."""
        return await self._async_get(f"/chargers/{ppid}/manual-schedules")

    async def async_tariffs(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/tariffs."""
        return await self._async_get(f"/chargers/{ppid}/tariffs")

    async def async_charge_statistics(
        self, ppid: str, date_from: datetime.date, date_to: datetime.date
    ) -> dict:
        """GET /chargers/{ppid}/charge-statistics?from=&to=."""
        return await self._async_get(
            f"/chargers/{ppid}/charge-statistics",
            {"from": date_from.isoformat(), "to": date_to.isoformat()},
        )

    async def async_charges(
        self, date_from: datetime.date, date_to: datetime.date
    ) -> dict:
        """GET /charges?from=&to= - session history across all chargers on the account."""
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
        """GET /chargers/{ppid}/security-logs - paginated event log."""
        return await self._async_get(f"/chargers/{ppid}/security-logs")

    async def async_charger_detail_arch5(self, ppid: str) -> dict | None:
        """GET /chargers/arch5/{ppid} - only populated for newer hardware generations."""
        return await self._async_get(f"/chargers/arch5/{ppid}")

    async def async_firmware(self, unit_id: int) -> dict | None:
        """GET /api3/v5/units/{unitId}/firmware - legacy path, superseded by
        async_charger_firmware() (ppid-addressed). Confirmed live: `data: [{serialNumber,
        versionInfo: {architecture, details, manifestId}, updateStatus: {isUpdateAvailable}}]`.
        """
        return await self._async_get(f"/api3/v5/units/{unit_id}/firmware")

    async def async_pod_detail_api3(self, pod_id, include: str | None = None) -> dict:
        """GET /api3/v5/pods/{podId}. podId is not unitId or ppid - obtain it from
        async_api3_pods() first."""
        params = {"include": include} if include else None
        return await self._async_get(f"/api3/v5/pods/{pod_id}", params)

    async def async_create_api3_session(self, email: str, password: str) -> dict:
        """POST /api3/v5/sessions - prerequisite for other api3/v5 calls. Needs the Firebase
        bearer token (handled automatically) plus the plain email/password again in the body.
        Returns `{"sessions": {"user_id": ..., "id": ...}}`; `user_id` is what async_api3_pods()
        needs. NOT YET CALLED live - sending the password again is a bigger deal than a normal
        read-only exploratory call, handle with the same credential care as elsewhere."""
        return await self._async_post_for_response(
            "/api3/v5/sessions", {"email": email, "password": password}
        )

    async def async_api3_pods(
        self, user_id, *, perpage: int = 5, page: int = 1, include: str | None = None
    ) -> dict:
        """GET /api3/v5/users/{userId}/pods. `user_id` comes from async_create_api3_session()
        (not the Firebase uid from /users). Confirmed live: `include=charges` is accepted but
        always returns an empty list - charges are a separate endpoint, see
        async_api3_charges()."""
        params: dict = {"perpage": perpage, "page": page}
        if include:
            params["include"] = include
        return await self._async_get(f"/api3/v5/users/{user_id}/pods", params)

    async def async_api3_charges(self, user_id, *, perpage: int = 5, page: int = 1) -> dict:
        """GET /api3/v5/users/{userId}/charges - source for charge history/current-session data
        (not async_api3_pods()'s `include=charges`, which always returns empty). Returns
        `{"charges": [...]}`; each entry has `id`, `kwh_used`, `duration`, `starts_at`,
        `ends_at`, `energy_cost`, `charging_duration`, `billing_event`, `location`, `pod`,
        `organisation` - `ends_at: null` with a live `kwh_used` marks the current session.
        NOT YET CALLED live."""
        return await self._async_get(
            f"/api3/v5/users/{user_id}/charges", {"perpage": perpage, "page": page}
        )

    async def async_get_users(self) -> dict:
        """GET /users - includes `balance: {currency, amount}`."""
        return await self._async_get("/users")

    # --- path and GET-vs-write verb confirmed structurally; none of these have actually been
    # called against a real account yet ---

    async def async_smart_schedule_active(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/smart-schedules/active."""
        return await self._async_get(f"/chargers/{ppid}/smart-schedules/active")

    async def async_solar_preferences(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/solar/preferences."""
        return await self._async_get(f"/chargers/{ppid}/solar/preferences")

    async def async_charger_subscription(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/subscriptions."""
        return await self._async_get(f"/chargers/{ppid}/subscriptions")

    async def async_get_charge_overrides(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/charge-overrides - reads the current override state."""
        return await self._async_get(f"/chargers/{ppid}/charge-overrides")

    async def async_create_charge_override(
        self, ppid: str, requested_at: datetime.datetime, end_at: datetime.datetime | None
    ) -> None:
        """POST /chargers/{ppid}/charge-overrides - triggers a boost ("Charge Now"). Body shape
        confirmed via the account's public OpenAPI schema (ChargeOverrideRequestDTO):
        `requestedAt` required, `endAt` nullable. The schema describes `endAt: null` as meaning
        an indefinite ("Always On") override, but confirmed live this account's server rejects
        that (403) - schema-documented doesn't mean server-accepted. Callers should pass a real
        `end_at`; see PodHomeBoostFullChargeButton (pod_home's button.py) for what "full charge"
        actually resolves to live. WRITE ENDPOINT with a real physical effect on the charger -
        see CLAUDE.md."""
        await self._async_post(
            f"/chargers/{ppid}/charge-overrides",
            {
                "requestedAt": requested_at.isoformat(),
                "endAt": end_at.isoformat() if end_at else None,
            },
        )

    async def async_delete_charge_override(self, ppid: str) -> None:
        """DELETE /chargers/{ppid}/charge-overrides - cancels the active boost. WRITE ENDPOINT
        with a real physical effect on the charger - see CLAUDE.md."""
        await self._async_delete(f"/chargers/{ppid}/charge-overrides")

    async def async_get_remote_lock_status(self, ppid: str) -> dict:
        """GET /remote-lock/{ppid} - reads the current lock state. Read-only; does not set the
        lock (that would be a POST/PUT, not implemented)."""
        return await self._async_get(f"/remote-lock/{ppid}")

    async def async_warranty(self, ppid: str) -> dict:
        """GET /warranties/{ppid}."""
        return await self._async_get(f"/warranties/{ppid}")

    async def async_access_status(self) -> dict:
        """GET /users/access-status."""
        return await self._async_get("/users/access-status")

    async def async_agreements(self) -> dict:
        """GET /users/agreements."""
        return await self._async_get("/users/agreements")

    async def async_energy_suppliers(self) -> dict:
        """GET /energy/suppliers."""
        return await self._async_get("/energy/suppliers")

    async def async_reward_wallet(self) -> dict:
        """GET /reward-wallet."""
        return await self._async_get("/reward-wallet")

    async def async_reward_wallet_transactions(self) -> dict:
        """GET /reward-wallet/transactions."""
        return await self._async_get("/reward-wallet/transactions")

    async def async_smart_charging_chargers_and_vehicles(self) -> list:
        """GET /smart-charging/delegated-controls/vehicles - a list, one entry per charger
        with any linked vehicles (unlike most endpoints here, the response isn't wrapped in a
        `data` object)."""
        return await self._async_get("/smart-charging/delegated-controls/vehicles")

    async def async_smart_charging_preferences(self, ppid: str) -> dict:
        """GET /smart-charging/delegated-controls/{ppid}/preferences."""
        return await self._async_get(f"/smart-charging/delegated-controls/{ppid}/preferences")

    async def async_subscriptions(self) -> dict:
        """GET /subscriptions."""
        return await self._async_get("/subscriptions")

    async def async_set_vehicle_intents(
        self, ppid: str, vehicle_id: str, intent_details: list[dict]
    ) -> None:
        """PUT /smart-charging/delegated-controls/{ppid}/vehicles/{vehicleId}/intents - sets a
        vehicle's per-day Smart Charging targets (Target Charge/Ready By). Body:
        `{"intentDetails": intent_details}`, each entry `{"dayOfWeek": ..., "chargeByTime":
        "HH:MM:SS", "chargeKWh": <energy, not percent>}` - see DAY_OF_WEEK_OPTIONS in pod_home's
        const.py. Not yet exercised against a real account. Real effect: changes what a real
        vehicle charges to and by when - requires explicit, informed authorization."""
        await self._async_put(
            f"/smart-charging/delegated-controls/{ppid}/vehicles/{vehicle_id}/intents",
            {"intentDetails": intent_details},
        )

    async def async_set_vehicle_charge_limit(
        self, ppid: str, vehicle_id: str, charge_limit_percent: float
    ) -> None:
        """PATCH /smart-charging/delegated-controls/{ppid}/vehicles/{vehicleId} - sets
        chargeLimitPercent on the vehicle's charge state (Target Charge; a vehicle-level
        percentage, distinct from the per-day energy figures async_set_vehicle_intents() writes
        for Ready By). Partial update - no required fields, so sending only chargeLimitPercent
        is valid. Not yet exercised against a real account. Real effect: changes what percentage
        a real vehicle charges to - requires explicit, informed authorization."""
        await self._async_patch(
            f"/smart-charging/delegated-controls/{ppid}/vehicles/{vehicle_id}",
            {"vehicle": {"chargeState": {"chargeLimitPercent": charge_limit_percent}}},
        )

    async def async_set_charge_priority_max_price(self, ppid: str, max_price: float) -> None:
        """PATCH /smart-charging/delegated-controls/{ppid}/preferences - sets the charger's
        Charge Priority by writing maxPrice directly (computed client-side from the account's
        tariff rates - see max_price_for_charging_priority() in pod_home's helpers.py). Partial
        update - no required fields, so sending only maxPrice is valid. Real effect: changes how
        Smart Charging prioritizes cost vs. completion - requires explicit, informed
        authorization."""
        await self._async_patch(
            f"/smart-charging/delegated-controls/{ppid}/preferences",
            {"maxPrice": max_price},
        )

    async def async_charger_restrictions(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/restrictions - the charger's allowance based on the
        authenticated user."""
        return await self._async_get(f"/chargers/{ppid}/restrictions")

    async def async_charger_model_info(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/model-info - a dedicated model-info endpoint, distinct from the
        modelInfo already embedded in each /chargers entry."""
        return await self._async_get(f"/chargers/{ppid}/model-info")

    async def async_charger_firmware(self, ppid: str) -> list:
        """GET /chargers/{ppid}/firmware - confirmed live: a bare list (not `data`-wrapped,
        unlike the legacy async_firmware()/api3/v5/units/{unitId}/firmware path this replaced in
        pod_home): `[{serialNumber, versionInfo: {architecture, details, manifestId},
        updateStatus: {isUpdateAvailable}}]`. ppid-addressed, no unitId dependency."""
        return await self._async_get(f"/chargers/{ppid}/firmware")

    async def async_flex_enrolment(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/flex-enrolment - the charger's enrolment in a grid-flexibility
        program."""
        return await self._async_get(f"/chargers/{ppid}/flex-enrolment")

    async def async_flex_requests(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/flex-requests - active grid-flexibility requests for this
        charger."""
        return await self._async_get(f"/chargers/{ppid}/flex-requests")

    async def async_dno_region(self, ppid: str) -> dict:
        """GET /chargers/{ppid}/dnoregion - the charger's Distribution Network Operator region."""
        return await self._async_get(f"/chargers/{ppid}/dnoregion")

    async def async_delegated_control(self, ppid: str) -> dict:
        """GET /smart-charging/delegated-controls/{ppid} - single-charger view, distinct from
        async_smart_charging_chargers_and_vehicles() (the account-wide list). Shape not yet
        confirmed."""
        return await self._async_get(f"/smart-charging/delegated-controls/{ppid}")

    async def async_vehicle_interventions(self, vehicle_id: str) -> dict:
        """GET /vehicles/{vehicleId}/interventions. Not yet exercised against a real account."""
        return await self._async_get(f"/vehicles/{vehicle_id}/interventions")
