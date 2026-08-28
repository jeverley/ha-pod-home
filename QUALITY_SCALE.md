# Quality scale checklist for pod_home

Home Assistant's [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
is 54 individual rules across four tiers (bronze → silver → gold → platinum). HACS doesn't
require any tier, but since core isn't ruled out, this tracks the current scaffold against it
so nothing has to be retrofitted later. Rule list confirmed against the live dev docs
(2026.8) at [`developers.home-assistant.io/docs/core/integration-quality-scale/rules/`](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/).

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

## Also addressed this phase, beyond the original four

- **Unconfirmed enum values (real correctness issue, not in the original list)** — an
  unrecognized `chargingState` fed straight to a `SensorDeviceClass.ENUM` entity would make HA
  core itself log an error on every state read. `helpers.known_or_none` + `PodHomeStatusSensor`
  now falls back to `None` with the raw value surfaced as an extra attribute instead.

## Deferred - real work, not needed for a HACS-quality v1

- **test-coverage / config-flow-test-coverage** — 95%/100% coverage requirements. No `tests/`
  directory exists yet. This is the single biggest gap for a core submission and genuinely
  substantial work (fixtures, mocked API responses, `pytest-homeassistant-custom-component`).
  Worth doing before ever proposing this for core; not blocking for personal/HACS use.
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
- **docs-* rules** (installation instructions, supported devices, troubleshooting, known
  limitations, etc.) — the current README is dev-notes, not end-user documentation. HACS shows
  the README as the integration's info page, so this matters for HACS too, just not urgent
  while the integration itself is still changing shape.

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
standalone PyPI package maintained by the same person as the integration. See
`PLATINUM_COMPARISON.md` for the extraction now underway to match that pattern.

## Recommendation

Tests and strict typing remain a deliberate, scoped chunk of work for whenever core submission
actually becomes a real plan rather than a maybe - trying to hit 95% coverage on a shape that's
still changing (write endpoints not even started yet) would mean rewriting a lot of those tests
anyway. Next real milestone is the first real-HA validation pass (see PLAN.md) - nothing above
the API layer has ever run inside actual Home Assistant yet.
