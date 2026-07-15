"""Editable narrative proposal cards with no execution authority."""

from __future__ import annotations

from .llm import LLMProvider, SuggestionRequest
from .llm_protocol import (
    EntityKind,
    SuggestedAction,
    SuggestedActionSet,
    new_protocol_id,
)
from .session import GameSession, SessionError
from .trace import TraceRecorder


def _boundaries(session: GameSession) -> tuple[str, ...]:
    data = session.story.data
    rules = list((data.get("player_role") or {}).get("constraints") or [])
    rules.extend((data.get("global_rules") or {}).get("boundaries") or [])
    return tuple(str(rule) for rule in rules)


def _expected_touches(session: GameSession, text: str) -> tuple[str, ...]:
    perception = session.perception()
    touches: list[str] = []
    for entity in perception.visible_entities:
        if entity.label not in text and entity.entity_id not in text:
            continue
        if entity.kind == EntityKind.EXIT:
            touches.append("人物位置")
        elif entity.kind == EntityKind.ITEM:
            touches.append("关键物品归属")
    if any(token in text for token in ("告诉", "说出秘密", "坦白")):
        touches.append("秘密披露")
    if any(token in text for token in ("答应", "承诺", "约好")):
        touches.append("人物承诺")
    return tuple(dict.fromkeys(touches))


def _fallback_drafts(session: GameSession) -> list[tuple[str, str, str, str]]:
    perception = session.perception()
    characters = [
        entity for entity in perception.visible_entities
        if entity.kind == EntityKind.CHARACTER
    ]
    environments = [
        entity for entity in perception.visible_entities
        if entity.kind in {EntityKind.ENVIRONMENT, EntityKind.STATE}
    ]
    exits = [
        entity for entity in perception.visible_entities
        if entity.kind == EntityKind.EXIT
    ]
    drafts: list[tuple[str, str, str, str]] = []
    if characters:
        target = characters[0]
        drafts.append((
            f"先听听{target.label}怎么说",
            f"我先和{target.label}聊聊，听清对方眼下真正顾虑的事。",
            "social",
            "从人物动机入手，不预设对方一定答应。",
        ))
    if environments:
        target = environments[0]
        drafts.append((
            f"仔细看看{target.label}",
            f"我停下来仔细观察{target.label}，确认眼前有哪些已经存在的线索和条件。",
            "investigate",
            "先补足可见信息，再决定是否推动铁律事实。",
        ))
    if exits:
        target = exits[0]
        drafts.append((
            target.label,
            f"我按「{target.label}」指明的方向动身，同时留意沿途人物和物品的位置。",
            "practical",
            "尝试改变场景，但移动事实仍需后续校验。",
        ))
    drafts.append((
        "换一种自己的做法",
        "我不急着照现成路线行动，先用自己的方式回应眼前局面，并把目标说具体。",
        "creative",
        "提案只是灵感，玩家始终可以完全改写。",
    ))
    return drafts


def generate_action_suggestions(
    session: GameSession,
    provider: LLMProvider,
    recorder: TraceRecorder,
    *,
    count: int = 5,
) -> SuggestedActionSet:
    count = max(1, min(5, count))
    perception = session.perception()
    raw_drafts: list[tuple[str, str, str, str]] = []
    try:
        response = provider.propose_suggestions(SuggestionRequest(
            perception=perception,
            count=count,
            boundaries=_boundaries(session),
        ))
    except Exception as exc:
        recorder.record("suggestions_fallback", {
            "turn": session.turn_no + 1,
            "state_revision": session.state_revision,
            "error": str(exc),
        })
    else:
        recorder.record("suggestions_response", {
            "turn": session.turn_no + 1,
            "state_revision": session.state_revision,
            "model": response.model,
            "prompt_version": response.prompt_version,
            "latency_ms": response.latency_ms,
            "usage": response.usage,
            "raw": response.raw,
        })
        raw_drafts.extend(
            (draft.title, draft.action_text, draft.focus, draft.rationale)
            for draft in response.suggestions
            if (
                draft.perception_revision == perception.state_revision
                and draft.action_text.strip()
            )
        )

    raw_drafts.extend(_fallback_drafts(session))
    actions: list[SuggestedAction] = []
    seen: set[str] = set()
    for title, action_text, focus, rationale in raw_drafts:
        signature = " ".join(action_text.split())
        if not signature or signature in seen:
            continue
        seen.add(signature)
        actions.append(SuggestedAction(
            suggestion_id=new_protocol_id("suggestion"),
            perception_revision=session.state_revision,
            title=title,
            action_text=action_text,
            focus=focus,
            rationale=rationale,
            expected_iron_law_touches=_expected_touches(session, action_text),
        ))
        if len(actions) >= count:
            break
    if not actions:
        raise SessionError("当前没有可用的行动提案，请直接输入你的做法")
    result = SuggestedActionSet(
        suggestion_set_id=new_protocol_id("suggestions"),
        perception_revision=session.state_revision,
        actions=tuple(actions),
    )
    recorder.record("suggestions_ready", {
        "turn": session.turn_no + 1,
        "state_revision": session.state_revision,
        "suggestion_set_id": result.suggestion_set_id,
        "actions": [action.action_text for action in result.actions],
    })
    return result


def prepare_suggested_action(
    session: GameSession,
    suggestion: SuggestedAction,
) -> str:
    if suggestion.perception_revision != session.state_revision:
        raise SessionError("行动提案已经过期：世界状态发生了变化，请重新生成提案")
    return suggestion.action_text
