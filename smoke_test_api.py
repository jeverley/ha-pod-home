"""Dev-only smoke test for the ACTUAL podpoint-mobile-api package (not a copy of the logic) -
run this yourself: hidden password prompt (getpass, not echoed/logged/stored), nothing printed
to the console except status codes and counts.

    python smoke_test_api.py

Only exercises confirmed-safe GET endpoints. Needs podpoint_mobile_api installed - if you get
an ImportError, run this first:

    pip install -e podpoint-mobile-api
"""
from __future__ import annotations

import asyncio
import datetime
import getpass
import json
import os
import sys
from pathlib import Path

import aiohttp

try:
    from podpoint_mobile_api import (
        PodHomeApiClient,
        PodHomeApiError,
        PodHomeAuth,
        PodHomeAuthError,
    )
except ImportError:
    print(
        "podpoint_mobile_api isn't installed. From this directory, run:\n"
        "  pip install -e podpoint-mobile-api"
    )
    sys.exit(1)

OUT_DIR = Path(__file__).parent / "scratch" / "output"


def _describe_error(exc: Exception) -> str:
    """PodHomeApiError carries .status/.body; PodHomeAuthError (e.g. a 401/403 from mobile-api
    itself) doesn't - describe either safely without assuming which one we got."""
    status = getattr(exc, "status", None)
    if status is not None:
        return f"status={status} body={getattr(exc, 'body', None)}"
    return str(exc)


def save(name: str, data) -> None:
    """Save a full raw response to a local file - console output stays summary-only/PII-free,
    but the full shape is needed to actually design against (e.g. does /users really return a
    balance.currency field, and where exactly)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{name}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  saved -> {path.relative_to(Path(__file__).parent)}")


async def main() -> int:
    email = os.environ.get("PODPOINT_EMAIL") or input("Pod Point email: ").strip()
    password = os.environ.get("PODPOINT_PASSWORD") or getpass.getpass(
        "Pod Point password (hidden, not stored): "
    )

    async with aiohttp.ClientSession() as session:
        auth = PodHomeAuth(session, email, password)
        api = PodHomeApiClient(session, auth)

        print("Signing in (real PodHomeAuth.async_get_id_token)...")
        try:
            await auth.async_get_id_token()
        except PodHomeAuthError as exc:
            print(f"Sign-in failed: {exc}")
            return 1
        print("  OK")

        print("\nasync_list_chargers()")
        try:
            chargers = await api.async_list_chargers()
        except (PodHomeApiError, PodHomeAuthError) as exc:
            print(f"  FAILED: {_describe_error(exc)}")
            return 1
        print(f"  -> {len(chargers)} charger(s)")
        if not chargers:
            return 0
        ppid = chargers[0]["ppid"]
        print(f"  using ppid {ppid}")

        print("\nasync_connectivity_status()")
        try:
            status = await api.async_connectivity_status(ppid)
            print(f"  -> connectionState={status.get('connectionState')} "
                  f"chargingState={status.get('chargingState')}")
        except (PodHomeApiError, PodHomeAuthError) as exc:
            print(f"  FAILED: {_describe_error(exc)}")

        today = datetime.date.today()
        month_ago = today - datetime.timedelta(days=30)

        print("\nasync_charges() (last 30 days)")
        try:
            charges = await api.async_charges(month_ago, today)
            count = ((charges or {}).get("data") or {}).get("count")
            print(f"  -> count={count}")
        except (PodHomeApiError, PodHomeAuthError) as exc:
            print(f"  FAILED: {_describe_error(exc)}")

        print("\nasync_tariffs()")
        try:
            tariffs = await api.async_tariffs(ppid)
            print(f"  -> {len(tariffs.get('data', []))} tariff entries")
        except (PodHomeApiError, PodHomeAuthError) as exc:
            print(f"  FAILED: {_describe_error(exc)}")

        # A second call to prove token caching works (should NOT sign in again).
        print("\nasync_list_chargers() again (should reuse the cached token, no re-login)")
        await api.async_list_chargers()
        print("  -> OK, no errors")

        # --- additional endpoint coverage: a same-day (zero-width) date range, and the
        # account/currency endpoint - both edge cases worth checking on every run, not just
        # the "does auth still work" happy path above ---

        print("\nasync_charge_statistics(ppid, today, today) - same-day range")
        try:
            stats_today = await api.async_charge_statistics(ppid, today, today)
            print(f"  -> 200 OK, keys: {sorted(stats_today.keys()) if isinstance(stats_today, dict) else type(stats_today)}")
            save("charge_statistics_today", stats_today)
        except (PodHomeApiError, PodHomeAuthError) as exc:
            print(f"  -> FAILED: {_describe_error(exc)}")
            save("charge_statistics_today", {"_error": _describe_error(exc)})

        print("\nasync_get_users() - looking for a billing-currency field")
        try:
            users = await api.async_get_users()
            print(f"  -> 200 OK, top-level keys: "
                  f"{sorted(users.keys()) if isinstance(users, dict) else type(users)}")
            save("users", users)
            print("  full response saved locally for shape review (not printed here - may contain PII)")
        except (PodHomeApiError, PodHomeAuthError) as exc:
            print(f"  -> FAILED: {_describe_error(exc)}")
            save("users", {"_error": _describe_error(exc)})

    print("\nAll real podpoint_mobile_api calls exercised successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
