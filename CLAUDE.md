# Working conventions for this repo

Home Assistant custom integration (`custom_components/pod_home/`, domain `pod_home`) for Pod
Point EV chargers, targeting the `mobile-api.pod-point.com` backend used by the "Pod Home" app
(Firebase auth) rather than the legacy `api.pod-point.com/v4` API the community `pod_point`
integration uses. A clean rewrite, not a patch to that older integration.

## Git workflow

Feature branch (`git checkout -b <branch>`), commit normally there, then squash-merge to
`master`: `git checkout master && git merge --squash <branch>` + one commit describing the whole
feature (matching `master`'s own one-coherent-chapter-per-commit history) - no GitHub PR, solo
repo. Delete the branch with `-D` after (`-d` refuses - a squash-merge commit isn't an ancestor
of the branch tip). Direct commits to `master` only for a genuinely one-line, self-contained
change (a doc typo, a CI-config tweak).

Batch related work onto one branch/push rather than pushing per-step - every push to `master`
runs both CI workflows for real minutes, and a "fix X" → "redesign X" → "delete X" sequence
pushed separately leaves a permanent history of a fix for a mechanism that no longer exists by
the next commit.

**The branch is where validation happens, not `master`.** Ask before squash-merging anything
more than trivial/mechanical - the real gate is the user confirming it's actually right, not CI
passing (see Verification's unit-tested-vs-live-verified distinction below). Don't commit at all
without being asked to, either. If other uncommitted work is sitting in the tree when a
squash-merge comes up, ask whether to bundle it in rather than silently leaving it out.

## Repo layout

- `custom_components/pod_home/` — the HA integration itself. Ships as-is in the public repo.
- `podpoint-mobile-api/` — the standalone async API client, zero HA dependency (same pattern as
  `ohme` on PyPI). Install locally with `pip install -e podpoint-mobile-api` before running
  anything that imports it. Not yet published anywhere installable, so `custom_components/
  pod_home/podpoint_mobile_api/` is a temporary vendored copy (`manifest.json`'s `requirements`
  stays empty until a real release exists) - never edit the vendored copy directly; edit
  `podpoint-mobile-api/src/podpoint_mobile_api/` and re-copy, `diff`-checking every changed file.
- `README.md` — the living end-user-facing project doc.

## Documentation style

Docs and code comments describe **the API itself** — confirmed behavior, endpoint shapes, field
names, confirmed vs. best-guess — not the investigation that produced that description. Same
scope applies to this file: describe the target, not the process.

Prefer `None`/unknown over a plausible-looking guessed value. Confirm live before relying on an
assumption about API behavior, and mark anything unconfirmed clearly, tersely, in the code
comment itself.

### Comments and docstrings

Terse by default, per Home Assistant's own convention ([development guidelines](https://developers.home-assistant.io/docs/development_guidelines)):
a one-line docstring for most things; Google-style `Args`/`Returns`/`Raises` only when genuinely
needed beyond what type hints already say. A comment states the load-bearing fact a future editor
needs, not the full reasoning or decision history behind it. No "per the user directly"/"confirmed
live" narrative framing in code comments. Don't point comments/docstrings at any `.md` file
(DECISIONS.md, CLAUDE.md, README.md, QUALITY_SCALE.md) either - if the fact matters, it's terse
enough to state directly; if it needs more than that, it doesn't belong in code at all.

A code comment may state a fact. It may never state a comparison. If a comment mentions an
alternative that was considered, a trade-off between two approaches, or explains why one approach
was chosen over another - that's reasoning, not a fact, and it doesn't belong in code at any
length, however compressed. A comment with no comparison in it - just the fact or gotcha itself,
standing alone - should almost always be one sentence. If a pure fact won't fit in one or two
sentences, that's usually a sign it's actually two facts (split it), or that the surrounding code
needs a clearer name/structure instead of a comment carrying the load.

Same test for docstrings: a docstring that's grown long is usually explaining *why* (reasoning -
doesn't belong in code) or explaining *too much of what* (the function does too much - see
"Function size" below), not failing to be a good docstring.

### Function size

No hard line-count ceiling - a field-by-field constructor call or an `if`/`elif` chain over a
closed enum is fine at any length. The real test: does this function mix more than 2-3
independently-nameable concerns (fetch this, then cache that, then branch on mode, then compute
something unrelated)? If yes, split along those concern boundaries into named private methods
regardless of current line count - prefer that over a longer docstring/comment that just explains
all the concerns a reader now has to hold in their head to follow the function.

Doesn't apply to a function whose job is assembling one aggregate value from several already-
fetched inputs, where each step runs once, only from this call site - that's not concern-mixing,
it's what assembling a rich object legitimately looks like, and splitting it further only adds
indirection with no reuse or independent-testability gained. Extract a piece only when it's
independently meaningful on its own terms (a real concept with a name, reusable or unit-testable
standalone) - not just to shrink the parent function's line count.

### Entity states and translations

A `device_class: enum` sensor's (or a `select`'s) raw state must be a stable snake_case key, with
the display text in `strings.json`/`translations/en.json`'s per-entity `state` block ([HA's
i18n convention](https://developers.home-assistant.io/docs/internationalization/core)) - not
baked into the raw value itself. Applies to this integration's own derived vocabulary (Status,
Charging Scheme, Charge Mode). Exception: raw wire-passthrough debug sensors (Charging state,
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
it from a message and relaying it into a tool call. Test scripts that need it prompt for it
themselves via `getpass` (hidden, not logged) and are run by the user in their own terminal. If a
script's output is needed, read the files it saves rather than asking the user to paste
credentials.

## Write endpoints

`charge-overrides` (charge now) and `remote-lock` (cable lock) have real physical effects on a
real charger. Do not implement, call, or test these without the user explicitly asking for that
specific action, live, knowing what it'll do.

More generally: any write-capable entity or service stays flagged "NOT YET TESTED against a real
account" in its docstring until the user has explicitly confirmed the write lands correctly live
— not just these two named physical-effect endpoints. Don't mark a write as working from code
review or offline reasoning alone.

## Verification

`.github/workflows/test.yml` is the authoritative place `pytest tests/` is verified (Linux
runners) - not necessarily any given local machine. Layered checks, cheapest first:

- `python -m py_compile` + `python -m pyflakes` on `custom_components/pod_home/` — syntax and
  unused-import/undefined-name only, doesn't import `homeassistant`.
- `python -c "import podpoint_mobile_api"` (after `pip install -e podpoint-mobile-api`) — a real
  import check; `podpoint-mobile-api/` has zero HA dependency.
- A local live-API smoke test (see "Credentials") exercises real API *behavior* — covers
  `podpoint_mobile_api` only, not the coordinator, entities, or config flow.
- `tests/test_helpers.py` exercises `helpers.py`'s pure functions offline with hand-built fixture
  data — no HA instance or live account needed (see `tests/_pod_home_loader.py` for how it
  imports helpers.py/const.py without Home Assistant installed). The main way to check
  coordinator/entity-level logic beyond compiling.
- `python -m pytest tests/` — the full suite, one unified `pytest-homeassistant-custom-component`
  session (`pip install -r requirements_test.txt` first; dev/test-only, never in
  `manifest.json`). Always use `python -m pytest tests/`, not bare `pytest` — only `-m` puts the
  repo root on `sys.path`. An entity constructed directly for a test (not through a real
  platform) never gets `self.hass` set automatically — assign it manually (`entity.hass = hass`)
  first (see `test_button.py`). One test file per module/platform; check a file's own docstring
  for what it covers rather than expecting this list to enumerate them. **Still not covered**:
  dynamic-device creation's `predicate`-gated path — needs a second real charger to exercise
  meaningfully, not just more mocking.

Unit-tested (mocked API responses, real `hass` fixture) is not the same claim as **live-verified**
against a real Home Assistant instance and a real account - track these separately. Don't claim
something works live just because it's covered by a test with mocked data; say plainly which
kind of verification a given piece of code actually has.
