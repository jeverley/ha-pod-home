"""Constants for the Pod Home integration.

Comments below note which values are confirmed against live API responses vs. still a best
guess.
"""
from __future__ import annotations

DOMAIN = "pod_home"
NAME = "Pod Home"
ATTRIBUTION = "Data provided by Pod Point's mobile-api (unofficial integration)"
MANUFACTURER = "Pod Point"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

# Firebase refresh-token Store (see __init__.py's async_setup_entry) - one per config entry,
# shared with config_flow.py, which must clear it on a successful reauth (see DECISIONS.md: a
# stale refresh token from before a password change must not survive a reauth).
AUTH_STORAGE_VERSION = 1


def auth_store_key(entry_id: str) -> str:
    """Storage key for the Firebase auth-token Store."""
    return f"{DOMAIN}_{entry_id}_auth"

# connectivity-status-v2 wire values. No _OPTIONS list - unlike CHARGING_STATE_*, nothing
# currently validates connectionState against a closed set.
CONNECTION_STATE_ONLINE = "Online"  # confirmed live
CONNECTION_STATE_OFFLINE = "Offline"  # unconfirmed
CONNECTION_STATE_UNKNOWN = "Unknown"
CONNECTION_STATE_RECONNECTING = "Reconnecting"

CHARGING_STATE_AVAILABLE = "Available"  # confirmed live
CHARGING_STATE_PREPARING = "Preparing"
CHARGING_STATE_CHARGING = "Charging"  # confirmed live
CHARGING_STATE_SUSPENDED_EVSE = "SuspendedEVSE"  # confirmed live
CHARGING_STATE_SUSPENDED_EV = "SuspendedEV"  # unconfirmed - guessed by analogy with the EVSE one
CHARGING_STATE_FINISHING = "Finishing"
CHARGING_STATE_RESERVED = "Reserved"
CHARGING_STATE_UNAVAILABLE = "Unavailable"
CHARGING_STATE_FAULTED = "Faulted"  # unconfirmed, casing/spelling is a guess
CHARGING_STATE_UNKNOWN = "Unknown"

CHARGING_STATE_OPTIONS = [
    CHARGING_STATE_AVAILABLE,
    CHARGING_STATE_PREPARING,
    CHARGING_STATE_CHARGING,
    CHARGING_STATE_SUSPENDED_EVSE,
    CHARGING_STATE_SUSPENDED_EV,
    CHARGING_STATE_FINISHING,
    CHARGING_STATE_RESERVED,
    CHARGING_STATE_UNAVAILABLE,
    CHARGING_STATE_FAULTED,
    CHARGING_STATE_UNKNOWN,
]

# Whether each chargingState implies a cable is physically connected. None where ambiguous
# (e.g. Faulted) or uncatalogued; use .get() so an unmapped value also returns None.
CHARGING_STATE_CABLE_CONNECTED: dict[str, bool | None] = {
    CHARGING_STATE_AVAILABLE: False,
    CHARGING_STATE_PREPARING: True,
    CHARGING_STATE_CHARGING: True,
    CHARGING_STATE_SUSPENDED_EVSE: True,
    CHARGING_STATE_SUSPENDED_EV: True,
    CHARGING_STATE_FINISHING: True,
    CHARGING_STATE_RESERVED: False,
    CHARGING_STATE_UNAVAILABLE: False,
    CHARGING_STATE_FAULTED: None,
    CHARGING_STATE_UNKNOWN: None,
}
# Every CHARGING_STATE_OPTIONS value must be classified above.
assert set(CHARGING_STATE_CABLE_CONNECTED) == set(CHARGING_STATE_OPTIONS)

# /chargers' delegatedControl.status. Not shown to the user directly - see SCHEDULE_MODE_*
# below for the app's own two-value framing.
DELEGATED_CONTROL_UNKNOWN = "UNKNOWN"
DELEGATED_CONTROL_ACTIVE = "ACTIVE"  # confirmed live
DELEGATED_CONTROL_INACTIVE = "INACTIVE"  # confirmed live
DELEGATED_CONTROL_PENDING = "PENDING"

DELEGATED_CONTROL_OPTIONS = [
    DELEGATED_CONTROL_UNKNOWN,
    DELEGATED_CONTROL_ACTIVE,
    DELEGATED_CONTROL_INACTIVE,
    DELEGATED_CONTROL_PENDING,
]

# Binary mapping behind delegatedControl.status (ACTIVE vs. not), displayed as Smart/Basic.
# snake_case translation keys, not display text - see CHARGER_STATUS_* below.
SCHEDULE_MODE_SMART_CHARGING = "smart"
SCHEDULE_MODE_BASIC_CHARGING = "basic"
SCHEDULE_MODE_OPTIONS = [SCHEDULE_MODE_SMART_CHARGING, SCHEDULE_MODE_BASIC_CHARGING]

