"""Constants for the Pod Home integration.

Targets Pod Point's new "mobile-api" backend (used by the Pod Home app) rather than the legacy
api.pod-point.com/v4 API the old pod_point integration used. Comments below note which values
are confirmed against live API responses vs. still a best guess.
"""
from __future__ import annotations

DOMAIN = "pod_home"
NAME = "Pod Home"
ATTRIBUTION = "Data provided by Pod Point's mobile-api (unofficial integration)"
MANUFACTURER = "Pod Point"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

# No single fixed scan interval - the coordinator adapts its own polling cadence based on how
# recently the charger's own lastSeenAt has actually changed. See coordinator.py's
# FAST_POLL_INTERVAL/SLOW_POLL_INTERVAL: measured directly against a real account, the charger
# checks in with Pod Point's cloud roughly every 300s on a quiet baseline, PLUS extra
# out-of-band check-ins on physical events (plug/unplug, state transitions) - noisy enough that
# predicting an exact next-check-in time isn't reliable, so the coordinator backs off based on
# time-since-last-change instead of a fixed interval or a predicted timestamp.

# Firebase auth constants and the mobile-api base URL now live in the podpoint-mobile-api
# package (../../podpoint-mobile-api/src/podpoint_mobile_api/const.py) - they're properties of
# the API itself, not of this HA integration, so they moved with the client extraction.

# --- chargingState / connectionState wire values (connectivity-status-v2) ---
# CONFIRMED live against a real Solo 3: "Online"/"Available". Everything else here is
# plausible (same PascalCase convention, names that fit the domain) but UNCONFIRMED - none of
# these have been seen in a real response yet. Treat status/availability logic built on the
# unconfirmed ones as provisional until seen live (e.g. during an actual charge, or a
# deliberately-caused fault/offline state).
CONNECTION_STATE_ONLINE = "Online"
CONNECTION_STATE_OFFLINE = "Offline"  # unconfirmed

CHARGING_STATE_AVAILABLE = "Available"  # confirmed
CHARGING_STATE_CHARGING = "Charging"  # unconfirmed
CHARGING_STATE_SUSPENDED_EVSE = "SuspendedEVSE"  # confirmed live
CHARGING_STATE_SUSPENDED_EV = "SuspendedEV"  # unconfirmed - guessed by analogy with the EVSE one
CHARGING_STATE_OUT_OF_SERVICE = "OutOfOrder"  # unconfirmed, casing/spelling is a guess
CHARGING_STATE_UNAVAILABLE = "Unavailable"  # unconfirmed

# All recognized chargingState values, shared between the status sensor's _attr_options and the
# coordinator's "have we seen an unrecognized value" logging - see helpers.py.
CHARGING_STATE_OPTIONS = [
    CHARGING_STATE_AVAILABLE,
    CHARGING_STATE_CHARGING,
    CHARGING_STATE_SUSPENDED_EVSE,
    CHARGING_STATE_SUSPENDED_EV,
    CHARGING_STATE_OUT_OF_SERVICE,
    CHARGING_STATE_UNAVAILABLE,
]

# Last-resort fallback if GET /users doesn't return a usable balance.currency (e.g. transient
# failure on the very first poll). Pod Point operates in both the UK and Ireland, so this is a
# real guess, not a safe universal default - it's only used until a real currency is fetched.
DEFAULT_CURRENCY = "GBP"
