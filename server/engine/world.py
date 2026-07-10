"""Generic spatial simulation over an abstract world graph.

The board is a graph, not necessarily a geometric grid.  Physical presence is
derived from the single authoritative ``state.positions`` mapping.  Each world
step evaluates every actor's movement rules against the same snapshot, selects
at most one proposal per actor, then applies all accepted moves atomically.
"""

from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .conditions import check_condition_block
from .content import Story
from .state import set_value


@dataclass
class WorldStepResult:
    step_no: int
    rules: list[str] = field(default_factory=list)
    moves: list[tuple[str, str, str]] = field(default_factory=list)
    narrative_hints: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def board_neighbors(story: Story, node_id: str) -> list[str]:
    """Return neighboring nodes in content declaration order."""
    neighbors: list[str] = []
    for edge in story.world_edges:
        if not isinstance(edge, dict):
            continue
        source, target = edge.get("from"), edge.get("to")
        if source == node_id and isinstance(target, str):
            neighbors.append(target)
        if edge.get("bidirectional") and target == node_id and isinstance(source, str):
            neighbors.append(source)
    return neighbors


def next_hop_toward(story: Story, source: str, target: str) -> str | None:
    """Find the first hop on a shortest path, or None when unreachable."""
    if source == target:
        return source
    queue: deque[tuple[str, str]] = deque()
    visited = {source}
    for neighbor in board_neighbors(story, source):
        queue.append((neighbor, neighbor))
        visited.add(neighbor)
    while queue:
        node, first_hop = queue.popleft()
        if node == target:
            return first_hop
        for neighbor in board_neighbors(story, node):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, first_hop))
    return None


def _proposed_destination(
    story: Story,
    snapshot: dict[str, Any],
    actor: str,
    rule: dict[str, Any],
) -> str | None:
    move = rule.get("move") or {}
    source = (snapshot.get("positions") or {}).get(actor)
    if not isinstance(source, str):
        return None
    if isinstance(move.get("to"), str):
        destination = move["to"]
        if destination == source:
            return source
        return destination if destination in board_neighbors(story, source) else None
    target_actor = move.get("toward")
    target = (snapshot.get("positions") or {}).get(target_actor)
    if isinstance(target, str):
        return next_hop_toward(story, source, target)
    return None


def advance_world(story: Story, state: dict[str, Any]) -> WorldStepResult:
    """Advance one deterministic world step and mutate positions atomically."""
    if not story.world_nodes:
        # Schema-v1 compatibility: no board means no new step semantics.
        return WorldStepResult(step_no=int((state.get("world") or {}).get("step", 0)))
    step_no = int((state.get("world") or {}).get("step", 0)) + 1
    state.setdefault("world", {})["step"] = step_no
    result = WorldStepResult(step_no=step_no)

    snapshot = copy.deepcopy(state)
    selected: dict[str, tuple[int, int, dict[str, Any], str]] = {}
    for index, rule in enumerate(story.world_rules):
        if not isinstance(rule, dict):
            continue
        actor = rule.get("actor")
        if not isinstance(actor, str):
            continue
        try:
            matches = check_condition_block(snapshot, rule.get("when") or {})
        except Exception as exc:
            result.errors.append(f"world rule '{rule.get('id', index)}' condition failed: {exc}")
            continue
        if not matches:
            continue
        destination = _proposed_destination(story, snapshot, actor, rule)
        if destination is None:
            result.errors.append(
                f"world rule '{rule.get('id', index)}' has no valid move from "
                f"'{(snapshot.get('positions') or {}).get(actor)}'"
            )
            continue
        candidate = (int(rule.get("priority", 0)), -index, rule, destination)
        current = selected.get(actor)
        if current is None or candidate[:2] > current[:2]:
            selected[actor] = candidate

    for actor, (_, _, rule, destination) in selected.items():
        source = (snapshot.get("positions") or {}).get(actor)
        if source == destination:
            continue
        set_value(state, f"positions.{actor}", destination)
        result.rules.append(str(rule.get("id", actor)))
        result.moves.append((actor, source, destination))
        hint = rule.get("narrative_hint")
        if isinstance(hint, str) and hint:
            result.narrative_hints.append(hint)
    return result
