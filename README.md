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

Every entity below is created per charger unless noted otherwise. Entity availability and which
ones are enabled by default depend on your account's Charging Mode (Smart Charging vs. Basic
Charging, set from the app) and tariff - see "Charging Mode" below.

**Sensors**: Status, Charging state (raw wire value), Charging mode, Last charge duration/energy/
cost, Month energy, Month cost, Total energy, Rewards balance, Electricity rate, Boost end time.

**Binary sensors**: Connectivity, Cable status.

**Update**: Firmware (shows when an update is available; see "Known limitations" for what the
version number does and doesn't tell you).

**Calendar**: Schedule - the charger's manual schedule (Basic Charging) or its live smart-charging
plan for the current session (Smart Charging), whichever mode is currently active.

**Controls** (Smart Charging accounts only, disabled by default outside Smart Charging - see
below): Ready by (time), Target charge (number), Charge priority (select).

**Boost** ("Charge Now", matching the app's own two options): Full charge, Boost for duration
(paired with a Boost duration time input), and Cancel boost.

**Vehicle device** (only if a vehicle is linked via Enode): Battery, Estimated range, Odometer,
Expected charge, Power delivery state, Charge rate, Max current, Charge time remaining, and a
Charging binary sensor.

### Energy Dashboard

Add **Month energy** / **Month cost** to Settings → Dashboards → Energy. Both reset at the start
of each calendar month in the charger's own local timezone. **Don't** add Last charge energy/cost
there - those are per-session snapshots, not a monotonic running total, and will break the
dashboard's math.

### Charging Mode

Pod Point chargers run in one of two modes, switched from the Pod Home app (this integration
doesn't add a mode-switch control):

- **Smart Charging** - schedule-optimized charging to a target %, by a target time, aware of your
  tariff. Ready by/Target charge/Charge priority/Electricity rate only apply here, and are
  automatically disabled outside this mode.
- **Basic Charging** - the charger follows its own fixed manual schedule instead. Note: selecting
  a tariff with more than two rate windows, or one where the supplier controls charging directly,
  reverts the account to Basic Charging automatically - that's Pod Point's own behaviour, not
  something this integration decides.

## Known limitations

- **Charge now / remote cable lock aren't identical to the app's real-time feel.** Commands are
  pull-based - the charger only picks up a pending action on its own next check-in with the
  cloud, observed to be up to ~5 minutes. This applies to the boost buttons and would apply to a
  future cable-lock control the same way.
- **Remote cable lock isn't implemented yet.** Boost ("Charge Now") is; cable lock is the one
  remaining write endpoint from the original scope.
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
