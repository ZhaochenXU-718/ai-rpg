"""Rule-based quoting: the stage-2 stand-in for the LLM understanding step.

Produces the quote card (understanding / benefits / risks / costs) and a
default generic-patch proposal from intent + objects alone. In stage 3 the
LLM replaces `default_proposal` and the understanding text, but the quote
contract (binding costs, requote limit) stays the same.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from .conditions import check_condition_block
from .content import Story
from .effects import TemporaryEffects
from .limits import clamp_generic_patch
from .perception import filter_changes_for_player

MAX_REQUOTES_PER_TURN = 3

RISK_TEXT = {
    "low": "低风险：最多消耗时间。",
    "medium": "中等风险：可能引起注意或留下印象。",
    "high": "高风险：失败会显著提高怀疑或警觉。",
    "variable": "风险取决于方案本身。",
}


def default_proposal(story: Story, state: dict[str, Any], intent_id: str, objects: list[str]) -> dict[str, Any]:
    """Fallback soft-state proposal declared by the story, not the engine.

    Content declares e.g. ``fallback_proposal: {npc_state: {key: trust,
    step: 1}}`` (applies to the first character among the targets) or a
    literal ``state_patch``.  The engine knows the shapes, never the story's
    vocabulary; the stage-3 LLM replaces this entirely.
    """
    fallback = story.intent(intent_id).get("fallback_proposal") or {}
    npc_rule = fallback.get("npc_state")
    if isinstance(npc_rule, dict) and npc_rule.get("key"):
        for obj in objects:
            if obj in story.characters:
                return {f"{obj}.{npc_rule['key']}": npc_rule.get("step", 1)}
        return {}
    patch = fallback.get("state_patch")
    return dict(patch) if isinstance(patch, dict) else {}


def hazard_notes(story: Story, state: dict[str, Any]) -> list[str]:
    """Story-declared quote warnings whose conditions currently hold."""
    notes = []
    for warning in story.data.get("quote_warnings") or []:
        if not isinstance(warning, dict) or not warning.get("text"):
            continue
        if check_condition_block(state, warning.get("when") or {}):
            notes.append(str(warning["text"]))
    return notes


def build_quote(
    story: Story,
    state: dict[str, Any],
    intent_id: str,
    objects: list[str],
    player_text: str = "",
    requote_count: int = 0,
    *,
    temporaries: TemporaryEffects | None = None,
    consumed: set[str] | None = None,
    turn_no: int = 1,
    proposal: dict[str, Any] | None = None,
    allow_unauthored: bool = False,
) -> dict[str, Any]:
    intent = story.intent(intent_id)
    label = intent.get("label", intent_id)
    scene_id = story.current_location(state)
    exit_labels = story.exit_labels(state)
    object_names = [
        exit_labels.get(obj, story.object_label(scene_id, obj, state)) for obj in objects
    ]

    # A caller-supplied proposal (the validated plan payload) takes priority
    # over the story's rule fallback; both pass the same whitelist clamp.
    if proposal is None:
        proposal = default_proposal(story, state, intent_id, objects)
    accepted, clamp_notes = clamp_generic_patch(proposal, state, story.resolution_limits)
    npc_targeted = isinstance((intent.get("fallback_proposal") or {}).get("npc_state"), dict)
    if npc_targeted and not any(obj in story.characters for obj in objects):
        clamp_notes.append("未指定人物对象，这次行动难以改变任何人的态度；输入 who 查看在场人物。")

    understanding = f"你打算以「{label}」的方式行动"
    if object_names:
        understanding += f"，涉及：{ '、'.join(object_names) }"
    if player_text:
        understanding += f"。你的方案：{player_text}"
    understanding += "。"

    risks = [RISK_TEXT.get(intent.get("base_risk", "medium"), RISK_TEXT["medium"])]
    risks.extend(hazard_notes(story, state))

    costs: dict[str, Any] = {}
    for key, value in (intent.get("typical_cost") or {}).items():
        costs[str(key) if "." in str(key) else f"world.{key}"] = value

    # The engine is deterministic, so the quote can preview the complete
    # state delta on private copies.  This keeps deterministic storylet costs
    # and rewards inside the binding quote instead of surprising the player
    # after confirmation.
    from .resolver import run_turn

    preview_state = copy.deepcopy(state)
    preview_result = run_turn(
        story,
        preview_state,
        copy.deepcopy(temporaries) if temporaries is not None else TemporaryEffects(),
        set(consumed or set()),
        turn_no,
        intent_id,
        objects,
        accepted,
        allow_unauthored=allow_unauthored,
    )
    by_path: dict[str, list[Any]] = {}
    path_order: list[str] = []
    for path, previous, new in preview_result.changes:
        if path not in by_path:
            by_path[path] = [copy.deepcopy(previous), copy.deepcopy(new)]
            path_order.append(path)
        else:
            by_path[path][1] = copy.deepcopy(new)
    expected_changes = [
        (path, by_path[path][0], by_path[path][1])
        for path in path_order
        if path not in costs and by_path[path][0] != by_path[path][1]
    ]
    # Disclosure wall: the player-facing card may only state consequences on
    # state the player can already perceive (story `perception` block).
    # Everything else — reveals, acquisitions, endings — stays a full
    # preview for binding enforcement and logs, never for the card.
    # The filter reads the *pre-action* state on purpose: a quote must not
    # become an oracle for what is about to be discovered.
    disclosed_changes = filter_changes_for_player(story, state, expected_changes)

    # Exception to the wall: the player's OWN movement is a first-class
    # consequence of their own action, not a hidden world fact — hiding it
    # broke quote binding ("我只是想聊天，怎么走出大厅了？").
    expected_move = None
    move_path = f"positions.{story.player_id}"
    if move_path in by_path and by_path[move_path][0] != by_path[move_path][1]:
        origin, destination = by_path[move_path]

        def node_name(node_id: Any) -> str:
            node = story.world_nodes.get(str(node_id)) or {}
            return node.get("name") or story.scene(str(node_id)).get("name", str(node_id))

        expected_move = {"from": node_name(origin), "to": node_name(destination)}

    return {
        "quote_id": uuid.uuid4().hex[:12],
        "intent": intent_id,
        "objects": objects,
        "player_text": player_text,
        "classification": "in_rules",
        "understanding": understanding,
        "benefits": [intent.get("description", "")],
        "risks": risks,
        "costs": costs,
        "disclosed_changes": disclosed_changes,
        "expected_move": expected_move,
        # Internal preview (binding enforcement + logs); never rendered.
        "expected_changes": expected_changes,
        "expected_storylets": list(preview_result.fired),
        "expected_clue_count": len(preview_result.new_clues),
        "expected_ending": preview_result.ending,
        "proposal": accepted,
        "notes": clamp_notes,
        "can_execute": True,
        "rejection_reason_in_world": None,
        "requote_count": requote_count,
    }
