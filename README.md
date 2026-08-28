# Pod Home (Home Assistant integration)

A clean-slate Home Assistant integration for Pod Point EV chargers, built against the new
`mobile-api.pod-point.com` backend (the one the "Pod Home" app, 2.6.4+, uses) rather than the
legacy `api.pod-point.com/v4` API that the older
[pod-point-home-assistant-component](https://github.com/mattrayner/pod-point-home-assistant-component)
targets.

## Status: quality-scale fixes + Energy Dashboard sensors done, still never run inside real HA

`smoke_test_api.py` has been run successfully against a real account multiple times, including
the two Step-0 pre-checks for this phase (a zero-activity same-day `charge-statistics` range,
and `GET /users` for the account currency). But the coordinator/entity/config-flow layer -
including everything added this phase - has **never actually run inside Home Assistant**. That
real-HA validation pass is the next milestone; see [`PLAN.md`](PLAN.md).

What's implemented, all built from **live-confirmed** endpoint shapes:

- Firebase email/password auth with automatic token refresh, and a read-only API client for
  chargers list, connectivity status, charges (session history), tariffs, manual schedules,
  security logs, and the account's user/balance profile (for currency) - both live in their own
  standalone package, [`podpoint-mobile-api/`](podpoint-mobile-api/), with zero Home Assistant
  dependency (`pip install -e podpoint-mobile-api` to use it locally). `custom_components/pod_home/`
  depends on it like any other library, the same pattern platinum-tier integrations with a real
  API surface use (e.g. `ohme` on PyPI).
- A `DataUpdateCoordinator` (`coordinator.py`) that polls chargers + connectivity status +
  recent charges + month-to-date charge stats, with an **adaptive interval** (60s right after
  any observed activity, backing off to 300s - the same default the legacy integration uses -
  once quiet, so idle-time request volume doesn't increase over the established baseline; see
  coordinator.py's FAST/SLOW_POLL_INTERVAL comments for the live measurements behind this),
  deduped non-fatal logging, and reactive entity creation for a charger added to the account
  mid-runtime.
- Sensors: Status, Last Charge Duration/Energy/Cost, **Energy This Month / Cost This Month**.
- Binary sensors: Connectivity (confirmed signal, with `last_seen` as an attribute), Cable
  Status (**heuristic** - see below).
- A config flow (email/password, reauth support) and `diagnostics.py`.

**Energy Dashboard**: add **Energy This Month** / **Cost This Month** to Settings → Dashboards →
Energy. **Don't** add Last Charge Energy/Cost there - those are per-session snapshots that jump
between arbitrary totals, not a monotonic series; only the *This Month* pair uses
`state_class: total_increasing` and resets predictably (each calendar month, in the charger's
own local timezone).

## Known gaps / deliberately deferred

- **Charge now / stop charge now** and **remote cable lock** are not implemented. Both are
  *write* endpoints (`/chargers/{ppid}/charge-overrides`, `/remote-lock/{ppid}`) with real
  physical side effects on the charger, and haven't been tested live yet. Add them once you're
  ready to test against a real charger and know what each call will actually do. **Confirmed
  live: the cloud can't push to the charger at all** - a charge-override issued via the app
  produced no reaction until the charger's own next check-in. Commands are pull-based (the
  charger fetches pending actions when it calls home), so expect these to inherit an
  up-to-~5-minute latency between issuing them and them actually taking effect, regardless of
  how the integration polls - set that expectation up front when building these.
- **Charge Mode / Smart Charging switch** - the old integration's manual/smart toggle doesn't
  have a confirmed new-API equivalent yet. `delegatedControl.status` and the
  `/smart-charging/delegated-controls/...` endpoint family look like the right area, but the
  read/write shapes are unconfirmed.
- **Cable Status is a heuristic**, not a confirmed field: it's derived from the most recent
  `/charges` entry's `pluggedInAt`/`unpluggedAt` (on when plugged in but not yet unplugged).
  This has not been verified against a real "currently charging/plugged in" session - do that
  before trusting it for automations.
- **Dynamic devices is code-reviewed only** - a charger added to the account should get
  entities without an HA restart, but there's no second charger on the test account to actually
  prove that live.
- **Firmware/update entity** was left out - a firmware-info endpoint exists
  (`/api3/v5/units/{unitId}/firmware`) but hasn't been called live yet.
- **`manifest.json`'s `requirements` is deliberately empty** - `podpoint-mobile-api` isn't
  published anywhere pip-installable yet (no PyPI release, no tagged git release), so there's
  nothing valid to put there (a broken entry would make HA's requirements installer fail
  outright at setup). With it empty, HA won't auto-install the package at all, so
  `custom_components/pod_home/__init__.py`'s `from podpoint_mobile_api import ...` will raise
  `ImportError` unless it's manually `pip install`-ed into HA's own Python environment first.
  Needs a real release before this integration can load in a real HA instance unassisted.
- No tests, no strict typing, no HACS packaging (`hacs.json`, LICENSE, `.github/`), no git repo
  yet - see [`QUALITY_SCALE.md`](QUALITY_SCALE.md) for the full itemized status against HA's
  Integration Quality Scale.

## Trying it

There's no HA install in this environment to test against yet. Two options to validate before
that:

1. `pip install -e podpoint-mobile-api`, then `python smoke_test_api.py` - exercises the
   *real*, installed `podpoint_mobile_api` package (not a copy) against your live account:
   prompts for your email and a hidden (not echoed) password, never logs or stores it. Confirms
   the async client itself is sound; saves full responses to `scratch/output/` (gitignored - it
   contains real account data).
2. Copy `custom_components/pod_home/` into a real Home Assistant `custom_components/` folder
   and add the integration via the UI.
