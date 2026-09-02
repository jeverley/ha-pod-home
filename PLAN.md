# Pod Home integration — plan

## Where this came from

`mattrayner/pod-point-home-assistant-component` (the community HA integration your fork was
based on) talks to Pod Point's legacy `api.pod-point.com/v4`. Pod Point shipped a new consumer
app ("Pod Home") on a different backend (`mobile-api.pod-point.com`, Firebase Auth) and split
branding — "Pod Point" now reads as the public/commercial charging network, "Pod Home" as the
home-charger app. That backend's endpoints and auth model have been mapped out and confirmed
live against your account.

Decision already made: build a clean new integration (`pod_home`) rather than patch the old one
in place — the auth mechanism changed completely (Firebase vs. Pod Point's own token) and enough
of the data model shifted that a mechanical port didn't seem worth forcing.

## What's actually proven right now

Live-tested, through the real shipped `podpoint_mobile_api` client (via a private local test
script, not itself part of the shipped repo):

- Firebase email/password sign-in + token caching, against your real account.
- `GET /chargers`, `/chargers/{ppid}/connectivity-status-v2`, `/charges`, `/chargers/{ppid}/tariffs`.
- Also confirmed: `/chargers/{ppid}/manual-schedules`, `/chargers/{ppid}/security-logs`,
  `/chargers/{ppid}/charge-statistics`, `/charges/stats`.

**Now run inside a real HA instance (HAOS)** for the first time: config flow completed live,
one device created with all 8 entities visible and populated with real values. One real bug
found and fixed this way - `Cost This Month` used `state_class: total_increasing`, which HA
core rejects on `device_class: monetary` (only ENERGY-like classes allow it); switched to
`total` with an explicit `last_reset`. Device naming was also corrected from the bare serial to
a humanized model name ("Solo 3"), with the serial moved to `DeviceInfo.serial_number`. Not yet
covered by that pass: reauth flow, diagnostics download contents, log behavior under a forced
failure, dynamic-device creation (still only one charger on the account), or a full
month/midnight rollover of the Energy Dashboard sensors - those remain open, see below.

Not touched at all: `charge-overrides` (charge now) and `remote-lock` (cable lock) — both
write, both have real physical effects, both need you present at the charger.

## Open decisions — resolved

1. **Scope for v1**: read-only first. Write endpoints are a separate, later phase.
2. **Generality**: design for other accounts/hardware now (HACS/core intent), not just this
   one Solo 3.
3. **Distribution**: HACS now, core not ruled out eventually — driving the QUALITY_SCALE.md work.
4. **Lifetime-total problem**: solved differently than any of the original three options —
   Energy Dashboard-safe **month-to-date** sensors (`state_class: total_increasing`, resets
   naturally each calendar month) rather than a true lifetime figure, which the API doesn't
   offer. Currency sourced from a dedicated `GET /users` call (confirmed live: returns
   `balance: {currency, amount}`), fetched once and cached on the coordinator rather than
   re-derived every poll.
5. **Cable Status heuristic**: resolved — verified live against a real plugged-in session, found
   wrong (see below), and replaced with a `chargingState`-derived signal instead.

## Phase 1 done: quality-scale fixes + generality + Energy Dashboard sensors

See [`QUALITY_SCALE.md`](QUALITY_SCALE.md) for the itemized rule-by-rule status. Summary of
what landed this pass:

- `runtime_data` migration, `PARALLEL_UPDATES`, `diagnostics.py` (never touches live tokens),
  deduped non-fatal logging.
- Unconfirmed `chargingState` values now degrade safely (no HA-core error spam) with the raw
  value surfaced as an extra attribute instead of silently dropped.
- Dynamic devices implemented (a charger added to the account gets entities without an HA
  restart) — code-reviewed only, no second charger available to verify live.
- Two new sensors: **Energy This Month** / **Cost This Month**, dashboard-safe, replacing the
  lifetime-total gap. **Last Charge Energy/Cost stay as session snapshots — do not add those to
  the Energy Dashboard**, only the *This Month* pair.
- Two live pre-checks done via `scratch/smoke_test_api.py` before any of this was built: a zero-activity
  same-day `charge-statistics` range (clean 200 with zeros, no 500 - so a fresh month starts
  clean too) and `GET /users` (confirmed the `balance.currency` shape).

**Not yet done: the real-HA validation pass.** Nothing above the API layer (coordinator,
entities, config flow, the new diagnostics/dynamic-devices code) has ever actually run inside
Home Assistant. That's the next real milestone, not a fifth Phase-1 sub-item — see PLAN's
original Phase 1 framing below, which still applies in full.

**Temporary state, to unblock that pass on HAOS:** `custom_components/pod_home/podpoint_mobile_api/`
is a vendored (copied-in) duplicate of `podpoint-mobile-api/src/podpoint_mobile_api/`, imported
via relative imports (`from .podpoint_mobile_api import ...`) instead of as an installed
dependency — `manifest.json`'s `requirements` stays empty. This sidesteps relying on a
`git+https` requirements URL, which has had real, recent breakage in Home Assistant Core
(requirement-parsing changes and a broader custom-integration requirements-not-importable
regression seen across 2026 releases) that's riskiest on HAOS specifically, where there's no
easy manual `pip install` fallback. **Undo this before any real release**: delete the vendored
copy, revert the three `from .podpoint_mobile_api import ...` lines (in `__init__.py`,
`coordinator.py`, `config_flow.py`) back to `from podpoint_mobile_api import ...`, and populate
`manifest.json`'s `requirements` once `podpoint-mobile-api` has an actual PyPI (or otherwise
reliably installable) release. Until then, the two copies can silently drift — don't edit
`custom_components/pod_home/podpoint_mobile_api/` directly; change
`podpoint-mobile-api/src/podpoint_mobile_api/` and re-copy.

