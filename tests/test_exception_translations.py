"""Every translated exception: the translation key exists in strings.json, every key there is
used, and each raise site supplies exactly the placeholders its message uses. Reads the source
with ast, so a typo in a key or placeholder fails here instead of showing a raw key to a user."""
from __future__ import annotations

import ast
import json
import pathlib
import re

COMPONENT = pathlib.Path(__file__).parent.parent / "custom_components" / "pod_home"


def _translated_raises() -> list[tuple[str, str, set[str]]]:
    """(file, translation key, placeholder names) for every call that sets a translation domain."""
    found = []
    for path in sorted(COMPONENT.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            keywords = {k.arg: k.value for k in node.keywords if k.arg}
            key = keywords.get("translation_key")
            if "translation_domain" not in keywords or not isinstance(key, ast.Constant):
                continue
            placeholders = keywords.get("translation_placeholders")
            names = (
                {k.value for k in placeholders.keys if isinstance(k, ast.Constant)}
                if isinstance(placeholders, ast.Dict)
                else set()
            )
            found.append((path.name, key.value, names))
    return found


def _messages() -> dict[str, str]:
    exceptions = json.loads((COMPONENT / "strings.json").read_text())["exceptions"]
    return {key: value["message"] for key, value in exceptions.items()}


def test_every_translation_key_used_in_code_exists() -> None:
    messages = _messages()
    missing = {key for _, key, _ in _translated_raises()} - set(messages)
    assert not missing


def test_every_exception_message_is_used() -> None:
    unused = set(_messages()) - {key for _, key, _ in _translated_raises()}
    assert not unused


def test_placeholders_match_the_message() -> None:
    messages = _messages()
    for file, key, names in _translated_raises():
        assert set(re.findall(r"\{(\w+)\}", messages[key])) == names, f"{file}: {key}"


def test_there_are_translated_raises() -> None:
    assert len(_translated_raises()) >= 25
