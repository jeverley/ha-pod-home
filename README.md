# Pod Home

A Home Assistant custom integration for Pod Point EV chargers, built against the same
`mobile-api.pod-point.com` backend (Firebase auth) the official **Pod Home** app uses - not the
older `api.pod-point.com/v4` API the community
[pod-point-home-assistant-component](https://github.com/mattrayner/pod-point-home-assistant-component)
targets. A clean rewrite for that newer backend, not a patch to the older integration.

## Requirements

- A Pod Point account with the email/password you use to sign in to the Pod Home app.
- At least one Pod Point charger on that account.

## Installation

### HACS

1. HACS → Integrations → ⋮ → **Custom repositories** → add this repository URL, category
   **Integration**.
2. Install **Pod Home**, then restart Home Assistant.

### Manual

Copy `custom_components/pod_home/` into your Home Assistant `config/custom_components/` folder,
then restart Home Assistant. The integration bundles its own API client - no separate package
install needed.

## Setting up

Settings → Devices & services → Add integration → **Pod Home**, then enter your Pod Home email
and password. Each charger on the account becomes its own device; a linked vehicle (connected via
Enode, if your account has one) becomes a second device.

## What you get

Three device types are created: one **charger** device per Pod Point charger on the account, one
**car** device only if a vehicle is linked via Enode, and a single account-level **Pod Point**
device (one per config entry) for entities that aren't tied to a specific charger. Entity
availability and which ones are enabled by default depend on your account's Charging Mode (Smart
Charging vs. Basic Charging, set from the app) and tariff - see "Charging Mode" below.

Each table's **Category** column is the same grouping Home Assistant's own device page uses:
**Sensors** and **Controls** are its two default (uncategorized) groups, split by entity domain;
**Configuration** and **Diagnostic** are this integration's explicit `entity_category` choices.

### Charger device

| Entity              | Category      | Type           | Notes |
| -------------------- | ------------- | -------------- | ----- |
| Status                | Sensors       | Sensor         | High-level status derived from the charger's wire state (Charging, Paused, Available, Finished, etc.) |
| Last charge duration   | Sensors       | Sensor         | Duration of the most recent charge - live if one is in progress, else the last finished one |
| Last charge energy     | Sensors       | Sensor         | Per-session snapshot - don't add to the Energy Dashboard |
| Last charge cost       | Sensors       | Sensor         | Per-session snapshot - don't add to the Energy Dashboard |
| Month energy           | Sensors       | Sensor         | Finalized charges only, resets each calendar month |
| Month cost             | Sensors       | Sensor         | Finalized charges only, resets each calendar month |
| Total energy           | Sensors       | Sensor         | Live-inclusive running total since this install started tracking - see "Energy Dashboard" below |
| Electricity rate       | Sensors       | Sensor         | Current tariff rate, computed from your configured tariff windows - Smart Charging only |
| Boost end time         | Sensors       | Sensor         | When the active boost ends, if any |
| Cable status           | Sensors       | Binary sensor  | On when a cable is physically connected |
| Firmware               | Controls      | Update         | See "Known limitations" for what the version number does and doesn't tell you |
| Schedule               | Controls      | Calendar       | Manual schedule (Basic) or live smart-charging plan (Smart), whichever mode is active |
| Boost duration         | Controls      | Time           | Local input (hh:mm) for Boost for duration below - no default, resets after each use |
| Full charge            | Controls      | Button         | Boost ("Charge Now") to 100%, matching the app |
| Boost for duration     | Controls      | Button         | Boost for the duration set above, matching the app |
| Cancel boost           | Controls      | Button         | Only available while a boost is active |
| Remote lock            | Controls      | Lock           | Solo 3S only - state stays `unknown` on other charger models (confirmed live via `offMode: null`), including the account this integration is developed against |
| Charge priority        | Configuration | Select         | Smart Charging only, disabled by default outside it |
| Charging state         | Diagnostic    | Sensor         | Raw wire value, disabled by default |
| Charging mode          | Diagnostic    | Sensor         | Smart / Basic |
| Connectivity           | Diagnostic    | Binary sensor  | On when the charger is reachable via Pod Point's cloud |

