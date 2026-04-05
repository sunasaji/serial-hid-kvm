"""Keyboard layout loader — reads overrides from YAML files.

Layout search order (for --layout jp106):
  1. SHKVM_LAYOUTS_DIR/jp106.yaml  (user-supplied custom layouts)
  2. <package>/layouts/jp106.yaml  (built-in layouts)
  3. Error if not found

YAML format:
  overrides:
    '"': [shift, 0x1F]
    '@': [none, 0x2F]
"""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path

import yaml

from .hid_keycodes import MODIFIER_MAP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_modifier(name: str) -> int:
    """Convert modifier string to bitmask.

    Supports single names ("shift") and combined ("shift+ralt").
    """
    if name == "none":
        return 0x00
    bits = 0
    for part in name.split("+"):
        part = part.strip().lower()
        if part not in MODIFIER_MAP:
            raise ValueError(f"Unknown modifier: {part!r}")
        bits |= MODIFIER_MAP[part]
    return bits


def _parse_keycode(val: int | str) -> int:
    """Convert a keycode value (int or hex string) to int."""
    if isinstance(val, int):
        return val
    # YAML may parse 0x1F as string if unquoted in some contexts
    if isinstance(val, str) and val.lower().startswith("0x"):
        return int(val, 16)
    return int(val)


def _load_yaml(filepath: str | Path) -> dict[str, tuple[int, int]]:
    """Parse a layout YAML file and return the overrides dict."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    overrides_raw: dict = data.get("overrides") or {}
    result: dict[str, tuple[int, int]] = {}

    for char, mapping in overrides_raw.items():
        char = str(char)
        modifier_name = str(mapping[0])
        keycode_raw = mapping[1]
        result[char] = (_parse_modifier(modifier_name), _parse_keycode(keycode_raw))

    return result


# ---------------------------------------------------------------------------
# Built-in layout directory (package resource)
# ---------------------------------------------------------------------------


def _builtin_layouts_dir() -> Path:
    """Return the path to the built-in layouts/ directory inside the package."""
    # importlib.resources.files() returns a Traversable (Python 3.9+)
    return Path(str(importlib.resources.files(__package__ or __name__) / "layouts"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _discover_builtin_layouts() -> list[str]:
    """List layout names from built-in YAML files."""
    d = _builtin_layouts_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


SUPPORTED_LAYOUTS = _discover_builtin_layouts()


def get_overrides(layout_name: str, layouts_dir: str | None = None) -> tuple[dict[str, tuple[int, int]], str]:
    """Load layout overrides from YAML file.

    Returns (overrides_dict, source_description).

    Search order:
      1. SHKVM_LAYOUTS_DIR/<layout_name>.yaml
      2. Built-in layouts/<layout_name>.yaml
      3. ValueError if not found
    """
    # 1. Check user-supplied directory first
    source = ""
    if layouts_dir:
        user_path = Path(layouts_dir) / f"{layout_name}.yaml"
        if user_path.is_file():
            logger.info(f"Loading layout {layout_name!r} from {user_path}")
            source = str(user_path)
            return _load_yaml(user_path), source

    # 2. Fall back to built-in
    builtin_path = _builtin_layouts_dir() / f"{layout_name}.yaml"
    if builtin_path.is_file():
        source = f"built-in ({builtin_path.name})"
        logger.info(f"Loading layout {layout_name!r} from built-in")
        return _load_yaml(builtin_path), source

    raise ValueError(
        f"Unknown layout: {layout_name!r}. "
        f"Supported built-in layouts: {', '.join(SUPPORTED_LAYOUTS)}"
    )
