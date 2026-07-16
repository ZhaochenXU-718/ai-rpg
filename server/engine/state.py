"""Minimal authoritative state for physical continuity."""

from __future__ import annotations

import copy
from typing import Any


class StatePathError(Exception):
    """Raised when a dotted state path cannot be resolved."""


def build_initial_state(story: dict[str, Any]) -> dict[str, Any]:
    initial = copy.deepcopy(story.get("initial_state", {}))
    state = {
        "positions": initial.get("positions", {}),
        "item_locations": initial.get("item_locations", {}),
    }
    return state


def resolve_path(state: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    """Return (container, leaf_key) for a dotted state path, creating containers."""
    parts = path.split(".")
    root = parts[0]
    if root in ("positions", "item_locations"):
        container = state[root]
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
