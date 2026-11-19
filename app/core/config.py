"""Configuration: config.yaml <- runtime overrides.

Every tunable lives here so the hidden "runtime changes" checks can move a value
without restarting the service.
"""

from __future__ import annotations

import copy
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.getenv("APP_CONFIG", str(ROOT / "config.yaml")))

_lock = threading.RLock()
_values: dict[str, Any] = {}
_overrides: dict[str, Any] = {}
_loaded_at: str = ""


def _flatten(node: dict, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(_flatten(value, path + "."))
        else:
            flat[path] = value
    return flat


def load() -> None:
    global _values, _loaded_at
    raw: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    with _lock:
        _values = _flatten(raw)
        _loaded_at = datetime.now(timezone.utc).isoformat()


def get(key: str, default: Any = None) -> Any:
    with _lock:
        if key in _overrides:
            return _overrides[key]
        if key in _values:
            return _values[key]
    # `_flatten` turns nested mappings into dotted keys, so a section like
    # "policy.access_levels" has to be rebuilt on read.
    section = get_section(key)
    return section if section else default


def get_section(prefix: str) -> dict:
    """Reconstruct a nested mapping from its dotted keys."""
    prefix = prefix.rstrip(".") + "."
    with _lock:
        scoped = {k: v for k, v in _values.items() if k.startswith(prefix)}
        scoped.update({k: v for k, v in _overrides.items() if k.startswith(prefix)})

    section: dict[str, Any] = {}
    for key, value in scoped.items():
        node = section
        parts = key[len(prefix) :].split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return section


def get_list(key: str, default: list | None = None) -> list:
    value = get(key, default if default is not None else [])
    return value if isinstance(value, list) else [value]


def set_override(key: str, value: Any) -> Any:
    with _lock:
        _overrides[key] = value
        return _overrides[key]


def clear_overrides() -> None:
    with _lock:
        _overrides.clear()


def snapshot() -> dict:
    with _lock:
        return {
            "loaded_at": _loaded_at,
            "overrides": copy.deepcopy(_overrides),
            "values": copy.deepcopy(_values),
        }


load()
