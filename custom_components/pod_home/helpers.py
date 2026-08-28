"""Small pure helper functions - deliberately free of any Home Assistant import so they can be
exercised in a plain script without installing homeassistant, the same way smoke_test_api.py
exercises the API layer.
"""
from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def known_or_none(value: T | None, known_values: list[T]) -> T | None:
    """Return value if it's a member of known_values, else None.

    Used for enum-shaped API fields where the confirmed set of values is incomplete (see
    const.py's CHARGING_STATE_* comments) - returning an unrecognized value straight through to
    a SensorDeviceClass.ENUM entity makes Home Assistant core itself log an error on every
    single state read (not once), since it checks native_value against _attr_options on every
    access. Falling back to None here avoids that; the raw value is still surfaced separately
    (see PodHomeStatusSensor.extra_state_attributes) so it isn't silently lost.
    """
    return value if value in known_values else None
