# Design decisions

Rationale for choices in the code that aren't obvious from reading it alone - what's confirmed
vs. guessed, why a particular approach was picked over the alternatives, and what would need to
change if an assumption turns out wrong. Code comments/docstrings point here instead of
repeating this inline, per Home Assistant's own docstring convention (terse by default, PEP 257
+ Google-style only when genuinely needed - see
[developers.home-assistant.io/docs/development_guidelines](https://developers.home-assistant.io/docs/development_guidelines)).

**Append-only.** New entries get added; existing ones are never rewritten or deleted, even once
outdated - a later entry documenting a correction is the correction, not an edit to the original.
This file is a record of how decisions evolved, not just their current state.

## Adaptive polling (`coordinator.py`)

`FAST_POLL_INTERVAL` (60s) / `SLOW_POLL_INTERVAL` (300s) / `RECENT_CHANGE_WINDOW` (360s).

Measured live against a real account: the charger checks in with Pod Point's cloud (i.e.
`connectivity-status-v2`'s `lastSeenAt` actually changes) roughly every 300s on a quiet
baseline, plus extra out-of-band check-ins on physical events (plug/unplug, state changes) -
observed gaps ranged 18s-283s alongside the clean 300s baseline, too noisy to predict an exact
next-check-in time. So the coordinator backs off based on time-since-last-*observed*-change
instead: poll fast right after any change (catches event clusters like plug-in → charge-start),
drift back to slow once quiet.

`RECENT_CHANGE_WINDOW` (360s) is deliberately wider than the noisiest gap seen (283s), so one
quiet gap mid-session doesn't flap back to slow and miss the next check-in.

`SLOW_POLL_INTERVAL` is deliberately kept at the same 300s the legacy `pod_point` integration
has run at for real users, not lower - a first draft used 30s/150s, which would have made the
*idle* baseline poll faster than the legacy integration's proven-acceptable default for zero
benefit (nothing changes while idle), and pushed a sustained multi-hour charging session to
~10x the old request volume. Idle-time load doesn't increase at all under the current constants;
the adaptive part only costs more during a bounded window of actual activity.

Also confirmed live: the cloud can't push to the charger at all - a charge-override issued via
the app produced no `lastSeenAt` reaction until the charger's own next check-in. Commands are
pull-based (the charger fetches pending actions when *it* calls home), so once charge-now/
remote-lock exist, they'll inherit this same up-to-~5-minute latency regardless of how fast this
integration polls - faster polling only helps us *see* state sooner, it can't make the charger
*act* sooner.

## Reducing per-endpoint call volume beyond the fast/slow poll switch (`coordinator.py`)

The fast/slow poll switch above changes how *often* the coordinator runs at all, but several of
the calls made on every run were unconditional regardless of whether their underlying data could
plausibly have changed since the last poll - three were tightened further, each keyed off
already-available state rather than a new API call of its own:

- **`smart-schedules/active`** - only called when `delegatedControl.status == ACTIVE` (Smart
  Charging). Confirmed live that a Basic Charging charger 404s on this endpoint
  (`NO_ACTIVE_CHARGING_SESSION`) anyway - the call was pure waste in that mode, not just
  infrequently useful. Still called every poll while in Smart Charging mode (unchanged from
  before) - it describes the live session's own plan, not something staleness-cacheable like
  firmware/tariffs.
- **`charge-statistics`** (month-to-date energy/cost) - now fetched every poll only while that
  charger was charging as of the *previous* poll (`is_momentarily_charging()` applied to
  `self.data`, i.e. last poll's already-stored state, not this poll's - avoids a fetch-order
  dependency on chargingState, which isn't known yet at the point this decision is made).
  Otherwise fetched on a `CHARGE_STATS_REFRESH_INTERVAL` (30 min) cadence, as a safety net for
  the rare change that isn't session-driven (a billing correction, or the month rolling over
  while idle). Cached per-ppid in `_month_stats_by_ppid`/`_month_stats_fetched_at`, same shape as
  the existing firmware/tariffs staleness cache.
- **`/charges`** (`latest_charge`, the Last Charge sensor) - same reasoning as charge-statistics,
  but evaluated account-wide rather than per-ppid, since one `/charges` call covers every
  charger: fetched every poll while *any* charger was charging last poll, otherwise on the same
  30 min cadence. Using "last poll's state" rather than "this poll's state" means the poll where
  a session actually ends still triggers a fetch (last poll still showed charging), so the final
  numbers land within one poll of the session finishing, not just during it.

`CHARGE_STATS_REFRESH_INTERVAL` (30 min) is intentionally much shorter than
`FIRMWARE_TARIFF_REFRESH_INTERVAL` (6h) - these two values change for reasons a long cache window
could plausibly miss for a whole session (a mid-month billing adjustment showing up while nothing
is plugged in), unlike firmware/tariffs which are genuinely rare, account-configuration-level
changes.

## Non-fatal error handling / dedup logging (`coordinator.py`)

`_warn_once`/`_clear_warning`/`_safe_call`: a persistently-failing non-critical call (e.g.
connectivity status for one charger) logs a WARNING once, drops to DEBUG on repeats, then one
INFO on recovery - not a fresh WARNING every poll forever.

`_safe_call` deliberately does NOT catch `PodHomeAuthError` - that's left to propagate up to
`_async_update_data`'s outer handler so an auth failure anywhere (not just the first call)
converts to `ConfigEntryAuthFailed` and triggers HA's reauth flow, instead of being swallowed as
a non-fatal API error.

A successful call can still return `None` (a 2xx with an empty body - seen for real on
`/chargers/arch5/{ppid}`); `_safe_call` normalizes that to `{}` since every caller does an
unguarded `.get()` on the result.

## Currency handling (`coordinator.py`, `const.py`)

`_async_fetch_currency` deliberately does not fall back to a hardcoded default when `GET /users`
fails - it retries every poll while `self.currency` stays `None`, rather than permanently
locking in a guess after one transient failure. Pod Point serves both GBP and EUR accounts, so a
wrong-but-plausible default would be worse than briefly showing no unit at all.

`DEFAULT_CURRENCY = "GBP"` in `const.py` is only a last-resort *display* fallback (used by
`PodHomeCostMonthSensor.native_unit_of_measurement`) for the window before the first successful
`GET /users` - not a claim that GBP is universally correct.

## Empty `/chargers` response (`coordinator.py`)

If `GET /chargers` returns an empty list but the coordinator already has data from a previous
poll, it keeps the previous data rather than wiping every entity to unavailable - a single odd
empty response is more likely a transient glitch than "this account now has zero chargers."

## Carrying forward the latest charge (`coordinator.py`)

`RECENT_CHARGES_LOOKBACK` (14 days) bounds each poll's `/charges` query. If a charger has no
session inside that window, its previous `latest_charge` is carried forward rather than dropped
- the session didn't stop being real, it's just older than the lookback window. Otherwise Last
Charge Duration/Energy/Cost and Cable Status would go unknown for any charger simply unused for
two weeks.

## Device naming (`entity.py`, `helpers.py`)

Device `name` is the humanized model (`"Solo 3"`), not the serial - so entity names read as
"Solo 3 Cost This Month" rather than "PSL-562804 Cost This Month". The serial lives in
`DeviceInfo.serial_number` instead, HA's dedicated field for exactly this, so it's not lost,
just not the headline name.

`humanize_model_style` derives the display name from `modelInfo.style` via a letter/digit-
boundary heuristic (space before a trailing digit run, then title-case) rather than a lookup
table, so an unrecognized style still degrades to something reasonable. Confirmed correct
against two real Pod Point model names: `"solo3"` → `"Solo 3"` and `"solo3s"` → `"Solo 3S"` -
only `"solo3"` has actually been seen from a live account so far.

`serial_number` uses `ppid` ("PSL number") - confirmed as Pod Point's own consumer-facing
identifier for the physical unit (the one used to link it to the app), not an internal ID. A
second, seemingly internal serial also exists (`firmware.serialNumber`) - surfaced as an
attribute on Firmware Version instead of here, since it isn't the identifier a user would
actually recognize as "their charger's serial".

## Entity name capitalization (all platform files)

Sentence case ("Battery level"), not Title Case ("Battery Level") - confirmed via HA's own dev
docs: "Entity names should start with a capital letter, the rest of the words are lower case
(unless it's a proper noun...)". Applies to every `_attr_name` (and matching `strings.json`/
`translations/en.json` entry) with more than one word; single-word names and device names
("Solo 3", vehicle display names) are unaffected - those are either already correct or genuine
proper nouns, exempted by the same rule.

## Charging Mode sensor (`sensor.py`)

Surfaces `delegatedControl.status` from `/chargers` - whether Smart Charging (vs. Basic
Charging) is active for a charger. Zero added request cost: this field is already present in a
response the coordinator fetches every poll regardless, just previously discarded. Full value
set confirmed authoritatively via the account's public OpenAPI schema (`GET /api-json` on
`mobile-api.pod-point.com` - a live, machine-readable spec): exactly `UNKNOWN`/`ACTIVE`/
`INACTIVE`/`PENDING`. `ACTIVE`/`INACTIVE` are additionally confirmed live. An earlier version of
this doc guessed `PAUSED`/`DISABLED` - those don't exist.

