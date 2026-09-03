# Working conventions for this repo

Home Assistant custom integration (`custom_components/pod_home/`, domain `pod_home`) for Pod
Point EV chargers, targeting the `mobile-api.pod-point.com` backend used by the "Pod Home" app
(Firebase auth) rather than the legacy `api.pod-point.com/v4` API the community `pod_point`
integration uses. A clean rewrite, not a patch to that older integration.

Status/roadmap live in [`PLAN.md`](PLAN.md). Check that before assuming what phase of work this
is in.

## Git workflow

Work on a feature branch, not directly on `master` - `git checkout -b <branch>`, commit normally
there (this repo's own commit-message conventions still apply per-commit on the branch), then
`git checkout master && git merge --squash <branch>` and commit once with a message describing
the whole feature, matching how `master`'s own history already reads (each commit there is one
coherent chapter, not a blow-by-blow). No GitHub PR - local squash-merge only, since this is a
solo-maintained repo and CI already runs on every push to `master`. Delete the branch after
merging - `git branch -D <branch>`, not `-d`: a squash-merge's commit is a new SHA, not an
ancestor of the branch tip, so git always sees a squashed branch as "not fully merged". Don't
commit directly to `master` for anything beyond a genuinely one-line, self-contained change (a
doc typo, a single CI-config tweak).

**Every push to `master` runs both CI workflows** (`test.yml` + `validate.yml`) - real minutes,
not free. A sequence like "fix a bug in X" → "redesign X" → "delete X entirely" pushed as three
separate commits triggers three full CI runs for work that reads, in hindsight, as one change -
and leaves a "fix" commit in the permanent history for a mechanism that no longer exists by the
next commit. Batch related work on one branch and squash-merge it as a single push, the same way
`master`'s own history already reads one coherent chapter per commit - don't push-per-step out of
habit just because each individual step compiles and passes lint.

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

`.github/workflows/test.yml` runs everything below on every push/PR (Linux runners) - **that's
the authoritative place `pytest tests/` is verified**, not this dev machine (Windows) - see the
`pytest tests/` bullet below for why. What's actually checkable:

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
- `pytest tests/` — the whole suite runs as one unified pytest session (`tests/conftest.py`
  registers `pytest-homeassistant-custom-component` for everything, no offline/HA-dependent
  split). 159 tests: `tests/test_translation_keys.py`, `tests/test_helpers.py`,
  `tests/test_config_flow.py` (user flow, duplicate-email abort, reauth), `tests/
  test_coordinator.py` (fetch/parse/staleness/error-handling logic against a mocked
  `PodHomeApiClient`), and one file per entity platform (`test_sensor.py`/`test_binary_sensor.py`/
  `test_number.py`/`test_select.py`/`test_time.py`/`test_calendar.py`/`test_button.py`/
  `test_update.py`), using the shared dataclass factories in `tests/_fixtures.py`. `pip install -r
  requirements_test.txt` first (dev/test-only, never referenced by `manifest.json`). Always use
  `python -m pytest tests/`, not bare `pytest` - only `-m` puts the repo root on `sys.path`,
  needed for `custom_components` to import. An entity constructed directly for a test (not added
  through a real platform) never gets `self.hass` set automatically - assign it manually
  (`entity.hass = hass`) before calling anything that needs it (see test_button.py). **On this
  dev machine (Windows), the full suite is currently broken** - Windows' `asyncio.ProactorEventLoop`
  needs a real loopback socket just to construct itself, which the plugin's socket-blocking
  intercepts, breaking every test in the session including the plain synchronous ones (confirmed
  local repro: 96 errors). This doesn't happen on Linux (CI). Known, diagnosed, not being chased
  further locally - `.github/workflows/test.yml` is what actually gates whether the suite passes;
  treat a local Windows failure here as expected, not a regression, and check the GitHub Actions
  run instead. `async_get_clientsession` is mocked in the config-flow tests regardless of
  platform - architecturally correct either way, not a Windows workaround. **Still not covered**:
  `async_setup_entry`'s real first refresh (config flow tests mock it; coordinator tests bypass
  it), the mode/tariff-gating reconciliation functions in entity.py, and dynamic-device creation -
  see QUALITY_SCALE.md for the honest remaining list.

Unit-tested (mocked API responses, real `hass` fixture) is not the same claim as **live-verified**
against a real Home Assistant instance and a real account - the two are tracked separately in
this file and DECISIONS.md/PLAN.md. Don't claim something works live just because it's covered by
a test with mocked data; say plainly which kind of verification a given piece of code actually
has.
