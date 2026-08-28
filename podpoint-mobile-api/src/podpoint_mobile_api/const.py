"""Constants for the podpoint-mobile-api client itself - not Home Assistant-specific, this
package has zero HA dependency (see py.typed / pyproject.toml: only aiohttp)."""
from __future__ import annotations

from datetime import timedelta

# --- Auth (Firebase Identity Toolkit) ---
# The confirmed-working prod/Android/password-sign-in Firebase Web API key (project
# "opencharge-mobile-app-e376b"). It identifies the Firebase project, not a secret credential -
# Google's own guidance is that Web API keys are safe to ship client-side.
FIREBASE_API_KEY = "AIzaSyDmwzY9NKXxEmqvwiGrp4TLGy7dJ0G2XiM"
FIREBASE_SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
FIREBASE_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
# Refresh proactively this long before the token's reported expiry, rather than waiting for a
# 401 mid-poll.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

# --- mobile-api.pod-point.com ---
MOBILE_API_BASE_URL = "https://mobile-api.pod-point.com"