The sensor shows the app's own two-value label for this field (`Smart Charging`/`Basic
Charging`, via `schedule_mode()` in `helpers.py`), not the raw 4-value wire enum. The app's own
UI text states this mapping directly: its settings toggle reads "On Smart Charging | Off Basic
Charging", and elsewhere: "a user with delegated control inactive will be on Basic Charging...
active will be on Smart Charging" - confirming it's a binary active-vs-not distinction, not a
1:1 display of the wire values. `schedule_mode()` deliberately does NOT extend that binary to
`UNKNOWN`/`PENDING`: `UNKNOWN` means the system itself doesn't know, and `PENDING` is a genuine
transitional third state the app's copy never addressed - both resolve to unknown rather than
being asserted into "Basic Charging". The raw status is still available as the
`raw_delegated_control_status` attribute for diagnostics. Confirmed live: `INACTIVE` (both
values seen by toggling the app's setting), and confirmed that toggling it does *not* clear the
vehicle/intents/smart-schedule data - Smart Charging keeps computing a full plan regardless of
whether it's currently allowed to act on it.

Also carries a `status_effective_from` attribute (when the mode last changed) from
`GET /smart-charging/delegated-controls/{ppid}` - a genuinely extra API call, unlike the state
itself, so fetched on the same 6h staleness cadence as firmware/tariffs/manual-schedules rather
than every poll. Deliberately sourced from the server's own timestamp, not derived from when
`pod_home` itself first observed the change - self-tracking would misleadingly reset to "now" on
every HA restart, since the coordinator has no memory of anything before that. Verified against
a real captured response: parses to the exact moment Smart Charging was switched back on.

## Firmware source (`coordinator.py`, `podpoint_mobile_api`)

Switched from the legacy `GET /api3/v5/units/{unitId}/firmware` to `GET /chargers/{ppid}/firmware`
- ppid-addressed, so it drops the `unitId`-missing guard and warning entirely (a charger always
has a `ppid`; `unitId` was an extra field that could be absent). The new endpoint's per-entry
shape matches what was already parsed (`serialNumber`/`versionInfo`/`updateStatus`); the one
real difference is it returns a bare list rather than a `data`-wrapped one, handled in
`_parse_firmware()`. Verified against a real captured response before switching, not just
assumed equivalent from the shape alone. The legacy method stays in `podpoint_mobile_api` (still
confirmed live, documented as superseded) rather than being deleted - no reason to lose a
working, previously-verified call.

## Schedule sensor (`sensor.py`)

One entity, not two, following Charging Mode: shows Smart Charging's plan
(`/chargers/{ppid}/smart-schedules/active`) while Charging Mode reads Smart Charging, or the
fixed manual schedule (`/chargers/{ppid}/manual-schedules`) while it reads Basic Charging. Both
underlying mechanisms are confirmed live to exist independently of the Charging Mode toggle
(both endpoints kept returning full, unchanged data whether Smart Charging was on or off in
every check so far) - so which one Basic Charging actually *enforces* is not independently
confirmed. This entity follows Charging Mode as the best available signal for "which schedule is
relevant right now", not a proven causal link - worth a live check (put the charger in Basic
Charging with an active manual window and confirm it actually gates charging) whenever that's
convenient, since the entity's correctness rests on that assumption.

Both mechanisms are collapsed onto the same `PLUGGED_IN`/`PAUSED`/`CHARGING` vocabulary so one
`SensorDeviceClass.ENUM` entity can represent either:

- **Smart Charging mode**: state is whichever `PAUSED`/`CHARGING` window (from
  `smart_schedule_windows`) contains the current time, compared as absolute UTC instants - no
  timezone resolution needed, unlike the tariff windows' day-of-week matching. `PLUGGED_IN`
  entries are a point-in-time marker (`timestamp`, not a range), so can never be "current".
  Refetched every poll, not staleness-cached - it describes the current session, not account
  config.
- **Basic Charging mode**: state is `CHARGING` if "now" (charger-local time) falls inside any
  active (`is_active`) window from `manual_schedule_windows`, else `PAUSED`. `start_day`/
  `end_day` are ISO weekday integers (1=Monday...7=Sunday, matching Python's `isoweekday()`
  directly - no name-based mapping needed like the tariff sensor), converted to minutes-since-
  Monday-00:00 and compared the same way `PodHomeElectricityRateSensor` handles a midnight-
  wrapping window, just on a weekly cycle instead of a daily one. Refreshed on the same 6h
  staleness cadence as firmware/tariffs - schedule configuration, not live state.

`None` when the mode itself can't be determined (unrecognized/missing `delegatedControl.status`)
or when the relevant window data hasn't loaded yet - never guessed. The full window list for
whichever mechanism is current is in the `windows` attribute (tagged `source:
"smart_schedule"`/`"manual_schedule"`), since sensor state can't itself hold a list; a `count`
of active manual windows was considered and rejected as the state - not informative on its own.

Neither the manual nor the smart schedule *write* endpoint has been called - this is a read-only
addition. A `select` entity for switching Charging Mode itself was also discussed and explicitly
deferred - at the time, no write endpoint for `delegatedControl.status` had even been looked for.
That's since changed: the public OpenAPI schema confirms `PATCH /smart-charging/delegated-
controls/{ppid}` with body `{ status: "ACTIVE" | "INACTIVE" }` ("Update a charger's delegated
controls status"). The deferral stands regardless - a `select` always renders as an interactive
control in HA, and this endpoint hasn't been called against the account or had its real effects
observed live, so building the entity now would still be ahead of the same live-confirm-before-
build discipline as every other write path here (same spirit the earlier stubbed-number-entity
idea was rejected for) - it's just now blocked on live testing + explicit go-ahead specifically,
not on finding the endpoint at all.

## Charge Status sensor (`sensor.py`, `helpers.py`)

Status (the `chargingState` pass-through) is unchanged and stays as-is for anything that wants
the raw wire value. Charge Status is a new, separate, derived sensor - `Unplugged`/`Charging`/
`Paused`/`Finished`/`Fault` - built specifically because `SuspendedEVSE` is confirmed live to
mean genuinely different things depending on context (observed directly: a live HA history graph
showed `SuspendedEVSE` for a 20-minute gap mid-session between two Smart Charging windows, then
again for 2+ hours after the vehicle's Ready By target had passed - identical state, opposite
meaning) - and separately, `SuspendedEVSE` can also be the charger pausing itself for its own
manual schedule, independent of any linked vehicle. Neither distinction is recoverable from
`chargingState` alone.

Deliberately small and conservative: `chargingState` values without a confident mapping
(`Preparing`/`SuspendedEV`/`Finishing`/`Reserved`/`Unavailable`/anything unrecognized) resolve to
unknown rather than being forced into one of the five buckets - same "don't guess" principle as
Cable Status's `Faulted -> None`.

### Sticky signals, not momentary re-derivation

An earlier version of this sensor resolved `SuspendedEVSE` by re-deriving Finished from the
Schedule sensor's *current* window on every call: "Schedule says CHARGING right now but the
charger disagrees → Finished". That has a real bug - a genuinely finished charge would flip back
to Paused the instant a manual schedule's window ended (or Smart Charging's plan disappeared
after Ready By passed), since by then the *current* instant no longer looked like "just
finished", even though nothing about the vehicle had actually changed. Flagged directly by the
user, who'd hit the identical problem with the legacy `pod_point` integration (which had the same
ambiguous-suspended-state issue) and worked around it with a template sensor + an `input_text`
helper + an automation that captured `charging_started`/`cable_unplugged`/`charged` timestamps on
state-transition triggers, then compared their recency (`charged_dt > charging_dt and charged_dt
> unplugged_dt`) to tell "genuinely finished" apart from "back to waiting". That's the same
mechanism this fix ports natively into the coordinator, so no template/helper/automation
infrastructure is needed on the HA side at all:

- `PodHomeCharger` carries three sticky timestamps - `charging_started_at`, `cable_unplugged_at`,
  `charge_finished_at` - the wall-clock time the coordinator last observed each condition true
  (`is_momentarily_charging()`/`is_momentarily_unplugged()`/`is_momentarily_finished()` in
  `helpers.py`), refreshed every poll (not staleness-cached - each is only as good as the most
  recent poll that actually saw it).
- `is_momentarily_finished()` is what the *old* single-shot logic used to do directly: the
  Schedule-sensor-window check (via `current_schedule_state()`, unchanged), falling back to
  `ready_by` when Schedule has nothing to go on (still deliberately not vehicle-dependent as the
  *primary* path, for the same reason as before: adding a vehicle is optional). It's now
  additionally strengthened by `vehicle.isFullyCharged` as a direct, independent trigger when a
  vehicle is linked. The difference is this result gets *remembered*, not just used once.
- `charge_status()` itself no longer re-derives anything about schedules - it just compares
  which of the three timestamps is most recent (`_is_finished_sticky()`). Finished holds for as
  long as `charge_finished_at` stays the newest of the three; a new charging session or a fresh
  unplug immediately invalidates it, exactly like the legacy template's boolean check did.
- Falls back to `Paused` (not unknown) when there's no sticky evidence either way - matching the
  legacy template's own `Waiting` fallback, not a new judgment call.
- Persisted across HA restarts via HA's own `Store` helper (`homeassistant.helpers.storage`),
  scoped per config entry rather than per-domain (a second Pod Home account, if one's ever added,
  gets its own file rather than colliding). Loaded once in `async_setup_entry`, before the first
  refresh, so the sticky state is already in place before any entity reads it. Saved with
  `async_delay_save` (coalesced, not one write per poll - the three dicts can change up to once a
  minute) rather than an immediate write. A corrupt or unreadable store file logs a warning and
  starts fresh instead of blocking setup - same "don't let bad cached state break the integration"
  posture as everything else here. This is a genuine difference from `_last_seen_changed_at`
  (adaptive-poll tracking), which still isn't persisted - that one only affects poll cadence, not
  correctness, so it wasn't worth the same treatment.

Verified offline (a throwaway script simulating a sequence of coordinator polls with fake
charger/vehicle/window objects, not part of the shipped repo) against the exact regression
scenario: charger finishes early inside an active manual-schedule window (Finished), the window
closes on a later poll with nothing else changed (still Finished - confirms the fix), then a new
charging session starts and finishes again (old Finished correctly invalidated, then re-earned).
Same sequence repeated for Smart Charging mode. Also covers the original branch set (Available/
Faulted/Charging/unmapped states) and the manual-schedule week-boundary wrap.

### Widened the sticky-Finished check, dropped Unplugged, renamed to Charger Status

The original version above only gave `SuspendedEVSE` the sticky treatment - every other
cable-connected-but-not-charging `chargingState` (`Preparing`/`SuspendedEV`/`Finishing`) fell
straight through to unknown, discarding the sticky Finished/Charging history entirely. Confirmed
live by the user: after a charge finishes, `chargingState` doesn't necessarily stay on
`SuspendedEVSE` - it can cycle through `Finishing`/`Preparing`/etc. before settling, and on any
poll that landed on one of those the entity flashed to Unknown instead of staying `Finished`,
defeating the whole point of the sticky mechanism above (the user's original legacy template
logic was shared specifically to keep the *value* sticky through exactly this kind of
transitional noise, not for its specific state vocabulary).

Fixed by running `_is_finished_sticky()` for every `chargingState` `CHARGING_STATE_CABLE_CONNECTED`
classifies as cable-connected (`SuspendedEVSE`/`SuspendedEV`/`Preparing`/`Finishing`), not just
`SuspendedEVSE` - Finished now survives chargingState wandering between any of them. **Past that
check**, the user drew a line the first pass had missed: their original template's Paused
fallback was an override for one specific case (don't lose "the car is charged" just because the
wire value moved off `SuspendedEVSE`), not a catch-all for everything this function doesn't
otherwise recognize. `SuspendedEVSE`/`SuspendedEV` literally mean "suspended", so they still
default to `Paused` absent a sticky Finished - but `Preparing`/`Finishing` describe something
else genuinely happening (about to start / wrapping up) that isn't confirmed enough to guess at,
so those fall through to unknown, same "don't guess" principle as everywhere else in this file.

Also, per the user: Cable Status (`binary_sensor.py`) already reports Unplugged, so Charger
Status no longer has its own `Unplugged` value at all - `CHARGE_STATUS_OPTIONS` dropped it,
`CHARGING_STATE_AVAILABLE`/`CHARGING_STATE_RESERVED`/`CHARGING_STATE_UNAVAILABLE` (cable not
connected) now resolve straight to unknown here instead of duplicating what Cable Status already
shows. Also renamed the sensor from "Charge status" to "Charger status" - display name, unique_id
(`..._charger_status`), and translation_key (`charger_status`) all changed together (pre-release,
so no entity-registry migration concern) - and moved Expected Charge from Diagnostic to Standard
(it's a user-facing Smart Charging prediction, not an internal/debug value).

Verified offline: extended the same throwaway script with the exact live-observed sequence
(`SuspendedEVSE` finishes → `Finishing` → `Preparing`, all while still Finished) plus the dropped
`Unplugged` value and an `Unknown`-chargingState case.

### Preparing/Finishing pass through as their own values, not unknown

The pass above still left `Preparing`/`Finishing` resolving to unknown when there's no sticky
Finished in play - flagged directly by the user as falling back to unknown too often. Fixed by
passing the raw chargingState value straight through as Charger Status's own state
(`CHARGE_STATUS_PREPARING = "Preparing"`, `CHARGE_STATUS_FINISHING = "Finishing"`, both added to
`CHARGE_STATUS_OPTIONS`) instead of discarding it: there's nothing wrong with showing the
charger's own wire value when there's no more useful derived meaning for it, only when there's
genuinely nothing to say at all (cable not connected - Cable Status covers that - or a wire value
`CHARGING_STATE_CABLE_CONNECTED` doesn't even classify). `SuspendedEVSE`/`SuspendedEV` are
unaffected - they still default to `Paused`, since that's the one case the override was actually
built for (see above); `Preparing`/`Finishing` were never "paused" in the first place, so passing
them through directly is more accurate than either guess.

The user then caught the same mistake one level up: "cable not connected" had been treated as a
single duplicate-of-Cable-Status bucket, but `Available`/`Reserved`/`Unavailable` are three
different concepts that only share a `CHARGING_STATE_CABLE_CONNECTED == False` classification for
that table's own purpose (whether a cable is physically present). First pass: kept `Available` as
the one deliberate exception (treated as a genuine duplicate of what Cable Status already reports
as Unplugged), passed `Reserved`/`Unavailable` through. The user then said explicitly: stop
mapping `Available` to unknown too - show it. So all three now pass through
(`CHARGE_STATUS_AVAILABLE = "Available"`, `CHARGE_STATUS_RESERVED = "Reserved"`,
`CHARGE_STATUS_UNAVAILABLE = "Unavailable"`, all added to `CHARGE_STATUS_OPTIONS`), same as
Preparing/Finishing above - Charger Status overlapping Cable Status for this one value is by
design now, not a duplicate to avoid. Unknown resolves to unknown, as does any future chargingState
value this integration hasn't named a constant for - the one remaining case where there's
genuinely nothing to say.

Introducing Charge Status also prompted a pass over the charger device's other entities'
`entity_category`. Only **Status** (raw `chargingState`) moved, to `Diagnostic`, now that Charge
Status is the friendly at-a-glance value built on top of it. **Schedule** and **Last Charge
Duration/Energy/Cost** were considered for the same move (Schedule as "which mechanism is
deciding" rather than what's happening; Last Charge as per-session historical stats rather than
day-to-day numbers) but rejected - kept `Standard`. **Energy This Month**/**Cost This Month**
(Energy Dashboard sensors) and **Electricity Rate** (answers a live "is it cheap right now"
question) stayed `Standard` too. On the vehicle side, **Odometer** moved from `Diagnostic` to
`Standard` - an ordinary at-a-glance stat, no reason it was categorized otherwise.

## Vehicle debug sensors (`sensor.py`, `binary_sensor.py`)

Five raw `vehicle.chargeState` fields not otherwise surfaced, all `entity_registry_enabled_default
= False`: **Power Delivery State** (`powerDeliveryState` - only `PLUGGED_IN:STOPPED` confirmed
live; the clearest path to a finer-grained Charge Status once `PLUGGED_IN:COMPLETE` is actually
seen), **Fully Charged** (`isFullyCharged`, a `binary_sensor` - no `BinarySensorDeviceClass` fits
cleanly, since `BATTERY_CHARGING` means something different and `BATTERY` is inverted, so it's
left without one), **Charge Rate**, **Max Current**, **Charge Time Remaining** (`chargeRate`/
`maxCurrent`/`chargeTimeRemaining` - all three have only ever been observed `null` on this
account, so their unit/shape is an unconfirmed guess (kW/A/minutes), included for other
vehicles/accounts rather than because they're proven here). All are `PodHomeVehicleEntity`
subclasses, so they simply don't exist for an account with no linked vehicle - not an error
state, the same as every other vehicle-scoped entity.

## Cable Status sensor (`binary_sensor.py`)

Derived from `chargingState`, via `CHARGING_STATE_CABLE_CONNECTED` in `const.py` - a dict, not
an allowlist, mapping every value in `CHARGING_STATE_OPTIONS` to `True`/`False`/`None`. `None`
covers two distinct cases deliberately given the same answer: a chargingState value not yet
catalogued at all, and `Faulted`, which is explicitly ambiguous (a fault can occur with or
without a cable connected) rather than confidently "not connected". The dict's keys are
asserted to exactly match `CHARGING_STATE_OPTIONS` at import time, so adding a new state to one
without classifying it in the other fails loudly at startup instead of silently defaulting at
runtime. Confirmed live: `chargingState` transitions before an active session starts (e.g.
`SuspendedEVSE`, seen live while a cable was physically connected but charging was withheld
pending a scheduled window), so this reacts immediately - no extra API call needed, since
`chargingState` is already fetched every poll.

Also confirmed live: `Preparing` and `Finishing` (added to `CHARGING_STATE_OPTIONS` on top of
the standard OCPP set after that live sighting) are themselves still unconfirmed - the mapping
above is a best-effort classification for values that haven't been seen live yet, same as the
enum values themselves. `Charging` is now confirmed live too (seen in a real HA history graph,
alongside two `SuspendedEVSE` gaps either side of it - the observation that fed into the Charge
Status sensor below).

Depending on the connectivity poll succeeding (charging_state is None on a fetch failure,
reported as unknown) is intentional, not a gap: showing unknown when the data that would reveal
the real state genuinely failed to fetch is more honest than confidently showing a value that
might be wrong - the same principle that motivated replacing the original `/charges`-derived
heuristic below.

Previously derived from the most recent `/charges` entry (`pluggedInAt` set, `unpluggedAt`
unset) - replaced after being observed wrong live: `/charges` only gets a new entry once a
session actually starts charging, so a cable plugged in but held at `SuspendedEVSE` (e.g. by an
active Smart Charging schedule) left the sensor reading the previous, fully-completed session
instead.

## Connectivity sensor (`binary_sensor.py`)

`lastSeenAt` is surfaced as an attribute on the Connectivity sensor rather than as its own
entity - it's diagnostic detail about connectivity specifically, not independently
dashboard-worthy on its own.

Named "Connectivity" (not "Online" or "Cloud Connection") to match both Home Assistant core's
own default display name for `BinarySensorDeviceClass.CONNECTIVITY`, and the convention used by
Sensibo (the closest `cloud_polling` precedent in HA core).

## `Cost This Month`'s state class (`sensor.py`)

`state_class: total`, not `total_increasing` - confirmed live that HA core rejects
`total_increasing` on `device_class: monetary` (only classes that can never legitimately
decrease, like `energy`, allow it; a monetary value could legitimately fall, e.g. a refund).
`total` instead relies on the explicit `last_reset` property (midnight on the 1st of the current
calendar month, in the charger's local timezone) to tell HA's recorder/statistics engine when
the current accumulation period began.

## Electricity Rate sensor (`sensor.py`)

Computed from `/chargers/{ppid}/tariffs`' recurring windows (day-of-week list + start/end time
+ price) matched against the current local time, rather than exposing the raw tariff shape
directly - gives a plain, dashboard-ready current £/kWh instead of requiring every consumer to
re-derive it. Two edge cases beyond the one confirmed-live 2-rate/all-7-days tariff, verified
against a standalone reimplementation of the matching logic rather than just live data (neither
case has been observed live yet):
- A window with `start == end` matches unconditionally (covers the whole day) - Pod Point's own
  app confirms single-rate tariffs exist as an account type, and a single all-day window is a
  plausible encoding for one.
- A window wrapping past midnight (e.g. `23:00`→`05:00`) is matched against *yesterday's* day
  name too during its after-midnight portion, not just today's - otherwise a genuinely
  day-specific wrapped window (not every day listed, unlike the one live tariff seen so far)
  would silently fail to match during the early-morning hours it's meant to cover.

## Firmware and tariffs: re-checked periodically, not every poll (`coordinator.py`)

Unlike connectivity/vehicle data, firmware version and tariff rates change rarely - gathering
them every poll (like the original design) would add two API calls per poll forever for little
benefit, the same request-volume mistake corrected once already for adaptive polling. Re-fetched
on `FIRMWARE_TARIFF_REFRESH_INTERVAL` (6h) per `ppid`, not literally once-ever: firmware version
changing is exactly what the Update entity exists to detect, so caching it permanently after the
first successful fetch would mean a real, user-installed update never gets noticed. A fetch
failure doesn't count as "fetched" (retried on the very next poll, not after a further 6h wait).

A missing `unitId` short-circuits the firmware fetch entirely (with one deduped warning) rather
than retrying a guaranteed-`.../units/None/firmware`-shaped failure forever - `ppid` already
gets the equivalent guard a few lines above for the same reason.

## Firmware `Update` entity (`update.py`)

HA's `UpdateEntity` compares `installed_version` against `latest_version` - both real version
strings in the normal case. Only the "no update available" response shape has been seen live
(`{"isUpdateAvailable": false}`); the underlying response model is confirmed richer than that
(a separate manifest-type sub-structure with its own status/architecture enums), but no field
carrying an actual target version string has been identified, live or otherwise.

`latest_version` is therefore not a genuine version number when `update_available` is true -
it's `installed_version` with a marker suffix appended, purely so HA's own equality check
reports "update available" correctly. This is a known, documented compromise (see the entity's
own docstring) rather than a clean solution - revisit once a live "update available" response
confirms the real target-version field. `update_available` is also surfaced directly as an
attribute (not just folded into `latest_version`), since HA's own state still can't show
anything meaningful when `installed_version` itself is unknown - the raw boolean stays visible
even then, rather than getting silently lost along with the state.

## Vehicle device (`entity.py`, `sensor.py`, `binary_sensor.py`)

A linked vehicle (via Enode, Pod Point's third-party connected-car data provider) gets its own,
fully standalone HA device - not `via_device`-linked to any charger. `via_device` specifically
means "reachable only through that other device" (HA's own docs frame it as hubs/gateways), and
that's not true here: Enode talks to the vehicle independently of any charger's own
connectivity, and the app itself doesn't nest vehicle info under a charger either - an earlier
version of this design used `via_device` for the operational association (which charger a
vehicle is currently linked to for delegated control), which was a real modeling mistake, not
just a UX call - "operationally associated with" and "reachable through" aren't the same
relationship.

`PodHomeVehicleEntity` fixes only `vehicle_id` at construction (globally unique across the
account) - `unique_id` doesn't include `ppid` at all, and which charger a vehicle is currently
linked to (used only for the internal `coordinator.data` lookup, not exposed as a device
relationship) is re-derived from live coordinator data on every access rather than frozen. This
matters on a multi-charger account: if the same vehicle later becomes associated with a
different charger, entities keyed by `(ppid, vehicle_id)` would collide on `unique_id` with the
already-registered ones under the old `ppid` (HA drops the add as a duplicate) while the old
entities stay permanently stuck looking at a charger that no longer has this vehicle. Re-deriving
the association live avoids both problems. `async_setup_dynamic_vehicles` mirrors this: it
dedupes purely by `vehicle_id`, not `(ppid, vehicle_id)`, so a vehicle moving between chargers is
never treated as a "new" vehicle needing a second set of entities. (Confirmed live separately,
and unaffected by this: a vehicle can drop out of `isPluggedInToThisCharger` - going `false` -
while still appearing in the same charger's response; `available` already handles that correctly
via `vehicle is not None`, since the vehicle object itself, not just the flag, keeps existing.)

`Target Charge` (`chargeLimitPercent`) and `Expected Charge` (its own sensor, from
`currentIntent.chargeDetail.expectedChargeByTargetPercent`) are deliberately two different
sensors, not a duplicate pair: the first is what you asked Smart Charging for (a user-set
target, e.g. "100% by 7am"), the second is what it currently predicts it'll actually deliver
given constraints - confirmed live diverging from each other (target 100%, predicted 91%) when
`cannotMeetTargetReason: "PRICE"` fires (Charge Priority set to lowest-cost won't use peak-rate
power even if that would let it hit the target). `can_meet_target`/`cannot_meet_target_reason`
are surfaced as attributes on Expected Charge for the same reason - "PRICE" is the only
confirmed value; other reasons are plausible but unconfirmed.

`Estimated Range` is exactly that - confirmed live it moves only when battery level percent
does, so it's derived from state of charge, not an independent live telemetry reading.

Battery Level keeps that exact name rather than the bare "Battery" HA core itself defaults to
for `device_class: battery` with no explicit name - confirmed precedent (Teslemetry, a real EV
integration in HA core) keeps the same "Battery Level" specificity specifically because it also
has multiple other battery-related sensors on the same device (battery range, usable battery
level, etc.) - the same situation this integration is now in with Target Charge and Expected
Charge sitting alongside it, where a bare "Battery" would be ambiguous.

`currentIntent` (and therefore the Ready By sensor) is confirmed to go `null` once not actively
plugged in/managed, while `intents` (the recurring weekly target schedule) and the vehicle's own
`chargeState` (battery %, range, etc.) persist regardless - confirmed live across an unplug.
Enode-sourced fields can lag reality (`chargeState.lastUpdated` stayed stale with
`refreshRequested: true` immediately after unplugging in one observed case) - this is a known
staleness characteristic of the underlying third-party data source, not a bug in how it's read
here.

## Timezone resolution (`helpers.py`)

`resolve_timezone` matters for correctness, not just tidiness: computing "today"/"this month" in
UTC would misattribute the first hour of each local day during BST, corrupting the month-to-date
sensors' totals right around a reset. Falls back to HA's configured default if the charger's
reported timezone is missing or unrecognized.

## Diagnostics (`diagnostics.py`)

`entry.data` (email/password) is left out entirely rather than included-then-redacted - it adds
nothing for debugging beyond "which account", already visible elsewhere in HA's UI. Only the
coordinator's `PodHomeCharger`/`PodHomeCharge` dataclasses are included (no token fields).

Must never reference `entry.runtime_data.api` or `.api._auth` - `PodHomeAuth` holds live
Firebase `_id_token`/`_refresh_token` as plain instance attributes with no redaction list
covering them; they should simply never be reachable from this file.

## Firebase API key (`podpoint_mobile_api/const.py`)

Not a secret credential - Google's own guidance is that Firebase Web API keys are safe to ship
client-side; the security boundary is Firebase's own project-level rules, not key secrecy. Same
pattern used by other Firebase-backed HA integrations/libraries (e.g. `anova-wifi`, a dependency
of the HA core `anova` integration).

## Auth token lock (`podpoint_mobile_api/auth.py`)

`PodHomeAuth` is shared by one coordinator polling several chargers concurrently
(`asyncio.gather`). Without `_token_lock`, two concurrent callers could each see an
expiring/missing token at once and both fire a refresh/sign-in request, racing to set
`_id_token`/`_refresh_token` with last-write-wins.

## 401/403 vs. other errors (`podpoint_mobile_api/client.py`)

A 401/403 from `mobile-api.pod-point.com` itself (session/token revoked server-side) raises
`PodHomeAuthError`, the same exception type as a Firebase sign-in/refresh failure - so callers
treat both as "needs reauth" uniformly, rather than the mobile-api case being swallowed as a
generic, often-non-fatal `PodHomeApiError`.

## Target Charge / Ready By as settable entities (`number.py`, `time.py`, `entity.py`)

The first real write path in the project. Explicitly **not called against the account** as part
of building it - the write endpoint's verb and body are confirmed via the public OpenAPI schema
(`GET /api-json` on `mobile-api.pod-point.com`), not by exercising it live, same distinction the
project draws everywhere else between "confirmed via schema" and "confirmed live". CLAUDE.md's
write-endpoint rule names `charge-overrides`/`remote-lock` specifically, but
"changes what a real vehicle actually charges to and by when" is exactly the kind of real effect
that rule exists to guard - so the same discipline applies here even though it isn't literally
one of the two named endpoints. Testing this for real, live, against the account is something to
do deliberately once deployed, not something done as part of writing the code.

Replaces (not adds alongside) the two read-only sensors that existed before - same unique_id/
translation_key, so this is a re-platforming from `sensor` to `number`/`time`, not a new parallel
pair.

**Target Charge and Ready By write through two different, unrelated endpoints - not the same
one.** The first version of this design routed both through the per-day intents endpoint
(`PUT .../vehicles/{vehicleId}/intents`), on the reasoning that `chargeKWh`/`chargeByTime` are
the only settable fields visible in that endpoint's schema. Directly challenged (correctly):
"are we sure the app isn't writing the percentage directly? this is a vehicle-level setting" -
and checking the OpenAPI schema properly turned up `UpdateChargeStateDtoImpl`
(`batteryCapacity`/`chargeLimitPercent`), reachable via `PATCH .../vehicles/{vehicleId}` - a
dedicated, vehicle-level, percentage-native endpoint that's a much more direct match for what
Target Charge actually is, with no `required` fields at all on any of the three nested request
schemas (a genuine partial update - `chargeLimitPercent` alone is a valid body). Switched to it:

- **Target Charge** (`number.py`) now calls `async_set_vehicle_charge_limit()` → `PATCH
  .../vehicles/{vehicleId}`, body `{"vehicle": {"chargeState": {"chargeLimitPercent": value}}}`.
  No unit conversion, no day-of-week fan-out, no dependency on Ready By or
  `battery_capacity_kwh` at all - it's now a fully independent, much simpler write.
- **Ready By** (`time.py`) still goes through the per-day intents endpoint. Re-checked properly
  after being directly challenged ("it's again a vehicle-level setting") by searching every
  schema for any settable time/charge-related field, not just the ones already noticed - nothing
  else turned up. `currentIntent.readyByTime`, the only other schema field with "ready" in its
  name, is confirmed read-only, never a request body anywhere in the spec. So no, this one
  really doesn't have a Target-Charge-style shortcut.
- Every `VehicleIntentEntryDtoImpl` entry requires `chargeKWh` alongside `chargeByTime` even
  though Ready By has no reason to change it. **An earlier version of this write computed a
  fresh `chargeKWh` from the vehicle's current `charge_limit_percent`/`battery_capacity_kwh`
  every time - wrong, caught by the same challenge.** That rested on an unproven assumption
  (`chargeKWh = capacity × target%`, independent of the vehicle's *current* battery level -
  plausible from the values observed live so far, never rigorously confirmed against a real
  change in current level at a fixed target) and, regardless of whether that assumption is even
  correct, meant actively synthesizing and pushing a value for a field this entity has no
  business changing. Fixed to echo back `vehicle.intent_charge_kwh` - the literal value last
  read from `intents.details[].chargeKWh` - instead, sidestepping the question of what the field
  actually means entirely; only `chargeByTime` is ever intentionally changed by this entity.
  `PodHomeVehicleIntentsWriteMixin._async_write_intents()` (kept factored out despite now having
  only one caller, in case another entity needs the same shape later) is unaffected by this -
  it's a thin wrapper either way, the fix is in what `time.py` passes it.
- Still fans the unchanged value across all 7 `DAY_OF_WEEK_OPTIONS` per write - not because the
  app is confirmed to do the same thing internally (it isn't - only the *read* side is confirmed
  to always show 7 identical entries), but because this is a `PUT` (full-resource-replace
  semantics), so sending back the complete 7-entry array we last read is the safer choice
  regardless: sending fewer risks the missing days' entries being dropped entirely, not merely
  left unchanged.
- Reads/writes `intent_charge_by_time` (`intents.details[].chargeByTime` - a plain "HH:MM:SS"
  local string, exactly what `TimeEntity` holds), not the old sensor's `ready_by`
  (`currentIntent.readyByTime` - Smart Charging's own live-resolved instant for the *current*
  session, which can lag a poll or two behind a just-written change, and carries a date that
  `chargeByTime` doesn't). Reading and writing the same underlying field avoids the display ever
  looking inconsistent with what was just set.

Genuine remaining uncertainty, not resolved by the schema alone: whether the app's own Target
Charge control *actually* calls the direct `chargeLimitPercent` endpoint rather than the intents
one is inferred from `chargeLimitSource`'s confirmed `"user"` value and the endpoint's shape
being a much more direct match - not confirmed by an actual capture of the app's traffic. Worth
keeping in mind as the reason this still needs a real, deliberate live test before being trusted,
same as the rest of this section already says.

`PodHomeVehicleEntity` gained a `ppid` property (re-derived live, same reasoning as `vehicle`)
since both write endpoints' URLs are scoped by `ppid` even though what they configure is about
the vehicle - delegated control is granted per-charger, not per-vehicle (same point made earlier
about the intents endpoint's URL shape, back when only the read side existed).

## Mode-conditional entities (`entity.py`, `__init__.py`)

Four entities only make sense in Smart Charging mode - Ready By/Target Charge/Expected Charge
(schedule optimization and prediction concepts), and Electricity Rate (tariff-aware optimization,
which Smart Charging requires a single-rate or two-rate tariff to even do - see
`smart_charging_supported` below). Nothing is Basic-mode-only - `Schedule` (both the sensor and
the new calendar) already adapts to whichever mode is active rather than needing to be split into
two entities; everything else isn't mode-specific at all.

Charge Priority was originally included here too (assumed to only mean anything while Smart
Charging is managing the charger) - **wrong, corrected by the user directly from the app**: it's
always viewable and changeable regardless of Charging Mode. Removed from `_MODE_GATED_ENTITIES`
and its `entity_registry_enabled_default = False` override - it now behaves like any other
always-on entity (Charging mode, Status, etc.), not like the four Smart-Charging-only ones above.

**Hide mechanism: the entity registry's `disabled_by`, not deletion or `available`.** Confirmed
via HA's own dev docs and core source
([entity_registry_disabled_by](https://developers.home-assistant.io/docs/entity_registry_disabled_by/),
[entity_registry.py](https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/entity_registry.py)):
`entity_registry.async_update_entity(entity_id, disabled_by=RegistryEntryDisabler.INTEGRATION)`
(and `disabled_by=None` to re-enable) is the standard, documented way for an integration to say
"this entity doesn't currently apply" - entities stay registered (history/statistics preserved
across a mode switch) but disappear from the default entity list and stop being polled while
disabled. `_async_apply_disabled_state()` in `entity.py` only ever touches entities it disabled
itself (checks `disabled_by == RegistryEntryDisabler.INTEGRATION` before re-enabling, and only
disables when `disabled_by` is currently `None`) - never stomps on a disable the user or another
integration set.

`_MODE_GATED_ENTITIES` is a small manifest of `(platform_domain, unique_id_suffix, scope)`
tuples in `entity.py`, not a per-entity mechanism, since the reconciliation function
(`async_sync_mode_gated_entities()`) needs to compute entity_ids via
`registry.async_get_entity_id()` without importing every platform module. Run once from
`__init__.py`'s `async_setup_entry`, after `async_forward_entry_setups()` (entities must be
registered before they can be looked up), and again on every coordinator update via
`coordinator.async_add_listener()` - Charging Mode can change any time from the app, not just at
startup, so this can't be a one-shot startup check.

## Charge Priority select (`select.py`)

Preferences (`chargingStrategy`/`maxPrice`) live under `GET/PATCH /smart-charging/delegated-
controls/{ppid}/preferences` - charger-scoped, so this entity is on the charger device, not the
vehicle. `MIN`/`MAX` themselves are confirmed via the account's public OpenAPI schema, correcting
an earlier version of this doc/`pod-point-new-api-findings.md` that guessed `lowestCost`/
`completeCharge` - those turned out to be the app's own display labels, not the wire values.
Write confirmed via the schema as a genuine partial update (`SmartChargingPreferencesDTO` has no
required fields) - not yet exercised against a real account, same discipline as Target Charge/
Ready By.

### Read side was wrong: reads `maxPrice`, not `chargingStrategy`

The first version of this entity read `chargingStrategy` directly, on the assumption the backend
would echo back whatever the app last set. Live testing proved that wrong: across four separate
tests (toggle to "prioritise full charge", re-probe; toggle to "lowest cost", re-probe; switch to
Basic Charging, re-probe; switch back to Smart Charging, re-probe), `chargingStrategy` **never
once appeared** in a `GET .../preferences` response - not `null`, genuinely absent from the JSON
every time - while `maxPrice` landed on an *exact* match of one of this account's own tariff
rates every time, and persisted unchanged through the two mode switches:

| App setting | `maxPrice` returned | Matches tariff... |
|---|---|---|
| Prioritise full charge | `0.2942` | the tariff's **peak** rate |
| Prioritise lowest cost | `0.0863` | `tariffs.json`'s `cheapestUnitPrice` exactly |
| (switched to Basic, then back to Smart) | `0.0863` (unchanged) | still the cheapest rate |

That's real, repeated, exact-match evidence - not a guess. `charging_priority_label()` in
`helpers.py` now derives the label by comparing `charger.max_price` against `charger.tariff_windows`
(`math.isclose()`, not exact equality - float round-tripping through JSON isn't guaranteed
bit-exact, and a tariff change between the preference being set and the read would also
legitimately produce a near-but-not-exact match) - `<=` the cheapest rate → "Lowest cost", `>=`
the priciest → "Complete charge", anything else → unknown (don't guess).

**The write side is unchanged** - still `PATCH {"chargingStrategy": "MIN"|"MAX"}`, not `maxPrice`
directly. Current working theory, consistent with everything observed: the app PATCHes the
shorthand `chargingStrategy`, the backend resolves it against the tariff and stores the result as
`maxPrice`, and `chargingStrategy` itself is write-only - consumed and translated, never
persisted or echoed back. Both `GET` and `PATCH` share the exact same `SmartChargingPreferencesDTO`
schema (no required fields on either), which is consistent with this but doesn't prove it -
confirming the write side for certain would need a live write test (PATCH `chargingStrategy` then
re-`GET` to see if `maxPrice` moves), which hasn't been done - same NOT YET TESTED discipline as
Target Charge/Ready By, so `charging_strategy_from_label()`'s MIN/MAX↔label pairing is still an
inferred guess, unlike the read side which now rests on real repeated evidence.

Also checked and ruled out: a dedicated live "current session" endpoint that some other Pod Point
API surface might expose this through more directly - none exists. Every `smart-charging`/
`preference`/`intent` path in the public OpenAPI schema was enumerated; `SmartChargingPreferencesDTO`
is the only schema anywhere with anything price/strategy-shaped.

## Schedule calendar (`calendar.py`)

One `Schedule` calendar entity, not two, mirroring the `Schedule` sensor exactly - not mode-gated
(see above), branches on `schedule_mode()` internally instead. Originally planned as a
Basic-mode-only entity showing just the manual schedule; caught directly by the user ("surely we
just need one 'schedule' calendar entity that can be used for both modes") before being built.

The two branches build events from genuinely different data shapes, so it's a real if/else, not
shared code:
- **Basic Charging**: `expand_manual_schedule_events()` in `helpers.py` expands
  `manual_schedule_windows`' weekly recurrence (ISO weekday `start_day`/`end_day`, matching
  Python's `isoweekday()` per the existing `_week_minutes()` convention) into concrete dated
  occurrences overlapping the requested range. Validated against this account's real
  `manual_schedules.json` before considering it done (not just the schema): 7 real entries
  (`startDay == endDay`, `00:30-05:30`, all active) produce exactly 7 events, one per day, over
  a 7-day range. The cross-midnight/week-wrap handling (a window whose `end_day` differs from
  `start_day`, or whose `end_time` isn't after `start_time` on the same day) stays
  defensive/unvalidated against real data either way - no captured window has ever shown that
  shape - but is covered by a synthetic offline test (a Sunday-night-to-Monday-morning window
  correctly produces two overlapping occurrences across a 7-day range, not one, since the
  recurrence crosses two different Sundays).
- **Smart Charging**: `smart_schedule_events()` in `helpers.py` builds events directly from
  `smart_schedule_windows`' own absolute UTC timestamps, clamped to the requested range - no
  recurrence needed, since these already describe the current session concretely. `PLUGGED_IN`
  entries (a point-in-time marker, not a range) never produce an event, same as
  `current_smart_schedule_window()`'s existing handling. **Not validated against a fresh live
  response** - this account wasn't mid-session while this was built (`smart_schedule_active.json`
  currently 404s with `NO_ACTIVE_CHARGING_SESSION`), so the offline test instead replays the
  exact real values captured earlier this session (`PLUGGED_IN`/`PAUSED`/`CHARGING` with
  `tariffRate: "OFF_PEAK"`) rather than a value pulled from a file that exists right now - a
  gap worth closing for real once there's an active session to capture, not something closeable
  from what was available while this was written.

Both branches return plain `(start, end, summary)` tuples from `helpers.py` (kept HA-free, no
`CalendarEvent` import there) - `calendar.py` wraps them into real `CalendarEvent` objects.

## `smart_charging_supported` (`coordinator.py`, Charging Mode sensor)

Piggybacks on the tariffs fetch already made every 6h for `tariff_windows` - same already-parsed
`data[0]` entry, zero extra requests, just a second small parse
(`_parse_smart_charging_supported()`) of a field that was already being fetched and discarded.
Told directly by the user, confirmed from firsthand experience running the real account: Smart
Charging only works with a single-rate or two-rate tariff - selecting a tariff with more rates,
or one where the supplier controls charging directly, reverts the account to Basic Charging
automatically. Exposed as a `smart_charging_supported` attribute on the Charging Mode sensor
(alongside the existing `raw_delegated_control_status`/`status_effective_from`) purely as
context for *why* a charger might be stuck in Basic Charging - `delegatedControl.status` (via
`schedule_mode()`) remains the actual source of truth for current mode, this flag doesn't drive
any logic itself.

## api3 - a second, older backend generation, proxied through mobile-api (`client.py`, `coordinator.py`)

The legacy community `pod_point` integration's "Current Charge Energy" sensor (live kWh for an
in-progress session) has no equivalent on any of mobile-api's own endpoints - checked
exhaustively against the account's full public OpenAPI schema, nothing matches. Finding where it
actually lives took several wrong turns worth recording, since the end state (api3, proxied
through mobile-api, no second auth system needed) isn't obvious from the API surface alone:

- First assumed `api.pod-point.com` (the legacy v4 API the community integration talks to
  directly) was simply retired - every direct request to it (root, `/v4/users`, `/docs/`, any
  path tried) returned an identical generic CloudFront/S3 `Access Denied`, never a real
  application response. Turned out to be inconclusive either way - that response shape is
  consistent with a WAF blocking non-app traffic just as much as with the backend being gone.
- Confirmed `api.pod-point.com` is still real, versioned `/v5/` (one version past the community
  integration's `/v4/`), and that mobile-api proxies it at `/api3/v5/...` (ticket EDA-1286).
  Confirmed independently in mobile-api's own OpenAPI schema, which lists several `/api3/v5/...`
  routes. "api3" is Pod Point's own name for this backend generation, not a version number - the
  schema puts it at v5, not v3.
- Guessed at `podId` for `GET /api3/v5/pods/{podId}` using every id already available in this
  project (ppid, unitId, both as int and str) - all four 404'd, with a JSON:API-shaped error
  body distinct from mobile-api's usual NestJS shape (real, if inconclusive, evidence the proxy
  reached a genuine upstream rather than mobile-api rejecting the route itself).
- Resolved for real by pulling the actively-maintained `podpointclient` Python library's current
  source directly (github.com/mattrayner/podpointclient, updated post the account's June-2023
  Pod Point auth changes - not the old, unmaintained HA component built on top of it years ago).
  It already targets `mobile-api.pod-point.com/api3/v5` itself, and its source lays out the real
  flow: `POST /api3/v5/sessions` (needs the plain email/password again in the body, alongside the
  Firebase bearer token every other call here uses - the api3 session model predates Firebase
  auth and apparently was never fully migrated off needing both) returns `{"sessions":
  {"user_id": ...}}`; that `user_id` - a third identifier, distinct from ppid/unitId/the Firebase
  uid - is what `GET /api3/v5/users/{userId}/pods` and `GET /api3/v5/users/{userId}/charges`
  actually need. Confirmed live: `include=charges` on the pods list is accepted but always
  returns empty regardless of an active session - charges are a genuinely separate endpoint.

`async_api3_charges()` returns entries newest-first; the live session (if any) is the first entry
with `ends_at: null` - confirmed live, matching a real in-progress test session exactly
(`kwh_used` climbing, `starts_at` matching the observed start time to the second). `duration` and
`charging_duration` are not populated by the API on an open entry (confirmed live: both 0/null) -
computed client-side instead (`now - starts_at`) in `_async_refresh_api3_charges()`.

`current_charge` on `PodHomeCharger` holds this - same `PodHomeCharge` shape as `latest_charge`
(mobile-api's own /charges, confirmed live to only ever show *finished* sessions - an
in-progress one simply isn't in that list yet, which is the whole reason this was worth chasing
down). The three Last Charge sensors (`sensor.py`) now read `current_charge or latest_charge` -
live and climbing while a session is active, falling back to the last finished one otherwise.

Two-tier staleness in the coordinator, both non-fatal (a failure just means current_charge stays
unavailable that poll, not that the update fails): the session/pod-id mapping
(`_async_refresh_api3_account()`) is account-level and rarely changes, refreshed on the same
conservative cadence as firmware/tariffs (6h) - only stamped as fetched when both the session and
pods calls succeed, so a partial failure retries next poll rather than waiting out the full
interval with an empty mapping. The charges fetch itself (`_async_refresh_api3_charges()`) uses
the same charging-aware/slow-fallback gating already established for mobile-api's own /charges
and month-to-date figures (every poll while a charger was charging last poll, else on
`CHARGE_STATS_REFRESH_INTERVAL`).

The coordinator now holds the account's plain email/password for the lifetime of the config
entry (passed in from `__init__.py`, alongside constructing the Firebase-authenticated `api`
client that already needed them) - not a new category of credential handling, `PodHomeAuth`
already retains both internally for the same reason (re-signing in when the refresh token itself
expires); this is the same values, just also needed one level up for the api3 session call.

Not yet run inside a real HA instance - same standing gap as everything above the API layer.

## Schedule calendar drops Paused blocks (`helpers.py`)

`smart_schedule_events()` originally built a calendar event for every `PAUSED` window alongside
`CHARGING` ones. Per the user directly: Paused is Smart Charging's default "not doing anything
right now" state, not a planned block worth showing as its own calendar entry - cluttering the
calendar with it defeats the point of a calendar (a plan of what *will* happen), and it's not
information the Basic Charging branch (`expand_manual_schedule_events()`) surfaces either - that
one already only ever shows active windows, never the gaps between them. Now only `CHARGING`
windows produce an event, matching that same shape.

## Connection-level retry (`coordinator.py`, not the client)

Confirmed live: a genuine DNS-timeout burst against `mobile-api.pod-point.com` (three occasions
in one overnight test session, roughly evenly spaced) left every `pod_home` entity unavailable
until the next poll - the coordinator's top-level `/chargers` call raised `UpdateFailed`
immediately on the first failure, with no retry anywhere before that.

**Correcting an initial framing mistake**: the recovery actually observed that morning was ~61s
(one `FAST_POLL_INTERVAL` cycle), not "up to 5 minutes" as first stated here - the coordinator
happened to already be in fast-poll mode (recent charging activity within
`RECENT_CHANGE_WINDOW`), so the next poll was only a minute away. The 5-minute figure is the real
worst case, but only applies once the coordinator has drifted to `SLOW_POLL_INTERVAL` (a quiet
charger) - that's the scenario this retry actually earns its keep in; a recently-active charger
is already cheaply self-healing via its own fast poll cadence, where a few seconds of retry adds
comparatively little.

**Placement - initially added to `podpoint_mobile_api/client.py`, then moved here.** Per the
user directly: retry/backoff is conventionally the integration's (coordinator's) responsibility,
not the client library's - HA's own `DataUpdateCoordinator` pattern already treats "try again
next poll" as the default resilience mechanism, and layering a few quick in-poll retries on top
of that is a deliberate coordinator-level enhancement, not something a generic client should
decide unilaterally for every caller. Concretely, `podpoint-mobile-api` is a standalone,
reusable package also used by the `scratch/` probe scripts - a human watching diagnostic output
in real time wants a fast, clear failure there, not a few extra seconds of hidden retry masking
what's actually happening. A "thin" client (raises immediately, no hidden timing behaviour) is
also just easier to reason about and test. So the client stayed exactly as it was;
`_async_with_connection_retry()` lives in `PodHomeDataUpdateCoordinator` instead.

Scoped narrowly to the one call whose failure is fatal to the whole poll: `GET /chargers` (raises
`UpdateFailed` directly). Everything else in this file already goes through `_safe_call`, which
is non-fatal on its own (a failure there just leaves that one field at its last-known value, not
a full outage) - extending retry to every `_safe_call` site would mean changing that helper's
calling convention across ~20 call sites (from a pre-built coroutine to a zero-arg factory, since
a coroutine can only be awaited once and retry needs to be able to call it again) - treated as a
separate, larger decision rather than folded into this fix silently.

Up to `CONNECTION_RETRY_ATTEMPTS` (3) attempts, `CONNECTION_RETRY_DELAY_SECONDS` (2) apart.
Deliberately narrow about what gets retried: only a connection-level failure (couldn't reach the
server at all - DNS/timeout/connection-refused, what the client wraps as `PodHomeApiError(0,
...)`) is retried. A genuine HTTP error response (4xx/5xx - the request *did* reach the server)
is not - retrying a real 404/500 immediately essentially never changes the outcome. An auth
failure is raised as `PodHomeAuthError`, a different exception type this retry loop doesn't catch
at all - retrying without addressing the underlying credential issue would be pointless.

Not extended to `PodHomeAuth`'s own Firebase sign-in/refresh calls (a separate host,
`identitytoolkit.googleapis.com`/`securetoken.googleapis.com`) - the confirmed incident was
specifically the mobile-api host being unreachable, not a Firebase auth failure, so that's a
known, related, but out-of-scope gap rather than something this fix claims to cover.

Verified offline (no live account or real HA install needed - `homeassistant.*` stubbed out just
enough to import `coordinator.py`, then a bare `PodHomeDataUpdateCoordinator.__new__(...)`
instance, since the retry helper touches no other instance state): connection failure twice then
success retries and returns the eventual result; a real HTTP error status is not retried; a
`PodHomeAuthError` is not caught/retried at all; exhausting every retry on a persistent
connection failure re-raises the last one.

## Entity category corrections (`sensor.py`, `binary_sensor.py`)

Per the user directly, three small reclassifications:

- **Charge time remaining** and **Fully charged** moved from Diagnostic (disabled by default,
  bundled with the other debug sensors - Power Delivery State/Charge Rate/Max Current, which
  stay as Diagnostic/disabled) to Standard, enabled by default - both are values someone would
  actually want on a dashboard, not just debug-only signals, even though Fully Charged also
  happens to back Charge Status's SuspendedEVSE handling internally.
- **Last Charge Duration**'s default *display* unit changed to minutes - a raw seconds count
  (e.g. `14157`) isn't a sensible default display for something typically hours long. First pass
  did this wrong: changed `native_unit_of_measurement` to minutes and rounded the value to fit -
  the user caught it, correctly: that throws away real precision from the stored/recorded value
  for a purely presentational concern. Fixed properly with `suggested_unit_of_measurement`
  (minutes) alongside `native_unit_of_measurement` staying seconds - HA converts for *display*
  automatically, the native value (and what the recorder/history stores) stays the exact,
  unrounded `PodHomeCharge.duration` in its own real unit.
- **Charging mode**'s values shortened from the app's own full labels ("Smart Charging"/"Basic
  Charging") to just "Smart"/"Basic" - per the user directly, repeating "Charging" is redundant
  on an entity already named Charging Mode. `SCHEDULE_MODE_SMART_CHARGING`/
  `SCHEDULE_MODE_BASIC_CHARGING` (`const.py`) are used as the actual comparison values throughout
  `helpers.py`/`entity.py`'s mode-gating/`calendar.py` - only the two constants' string values
  changed, not their names, so every comparison site kept working unchanged.

## Fully Charged (vehicle) removed entirely

Reconsidered right after being promoted to Standard/enabled-by-default (see above) - per the
user directly, not convinced it earns a standalone entity at all: it's never actually been
observed `true` on this account (every real capture this session shows `isFullyCharged: false`,
so its real-world behavior - does it stay true, reset on the next cycle - is genuinely unknown),
and what it does contribute is already surfaced indirectly via Charger Status showing "Finished"
(it's the strongest signal `is_momentarily_finished()` uses internally). As its own entity it
mostly duplicated what Charger Status + Battery Level at 100% already implied, not new
information - unlike Charge Time Remaining, which stayed Standard since nothing else provides
that value. `PodHomeVehicle.is_fully_charged` itself is untouched - still fetched and still used
internally by Charge Status, only the entity wrapping it (`binary_sensor.py`) is gone.

## Tariff rate humanized in the Schedule calendar (`helpers.py`)

The Schedule calendar's Smart Charging event summaries showed the raw `tariffRate` value
verbatim - e.g. "Charging (OFF_PEAK)". Per the user directly: SHOUTY_SNAKE_CASE reads badly in a
UI meant for humans. `humanize_tariff_rate()` (same pattern as the existing
`humanize_model_style()`) does a generic `"_" -> " "` transform rather than a hand-picked mapping
- `tariffRate` isn't a closed, documented enum (this account has only ever shown `OFF_PEAK`
live), so a lookup table would just be guessing at values never observed. Sentence case ("Off
peak"), not Title Case ("Off Peak") - corrected after an initial version got this wrong: Home
Assistant's own convention for entity/state display strings only capitalizes the first word.
Only the calendar's display string changed - `PodHomeSmartScheduleWindow.tariff_rate` itself
stays the raw wire value, since nothing else reads it.

## `charge_status()` renamed to `charger_status()` throughout

Per the user directly: the entity itself was already renamed "Charge status" -> "Charger status"
earlier this session (see above), but the internal function/constant/class names never followed -
`charge_status()`, `CHARGE_STATUS_*` (`const.py`), `PodHomeChargeStatusSensor`. Renamed
everywhere for consistency: `charger_status()`, `CHARGER_STATUS_*`, `PodHomeChargerStatusSensor`,
and every "Charge Status" comment/docstring - a mechanical rename, no behavior change. Also
renamed the persisted `Store` filename (`..._charge_status` -> `..._charger_status`) for the same
consistency - unlike the rest of this rename, that one has a real, if minor, consequence: it
orphans any already-persisted sticky Charge Status timestamps on the next restart. Not an error -
the existing "corrupt/missing store" handling already starts fresh gracefully - but worth noting
plainly since it's a one-time state reset, not a pure no-op rename like everything else here.

## `vehicle.is_charging` dropped as a Charger Status signal

`charger_status()` and the sticky-charging signal both used to trust `chargingState ==
CHARGING_STATE_CHARGING` OR the linked vehicle's own `isCharging` flag (from
`/smart-charging/delegated-controls/vehicles`, via Enode). Caught by the user directly: that
flag isn't scoped to any one charger - `PodHomeVehicle`'s own docstring already noted it
"persists across plug/unplug", and the API separately exposes `isPluggedInToThisCharger`
specifically because `isCharging` doesn't imply "charging via this charger" on its own (both
confirmed live, `scratch/output/smart_charging_chargers_and_vehicles.json` and
`output_plugged_in`/`output_unplugged`). A vehicle linked as primary to more than one charger on
the account - or simply charging away from home - could read `isCharging: true` while sitting
next to a charger this integration reports as idle, incorrectly showing that charger as
Charging. First proposed fix was to additionally gate on `isPluggedInToThisCharger`; the user
pushed further and asked why `chargingState` alone (reported per-charger by the API) isn't
sufficient on its own - no prior reasoning in this file or in code comments justified the
vehicle-flag fallback ever being added, so it was dropped entirely rather than patched.
`charger_status()`'s Charging branch, the coordinator's `charging_started_at` sticky signal, the
account-wide `any_charging_last_poll`/`was_charging_last_poll` gates, and
`is_momentarily_finished()`'s internal charging check all now use `chargingState ==
CHARGING_STATE_CHARGING` alone. `is_momentarily_charging()` itself was removed - once it dropped
the vehicle-flag fallback it was just an equality check wrapped in a name, so its three call
sites now inline the comparison directly instead. `is_momentarily_finished()` keeps
`vehicle_is_fully_charged`/`vehicle_ready_by` unchanged - a vehicle's battery/target isn't an
ambiguous-across-chargers concept the way "is it charging right now" is, so those two didn't need
the same treatment. `PodHomeVehicle.is_charging` itself is untouched - still fetched and still
used by the Vehicle Charging binary sensor (a vehicle-scoped entity, where the vehicle's own
global state is exactly what's wanted), only its use inside charger-scoped logic was removed.

## SuspendedEV confirmed as the real "just finished" signal; schedule/ready-by inference dropped

Real evidence, not inference this time: the user shared their original Home Assistant
automation's full YAML. Its "Charged" timestamp was stamped on transition **to `suspended-ev`**
specifically, unconditionally - no schedule/ready-by check at all (see
`legacy-charge-status-template` in persistent memory for the full automation and the corrected
real sequence: `Charging -> SuspendedEV (Charged, immediately) -> SuspendedEVSE (persists for the
rest of the window)`). `SuspendedEVSE` was never the trigger for "Charged" - it only ever showed
up afterwards. This flips what `is_momentarily_finished()` had been checking: it only gave
`SuspendedEVSE` the sticky-Finished treatment, gated behind a schedule-state/ready-by inference
that had no real evidence behind it (never validated against real data, unlike everything else
in this file).

Also caught: `vehicle_is_fully_charged` (Enode's `isFullyCharged`, the function's first/strongest
check) has the identical charger-scoping flaw already fixed for `vehicle.is_charging` below - it
comes from the same non-charger-scoped `chargeState` block, so a vehicle already full via a
different charger (or before ever being plugged in here) would wrongly stamp *this* charger's
`charge_finished_at`. Combined with it never once having been observed `true` on this account
even mid-plateau, dropped entirely rather than gated.

Net result: `is_momentarily_finished()` removed entirely (it collapsed to a single equality
check, `charging_state == CHARGING_STATE_SUSPENDED_EV`, once both were gone - same fate as
`is_momentarily_charging()` below, inlined at its one coordinator call site instead of kept as a
named wrapper around nothing). `status()` (see the rename below) now ORs
`charging_state == CHARGING_STATE_SUSPENDED_EV` directly into its Finished branch, alongside the
existing `_is_finished_sticky()` timestamp comparison - `SuspendedEV` is trusted immediately and
unconditionally, `SuspendedEVSE` still only shows Finished via the sticky comparison (i.e. only
once a prior `SuspendedEV` poll actually set it).

**Real, accepted gap, stated plainly**: if a poll happens to land exactly when `chargingState`
has already moved past `SuspendedEV` straight to `SuspendedEVSE` (a fast transition missed
between polls), there's no fallback left - Charger Status shows Paused until the next
Charging/Unplug event, not Finished. The dropped schedule/ready-by fallback used to paper over
this with unconfirmed inference; the user's call was that accepting a known, narrow edge case is
better than guessing. `current_schedule_state()`, `current_smart_schedule_window()`,
`manual_schedule_state()`, `_week_minutes()`, and `_now_in_manual_window()` were then dead code
(nothing else called them) and removed from `helpers.py`.

Ordering note, also settled directly with the user: `Preparing`/`Finishing` stay checked *after*
the Finished check in `status()`, not before, even though the user initially proposed the
opposite (their recollection of "Preparing" possibly meaning "a command was issued from the app
but hasn't reached the charger yet" turned out on reflection to have been about the legacy
integration's separate `Pending` state - unrelated to `chargingState` - not `Preparing`). Checking
Finished first is what's confirmed live and covered by the offline test script: a genuinely
finished charge can wander through `Finishing`/`Preparing` before settling, and must stay
Finished through that wandering, not flip to showing the transient wire value.

## `vehicle.is_charging` dropped as a Charger Status signal

`status()` and the sticky-charging signal both used to trust `chargingState ==
CHARGING_STATE_CHARGING` OR the linked vehicle's own `isCharging` flag (from
`/smart-charging/delegated-controls/vehicles`, via Enode). Caught by the user directly: that
flag isn't scoped to any one charger - `PodHomeVehicle`'s own docstring already noted it
"persists across plug/unplug", and the API separately exposes `isPluggedInToThisCharger`
specifically because `isCharging` doesn't imply "charging via this charger" on its own (both
confirmed live, `scratch/output/smart_charging_chargers_and_vehicles.json` and
`output_plugged_in`/`output_unplugged`). A vehicle linked as primary to more than one charger on
the account - or simply charging away from home - could read `isCharging: true` while sitting
next to a charger this integration reports as idle, incorrectly showing that charger as
Charging. First proposed fix was to additionally gate on `isPluggedInToThisCharger`; the user
pushed further and asked why `chargingState` alone (reported per-charger by the API) isn't
sufficient on its own - no prior reasoning in this file or in code comments justified the
vehicle-flag fallback ever being added, so it was dropped entirely rather than patched.
`status()`'s Charging branch, the coordinator's `charging_started_at` sticky signal, the
account-wide `any_charging_last_poll`/`was_charging_last_poll` gates, and (at the time)
`is_momentarily_finished()`'s internal charging check all now use `chargingState ==
CHARGING_STATE_CHARGING` alone. `is_momentarily_charging()` itself was removed - once it dropped
the vehicle-flag fallback it was just an equality check wrapped in a name, so its three call
sites now inline the comparison directly instead. `PodHomeVehicle.is_charging` itself is
untouched - still fetched and still used by the Vehicle Charging binary sensor (a vehicle-scoped
entity, where the vehicle's own global state is exactly what's wanted), only its use inside
charger-scoped logic was removed.

## `charger_status()` renamed to `status()`; raw chargingState passthrough renamed to Charging state

Per the user directly: rename the derived entity (and every internal reference - function,
constants, class, unique_id, translation_key, Store filename) back down to just "Status", and
rename the existing raw `chargingState` passthrough sensor (previously *also* called "Status",
`unique_id` `_status`) to "Charging state" instead, matching the API's own field name. Swapped
identities rather than colliding: `PodHomeStatusSensor` (`_status`, `STATUS_OPTIONS`, was
`PodHomeChargerStatusSensor`/`_charger_status`) is now the derived entity;
`PodHomeChargingStateSensor` (`_charging_state`, `CHARGING_STATE_OPTIONS`, was
`PodHomeStatusSensor`/`_status`) is the raw passthrough, unchanged in behavior. `helpers.py`'s
`charger_status()` -> `status()`, `const.py`'s `CHARGER_STATUS_*` -> `STATUS_*`. The persisted
`Store` filename changed again too (`..._charger_status` -> `..._status`) - same real, if minor,
consequence noted for the previous rename: orphans any already-persisted sticky timestamps once
on next restart, self-healing not erroring, now the second such reset this project has caused.

## Partial revert: internal `status()`/`STATUS_*` renamed back to `charger_status()`/`CHARGER_STATUS_*`

Caught by the user directly, immediately after the rename above: `helpers.py` is a flat module
shared by both charger- and vehicle-scoped entities, so a bare `status()` function (and
`STATUS_*` constants in the equally flat `const.py`) is exactly the same collision risk the
entity-level rename had just fixed, one layer down - the moment a vehicle-scoped status concept
needs the same word, `status()` is already taken and ambiguous about which it means. The
entity-level rename doesn't have this problem (HA scopes entity names by device in the UI, so
"Status" under the charger device is unambiguous) and was kept as-is. Only the internal
Python-level names were reverted: `status()` -> `charger_status()`, `STATUS_*` -> back to
`CHARGER_STATUS_*`. Matches an existing pattern already in this codebase - `schedule_mode()`
returns the short "Smart"/"Basic" display values while its own name and constants
(`SCHEDULE_MODE_SMART_CHARGING` etc.) stay fully descriptive internally; same split now applies
to Status.

Also discussed and decided against: splitting entity platform files (`sensor.py` etc.) into
charger/vehicle subdirectories. Home Assistant's own convention (used by every core integration
and generated by `script.scaffold integration`) is one file per *platform*, not per device type -
`async_setup_entry` is looked up per-platform-file by HA's entity component loader, so a
device-type split would fight that mechanism rather than just be an unusual layout. This
project's existing `PodHomeCharger*`/`PodHomeVehicle*` class-name prefixes plus the two
`entity.py` base classes (`PodHomeEntity`, `PodHomeVehicleEntity`) already give the same
separation without breaking the convention.

## Energy/Cost this month now include the live in-progress session

Per the user directly, after discussing the legacy integration's `Total Energy` entity (see
below): rather than reproducing that lifetime-total approach - it turned out to be entirely
client-side, computed by the legacy integration paginating through the account's *entire* charge
history and summing `kwh_used`, not backed by any wire field - settle for making the existing
`Energy this month`/`Cost this month` sensors (already Energy-Dashboard-safe, `TOTAL_INCREASING`/
`TOTAL`) accurate in real time instead. `month_energy_kwh`/`month_cost_amount` come from
mobile-api's own `charge-statistics` aggregate, which - confirmed by the same gap that already
motivated building `current_charge` via api3 (mobile-api's own `/charges` list excludes a
currently open session until it closes) - excludes a live session's energy/cost until it
finalizes. Both sensors now add `current_charge`'s own running total on top when one exists,
guarded by a new pure helper, `current_charge_in_month()` (`helpers.py`, offline-tested): only
adds it if the session actually started within the current calendar month in the charger's local
time, so a session that started last month and is still open (crossing the boundary) doesn't get
wrongly attributed to the new month's figure.

## Total Energy (legacy `pod.total_kwh`) - investigated, not built

The user asked about reproducing the legacy `pod_point` integration's lifetime "Total Energy"
entity, reasoning it would help the Energy Dashboard given mobile-api's `/charges` excludes live
sessions. Checked the legacy integration's actual coordinator source (`pod-point-home-assistant-
component`, via `gh api`): `Pod.total_kwh` initializes to `0.0` in the model and is never parsed
from the wire at all - it's built up entirely client-side, by paginating through the account's
full charge history once (`perpage=50`, every page) summing `kwh_used` for `location.home is
True` entries, then incrementally adding only newly-seen charges each subsequent poll. No lifetime
total exists as a field anywhere in v4, api3, or mobile-api. api3's own `/charges` (already wired
up as `async_api3_charges`) is confirmed the same lineage/shape the legacy integration used
(`kwh_used`/`location.home`/`pod.id`, paginated), so a from-scratch backfill-and-persist version
of this is buildable, but real design/cost questions remain unresolved: a one-time full-history
paginated backfill (this account's been active since 2023, so potentially hundreds of requests
on first setup), then an incrementally-maintained running total persisted via `Store` (same
pattern as the sticky Charger Status timestamps) so restarts don't re-trigger the backfill. Not
pursued for now - the user opted for the simpler Energy/Cost this month fix above instead, which
solves the actual live-session gap without the backfill/persistence cost. Revisit if a genuine
lifetime-total need comes up later; the design sketch above is the starting point.

## Total Energy/Cost added; Energy/Cost this month reverted to finalized-only

The month-boundary problem above (a session spanning midnight has no confirmed way to split
between months) turned out to be specific to trying to make a *calendar-month-resetting* sensor
live-inclusive - it doesn't apply to a sensor with no calendar boundary at all. Per the user's
own proposed split:

- **Energy/Cost this month** reverted to a plain pass-through of `month_energy_kwh`/
  `month_cost_amount` (mobile-api's `charge-statistics`, finalized charges only) - matching what
  the app itself shows, same lag and all. `current_charge_in_month()` and the top-up logic from
  the previous two entries above are removed entirely - simplest fix for the boundary problem is
  not attempting it here at all.
- **New Total Energy/Total Cost** sensors: a running total *since Pod Home started tracking it*,
  not a true account-lifetime total the way the legacy community integration's own `total_kwh`
  was (see "Total Energy (legacy pod.total_kwh) - investigated, not built" above) - deliberately
  not backfilled via full-history pagination. `total_started_at` is set once, the first time a
  given ppid is seen with no persisted total yet (coordinator.py, in the main per-ppid loop), and
  exposed as a `tracking_started_at` attribute for transparency about what the number actually
  represents.

Incremental accumulation (`PodHomeDataUpdateCoordinator._accumulate_total_energy()`) reuses the
same `/charges` response already fetched for `latest_charge` each poll - no second API call.
Only entries with `endedAt` set (finalized) are ever added to the persisted running total; a
still-open session is never counted there, only shown live via `current_charge` added on top at
display time (same split as `latest_charge`/`current_charge` already established for Last
Charge). Per the user directly ("make sure we don't double count a session after completion"):
this is safe by construction, not just by care - a charge can't be both open (`current_charge`
present, `endedAt` null) and finalized (`endedAt` set) at the same time, so a session's energy is
counted exactly once, the instant it finalizes, never twice.

Watermarked by each ppid's most recently *counted* finalized charge's `endedAt`
(`_total_watermark_by_ppid`), so a poll never re-sums from scratch and never re-adds an
already-counted charge - only strictly newer entries get added, and the watermark only ever
moves forward (compared against a snapshot taken before each batch, not the live-updating value,
so out-of-order entries within one poll's batch - e.g. two sessions finalizing between polls -
can't cause an older-but-still-new entry to be wrongly skipped). Persisted via the same `Store`
file the sticky Charger Status signals already use (not a second Store) - extended schema, not a
rename, so no orphaning consequence this time.

Total Energy is the sensor recommended for the Energy Dashboard specifically (genuinely
monotonic, no calendar-boundary reset to worry about) - Energy this month remains a simple
"how much this month" glance value matching the app, not what the Dashboard needs.

## Total Cost dropped; Rewards balance + "Pod Point" account device added

Per the user directly: Total Cost wasn't wanted alongside Total Energy - removed entirely
(sensor, `PodHomeCharger.total_cost_amount`, the coordinator's cost-side accumulation and
persisted state), not just hidden. Total Energy is unaffected; the watermark/persistence
mechanism it shares with the removed cost tracking is untouched.

Added the account device discussed earlier (rewards balance investigation - confirmed
`GET /reward-wallet` has no per-charger dimension at all: empty schema `parameters`, no
charger/pod breakdown in the response, unlike `/charges` which is genuinely per-charger data
just delivered as one combined list - see the "charges nested under a charger" exchange).
`PodHomeAccountEntity` (`entity.py`) is a third entity base alongside `PodHomeEntity`/
`PodHomeVehicleEntity`, device-grouped under a single "Pod Point" device per config entry
(`DOMAIN, entry.entry_id`) rather than going device-less - explicitly named "Pod Point", not
"Pod Home", since account/rewards features are account-wide, not specific to the Home charger
product line (matches the still-open Pod Home vs Pod Point rebrand discussion). Device-less was
rejected the same way it was for `PodHomeVehicleEntity` earlier: an orphan entity is harder to
find/rename/disable as a group, and the legacy community integration's own account balance
entity is a real example of getting this wrong (inherits straight from `CoordinatorEntity`, no
device_info at all).

`PodHomeRewardsBalanceSensor` is registered once per config entry, directly via
`async_add_entities([...])` in `sensor.py`'s `async_setup_entry` - not through either
`async_setup_dynamic_chargers`/`async_setup_dynamic_vehicles` helper, since there's exactly one
of it per account, not one per discovered charger/vehicle. Backing data
(`PodHomeDataUpdateCoordinator.rewards: PodHomeRewards | None`) is fetched on the same 6h
`FIRMWARE_TARIFF_REFRESH_INTERVAL` cadence as firmware/tariffs/api3 account mapping - rewards
aren't obviously tied to charging activity the way charge-stats are, so no charging-aware
fast-path. `balance_gbp` is the primary value (native unit hardcoded `"GBP"`, not
`coordinator.currency` - the rewards scheme is its own fixed-currency system, not tied to the
account's billing currency); `balance_miles`/`balance_points` (same balance, other units) and the
allowance/payout-threshold figures are exposed as attributes rather than separate entities.

## Code-review fixes: live-session cost, single-rate Charge Priority, split Store, stale-cache gating

A `/code-review` pass surfaced eight real issues, all fixed:

- **Last Charge Cost showed £0.00 for an entire active session**: `current_charge.cost_amount`
  was set from api3's raw `energy_cost`, confirmed-live-0 on an open entry (same limitation
  `duration` was already worked around for) - the sensor's `is None` guard doesn't catch `0`.
  Now `cost_amount=None` on `current_charge` (cost genuinely isn't derivable live, unlike
  duration), so the sensor correctly goes unavailable instead of showing a false £0.00.
- **`charging_priority_label()` always said "Lowest cost" on a single-rate tariff**: when every
  window shares the same price, `min(prices) == max(prices)`, so `maxPrice` can't distinguish
  the two Charge Priority settings - the function checked the "Lowest cost" branch first, so it
  always matched. Now resolves to unknown in that case rather than guessing.
- **Diagnostics leaked vehicle PII and firmware serials**: `dataclasses.asdict(charger)` recurses
  into the `vehicle`/`firmware` nested dataclasses with no redaction. Both are now redacted
  (`**REDACTED**`) before being included.
- **Firmware/tariffs/manual-schedules could serve stale cached data for up to 6h**: `_fetched_at`
  was stamped whenever the raw HTTP response was truthy, even when parsing it produced nothing -
  now only stamped once parsing actually succeeds, so a bad/unexpected response is retried next
  poll instead of masking a possibly-stale cache for the full interval. `month_stats` is
  deliberately left as-is (gate-on-raw is intentional there - see its own comment).
- **Total Energy shared one Store/load-path with Charger Status's sticky signals**: a corrupt
  store file would silently reset the running total's accumulated history, not just the
  low-stakes status timestamps. Split into two independent `Store` instances
  (`..._status` / `..._total_energy`) with independent load/save, so a problem in one can't take
  out the other.
- **api3 session `user_id` used a falsy check** (`if not user_id`) inconsistent with the correct
  `is not None` pattern used two lines later for the same kind of id - fixed to match.
- **Duplicate `/charges` entry-parsing** between `_latest_charge_per_ppid()` and
  `_accumulate_total_energy()` - factored into a shared `_charge_entries_by_ppid()` generator so
  the two can't silently drift on how an entry is read.
- **CLAUDE.md documentation-style violations**: two comments (`const.py`, `coordinator.py`)
  referenced "the app binary"/"a binary-strings search" - this repo's own convention is facts
  about the API only, not how they were established. Reworded to state the confirmed facts
  without the methodology mentions.

## Charge Priority preferences moved to the regular poll tick

Per the user directly: `chargingStrategy`/`maxPrice` (backing the Charge Priority select) was
staleness-cached on the same 6h `FIRMWARE_TARIFF_REFRESH_INTERVAL` as firmware/tariffs - too slow
for a setting someone might change in the app and expect reflected promptly. Moved into the same
every-poll `asyncio.gather` as the connectivity-status fetch (and smart-schedules/active, in
Smart Charging mode), alongside it in both the Smart and Basic Charging branches - confirmed
Charge Priority stays viewable/changeable in either mode, so it needs fetching every poll
regardless of which one is active. `_preferences_fetched_at` and its staleness gate removed
entirely, no longer needed now that it's unconditional.

## Critical bug: current_charge never resolved - wrong id used for the api3 pod-id mapping

Found from a real overnight charge (Low Cost priority, live account) reported directly by the
user: Last Charge/Last Charge Cost still showed the *previous* finished session, and Total
Energy didn't move at all while actively charging. Root cause, confirmed by cross-referencing
the live captures: api3's `/pods` endpoint has its own `id` field (240843 for this account) *and*
a separate `unit_id` field (290245) - and api3's `/charges` endpoint names its own per-entry
field `pod.id`, but that field's actual value (290245) matches `/pods`' `unit_id`, not `/pods`'
own `id`. Two different api3 endpoints both using the word "id" for different underlying values.
`_api3_pod_id_by_ppid` was built from `/pods`' `id` (an inference from `DECISIONS.md`'s earlier
id-guessing trail, never actually confirmed against a real open session until now) - meaning
`_async_refresh_api3_charges()`'s `pod_id_to_ppid.get(entry.pod.id)` lookup has never once
matched, silently dropping every /charges entry (`if not ppid: continue`) and leaving
`current_charge` permanently `None` for every charger since the feature was built.

Fixed: `_api3_pod_id_by_ppid` now built from `pod["unit_id"]`, confirmed live to match what
`/charges`' `pod.id` field actually contains.

**Separately, still open**: even with this fixed, api3's `ends_at` (used to decide whether a
session is still "current") tracks when the cable is *unplugged*, not when charging actually
*finishes* (confirmed live: the Aug 30 session's `ends_at` matched mobile-api's `unpluggedAt`
18:35, not its own `endedAt` 09:30 when charging genuinely stopped ~5h earlier). So `current_charge`
will keep reporting a growing `duration` for however long the cable stays connected after
charging finishes, not the real charging time. Not yet fixed - needs a decision on the right
signal to use instead (e.g. falling back to `latest_charge` once chargingState leaves
Charging/the momentary-finished states, rather than trusting api3's `ends_at`).

## select_last_charge() - prefer mobile-api's finalized session over api3's still-open one

Follow-on from the pod-id mapping fix above, from the same overnight session: even with
current_charge correctly resolving, api3's own "is this session over" signal (`ends_at`) tracks
when the cable is *unplugged*, not when charging *finishes* - confirmed live, the Aug 30 session
showed mobile-api's `endedAt` (09:30, when charging genuinely stopped) hours before api3's
`ends_at`/the real `unpluggedAt` (18:35). Two consequences, both real:

- Last Charge Duration would keep climbing toward "time since plugged in" instead of freezing at
  the real charging duration, for however long the cable stays connected after charging stops.
- Total Energy would double-count: `_accumulate_total_energy()` watermarks on mobile-api's
  `endedAt` and folds a session's energy into the persisted total the moment mobile-api reports
  it finished, while the sensor's live top-up kept adding `current_charge.energy_total` for the
  same session throughout that same window - both counted, same energy, twice.

Fixed with `select_last_charge()` (helpers.py, offline-tested): prefers `latest_charge`
(mobile-api) over `current_charge` (api3) once both describe the same session (matching
`started_at`) and mobile-api's copy already shows `ended_at` set - otherwise keeps the original
current_charge-first behavior unchanged, so a session still genuinely in progress (or one
mobile-api hasn't reported back yet) is completely unaffected; api3 remains the sole, fully
active source for that entire window, exactly as before. `PodHomeEntity.last_charge` (Last
Charge Duration/Energy/Cost) and `PodHomeTotalEnergySensor`'s live top-up both route through it
now. Also added a defensive guard (never observed live, but nothing rules it out): don't switch
to the "ended" snapshot if api3 has since reported *more* energy than it accounts for - would
mean charging resumed after a brief pause within the same plug-in, so api3's still-open record
is the one actually tracking reality.

Also caught in the same pass: the offline test script had a leftover block still calling
`current_charge_in_month()`, removed from `helpers.py` days earlier when Energy/Cost this month
was reverted to plain finalized-only figures - it was silently crashing the script partway
through every run since, and the `grep -c "^\[OK\]"` check used throughout this session never
caught it (a crash produces no `[FAIL]` line, so it just looked like a shorter, still-passing
run). Removed the dead block, replaced it with real `select_last_charge()` coverage, and
confirmed the script now actually exits 0 rather than just checking its printed output.

## Firebase auth token persistence, and a batch of display fixes

**Auth token persistence** (fixes the "new sign-in notification on every restart" the user
reported live): `PodHomeAuth` (podpoint_mobile_api/auth.py, both copies) already supported
Firebase's refresh-token flow correctly, but `_id_token`/`_refresh_token`/`_expires_at` only ever
lived in memory - since `PodHomeAuth` is constructed fresh in `__init__.py`'s
`async_setup_entry`, which runs on every HA restart/reload, `_refresh_token` always started
`None`, so the very first `async_get_id_token()` call always fell through to a full
email+password sign-in every time, regardless of whether the previous session's refresh token
was still valid - almost certainly what was triggering Pod Point's own "new sign-in" account
notification each restart. Fixed: `PodHomeAuth` gained `export_tokens()`/`import_tokens()` (plain
dict in/out, no HA import - keeps the package HA-independent) and an optional `on_token_change`
callback invoked after a real sign-in or refresh. `__init__.py` persists them via a new small
`Store` (`..._auth`, same coalesced-save pattern as the sticky/total-energy state), loaded before
constructing `PodHomeAuth` and restored via `import_tokens()` before the first API call - a
normal restart should now refresh silently instead of re-authenticating from scratch. Worth
noting plainly: this persists a live Firebase refresh token to disk in HA's storage directory,
unencrypted - the same pattern HA's own OAuth2 helper and most token-based integrations use, not
something unusual, but it's credential-adjacent even though it's never the user's actual
password.

**Last Charge Duration**: suggested display unit changed from minutes to hours, per the user
directly - a multi-hour overnight charge reads more naturally as "5.2 h" than "312 min". Native
stays seconds, exact, unchanged.

**Range/Odometer suggested unit**: now driven by the account's own `preferences.unitOfDistance`
(GET /users, confirmed live: "mi" for this account) rather than inferring from billing currency -
a GBP-billed account isn't reliably a miles-preferring one, and the account already has an
explicit field for exactly this. `PodHomeDataUpdateCoordinator.unit_of_distance` fetched
alongside currency (same `/users` call, no extra request), both sensors switch their
`suggested_unit_of_measurement` to miles when it's "mi", otherwise stay at the kilometre native
value.

**Currency icons**: Last Charge Cost and Cost this month now show `mdi:currency-gbp`/
`mdi:currency-eur` based on the actual currency in play (`currency_icon()`, helpers.py, offline-
tested) - Pod Point only bills in these two currencies per the user directly, so this is a small
closed lookup, not a general-purpose currency-to-icon mapping, falling back to HA's own generic
"mdi:cash" default for anything else. Confirmed first (HA core's `sensor/icons.json`) that HA
itself has no currency-aware icon selection built in - device_class=MONETARY's own default is a
single static "mdi:cash" regardless of unit, so this needed doing ourselves. Rewards balance's
icon (`mdi:cash-plus`) is unchanged, per the user directly - it's fixed-GBP by design (see
PodHomeRewards' docstring), no dynamic currency to reflect.

## Charge time remaining suggested unit -> hours too

Same reasoning as Last Charge Duration's earlier change, applied to
`PodHomeVehicleChargeTimeRemainingSensor` (vehicle.chargeState.chargeTimeRemaining) - it's the
same kind of duration estimate, so gets the same suggested_unit_of_measurement = hours treatment,
per the user directly. Native stays minutes (still the assumed, unconfirmed wire unit - this
field has never been observed non-null on this account).

## Second code-review pass: 8 more fixes

- **diagnostics.py**: `charger_data.get("firmware", {})` didn't guard against the key being
  present-but-`None` (only `dict.get`'s missing-key case substitutes the default) - crashed
  `async_get_config_entry_diagnostics` with `AttributeError` for any charger before its first
  successful firmware fetch. Fixed to `(charger_data.get("firmware") or {})`.
- **Account preferences retry-forever**: `if self.currency is None or self.unit_of_distance is
  None` re-fetched `GET /users` every poll forever if `unitOfDistance` was ever legitimately
  absent for an account. Now gated by a real staleness cadence
  (`_account_preferences_fetched_at`, `FIRMWARE_TARIFF_REFRESH_INTERVAL`) stamped on any
  successful response, not on both fields ending up populated.
- **Last Charge Cost docstring**: still claimed cost is "live and still climbing" during an
  active session, contradicting today's earlier fix (`cost_amount` is deliberately `None` for
  `current_charge`, confirmed api3's own field is always 0 on an open entry). Reworded to
  describe the actual current behavior.
- **`select_last_charge()` exact-equality**: now allows a small tolerance
  (`_SAME_SESSION_TOLERANCE`, 5s) matching `started_at` between api3 and mobile-api instead of
  requiring bit-for-bit equality, which had no confirmed guarantee of holding.
- **`_parse_dt()`**: now always returns a timezone-aware datetime, assuming UTC if a parsed
  timestamp comes back naive - closes a `TypeError` risk anywhere a parsed timestamp gets
  compared against `dt_util.utcnow()` (e.g. current_charge's duration calculation), should api3
  (a different, older backend) ever omit an offset. Every real capture so far has included one.
- **`async_sync_mode_gated_entities`**: vehicle-scoped entities (Ready By/Target Charge/Expected
  Charge) are now resolved in a second pass keyed by vehicle_id, using only the first charger
  each vehicle is linked to in iteration order - matches `PodHomeVehicleEntity.
  _charger_for_vehicle()`'s own resolution exactly, so gating can't disagree with which
  charger's mode the entity itself would read on a multi-charger account sharing one vehicle.
- **api3 pod-id mapping**: now warns once (`_warn_once("api3_charges_unmatched", ...)`) if an
  open session exists and a real pod-id mapping exists but nothing matched - the exact silent
  failure signature the `pod["id"]`-vs-`unit_id` bug had, now detectable without another live
  account trace.
- **`__init__.py` auth-store load failure**: now logs a warning on a corrupt/unreadable store
  file, matching the coordinator's equivalent stores instead of failing silently.

Offline suite extended (select_last_charge tolerance cases) and re-verified by exit code, not
just printed-OK count, after last time's silent-crash lesson - 59/59, exit 0.

## Icon adjustments: Electricity rate dynamic, Last Charge Cost back to static

Per the user directly: Electricity Rate was hardcoded `mdi:currency-usd` - wrong for a GBP/EUR
account, same oversight the cost sensors had before `currency_icon()` was added. Now uses
`currency_icon(self.coordinator.currency)` too, matching Cost this month. Last Charge Cost
reverted to a static `mdi:cash` - the user's call, not every monetary sensor needs the dynamic
treatment.

## Cost this month reverted to static mdi:cash-multiple

Per the user directly: `currency_icon()`'s dynamic treatment is now only used by Electricity
rate. Last Charge Cost and Cost this month both went back to static icons (`mdi:cash`/
`mdi:cash-multiple`) - the user's call on which sensors actually warrant the dynamic currency
icon.

## Status gets a per-state dynamic icon

Per the user directly: `PodHomeStatusSensor` previously showed a single static `mdi:ev-station`
regardless of its actual value. Confirmed first (HA core's `sensor/icons.json`) that HA's own
icon system only supports per-numeric-range mapping (e.g. `battery`'s 0/10/20.../100 thresholds),
not per-enum-value - so a status-dependent icon needs a custom `icon` property, same pattern
`PodHomeConnectivitySensor` (binary_sensor.py) already uses. Added a `_STATUS_ICONS` lookup
covering all nine `CHARGER_STATUS_*` values (Charging/Paused/Available/Preparing/Finishing/
Reserved/Unavailable/Finished/Fault), falling back to the original `mdi:ev-station` for
anything unmapped. Icon names spot-checked against the live Pictogrammers MDI library
(pause-circle-outline, timer-sand, progress-check, power-plug-off, calendar-clock,
alert-circle(-outline), check-circle) rather than assumed.

## Energy this month / Cost this month renamed to Month energy / Month cost

Per the user directly: renamed to match the "Last charge duration/energy/cost" prefix-first
naming convention, at every level - class (`PodHomeEnergyMonthSensor`/`PodHomeCostMonthSensor` ->
`PodHomeMonthEnergySensor`/`PodHomeMonthCostSensor`), `unique_id` suffix (`_energy_month`/
`_cost_month` -> `_month_energy`/`_month_cost`), `translation_key`
(`energy_month`/`cost_month` -> `month_energy`/`month_cost`), display name ("Energy this
month"/"Cost this month" -> "Month energy"/"Month cost"). `PodHomeCharger.month_energy_kwh`/
`month_cost_amount` (coordinator.py) were already in month-first order, so no change needed
there - only the entity-level naming was backwards relative to the dataclass fields and the
Last Charge convention.

**Real consequence, flagged plainly**: the `unique_id` change means HA treats these as brand-new
entities on next restart, not a rename of the existing ones - any existing recorder
history/long-term statistics tied to the old `unique_id`s (e.g. Energy Dashboard usage) doesn't
carry over to the renamed entities. Not a data-loss bug, just a fresh start for those two
specifically, same class of consequence already noted for this project's Store-filename renames.

## Charge Priority write side switched from chargingStrategy to maxPrice

Live-tested by the user directly: selecting a Charge Priority option went to "unknown" with no
observed change in the app - confirming the earlier "backend resolves chargingStrategy (MIN/MAX)
into maxPrice itself" theory (always flagged as unconfirmed, "NOT YET TESTED live") was simply
wrong. PATCHing chargingStrategy alone has no observed effect on the account at all.

Fixed by dropping chargingStrategy entirely - for both read (it was already unused there,
confirmed live to never appear in a GET response) and write - in favor of writing `maxPrice`
directly, computed client-side from the account's own tariff rates
(`max_price_for_charging_priority()`, helpers.py - the exact inverse of `charging_priority_label()`,
so write and read are guaranteed to agree on the same field). Removed as dead/wrong:
`CHARGING_STRATEGY_MIN`/`MAX`/`OPTIONS` (const.py), `charging_strategy_from_label()` (helpers.py),
`PodHomeCharger.charging_strategy` (coordinator.py - simplified `_preferences_by_ppid`'s tuple
down to a plain `_max_price_by_ppid` float dict now that only maxPrice is tracked),
`async_set_charging_strategy()` (client, both copies) replaced by
`async_set_charge_priority_max_price(ppid, max_price)`. `async_select_option()` now refuses to
write (raises `HomeAssistantError`) rather than guess if tariff data isn't known yet, since a
price can't be computed without it.

## Charge time remaining / Expected charge: unavailable when unplugged, not unknown

Per the user directly: both sensors showed "unknown" rather than "unavailable" while the vehicle
was unplugged. Root cause: `PodHomeVehicleEntity.available` only checks `self.vehicle is not
None`, but `vehicle` persists across plug/unplug (Enode-linked data, not gated on physical
connection - only `vehicle.is_plugged_in_to_this_charger` reflects that, per its own docstring).
Both sensors are predictions about the *current* charging session (time remaining, expected %
by Ready By) that are genuinely meaningless once unplugged, not just momentarily unknown -
overrode `available` on each to also require `is_plugged_in_to_this_charger`. Charge Rate/Max
Current/Power Delivery State (debug sensors, same session-specific reasoning) were deliberately
left alone for now - not requested, and changing debug-sensor availability semantics without
being asked felt like overreach.

Electricity Rate -> account device: discussed, not applied - it's fetched from
`/chargers/{ppid}/tariffs`, charger-scoped by the API's own design, and this account's single
charger means whether two chargers could have different tariffs is genuinely untested either
way. Left on the charger device rather than guessing it's account-wide.

## Linked-vehicle fetch: tiered cadence, not every-poll

`async_smart_charging_chargers_and_vehicles` (battery/range/odometer/charge-rate/etc, one
account-wide call) used to be fetched unconditionally every poll - a known, flagged inefficiency
that hadn't been fixed. Given a charging-aware gate the same shape as `/charges`/api3 charges,
per the user directly: three tiers instead of a flat charging/not-charging split, since "plugged
in but not charging" and "unplugged" are genuinely different in how fast the underlying data
actually moves.

- **Charging** (any charger, previous poll) -> every poll.
- **Cable connected but not charging** (Preparing/SuspendedEVSE/SuspendedEV/Finishing - a car
  just sitting plugged in) -> `CHARGE_STATS_REFRESH_INTERVAL` (30 min). Nothing about a parked,
  non-charging vehicle changes on its own; SoC only moves while actually charging.
- **Not connected** (possibly driving) -> `VEHICLE_REFRESH_INTERVAL` (5 min, new constant). The
  one case where the vehicle itself might be moving, so kept meaningfully fresher than the
  30-minute tier even though it's also account-wide and not gated on any one charger.

Tier is decided from the **previous** poll's charger-side `chargingState` (via
`CHARGING_STATE_CABLE_CONNECTED`), not the vehicle's own `is_plugged_in_to_this_charger` flag -
this closes what would otherwise be a stale-availability gap for free: the poll where a charger's
`chargingState` transitions out of a cable-connected state (cable physically unplugged) is
exactly the poll where the previous poll's state was still "connected", so the tier decision for
*that* poll still fetches vehicles, and `is_plugged_in_to_this_charger` (which
`PodHomeVehicleExpectedChargeSensor`/`PodHomeVehicleChargeTimeRemainingSensor`/etc.'s
`available` overrides depend on) flips false the same poll the unplug is discovered - no separate
forced-refetch logic needed. The reverse (plugging in) can lag up to the 5-minute tier-3 fallback
before those sensors notice - accepted as the safe direction to be behind in: they stay
`unavailable` a little longer rather than showing something they shouldn't.

Decided against evaluating the tier per-vehicle: the endpoint is one combined account-wide call,
not addressable per-charger, so per-vehicle tiering isn't actually possible without splitting it
into per-charger calls the API doesn't support - the account-wide "any charger" framing (already
used for `/charges`' own gating) was kept deliberately simple instead, per the user directly.

`self._vehicle_by_ppid` is now a persistent cache (previously rebuilt from scratch every poll,
since the fetch always ran) - a skipped poll keeps the last-known value rather than a charger's
`vehicle` field going stale-empty just because this poll didn't refetch.

## Vehicle sensors: reverted unavailable-when-unplugged back to unknown

Earlier this session, Expected Charge/Charge Time Remaining/Charge Rate/Max Current were given
`available` overrides requiring `vehicle.is_plugged_in_to_this_charger`, so they'd show
unavailable rather than unknown once unplugged. Reconsidered, per the user directly: unavailable
reads as "something's wrong," which isn't true here. Checked against HA's own quality-scale rule
([entity-unavailable](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/entity-unavailable/)):
unavailable is for "can't fetch data from a device or service"; unknown is for "successfully
fetched data but temporarily missing a value" - unplugged is the second case, not the first.
Confirmed safe to just delete the overrides (not just change what they return) by checking a real
unplugged capture (`scratch/output_unplugged/smart_charging_chargers_and_vehicles.json`):
`chargeRate`/`maxCurrent`/`chargeTimeRemaining`/`currentIntent` are all already null/absent once
unplugged, so the base `PodHomeVehicleEntity.available` (`vehicle is not None`) plus each
sensor's existing `native_value` already produces unknown for free, no new logic needed.

## Charge Rate confirmed live; Max Current still isn't

Charge Rate's docstring previously said "always null on this account to date" - based on this
project's own point-in-time API captures, which happened to never land mid-charge. Corrected
after the user supplied a real HA history graph: `Vauxhall Grandland Charge rate` showing real
values (0.7-2.7 kW) through an actual charging session on 31 Aug, dropping to Unknown the moment
charging stopped - confirming both that the field genuinely works on this account and that kW is
the right unit. Promoted from a disabled-by-default Diagnostic debug sensor to Standard,
enabled-by-default, matching Charge Time Remaining's earlier promotion (a real, working,
user-facing value). Max Current is unaffected by this - per the user directly, it's never been
observed populated, live-app or otherwise - so it keeps its original disabled/Diagnostic/
unconfirmed treatment; only Charge Rate's docstring and default-enabled state changed.

## Live boost-test findings: Boost end time, vehicle-tier fix, freshness attribute

Ran two live probes today: `vehicle_sync_probe.py` (watches three vehicle-sync timestamps) and a
new `boost_watcher_probe.py` (generic diff across six read-only endpoints, timed against
triggering a real 15-minute boost in the app). Findings and the resulting changes:

**`charge-overrides` confirmed as the real boost signal, not just Smart Charging's own internal
pause/resume plumbing** (the ambiguity `pod-point-new-api-findings.md` flagged). A fresh,
non-deleted entry appeared at the exact instant the boost was triggered, `endAt` matching the
selected 15-minute duration exactly. `_current_boost_end_at()` (coordinator.py) picks the
non-deleted, not-yet-ended entry with the latest `requestedAt` as "current" (list order isn't
trusted). Wired into the per-charger `asyncio.gather` every poll, same as Charge Priority/
`max_price` - not staleness-cached, and not mode-gated (confirmed live not tied to
`delegatedControl.status`). New `PodHomeBoostEndTimeSensor` (sensor.py), `SensorDeviceClass.
TIMESTAMP`. **Deliberately unavailable (not unknown) when no boost is running** - per the user
directly, a conscious exception to the unavailable-vs-unknown convention just established for
the vehicle sensors above: a boost genuinely doesn't exist as a concept most of the time, unlike
a vehicle's charge state which always has *some* meaning even when unplugged.

Also observed live in the same test: the off-peak charging window that night shortened by
exactly 15 minutes in the same poll the override appeared - confirms the "borrow now, pay back
off-peak" mechanism the user described, live rather than just as described.

**Vehicle-fetch tier decision closed a real gap.** The tier (added earlier this session - see
"Linked-vehicle fetch: tiered cadence" above) decided "charging" purely from the charger's own
previous-poll `chargingState`. Live testing showed the vehicle side can confirm `isCharging` via
Enode *before* the charger confirms `chargingState: Charging` - ordering between the two sources
isn't fixed, it varies per occasion (confirmed on two separate boost tests, each ordering seen
once). In that ordering, the tier stayed on the slower cable-connected tier despite the vehicle
already being in its fast-changing state. Fixed by also checking the vehicle's own previous-poll
`is_charging` (`any_vehicle_charging_last_poll`) - promotes to the fast tier if *either* source
confirms charging, no new fetch cost since the data's already in `self.data`.

**Vehicle data freshness exposed.** `PodHomeVehicle.synced_at` (from `chargeState.lastUpdated`)
was previously unparsed - added and surfaced as a `synced_at` attribute on the Battery sensor.
Justified by live evidence, not guessed: the same two tests showed genuinely variable lag between
Enode's real sync and what we display (as fast as ~27s, as slow as ~6 min in one case) - no
reliable way to predict which without polling faster than any interval choice would make sensible
(a 30s-cadence probe hit the same ~6-minute stall a 5-minute-tier fetch would have), so exposing
the real timestamp is more honest than assuming freshness.

**Considered and explicitly rejected**: retuning `FAST_POLL_INTERVAL`/`SLOW_POLL_INTERVAL`/
`RECENT_CHANGE_WINDOW`/`VEHICLE_REFRESH_INTERVAL` against today's numbers - all held up against
real observed gaps. The one theoretical concern (`SLOW_POLL_INTERVAL` aliasing against the
charger's own ~300s idle heartbeat, risking up to ~600s worst-case staleness) would only improve
to ~540s at a real 25% idle-load increase - a bad trade against the already-deliberate choice
(see "Adaptive polling" above) to match the legacy integration's proven-acceptable idle load.

**Considered and ruled out**: `delegated_control` (per-charger, currently 6h-cached) looked like a
consolidation candidate live - it appeared to carry the same vehicle/chargeState payload as the
separate account-wide `vehicles` endpoint. Checked properly against the full
`boost_watcher_probe.jsonl` log (61 polls, not just the one instant noticed live):
`chargeState`/`lastSeen`/`isPluggedInToThisCharger`/`isPrimary`/`intents` matched exactly on
every single poll, but `delegated_control`'s vehicleLinks entries are missing `currentIntent`
entirely, on all 61 - the field backing Expected Charge (`expected_charge_percent`) and the
`can_meet_target`/`cannot_meet_target_reason` attributes. `delegated_control` isn't a superset of
`vehicles`, so consolidating onto it alone would silently drop Expected Charge. Closed, not just
deferred - no amount of further polling would surface a field that isn't in the response shape.
Both fetches stay as they are.

## Schedule calendar labels: app's own wording, not a mechanical transform

Initially used `humanize_tariff_rate()`'s generic SHOUTY_SNAKE_CASE -> sentence case transform,
lowercased for the parenthetical (`"Charging (off peak)"`). Per the user directly, corrected
twice: first to lowercase (was sentence case), then to match the Pod Home app's own real wording
exactly - "Boost"/"Off-peak"/"Peak", Title Case, hyphenated "Off-peak", and "ON_PEAK" dropping
"on" entirely rather than reading "On peak". New `CALENDAR_TARIFF_RATE_LABELS`/
`calendar_tariff_rate_label()` (helpers.py) - a hand-picked mapping, not `humanize_tariff_rate()`
(kept as-is, still generic, now documented as the fallback for an unrecognized rate rather than
the calendar's own primary source). Also corrects that function's stale "only OFF_PEAK confirmed
live" claim - both `ON_PEAK` and `OFF_PEAK` are now confirmed, `ON_PEAK` from the live boost test.

Also tried, then dropped: labelling a `Charging` window overlapping the active boost `"(Boost)"`
instead of its tariff rate (`boost_window` param on `smart_schedule_events()`, sourced from
`_current_boost_window()` in coordinator.py, which returned `(started_at, end_at)` instead of
just `end_at` to support the overlap check). Removed again per the user directly - the calendar
just shows each `Charging` window's tariff rate now, with no boost-specific case.
`_current_boost_window()` reverted to `_current_boost_end_at()` (just the end, as the Boost end
time sensor alone needs), and `PodHomeCharger.boost_window` back to `boost_end_at`.

## Charge Priority hidden on a single-rate tariff, matching the app

Per the user directly: the app itself hides the Charge Priority option entirely on a single-rate
tariff, since there's no cost-vs-completion tradeoff to make when every rate is the same -
`charging_priority_label()` already returned unknown in that case (added earlier this session),
but the select entity itself stayed visible/enabled, unlike the app.

New `is_single_rate_tariff()` (helpers.py) - the same `math.isclose(min(prices), max(prices))`
test `charging_priority_label()` already used, factored out as its own predicate (kept as a
separate function rather than having `charging_priority_label()` call it, since that function
still needs the actual `lowest`/`highest` values, not just the boolean - calling both would
duplicate the price-list comprehension, not reduce it). Returns None (not a guess) when the
tariff isn't known yet, same "don't guess" principle as everywhere else in this project.

New `_TARIFF_GATED_ENTITIES`/`async_sync_tariff_gated_entities()` (entity.py) - deliberately a
separate manifest/function from `_MODE_GATED_ENTITIES`/`async_sync_mode_gated_entities()` rather
than extending that one, since tariff shape is a genuinely different gating axis from Charging
Mode (that manifest's own comment already anticipated only adding more *mode* conditions, not a
different kind of condition entirely). Reuses `_async_apply_disabled_state()` directly rather
than reimplementing the registry-lookup/update logic a second time - only the "which entities,
what condition" part is new. Called from `__init__.py` alongside the mode-gated sync, same two
trigger points (after initial platform setup, and on every coordinator update thereafter, since
the account's tariff could change at any time from the app).

## Code review: 6 findings applied

Ran `/code-review` (medium effort, 8 finder angles, 7 verified - 2 self-refuted against already-
known facts before spending a verify slot) against the full `git diff HEAD` (no prior commit to
diff against beyond the bare "Initial commit" scaffold, so this covered the whole project).
6 of 8 verified candidates survived (5 CONFIRMED, 1 PLAUSIBLE); all 6 applied:

1. **Electricity Rate wrap-window bug (CONFIRMED)** - `_current_window()` (sensor.py) matched a
   day-specific wrapping tariff window during the pre-start hours of its own start day (e.g.
   Friday 03:00, before that Friday's 22:00-06:00 window has begun) - `today_name in days`
   matched unconditionally whenever `_time_in_window()` was true, regardless of which half of
   the wrap `current_time` fell in. Fixed by gating the today/yesterday check on which half of
   the wrap it is, not checking today unconditionally first. Confirmed dormant only because the
   one live-observed tariff lists all 7 days on every window (inert there since today_name is
   always a match either way); offline-traced against both the bug scenario and the all-7-days
   live case to confirm the fix doesn't regress the working case.
2. **Total Energy same-poll duplicate double-count (CONFIRMED)** - `_accumulate_total_energy()`
   compared every entry against a frozen pre-batch watermark snapshot (deliberate, for tolerating
   genuinely-distinct out-of-order entries), which didn't protect against two same-ppid entries
   sharing an `endedAt` in one response both passing the same check. Fixed with a per-batch
   `seen_ids` set, deduping by charge `id` without weakening the snapshot's actual purpose.
3. **Rewards sensor never unavailable (CONFIRMED)** - `PodHomeAccountEntity` had no `available`
   override, unlike `PodHomeEntity`/`PodHomeVehicleEntity`. Fixed on `PodHomeRewardsBalanceSensor`
   itself rather than the base class, since (unlike charger/vehicle) `PodHomeAccountEntity` has
   no single backing-data property shared across all its subclasses to check generically - it's
   currently the only subclass, so this is scoped to it specifically.
4. **Six account-level fetches serialized, not gathered (CONFIRMED)** - confirmed to recur
   roughly every 6h for the life of a running instance (not just once at cold start), since the
   6h-cadence fetches (account_preferences, rewards, api3_account) all start `None` and
   resynchronize. Restructured into two `asyncio.gather` groups (account_preferences+vehicles;
   charges+api3_account+rewards), reusing the same dict-keyed-gather pattern already used
   elsewhere in this file for the per-charger `stale_calls` block - api3_charges still awaited
   separately afterward, since it genuinely depends on api3_account's user_id.
5. **Refined charge duration not written back (PLAUSIBLE)** - `current_charge.duration`'s
   refinement (see "Last Charge Duration during an active session" above) patched only the local
   loop variable, not `self._current_charge_by_ppid` itself - confirmed inert today (nothing
   reads the raw dict directly) but a latent trap for future code. Fixed with one extra line
   writing the refined value back into the dict.
6. **HH:MM:SS parsing tripled (CONFIRMED)** - the same parse-or-None logic existed independently
   in `helpers._parse_time_of_day()` (private), `PodHomeElectricityRateSensor._parse_time()`
   (sensor.py), and inline in `PodHomeVehicleReadyByTime.native_value` (time.py). Exported as
   `parse_time_of_day()` (dropped the underscore) and had both other call sites import and use it
   instead of their own copies - no behavior change, confirmed all three were byte-for-byte
   equivalent before merging.

Two candidates were refuted before reaching a verify agent (already-known facts made this
possible without spending a slot): the `charge_overrides_raw` bare-list assumption (confirmed via
this session's own earlier captures that the real response shape is a bare list, matching the
code) and the connection-retry scope "inconsistency" (the code's own comment already explains
`async_list_chargers` alone gets retried because it's the one call fatal to the whole poll -
deliberate, not an oversight). One verified candidate (duplicated Smart/Basic `asyncio.gather`
branches) was REFUTED by its verifier - only the connectivity-status call was actually duplicated
between the two branches, not all three coroutines the candidate claimed.

## Enum sensor/select state localization

Checked HA's actual documented convention (developers.home-assistant.io/docs/internationalization/
core - "Translated states must be snake_case"): an enum entity's raw state should be a stable
snake_case key, with the display text living in strings.json/translations/en.json's per-entity
`state` block, not baked into the raw value itself. This project's derived-vocabulary enum
entities (not wire pass-throughs - our own invented values) weren't following that: `CHARGER_
STATUS_CHARGING = "Charging"` etc. had the raw state ARE the English display text, with no
separate translation layer - meaning even a future Spanish strings file wouldn't actually
translate these, since there'd be nothing to look the display text up from. Worth fixing for
real (not speculative) - the account is used in the UK, Ireland, and Spain.

Converted three entities' underlying constants to snake_case, with `state` blocks added to both
`strings.json` and `translations/en.json`: `CHARGER_STATUS_*` (Status sensor), `SCHEDULE_MODE_*`
(Charging Mode sensor), `CHARGE_PRIORITY_*` (Charge Priority select). Safe to rename outright
(not a two-tier wire-value/display-key split) since these are entirely this integration's own
vocabulary - nothing compares them against real API text, confirmed via grep before renaming.

**Deliberately left as raw wire pass-throughs, not localized** - per the user directly: Charging
state (`CHARGING_STATE_*`) and Power delivery state (`POWER_DELIVERY_STATE_*`) both mirror the
API's own raw field values directly (matching the API's own vocabulary, e.g. "SuspendedEVSE",
"PLUGGED_IN:CHARGING") - debug/diagnostic sensors by design, not meant to be pretty or localized.
Translating these would also be more involved (the wire values themselves are used pervasively in
internal comparison logic throughout coordinator.py/helpers.py - `charger_status()`, sticky
signals, vehicle-fetch tiering - so localizing the sensor's own display would need a separate
key-mapping layer at the sensor boundary rather than a straight rename, unlike the three above).

This is a real, intentional breaking change to the three converted entities' own state contract
(an automation matching `state: "Charging"` on the Status sensor would need updating to `state:
"charging"`) - accepted now, before any real users/automations depend on the old English text,
rather than deferred to after a public release.

Calendar event summaries (`Charging (Off-peak)` etc.) remain untouched - confirmed via the same
HA docs that calendar entities have no i18n mechanism at all; that's a separate, unresolved
question the user flagged as "interesting" but didn't ask to act on yet.

## PLATINUM_COMPARISON.md merged into QUALITY_SCALE.md

Per the user directly: two separate quality docs (`QUALITY_SCALE.md`'s abstract 54-rule
checklist, `PLATINUM_COMPARISON.md`'s concrete gap analysis against Ohme/Peblar) risked the same
drift problem as verbose code comments duplicating DECISIONS.md - both already cross-referenced
each other's findings (dependency extraction, diagnostics) with no single source of truth for
either. Merged `PLATINUM_COMPARISON.md`'s content into `QUALITY_SCALE.md` as a "Platinum
comparison against Ohme and Peblar" section, reconciling the two files' separate "Recommendation"
sections into one, then deleted `PLATINUM_COMPARISON.md`. `QUALITY_SCALE.md`'s header now states
platinum as the explicit target tier, not just passive compliance tracking, and CLAUDE.md now
points to it as the one place quality-scale status lives - see CLAUDE.md's own "Quality scale"
section for the standing instruction to check changes against it and cross off items as closed.
That section also now says to periodically re-check the rule list/recommendations against the
live HA dev docs, not treat the "confirmed 2026.8" snapshot as permanent.

## CLAUDE.md's own documentation-style rule was violating itself

Per the user directly: "No mentions of decompiling, app packages, static analysis, or similar in
anything that ships in the repo" - stated in CLAUDE.md, which itself ships in the repo, naming
the exact excluded methods as examples. Reworded to state the constraint (say confirmed/
unconfirmed, never the method) without listing the methods being excluded, and added a line
noting the rule applies to CLAUDE.md itself. Deliberately left DECISIONS.md's own historical
entries (which quote the original wording while describing past violations/fixes) untouched -
that's backward-looking record of what was already wrong and corrected, not a live instruction,
and DECISIONS.md is append-only by convention, never rewritten retroactively.

Follow-up per the user directly: the first rewrite ("never say or hint at the method... that
itself is the leak the rule exists to prevent") still read as suspicious/cloak-and-dagger -
drawing attention to secrecy is itself a tell, regardless of whether the specific methods are
named. Reworded again to plain scoping language with no reference to hiding, leaking, or
excluding anything: docs describe the API (the target), not the investigation that produced the
description (the process) - an ordinary technical-writing convention, not a security rule.

## Last Charge Duration during an active session: fixed via smart-schedule sub-window summing

Per the user directly: the live figure (from `current_charge`, api3) showed time since plugged
in, not time actually spent charging, while the reconciled figure (from `latest_charge`,
mobile-api, once the session finalizes) correctly reflects charging-only time - confirmed by
observation, matching what's already on record about api3's `current_charge` staying open until
cable-unplug rather than until charging stops (see `select_last_charge()`'s docstring,
helpers.py). Root cause: `current_charge.duration` is computed as `now - started_at`
(`_async_refresh_api3_charges()`, coordinator.py), and `started_at` is api3's own `starts_at` -
plug-in time, not charging-start time. A session that pauses (Smart Charging waiting for a
cheaper window, say) before actually charging would show live duration counting through that
pause.

Initially flagged as unfixable without guessing - a true "cumulative time actually charging so
far" would need summing every CHARGING sub-window in `smart_schedule_windows`. Explored properly
before deciding that: checked a real captured schedule (`scratch/output/boost_watcher_probe.jsonl`)
and confirmed it retains every past window for the current session back to the original
PLUGGED_IN marker, not just forward-looking ones - so summing is possible with data already
fetched every poll, not a guess. New `cumulative_charging_seconds()` (helpers.py) sums each
CHARGING window clipped to `[session_start, now]`; `_async_fetch_data()`'s per-charger loop
applies it to `current_charge.duration` via `dataclasses.replace()` whenever this poll's Smart
Charging schedule is available, falling back to the original naive figure in Basic Charging mode
(no schedule exists to refine against there) or if the schedule hasn't been fetched yet this
poll. Verified against the real capture used to find the bug: cumulative gave 43m34s against the
naive figure's 1h06m31s - a ~23-minute overcount matching the session's real Paused stretch
(11:06-11:28:57) almost exactly.

## Vehicle sensor naming pass

Reviewed the vehicle device's full entity name set for consistency. Initially proposed adding
"level" to Target charge/Expected charge (both battery percentages, to disambiguate from
Charge rate/Charge time remaining, which are about the charging process, not a level) and
shortening Charge time remaining to Time remaining. Both reconsidered, per the user directly:

- "Charge time remaining" -> "Time remaining" rejected: with `has_entity_name`, HA renders this
  as "<vehicle name> Time remaining" - losing "charge" loses the only word tying it to charging
  at all, reading as meaningless on its own. Kept as Charge time remaining.
- The "level" suffix idea was superseded, not just for Target/Expected charge but for Battery
  too: many HA integrations name a `SensorDeviceClass.BATTERY` sensor plainly "Battery" (the
  device_class itself already implies "this is a level/percentage" - same reason Estimated Range
  doesn't spell out "percentage of tank/battery" either). Given that convention, adding "level"
  to Target/Expected charge would make those inconsistent with Battery's own naming rather than
  consistent with it. Net change: only `Battery level` -> `Battery` (display name/translation
  only - unique_id/translation_key both untouched, so no history/statistics impact). Target
  charge, Expected charge, and everything else on the vehicle device kept their original names.

## Six more CLAUDE.md conventions added

Per the user directly, six more real patterns from this session that were being followed but
never written down:

1. **DECISIONS.md is append-only** - stated explicitly in both CLAUDE.md and this file's own
   header now, not just inferred convention.
2. **New platform checklist** - `PARALLEL_UPDATES = 0`, shared base-entity pattern, matching
   `strings.json`/`translations/en.json` entries, `PLATFORMS` registration - written down ahead
   of the write-endpoint platforms (button/switch) still pending in PLAN.md.
3. **Offline verification via `helpers.py` + exit-code discipline** - the main way to check
   coordinator/entity logic without a real HA instance, plus the hard-won lesson from earlier
   this session: a `grep -c "^\[OK\]"` line-count check doesn't catch a script that crashes
   partway through (no `[FAIL]` line gets printed) - check the real exit code instead.
4. **Write-endpoint caution generalized** beyond the two named physical-effect endpoints to any
   write-capable entity - matches how Ready By/Target Charge/Charge Priority were actually
   handled (flagged NOT YET TESTED until explicit live confirmation).
5. **Fail-loud `assert` pattern for classification completeness** - named as a standing
   convention (the `CHARGING_STATE_CABLE_CONNECTED` pattern), not just documented at that one
   call site.
6. **No blocking I/O** - standard HA async constraint, already followed throughout, now stated
   for future code.

## Second code review: 7 findings applied

Ran `/code-review` again before the first real commit, against the full accumulated diff
(everything since "Initial commit" - including the docstring-trimming pass and enum-localization
work from earlier the same session). 8 finder angles, 8 candidates verified, 1 REFUTED and
dropped, 7 applied:

1. **Vehicle mode-gating charger mismatch (CONFIRMED)** - `async_sync_mode_gated_entities()`
   (entity.py) skipped a charger via `continue` before recording its linked vehicle's gating
   decision whenever that charger's mode was unresolved, so on a multi-charger account the
   decision could be sourced from a LATER charger than the one `_charger_for_vehicle()` actually
   resolves data/writes through - directly falsifying the "these two resolutions always agree"
   claim both the docstring and an earlier DECISIONS.md entry made. Fixed by recording `None`
   (not skipping) when the first-linked charger's mode is unresolved, so the vehicle's gating
   decision always tracks the SAME charger `_charger_for_vehicle()` would pick - offline-traced
   against all four orderings (first Smart/second unresolved, first Basic/second Smart, both
   unresolved, both resolved) to confirm. Untested live - single-charger account, this bug has
   never actually manifested on it.
2. **const.py lost confirmed/unconfirmed markers (CONFIRMED)** - the large docstring-trimming
   pass deleted the module docstring sentence explaining the confirmed-vs-unconfirmed comment
   convention, and dropped the actual markers from `CONNECTION_STATE_OFFLINE`,
   `CHARGING_STATE_SUSPENDED_EV`, and the renamed `CHARGING_STATE_FAULTED` (was
   `CHARGING_STATE_OUT_OF_SERVICE = "OutOfOrder"  # unconfirmed, casing/spelling is a guess`) -
   exactly the kind of information loss the trimming instructions were told to avoid. Restored
   the docstring sentence and all three markers verbatim.
