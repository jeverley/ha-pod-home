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

Live-tested, through the real shipped code (`smoke_test_api.py`):

- Firebase email/password sign-in + token caching, against your real account.
- `GET /chargers`, `/chargers/{ppid}/connectivity-status-v2`, `/charges`, `/chargers/{ppid}/tariffs`.
- Also confirmed: `/chargers/{ppid}/manual-schedules`, `/chargers/{ppid}/security-logs`,
  `/chargers/{ppid}/charge-statistics`, `/charges/stats`.

Built but **not yet run inside Home Assistant at all** — coordinator, entities, config flow are
untested beyond compiling.

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
5. **Cable Status heuristic**: still unverified against a real plugged-in session — this one's
   still open, genuinely can't be resolved without you actually plugging in and watching it.

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
- Two live pre-checks done via `smoke_test_api.py` before any of this was built: a zero-activity
  same-day `charge-statistics` range (clean 200 with zeros, no 500 - so a fresh month starts
  clean too) and `GET /users` (confirmed the `balance.currency` shape).

**Not yet done: the real-HA validation pass.** Nothing above the API layer (coordinator,
entities, config flow, the new diagnostics/dynamic-devices code) has ever actually run inside
Home Assistant. That's the next real milestone, not a fifth Phase-1 sub-item — see PLAN's
original Phase 1 framing below, which still applies in full.

## Remaining phased shape

- **Real-HA validation** (next). Install `custom_components/pod_home/` in an actual HA
  instance, run the config flow for real, confirm entities/coordinator/device registry behave,
  including: reauth flow, diagnostics ZIP contents, log behavior under a forced failure, and
  Energy This Month/Cost This Month in the Energy Dashboard across a midnight/month rollover.
- **Close remaining read-side gaps**: verify Cable Status against a real plugged-in session;
  decide fate of Charge Mode/Smart Charging (read-only first — just surface
  `delegatedControl.status` — before attempting to write it).
- **Write endpoints**: `charge-overrides` and `remote-lock`, live-tested at the charger, then
  wired to services/switches.
- **Packaging**: `hacs.json`, LICENSE, `.github/`, git repo — sized for HACS distribution.
