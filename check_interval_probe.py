"""One-off diagnostic: empirically measure how often the charger actually checks in with
Pod Point's cloud (connectivity-status-v2's lastSeenAt), instead of assuming a fixed interval.
Run this yourself - same rules as smoke_test_api.py: hidden password prompt, nothing logged
except timestamps. Uses the real podpoint_mobile_api package, not a copy - needs it installed
(pip install -e podpoint-mobile-api, same as smoke_test_api.py).

    python check_interval_probe.py [minutes_to_run]      (default 15 minutes)

Polls GET /chargers/{ppid}/connectivity-status-v2 every 15s and prints a line every time
lastSeenAt actually changes, with the gap since the previous change - that gap IS the
charger's real cloud check-in interval. Run it once while the charger is idle, and again
while it's actively charging if you can, since the interval may not be constant.
"""
from __future__ import annotations

import asyncio
import datetime
import getpass
import os
import sys

import aiohttp

try:
    from podpoint_mobile_api import PodHomeApiClient, PodHomeApiError, PodHomeAuth
except ImportError:
    print(
        "podpoint_mobile_api isn't installed. From this directory, run:\n"
        "  pip install -e podpoint-mobile-api"
    )
    sys.exit(1)

POLL_EVERY_SECONDS = 15


async def main() -> int:
    run_minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 15

    email = os.environ.get("PODPOINT_EMAIL") or input("Pod Point email: ").strip()
    password = os.environ.get("PODPOINT_PASSWORD") or getpass.getpass(
        "Pod Point password (hidden, not stored): "
    )

    async with aiohttp.ClientSession() as session:
        auth = PodHomeAuth(session, email, password)
        api = PodHomeApiClient(session, auth)

        chargers = await api.async_list_chargers()
        if not chargers:
            print("No chargers on this account.")
            return 1
        ppid = chargers[0]["ppid"]
        print(
            f"Watching {ppid}'s lastSeenAt for {run_minutes:.0f} minutes, polling every "
            f"{POLL_EVERY_SECONDS}s. Ctrl+C to stop early - whatever's printed so far is "
            f"still useful.\n"
        )

        last_seen = None
        last_change_observed_at = None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + run_minutes * 60

        while loop.time() < deadline:
            try:
                status = await api.async_connectivity_status(ppid)
            except PodHomeApiError as exc:
                print(f"  poll failed (non-fatal): {exc}")
                await asyncio.sleep(POLL_EVERY_SECONDS)
                continue

            seen = status.get("lastSeenAt")
            charging_state = status.get("chargingState")
            now = datetime.datetime.now()
            if seen != last_seen:
                if last_change_observed_at is not None:
                    gap = now - last_change_observed_at
                    print(
                        f"[{now:%H:%M:%S}] lastSeenAt changed ({charging_state}): "
                        f"{last_seen} -> {seen}  "
                        f"(observed {gap.total_seconds():.0f}s after the previous change)"
                    )
                else:
                    print(f"[{now:%H:%M:%S}] lastSeenAt initially ({charging_state}): {seen}")
                last_seen = seen
                last_change_observed_at = now

            await asyncio.sleep(POLL_EVERY_SECONDS)

    print("\nDone. The gaps printed above are the charger's real cloud check-in interval.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