3. **DECISIONS.md names decompiling tooling (PLAUSIBLE, kept despite a REFUTED verifier vote)** -
   the api3-discovery entry named jadx/WSL/a portable JDK and referenced `BuildConfig.java` and a
   bundled code comment. A verifier found textual cover in an earlier DECISIONS.md meta-entry
   about not rewriting historical entries - but that entry was about quoting old removed CODE
   while describing a fix, not about DECISIONS.md's own prose directly narrating decompiling
   methodology as a first-class fact. Redacted the specific tooling/artifact mentions, kept the
   actual API-fact conclusion (api.pod-point.com still real, versioned /v5/, mobile-api proxies
   it at /api3/v5/..., confirmed independently via the OpenAPI schema) - matching this project's
   own established precedent of redacting such mentions rather than declaring any file exempt.
4. **No safety net linking enum constants to translation JSON (CONFIRMED)** - new
   `check_translation_keys.py` (repo root, no HA dependency) cross-checks `strings.json`/
   `translations/en.json`'s `state` blocks against `CHARGER_STATUS_OPTIONS`/
   `SCHEDULE_MODE_OPTIONS`/`CHARGE_PRIORITY_OPTIONS`, matching the same tier as the existing
   `podpoint_mobile_api` import check. Wired into CLAUDE.md's Verification section.
