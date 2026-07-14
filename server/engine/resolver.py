"""The turn resolver: one action in, adjudicated consequences out.

Implements docs/content-schema.md section 10.3:

1. apply the intent's typical_cost
2. apply the generic (fallback) patch, validated against resolution_limits
3. apply a validated built-in action (currently authored board exits), if any
4. single ordered action-storylet pass, cascading, max one player move
5. advance one snapshot-based, atomically applied world step
6. run after-world reaction storylets
7. expire temporary effects and evaluate endings by priority

The LLM never decides success or failure; in stage 3 it only supplies the
generic-patch *proposal* and renders the outcome.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .capability_effects import CapabilityResolution
from .conditions import check_condition_block, evaluate_endings
from .content import Story
from .effects import TemporaryEffects, apply_effect, effect_changes_scene
from .limits import clamp_generic_patch, validate_generic_patch
from .llm_protocol import CommittedDirectorBeat
from .state import apply_patch_value, get_value, set_value
from .world import advance_world

# Storylet types that read as a gain vs. a cost, used only to label the
# outcome tier for the player; the actual consequences are the effects.
GAIN_TYPES = {
    "reveal", "reward", "progress", "partial_progress", "opportunity",
    "opportunity_with_cost", "ending_route",
}
COST_TYPES = {"pressure", "failure_pressure", "consequence", "opportunity_with_cost", "partial_progress"}

RESULT_SUCCESS = "success"
RESULT_PARTIAL = "partial_success"
RESULT_FAIL_FORWARD = "fail_forward"
ATTRIBUTION_WORLD_BEAT = "world_beat"


@dataclass
class TurnResult:
    turn_no: int
    intent: str
    objects: list[str]
    scene_before: str
    scene_after: str
    fired: list[str] = field(default_factory=list)
    changes: list[tuple[str, Any, Any]] = field(default_factory=list)
    change_sources: list[str] = field(default_factory=list)
    action_effect_sources: list[str] = field(default_factory=list)
    matched_action_storylets: list[str] = field(default_factory=list)
    expired: list[tuple[str, Any]] = field(default_factory=list)
    narrative_hints: list[str] = field(default_factory=list)
    action_response_hints: list[str] = field(default_factory=list)
    world_beat_hints: list[str] = field(default_factory=list)
    world_reaction_hints: list[str] = field(default_factory=list)
    prior_events: list[str] = field(default_factory=list)
    new_facts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    world_rules: list[str] = field(default_factory=list)
    director_beats: list[CommittedDirectorBeat] = field(default_factory=list)
    director_trace: dict[str, Any] = field(default_factory=dict)
    result_tier: str = RESULT_FAIL_FORWARD
    primary_goal_status_override: str | None = None
    ending: str | None = None


def _record_changes(
    result: TurnResult,
    changes: list[tuple[str, Any, Any]],
    source: str,
) -> None:
    """Append state changes and keep their provenance aligned by index."""
    result.changes.extend(changes)
    result.change_sources.extend(source for _ in changes)


def _mark_action_effect(result: TurnResult, source: str) -> None:
    if source not in result.action_effect_sources:
        result.action_effect_sources.append(source)


def classify_result(story: Story, fired: list[str], generic_applied: bool) -> str:
    types = set()
    by_id = {s.get("id"): s for s in story.storylets}
    for storylet_id in fired:
        storylet = by_id.get(storylet_id) or {}
        if storylet.get("attribution") == ATTRIBUTION_WORLD_BEAT:
            continue
        types.add(storylet.get("type"))
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


def _trigger_is_for_intent(trigger: dict[str, Any], intent_id: str) -> bool:
    """Whether a storylet explicitly declares support for this action."""
    if "intent" in trigger:
        return trigger.get("intent") == intent_id
    if "intent_any" in trigger:
        return intent_id in (trigger.get("intent_any") or [])
    return False


def _fallback_action_response(
    story: Story,
    state: dict[str, Any],
    intent_id: str,
    objects: list[str],
    scene_id: str,
) -> str | None:
    """Ground an intent fallback in the actual targets of this action."""
    narrative = story.intent(intent_id).get("fallback_narrative")
    if not isinstance(narrative, str) or not narrative.strip():
        return None
    labels = [story.object_label(scene_id, obj, state) for obj in objects]
    if labels:
        return f"针对「{'、'.join(labels)}」，{narrative.strip()}"
    return narrative.strip()


def _has_matching_action_storylet(
    story: Story,
    state: dict[str, Any],
    consumed: set[str],
    intent_id: str,
    objects: list[str],
    generic_patch: dict[str, Any] | None,
) -> bool:
    """Preview the action phase and find an authored interaction.

    Automatic storylets before the interaction are applied to the private
    snapshot because they may unlock a later action storylet in the same
    ordered cascade.  No world step is advanced and the live session is never
    mutated.
    """
    preview = copy.deepcopy(state)
    for key, value in (story.intent(intent_id).get("typical_cost") or {}).items():
        path = str(key) if "." in str(key) else f"world.{key}"
        apply_patch_value(preview, path, value)
    for path, value in (generic_patch or {}).items():
        apply_patch_value(preview, str(path), value)

    preview_consumed = set(consumed)
    preview_temporaries = TemporaryEffects()
    player_moved = False
    for storylet in story.storylets:
        if storylet.get("phase", "action") != "action":
            continue
        storylet_id = storylet.get("id")
        if storylet.get("once") and storylet_id in preview_consumed:
            continue
        effect = storylet.get("effect") or {}
        moves_player = effect_changes_scene(effect, story.player_id)
        if player_moved and moves_player:
            continue
        trigger = storylet.get("trigger") or {}
        if not check_condition_block(preview, trigger, intent_id, objects):
            continue
        if _trigger_is_for_intent(trigger, intent_id):
            return True
        apply_effect(preview, effect, 0, preview_temporaries)
        if storylet.get("once"):
            preview_consumed.add(storylet_id)
        if moves_player:
            player_moved = True
    return False


def validate_action(
    story: Story,
    state: dict[str, Any],
    intent_id: str,
    objects: list[str] | None = None,
    generic_patch: dict[str, Any] | None = None,
    consumed: set[str] | None = None,
    allow_unauthored: bool = False,
) -> list[str]:
    """Validate an action atomically before it can spend time or a turn.

    ``allow_unauthored`` is the LLM-path policy switch: a well-formed attempt
    with no authored interaction is not an error there — it becomes a costed
    fail-forward turn instead of a free rejection (free rejections would let
    players probe the authored surface at zero cost). Structured no-LLM play
    keeps the strict stage-2 behaviour.
    """
    objects = [str(obj) for obj in objects or []]
    intent = story.intent(intent_id)
    if not intent:
        return [f"未知意图：{intent_id}"]

    unavailable = set(objects) - story.actionable_objects(state)
    if unavailable:
        scene_id = story.current_location(state)
        labels = [story.object_label(scene_id, obj) for obj in sorted(unavailable)]
        return [
            "目标当前不可见、未持有或不可交互：" + "、".join(labels)
        ]

    minimum = int(intent.get("min_objects", 0))
    maximum = intent.get("max_objects")
    if len(objects) < minimum or (isinstance(maximum, int) and len(objects) > maximum):
        label = intent.get("label", intent_id)
        if maximum is None:
            return [f"意图「{label}」至少需要 {minimum} 个目标"]
        return [f"意图「{label}」需要 {minimum} 到 {maximum} 个目标"]

    if intent.get("engine_action") == "move":
        selected_exit = story.exit_for_target(state, objects[0]) if len(objects) == 1 else None
        if selected_exit is None:
            return ["移动需要且只能指定一个当前可用出口"]

    if (
        intent.get("requires_storylet_match")
        and not allow_unauthored
        and not _has_matching_action_storylet(
            story, state, consumed or set(), intent_id, objects, generic_patch
        )
    ):
        scene_id = story.current_location(state)
        targets = "、".join(
            story.object_label(scene_id, obj, state) for obj in objects
        ) or "未指定目标"
        label = intent.get("label", intent_id)
        return [f"当前没有与「{label}」及这些目标匹配的可执行交互：{targets}"]
    return []


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
    allow_unauthored: bool = False,
    capability_resolution: CapabilityResolution | None = None,
) -> TurnResult:
    """Resolve one confirmed action. Mutates state / temporaries / consumed."""
    objects = [str(obj) for obj in objects or []]
    result = TurnResult(
        turn_no=turn_no,
        intent=intent_id,
        objects=objects,
        scene_before=story.current_location(state),
        scene_after=story.current_location(state),
    )

    if capability_resolution is None:
        result.errors.extend(
            validate_action(
                story, state, intent_id, objects, generic_patch, consumed,
                allow_unauthored=allow_unauthored,
            )
        )
    if result.errors:
        return result
    intent = story.intent(intent_id)
    engine_action = intent.get("engine_action")

    accepted: dict[str, Any] = {}
    patch_notes: list[str] = []
    if generic_patch:
        if strict_patch:
            errors = validate_generic_patch(
                generic_patch, state, story.resolution_limits, f"turn {turn_no}"
            )
            if errors:
                result.errors.extend(errors)
                return result
            accepted = dict(generic_patch)
        else:
            accepted, patch_notes = clamp_generic_patch(
                generic_patch, state, story.resolution_limits
            )
    result.notes.extend(patch_notes)

    selected_exit = None
    if engine_action == "move":
        selected_exit = story.exit_for_target(state, objects[0])

    # 1. intent cost (bare keys resolve to the world namespace)
    for key, value in (intent.get("typical_cost") or {}).items():
        path = str(key) if "." in str(key) else f"world.{key}"
        previous, new = apply_patch_value(state, path, value)
        _record_changes(result, [(path, previous, new)], "intent_cost")

    # 2. generic (fallback) resolution grant
    generic_applied = False
    for key, value in accepted.items():
        previous, new = apply_patch_value(state, key, value)
        _record_changes(result, [(key, previous, new)], "generic_patch")
        generic_applied = True
        _mark_action_effect(result, "generic_patch")

    if capability_resolution is not None:
        source = capability_resolution.capability_id
        for mutation in capability_resolution.mutations:
            if mutation.operation == "increment":
                previous, new = apply_patch_value(
                    state, mutation.path, mutation.value
                )
            else:
                previous = copy.deepcopy(get_value(state, mutation.path))
                new = copy.deepcopy(mutation.value)
                set_value(state, mutation.path, new)
            _record_changes(result, [(mutation.path, previous, new)], source)
        _mark_action_effect(result, source)
        result.primary_goal_status_override = (
            capability_resolution.primary_goal_status
        )
        result.action_response_hints.append(
            capability_resolution.response_hint
        )
        result.narrative_hints.append(capability_resolution.response_hint)

    player_moved = False
    if selected_exit is not None:
        source = story.current_location(state)
        destination = str(selected_exit["to"])
        set_value(state, f"positions.{story.player_id}", destination)
        _record_changes(
            result,
            [(f"positions.{story.player_id}", source, destination)],
            "engine.move",
        )
        _mark_action_effect(result, "engine.move")
        hint = str(
            selected_exit.get("narrative_hint")
            or f"你移动到了{story.scene(destination).get('name', destination)}。"
        )
        result.narrative_hints.append(hint)
        result.action_response_hints.append(hint)
        player_moved = True

    # 3. ordered action-storylet pass, cascading, max one player move per turn
    fact_count = len(state["facts"])
    player_moved = _run_storylet_phase(
        story, state, temporaries, consumed, turn_no, "action",
        intent_id, objects, result, player_moved,
    )

    # 4. one deterministic world step. All NPC rules read the same snapshot.
    world_step = advance_world(story, state)
    result.world_rules.extend(world_step.rules)
    result.errors.extend(world_step.errors)
    result.narrative_hints.extend(world_step.narrative_hints)
    result.world_reaction_hints.extend(world_step.narrative_hints)
    if len(world_step.rules) != len(world_step.moves):
        raise AssertionError("world step rule/move provenance is misaligned")
    for rule_id, (actor, source, destination) in zip(
        world_step.rules, world_step.moves
    ):
        _record_changes(
            result,
            [(f"positions.{actor}", source, destination)],
            f"world_rule.{rule_id}",
        )

    # Arrival/reaction storylets see the atomically applied world positions.
    _run_storylet_phase(
        story, state, temporaries, consumed, turn_no, "after_world",
        intent_id, objects, result, player_moved,
    )
    # Every valid action gets its own response channel.  When no authored
    # storylet responded, ground the content-declared intent fallback in the
    # selected targets so a simultaneous world beat cannot replace the action
    # in either LLM narration or template fallback.
    if not result.action_response_hints and capability_resolution is None:
        fallback = _fallback_action_response(
            story, state, intent_id, objects, result.scene_before
        )
        if fallback:
            result.action_response_hints.append(fallback)
            result.narrative_hints.append(fallback)
    result.new_facts = state["facts"][fact_count:]

    # 5. temporary effect expiry
    result.expired = temporaries.expire(state, turn_no)

    # 6. endings
    result.ending = evaluate_endings(state, story.endings)

    result.scene_after = story.current_location(state)
    result.result_tier = (
        capability_resolution.result_tier
        if capability_resolution is not None
        else classify_result(story, result.fired, generic_applied)
    )
    return result


def _run_storylet_phase(
    story: Story,
    state: dict[str, Any],
    temporaries: TemporaryEffects,
    consumed: set[str],
    turn_no: int,
    phase: str,
    intent_id: str | None,
    objects: list[str],
    result: TurnResult,
    player_moved: bool,
) -> bool:
    """Run one ordered storylet phase and return whether the player moved."""
    for storylet in story.storylets:
        if storylet.get("phase", "action") != phase:
            continue
        storylet_id = storylet.get("id")
        if storylet.get("once") and storylet_id in consumed:
            continue
        effect = storylet.get("effect") or {}
        moves_player = effect_changes_scene(effect, story.player_id)
        if player_moved and moves_player:
            continue
        if not check_condition_block(state, storylet.get("trigger") or {}, intent_id, objects):
            continue
        source = f"storylet.{storylet_id}"
        _record_changes(
            result,
            apply_effect(state, effect, turn_no, temporaries),
            source,
        )
        result.fired.append(storylet_id)
        trigger = storylet.get("trigger") or {}
        if (
            storylet.get("attribution") != ATTRIBUTION_WORLD_BEAT
            and intent_id is not None
            and _trigger_is_for_intent(trigger, intent_id)
        ):
            result.matched_action_storylets.append(storylet_id)
            _mark_action_effect(result, source)
        hint = storylet.get("narrative_hint")
        if hint:
            result.narrative_hints.append(hint)
            if storylet.get("attribution") == ATTRIBUTION_WORLD_BEAT:
                result.world_beat_hints.append(hint)
            else:
                result.action_response_hints.append(hint)
        if storylet.get("once"):
            consumed.add(storylet_id)
        if moves_player:
            player_moved = True
    return player_moved
