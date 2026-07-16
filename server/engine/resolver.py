"""Mechanical commit seam for admitted physical facts.

``FactBatch`` is the canonical, already-admitted transport. Untrusted prose
extractions are checked by the physical-continuity layer before this
deterministic module applies them.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .content import Story
from .llm_protocol import CommittedTurn, FactBatch
from .state import get_value, set_value


@dataclass
class TurnResult:
    turn_no: int
    player_text: str
    narrative: str
    references: tuple[str, ...]
    scene_before: str
    scene_after: str
    changes: list[tuple[str, Any, Any]] = field(default_factory=list)
    committed_turn: CommittedTurn | None = None


def _record_changes(
    result: TurnResult,
    changes: list[tuple[str, Any, Any]],
) -> None:
    for change in changes:
        if change[1] == change[2]:
            continue
        result.changes.append(change)


def commit_facts(
    story: Story,
    state: dict[str, Any],
    turn_no: int,
    batch: FactBatch,
) -> TurnResult:
    """Atomically apply an already validated physical fact batch.

    Iron-law conflict checks deliberately live before this seam. Keeping this
    function mechanical makes work-copy atomicity straightforward to audit.
    """
    scene_before = story.current_location(state)
    result = TurnResult(
        turn_no=turn_no,
        player_text=batch.player_text,
        narrative=batch.narrative,
        references=tuple(batch.references),
        scene_before=scene_before,
        scene_after=scene_before,
    )

    for path, value in batch.state_changes.items():
        previous = copy.deepcopy(get_value(state, str(path)))
        new = copy.deepcopy(value)
        set_value(state, str(path), new)
        _record_changes(result, [(str(path), previous, new)])
    result.scene_after = story.current_location(state)
    return result
