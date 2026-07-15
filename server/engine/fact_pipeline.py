"""Phase 2 prose → extraction → iron-law validation → atomic commit loop."""

from __future__ import annotations

from .iron_laws import build_fact_ledger, validate_fact_extraction
from .llm import FactExtractionRequest, LLMProvider
from .llm_protocol import IronLawViolation
from .narration import narrate_player_turn
from .resolver import TurnResult
from .session import GameSession, SessionError
from .trace import TraceRecorder


MAX_REGENERATIONS = 1


class TurnResolutionError(SessionError):
    def __init__(self, violations: tuple[IronLawViolation, ...]) -> None:
        self.violations = violations
        detail = "；".join(violation.message for violation in violations)
        super().__init__(
            "眼前的事实接不上这段发展，本回合没有发生：" + detail
        )


def resolve_player_turn(
    session: GameSession,
    provider: LLMProvider,
    recorder: TraceRecorder,
    player_text: str,
    *,
    max_regenerations: int = MAX_REGENERATIONS,
) -> TurnResult:
    """Resolve one player input without charging failed prose candidates."""
    perception = session.perception()
    feedback: tuple[IronLawViolation, ...] = ()
    attempts = max(0, min(2, max_regenerations)) + 1

    for attempt in range(attempts):
        narrative, references = narrate_player_turn(
            session,
            provider,
            player_text,
            recorder,
            violations=feedback,
        )
        request = FactExtractionRequest(
            perception=perception,
            player_text=player_text,
            narrative=narrative,
            ledger=build_fact_ledger(session.story, session.state, perception),
        )
        response = provider.extract_facts(request)
        recorder.record("fact_extraction", {
            "turn": session.turn_no + 1,
            "attempt": attempt + 1,
            "state_revision": session.state_revision,
            "model": response.model,
            "prompt_version": response.prompt_version,
            "latency_ms": response.latency_ms,
            "usage": response.usage,
            "raw": response.raw,
            "extraction": response.extraction.to_dict(),
        })
        validation = validate_fact_extraction(
            session.story,
            session.state,
            response.extraction,
            player_text=player_text,
            narrative=narrative,
            references=references,
            perception=perception,
            turn_no=session.turn_no + 1,
        )
        recorder.record("iron_law_validation", {
            "turn": session.turn_no + 1,
            "attempt": attempt + 1,
            "state_revision": session.state_revision,
            "accepted": validation.accepted,
            "violations": [
                violation.to_dict() for violation in validation.violations
            ],
            "state_changes": (
                validation.batch.state_changes if validation.batch else {}
            ),
        })
        if validation.accepted and validation.batch is not None:
            result = session.commit_fact_batch(
                validation.batch,
                director_provider=provider,
            )
            recorder.record("turn_committed", {
                "turn": result.turn_no,
                "state_revision": session.state_revision,
                "batch_id": validation.batch.batch_id,
                "committed_turn": (
                    result.committed_turn.to_dict()
                    if result.committed_turn is not None
                    else None
                ),
            })
            return result

        feedback = validation.violations
        if any(not violation.retryable for violation in feedback):
            break
        if attempt + 1 < attempts:
            recorder.record("narrative_regeneration", {
                "turn": session.turn_no + 1,
                "next_attempt": attempt + 2,
                "violations": [
                    violation.to_dict() for violation in feedback
                ],
            })

    raise TurnResolutionError(feedback)
