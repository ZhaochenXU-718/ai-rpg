"""The single-director vertical loop for free-text player actions.

understand → validate → (one replan) → quote → confirm → commit

Fairness contract carried over from stage 2: a plan the engine cannot honour
never spends time or a turn; what the quote card promised is exactly the
payload the resolver commits; when understanding fails, the player is told
what was not caught instead of the engine guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .llm import LLMProvider, PlanRequest, PlanResponse
from .llm_protocol import (
    ActionPlan,
    CommittedOutcome,
    IssueSeverity,
    ValidationResult,
    new_protocol_id,
)
from .quote import build_quote
from .session import GameSession
from .state import get_value
from .trace import TraceRecorder

MAX_REPLANS = 1


@dataclass
class ActionLoopResult:
    """Outcome of the understand-validate-quote phase (nothing committed)."""

    plan: ActionPlan
    validation: ValidationResult
    quote: dict[str, Any] | None = None
    clarification: str | None = None
    replanned: bool = False
    confirmation_required: bool = True

    @property
    def can_execute(self) -> bool:
        return self.validation.can_execute and self.quote is not None

    def rejection_messages(self) -> list[str]:
        return [
            issue.message
            for issue in self.validation.issues
            if issue.severity == IssueSeverity.ERROR
        ]

    def adjustment_messages(self) -> list[str]:
        return [
            issue.message
            for issue in self.validation.issues
            if issue.severity == IssueSeverity.ADJUSTMENT
        ]


def _world_rules(session: GameSession) -> tuple[str, ...]:
    """Player-known boundaries fed to the understanding layer."""
    data = session.story.data
    rules = list((data.get("player_role") or {}).get("constraints") or [])
    rules.extend((data.get("global_rules") or {}).get("boundaries") or [])
    return tuple(str(rule) for rule in rules)


def _proposal_space(session: GameSession) -> dict[str, Any]:
    """Soft-state paths open to proposals (mechanics surface, not spoilers)."""
    limits = session.story.resolution_limits or {}
    return {
        "patchable": dict(limits.get("patchable") or {}),
        "max_paths_per_action": limits.get("max_paths_per_action"),
    }


def _propose(
    session: GameSession,
    provider: LLMProvider,
    recorder: TraceRecorder,
    player_text: str,
    attempt: int,
    previous: tuple[ActionPlan, ValidationResult] | None,
) -> PlanResponse:
    perception = session.perception()
    recorder.record("perception", {
        "turn": session.turn_no + 1,
        "attempt": attempt,
        "perception": perception.to_dict(),
    })
    # A pending clarification makes this input a reply to it: carry the
    # question and the plan it belongs to so the model has the exchange.
    pending = session.pending_clarification if attempt == 0 else None
    previous_plan = previous[0] if previous else (pending or {}).get("plan")
    response = provider.propose_plan(PlanRequest(
        perception=perception,
        player_text=player_text,
        attempt=attempt,
        previous_plan=previous_plan,
        previous_validation=previous[1] if previous else None,
        world_rules=_world_rules(session),
        proposal_space=_proposal_space(session),
        pending_clarification=(pending or {}).get("question"),
    ))
    recorder.record("llm_response", {
        "turn": session.turn_no + 1,
        "attempt": attempt,
        "model": response.model,
        "prompt_version": response.prompt_version,
        "latency_ms": response.latency_ms,
        "usage": response.usage,
        "raw": response.raw,
        "plan": response.plan.to_dict(),
    })
    return response


def _build_plan_quote(
    session: GameSession,
    plan: ActionPlan,
    validation: ValidationResult,
) -> dict[str, Any] | None:
    payload = session.plan_payload(validation)
    if payload is None:
        return None
    capability_resolution = payload.get("capability_resolution")
    if capability_resolution is not None:
        expected_changes = []
        for mutation in capability_resolution.mutations:
            expected_changes.append((
                mutation.path,
                get_value(session.state, mutation.path),
                mutation.value,
            ))
        return {
            "quote_id": new_protocol_id("quote"),
            "intent": capability_resolution.capability_id,
            "objects": list(capability_resolution.objects),
            "player_text": plan.player_text,
            "classification": "capability",
            "understanding": plan.interpretation,
            "benefits": [],
            "risks": [risk.description for risk in plan.risks],
            "costs": {},
            "disclosed_changes": [],
            "expected_move": None,
            "expected_changes": expected_changes,
            "expected_storylets": [],
            "expected_fact_count": 0,
            "expected_ending": None,
            "proposal": {},
            "notes": [],
            "can_execute": True,
            "rejection_reason_in_world": None,
            "requote_count": 0,
            "plan_id": plan.plan_id,
            "validation_id": validation.validation_id,
        }
    unauthored = bool(payload.get("allow_unauthored"))
    quote = build_quote(
        session.story,
        session.state,
        payload["intent_id"],
        payload["objects"],
        player_text=plan.player_text,
        temporaries=session.temporaries,
        consumed=session.consumed,
        turn_no=session.turn_no + 1,
        proposal=payload["generic_patch"],
        allow_unauthored=unauthored,
    )
    # The plan's own understanding replaces the template one; its declared
    # risks join the story-declared quote warnings.
    quote["understanding"] = plan.interpretation
    quote["classification"] = "creative" if payload["intent_id"] == "custom" else "in_rules"
    quote["risks"] = list(quote["risks"]) + [risk.description for risk in plan.risks]
    if unauthored:
        quote["risks"].append("没有把握：这不是一条已知可行的路径，尝试会消耗时间，可能一无所获。")
    quote["plan_id"] = plan.plan_id
    quote["validation_id"] = validation.validation_id
    return quote


def run_action_loop(
    session: GameSession,
    provider: LLMProvider,
    player_text: str,
    recorder: TraceRecorder,
) -> ActionLoopResult:
    """Understand and validate a free-text action; commit is a separate step."""
    previous: tuple[ActionPlan, ValidationResult] | None = None
    replanned = False
    for attempt in range(MAX_REPLANS + 1):
        response = _propose(session, provider, recorder, player_text, attempt, previous)
        plan = response.plan
        validation = session.validate_plan(plan)
        recorder.record("validation", {
            "turn": session.turn_no + 1,
            "attempt": attempt,
            "validation": validation.to_dict(),
        })
        if plan.needs_clarification:
            session.pending_clarification = {
                "plan": plan,
                "question": plan.clarification_question,
            }
            return ActionLoopResult(
                plan=plan,
                validation=validation,
                clarification=plan.clarification_question,
                replanned=replanned,
            )
        # The reply (if any) was consumed by this understanding round.
        session.pending_clarification = None
        if validation.can_execute:
            quote = _build_plan_quote(session, plan, validation)
            payload = session.plan_payload(validation) or {}
            confirmation_required = bool(
                payload.get(
                    "confirmation_required",
                    session.story.quote_required(
                        str(payload.get("intent_id") or "")
                    ),
                )
            )
            return ActionLoopResult(
                plan=plan,
                validation=validation,
                quote=quote,
                replanned=replanned,
                confirmation_required=confirmation_required,
            )
        if not validation.can_replan or attempt == MAX_REPLANS:
            return ActionLoopResult(plan=plan, validation=validation, replanned=replanned)
        previous = (plan, validation)
        replanned = True
    raise AssertionError("unreachable")


def commit_action(
    session: GameSession,
    loop_result: ActionLoopResult,
    recorder: TraceRecorder,
    director_provider: LLMProvider | None = None,
) -> CommittedOutcome:
    """Commit a validated plan, after confirmation only when policy requires it."""
    decision_event = (
        "quote_confirmed" if loop_result.confirmation_required else "auto_committed"
    )
    recorder.record(decision_event, {
        "turn": session.turn_no + 1,
        "state_revision": loop_result.validation.state_revision,
        "plan_id": loop_result.plan.plan_id,
        "validation_id": loop_result.validation.validation_id,
        "quote": {
            key: value for key, value in (loop_result.quote or {}).items()
            if key not in ("expected_changes", "expected_storylets")
        },
    })
    outcome = session.resolve_plan(
        loop_result.plan,
        loop_result.validation,
        director_provider=director_provider,
    )
    if session.last_result is not None and session.last_result.director_trace:
        recorder.record("director_cycle", {
            "turn": outcome.turn_no,
            "state_revision_before": outcome.state_revision_before,
            **session.last_result.director_trace,
        })
    recorder.record("committed_outcome", {
        "turn": outcome.turn_no,
        "checkpoint_id": session.current_checkpoint_id,
        "branch_id": session.current_branch_id,
        "outcome": outcome.to_dict(),
    })
    return outcome
