# Working conventions for this repo

Home Assistant custom integration (`custom_components/pod_home/`, domain `pod_home`) for Pod
Point EV chargers, targeting the `mobile-api.pod-point.com` backend used by the "Pod Home" app
(Firebase auth) rather than the legacy `api.pod-point.com/v4` API the community `pod_point`
integration uses. A clean rewrite, not a patch to that older integration.

Status/roadmap live in [`PLAN.md`](PLAN.md). Check that before assuming what phase of work this
is in.

## Quality scale

**Target tier: platinum**, not passive compliance tracking. [`QUALITY_SCALE.md`](QUALITY_SCALE.md)
is the one place Home Assistant Integration Quality Scale status lives (abstract 54-rule
checklist plus a concrete comparison against real platinum-tier integrations) - don't let a
second doc grow alongside it. Check a change against it when it plausibly touches a rule (entity
availability, diagnostics, config flow, translations, etc.), and cross off/update an item there
once it's genuinely done - an item marked done that silently regressed is worse than one honestly
still open. `QUALITY_SCALE.md`'s rule list and recommendations were confirmed against the live
[HA dev docs](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/)
at a point in time (currently 2026.8, noted in the file) - re-check periodically rather than
treating that snapshot as permanently authoritative, since HA's own rule set and guidance evolve.

## Repo layout

- `custom_components/pod_home/` — the HA integration itself (coordinator, entities, config
  flow). Everything here should be able to ship in a public repo as-is.
- `podpoint-mobile-api/` — the standalone async API client (Firebase auth + mobile-api.pod-point.com
  endpoints), extracted into its own installable package with zero Home Assistant dependency -
  same pattern platinum-tier integrations with a real API surface use (e.g. `ohme` on PyPI).
  `custom_components/pod_home/` depends on it via a normal `import podpoint_mobile_api`. Install
  locally with `pip install -e podpoint-mobile-api` before running anything that imports it.
  Not yet published anywhere pip-installable (no PyPI/tagged-git release), so `manifest.json`'s
  `requirements` deliberately doesn't reference it yet - adding a requirements entry HA can't
  actually resolve would break the integration loading for real. `custom_components/pod_home/
  podpoint_mobile_api/` is a vendored copy of this package (temporary, see PLAN.md) - never edit
  it directly; edit `podpoint-mobile-api/src/podpoint_mobile_api/` and re-copy, then `diff` both
  copies of every changed file to confirm they still match.
- `README.md` / `PLAN.md` / `QUALITY_SCALE.md` — living project docs, part of the repo.
- `scratch/` — gitignored, everything here is local-only and may or may not exist on a given
  checkout: captured live-account API output, an installed app package used for API research,
  research notes, a throwaway PoC client, and the dev/test scripts (`smoke_test_api.py`,
  `check_interval_probe.py`, and any one-off probe script) that exercise the real, installed
  `podpoint_mobile_api` package against a live account (see "Credentials" below) - kept private
  rather than shipped, deliberately. Never reference anything under `scratch/` as if it ships
  with the repo, and never move anything out of `scratch/` into the tracked tree without
  checking it doesn't contain real account data.

## Documentation style

Docs and code comments describe **the API itself** — confirmed behavior, endpoint shapes, field
names, confirmed vs. best-guess — not the investigation that produced that description. Keep the
same scope here in this file: describe the target (the API), not the process.

Prefer `None`/unknown over a plausible-looking guessed value. Confirm live before relying on an
assumption about API behavior, and mark anything unconfirmed clearly — in the code comment
tersely, in [`DECISIONS.md`](DECISIONS.md) with the full reasoning.

### Comments and docstrings

Terse by default, per Home Assistant's own convention ([development guidelines](https://developers.home-assistant.io/docs/development_guidelines)):
a one-line docstring for most things; Google-style `Args`/`Returns`/`Raises` only when genuinely
needed beyond what type hints already say. Full reasoning, decision history, and "why" belong in
[`DECISIONS.md`](DECISIONS.md), not inline — a comment states the load-bearing fact a future
editor needs and, if there's more to it, points there. No "per the user directly"/"confirmed
live" narrative framing in code comments; that's DECISIONS.md's job. DECISIONS.md itself is
append-only — add new entries, never rewrite or delete an old one, even once outdated; a later
entry documenting a correction is the correction.

### Entity states and translations

A `device_class: enum` sensor's (or a `select`'s) raw state must be a stable snake_case key, with
the display text in `strings.json`/`translations/en.json`'s per-entity `state` block ([HA's
i18n convention](https://developers.home-assistant.io/docs/internationalization/core)) - not
baked into the raw value itself. Applies to this integration's own derived vocabulary (Status,
Charging Mode, Charge Priority). Exception: raw wire-passthrough debug sensors (Charging state,
Power delivery state) stay untranslated, exactly matching the API's own field values - deliberate,
not an oversight. Keep `strings.json` and `translations/en.json` byte-identical (`diff` them
after editing either) - `strings.json` is the dev-time source, `translations/en.json` is what HA
actually loads.

