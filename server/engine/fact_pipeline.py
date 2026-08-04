"""Phase 2 prose → extraction → iron-law validation → atomic commit loop."""

from __future__ import annotations

from typing import Any

from .iron_laws import (
    FactValidationResult,
    build_fact_ledger,
    validate_fact_extraction,
)
from .llm import FactExtractionRequest, LLMProvider, NarrativeStream
from .llm_protocol import PerceptionSnapshot, PhysicalFactViolation
from .memory_pipeline import BackgroundMemoryCompactor, maybe_compact_memory
from .modules import offered_module_states, select_candidate_modules
from .narration import narrate_player_turn
from .prose_editor import (
    ProseEditorComparison,
    ProseEditorMode,
    ProseEditorObserver,
    run_prose_editor,
)
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


def _protected_editor_terms(session: GameSession) -> tuple[str, ...]:
    perception = session.perception()
    terms = [perception.location_name]
    terms.extend(
        entity.label
        for entity in (*perception.visible_entities, *perception.inventory)
    )
    return tuple(dict.fromkeys(term.strip() for term in terms if term.strip()))


def _extract_and_validate(
    session: GameSession,
    provider: LLMProvider,
    recorder: TraceRecorder,
    *,
    perception: PerceptionSnapshot,
    player_text: str,
    narrative: str,
    references: tuple[str, ...],
    ledger: dict[str, Any],
    attempt: int,
    candidate_kind: str,
) -> FactValidationResult:
    request = FactExtractionRequest(
        perception=perception,
        player_text=player_text,
        narrative=narrative,
        ledger=ledger,
    )
    try:
        response = provider.extract_facts(request)
    except Exception as exc:
        recorder.record("fact_extraction_error", {
            "turn": session.turn_no + 1,
            "attempt": attempt,
            "candidate_kind": candidate_kind,
            "state_revision": session.state_revision,
            "error": str(exc),
            "diagnostics": dict(getattr(exc, "diagnostics", {}) or {}),
        })
        raise
    recorder.record("fact_extraction", {
        "turn": session.turn_no + 1,
        "attempt": attempt,
        "candidate_kind": candidate_kind,
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
        "attempt": attempt,
        "candidate_kind": candidate_kind,
        "state_revision": session.state_revision,
        "accepted": validation.accepted,
        "violations": [
            violation.to_dict() for violation in validation.violations
        ],
        "state_changes": (
            validation.batch.state_changes if validation.batch else {}
        ),
    })
    return validation


