"""Storylet effect application and temporary-effect expiry.

Semantics follow docs/content-schema.md section 10.2: top-level mutations,
including explicit entity and item moves, are permanent; a `temporary` block
records original values and restores them after `duration_turns` turns.
"""

from __future__ import annotations

import copy
from typing import Any

from .state import apply_patch_value, get_value, set_value

EFFECT_MUTATION_KEYS = (
    "set_flags", "set_world", "state_patch", "move_entities", "move_items",
)


def effect_changes_scene(effect: dict[str, Any], player_id: str = "player") -> bool:
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
        move_entities = block.get("move_entities")
        if isinstance(move_entities, dict) and player_id in move_entities:
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
    for entity_id, node_id in (block.get("move_entities") or {}).items():
        path = f"positions.{entity_id}"
        previous = get_value(state, path)
        set_value(state, path, node_id)
        changes.append((path, previous, node_id))
    for item_id, placement in (block.get("move_items") or {}).items():
        path = f"item_locations.{item_id}"
        previous = copy.deepcopy(get_value(state, path))
        new_placement = copy.deepcopy(placement)
        set_value(state, path, new_placement)
        changes.append((path, previous, new_placement))
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

    def snapshot(self) -> tuple[dict[str, Any], ...]:
        """Return an isolated checkpoint payload for the pending rollbacks."""
        return tuple(copy.deepcopy(self._entries))

    def restore(self, snapshot: tuple[dict[str, Any], ...]) -> None:
        """Replace the registry from a checkpoint payload."""
        self._entries = copy.deepcopy(list(snapshot))

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
    for fact in effect.get("add_facts") or []:
        state["facts"].append(fact)
    temporary = effect.get("temporary")
    if isinstance(temporary, dict):
        touched = apply_mutations(state, temporary)
        temporaries.add(turn_no + int(temporary["duration_turns"]), touched)
        changes.extend(touched)
    return changes