5. **Live charge duration can freeze for up to ~30 min (PLAUSIBLE)** - confirmed narrow and
   self-correcting (not a regression for Basic Charging, a net improvement for Smart Charging
   overall), but real: when `cumulative_charging_seconds()` returns `None` this poll (schedule
   transiently unavailable) the old duration was left untouched instead of at least advancing via
   the naive "time since plug-in" fallback. Fixed with the suggested mitigation - recompute the
   naive fallback fresh whenever refinement isn't possible this poll, instead of freezing.
6. **`CONNECTION_STATE_OPTIONS`/`SMART_SCHEDULE_TYPE_OPTIONS` unused (PLAUSIBLE)** - both
   aggregate list constants confirmed dead (only their own definitions match a grep); removed.
   The individual constants they were built from stay - some (`CONNECTION_STATE_ONLINE`,
   `SMART_SCHEDULE_TYPE_CHARGING`) are genuinely used elsewhere, others are kept as documented
   wire-value names even though nothing currently validates against them.
7. **`charges_raw` parsed twice per poll (PLAUSIBLE)** - `_latest_charge_per_ppid()` and
   `_accumulate_total_energy()` each independently re-iterated `_charge_entries_by_ppid()`
   despite their own docstrings claiming to share one pass. Negligible cost at this integration's
   scale, but the docstrings were actively wrong. Fixed by materializing the generator once at
   the call site and passing the same list to both functions - matches what the docstrings always
   claimed happened.

