"""Narrative-first fact commit and anchor scanning.

``FactBatch`` is the canonical, already-admitted transport. Untrusted prose
extractions are checked in ``iron_laws.py`` before this deterministic module
applies them, scans Canon anchors and evaluates endings.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .conditions import check_condition_block, evaluate_endings
from .content import Story
from .effects import apply_mutations
from .llm_protocol import (
    CommittedDirectorBeat,
    CommittedLocalCanon,
    CommittedTurn,
    FactBatch,
)
from .local_canon import expire_situations
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
    change_sources: list[str] = field(default_factory=list)
    new_facts: list[str] = field(default_factory=list)
    fired: list[str] = field(default_factory=list)
    narrative_hints: list[str] = field(default_factory=list)
    director_beats: list[CommittedDirectorBeat] = field(default_factory=list)
    local_canon: list[CommittedLocalCanon] = field(default_factory=list)
    director_trace: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ending: str | None = None
    prior_events: list[str] = field(default_factory=list)
    committed_turn: CommittedTurn | None = None


def _record_changes(
    result: TurnResult,
    changes: list[tuple[str, Any, Any]],
    source: str,
) -> None:
    for change in changes:
        if change[1] == change[2]:
            continue
        result.changes.append(change)
        result.change_sources.append(source)


def _append_fact(state: dict[str, Any], result: TurnResult, fact: Any) -> None:
    text = str(fact).strip()
    if not text or text in state["facts"]:
        return
    state["facts"].append(text)
    result.new_facts.append(text)


def _run_fact_anchors(
    story: Story,
    state: dict[str, Any],
    consumed: set[str],
    result: TurnResult,
) -> None:
    """Run each matching post-commit anchor at most once in this turn."""
    fired_this_turn: set[str] = set()
    while True:
        progressed = False
        for anchor in story.storylets:
            anchor_id = str(anchor.get("id") or "")
            if not anchor_id or anchor_id in fired_this_turn:
                continue
            if anchor.get("phase", "after_world") != "after_world":
                continue
            if anchor.get("once") and anchor_id in consumed:
                continue
            trigger = anchor.get("trigger") or {}
            if not isinstance(trigger, dict) or not check_condition_block(state, trigger):
                continue

            effect = anchor.get("effect") or {}
            if isinstance(effect, dict):
                _record_changes(
                    result,
                    apply_mutations(state, effect),
                    f"anchor.{anchor_id}",
                )
                for fact in effect.get("add_facts") or []:
                    _append_fact(state, result, fact)
            if anchor.get("once"):
                consumed.add(anchor_id)
            fired_this_turn.add(anchor_id)
            result.fired.append(anchor_id)
            hint = str(anchor.get("narrative_hint") or "").strip()
            if hint:
                result.narrative_hints.append(hint)
            progressed = True
        if not progressed:
            return


def commit_facts(
    story: Story,
    state: dict[str, Any],
    consumed: set[str],
    turn_no: int,
    batch: FactBatch,
) -> TurnResult:
    """Atomically apply a fact batch, then scan anchors and endings.

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
        _record_changes(result, [(str(path), previous, new)], "fact_batch")
    for fact in batch.facts:
        _append_fact(state, result, fact)

    _run_fact_anchors(story, state, consumed, result)

    for situation_id, record in expire_situations(state, turn_no):
        path = f"generated.situations.{situation_id}.active"
        _record_changes(result, [(path, True, False)], "local_canon.expiry")
        archetype = story.situation_archetypes.get(str(record.get("archetype"))) or {}
        hint = str(
            archetype.get("expiry_narrative")
            or f"「{record.get('name', situation_id)}」结束了。"
        )
        result.narrative_hints.append(hint)

    result.ending = evaluate_endings(state, story.endings)
    result.scene_after = story.current_location(state)
    return result