## Remaining phased shape

- **Real-HA validation** - substantially underway, not just "next" anymore. Installed and
  running against a real HA instance across many live sessions since: config flow, coordinator,
  entities, and multiple real write endpoints have all been exercised live (see below), plus
  several real bugs found and fixed only because of that (api3 pod-id mapping, diagnostics
  `AttributeError`, auth-token-persistence-on-restart, and others - see DECISIONS.md). Of the four
  gaps this section used to list: **diagnostics download contents - confirmed live** (a real download
  inspected: `vehicle`/`firmware.serial_number` correctly `**REDACTED**`, no `entry.data`/
  credentials anywhere); **Energy This Month/Cost This Month across a midnight/month rollover -
  confirmed live** (observed resetting correctly); **reauth flow - confirmed live**, after a real
  bug: the Repair issue fired correctly (HA core creates it automatically, no code needed there),
  but a new password was accepted then immediately re-failed - root cause was `__init__.py`
  restoring the *old* (pre-password-change) Firebase refresh token from its persisted Store on
  the post-reauth reload, silently trying to reuse the invalidated session instead of signing in
  fresh. Fixed (`config_flow.py` now clears that Store on a successful reauth) and **the fix is
  confirmed working live** - reauth now sticks. See DECISIONS.md and closed
  [jeverley/ha-pod-home#1](https://github.com/jeverley/ha-pod-home/issues/1). **Still open**: log
  behavior under a forced failure - wasn't explicitly confirmed as part of the reauth retest.
- **Cable Status: done** - verified live, fixed, `chargingState`-derived now.
- **Charge Mode select (Basic ⇄ Smart Charging switch) - deliberately still not built.**
  `delegatedControl.status` is read and surfaced (Charging Mode sensor); the write endpoint is
  confirmed via the account's public OpenAPI schema (`PATCH /smart-charging/delegated-controls/
  {ppid}`, body `{ status: "ACTIVE" | "INACTIVE" }`) but not called. Not just the usual
  write-endpoint discipline this time - the user has said mode switching is deliberately done
  from the app, not HA, so this select isn't planned at all going forward, not merely deferred.
  Don't confuse this with the *new* Charge Priority select below, a different entity for a
  different field (`chargingStrategy`, not `delegatedControl.status`).
- **Ready By / Target Charge as settable number/time entities - built** (`number.py`, `time.py`),
  replacing the earlier read-only sensors. Target Charge: `PATCH /smart-charging/delegated-
  controls/{ppid}/vehicles/{vehicleId}`, body `{ vehicle: { chargeState: { chargeLimitPercent }
  } }` - a direct vehicle-level percentage, no conversion needed (corrected from an earlier
  version of this entity that wrongly routed it through the intents endpoint below - see
  DECISIONS.md). Ready By: `PUT /smart-charging/delegated-controls/{ppid}/vehicles/{vehicleId}/
  intents`, body `{ intentDetails: [{ dayOfWeek, chargeByTime, chargeKWh }, ...] }`, fanned
  identically across all 7 days per write, `chargeKWh` echoed back from the last-read value
  rather than recomputed (also corrected - see DECISIONS.md). **Both endpoints confirmed working
  live** - the user has tested Target Charge and Ready By writes against the real account and
  confirmed they land correctly.
- **Charge Priority select - built** (`select.py`) and **confirmed working live** - an initial
  write theory (`chargingStrategy`) turned out to have no observed effect on a real write test;
  fixed to write `maxPrice` directly instead, then reconfirmed live ("That works now." - see
  DECISIONS.md). Not mode-gated - confirmed via the app that it's always viewable/changeable
  regardless of Charging Mode (corrected after an initial wrong assumption - see DECISIONS.md).
  Also built: mode-conditional entities (Ready By/Target Charge/Expected Charge/Electricity Rate
  disabled via the entity registry outside Smart Charging mode) and a unified `Schedule` calendar
  (`calendar.py`, mirrors the `Schedule` sensor - one entity adapting to whichever mode is
  active, not two mode-specific ones). See DECISIONS.md for all three.
- **`charge-overrides` (boost) - built and confirmed working live** (`button.py`: Boost full
  charge/Boost for duration/Cancel boost; `time.py`: Boost duration, a local-only hh:mm input,
  see DECISIONS.md). Matches the app's own two boost options plus a cancel action, per the user
  directly. **All three confirmed working live**: Boost for duration worked first try; Boost
  full charge's flat-12h-`endAt` fix and Cancel boost have both since been confirmed live too.
- **`remote-lock`**: still completely untouched - no read or write entities built. **Deliberately
  deprioritized, not just deferred** - per the user directly, their own charger doesn't support
  it, so there's no way to test it live even if built. Revisit only if that changes.
- **Packaging - done for now**: `hacs.json`, LICENSE, `.github/workflows/validate.yml`
  (`hacs/action` + `hassfest`), README.md rewritten as end-user documentation. Installable today
  via HACS as a custom repository (Integrations → ⋮ → Custom repositories). `validate.yml`'s
  `hacs` job still fails - confirmed via `gh run view` that GitHub topics are genuinely set
  correctly, but HACS's validator can't read topics/manifest content on a **private** repo at
  all, so `hacsjson`/`integration_manifest`/`topics` all fail regardless of content. Going public
  is the only fix, and that's the user's call - repo is staying private for now. Not yet on
  HACS's default repository list and no tagged release yet either - both still premature while
  the integration is actively changing shape.
