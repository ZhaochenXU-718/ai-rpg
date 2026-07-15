"""Narrative-first prose generation scoped by the player's perception."""

from __future__ import annotations

from typing import Any

from .llm import LLMProvider, NarrativeRequest
from .session import GameSession
from .trace import TraceRecorder


def _style(session: GameSession) -> dict[str, Any]:
    return dict(session.story.data.get("style_bible") or {})


def match_references(session: GameSession, player_text: str) -> tuple[str, ...]:
    """Ground explicit visible ids/labels without classifying an intent."""
    perception = session.perception()
    matched: list[tuple[int, str]] = []
    for entity in (*perception.visible_entities, *perception.inventory):
        positions = [
            player_text.find(token)
            for token in (entity.label, entity.entity_id)
            if token and player_text.find(token) >= 0
        ]
        if positions:
            matched.append((min(positions), entity.entity_id))
    return tuple(dict.fromkeys(entity_id for _, entity_id in sorted(matched)))


def build_narrative_facts(
    session: GameSession,
    player_text: str,
    references: tuple[str, ...],
) -> dict[str, Any]:
    perception = session.perception()
    scene = session.story.scene(perception.location_id)
    visible = {
        entity.entity_id: {
            "名称": entity.label,
            "类型": entity.kind.value,
            "描述": entity.description,
        }
        for entity in perception.visible_entities
    }
    boundaries = list(
        (session.story.data.get("player_role") or {}).get("constraints") or []
    ) + list((session.story.data.get("global_rules") or {}).get("boundaries") or [])
    return {
        "玩家输入": player_text,
        "明确提到的可见实体": [visible[item] for item in references if item in visible],
        "当前场景": {
            "名称": perception.location_name,
            "描述": str(scene.get("entry_text") or "").strip(),
        },
        "当前目标": perception.current_goal,
        "在场可见实体": list(visible.values()),
        "已知事实": list(perception.known_facts),
        "近期叙事": list(perception.recent_events[-3:]),
        "世界边界": [str(rule) for rule in boundaries],
        "本阶段提交限制": (
            "Phase 2 的事实抽取和铁律校验尚未接入。本回合只能描写玩家正在尝试，"
            "不得断言移动完成、物品转移、秘密披露、人物承诺、生死变化或锚点推进。"
        ),
        "输出要求": "输出 2-4 句连贯散文，不解释系统阶段，不输出状态表。",
    }


def narrate_player_turn(
    session: GameSession,
    provider: LLMProvider,
    player_text: str,
    recorder: TraceRecorder | None = None,
) -> tuple[str, tuple[str, ...]]:
    references = match_references(session, player_text)
    facts = build_narrative_facts(session, player_text, references)
    try:
        response = provider.render_narrative(NarrativeRequest(
            kind="turn",
            perception=session.perception(),
            facts=facts,
            style=_style(session),
        ))
    except Exception as exc:
        if recorder is not None:
            recorder.record("narration_error", {
                "turn": session.turn_no + 1,
                "error": str(exc),
            })
        response = None

    if response is None or not response.text.strip():
        attempt = player_text.strip()
        if attempt and attempt[-1] not in "。！？!?":
            attempt += "。"
        narrative = (
            f"你开始尝试：{attempt}眼前的局面暂时没有出现足以写入账本的变化。"
        )
        if recorder is not None:
            recorder.record("narration_fallback", {
                "turn": session.turn_no + 1,
                "reason": "provider_unavailable_or_empty",
            })
        return narrative, references

    narrative = response.text.strip()
    if recorder is not None:
        recorder.record("narration", {
            "turn": session.turn_no + 1,
            "model": response.model,
            "prompt_version": response.prompt_version,
            "latency_ms": response.latency_ms,
            "usage": response.usage,
            "text": narrative,
            "references": list(references),
        })
    return narrative, references