The one REFUTED candidate (a literal reading of "no 'confirmed live' narrative framing" as
banning the terse two-word tag `# confirmed live` itself, rather than the longer "per the user
directly"-paired storytelling pattern it's actually aimed at) was dropped - CLAUDE.md's
Documentation style section explicitly requires exactly this kind of terse confirmed-vs-guessed
marker, and the two sections aren't in tension once read together.

## check_translation_keys.py promoted to a real pytest test

Initially a standalone script (`python check_translation_keys.py`, no framework) - deliberately
kept that way at first, matching QUALITY_SCALE.md's own recorded position that full test
infrastructure is scoped, deliberate future work, not something to start piecemeal. Per the user
directly, promoted anyway: moved to `tests/test_translation_keys.py`, same check logic
(cross-checks strings.json/translations/en.json's `state` blocks against const.py's OPTIONS
lists), now parametrized over both translation files and all three checked entities (6 test
cases) using plain `pytest.mark.parametrize` + `assert` rather than print/return-code. `pytest`
was already available in this environment but not previously declared as a project dependency or
backed by a `tests/` directory - this is genuinely the first real test in the repo, not a second
copy of the same idea. QUALITY_SCALE.md's test-coverage entry updated to note this honestly: a
first real test exists now, but it's not the deliberate full pass (fixtures, mocked API
responses, `pytest-homeassistant-custom-component`, coordinator/entity/config-flow coverage)
that rule actually needs - the old standalone script is deleted, not kept alongside the test.

