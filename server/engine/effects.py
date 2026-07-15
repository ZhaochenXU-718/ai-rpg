"""Deterministic mutations used by fact anchors.

Temporary action effects belonged to the pre-pivot resolver and were removed
during Phase 1.  Narrative-first anchors only apply permanent, auditable
changes after a fact batch has been committed.
"""

from __future__ import annotations

import copy
from typing import Any

from .state import apply_patch_value, get_value, set_value


def apply_mutations(
    state: dict[str, Any], block: dict[str, Any]
) -> list[tuple[str, Any, Any]]:
    """Apply one anchor mutation block and return change receipts."""
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
