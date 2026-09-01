"""Loads const.py/helpers.py without Home Assistant installed.

Both modules have zero Home Assistant dependency, but helpers.py's `from .const import ...` is a
*relative* import, which fails if helpers.py is imported as a bare top-level module (no parent
package - see test_translation_keys.py's simpler same-directory `sys.path` trick, which only
works because const.py itself has no relative imports).

Importing the real `custom_components.pod_home` package to get a proper parent isn't an option
either - its `__init__.py` unconditionally imports `homeassistant`, which isn't installed in this
environment (see CLAUDE.md's "Verification" section). Instead, a synthetic `pod_home` package is
registered in `sys.modules` pointing at the real source directory, then const.py/helpers.py are
loaded as its submodules via `importlib` - this lets helpers.py's relative import resolve
normally without ever executing the real `pod_home/__init__.py`.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_PKG_DIR = Path(__file__).parent.parent / "custom_components" / "pod_home"


def _load(module_name: str, filename: str) -> types.ModuleType:
    full_name = f"pod_home.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    if "pod_home" not in sys.modules:
        pkg = types.ModuleType("pod_home")
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["pod_home"] = pkg
    spec = importlib.util.spec_from_file_location(full_name, _PKG_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


const = _load("const", "const.py")
helpers = _load("helpers", "helpers.py")
