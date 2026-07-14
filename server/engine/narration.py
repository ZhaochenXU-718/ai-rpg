"""LLM narrative rendering with facts-only inputs and template fallback.

The renderer never sees raw state.  A fact sheet is assembled from the
committed TurnResult plus perception-safe context (plan section 8.2), the
provider turns it into prose, and any failure falls back to the stage-2
template silently — narration must never block or corrupt a turn.

The fairness line: prose may dramatise the facts, not extend them.  The
system prompt forbids inventing entities, spaces or events, and the fact
sheet is the whole authority the renderer gets.
"""

from __future__ import annotations

from typing import Any

from .content import Story
from .director import current_goal
from .llm import LLMProvider, NarrativeRequest
from .perception import filter_changes_for_player, perception_config
from .resolver import TurnResult
from .session import GameSession
from .state import get_value
from .trace import TraceRecorder


def _style(story: Story) -> dict[str, Any]:
    return dict(story.data.get("style_bible") or {})


def _public_snapshot(story: Story, state: dict[str, Any]) -> dict[str, Any]:
    """Player-visible numbers for tone (time pressure, visible attitudes)."""
    config = perception_config(story)
    snapshot: dict[str, Any] = {}
    for key, label in config["world_state"].items():
        value = get_value(state, f"world.{key}")
        if value is not None:
            snapshot[label] = value
    for char_id in story.characters_at(state):
        values = {}
        for key, label in config["character_state"].items():
            value = get_value(state, f"{char_id}.{key}")
            if value is not None:
                values[label] = value
        if values:
            snapshot[story.character_name(char_id)] = values
    return snapshot


def _target_location(
    story: Story,
    state: dict[str, Any],
    action_scene_id: str,
    current_scene_id: str,
    object_id: str,
) -> str:
    """Describe a referenced target without leaking hidden coordinates."""
    action_scene_name = story.scene(action_scene_id).get("name", action_scene_id)
    current_scene_name = story.scene(current_scene_id).get("name", current_scene_id)
    if object_id in story.characters:
        position = (state.get("positions") or {}).get(object_id)
        if position == current_scene_id:
            return f"当前场景「{current_scene_name}」，与玩家同场"
        if position == action_scene_id and action_scene_id != current_scene_id:
            return f"行动发生时位于「{action_scene_name}」；现在已不与玩家同场"
        return "已不在当前场景"
    if object_id in story.items:
        placement = (state.get("item_locations") or {}).get(object_id) or {}
        if placement.get("type") == "carried_by":
            owner = placement.get("id")
            if owner == story.player_id:
                return "玩家口袋中"
            if owner in story.characters and owner in story.characters_at(state, current_scene_id):
                return f"由在场人物「{story.character_name(owner)}」携带"
            return "已不在当前可见范围"
        if placement.get("type") == "board" and placement.get("id") == current_scene_id:
            return f"当前场景「{current_scene_name}」"
        if placement.get("type") == "board" and placement.get("id") == action_scene_id:
            return f"行动发生时位于「{action_scene_name}」；现在已不在当前场景"
        return "已不在当前可见范围"
    if object_id in story.world_nodes:
        return "行动发生时的可用出口"
    if action_scene_id == current_scene_id:
        return f"当前场景「{current_scene_name}」中的环境对象"
    return f"行动发生时位于「{action_scene_name}」中的环境对象"


