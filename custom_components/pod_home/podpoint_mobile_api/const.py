"""Constants for the podpoint-mobile-api client itself - no Home Assistant dependency."""
from __future__ import annotations

from datetime import timedelta

# --- Auth (Firebase Identity Toolkit) ---
# Identifies the Firebase project ("opencharge-mobile-app-e376b") - not a secret credential,
# Firebase Web API keys are safe to ship client-side.
FIREBASE_API_KEY = "AIzaSyDmwzY9NKXxEmqvwiGrp4TLGy7dJ0G2XiM"
FIREBASE_SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
FIREBASE_REFRESH_URL = "https://securetoken.googleapis.com/v1/token"
# Refresh proactively this long before the token's reported expiry.
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)

# --- mobile-api.pod-point.com ---
MOBILE_API_BASE_URL = "https://mobile-api.pod-point.com"
