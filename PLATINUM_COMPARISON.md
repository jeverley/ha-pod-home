# Comparison against Ohme and Peblar (both platinum-tier)

Ohme (`homeassistant/components/ohme`) is the closer analog — UK, cloud-polling, email/password
Firebase-adjacent auth, smart EV charging. Peblar (`homeassistant/components/peblar`) is
local-polling (LAN/direct-to-charger), so its coordinator/auth patterns don't transfer, but its
entity design is still a useful reference. Compared against pod_home's current source, not just
the abstract quality-scale rules.

## Real gaps found

1. ✅ **Fixed.** ~~Password field isn't masked in the UI~~ - `config_flow.py` now uses
   `TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))`
   for the password field and `TextSelectorType.EMAIL` for the email field, matching Ohme.

2. **No reconfigure flow.** Ohme has `async_step_reconfigure` (change email/password
   proactively, from Settings, without deleting and re-adding the integration) as a distinct
   flow from `async_step_reauth` (which only triggers automatically on an auth failure).
   pod_home only has the reauth path.

3. **Single coordinator, one cadence for everything it fetches.** ~~pod_home fetches
   chargers, connectivity/charging status, month-to-date stats, and recent charges every 5
   minutes, uniformly.~~ **Partially addressed**: the coordinator's polling *frequency* is now
   adaptive (60s after observed activity, 300s once quiet - measured live, see
   coordinator.py's FAST/SLOW_POLL_INTERVAL), but every fetch still moves together at whatever
   that one cadence currently is. Ohme goes further and splits *what* gets fetched onto
   separate cadences via two coordinators sharing a common abstract base
   (`OhmeChargeSessionCoordinator`, 30s - session/status data that changes fast;
   `OhmeDeviceInfoCoordinator`, 30min - device settings that barely change), bundled in a small
   `OhmeRuntimeData` dataclass as `entry.runtime_data`. For pod_home that would mean e.g.
   tariffs/currency (essentially static) not being re-fetched every 60s just because
   connectivity status is - still a real, separate improvement, not done by the interval fix.

4. ✅ **Fixed.** ~~Diagnostics could be simpler~~ - `diagnostics.py` now matches Ohme's pattern:
   `entry.data` (email/password) is left out entirely rather than included-then-redacted.

5. **Real `quality_scale.yaml` convention.** HA core's actual mechanism for declaring
   per-rule status is a `quality_scale.yaml` file *inside* the integration's own directory
   (`status: done` / `status: exempt` + a comment, per rule) - not a top-level markdown doc.
   `QUALITY_SCALE.md` is a reasonable stand-in for now; low urgency since HACS doesn't require
   it, but worth knowing the real target format for whenever core submission is a real plan.

6. **Exception messages aren't translatable.** Ohme's coordinator raises
   `UpdateFailed(translation_key="api_failed", translation_domain=DOMAIN)` instead of a raw
   f-string. pod_home raises `UpdateFailed(str(exc))` - functional, but not translated
   (the "exception-translations" gold rule). Lower priority - polish, not correctness.

7. **Reauth's data update, style only.** Ohme calls
   `self.async_update_reload_and_abort(reauth_entry, data_updates=user_input)` (merges just
   the changed fields). pod_home manually spreads `data={**reauth_entry.data, CONF_PASSWORD: ...}`
   - correct, just more verbose than necessary.

8. ✅ **Fixed.** ~~No standalone API client library~~ - the async client is now
   [`podpoint-mobile-api/`](podpoint-mobile-api/), a real installable package with zero HA
   dependency, matching `ohme` on PyPI (maintained by the same person as the `ohme` HA
   integration). This is also what `QUALITY_SCALE.md`'s `async-dependency`/
   `dependency-transparency` rules were actually pointing at - see that file's note on the
   earlier "N/A, no dependency" call being wrong. Not yet published anywhere pip-installable
   (no PyPI/tagged-git release), so `manifest.json`'s `requirements` still can't reference it
   for real - that's the next step in this specific item, not a full close-out.

## Not a gap - a genuine API constraint, not a design mistake

Peblar ships a native `energy_total` sensor (`TOTAL_INCREASING`, Wh) because its charger
firmware tracks a true lifetime counter on-device. Ohme has *no* native energy sensor at all -
docs explicitly say to build one yourself via HA's Integral helper over its Power sensor, because
Ohme's API doesn't expose a cumulative counter either. Pod Point's API exposes neither a live
power reading nor a lifetime total (confirmed - only date-range aggregation), so pod_home's
Energy This Month / Cost This Month (monthly-reset `TOTAL_INCREASING`) is a third reasonable
answer to the same underlying constraint, not something to "fix" to match either of these.

## Recommendation

Items 1, 4, and 8 are done. Items 2 and 3 are real architecture work (a second coordinator, a
new config flow step) worth a deliberate decision on scope/timing rather than doing reflexively.
Items 5-7 are gold-tier polish, fine to defer alongside the rest of `QUALITY_SCALE.md`'s
deferred list.
