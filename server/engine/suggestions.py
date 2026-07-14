"""Generate, validate and execute frozen LLM action suggestion cards."""

from __future__ import annotations

import json

from .llm import LLMProvider, SuggestionRequest
from .llm_loop import ActionLoopResult, _build_plan_quote
from .llm_protocol import (
    SuggestedAction,
    SuggestedActionSet,
    new_protocol_id,
)
from .session import GameSession, SessionError
from .trace import TraceRecorder


def _world_rules(session: GameSession) -> tuple[str, ...]:
    data = session.story.data
    rules = list((data.get("player_role") or {}).get("constraints") or [])
    rules.extend((data.get("global_rules") or {}).get("boundaries") or [])
    return tuple(str(rule) for rule in rules)


def _proposal_space(session: GameSession) -> dict:
    limits = session.story.resolution_limits or {}
    return {
        "patchable": dict(limits.get("patchable") or {}),
        "max_paths_per_action": limits.get("max_paths_per_action"),
    }


def generate_action_suggestions(
    session: GameSession,
    provider: LLMProvider,
    recorder: TraceRecorder,
    *,
    count: int = 5,
) -> SuggestedActionSet:
    """Ask the LLM for drafts, then expose only executable, distinct cards."""
    count = max(1, min(5, count))
    perception = session.perception()
    response = provider.propose_suggestions(SuggestionRequest(
        perception=perception,
        count=count,
        world_rules=_world_rules(session),
        proposal_space=_proposal_space(session),
    ))
    recorder.record("suggestions_response", {
        "turn": session.turn_no + 1,
        "state_revision": session.state_revision,
        "model": response.model,
        "prompt_version": response.prompt_version,
        "latency_ms": response.latency_ms,
        "usage": response.usage,
        "raw": response.raw,
        "drafts": [draft.to_dict() for draft in response.suggestions],
    })

    accepted: list[SuggestedAction] = []
    seen: set[str] = set()
    rejected: list[dict] = []
    for draft in response.suggestions:
        validation = session.validate_plan(draft.plan)
        if not validation.can_execute:
            rejected.append({
                "suggestion_id": draft.suggestion_id,
                "issues": [issue.to_dict() for issue in validation.issues],
            })
            continue
        step = draft.plan.steps[0]
        signature = json.dumps(
            [step.tool_id, step.arguments],
            ensure_ascii=False,
            sort_keys=True,
        )
        if signature in seen:
            rejected.append({
                "suggestion_id": draft.suggestion_id,
                "issues": [{"code": "suggestion.duplicate"}],
            })
            continue
        seen.add(signature)
        accepted.append(SuggestedAction(
            **draft.to_dict(),
            validation=validation,
        ))
        if len(accepted) >= count:
            break

    recorder.record("suggestions_validated", {
        "turn": session.turn_no + 1,
        "state_revision": session.state_revision,
        "accepted_ids": [action.suggestion_id for action in accepted],
        "rejected": rejected,
    })
    if not accepted:
        raise SessionError("当前没有生成通过验证的行动提案，请直接输入你的做法")
    return SuggestedActionSet(
        suggestion_set_id=new_protocol_id("suggestions"),
        perception_revision=session.state_revision,
        actions=tuple(accepted),
    )


def prepare_suggested_action(
    session: GameSession,
    suggestion: SuggestedAction,
) -> ActionLoopResult:
    """Use the card's frozen validation; never send its prose back to the LLM."""
    if suggestion.perception_revision != session.state_revision:
        raise SessionError(
            "行动提案已经过期：世界状态发生了变化，请重新生成提案"
        )
    payload = session.plan_payload(suggestion.validation)
    if payload is None:
        raise SessionError("行动提案的验证结果已经失效，请重新生成提案")
    quote = _build_plan_quote(session, suggestion.plan, suggestion.validation)
    confirmation_required = bool(
        payload.get(
            "confirmation_required",
            session.story.quote_required(str(payload.get("intent_id") or "")),
        )
    )
    return ActionLoopResult(
        plan=suggestion.plan,
        validation=suggestion.validation,
        quote=quote,
        confirmation_required=confirmation_required,
    )
