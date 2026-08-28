# Working conventions for this repo

Home Assistant custom integration (`custom_components/pod_home/`, domain `pod_home`) for Pod
Point EV chargers, targeting the `mobile-api.pod-point.com` backend used by the "Pod Home" app
(Firebase auth) rather than the legacy `api.pod-point.com/v4` API the community `pod_point`
integration uses. A clean rewrite, not a patch to that older integration.

Status/roadmap live in [`PLAN.md`](PLAN.md); Home Assistant Integration Quality Scale
compliance tracking lives in [`QUALITY_SCALE.md`](QUALITY_SCALE.md). Check those before
assuming what phase of work this is in.

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
  actually resolve would break the integration loading for real.
- `README.md` / `PLAN.md` / `QUALITY_SCALE.md` — living project docs, part of the repo.
- `smoke_test_api.py` / `check_interval_probe.py` — dev tools that exercise the real,
  installed `podpoint_mobile_api` package against a live account (see "Credentials" below).
- `scratch/` — gitignored, everything here is local-only and may or may not exist on a given
  checkout: captured live-account API output, an installed app package used for API research,
  research notes, a throwaway PoC client. Never reference anything under `scratch/` as if it
  ships with the repo, and never move anything out of `scratch/` into the tracked tree without
  checking it doesn't contain real account data.

## Documentation style

Docs and code comments state **facts about the API** (confirmed live vs. unconfirmed guess,
endpoint shapes, field names) — not **how those facts were established**. No mentions of
decompiling, app packages, static analysis, or similar in anything that ships in the repo.

## Credentials

Never handle the user's Pod Point password directly — no typing it into a command, no reading
it from a message and relaying it into a tool call. Test scripts (`smoke_test_api.py`, anything
under `scratch/`) prompt for it themselves via `getpass` (hidden, not logged) and are run by the
user in their own terminal. If a script's output is needed, read the files it saves rather than
asking the user to paste credentials.

## Write endpoints

`charge-overrides` (charge now) and `remote-lock` (cable lock) have real physical effects on a
real charger. Do not implement, call, or test these without the user explicitly asking for that
specific action, live, knowing what it'll do.

## Verification

No Home Assistant install exists in this environment. What's actually checkable:

- `python -m py_compile` + `python -m pyflakes` across `custom_components/pod_home/` — syntax
  and unused-import/undefined-name checks only, does not import `homeassistant`.
- `podpoint-mobile-api/` has zero HA dependency, so it's fully import-testable, not just
  compile-checkable - `python -c "import podpoint_mobile_api"` (once `pip install -e
  podpoint-mobile-api` has been run) is a real check, not just a syntax one.
- `python smoke_test_api.py` — the one thing that exercises real API *behavior*, run by the
  user (see "Credentials"). Only covers `podpoint_mobile_api`, not the coordinator, entities,
  or config flow.

Everything above the API layer (`coordinator.py`, `entity.py`, `sensor.py`, `binary_sensor.py`,
`config_flow.py`, `diagnostics.py`) is unverified beyond compiling until it's actually run
inside a real Home Assistant instance — don't claim it works, say that plainly instead.