## Boost buttons (`charge-overrides`) built

Per the user directly, matching the app's own two boost options plus a cancel action: `Boost
full charge` (indefinite override), `Boost for duration` (reads a local duration input at press
time), `Cancel boost` (deletes the active override) - three `ButtonEntity`s (`button.py`, new
platform), all charger-scoped. **NOT YET TESTED against a real account** - same write-endpoint
discipline as Ready By/Target Charge/Charge Priority before their live confirmation; built and
compiled/offline-verified only.

Request body confirmed via the account's public OpenAPI schema (`ChargeOverrideRequestDTO`), not
guessed: `{requestedAt: <required>, endAt: <nullable>}`, with `endAt`'s own schema description
stating "Omit or pass null for an indefinite (Always On) override". `DELETE
/chargers/{ppid}/charge-overrides` (Cancel boost) is also schema-confirmed, no body.

**Correction after live testing**: Boost for duration worked first try; `endAt: null` for Boost
full charge was rejected by the server (403), and separately, the user triggered "Full charge"
from the app itself and observed a real end time exactly 12 hours out - not indefinite at all.
The schema documenting `endAt: null` as a valid, accepted value doesn't mean this account's
server actually honours it, and more importantly the app's own "Full charge" was never indefinite
in the first place - a wrong assumption corrected by real evidence, not just the 403. Fixed:
`PodHomeBoostFullChargeButton` now sends `endAt = requestedAt + 12h` (new `_FULL_CHARGE_DURATION`
constant, button.py) instead of `None`. `async_create_charge_override()`'s `end_at` parameter
stays nullable in the client (the schema-documented option, kept in case it's ever useful
elsewhere or the 403 turns out to be narrower than it looks), but the docstring no longer asserts
it works - callers should pass a real end time, per what's now confirmed live.

`_async_write()` (podpoint_mobile_api/client.py) already generalized over HTTP method via
`session.request(method, ...)`, so adding POST/DELETE support was two thin wrappers
(`_async_post`/`_async_delete`) rather than new request-handling logic - `_async_delete` passes
`json_body=None` to `_async_write`, which aiohttp treats as "no JSON payload" (widened that
parameter's type hint from `dict` to `dict | None` to make this explicit rather than implicit).

**Boost duration (`time.py`) is a genuinely local entity, not backed by the coordinator/API at
all** - per the user directly, reusing `TimeEntity`'s hh:mm picker widget to represent a
*duration* (H hours M minutes) rather than a wall-clock time, since HA has no dedicated duration
entity domain. There's no "configured boost duration" API field to read back (it's purely a
button-press-time parameter), so this entity's `native_value` comes from `self._value`, a plain
instance attribute, not `self.charger`. Persists across restarts via `RestoreEntity` mixed in
alongside `PodHomeEntity`/`CoordinatorEntity` - the actual restore-plus-coordinator MRO
interaction is unverified beyond compiling, like everything else above the API layer; worth
confirming this combination behaves as expected the first time it's actually restarted inside
real HA. Defaults to 15 minutes if never set - deliberately short, requiring an explicit increase
for a longer boost rather than the reverse.

`Boost for duration`'s button reads the duration entity's current value via the entity registry
+ `hass.states.get()` (`_read_boost_duration()` in button.py) rather than holding a direct Python
object reference to the `time.py` entity instance - the two are constructed independently by
separate platform `async_setup_entry` calls, so the entity registry + state machine is the
standard HA cross-entity read pattern here, not a workaround.

None of the three buttons, nor the duration input, carry an `entity_category` - per the user
directly, these belong in the device page's "Controls" group (interactive, act-on-now entities),
not "Configuration" (a background preference like Charge Priority) - matching HA's own frontend
grouping convention, not just a style preference.

**Cancelling a boost doesn't immediately clear the schedule server-side.** After a live Cancel
boost, the user observed the smart-charging schedule still showing some of the boost's future
intervals for a while, in both the Pod Point app itself and this integration's `Schedule`
calendar. Because the app shows the identical stale intervals, this is a server-side recompute
lag on Pod Point's backend (the schedule plan not being regenerated instantly on cancellation),
not a bug in `calendar.py`'s rendering of whatever `smart_schedule_windows` the API currently
returns - no code change made. Not confirmed how long the lag lasts or whether a manual refresh
speeds it up; noted here as an observed real-account quirk rather than investigated further.

## HACS packaging + README rewrite

`hacs.json` added (minimal shape: `name` + `render_readme`) - no `homeassistant` minimum-version
key set, since no real floor has ever been tested against; no `country`/`zip_release` fields,
neither applies. `.github/workflows/validate.yml` added running `hacs/action` and
`home-assistant/actions/hassfest`, matching what HACS's own default-repository validation runs -
this repo isn't on that list yet, but the workflow catches the same class of manifest/hacs.json
mistakes early regardless. LICENSE already existed from before this session.

README.md rewritten from dev-notes into end-user documentation (installation via HACS custom
repository or manual copy, setup steps, the full current entity list, Energy Dashboard and
Charging Mode guidance, a "Known limitations" section). Confirmed while writing the installation
section that manual/HACS install genuinely needs no separate `pip install` step: all three
`podpoint_mobile_api` imports in the shipped integration (`__init__.py`, `config_flow.py`,
`coordinator.py`) are relative (`from .podpoint_mobile_api import ...`), resolving to the vendored
copy under `custom_components/pod_home/podpoint_mobile_api/`, not the standalone package -
`manifest.json`'s empty `requirements` was previously documented (old README) as meaning HA
"won't auto-install the package... unless it's manually pip-installed first," which was already
wrong by the time that sentence was read again now - the vendoring makes the integration
self-contained. Old dev-notes README also claimed charge-overrides had "produced no reaction
until the charger's own next check-in" as an unqualified statement; kept that pull-based/~5-minute
latency behaviour in the new README's "Known limitations" section, since it's still accurate and
relevant to the now-implemented boost buttons.

## Correction: single-rate tariff does NOT revert to Basic Charging

The earlier `smart_charging_supported` entry above ("selecting a tariff with more rates, or one
where the supplier controls charging directly, reverts the account to Basic Charging
automatically") conflated two different things under "supplier controls charging directly."
Corrected, per the user directly: a single-rate tariff does **not** cause a revert to Basic
Charging - Smart Charging keeps working on one, with Pod Point coordinating directly with the
supplier to charge during low-demand periods. That supplier coordination on a single-rate tariff
is normal Smart Charging behaviour, not a trigger for reverting to Basic. The only confirmed
revert trigger is a tariff with more than two rate windows. `smart_charging_supported` itself
(coordinator.py/sensor.py) still reflects live API data either way and needed no code change -
this was a documentation-only error in how that flag's cause was described in README.md/
DECISIONS.md, not in the code that reads it.

## HACS packaging README follow-ups from live feedback

Three corrections/restructures to the README.md written above, from the user's own review of it:

- **Energy Dashboard**: the user's actual dashboard uses **Total energy** for the energy panel,
  not Month energy - corrected the recommendation. Total energy is the live-inclusive running
  total (a session in progress counts immediately, see its docstring in sensor.py), Month energy
  lags until each session finalizes; Month cost is still the one used for the cost panel, since
  no live-inclusive Total cost sensor exists.
- **Charging Mode**: see the correction entry directly above.
- **"What you get"**: restructured into two tables (charger device, car device) rather than prose
  paragraphs, per the user's request to call out that these are genuinely two separate HA devices.
  First pass of that table wrongly placed Ready by/Target charge under the charger device - both
  are actually `PodHomeVehicleEntity` (time.py/number.py), i.e. car-device entities, confirmed
  against `_MODE_GATED_ENTITIES` in entity.py (`("time", "_ready_by", "vehicle")`,
  `("number", "_target_charge", "vehicle")`). Moved to the car device table alongside Expected
  charge, which is also vehicle-scoped and Smart-Charging-gated the same way.

## README "What you get" split into three devices, grouped by category

Per the user's request to add category/split the tables further. Two corrections made along the
way:

- **A third device exists and was missing entirely**: Rewards balance is `PodHomeAccountEntity`
  (entity.py), grouped under its own account-level "Pod Point" device (one per config entry,
  `DeviceInfo(identifiers={(DOMAIN, config_entry.entry_id)})`) - not the charger device where the
  first table pass had it. Added as its own third device section.
- **Category column reframed as a per-device split into Sensors/Controls/Configuration/
  Diagnostic tables**, matching how Home Assistant's own device page actually groups entities:
  `entity_category` (`CONFIG`/`DIAGNOSTIC`) drives Configuration/Diagnostic directly; everything
  uncategorized then splits by domain into Sensors (`sensor`/`binary_sensor`) or Controls
  (everything else this integration ships - `button`, `number`, `select`, `time`, `update`,
  `calendar`). The Controls-domain-set part of this (which non-sensor domains land in Controls)
  is stated with the same confidence as the earlier live conversation on this topic - a
  well-established HA frontend convention, not something re-verified against frontend source for
  this doc pass.

## README "Known limitations" - two bullets removed

Per the user directly: the boost-latency bullet (~5 min pull-based command latency) isn't
actually a limitation of this integration - the app has the exact same latency, since it's a
property of the charger's own check-in cadence, not something either client controls. Removed
rather than reworded. The "remote cable lock isn't implemented yet" bullet was also removed for
now, per the user's request - remote-lock remains untouched in the code/PLAN.md either way, this
is a doc-only removal, not a decision to drop it from scope.

## tests/test_helpers.py - offline coverage for helpers.py's pure functions

First of two planned pieces (per the user, offline first, full HA suite second - see
QUALITY_SCALE.md). 90 cases across every function in helpers.py.

**Loading problem solved**: helpers.py has zero Home Assistant dependency, but its
`from .const import ...` is a *relative* import, which fails if imported as a bare top-level
module (`ImportError: attempted relative import with no known parent package` - confirmed by
trying it). test_translation_keys.py's existing `sys.path` trick only works for const.py because
const.py itself has no relative imports. Importing the real `custom_components.pod_home` package
to get a proper parent isn't an option either - its `__init__.py` unconditionally imports
`homeassistant`, not installed in this environment. Solved with a new `tests/_pod_home_loader.py`:
registers a synthetic `pod_home` module in `sys.modules` (a bare `types.ModuleType` with
`__path__` set to the real source directory, never executing the real `__init__.py`), then loads
const.py/helpers.py as its submodules via `importlib.util.spec_from_file_location` - this lets
helpers.py's relative import resolve normally. Reusable by any future offline test file that
needs const.py/helpers.py.

**Fixture approach**: PodHomeCharger/PodHomeCharge/PodHomeTariffWindow/etc. are real dataclasses,
but they're defined in coordinator.py, which imports Home Assistant heavily at module level - not
importable the same way. Since every helpers.py function under test only does plain attribute
access on these objects (duck typing, and type hints are just strings under
`from __future__ import annotations` so nothing enforces the real dataclass at runtime),
test_helpers.py uses small `SimpleNamespace`-based factory functions (`_charger()`, `_charge()`,
`_tariff_window()`, etc.) instead of importing coordinator.py at all - matches CLAUDE.md's
"hand-built fixture data" description of how this was always meant to be tested.

**Coverage**: every `CHARGING_STATE_*` → `CHARGER_STATUS_*` mapping in `charger_status()`
including all four Finished-sticky scenarios (sticky while nothing newer, cleared by a newer
charging_started_at, cleared by a newer cable_unplugged_at, never sticky without
charge_finished_at); `select_last_charge()`'s same-session/different-session/tolerance branches;
`is_single_rate_tariff()`/`charging_priority_label()`/`max_price_for_charging_priority()`
including the single-rate "can't disambiguate" case and a round-trip check between the write and
read sides; `expand_manual_schedule_events()`'s same-day/midnight-crossing/week-boundary-wrap/
inactive-window/missing-fields cases (the midnight-crossing and week-wrap cases match the
function's own docstring caveat that they're "not confirmed to occur live" - tested for
correctness of the code as written, not as a claim these shapes have been seen from the real
API); `smart_schedule_events()`'s CHARGING-only inclusion and range-clamping; and
`cumulative_charging_seconds()`'s window-clipping and the "real 0 vs. None" distinction its own
docstring calls out.

## pytest-homeassistant-custom-component installed - real HA test harness, first slice: config flow

Second half of the two-part plan (offline helpers.py tests first, full HA suite second - per the
user). Installed globally into this dev machine's system Python (no venv exists for this repo,
matching how `podpoint-mobile-api` was already installed - `pip install -e podpoint-mobile-api`,
confirmed via `pip show`). **Worth knowing**: this pulled in `homeassistant==2025.1.4` and ~100
packages, and `pip` reported real version conflicts against other unrelated projects on this
machine sharing the same global Python (esphome, androguard, aioesphomeapi - downgraded
`cryptography`/`aiohttp`/`pillow`/`jinja2`/`pyyaml`/`requests`/`voluptuous`/`bleak` versions those
projects had pinned higher). Not fixed or investigated further - out of this repo's scope - but
flagged here since it's a real side effect of work done in this session, not contained to
`podpoint`.

**Four genuine environment problems hit and solved, in order** (all Windows-specific; irrelevant
on the Linux CI Home Assistant's own test suite runs on, which is presumably why none of this is
documented anywhere obvious upstream):

1. **The plugin is disruptive session-wide once installed.** Just having
   `pytest-homeassistant-custom-component` importable makes pytest auto-load its plugin for
   *every* test in the session (entry-point-based), which swaps pytest-asyncio's `event_loop`
   fixture for HA's own and blocks real sockets via `pytest-socket` - broke the already-passing
   `tests/test_helpers.py`/`tests/test_translation_keys.py` immediately on install, even though
   neither file touches Home Assistant. Fixed with a root `pytest.ini` (`-p no:homeassistant`,
   entry-point name confirmed via `importlib.metadata.entry_points(group="pytest11")` - it's
   `homeassistant`, not the package's own dotted name) plus `--ignore=tests/integration`, so a
   bare `pytest`/`pytest tests/` only ever collects the offline suite.
2. **Windows' `ProactorEventLoop` needs a real loopback socket just to exist.** Its internal
   self-pipe uses `socket.socketpair()`, which pytest-socket's blocking (still active once the
   plugin is deliberately re-enabled for `tests/integration/`, see below) intercepts before any
   test code runs, raising `SocketBlockedError` during `event_loop` fixture setup. Fixed with
   `--force-enable-socket` in `pytest.ini` - not a real loss of coverage, since every actual
   network call in these tests is mocked at the auth-client boundary anyway (see point 4).
3. **The async `hass` fixture wasn't being awaited by fixtures that depend on it**
   (`enable_custom_integrations(hass)` received the raw `async_generator`, not a real
   `HomeAssistant`, and crashed with `AttributeError: 'async_generator' object has no attribute
   'data'`). Fixed with `asyncio_mode = auto` in `pytest.ini`.
4. **`homeassistant`'s `async_get_clientsession` hardcodes `aiohttp`'s `AsyncResolver`**, which
   requires `aiodns` to be installed at all (so `aiodns` must stay pinned to the exact version
   `homeassistant==2025.1.4` declares, `==3.2.0` - a newer `aiodns` resolved by default breaks the
   pin) *and* requires the running event loop to be either a plain `SelectorEventLoop` or (on
   Windows, off the default Proactor policy) the exact `winloop.Loop` instance - neither of which
   `homeassistant.runner.HassEventLoopPolicy` (a thin subclass of `asyncio.DefaultEventLoopPolicy`,
   which is `WindowsProactorEventLoopPolicy` on Windows) will ever hand back, installing `winloop`
   or not. Rather than fight the event loop policy itself (would mean monkeypatching
   `asyncio.DefaultEventLoopPolicy` before `homeassistant.runner` is first imported - fragile,
   invasive, and pointless for what these tests actually need), `test_config_flow.py` patches
   `custom_components.pod_home.config_flow.async_get_clientsession` directly in its autouse
   `no_real_setup` fixture. Architecturally clean, not just a workaround: every test in this file
   already independently mocks `PodHomeAuth.async_get_id_token`, which never touches the session
   object it's given, so the session's realness was never relevant to what's under test.

**Subtree isolation, and why it's a *separate* pytest invocation, not a flag**: `tests/integration/
conftest.py` re-declares `pytest_plugins = ["pytest_homeassistant_custom_component.plugins"]`
(the dotted submodule, not the bare package name - the bare name has no fixtures, confirmed by
trying it first and getting `fixture 'enable_custom_integrations' not found` even though it's
right there in `plugins.py`). Tried re-enabling via a `-p homeassistant` CLI flag instead of the
nested `pytest_plugins` declaration first (avoids pytest's "nested conftest declaring
pytest_plugins" deprecation error when the parent `tests/` dir is *also* being collected in the
same session) - didn't work: `--force-enable-socket` stopped being honoured with that toggle
order for reasons not fully root-caused, socket blocking came back. Reverted to the
`pytest_plugins` declaration, which does work correctly - but only when `tests/integration/` is
targeted directly as its own pytest invocation, never together with `tests/` in one run (confirmed
both ways: `pytest tests/integration/` - 6 passed; `pytest tests/` with the nested
`pytest_plugins` restored - fails at collection with pytest's non-top-level-conftest error).
`--ignore=tests/integration` in the root `pytest.ini` keeps the two suites from ever colliding
by accident. This is a real operational split, not a temporary workaround: **`pytest tests/
homeassistant/` must always be run separately from `pytest tests/`**, documented in CLAUDE.md's
Verification section.

**Coverage landed**: `tests/integration/test_config_flow.py` - user flow (success, invalid
auth), duplicate-email abort (including case-insensitivity, since `async_set_unique_id`
lowercases the email before comparing), reauth flow (success updates the stored password and
leaves the email unchanged; invalid auth shows an error and leaves the password unchanged). Every
test patches `custom_components.pod_home.async_setup_entry` to a no-op success - these tests are
about the flow's own behaviour, not about the coordinator's first refresh actually succeeding,
which stays part of the still-open coordinator/entity test-coverage gap (see QUALITY_SCALE.md).

## .github/workflows/test.yml - both test suites moved to CI

Per the user directly: run the tests on GitHub instead of only locally. Four jobs, all
`ubuntu-latest`: `lint` (py_compile/pyflakes across both `custom_components/pod_home/` and
`podpoint-mobile-api/`), `podpoint-mobile-api` (installs it with `pip install -e` and imports it -
the one existing check beyond compiling for that package), `offline-tests` (`pytest tests/`, just
`pytest` itself as a dependency), `integration-tests` (`pip install -r requirements-test.txt`
then `pytest tests/integration/`, kept as its own separate invocation on CI too - the
`pytest.ini`/`conftest.py` split documented in the previous entry applies here unchanged).

Deliberately not folded into the existing `.github/workflows/validate.yml` (hacs/action +
hassfest) - different concern (test correctness vs. HACS/manifest schema validation), separate
file keeps each focused and lets one fail without obscuring the other in the Actions UI.

Expected to be markedly less fragile than the local Windows run that led to this suite existing:
GitHub's runners are Linux, `pytest-homeassistant-custom-component`'s actual target platform - none
of the four Windows-specific problems from the previous entry (ProactorEventLoop's socketpair,
aiodns/winloop's event-loop-type requirement) should apply there. The `async_get_clientsession`
mock in `test_config_flow.py` stays regardless - it's the architecturally correct choice on any
platform (isolates the config-flow test from real network entirely), not merely a Windows
workaround. Not yet confirmed green on an actual GitHub Actions run - first push will tell.

## First real CI run failed both test jobs - two genuine bugs in the workflow itself, fixed

Checked the actual run (`gh run view`) rather than assuming green from "not yet confirmed" above.
Both `offline-tests` and `integration-tests` failed, neither for a Linux-vs-Windows reason -
both were bugs in `test.yml` itself:

1. **`offline-tests` failed with `pytest: error: unrecognized arguments: --force-enable-socket`.**
   Root cause: `--force-enable-socket` was living in the *shared* root `pytest.ini`'s `addopts`,
   which every pytest invocation reads - including `offline-tests`, whose job never installs
   `pytest-homeassistant-custom-component` (so `pytest-socket`, the plugin that owns that flag,
   isn't present either). An unrecognized CLI option is a hard error, unlike an unknown ini key
   (only warns) - so this broke immediately. Fixed by moving `--force-enable-socket` (and
   `asyncio_mode`, same reasoning, passed as `-o asyncio_mode=auto`) out of the shared
   `pytest.ini` entirely, onto the `integration-tests` job's own command line only. Root
   `pytest.ini` now only has `-p no:homeassistant --ignore=tests/integration` - both safe
   unconditionally, neither depends on a plugin being installed.
2. **`integration-tests` failed with `ModuleNotFoundError: No module named 'custom_components'`.**
   Root cause: `test.yml` ran bare `pytest tests/integration/ -v`. Only `python -m pytest` (not
   the standalone `pytest` script) inserts the current working directory onto `sys.path` - bare
   `pytest` relies purely on its own rootdir-insertion logic, which for a test file with no
   `__init__.py` anywhere above it only adds that file's *own* directory (`tests/integration/`),
   never the repo root three levels up. Every local verification in the previous entry used
   `python -m pytest ...` throughout (habit, not a deliberate choice at the time) and so never hit
   this - the workflow file was the one place still using bare `pytest`. Fixed by switching both
   `test.yml` jobs to `python -m pytest`, and documented as a hard requirement in
   `tests/integration/conftest.py`'s docstring and CLAUDE.md so it isn't silently reintroduced.

Neither bug was actually Windows/Linux-specific - both would have broken a local run too if
someone had copied `test.yml`'s exact command lines instead of the ones this file's own docstring
documents. Retested locally after the fix (`python -m pytest tests/integration/
--force-enable-socket -o asyncio_mode=auto` matching the corrected `test.yml` exactly): 6 passed.

Confirmed on the actual next GitHub Actions run (`33545679973`): all four jobs green -
`lint`, `podpoint-mobile-api`, `offline-tests`, `integration-tests`.

## `tests/homeassistant/` renamed to `tests/integration/`

Per the user directly: a subfolder named "homeassistant" read oddly for a repo where *everything*
is Home Assistant code - it looks like it's claiming to be the one place that tests Home
Assistant, when what it actually means is "the tests that need a running HA test harness."
`tests/integration/` says that directly, using the standard, framework-agnostic unit-vs-
integration split any Python developer already recognizes, rather than an HA-specific-sounding
name. CI job renamed to match (`homeassistant-tests` -> `integration-tests`).

**Tried first, confirmed not viable**: merging both suites into one unified pytest session (so
there'd be no split at all) - the actual root cause of the "weird" structure. Set
`--force-enable-socket`/`asyncio_mode = auto` globally and let the plugin auto-load for the whole
`tests/` tree, no `-p no:homeassistant`/`--ignore` anywhere. Broke immediately: even with the
right flags, `pytest-homeassistant-custom-component`'s other autouse fixtures still interfere
with plain synchronous tests that never asked for a `hass` fixture at all - `tests/test_helpers.py`
went from 90 passing to 96 errors (event_loop fixture failures on tests that don't use asyncio).
Confirmed this isn't fixable by tuning ini options further - it's a structural conflict between
"a session where HA's test harness is active" and "a session where it isn't," not a matter of
getting the right flags. The two-invocation split stays; only the naming changed.

Every path/job-name reference updated together (`pytest.ini`, `tests/integration/conftest.py`,
`.github/workflows/test.yml`, `requirements-test.txt`, CLAUDE.md, QUALITY_SCALE.md) - retested
both suites after the rename (`python -m pytest tests/` - 90 passed; `python -m pytest
tests/integration/ --force-enable-socket -o asyncio_mode=auto` - 6 passed) before pushing.

## Merged into one unified `tests/` suite - CI (Linux) is now the authoritative verification, not this dev machine

Per the user directly, in immediate follow-up to the rename above: still felt wrong to have a
split at all, and asked to remove the Windows-specific workarounds - now that CI actually runs on
Linux, that's the real verification environment, not this local Windows dev machine. The earlier
"tried first, confirmed not viable" merge attempt (previous entry) was re-examined with that
framing: it failed with the exact same Windows-only `ProactorEventLoop`/`socket.socketpair()`
mechanism documented in the very first HA-harness entry, not a fundamental cross-platform
incompatibility between "sync tests" and "the plugin active for the session." Nothing in that
failure mode is Linux-specific - Unix event loops build their self-pipe via `os.pipe()`, not a
blocked `socket.socketpair()` call, so pytest-socket's blocking shouldn't intercept it there.
Decided to trust that reasoning and restructure for real, verifying on the actual CI (Linux)
rather than this local (Windows) machine, since local Windows repro is now a known-broken,
diagnosed-and-accepted gap, not a signal to act on.

**Restructure**: `tests/integration/` merged back into flat `tests/` - `test_config_flow.py`
moved up, `tests/integration/conftest.py`'s content merged into a single `tests/conftest.py` that
just registers the plugin unconditionally (`pytest_plugins` removed entirely - no `-p
no:homeassistant` anywhere left to fight against, so the entry-point auto-load is the only
registration path now, avoiding the "Plugin already registered under a different name" error that
a redundant explicit `pytest_plugins` alongside auto-load produces). Root `pytest.ini` reduced to
just `asyncio_mode = auto` - no `-p no:homeassistant`, no `--ignore`, no `--force-enable-socket`
(not needed at all once there's no Windows-specific ProactorEventLoop construction path forcing
it - removed rather than kept "just in case", since keeping unnecessary Windows-only flags around
is exactly the clutter being removed here). `requirements-test.txt` reduced to just
`pytest-homeassistant-custom-component` - the explicit `aiodns==3.2.0` pin and the
Windows-conditional `winloop` line both dropped: `aiodns==3.2.0` was only needed because this
specific local machine had a stray newer `aiodns` from an earlier manual `pip install` polluting
things, not something a fresh CI environment would ever hit (installing `homeassistant` alone
already hard-pins `aiodns==3.2.0` via its own declared dependencies); `winloop` was purely a
Windows/ProactorEventLoop fix with no Linux relevance at all now that the whole flow it was
patching around isn't reachable. `test.yml`'s four jobs collapsed to three - `offline-tests` and
`integration-tests` merged into one `tests` job (`pip install -r requirements-test.txt` then
`python -m pytest tests/ -v`); `lint` and `podpoint-mobile-api` stay separate, different concerns.

**Locally (Windows) this is now expected to fail** - confirmed: `python -m pytest tests/ -q`
after this change produces 96 errors, the exact same `ProactorEventLoop`/`_ssock` construction
failure as every earlier Windows repro in this file. Deliberately not chased further or worked
around again - CLAUDE.md's Verification section now says plainly that `.github/workflows/
test.yml` is the authoritative place this suite is checked, and a local Windows failure here is
expected, not a regression. Not yet confirmed green on the actual next GitHub Actions run at the
time of writing this entry - check the Actions tab (or `gh run list --workflow=test.yml`) for the
real result rather than trusting this paragraph's expectation.

## requirements-test.txt renamed to requirements_test.txt

Per the user directly: underscore, not hyphen, matching Python module-name convention rather than
a package/CLI-name convention. Cosmetic - `.github/workflows/test.yml` and CLAUDE.md updated to
match. Earlier entries above still say `requirements-test.txt` where they're describing what was
true at the time - left as written, per this file's append-only rule.

## Boost live-confirmed in full; remote-lock deliberately deprioritized; HACS validate.yml root cause confirmed

Three things from the user directly, in response to the "what's next" options presented:

- **Boost full charge and Cancel boost both confirmed working live** - the last two boost pieces
  that were built-but-untested. All three boost buttons (Full charge, Boost for duration, Cancel
  boost) are now live-confirmed. `button.py`'s module docstring updated (no longer says "NOT YET
  TESTED"); PLAN.md updated to match.
- **`remote-lock` deliberately deprioritized, not just deferred**: the user's own charger doesn't
  support it, so there's no way to test it live even if built - matches this project's standing
  rule (CLAUDE.md's write-endpoint discipline: don't mark a write as working without live
  confirmation, and by extension don't build toward something that can never get that
  confirmation on this account). Revisit only if that changes.
- **`validate.yml`'s `hacs` job root cause confirmed, not just suspected**: GitHub topics were
  added (`gh repo view` confirms `ev-charging`/`home-assistant`/`pod-point`/`pod-home` are
  genuinely set), but the `hacs` job's `topics` check still fails identically to before, alongside
  `hacsjson`/`integration_manifest`. This confirms the earlier hypothesis - HACS's validator
  can't read repository metadata (topics, file contents) on a **private** repo at all, regardless
  of what's actually there. Repo stays private for now (user's call); this check will keep
  failing until that changes - not a content bug to keep chasing.

## Two of the four real-HA validation gaps confirmed live; reauth needs no code, just live confirmation

- **Diagnostics ZIP - confirmed live.** The user attached a real downloaded diagnostics file
  (`config_entry-pod_home-*.json`, HA 2026.8.3 on HAOS/aarch64). Matches what diagnostics.py is
  supposed to produce exactly: `chargers` keyed by ppid, `vehicle: "**REDACTED**"`,
  `firmware.serial_number: "**REDACTED**"`, no `entry.data`/credentials anywhere - confirms both
  the redaction logic and the "entry.data left out entirely" correction just made to
  QUALITY_SCALE.md above are actually true live, not just true in the source.
- **Energy This Month/Cost This Month month rollover - confirmed live.** Per the user directly:
  already observed resetting correctly at a real rollover, two days before this was asked about.
- **Reauth "raise an issue" - clarified and resolved without any code change.** The user's first
  ask ("can we raise an issue for the re-auth flow?") was initially misread as "open a GitHub
  issue to track testing this" - a GitHub issue (`jeverley/ha-pod-home#1`) was created, then the
  user corrected: they meant a real Home Assistant **Repair issue** (Settings → Repairs), i.e.
  should `pod_home` be creating one itself when reauth is needed. Confirmed by reading
  `homeassistant/config_entries.py` directly (the `pytest-homeassistant-custom-component`-pinned
  `homeassistant==2025.1.4` installed locally) rather than guessing: `_async_init_reauth` already
  creates exactly this Repair issue automatically, for every integration, whenever
  `ConfigEntryAuthFailed` is raised and the resulting reauth flow doesn't auto-complete (shows a
  form) -
  ```python
  issue_id = f"config_entry_reauth_{self.domain}_{self.entry_id}"
  ir.async_create_issue(
      hass, HOMEASSISTANT_DOMAIN, issue_id, data={"flow_id": result["flow_id"]},
      is_fixable=False, issue_domain=self.domain, severity=ir.IssueSeverity.ERROR,
      translation_key="config_entry_reauth", translation_placeholders={"name": self.title},
  )
  ```
  `pod_home` already does both halves this depends on (`coordinator.py` raises
  `ConfigEntryAuthFailed`; `config_flow.py` implements `async_step_reauth`/
  `async_step_reauth_confirm`) - so this was already correctly wired up with zero code change
  needed. The mistaken GitHub issue was closed rather than left open. **Not the same thing as the
  `repair-issues` quality-scale rule** (checked separately via WebFetch to HA dev docs) - that
  rule is about an integration raising its own issues for *other* fixable problems it detects,
  unrelated to reauth's own dedicated (and automatic) mechanism; still correctly listed as
  deferred/nice-to-have in QUALITY_SCALE.md, this finding doesn't change that.

