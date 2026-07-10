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

from .content import Story
from .effects import TemporaryEffects
from .limits import clamp_generic_patch
from .state import get_value

MAX_REQUOTES_PER_TURN = 3

# Which soft state a bare intent nudges when no storylet catches the action.
# NPC-targeted intents pick the first NPC among the chosen objects.
NPC_PROPOSALS = {
    "negotiate": ("trust", 1),
    "threaten": ("suspicion", 1),
}
SCENE_PROPOSALS = {
    "create_distraction": {"scene.noise_level": 1},
}

RISK_TEXT = {
    "low": "低风险：最多消耗时间。",
    "medium": "中等风险：可能引起注意或留下印象。",
    "high": "高风险：失败会显著提高怀疑或警觉。",
    "variable": "风险取决于方案本身。",
}


def default_proposal(story: Story, state: dict[str, Any], intent_id: str, objects: list[str]) -> dict[str, Any]:
    npc_rule = NPC_PROPOSALS.get(intent_id)
    if npc_rule:
        key, step = npc_rule
        for obj in objects:
            if obj in story.characters:
                return {f"{obj}.{key}": step}
        return {}
    return dict(SCENE_PROPOSALS.get(intent_id, {}))


def hazard_notes(story: Story, state: dict[str, Any]) -> list[str]:
    notes = []
    if (get_value(state, "scene.fire_risk") or 0) >= 1:
        notes.append("火势风险已存在，再生事端可能失控。")
    for char_id in story.characters:
        suspicion = get_value(state, f"{char_id}.suspicion")
        if isinstance(suspicion, (int, float)) and suspicion >= 4:
            notes.append(f"{story.character_name(char_id)}已经高度怀疑你。")
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
) -> dict[str, Any]:
    intent = story.intent(intent_id)
    label = intent.get("label", intent_id)
    scene_id = story.current_location(state)
    exit_labels = story.exit_labels(state)
    object_names = [
        exit_labels.get(obj, story.object_label(scene_id, obj, state)) for obj in objects
    ]

    proposal = default_proposal(story, state, intent_id, objects)
    accepted, clamp_notes = clamp_generic_patch(proposal, state, story.resolution_limits)
    if intent_id in NPC_PROPOSALS and not any(obj in story.characters for obj in objects):
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
