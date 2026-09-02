# Quality scale checklist for pod_home

**Target tier: platinum.** Home Assistant's [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
is 54 individual rules across four tiers (bronze → silver → gold → platinum). HACS doesn't
require any tier, but since core isn't ruled out, this is a real goal, not passive tracking - so
nothing has to be retrofitted later. Rule list confirmed against the live dev docs (2026.8) at
[`developers.home-assistant.io/docs/core/integration-quality-scale/rules/`](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/).
This is the one place quality-scale status is tracked - keep it that way rather than letting a
second doc grow alongside it (see DECISIONS.md for the PLATINUM_COMPARISON.md merge history).

## Already compliant (mostly by accident of following normal HA conventions)

- **inject-websession** — `async_get_clientsession(hass)` is already used, never a private session.
- **common-modules** — already have `coordinator.py` (extends `DataUpdateCoordinator`) and
  `entity.py` (extends `CoordinatorEntity`).
- **has-entity-name** — `_attr_has_entity_name = True` set on the base entity.
- **entity-unique-id** — every entity has a stable, domain-scoped unique_id.
- **config-flow** — UI setup via `config_flow.py` + `strings.json`, matches the expected shape.
- **test-before-configure** — the config flow calls `auth.async_get_id_token()` before creating
  the entry, so bad credentials fail in the flow, not after.
- **unique-config-entry** — `async_set_unique_id(email)` + `_abort_if_unique_id_configured()`.
- **reauthentication-flow** — `async_step_reauth`/`async_step_reauth_confirm` implemented.
- **test-before-setup** — `_async_update_data` raises `ConfigEntryAuthFailed` on auth failure,
  which HA routes into the reauth flow automatically; `UpdateFailed` on transient API errors.

## Closed this phase (see PLAN.md for the phase writeup)

- **runtime-data** — ✅ `type PodHomeConfigEntry = ConfigEntry[PodHomeDataUpdateCoordinator]` in
  `__init__.py`, `entry.runtime_data` used throughout (`__init__.py`, `coordinator.py`,
  `sensor.py`, `binary_sensor.py`, `diagnostics.py`). Verified zero remaining `hass.data[DOMAIN]`
  references (grep, code only - a comment in `__init__.py` mentions the old pattern by name).
- **parallel-updates** — ✅ `PARALLEL_UPDATES = 0` added to `sensor.py`/`binary_sensor.py`.
- **diagnostics** — ✅ `diagnostics.py` added. Deliberately never touches
  `entry.runtime_data.api`/`.api._auth` at all (rather than redacting after the fact) - the
  coordinator's `PodHomeCharger`/`PodHomeCharge` dataclasses contain no token fields, so only
  those plus `entry.data` (redacted) are exposed.
- **log-when-unavailable** — ✅ coordinator now has `_warn_once`/`_clear_warning` dedup (warn
  once, drop to debug on repeats, info on recovery), applied to every non-fatal call site
  (`async_charges`, per-charger `async_connectivity_status`/`async_charge_statistics`) and to
  unrecognized `chargingState` values.
- **dynamic-devices** — ✅ implemented (was originally deferred, revisited and done this phase
  per explicit request). `entity.py`'s `async_setup_dynamic_chargers` diffs `coordinator.data`
  against known ppids on every update via `coordinator.async_add_listener`, shared by both
  platform files. Code-reviewed only, not end-to-end verified - no second charger available to
  test against.

- **docs-installation-parameters, docs-known-limitations** — ✅ README.md rewritten as end-user
  documentation: HACS custom-repository/manual installation, setup, a full entity list, Energy
  Dashboard/Charging Mode notes, a "Known limitations" section covering boost latency, the
  Firmware version-string caveat, and remote-lock's absence.
