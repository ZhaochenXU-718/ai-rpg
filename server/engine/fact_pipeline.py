"""Phase 2 prose → extraction → iron-law validation → atomic commit loop."""

from __future__ import annotations

from .iron_laws import build_fact_ledger, validate_fact_extraction
from .llm import FactExtractionRequest, LLMProvider, NarrativeStream
from .llm_protocol import PhysicalFactViolation
from .memory_pipeline import BackgroundMemoryCompactor, maybe_compact_memory
from .modules import offered_module_states, select_candidate_modules
from .narration import narrate_player_turn
from .resolver import TurnResult
from .session import GameSession, SessionError
from .trace import TraceRecorder


MAX_REGENERATIONS = 1


class TurnResolutionError(SessionError):
    def __init__(self, violations: tuple[PhysicalFactViolation, ...]) -> None:
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
    stream: NarrativeStream | None = None,
    compactor: BackgroundMemoryCompactor | None = None,
) -> TurnResult:
    """Resolve one player input without charging failed prose candidates.

    ``stream`` mirrors narrative prose to the caller as it is generated;
    ``compactor`` moves the post-commit memory compaction call off the
    gameplay thread instead of running it synchronously here.
    """
    perception = session.perception()
    candidate_module_ids = tuple(
        candidate.module_id
        for candidate in select_candidate_modules(
            session.story, session.state, session.memory, session.turn_no
        )
    )
    feedback: tuple[PhysicalFactViolation, ...] = ()
    attempts = max(0, min(2, max_regenerations)) + 1

    for attempt in range(attempts):
        narrative, references = narrate_player_turn(
            session,
            provider,
            player_text,
            recorder,
            violations=feedback,
            stream=stream,
        )
        request = FactExtractionRequest(
            perception=perception,
            player_text=player_text,
            narrative=narrative,
            ledger=build_fact_ledger(session.story, session.state, perception),
        )
        try:
            response = provider.extract_facts(request)
        except Exception as exc:
            recorder.record("fact_extraction_error", {
                "turn": session.turn_no + 1,
                "attempt": attempt + 1,
                "state_revision": session.state_revision,
                "error": str(exc),
                "diagnostics": dict(getattr(exc, "diagnostics", {}) or {}),
            })
            raise
        recorder.record("fact_extraction", {
            "turn": session.turn_no + 1,
            "attempt": attempt + 1,
            "state_revision": session.state_revision,
            "model": response.model,
            "prompt_version": response.prompt_version,
            "latency_ms": response.latency_ms,
            "usage": response.usage,
            "diagnostics": response.diagnostics,
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
        )
        recorder.record("physical_fact_validation", {
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
            result = session.commit_fact_batch(validation.batch)
            if candidate_module_ids:
                session.apply_module_states(
                    offered_module_states(
                        session.memory,
                        candidate_module_ids,
                        result.turn_no,
                    ),
                    reason="candidates_offered",
                )
            if compactor is not None:
                compaction = compactor.kick(session)
            else:
                compaction = maybe_compact_memory(session, provider, recorder)
            recorder.record("turn_committed", {
                "turn": result.turn_no,
                "state_revision": session.state_revision,
                "batch_id": validation.batch.batch_id,
                "committed_turn": (
                    result.committed_turn.to_dict()
                    if result.committed_turn is not None
                    else None
                ),
                "memory_event": session.memory.events[-1].to_dict(),
                "memory_compaction": compaction.to_dict(),
                "module_candidates": list(candidate_module_ids),
                "module_states": [
                    record.__dict__ for record in session.memory.module_states
                ],
            })
            return result

        feedback = validation.violations
        if any(not violation.retryable for violation in feedback):
            break
        if attempt + 1 < attempts:
            if stream is not None:
                stream.restart("physical_conflict")
            recorder.record("narrative_regeneration", {
                "turn": session.turn_no + 1,
                "next_attempt": attempt + 2,
                "violations": [
                    violation.to_dict() for violation in feedback
                ],
            })

    raise TurnResolutionError(feedback)