# /chargers/{ppid}/smart-schedules/active's schedule[].type. PLUGGED_IN entries are a
# point-in-time marker (a `timestamp`, not a `fromTimestamp`/`toTimestamp` range), not a
# window that can be "current" alongside PAUSED/CHARGING.
SMART_SCHEDULE_TYPE_PLUGGED_IN = "PLUGGED_IN"  # confirmed live
SMART_SCHEDULE_TYPE_PAUSED = "PAUSED"  # confirmed live
SMART_SCHEDULE_TYPE_CHARGING = "CHARGING"  # confirmed live

# Charger Status - a derived, user-meaningful state combining chargingState and the sticky
# charging/unplugged/finished timestamps (see charger_status() in helpers.py); not a wire value.
# The raw chargingState passthrough lives on its own separate entity (see CHARGING_STATE_OPTIONS
# above). Named CHARGER_STATUS_* rather than STATUS_* to avoid future collisions in this flat
# const.py; the entity itself is just named "Status" in the UI.
#
# snake_case translation keys - display text lives in strings.json/translations/en.json's
# per-entity `state` block instead.
CHARGER_STATUS_CHARGING = "charging"
CHARGER_STATUS_PAUSED = "paused"
CHARGER_STATUS_AVAILABLE = "available"
CHARGER_STATUS_PREPARING = "preparing"
CHARGER_STATUS_FINISHING = "finishing"
CHARGER_STATUS_RESERVED = "reserved"
CHARGER_STATUS_UNAVAILABLE = "unavailable"
CHARGER_STATUS_FINISHED = "finished"
CHARGER_STATUS_FAULT = "fault"

CHARGER_STATUS_OPTIONS = [
    CHARGER_STATUS_CHARGING,
    CHARGER_STATUS_PAUSED,
    CHARGER_STATUS_AVAILABLE,
    CHARGER_STATUS_PREPARING,
    CHARGER_STATUS_FINISHING,
    CHARGER_STATUS_RESERVED,
    CHARGER_STATUS_UNAVAILABLE,
    CHARGER_STATUS_FINISHED,
    CHARGER_STATUS_FAULT,
]

# vehicle.chargeState.powerDeliveryState. Not used to derive Status (see charger_status() in
# helpers.py); exposed as its own debug sensor instead.
POWER_DELIVERY_STATE_OPTIONS = [
    "UNPLUGGED",  # confirmed live
    "UNKNOWN",
    "PLUGGED_IN:CHARGING",
    "PLUGGED_IN:COMPLETE",
    "PLUGGED_IN:FAULT",
    "PLUGGED_IN:INITIALIZING",
    "PLUGGED_IN:NO_POWER",
    "PLUGGED_IN:STOPPED",  # confirmed live
]

# VehicleIntentEntryDtoImpl.dayOfWeek's full set. Used to fan a single Target Charge/Ready By
# value out across all 7 days when writing intents (the API's per-day granularity isn't exposed
# in the UI).
DAY_OF_WEEK_OPTIONS = [
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
]

# Charge Priority select's display labels - the same entity, same underlying "respect the
# schedule/cost plan vs prioritise charging over it" choice in both Charging Modes, but matching
# the app's own wording exactly per mode rather than a shared generic label (per the user's
# explicit decision - see DECISIONS.md). Smart Charging: read/write both go through maxPrice, not
# SmartChargingPreferencesDTO.chargingStrategy - see charging_priority_label()/
# max_price_for_charging_priority() in helpers.py. Basic Charging: read-only for now - see
# charge_priority_label_basic() in helpers.py and select.py's docstring for why. snake_case
# translation keys, not display text.
CHARGE_PRIORITY_LOWEST_COST = "lowest_cost"
CHARGE_PRIORITY_COMPLETE_CHARGE = "complete_charge"
CHARGE_PRIORITY_SCHEDULE = "schedule"
CHARGE_PRIORITY_ALWAYS_ON = "always_on"
CHARGE_PRIORITY_SMART_OPTIONS = [CHARGE_PRIORITY_LOWEST_COST, CHARGE_PRIORITY_COMPLETE_CHARGE]
CHARGE_PRIORITY_BASIC_OPTIONS = [CHARGE_PRIORITY_SCHEDULE, CHARGE_PRIORITY_ALWAYS_ON]
# All four, for strings.json/translations completeness checking only (test_translation_keys.py) -
# the entity itself only ever exposes one pair or the other at a time, see select.py's `options`.
CHARGE_PRIORITY_OPTIONS = CHARGE_PRIORITY_SMART_OPTIONS + CHARGE_PRIORITY_BASIC_OPTIONS

# Display-only fallback until the real per-account currency is fetched.
DEFAULT_CURRENCY = "GBP"