- **HACS packaging** — ✅ `hacs.json` added (minimal: name + render_readme), `.github/workflows/
  validate.yml` added (`hacs/action` + `hassfest`, matching HACS's own default-repo validation).
  LICENSE already existed. **Still open**: no tagged GitHub release yet - HACS can install
  straight from the default branch via a custom repository, but versioned upgrades need at least
  one release/tag; not done yet since the integration is still actively changing shape.
- **config-flow-test-coverage** — ✅ `tests/test_config_flow.py`, using a real
  `pytest-homeassistant-custom-component` harness (installed as a dev dependency this phase, run
  as part of the unified `tests/` suite - see CLAUDE.md's Verification section). Covers the
  user flow (success, invalid auth), duplicate-email abort (including case-insensitivity, since
  `async_set_unique_id` lowercases), and the reauth flow (success updates the stored password,
  invalid auth shows an error and leaves it unchanged). **Still open**: this is config_flow.py
  only - `async_setup_entry` itself (the coordinator's first refresh, real or mocked) isn't
  exercised by these tests, and remains part of the broader `test-coverage` gap above.

## Also addressed this phase, beyond the original four

- **Unconfirmed enum values (real correctness issue, not in the original list)** — an
  unrecognized `chargingState` fed straight to a `SensorDeviceClass.ENUM` entity would make HA
  core itself log an error on every state read. `helpers.known_or_none` + `PodHomeStatusSensor`
  now falls back to `None` with the raw value surfaced as an extra attribute instead.

## Deferred - real work, not needed for a HACS-quality v1

- **test-coverage** — 95% coverage requirement, coordinator + entities. `tests/test_helpers.py`
  (90 cases, offline) covers every pure function `coordinator.py`/`sensor.py` etc. lean on, but
  the coordinator and entity classes themselves (the `DataUpdateCoordinator` subclass, every
  platform's entity classes) still have zero direct test coverage - needs
  `pytest-homeassistant-custom-component` with mocked API responses, a real HA test harness.
  Real, substantial work, not started. Still the single biggest gap for a core submission; not
  blocking for personal/HACS use.
- **strict-typing** — full PEP-561 typing + a `py.typed` marker + entry in core's
  `.strict-typing` file. The code is already reasonably typed (`from __future__ import
  annotations`, most signatures annotated) but hasn't been audited against `mypy --strict`.
- **action-setup** — once charge-now/remote-lock exist, register services in `async_setup`
  (hass-level), not `async_setup_entry`, and validate the target entry inside the handler with
  `ServiceValidationError` rather than gating registration on entry state. The *old*
  `pod_point` integration already does this correctly (`services.py`) - just needs carrying
  forward when write endpoints land.
- **repair-issues, brands, discovery** — repair-issues is a nice-to-have; brands only matters
  for a core PR (a logo submitted to `home-assistant/brands`); discovery doesn't apply, this is
  a cloud API with nothing to discover on the local network.
- **docs-supported-devices, docs-troubleshooting, docs-data-update, docs-use-cases,
  docs-examples, docs-configuration-parameters, docs-removal-instructions** — a real device
  compatibility list (only ever tested against one Solo 3), troubleshooting steps, etc. Not
  urgent while this is a single-account personal integration rather than something with a wider
  user base filing real support requests. See "Closed this phase" below for the docs/packaging
  work that has landed.

## Not applicable

- **discovery / discovery-update-info** — no local network presence to discover, it's a cloud API.

## Corrected: async-dependency / dependency-transparency were wrongly marked N/A

Originally dismissed here as "no external PyPI dependency, nothing to vet" - technically true at
the time, but the wrong conclusion. Neither rule literally mandates extracting the API client
into a separate library (`dependency-transparency`'s four bullets - OSI license, on PyPI, built
from public CI, PyPI version matches a tagged release - just describe properties *any* external
dependency must have), but both rules only have something to check when an external dependency
exists. Not having one doesn't satisfy them, it just keeps the integration off platinum entirely
- confirmed by checking Ohme's actual manifest.json (`"requirements": ["ohme==1.9.1"]`), a real
standalone PyPI package maintained by the same person as the integration. See the platinum
comparison below for the extraction now underway to match that pattern.

## Platinum comparison against Ohme and Peblar

Ohme (`homeassistant/components/ohme`) is the closer analog - UK, cloud-polling, email/password
Firebase-adjacent auth, smart EV charging. Peblar (`homeassistant/components/peblar`) is
local-polling (LAN/direct-to-charger), so its coordinator/auth patterns don't transfer, but its
entity design is still a useful reference. Compared against pod_home's current source, not just
the abstract rule list above.

### Real gaps found

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
   coordinator.py's FAST/SLOW_POLL_INTERVAL), and several per-resource fetches now have their
   own staleness/activity-aware cadence too (vehicle data, month stats, firmware/tariffs).
   Ohme still goes further with two separate coordinators sharing a common abstract base
   (`OhmeChargeSessionCoordinator`, 30s; `OhmeDeviceInfoCoordinator`, 30min), bundled in a small
   `OhmeRuntimeData` dataclass as `entry.runtime_data` - a real, separate architectural
   improvement over per-resource staleness gating within one coordinator.

4. ✅ **Fixed.** ~~Diagnostics could be simpler~~ - `diagnostics.py` now matches Ohme's pattern:
   `entry.data` (email/password) is left out entirely rather than included-then-redacted.

5. **Real `quality_scale.yaml` convention.** HA core's actual mechanism for declaring
   per-rule status is a `quality_scale.yaml` file *inside* the integration's own directory
   (`status: done` / `status: exempt` + a comment, per rule) - not a top-level markdown doc.
   This file is a reasonable stand-in for now; low urgency since HACS doesn't require it, but
   worth knowing the real target format for whenever core submission is a real plan.

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
   integration). This is also what `async-dependency`/`dependency-transparency` above were
   actually pointing at. Not yet published anywhere pip-installable (no PyPI/tagged-git
   release), so `manifest.json`'s `requirements` still can't reference it for real - that's the
   next step in this specific item, not a full close-out.

### Not a gap - a genuine API constraint, not a design mistake

Peblar ships a native `energy_total` sensor (`TOTAL_INCREASING`, Wh) because its charger
firmware tracks a true lifetime counter on-device. Ohme has *no* native energy sensor at all -
docs explicitly say to build one yourself via HA's Integral helper over its Power sensor, because
Ohme's API doesn't expose a cumulative counter either. Pod Point's API exposes neither a live
power reading nor a lifetime total (confirmed - only date-range aggregation), so pod_home's Month
Energy / Month Cost (monthly-reset, `TOTAL_INCREASING` for energy / `TOTAL`+`last_reset` for cost
- HA core doesn't allow `TOTAL_INCREASING` on `monetary`) is a third reasonable answer to the
same underlying constraint, not something to "fix" to match either of these.

## Recommendation

Tests and strict typing remain a deliberate, scoped chunk of work for whenever core submission
actually becomes a real plan rather than a maybe - trying to hit 95% coverage on a shape that's
still changing (write endpoints not even started yet) would mean rewriting a lot of those tests
anyway. Next real milestone is the first real-HA validation pass (see PLAN.md) - nothing above
the API layer has ever run inside actual Home Assistant yet.

Platinum comparison items 1, 4, and 8 above are done. Items 2 and 3 are real architecture work (a
second coordinator, a new config flow step) worth a deliberate decision on scope/timing rather
than doing reflexively. Items 5-7 are gold-tier polish, fine to defer alongside the rest of this
file's deferred list.
