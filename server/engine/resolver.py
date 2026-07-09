"""The turn resolver: one action in, adjudicated consequences out.

Implements docs/content-schema.md section 10.3 exactly:

1. apply the intent's typical_cost
2. apply the generic (fallback) patch, validated against resolution_limits
3. single ordered pass over storylets, cascading, max one scene change per turn
4. expire temporary effects
5. evaluate endings by priority

The LLM never decides success or failure; in stage 3 it only supplies the
generic-patch *proposal* and renders the outcome.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .conditions import check_condition_block, evaluate_endings
from .content import Story
from .effects import TemporaryEffects, apply_effect, effect_changes_scene
from .limits import clamp_generic_patch, validate_generic_patch
from .state import apply_patch_value

# Storylet types that read as a gain vs. a cost, used only to label the
# outcome tier for the player; the actual consequences are the effects.
GAIN_TYPES = {"reveal", "reward", "progress", "opportunity", "ending_route"}
COST_TYPES = {"pressure", "failure_pressure", "consequence", "opportunity_with_cost", "partial_progress"}

RESULT_SUCCESS = "success"
RESULT_PARTIAL = "partial_success"
RESULT_FAIL_FORWARD = "fail_forward"


@dataclass
class TurnResult:
    turn_no: int
    intent: str
    objects: list[str]
    scene_before: str
    scene_after: str
    fired: list[str] = field(default_factory=list)
    changes: list[tuple[str, Any, Any]] = field(default_factory=list)
    expired: list[tuple[str, Any]] = field(default_factory=list)
    narrative_hints: list[str] = field(default_factory=list)
    new_clues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    result_tier: str = RESULT_FAIL_FORWARD
    ending: str | None = None


def classify_result(story: Story, fired: list[str], generic_applied: bool) -> str:
    types = set()
    by_id = {s.get("id"): s for s in story.storylets}
    for storylet_id in fired:
        types.add((by_id.get(storylet_id) or {}).get("type"))
    has_gain = bool(types & GAIN_TYPES)
    has_cost = bool(types & COST_TYPES)
    if has_gain and not has_cost:
        return RESULT_SUCCESS
    if has_gain and has_cost:
        return RESULT_PARTIAL
    if has_cost:
        return RESULT_FAIL_FORWARD
    if generic_applied:
        return RESULT_PARTIAL
    return RESULT_FAIL_FORWARD


def run_turn(
    story: Story,
    state: dict[str, Any],
    temporaries: TemporaryEffects,
    consumed: set[str],
    turn_no: int,
    intent_id: str,
    objects: list[str] | None = None,
    generic_patch: dict[str, Any] | None = None,
    strict_patch: bool = False,
) -> TurnResult:
    """Resolve one confirmed action. Mutates state / temporaries / consumed."""
    objects = [str(obj) for obj in objects or []]
    result = TurnResult(
        turn_no=turn_no,
        intent=intent_id,
        objects=objects,
        scene_before=state["world"].get("scene"),
        scene_after=state["world"].get("scene"),
    )

    intent = story.intent(intent_id)
    if not intent:
        result.errors.append(f"unknown intent '{intent_id}'")
        return result

    # 1. intent cost (bare keys resolve to the world namespace)
    for key, value in (intent.get("typical_cost") or {}).items():
        path = str(key) if "." in str(key) else f"world.{key}"
        previous, new = apply_patch_value(state, path, value)
        result.changes.append((path, previous, new))

    # 2. generic (fallback) resolution grant
    generic_applied = False
    if generic_patch:
        if strict_patch:
            errors = validate_generic_patch(generic_patch, state, story.resolution_limits, f"turn {turn_no}")
            result.errors.extend(errors)
            accepted = {} if errors else dict(generic_patch)
        else:
            accepted, notes = clamp_generic_patch(generic_patch, state, story.resolution_limits)
            result.notes.extend(notes)
        for key, value in accepted.items():
            previous, new = apply_patch_value(state, key, value)
            result.changes.append((key, previous, new))
            generic_applied = True

    # 3. single ordered storylet pass, cascading, one scene change per turn
    clue_count = len(state["clues"])
    scene_changed = False
    for storylet in story.storylets:
        storylet_id = storylet.get("id")
        if storylet.get("once") and storylet_id in consumed:
            continue
        effect = storylet.get("effect") or {}
        if scene_changed and effect_changes_scene(effect):
            continue
        if not check_condition_block(state, storylet.get("trigger") or {}, intent_id, objects):
            continue
        result.changes.extend(apply_effect(state, effect, turn_no, temporaries))
        result.fired.append(storylet_id)
        hint = storylet.get("narrative_hint")
        if hint:
            result.narrative_hints.append(hint)
        if storylet.get("once"):
            consumed.add(storylet_id)
        if effect_changes_scene(effect):
            scene_changed = True
    result.new_clues = state["clues"][clue_count:]

    # 4. temporary effect expiry
    result.expired = temporaries.expire(state, turn_no)

    # 5. endings
    result.ending = evaluate_endings(state, story.endings)

    result.scene_after = state["world"].get("scene")
    result.result_tier = classify_result(story, result.fired, generic_applied)
    return result