## Correction: "diagnostics ZIP" was wrong - it's a plain JSON file download

Per the user directly. Every prior mention in this file and PLAN.md called HA's diagnostics
download a "ZIP" - it isn't, at least not for this integration/HA version: Settings → Devices &
services → Pod Home → ⋮ → Download diagnostics produces a single `.json` file (matches the
`config_entry-pod_home-*.json` filename on the real file the user attached earlier). PLAN.md's
wording fixed to "diagnostics download contents" rather than "ZIP contents" - this file's own
earlier "Diagnostics ZIP - confirmed live" entry left as written per the append-only rule, but
the underlying fact it confirmed (redaction working correctly, no credentials in the output) is
unaffected by the file-format wording being wrong.

## Real bug found live: reauth immediately re-failed after a correct password change

The user tested reauth live: the Repair issue fired correctly (confirming the earlier finding
that HA core creates it automatically), and submitting the new password in the reauth form was
accepted and the issue disappeared - but a new one **immediately reappeared** with the same 401,
even though the password was genuinely correct.

**Root cause**: `__init__.py`'s `async_setup_entry` always calls `auth.import_tokens(auth_data)`
with the Firebase refresh token persisted in a `Store` keyed by `entry.entry_id` - which does
*not* change across a reauth (reauth updates the existing config entry and reloads it, it doesn't
create a new one). `config_flow.py`'s `async_step_reauth_confirm` validated the *new* password
correctly (via its own throwaway `PodHomeAuth` instance, which never touches the Store), then
called `async_update_reload_and_abort`, which reloads the entry - and that reload's
`async_setup_entry` unconditionally restored the *old*, pre-password-change refresh token from
Store. `PodHomeAuth.async_get_id_token()` (auth.py) prefers refreshing an existing refresh token
over signing in fresh whenever one is present, regardless of what `self._password` currently is -
so every subsequent request kept trying to use the stale session tied to the old password,
getting rejected by mobile-api (401) even though Firebase itself may have accepted the refresh
briefly (explains the "repair disappears, then immediately reappears" pattern - a genuine sign-in
with the new password never actually happened at the production `PodHomeAuth` instance).

