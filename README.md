# Pod Home

A Home Assistant custom integration for Pod Point EV chargers, built on the same backend as the
official **Pod Home** app.

## Requirements

- Home Assistant 2026.10 or later.
- The email and password you use to sign in to the Pod Home app.
- At least one Pod Point charger on that account.

## Installation

### Marketplace

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jeverley&repository=ha-pod-home&category=integration)

Or add it yourself:

1. Settings → **Marketplace** → **Add from link** (upper-right) → enter `jeverley/ha-pod-home` →
   **Add**. The Marketplace asks you to connect GitHub the first time you add a repository.
2. Open **Pod Home** → **Install**. If Home Assistant needs a restart first, a repair under
   Settings → System → Repairs offers to do it.

### Manual

Copy `custom_components/pod_home/` into your Home Assistant `config/custom_components/` folder,
then restart Home Assistant. No separate package install is needed.

## Setting up

Settings → Devices & services → Add integration → **Pod Home**, then enter your Pod Home email
and password.

## Features

The integration creates a **charger** device per charger, a **car** device if a vehicle is linked
via Enode, and one account-level **Pod Point** device. Some entities depend on your Charging
Scheme (see below).

### Charger device

| Entity             | Category   | Type          | Notes |
| ------------------ | ---------- | ------------- | ----- |
| Status             | Sensors    | Sensor        | Charging, Paused, Available, Finished, Pending (boost not yet picked up by the charger), etc. |
| Charge duration    | Sensors    | Sensor        | Current charge, or the last finished one |
| Charge energy      | Sensors    | Sensor        | Per charge; not for the Energy Dashboard |
| Charge cost        | Sensors    | Sensor        | Per charge |
| Month energy       | Sensors    | Sensor        | Finished charges, resets monthly |
| Month cost         | Sensors    | Sensor        | Finished charges, resets monthly |
| Total energy       | Sensors    | Sensor        | Running total including the current charge; use this for the Energy Dashboard |
| Electricity rate   | Sensors    | Sensor        | Current rate from your tariff, in either scheme |
| Boost end time     | Sensors    | Sensor        | End of the active or upcoming boost |
| Cable status       | Sensors    | Binary sensor | On when a cable is connected |
| Schedule           | Controls   | Calendar      | Manual schedule (Basic) or smart-charging plan (Smart), plus any boost |
| Boost duration     | Controls   | Time          | Duration for Boost for duration; resets after each use |
| Full charge        | Controls   | Button        | Boost to 100%, as in the app |
| Boost for duration | Controls   | Button        | Boost for the duration set above |
| Cancel boost       | Controls   | Button        | Available while a boost is active |
| Remote lock        | Controls   | Lock          | Solo 3S only; appears once support is detected |
| Charge mode        | Controls   | Select        | Options depend on Charging Scheme |
| Charging state     | Diagnostic | Sensor        | Raw API value, disabled by default |
| Charging scheme    | Diagnostic | Sensor        | Smart or Basic |
| Connectivity       | Diagnostic | Binary sensor | On when the charger is online |

### Car device

| Entity                | Category      | Type          | Notes |
| --------------------- | ------------- | ------------- | ----- |
| Battery               | Sensors       | Sensor        | Reported by the vehicle via Enode |
| Estimated range       | Sensors       | Sensor        | Derived from battery level |
| Odometer              | Sensors       | Sensor        | Reported by the vehicle via Enode |
| Expected charge       | Sensors       | Sensor        | Smart Charging's predicted % by Ready by |
| Charge rate           | Sensors       | Sensor        | While charging |
| Charge time remaining | Sensors       | Sensor        | If reported by the vehicle |
| Charging              | Sensors       | Binary sensor | On while the vehicle reports charging |
| Ready by              | Configuration | Time          | Smart Charging only |
| Target charge         | Configuration | Number        | Smart Charging only |
| Power delivery state  | Diagnostic    | Sensor        | Raw API value, disabled by default |
| Max current           | Diagnostic    | Sensor        | Raw API value, disabled by default |

### Pod Point device

| Entity          | Category | Type   | Notes |
| --------------- | -------- | ------ | ----- |
| Rewards balance | Sensors  | Sensor | Pod Point rewards balance, in GBP |

### Energy Dashboard

Add **Total energy** to Settings → Dashboards → Energy. Don't add Charge energy or Charge cost:
they reset with each charge.

### Charging Scheme

Set in the Pod Home app:

- **Smart Charging:** charges to a target % by a set time, using your tariff. Ready by, Target
  charge and Expected charge only work in this scheme.
- **Basic Charging:** follows a fixed manual schedule. A tariff with more than two rate windows
  switches the account to Basic Charging.

**Charge mode** offers **Lowest cost** or **Complete charge** in Smart Charging, and **Schedule**
or **Always on** in Basic Charging.

### Actions

- **`pod_home.start_boost`** starts a boost on a charger.
  - `duration` (optional): how long to boost. Without it, boosts to a full charge (12 hours).
  - `start` (optional): when to start. The charger begins at its next check-in, up to about 5
    minutes later.
  - Works with the cable unplugged; the boost applies once it's plugged in.
  - Replaces any existing boost. Fails if Always on is active.
- **`pod_home.cancel_boost`** cancels the active boost.

## Known limitations

- **Total energy** misses charges that finish while Home Assistant is offline for more than 31 days.
- **More than one charger** on an account is supported but untested.
- **Remote lock** is untested on a real Solo 3S. Feedback from Solo 3S owners is welcome.
- Not yet in the Marketplace catalog. Add it from a link (see Installation).

## About

Developed with the assistance of Claude (Anthropic). Licensed under the [MIT License](LICENSE).
