"""The single condition evaluator shared by triggers, exit_conditions and endings.

Syntax follows docs/content-schema.md section 9. There is deliberately only
one implementation of condition semantics in the codebase.
"""

from __future__ import annotations

from typing import Any

from .state import get_value

COMPARATORS = {
    "gte": lambda a, b: a is not None and a >= b,
    "lte": lambda a, b: a is not None and a <= b,
    "gt": lambda a, b: a is not None and a > b,
    "lt": lambda a, b: a is not None and a < b,
    "ne": lambda a, b: a != b,
    "eq": lambda a, b: a == b,
}


class ConditionError(Exception):
    """Raised on unsupported condition syntax."""


def split_comparator(key: str) -> tuple[str, str]:
    for suffix, name in (("_gte", "gte"), ("_lte", "lte"), ("_gt", "gt"), ("_lt", "lt"), ("_ne", "ne")):
        if key.endswith(suffix):
            return key[: -len(suffix)], name
    return key, "eq"


def check_path_condition(state: dict[str, Any], path_key: str, expected: Any, prefix: str = "") -> bool:
    path, comparator = split_comparator(path_key)
    if prefix:
        path = f"{prefix}.{path}"
    return COMPARATORS[comparator](get_value(state, path), expected)


def check_condition_block(
    state: dict[str, Any],
    block: dict[str, Any],
    intent: str | None = None,
    objects: list[str] | None = None,
) -> bool:
    """AND-evaluate one condition block (storylet trigger or exit block)."""
    objects = objects or []
    for group, value in block.items():
        if group == "scene":
            if state["world"].get("scene") != value:
                return False
        elif group == "scene_any":
            if state["world"].get("scene") not in value:
                return False
        elif group == "intent":
            if intent != value:
                return False
        elif group == "intent_any":
            if intent not in value:
                return False
        elif group == "object_any":
            if not set(value) & set(objects):
                return False
        elif group in ("world_state", "player_state", "flags"):
            prefix = {"world_state": "world", "player_state": "player", "flags": "flags"}[group]
            for key, expected in value.items():
                if not check_path_condition(state, str(key), expected, prefix):
                    return False
        elif group == "npc_state":
            for key, expected in value.items():
                if not check_path_condition(state, str(key), expected):
                    return False
        elif group in ("state_gte", "state_lte"):
            comparator = COMPARATORS[group.split("_")[1]]
            for key, expected in value.items():
                if not comparator(get_value(state, str(key)), expected):
                    return False
        else:
            raise ConditionError(f"unsupported condition group '{group}'")
    return True


def evaluate_endings(state: dict[str, Any], endings: dict[str, Any]) -> str | None:
    """Return the reached ending id (lowest priority number wins), or None."""
    candidates = []
    for ending_id, ending in (endings or {}).items():
        conditions = ending.get("conditions") or {}
        if all(check_path_condition(state, str(key), expected) for key, expected in conditions.items()):
            candidates.append((ending.get("priority", 999), ending_id))
    if not candidates:
        return None
    return min(candidates)[1]


def check_exit_conditions(
    state: dict[str, Any],
    exit_conditions: dict[str, Any] | None,
    endings: dict[str, Any],
) -> bool:
    """Evaluate a scene's structured exit_conditions (schema 9.4/9.5)."""
    if not exit_conditions:
        return False
    for block in exit_conditions.get("any") or []:
        if "ending_reached" in block:
            remainder = {k: v for k, v in block.items() if k != "ending_reached"}
            if evaluate_endings(state, endings) is not None and check_condition_block(state, remainder):
                return True
            continue
        if check_condition_block(state, block):
            return True
    return False