**Fix**: `config_flow.py`'s `async_step_reauth_confirm` now clears that Store
(`Store(hass, AUTH_STORAGE_VERSION, auth_store_key(entry_id)).async_remove()`) immediately after
validating the new password, before reloading - forcing the reloaded `PodHomeAuth` to sign in
fresh with the new password instead of trying to reuse the old session.
`AUTH_STORAGE_VERSION`/`auth_store_key()` moved to const.py so both `__init__.py` and
`config_flow.py` share the exact key-construction logic rather than duplicating the f-string
(a second inconsistent copy would have silently reintroduced this same class of bug).
`Store.async_remove()` is confirmed safe to call even when no file exists yet (HA core wraps the
delete in `suppress(FileNotFoundError)`) - matters for the fresh-install/first-ever-setup case,
and for the existing `tests/test_config_flow.py` reauth tests, which don't mock `Store` and so
now exercise a real (no-op, ephemeral test storage dir) removal.

**Not yet re-tested live** - this is a real fix for a confirmed live bug, but per this project's
write-endpoint/live-confirmation discipline, needs the user to actually force another auth
failure and confirm reauth now sticks before this is considered closed.

## Reauth fix confirmed working live

Per the user directly: "confirmed resolved with new code." Reauth now sticks after a real forced
auth failure and password change - the stale-refresh-token bug above is fixed for real, not just
in reasoning. [jeverley/ha-pod-home#1](https://github.com/jeverley/ha-pod-home/issues/1) closed
with that confirmation. Forced-failure log-dedup behavior (`_warn_once`/`_clear_warning`) wasn't
explicitly called out as confirmed alongside this - still open in PLAN.md.

## Real HA log from the reauth test - top-level auth logging confirmed, _warn_once still not exercised

The user attached a real downloaded HA log spanning the forced-failure/reauth/recovery window.
Read closely rather than taken as blanket confirmation of "log behavior under a forced failure":

- The 401 at `11:19:15` produced exactly one `ERROR "Authentication failed while fetching
  pod_home data: ..."` line plus a `DEBUG "Full error:"` traceback, then clean recovery (two
  subsequent polls both `Finished fetching pod_home data ... success: True`, no repeated ERROR
  spam). Correct, expected behavior.
- But that logging comes from **HA core's own `DataUpdateCoordinator`** - the traceback shows
  `homeassistant/helpers/update_coordinator.py:435` calling `_async_update_data()`, which in
  `coordinator.py` just does `except PodHomeAuthError as exc: raise ConfigEntryAuthFailed(str(exc))
  from exc` - no pod_home logging call of its own on this path at all.
- `_warn_once`/`_clear_warning` (the actual mechanism QUALITY_SCALE.md's `log-when-unavailable`
  entry describes) is only used for non-fatal per-endpoint failures inside `_safe_call` -
  tariffs/firmware/rewards/api3 session/api3 charges. None of those call sites' messages (nor
  any `INFO "Recovered: ..."`) appear anywhere in the log, because a top-level auth failure
  doesn't reach any of them - the whole poll fails at the first API call
  (`async_list_chargers`), before those individual endpoints are ever attempted.
- **Conclusion**: the auth-failure logging path is confirmed correct and not spammy. The
  `_warn_once`/`_clear_warning` dedup mechanism itself remains genuinely unconfirmed - would need
  a *non-fatal* single-endpoint failure (e.g. tariffs briefly erroring while the rest of the poll
  succeeds) to actually exercise, not a full auth outage. PLAN.md worded to reflect this split
  rather than mark the whole gap closed.

Also noted, unrelated: a `WARNING ... blocking call to import_module ...
custom_components.pod_point.config_flow` in the same log is about the old community `pod_point`
integration (mattrayner's), not `pod_home` - not something to act on here.
