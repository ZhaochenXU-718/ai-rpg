"""Editable narrative proposal cards with no execution authority."""

from __future__ import annotations

import re

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
    hidden_tokens = _hidden_authored_tokens(session)
    return tuple(
        text
        for rule in rules
        if (text := str(rule).strip())
        and not _contains_token(text, hidden_tokens)
    )


def _hidden_authored_tokens(session: GameSession) -> tuple[str, ...]:
    perception = session.perception()
    visible_ids = {
        perception.subject_id,
        perception.location_id,
        *(
            entity.entity_id
            for entity in (*perception.visible_entities, *perception.inventory)
        ),
    }
    catalogs = (
        session.story.characters,
        session.story.items,
        session.story.scenes,
    )
    tokens: list[str] = []
    for catalog in catalogs:
        for entity_id, record in catalog.items():
            if entity_id in visible_ids:
                continue
            tokens.append(str(entity_id))
            if isinstance(record, dict) and record.get("name"):
                tokens.append(str(record["name"]))
    return tuple(dict.fromkeys(token for token in tokens if token.strip()))


def _contains_token(text: str, tokens: tuple[str, ...]) -> bool:
    for token in tokens:
        if token.isascii() and re.fullmatch(r"[A-Za-z0-9_-]+", token):
            if re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])",
                text,
            ):
                return True
        elif token in text:
            return True
    return False


def _normalize_action_text(session: GameSession, text: str) -> str:
    action_text = text.strip()
    if not action_text or action_text.startswith("我"):
        return action_text
    for entity in session.perception().visible_entities:
        if entity.kind != EntityKind.CHARACTER or not action_text.startswith(
            entity.label
        ):
            continue
        spoken = action_text[len(entity.label):].lstrip("，,：: ").strip()
        spoken = spoken.strip("“”\"")
        if spoken:
            return f"我对{entity.label}说：“{spoken}”"
    return f"我打算这样做：{action_text}"


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
            "先补足可见信息，再决定是否改变人物或物品的位置。",
        ))
    if exits:
        target = exits[0]
        drafts.append((
            target.label,
            f"我按「{target.label}」指明的方向动身，同时留意沿途人物和物品的位置。",
            "practical",
            "尝试改变场景；真正发生的移动会在散文生成后核对。",
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
    memory_context = session.memory_context()
    memory_payload = (
        memory_context.to_dict() if memory_context is not None else None
    )
    raw_drafts: list[tuple[str, str, str, str]] = []
    try:
        response = provider.propose_suggestions(SuggestionRequest(
            perception=perception,
            count=count,
            boundaries=_boundaries(session),
            memory_context=memory_context,
        ))
    except Exception as exc:
        recorder.record("suggestions_fallback", {
            "turn": session.turn_no + 1,
            "state_revision": session.state_revision,
            "error": str(exc),
            "diagnostics": dict(getattr(exc, "diagnostics", {}) or {}),
            "memory_context": memory_payload,
        })
    else:
        recorder.record("suggestions_response", {
            "turn": session.turn_no + 1,
            "state_revision": session.state_revision,
            "model": response.model,
            "prompt_version": response.prompt_version,
            "latency_ms": response.latency_ms,
            "usage": response.usage,
            "diagnostics": response.diagnostics,
            "raw": response.raw,
            "memory_context": memory_payload,
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
    hidden_tokens = _hidden_authored_tokens(session)
    for title, action_text, focus, rationale in raw_drafts:
        if _contains_token(
            "\n".join((title, action_text, rationale)),
            hidden_tokens,
        ):
            continue
        action_text = _normalize_action_text(session, action_text)
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
        "memory_context": memory_payload,
    })
    return result


def prepare_suggested_action(
    session: GameSession,
    suggestion: SuggestedAction,
) -> str:
    if suggestion.perception_revision != session.state_revision:
        raise SessionError("行动提案已经过期：世界状态发生了变化，请重新生成提案")
    return suggestion.action_text