### Car device (only if a vehicle is linked via Enode)

| Entity                | Category      | Type          | Notes |
| ---------------------- | ------------- | ------------- | ----- |
| Battery                 | Sensors       | Sensor        | Vehicle's reported battery level, via Enode |
| Estimated range         | Sensors       | Sensor        | Vehicle's estimated range, derived from battery level (not live telemetry) |
| Odometer                | Sensors       | Sensor        | Vehicle's odometer reading, via Enode |
| Expected charge         | Sensors       | Sensor        | Smart Charging's live prediction for the % it'll actually reach by Ready by - can diverge from Target charge if a constraint (e.g. Charge priority) prevents hitting it. Smart Charging only |
| Charge rate             | Sensors       | Sensor        | Vehicle's charge rate - null once charging stops |
| Charge time remaining   | Sensors       | Sensor        | Estimated time remaining, if reported by the vehicle |
| Charging                | Sensors       | Binary sensor | On while the vehicle itself reports charging, independent of the charger's own state |
| Ready by                | Configuration | Time          | Smart Charging only, disabled by default outside it |
| Target charge           | Configuration | Number        | Smart Charging only, disabled by default outside it |
| Power delivery state    | Diagnostic    | Sensor        | Raw wire value from the vehicle's own charge state, disabled by default |
| Max current             | Diagnostic    | Sensor        | Raw wire value, disabled by default - always null on this account so far |

### Pod Point device (account-level, one per config entry)

| Entity          | Category | Type   | Notes |
| ---------------- | -------- | ------ | ----- |
| Rewards balance   | Sensors  | Sensor | Account-wide Pod Point rewards balance, always in GBP regardless of your billing currency |

### Energy Dashboard

Add **Total energy** to Settings → Dashboards → Energy - it's a live-inclusive running total
(a session in progress counts immediately), unlike Month energy, which lags until each session
finalizes. **Don't** add Last charge energy/cost there - those are per-session snapshots, not
a monotonic running total, and will break the dashboard's math.

### Charging Mode

Pod Point chargers run in one of two modes, switched from the Pod Home app (this integration
doesn't add a mode-switch control):

- **Smart Charging** - schedule-optimized charging to a target %, by a target time, aware of your
  tariff. Ready by/Target charge/Charge priority/Electricity rate only apply here, and are
  automatically disabled outside this mode. Works on a single-rate or two-rate tariff - Pod Point
  coordinates directly with your supplier to charge during low-demand periods even on a
  single-rate tariff, this does not force a switch to Basic Charging.
- **Basic Charging** - the charger follows its own fixed manual schedule instead. Selecting a
  tariff with more than two rate windows reverts the account to Basic Charging automatically -
  that's Pod Point's own behaviour, not something this integration decides.

## Known limitations

- **Firmware update version numbers are a placeholder when an update is pending.** The API
  exposes "update available" as a plain yes/no; no field carrying the actual target version
  string has been identified yet, so the Update entity's `latest_version` is best-effort, not a
  real version string, whenever an update is flagged.
- **Dynamic device creation (a charger added to the account appearing without a restart) is
  built but not verified against a second physical charger** - only ever tested against a
  single-charger account.
- **Remote lock is untested against a real Solo 3S** - it's a Solo 3S-only feature (per Pod
  Point's own app guide) and the account this integration is developed against has a Solo 3,
  which reports `offMode: null` (state `unknown`) rather than a real locked/unlocked value.
  Built code-reviewed-only for that reason - if you have a Solo 3S and try it, feedback on
  whether it works as expected is genuinely useful.
- Not yet packaged for HACS's default repository (no releases/tags yet) - install as a custom
  repository (see above) for now.

## More detail

- [`PLAN.md`](PLAN.md) - status, what's live-confirmed vs. still unverified, phased roadmap.
- [`QUALITY_SCALE.md`](QUALITY_SCALE.md) - status against Home Assistant's Integration Quality
  Scale, target tier platinum.
- [`DECISIONS.md`](DECISIONS.md) - the full reasoning behind every non-obvious design and API
  choice in this repo.
- [`CLAUDE.md`](CLAUDE.md) - working conventions for this repo.
