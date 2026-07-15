"""Narrative-first prose generation scoped by the player's perception."""

from __future__ import annotations

from typing import Any

from .llm import LLMProvider, NarrativeRequest
from .llm_protocol import IronLawViolation
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
    violations: tuple[IronLawViolation, ...] = (),
) -> dict[str, Any]:
    perception = session.perception()
    scene = session.story.scene(perception.location_id)
    visible = {
        entity.entity_id: {
            "名称": entity.label,
            "类型": entity.kind.value,
            "描述": entity.description,
            "公开状态": entity.public_state,
        }
        for entity in perception.visible_entities
    }
    boundaries = list(
        (session.story.data.get("player_role") or {}).get("constraints") or []
    ) + list((session.story.data.get("global_rules") or {}).get("boundaries") or [])
    facts = {
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
        "事实表达要求": (
            "可以写行动成功、失败或人物回应。若位置、物品归属、秘密披露或人物承诺"
            "发生变化，必须在散文中明确写出；不得跳过在场、相邻路线与当前归属。"
        ),
        "输出要求": "输出 2-4 句连贯散文，不解释系统阶段，不输出状态表。",
    }
    if violations:
        facts["上次候选的冲突反馈"] = [
            {
                "code": violation.code,
                "message": violation.message,
                "evidence": violation.evidence,
            }
            for violation in violations
        ]
        facts["重写要求"] = "修正冲突事实；不要在散文中提及校验或重生成。"
    return facts


def narrate_player_turn(
    session: GameSession,
    provider: LLMProvider,
    player_text: str,
    recorder: TraceRecorder | None = None,
    violations: tuple[IronLawViolation, ...] = (),
) -> tuple[str, tuple[str, ...]]:
    references = match_references(session, player_text)
    facts = build_narrative_facts(
        session, player_text, references, violations=violations
    )
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
            "regeneration_feedback": [
                violation.to_dict() for violation in violations
            ],
        })
    return narrative, references