### Entity availability

`unavailable` means "can't fetch data"; `unknown` means "fetched fine, no applicable value right
now" ([entity-unavailable](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/entity-unavailable)).
Don't use `unavailable` for a value that's merely inapplicable in the current state (e.g. a
vehicle sensor while the car's unplugged) — that's `unknown`.

### Classification/mapping completeness

When a dict or table classifies every value of an existing options list (e.g.
`CHARGING_STATE_CABLE_CONNECTED` classifying every `CHARGING_STATE_OPTIONS` value), add
`assert set(mapping) == set(options_list)` at import time. A newly-added option that isn't also
classified should fail loudly on load, not silently fall through to a default.

### No blocking I/O

Everything in this integration runs in HA's event loop or the standalone async client — no
`requests`, no blocking file/network I/O anywhere in `custom_components/pod_home/` or
`podpoint-mobile-api/`. `aiohttp` throughout, matching what's already there.

## New platform checklist

Adding a new platform file (e.g. `button.py`/`switch.py` for the write endpoints below) needs,
matching every existing platform module:

- `PARALLEL_UPDATES = 0`
- Entities built on the shared base classes in `entity.py` (`PodHomeEntity`/`PodHomeVehicleEntity`/
  `PodHomeAccountEntity`), using their `translation_key`/`unique_id` pattern
- A matching entry in both `strings.json` and `translations/en.json` (see "Entity states and
  translations" above for keeping them in sync)
- The new `Platform.*` registered in `__init__.py`'s `PLATFORMS` list

## Credentials

Never handle the user's Pod Point password directly — no typing it into a command, no reading
it from a message and relaying it into a tool call. Test scripts (all under `scratch/`) prompt
for it themselves via `getpass` (hidden, not logged) and are run by the user in their own
terminal. If a script's output is needed, read the files it saves rather than asking the user to
paste credentials.

## Write endpoints

`charge-overrides` (charge now) and `remote-lock` (cable lock) have real physical effects on a
real charger. Do not implement, call, or test these without the user explicitly asking for that
specific action, live, knowing what it'll do.

More generally: any write-capable entity (Ready By, Target Charge, and Charge Priority all went
through this) stays flagged "NOT YET TESTED against a real account" in its docstring until the
user has explicitly confirmed the write lands correctly live — not just these two named
physical-effect endpoints. Don't mark a write as working from code review or offline reasoning
alone.

## Verification

No Home Assistant install exists in this environment. What's actually checkable:

- `python -m py_compile` + `python -m pyflakes` across `custom_components/pod_home/` — syntax
  and unused-import/undefined-name checks only, does not import `homeassistant`.
- `podpoint-mobile-api/` has zero HA dependency, so it's fully import-testable, not just
  compile-checkable - `python -c "import podpoint_mobile_api"` (once `pip install -e
  podpoint-mobile-api` has been run) is a real check, not just a syntax one.
- `python scratch/smoke_test_api.py` — the one thing that exercises real API *behavior*, run by
  the user (see "Credentials"). Only covers `podpoint_mobile_api`, not the coordinator,
  entities, or config flow.
- `helpers.py` has no HA import, so its pure functions (`charger_status`, `select_last_charge`,
  `cumulative_charging_seconds`, etc.) are exercisable offline with hand-built fixture data - no
  real HA instance or live account needed. `tests/test_helpers.py` now covers every function this
  way (see `tests/_pod_home_loader.py` for how it imports helpers.py/const.py without Home
  Assistant installed - reuse it rather than re-solving the relative-import problem). This is the
  main way to check coordinator/entity-level logic beyond compiling, when adding new pure
  functions or extending existing ones.
- `pytest tests/` — real test coverage of two kinds so far: `tests/test_translation_keys.py`
  (cross-checks `strings.json`/`translations/en.json`'s `state` blocks against the const.py
  `OPTIONS` lists they translate, see "Entity states and translations" above) and
  `tests/test_helpers.py` (every `helpers.py` function, offline). **Still not covered**: the
  deliberate full test-coverage pass QUALITY_SCALE.md still defers - the coordinator, entities,
  and config flow, which need `pytest-homeassistant-custom-component` (mocked API responses, a
  real HA test harness), not started yet. Run `pytest tests/` after touching `helpers.py` or any
  `CHARGER_STATUS_*`/`SCHEDULE_MODE_*`/`CHARGE_PRIORITY_*` constant or its translation entries.

Everything above the API layer (`coordinator.py`, `entity.py`, `sensor.py`, `binary_sensor.py`,
`config_flow.py`, `diagnostics.py`) is unverified beyond compiling and the offline `helpers.py`
checks above until it's actually run inside a real Home Assistant instance — don't claim it
works, say that plainly instead.
