"""Cross-checks strings.json/translations/en.json's per-entity "state" translation blocks
against the actual enum options they're meant to translate (const.py's CHARGER_STATUS_OPTIONS/
SCHEDULE_MODE_OPTIONS/CHARGE_PRIORITY_OPTIONS). No Home Assistant dependency - just stdlib json
plus importing const.py directly.

A CHARGER_STATUS_*/SCHEDULE_MODE_*/CHARGE_PRIORITY_* constant renamed or added without updating
strings.json/translations/en.json currently only surfaces at runtime as a "missing translation"
raw key shown in the HA UI - this catches that offline instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "custom_components" / "pod_home"))

import const  # noqa: E402 - path insert must happen first

# (translations-file path segments, const.py OPTIONS list) - add a row here whenever a new
# translated enum entity is added, matching CLAUDE.md's "Entity states and translations" rule.
_CHECKS = [
    (("entity", "sensor", "status", "state"), const.CHARGER_STATUS_OPTIONS),
    (("entity", "sensor", "charging_scheme", "state"), const.SCHEDULE_MODE_OPTIONS),
    (("entity", "select", "charge_mode", "state"), const.CHARGE_PRIORITY_OPTIONS),
]


def _dig(data: dict, path: tuple[str, ...]) -> dict:
    for key in path:
        data = data[key]
    return data


@pytest.mark.parametrize("translations_file", ["strings.json", "translations/en.json"])
@pytest.mark.parametrize("json_path,options", _CHECKS)
def test_translation_state_keys_match_const(
    translations_file: str, json_path: tuple[str, ...], options: list[str]
) -> None:
    path = ROOT / "custom_components" / "pod_home" / translations_file
    data = json.loads(path.read_text(encoding="utf-8"))
    keys = set(_dig(data, json_path))
    expected = set(options)
    assert keys == expected, (
        f"{translations_file}:{'.'.join(json_path)} out of sync with const.py "
        f"(missing from JSON: {sorted(expected - keys)}, "
        f"extra in JSON: {sorted(keys - expected)})"
    )
