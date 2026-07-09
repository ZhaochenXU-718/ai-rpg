"""Storylet effect application and temporary-effect expiry.

Semantics follow docs/content-schema.md section 10.2: top-level mutations are
permanent; a `temporary` block records original values and restores them
after `duration_turns` turns.
"""

from __future__ import annotations

from typing import Any

from .state import apply_patch_value, get_value, set_value

EFFECT_MUTATION_KEYS = ("set_flags", "set_world", "state_patch")


def effect_changes_scene(effect: dict[str, Any]) -> bool:
    blocks = [effect]
    if isinstance(effect.get("temporary"), dict):
        blocks.append(effect["temporary"])
    for block in blocks:
        set_world = block.get("set_world")
        if isinstance(set_world, dict) and "scene" in set_world:
            return True
        state_patch = block.get("state_patch")
        if isinstance(state_patch, dict) and "world.scene" in state_patch:
            return True
    return False


def apply_mutations(state: dict[str, Any], block: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """Apply one mutation block; return (path, previous, new) records."""
    changes: list[tuple[str, Any, Any]] = []
    for flag, value in (block.get("set_flags") or {}).items():
        path = f"flags.{flag}"
        previous = get_value(state, path)
        set_value(state, path, value)
        changes.append((path, previous, value))
    for key, value in (block.get("set_world") or {}).items():
        path = f"world.{key}"
        previous = get_value(state, path)
        set_value(state, path, value)
        changes.append((path, previous, value))
    for key, value in (block.get("state_patch") or {}).items():
        path = str(key)
        previous, new = apply_patch_value(state, path, value)
        changes.append((path, previous, new))
    return changes


class TemporaryEffects:
    """Registry of pending rollbacks for `temporary` effect blocks."""

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []

    def add(self, expires_after_turn: int, restore: list[tuple[str, Any, Any]]) -> None:
        self._entries.append({
            "expires_after_turn": expires_after_turn,
            "restore": [(path, previous) for path, previous, _ in restore],
        })

    def expire(self, state: dict[str, Any], turn_no: int) -> list[tuple[str, Any]]:
        """Roll back entries that expire at the end of `turn_no`."""
        restored: list[tuple[str, Any]] = []
        for entry in [e for e in self._entries if e["expires_after_turn"] <= turn_no]:
            for path, previous in entry["restore"]:
                set_value(state, path, previous)
                restored.append((path, previous))
            self._entries.remove(entry)
        return restored


def apply_effect(
    state: dict[str, Any],
    effect: dict[str, Any],
    turn_no: int,
    temporaries: TemporaryEffects,
) -> list[tuple[str, Any, Any]]:
    """Apply a full storylet effect; return permanent+temporary change records."""
    changes = apply_mutations(state, effect)
    for clue in effect.get("add_clues") or []:
        state["clues"].append(clue)
    temporary = effect.get("temporary")
    if isinstance(temporary, dict):
        touched = apply_mutations(state, temporary)
        temporaries.add(turn_no + int(temporary["duration_turns"]), touched)
        changes.extend(touched)
    return changes