def resolve_player_turn(
    session: GameSession,
    provider: LLMProvider,
    recorder: TraceRecorder,
    player_text: str,
    *,
    max_regenerations: int = MAX_REGENERATIONS,
    stream: NarrativeStream | None = None,
    compactor: BackgroundMemoryCompactor | None = None,
    prose_editor_mode: ProseEditorMode | str = ProseEditorMode.OFF,
    prose_editor_observer: ProseEditorObserver | None = None,
) -> TurnResult:
    """Resolve one player input without charging failed prose candidates.

    ``stream`` mirrors narrative prose to the caller as it is generated;
    ``compactor`` moves the post-commit memory compaction call off the
    gameplay thread instead of running it synchronously here. ``on`` prose
    editing buffers the draft and compares its physical signature with the
    edited candidate before either text reaches ``stream``.
    """
    editor_mode = ProseEditorMode.coerce(prose_editor_mode)
    perception = session.perception()
    ledger = build_fact_ledger(session.story, session.state, perception)
    previous_narrative = (
        session.memory.events[-1].narrative if session.memory.events else ""
    )
    protected_terms = _protected_editor_terms(session)
    candidate_module_ids = tuple(
        candidate.module_id
        for candidate in select_candidate_modules(
            session.story, session.state, session.memory, session.turn_no
        )
    )
    feedback: tuple[PhysicalFactViolation, ...] = ()
    attempts = max(0, min(2, max_regenerations)) + 1

    for attempt in range(attempts):
        # ``on`` buffers the draft. Nothing reaches the player until both the
        # original and edited physical signatures have been checked.
        narration_stream = None if editor_mode is ProseEditorMode.ON else stream
        draft, references = narrate_player_turn(
            session,
            provider,
            player_text,
            recorder,
            violations=feedback,
            stream=narration_stream,
        )
        validation = _extract_and_validate(
            session,
            provider,
            recorder,
            perception=perception,
            player_text=player_text,
            narrative=draft,
            references=references,
            ledger=ledger,
            attempt=attempt + 1,
            candidate_kind="draft",
        )
        if validation.accepted and validation.batch is not None:
            selected_validation = validation
            selection_reason = "editor_off"
            editor_comparison: ProseEditorComparison | None = None

            if editor_mode is not ProseEditorMode.OFF:
                edit_outcome = run_prose_editor(
                    provider,
                    recorder,
                    mode=editor_mode,
                    turn_no=session.turn_no + 1,
                    draft=draft,
                    player_text=player_text,
                    previous_narrative=previous_narrative,
                    protected_terms=protected_terms,
                )
                if editor_mode is ProseEditorMode.SHADOW:
                    selection_reason = "shadow_mode"
                elif edit_outcome.usable_candidate:
                    try:
                        edited_validation = _extract_and_validate(
                            session,
                            provider,
                            recorder,
                            perception=perception,
                            player_text=player_text,
                            narrative=edit_outcome.candidate or draft,
                            references=references,
                            ledger=ledger,
                            attempt=attempt + 1,
                            candidate_kind="edited",
                        )
                    except Exception as exc:
                        selection_reason = "edited_fact_check_error"
                        recorder.record("prose_editor_fact_check_error", {
                            "turn": session.turn_no + 1,
                            "error": str(exc),
                            "diagnostics": dict(
                                getattr(exc, "diagnostics", {}) or {}
                            ),
                        })
                    else:
                        same_physical_changes = (
                            edited_validation.accepted
                            and edited_validation.batch is not None
                            and edited_validation.batch.state_changes
                            == validation.batch.state_changes
                        )
                        if same_physical_changes:
                            selected_validation = edited_validation
                            selection_reason = "edited_candidate_accepted"
                        elif not edited_validation.accepted:
                            selection_reason = "edited_fact_validation_failed"
                        else:
                            selection_reason = "physical_facts_changed"
                else:
                    if edit_outcome.status == "error":
                        selection_reason = "editor_error"
                    elif edit_outcome.guard.accepted:
                        selection_reason = "editor_unchanged"
                    else:
                        selection_reason = "editor_candidate_rejected"

                selected_kind = (
                    "edited"
                    if selected_validation is not validation
                    else "draft"
                )
                recorder.record("prose_editor_selection", {
                    "turn": session.turn_no + 1,
                    "mode": editor_mode.value,
                    "selected": selected_kind,
                    "reason": selection_reason,
                    "draft": draft,
                    "final": selected_validation.batch.narrative,
                })
                editor_comparison = ProseEditorComparison(
                    turn_no=session.turn_no + 1,
                    mode=editor_mode,
                    status=edit_outcome.status,
                    draft=draft,
                    candidate=edit_outcome.candidate,
                    guard=edit_outcome.guard,
                    selected=selected_kind,
                    reason=selection_reason,
                    error=edit_outcome.error,
                )

            if editor_mode is ProseEditorMode.ON and stream is not None:
                stream.delta(selected_validation.batch.narrative)
            result = session.commit_fact_batch(selected_validation.batch)
            if editor_comparison is not None and prose_editor_observer is not None:
                try:
                    prose_editor_observer.comparison(editor_comparison)
                except Exception as exc:
                    recorder.record("prose_editor_observer_error", {
                        "turn": result.turn_no,
                        "error": str(exc),
                    })
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
                "batch_id": selected_validation.batch.batch_id,
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
            if stream is not None and editor_mode is not ProseEditorMode.ON:
                stream.restart("physical_conflict")
            recorder.record("narrative_regeneration", {
                "turn": session.turn_no + 1,
                "next_attempt": attempt + 2,
                "violations": [
                    violation.to_dict() for violation in feedback
                ],
            })

    raise TurnResolutionError(feedback)
