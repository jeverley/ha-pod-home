# Pod Home

A Home Assistant custom integration for Pod Point EV chargers, built on the same backend as the
official **Pod Home** app.

## Requirements

- Home Assistant 2026.10 or later.
- The email and password you use to sign in to the Pod Home app.
- At least one Pod Point charger on that account.

## Supported devices

Tested with a Pod Point **Solo 3**. Other Pod Point home chargers on a Pod Home account are not
tested. Remote lock needs a **Solo 3S**. A car appears if one is linked to your account in the Pod
Home app, through Enode.

## Installation

### Marketplace

1. Open Settings → **Marketplace** and search for **Pod Home**.
2. Select it → **Install**. If Home Assistant needs a restart first, a repair under
   Settings → System → Repairs offers to do it.

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=jeverley&repository=ha-pod-home&category=integration)

### Manual

Copy `custom_components/pod_home/` into your Home Assistant `config/custom_components/` folder,
then restart Home Assistant. No separate package install is needed.

## Setting up

Settings → Devices & services → Add integration → **Pod Home**, then enter your Pod Home email
and password.

To change the email or password later, use Settings → Devices & services → Pod Home → ⋮ →
**Reconfigure**. If Pod Point rejects the saved password, Home Assistant asks you to sign in again.

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

## How data is updated

Home Assistant polls Pod Point's cloud every 5 minutes, timed to just after the charger's own
check-in. It polls every minute for a few minutes after the car is plugged in or a boost starts,
until charging begins. A finished charge is added to Month energy and Month cost when the car is
unplugged. If Pod Point can't be reached, entities show Unavailable until the next successful
update.

## Use cases

- Boost charging during a free-electricity session, such as an Octopus Power-up.
- Show charge status, energy and cost on a dashboard, and Total energy in the Energy Dashboard.
- Set Ready by and Target charge for Smart Charging from an automation or dashboard.

## Examples

Boost for two hours from an automation:

```yaml
actions:
  - action: pod_home.start_boost
    data:
      device_id: <charger device id>
      duration:
        hours: 2
```

<details>
<summary>Boost for every joined Octopus Power-up session</summary>

Needs the Octopus Energy integration with Power-up sessions joined. It runs every 5 minutes and
whenever the joined sessions change, and boosts when a run of back-to-back sessions starts within
30 minutes (or is under way) and no boost covers it. Replace the placeholders with your own entity
IDs and charger device ID.

```yaml
alias: Boost charging for Octopus Power-ups
triggers:
  - trigger: time_pattern
    minutes: /5
  - trigger: state
    entity_id: event.octopus_energy_<account>_octoplus_power_up_events
    attribute: events
variables:
  chain: >-
    {%- set ns = namespace(start=none, end=none, done=false) -%}
    {%- for s in (state_attr('event.octopus_energy_<account>_octoplus_power_up_events', 'events') or [])
       | selectattr('end', 'gt', now()) | sort(attribute='start') -%}
      {%- if ns.start is none -%}
        {%- set ns.start = s.start -%}{%- set ns.end = s.end -%}
      {%- elif not ns.done and s.start <= ns.end -%}
        {%- set ns.end = [ns.end, s.end] | max -%}
      {%- else -%}
        {%- set ns.done = true -%}
      {%- endif -%}
    {%- endfor -%}
    {{ [ns.start.isoformat(), ns.end.isoformat()] if ns.start is not none else [] }}
  boost_end_state: "{{ states('sensor.<charger>_boost_end_time') }}"
  boost_end: >-
    {{ as_datetime(boost_end_state).isoformat()
       if boost_end_state not in ['unknown', 'unavailable', 'none', ''] else '' }}
conditions:
  - condition: template
    value_template: "{{ boost_end_state != 'unavailable' }}"
  - condition: template
    value_template: >-
      {{ chain | count > 0
         and as_datetime(chain[0]) - now() <= timedelta(minutes=30)
         and as_datetime(chain[1]) - now() > timedelta(minutes=2) }}
  - condition: template
    value_template: >-
      {{ not (boost_end != ''
              and as_datetime(boost_end) >= as_datetime(chain[1]) - timedelta(seconds=60)) }}
  - condition: template
    value_template: >-
      {{ not (boost_end != ''
              and as_datetime(boost_end) > now()
              and as_datetime(boost_end) <= as_datetime(chain[0])) }}
actions:
  - action: pod_home.start_boost
    data:
      device_id: <charger device id>
      start: "{{ chain[0] }}"
      duration:
        seconds: "{{ (as_datetime(chain[1]) - as_datetime(chain[0])).total_seconds() | int }}"
mode: single
```

</details>

## Troubleshooting

- **Entities are Unavailable.** Home Assistant couldn't reach Pod Point, because of a network
  problem or a Pod Point outage. They recover on the next successful update.
- **Status is Unknown and Connectivity is off.** Pod Point can't see the charger, for example
  when the charger has lost its network connection. They recover once it reconnects.
- **Home Assistant asks you to sign in again.** Pod Point rejected the saved password. Enter the
  current one, or use Reconfigure.
- **A boost starts a few minutes late.** The charger picks a boost up at its next check-in, up to
  about 5 minutes after the start. Status shows Pending until it does.
- **The boost buttons are unavailable.** The cable isn't connected, or Always on is active.
- **To see more detail,** turn on debug logging for the integration:

  ```yaml
  logger:
    logs:
      custom_components.pod_home: debug
  ```

## Removing the integration

1. Settings → Devices & services → Pod Home → ⋮ → **Delete**. This removes the charger, car and
   Pod Point devices and their entities, and deletes the sign-in and running totals the
   integration saved.
2. Remove Pod Home from the Marketplace, or delete `custom_components/pod_home/` if you installed
   it manually, then restart Home Assistant.

## Known limitations

- **Total energy** misses charges that finish while Home Assistant is offline for more than 31 days.
- **More than one charger** on an account is supported but untested.
- **Remote lock** is untested on a real Solo 3S. Feedback from Solo 3S owners is welcome.

## About

Developed with the assistance of Claude (Anthropic). Licensed under the [MIT License](LICENSE).
