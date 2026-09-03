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

Tables below are grouped the same way Home Assistant's own device page groups them: **Sensors**
and **Controls** are the two default (uncategorized) groups Home Assistant splits by entity
domain; **Configuration** and **Diagnostic** are this integration's explicit `entity_category`
choices.

### Charger device

**Sensors**

| Entity | Type | Notes |
| --- | --- | --- |
| Status | Sensor | |
| Last charge duration | Sensor | |
| Last charge energy | Sensor | Per-session snapshot - don't add to the Energy Dashboard |
| Last charge cost | Sensor | Per-session snapshot - don't add to the Energy Dashboard |
| Month energy | Sensor | Finalized charges only, resets each calendar month |
| Month cost | Sensor | Finalized charges only, resets each calendar month |
| Total energy | Sensor | Live-inclusive running total since this install started tracking - see "Energy Dashboard" below |
| Electricity rate | Sensor | Smart Charging only |
| Boost end time | Sensor | When the active boost ends, if any |
| Cable status | Binary sensor | |

**Controls**

| Entity | Type | Notes |
| --- | --- | --- |
| Firmware | Update | See "Known limitations" for what the version number does and doesn't tell you |
| Schedule | Calendar | Manual schedule (Basic) or live smart-charging plan (Smart), whichever mode is active |
| Boost duration | Time | Local input (hh:mm) for the Boost for duration button below |
| Full charge | Button | Boost ("Charge Now") to 100%, matching the app |
| Boost for duration | Button | Boost for the duration set above, matching the app |
| Cancel boost | Button | Only available while a boost is active |

**Configuration**

| Entity | Type | Notes |
| --- | --- | --- |
| Charge priority | Select | Smart Charging only, disabled by default outside it |

**Diagnostic**

| Entity | Type | Notes |
| --- | --- | --- |
| Charging state | Sensor | Raw wire value, disabled by default |
| Charging mode | Sensor | Smart / Basic |
| Connectivity | Binary sensor | |

### Car device (only if a vehicle is linked via Enode)

**Sensors**

| Entity | Type | Notes |
| --- | --- | --- |
| Battery | Sensor | |
| Estimated range | Sensor | |
| Odometer | Sensor | |
| Expected charge | Sensor | Smart Charging only |
| Charge rate | Sensor | |
| Charge time remaining | Sensor | |
| Charging | Binary sensor | |

**Configuration**

| Entity | Type | Notes |
| --- | --- | --- |
| Ready by | Time | Smart Charging only, disabled by default outside it |
| Target charge | Number | Smart Charging only, disabled by default outside it |

**Diagnostic**

| Entity | Type | Notes |
| --- | --- | --- |
| Power delivery state | Sensor | |
| Max current | Sensor | |

### Pod Point device (account-level, one per config entry)

**Sensors**

| Entity | Type | Notes |
| --- | --- | --- |
| Rewards balance | Sensor | |

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
- Not yet packaged for HACS's default repository (no releases/tags yet) - install as a custom
  repository (see above) for now.

## More detail

- [`PLAN.md`](PLAN.md) - status, what's live-confirmed vs. still unverified, phased roadmap.
- [`QUALITY_SCALE.md`](QUALITY_SCALE.md) - status against Home Assistant's Integration Quality
  Scale, target tier platinum.
- [`DECISIONS.md`](DECISIONS.md) - the full reasoning behind every non-obvious design and API
  choice in this repo.
- [`CLAUDE.md`](CLAUDE.md) - working conventions for this repo.