def build_turn_facts(
    story: Story,
    state: dict[str, Any],
    result: TurnResult,
    player_text: str = "",
    recent_events: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Assemble the renderer's whole world: committed, player-visible facts."""
    scene_after = story.scene(result.scene_after)
    intent = story.intent(result.intent)
    facts_label = perception_config(story)["facts_label"]
    facts: dict[str, Any] = {
        "玩家行动": player_text or f"以「{intent.get('label', result.intent)}」方式行动",
        "行动目标": [
            {
                "名称": story.object_label(result.scene_before, obj, state),
                "位置": _target_location(
                    story,
                    state,
                    result.scene_before,
                    result.scene_after,
                    obj,
                ),
            }
            for obj in result.objects
        ],
        "判定档位": result.result_tier,
        "玩家行动回应提示": list(result.action_response_hints),
        "世界节拍提示（非玩家行动直接造成）": list(result.world_beat_hints),
        "世界反应提示（非玩家直接控制）": list(result.world_reaction_hints),
        f"新{facts_label}": list(result.new_facts),
        "当前场景": scene_after.get("name", result.scene_after),
        "当前目标": current_goal(story, state),
        "可见状态变化": [
            f"{path}: {previous} -> {new}"
            for path, previous, new in filter_changes_for_player(story, state, result.changes)
        ],
        "过去回合发生（非本回合）": list(recent_events[-3:]),
        "公开状态": _public_snapshot(story, state),
    }
    if not result.action_response_hints:
        facts["玩家行动无专门回应"] = (
            "没有作者事件卡专门回应这次行动；只能叙述已提交的状态变化，"
            "不得把世界节拍写成该行动的直接成果。"
        )
    if result.scene_after != result.scene_before:
        facts["刚进入新场景"] = {
            "名称": scene_after.get("name", result.scene_after),
            "场景描述": (scene_after.get("entry_text") or "").strip(),
        }
    if not result.fired and not result.narrative_hints:
        facts["本回合无预设事件"] = (
            "这次尝试没有触发任何既定事件——如实描述玩家做了尝试、付出了时间，"
            "但没有取得实质进展；不得虚构成功或新发现。"
        )
    if result.ending:
        ending = story.endings.get(result.ending) or {}
        facts["结局"] = {
            "标题": ending.get("title", result.ending),
            "结局描述": (ending.get("outcome") or "").strip(),
        }
    return facts


def narrate_turn(
    session: GameSession,
    provider: LLMProvider | None,
    result: TurnResult,
    recorder: TraceRecorder | None = None,
    player_text: str = "",
) -> str | None:
    """Render one committed turn as prose; None means use the template."""
    if provider is None:
        return None
    facts = build_turn_facts(
        session.story,
        session.state,
        result,
        player_text=player_text,
        recent_events=tuple(result.prior_events),
    )
    return _render(session, provider, recorder, "turn", facts)


def narrate_rejection(
    session: GameSession,
    provider: LLMProvider | None,
    reasons: list[str],
    player_text: str,
    recorder: TraceRecorder | None = None,
) -> str | None:
    """Voice a rejection in world language; the raw reasons stay printed."""
    if provider is None or not reasons:
        return None
    facts = {
        "玩家行动": player_text,
        "行动未执行": True,
        "拒绝原因": list(reasons),
        "当前场景": session.story.scene(
            session.story.current_location(session.state)
        ).get("name", ""),
        "要求": "用世界内的口吻向玩家解释这个方案为什么行不通，一两句话，不提规则或系统。",
    }
    return _render(session, provider, recorder, "rejection", facts)


def _render(
    session: GameSession,
    provider: LLMProvider,
    recorder: TraceRecorder | None,
    kind: str,
    facts: dict[str, Any],
) -> str | None:
    try:
        response = provider.render_narrative(NarrativeRequest(
            kind=kind, facts=facts, style=_style(session.story),
        ))
    except Exception as exc:
        if recorder is not None:
            recorder.record("narration_error", {
                "turn": session.turn_no, "kind": kind, "error": str(exc),
            })
        return None
    if response is None or not response.text.strip():
        if recorder is not None:
            recorder.record("narration_fallback", {
                "turn": session.turn_no,
                "kind": kind,
                "reason": "empty_response",
            })
        return None
    if recorder is not None:
        recorder.record("narration", {
            "turn": session.turn_no,
            "kind": kind,
            "model": response.model,
            "prompt_version": response.prompt_version,
            "latency_ms": response.latency_ms,
            "usage": response.usage,
            "text": response.text,
        })
    return response.text.strip()
