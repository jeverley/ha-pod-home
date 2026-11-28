"""Entity names come from translation keys, not hard-coded names, and strings.json stays
identical to translations/en.json. Reads the platform source with ast: Home Assistant wraps the
_attr_* class attributes in properties, so they can't be read back off the classes."""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

COMPONENT = pathlib.Path(__file__).parent.parent / "custom_components" / "pod_home"
PLATFORMS = ["binary_sensor", "button", "calendar", "lock", "number", "select", "sensor", "time"]


def _class_attributes(source: str) -> dict[str, dict[str, str]]:
    """Maps each class to its string-constant class attributes."""
    classes: dict[str, dict[str, str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        attrs: dict[str, str] = {}
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                attrs[stmt.targets[0].id] = stmt.value.value
        classes[node.name] = attrs
    return classes


def _entity_classes() -> list[tuple[str, str, dict[str, str]]]:
    found = []
    for platform in PLATFORMS:
        source = (COMPONENT / f"{platform}.py").read_text()
        for name, attrs in _class_attributes(source).items():
            if "_attr_translation_key" in attrs or "_attr_name" in attrs:
                found.append((platform, name, attrs))
    return found


def test_strings_and_english_translations_are_identical() -> None:
    assert (COMPONENT / "strings.json").read_bytes() == (
        COMPONENT / "translations" / "en.json"
    ).read_bytes()


@pytest.mark.parametrize(("platform", "name", "attrs"), _entity_classes())
def test_entity_name_comes_from_translations(
    platform: str, name: str, attrs: dict[str, str]
) -> None:
    assert "_attr_name" not in attrs, f"{name} hard-codes its name"
    translations = json.loads((COMPONENT / "strings.json").read_text())["entity"][platform]
    assert translations[attrs["_attr_translation_key"]]["name"]


def test_every_platform_has_entity_classes() -> None:
    assert {platform for platform, _, _ in _entity_classes()} == set(PLATFORMS)


def test_hard_coded_name_is_detected() -> None:
    source = 'class Sensor:\n    _attr_translation_key = "status"\n    _attr_name = "Status"\n'
    assert "_attr_name" in _class_attributes(source)["Sensor"]
