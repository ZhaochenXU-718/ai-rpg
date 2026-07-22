"""Best-effort model compaction for branch-scoped narrative memory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .llm import LLMProvider, MemoryCompactionRequest
from .memory import plan_memory_compaction
from .modules import (
    compaction_module_catalog,
    compaction_module_updates,
    pending_requires_blockers,
)
from .session import GameSession
from .trace import TraceRecorder


@dataclass(frozen=True)
class MemoryCompactionOutcome:
    """Observable result of an optional post-commit compaction attempt."""

    attempted: bool = False
    success: bool = False
    trigger: str | None = None
    through_turn: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _record_safely(
    recorder: TraceRecorder,
    event: str,
    payload: dict[str, Any],
) -> None:
    """Tracing must not turn a soft-memory failure into a gameplay failure."""
    try:
        recorder.record(event, payload)
    except Exception:
        pass


def maybe_compact_memory(
    session: GameSession,
    provider: LLMProvider,
    recorder: TraceRecorder,
) -> MemoryCompactionOutcome:
    """Compact an eligible old-event batch without endangering its commit."""
    try:
        blocked_modules = pending_requires_blockers(
            session.story, session.memory, session.turn_no
        )
        plan = plan_memory_compaction(
            session.memory,
            urgent=bool(blocked_modules),
        )
    except Exception as exc:
        error = str(exc).strip() or type(exc).__name__
        try:
            session.record_memory_compaction_failure(error)
        except Exception:
            pass
        _record_safely(recorder, "memory_compaction_error", {
            "turn": session.turn_no,
            "state_revision": session.state_revision,
            "error": error,
            "stage": "planning",
        })
        return MemoryCompactionOutcome(attempted=True, error=error)
    if plan is None:
        return MemoryCompactionOutcome()

    common = {
        "turn": session.turn_no,
        "state_revision": session.state_revision,
        "trigger": plan.trigger,
        "through_turn": plan.through_turn,
        "event_turns": [event.turn_no for event in plan.events],
        "bookkeeping_blocked_modules": list(blocked_modules),
    }

    try:
        known_character_ids = {
            group.subject_id
            for group in session.memory.character_notes
        }
        known_scene_ids = {
            group.subject_id
            for group in session.memory.scene_notes
        }
        for event in session.memory.events:
            if event.turn_no > plan.through_turn:
                break
            known_character_ids.update(event.participants)
            known_character_ids.update(
                reference
                for reference in event.references
                if reference in session.story.characters
            )
            known_scene_ids.update((event.scene_before, event.scene_after))
        request = MemoryCompactionRequest(
            story_id=session.story.id,
            state_revision=session.state_revision,
            previous_digest=session.memory.digest,
            events=plan.events,
            character_catalog=tuple(
                (character_id, session.story.character_name(character_id))
                for character_id in session.story.characters
                if character_id in known_character_ids
            ),
            scene_catalog=tuple(
                (scene_id, session.story.location_name(session.state, scene_id))
                for scene_id in session.story.scenes
                if scene_id in known_scene_ids
            ),
            module_catalog=compaction_module_catalog(
                session.story, session.memory
            ),
        )
        _record_safely(recorder, "memory_compaction_requested", common)
        response = provider.compact_memory(request)
        if response.digest.compacted_through_turn != plan.through_turn:
            raise ValueError(
                "memory provider returned an unexpected compaction boundary"
            )
        session.apply_memory_digest(response.digest)
    except Exception as exc:
        error = str(exc).strip() or type(exc).__name__
        diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
        try:
            session.record_memory_compaction_failure(error)
        except Exception:
            # The committed turn and its raw event remain the source of truth.
            pass
        _record_safely(recorder, "memory_compaction_error", {
            **common,
            "error": error,
            "diagnostics": diagnostics,
        })
        return MemoryCompactionOutcome(
            attempted=True,
            trigger=plan.trigger,
            through_turn=plan.through_turn,
            error=error,
        )

    if response.digest.module_updates:
        try:
            records = compaction_module_updates(
                session.memory, response.digest.module_updates
            )
            if records is not None:
                session.apply_module_states(records, reason="memory_compaction")
        except Exception as exc:
            # Module lifecycle stays soft: a bad update must not undo the digest.
            _record_safely(recorder, "module_update_error", {
                **common,
                "error": str(exc).strip() or type(exc).__name__,
            })

    _record_safely(recorder, "memory_compaction", {
        **common,
        "model": response.model,
        "prompt_version": response.prompt_version,
        "latency_ms": response.latency_ms,
        "usage": response.usage,
        "diagnostics": response.diagnostics,
        "raw": response.raw,
        "digest": response.digest.to_dict(),
    })
    return MemoryCompactionOutcome(
        attempted=True,
        success=True,
        trigger=plan.trigger,
        through_turn=plan.through_turn,
    )
