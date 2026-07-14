"""Director-layer signals: scene goals and goal-achievement checks.

Stage 2 keeps the director thin: it reads exit_conditions as "the scene goal
is met, push toward a transition" and surfaces the current goal for the UI.
Event scheduling itself lives in the storylet passes; the player's current
scene is derived from their authoritative board position in schema v2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .conditions import check_condition_block, check_exit_conditions
from .content import Story
from .effects import effect_changes_scene
from .llm import DirectorRequest, LLMProvider
from .llm_protocol import (
    AuthorityLevel,
    CommittedChange,
    CommittedDirectorBeat,
    CommittedLocalCanon,
    DirectorBeat,
    DirectorBeatKind,
    DirectorBeatValidation,
    IssueSeverity,
    ValidationIssue,
    new_protocol_id,
)
from .local_canon import (
    MAX_LOCAL_CANON_PER_TURN,
    commit_local_canon,
    generation_context,
    validate_local_canon,
)
from .state import set_value
from .world import board_neighbors


def current_scene_id(state: dict[str, Any]) -> str:
    player_id = (state.get("_meta") or {}).get("player_id", "player")
    return (state.get("positions") or {}).get(player_id, state["world"].get("scene", ""))


def current_goal(story: Story, state: dict[str, Any]) -> str:
    scene = story.scene(current_scene_id(state))
    return scene.get("goal") or state["world"].get("current_goal", "")


def goal_achieved(story: Story, state: dict[str, Any]) -> bool:
    scene = story.scene(current_scene_id(state))
    return check_exit_conditions(state, scene.get("exit_conditions"), story.endings)


def suggested_intents(story: Story, state: dict[str, Any]) -> list[str]:
    scene = story.scene(current_scene_id(state))
    return list(scene.get("suggested_intents") or story.intents.keys())


def transition_options(
    story: Story,
    state: dict[str, Any],
    consumed: set[str],
) -> list[dict[str, Any]]:
    """Player-actionable transitions whose state preconditions already hold.

    Surfaces scene-changing storylets and explicitly opted-in interactions as
    concrete director hints ("可尝试：后楼梯上二楼——潜入"), so an earned
    opportunity is never invisible.
    Only intent/object requirements are left for the player to supply;
    auto-firing transitions (no intent requirement) are excluded.
    """
    options: list[dict[str, Any]] = []
    actionable = story.actionable_objects(state)
    for storylet in story.storylets:
        if storylet.get("phase", "action") != "action":
            continue
        if storylet.get("once") and storylet.get("id") in consumed:
            continue
        effect = storylet.get("effect") or {}
        director_hint = storylet.get("director_hint", False)
        if not effect_changes_scene(effect, story.player_id) and not director_hint:
            continue
        trigger = storylet.get("trigger") or {}
        intents = [trigger["intent"]] if "intent" in trigger else list(trigger.get("intent_any") or [])
        if not intents:
            continue  # fires on state alone; nothing for the player to do
        state_conditions = {
            key: value for key, value in trigger.items()
            if key not in ("intent", "intent_any", "object_any", "object_all")
        }
        if not check_condition_block(state, state_conditions):
            continue
        required_objects = list(trigger.get("object_all") or [])
        alternative_objects = list(trigger.get("object_any") or [])
        if not set(required_objects).issubset(actionable):
            continue
        available_alternatives = [obj for obj in alternative_objects if obj in actionable]
        if alternative_objects and not available_alternatives:
            continue
        options.append({
            "title": director_hint if isinstance(director_hint, str) else storylet.get(
                "title", storylet.get("id")
            ),
            "intents": intents,
            "objects": list(dict.fromkeys(
                required_objects + available_alternatives
            )),
        })
    return options


def _beat_issue(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=IssueSeverity.ERROR,
        message=message,
        retryable=True,
    )


def _locations_are_adjacent(story: Story, source: str, target: str) -> bool:
    for edge in story.world_edges:
        if not isinstance(edge, dict):
            continue
        origin = str(edge.get("from") or "")
        destination = str(edge.get("to") or "")
        if origin == source and destination == target:
            return True
        if edge.get("bidirectional") and origin == target and destination == source:
            return True
    return False


def validate_director_beat(
    story: Story,
    state: dict[str, Any],
    beat: DirectorBeat,
    *,
    state_revision: int,
    moved_actor_ids: set[str] | None = None,
) -> DirectorBeatValidation:
    """Validate scheduling only; committing a beat remains a later module."""
    issues: list[ValidationIssue] = []
    if beat.state_revision != state_revision:
        issues.append(_beat_issue(
            "director.stale_beat",
            f"导演节拍基于修订 {beat.state_revision}，当前是 {state_revision}。",
        ))
    if beat.actor_id == story.player_id or beat.actor_id not in story.characters:
        issues.append(_beat_issue(
            "director.actor_not_authored",
            "Director Beat 只能引用作者预定义的非玩家角色。",
        ))

    known_locations = set(story.world_nodes) | set(story.scenes)
    if beat.target_location_id not in known_locations:
        issues.append(_beat_issue(
            "director.location_unknown",
            "Director Beat 的目标地点不在作者定义的世界图中。",
        ))

    player_location = story.current_location(state)
    known_targets = (
        set(story.characters)
        | set(story.items)
        | set(story.world_nodes)
        | set(story.scenes)
        | set(story.visible_scene_objects(player_location, state))
    )
    if set(beat.target_ids) - known_targets:
        issues.append(_beat_issue(
            "director.target_unknown",
            "Director Beat 引用了状态中不存在的目标实体。",
        ))

    positions = state.get("positions") or {}
    source = positions.get(beat.actor_id)
    if beat.target_location_id != player_location:
        issues.append(_beat_issue(
            "director.target_not_current_scene",
            "第一版 Director Beat 只能调度当前玩家场景中的节拍。",
        ))
    if source is None and beat.actor_id in story.characters:
        issues.append(_beat_issue(
            "director.actor_unplaced",
            "角色没有权威位置，不能由 Director 临时补写位置。",
        ))
    elif beat.kind == DirectorBeatKind.ENTER_SCENE:
        if beat.actor_id in (moved_actor_ids or set()):
            issues.append(_beat_issue(
                "director.actor_already_moved",
                "角色已经在本次提交中移动，不能再由 Director 追加一次移动。",
            ))
        elif source == beat.target_location_id:
            issues.append(_beat_issue(
                "director.actor_already_present",
                "角色已经在目标场景，不能再次作为进场节拍。",
            ))
        elif source is not None and not _locations_are_adjacent(
            story, str(source), beat.target_location_id
        ):
            issues.append(_beat_issue(
                "director.route_unreachable",
                "角色不能在一个世界步内到达目标场景。",
            ))
    elif beat.kind in (
        DirectorBeatKind.REACT,
        DirectorBeatKind.ADVANCE_PLAN,
    ) and source != beat.target_location_id:
        issues.append(_beat_issue(
            "director.actor_not_present",
            "角色不在当前场景，不能直接反应或推进计划。",
        ))

    return DirectorBeatValidation(
        validation_id=new_protocol_id("beatval"),
        beat_id=beat.beat_id,
        state_revision=state_revision,
        can_schedule=not issues,
        issues=tuple(issues),
    )


@dataclass
class DirectorCycleResult:
    """Non-authoritative provider response plus accepted engine commits."""

    accepted: list[CommittedDirectorBeat] = field(default_factory=list)
    accepted_local_canon: list[CommittedLocalCanon] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    model: str = "none"
    prompt_version: str = "n/a"
    raw: str = ""
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def trace_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt_version": self.prompt_version,
            "raw": self.raw,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
            "accepted": [beat.to_dict() for beat in self.accepted],
            "accepted_local_canon": [
                record.to_dict() for record in self.accepted_local_canon
            ],
            "rejected": self.rejected,
            "error": self.error,
        }


def _moved_actor_ids(result: Any, player_id: str) -> set[str]:
    moved: set[str] = set()
    for path, _, _ in result.changes:
        if not str(path).startswith("positions."):
            continue
        actor_id = str(path).split(".", 1)[1]
        if actor_id != player_id:
            moved.add(actor_id)
    return moved


def _director_candidates(
    story: Story,
    state: dict[str, Any],
    result: Any,
) -> tuple[dict[str, Any], ...]:
    location_id = story.current_location(state)
    moved = _moved_actor_ids(result, story.player_id)
    positions = state.get("positions") or {}
    candidates: list[dict[str, Any]] = []
    for actor_id, character in story.characters.items():
        if actor_id == story.player_id or not isinstance(character, dict):
            continue
        source = positions.get(actor_id)
        if not isinstance(source, str):
            continue
        if source == location_id:
            status = "present"
        elif location_id in board_neighbors(story, source):
            status = "adjacent"
        else:
            continue
        candidates.append({
            "actor_id": actor_id,
            "name": story.character_name(actor_id),
            "role": str(character.get("role") or ""),
            "public_profile": str(character.get("public_profile") or ""),
            "motivation": str(character.get("motivation") or ""),
            "location_id": source,
            "status": status,
            "can_enter": status == "adjacent" and actor_id not in moved,
            "moved_this_step": actor_id in moved,
        })
    return tuple(candidates)


def run_director_cycle(
    story: Story,
    state: dict[str, Any],
    result: Any,
    provider: LLMProvider,
    *,
    state_revision: int,
    player_action: str = "",
    world_rules: tuple[str, ...] = (),
    max_beats: int = 2,
) -> DirectorCycleResult:
    """Propose, revalidate and atomically attach optional beats to a turn."""
    cycle = DirectorCycleResult()
    candidates = _director_candidates(story, state, result)
    generation = generation_context(story, state)
    generation_open = any(
        budget.get("remaining", 0) > 0
        for budget in (generation.get("budgets") or {}).values()
    )
    if not candidates and not generation_open:
        result.director_trace = cycle.trace_dict()
        return cycle
    location_id = story.current_location(state)
    scene = story.scene(location_id)
    request = DirectorRequest(
        story_id=story.id,
        state_revision=state_revision,
        turn_no=result.turn_no,
        location_id=location_id,
        location_name=str(scene.get("name") or location_id),
        current_goal=current_goal(story, state),
        player_id=story.player_id,
        player_action=player_action or f"{result.intent}: {', '.join(result.objects)}",
        action_targets=tuple(result.objects),
        committed_result={
            "result_tier": result.result_tier,
            "action_response_hints": list(result.action_response_hints),
            "world_beat_hints": list(result.world_beat_hints),
            "world_reaction_hints": list(result.world_reaction_hints),
        },
        candidates=candidates,
        world_rules=world_rules,
        max_beats=max(0, min(2, max_beats)),
        generation=generation,
    )
    try:
        response = provider.propose_director_beats(request)
    except Exception as exc:
        # Director is an optional post-action layer. Provider/network failures
        # must never strand an already adjudicated player action before its
        # revision and checkpoint are written.
        cycle.error = str(exc)
        result.notes.append(f"Director unavailable: {exc}")
        result.director_trace = cycle.trace_dict()
        return cycle

    cycle.model = response.model
    cycle.prompt_version = response.prompt_version
    cycle.raw = response.raw
    cycle.latency_ms = response.latency_ms
    cycle.usage = dict(response.usage)
    moved = _moved_actor_ids(result, story.player_id)
    used_actors: set[str] = set()
    for beat in response.beats[: request.max_beats]:
        if beat.actor_id in used_actors:
            cycle.rejected.append({
                "beat_id": beat.beat_id,
                "issues": [{"code": "director.actor_duplicate"}],
            })
            continue
        validation = validate_director_beat(
            story,
            state,
            beat,
            state_revision=state_revision,
            moved_actor_ids=moved,
        )
        if not validation.can_schedule:
            cycle.rejected.append({
                "beat_id": beat.beat_id,
                "issues": [issue.to_dict() for issue in validation.issues],
            })
            continue

        committed_changes: tuple[CommittedChange, ...] = ()
        if beat.kind == DirectorBeatKind.ENTER_SCENE:
            path = f"positions.{beat.actor_id}"
            previous = (state.get("positions") or {}).get(beat.actor_id)
            set_value(state, path, beat.target_location_id)
            source = f"director.{beat.beat_id}"
            result.changes.append((path, previous, beat.target_location_id))
            result.change_sources.append(source)
            moved.add(beat.actor_id)
            committed_changes = (CommittedChange(
                path=path,
                previous=previous,
                new=beat.target_location_id,
                authority=AuthorityLevel.MECHANICAL,
                source=source,
                reason="通过验证的既有人物进场节拍",
            ),)

        committed = CommittedDirectorBeat(
            beat_id=beat.beat_id,
            validation_id=validation.validation_id,
            kind=beat.kind,
            actor_id=beat.actor_id,
            target_location_id=beat.target_location_id,
            narrative_hint=beat.summary,
            committed_changes=committed_changes,
        )
        cycle.accepted.append(committed)
        result.director_beats.append(committed)
        result.narrative_hints.append(beat.summary)
        result.world_beat_hints.append(beat.summary)
        used_actors.add(beat.actor_id)

    # Local Canon proposals ride the same cycle but face their own admission
    # checks; the per-turn cap is a hard engine throttle, not a suggestion.
    for proposal in getattr(response, "local_canon", ())[:MAX_LOCAL_CANON_PER_TURN]:
        validation = validate_local_canon(
            story, state, proposal, state_revision=state_revision,
        )
        if not validation.can_commit:
            cycle.rejected.append({
                "proposal_id": proposal.proposal_id,
                "issues": [issue.to_dict() for issue in validation.issues],
            })
            continue
        committed_fact = commit_local_canon(
            story,
            state,
            proposal,
            validation,
            turn_no=result.turn_no,
        )
        for change in committed_fact.committed_changes:
            result.changes.append((change.path, change.previous, change.new))
            result.change_sources.append(change.source)
        cycle.accepted_local_canon.append(committed_fact)
        result.local_canon.append(committed_fact)
        result.narrative_hints.append(committed_fact.narrative_hint)
        result.world_beat_hints.append(committed_fact.narrative_hint)

    result.director_trace = cycle.trace_dict()
    return cycle
