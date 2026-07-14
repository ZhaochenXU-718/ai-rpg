"""Runtime state model: namespaces, dotted paths, patch semantics.

Path and patch semantics follow docs/content-schema.md sections 8 and 10.2:
numbers are increments, everything else assigns.
"""

from __future__ import annotations

import copy
from typing import Any


class StatePathError(Exception):
    """Raised when a dotted state path cannot be resolved."""


def build_initial_state(story: dict[str, Any]) -> dict[str, Any]:
    initial = copy.deepcopy(story.get("initial_state", {}))
    state = {
        "_meta": {"player_id": (story.get("player_role") or {}).get("id", "player")},
        "world": initial.get("world", {}),
        "player": initial.get("player", {}),
        "scene": initial.get("scene", {}),
        "flags": initial.get("flags", {}),
        "positions": initial.get("positions", {}),
        "item_locations": initial.get("item_locations", {}),
        "characters": {},
        "facts": [],
        # Validated generative facts (Local Canon). Authors never seed this
        # namespace; it is populated only through the engine's admission
        # checks and participates in checkpoints like any other state.
        "generated": {"locations": {}, "situations": {}},
    }
    for char_id, char in (story.get("characters") or {}).items():
        state["characters"][char_id] = copy.deepcopy(char.get("initial_state", {}))
    return state


def resolve_path(state: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    """Return (container, leaf_key) for a dotted state path, creating containers."""
    parts = path.split(".")
    root = parts[0]
    if root in ("world", "player", "scene", "flags", "positions", "item_locations", "generated"):
        container = state.setdefault(root, {}) if root == "generated" else state[root]
        parts = parts[1:]
    elif root == "characters":
        container = state["characters"]
        parts = parts[1:]
    elif root in state["characters"]:
        container = state["characters"][root]
        parts = parts[1:]
    else:
        raise StatePathError(f"unknown state path root '{root}' in '{path}'")
    if not parts:
        raise StatePathError(f"state path '{path}' has no leaf key")
    for part in parts[:-1]:
        container = container.setdefault(part, {})
    return container, parts[-1]


def get_value(state: dict[str, Any], path: str) -> Any:
    container, leaf = resolve_path(state, path)
    return container.get(leaf)


def set_value(state: dict[str, Any], path: str, value: Any) -> None:
    container, leaf = resolve_path(state, path)
    container[leaf] = value


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def apply_patch_value(state: dict[str, Any], path: str, value: Any) -> tuple[Any, Any]:
    """Apply one patch entry; return (previous, new) values."""
    previous = get_value(state, path)
    if is_number(value):
        base = previous if is_number(previous) else 0
        new = base + value
    else:
        new = value
    set_value(state, path, new)
    return previous, new


def apply_patch(state: dict[str, Any], patch: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Apply a patch mapping; return (path, previous, new) change records."""
    changes = []
    for key, value in (patch or {}).items():
        previous, new = apply_patch_value(state, str(key), value)
        changes.append((str(key), previous, new))
    return changes
