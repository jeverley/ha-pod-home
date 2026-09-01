# Pod Home (Home Assistant integration)

A clean-slate Home Assistant integration for Pod Point EV chargers, built against the new
`mobile-api.pod-point.com` backend (the one the "Pod Home" app, 2.6.4+, uses) rather than the
legacy `api.pod-point.com/v4` API that the older
[pod-point-home-assistant-component](https://github.com/mattrayner/pod-point-home-assistant-component)
targets.

## Status: quality-scale fixes + Energy Dashboard sensors done, first real-HA pass underway

The API/auth layer has been exercised successfully against a real account multiple times,
including a zero-activity same-day `charge-statistics` range and `GET /users` for the account
currency. The integration has now also run inside a real Home Assistant instance for the first
time - see [`PLAN.md`](PLAN.md) for what that pass has and hasn't covered so far.

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
- Sensors: Status, Last Charge Duration/Energy/Cost, **Energy This Month / Cost This Month**,
  Electricity Rate (current £/kWh from the account's tariff schedule).
- Binary sensors: Connectivity (confirmed signal, with `last_seen` as an attribute), Cable
  Status (derived from `chargingState`, confirmed live).
- A Firmware **Update** entity - see "Known gaps" for a caveat on what "update available"
  actually means here.
- **A linked vehicle** (via Enode, when one's connected to your Pod Point account) gets its own
  device: Battery Level, Range, Odometer, Ready By sensors and a Charging binary sensor.
- A config flow (email/password, reauth support) and `diagnostics.py`.

**Energy Dashboard**: add **Energy This Month** / **Cost This Month** to Settings → Dashboards →
Energy. **Don't** add Last Charge Energy/Cost there - those are per-session snapshots that jump
between arbitrary totals, not a monotonic series. Energy This Month uses
`state_class: total_increasing` (energy can only accumulate); Cost This Month uses
`state_class: total` with an explicit `last_reset` (HA core doesn't permit `total_increasing` on
`device_class: monetary`, since a monetary value could legitimately fall). Both reset
predictably each calendar month, in the charger's own local timezone.

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
  have a wired-up new-API equivalent yet, though the read side is now confirmed live:
  `delegatedControl.status` (`/chargers`), `/smart-charging/delegated-controls/{ppid}/preferences`
  (`chargingStrategy`/`maxPrice`), and `/chargers/{ppid}/smart-schedules/active` (a real
  plugged-in/paused/charging schedule, with tariff-rate-tagged windows) are all real, readable
  shapes. Write shape for changing the mode is still unconfirmed.
- **Dynamic devices is code-reviewed only** - a charger added to the account should get
  entities without an HA restart, but there's no second charger on the test account to actually
  prove that live.
- **Firmware Update entity's `latest_version` is a placeholder, not a real version, when an
  update is pending** - `isUpdateAvailable` is a plain boolean; only the "no update" response
  shape has been seen live, and no field carrying an actual target version string has been
  identified. See `DECISIONS.md` for the compromise this entity makes.
- **`manifest.json`'s `requirements` is deliberately empty** - `podpoint-mobile-api` isn't
  published anywhere pip-installable yet (no PyPI release, no tagged git release), so there's
  nothing valid to put there (a broken entry would make HA's requirements installer fail
  outright at setup). With it empty, HA won't auto-install the package at all, so
  `custom_components/pod_home/__init__.py`'s `from podpoint_mobile_api import ...` will raise
  `ImportError` unless it's manually `pip install`-ed into HA's own Python environment first.
  Needs a real release before this integration can load in a real HA instance unassisted.
- No tests, no strict typing, no HACS packaging (`hacs.json`, `.github/`) yet - see
  [`QUALITY_SCALE.md`](QUALITY_SCALE.md) for the full itemized status against HA's Integration
  Quality Scale.

## Trying it

Copy `custom_components/pod_home/` into a real Home Assistant `custom_components/` folder and
add the integration via the UI.
