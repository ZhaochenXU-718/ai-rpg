"""Best-effort model compaction for branch-scoped narrative memory."""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, replace
from typing import Any

from .llm import LLMProvider, MemoryCompactionRequest, MemoryCompactionResponse
from .memory import MemoryCompactionPlan, plan_memory_compaction
from .modules import (
    compaction_module_catalog,
    compaction_module_updates,
    pending_requires_blockers,
)
from .session import GameSession
from .trace import TraceRecorder


@dataclass(frozen=True)
class MemoryCompactionOutcome:
    """Observable result of an optional post-commit compaction attempt.

    ``mode`` distinguishes the synchronous path ("sync") from the
    background worker: "background_scheduled" means the slow call left the
    gameplay thread and its digest will be applied by a later poll.
    """

    attempted: bool = False
    success: bool = False
    trigger: str | None = None
    through_turn: int | None = None
    error: str | None = None
    mode: str = "sync"

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


def _plan_compaction(
    session: GameSession,
    recorder: TraceRecorder,
) -> tuple[
    MemoryCompactionPlan | None,
    tuple[str, ...],
    MemoryCompactionOutcome | None,
]:
    """Plan one bounded batch; a planning failure becomes an error outcome."""
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
        return None, (), MemoryCompactionOutcome(attempted=True, error=error)
    return plan, blocked_modules, None


def _compaction_trace_payload(
    session: GameSession,
    plan: MemoryCompactionPlan,
    blocked_modules: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "turn": session.turn_no,
        "state_revision": session.state_revision,
        "trigger": plan.trigger,
        "through_turn": plan.through_turn,
        "event_turns": [event.turn_no for event in plan.events],
        "bookkeeping_blocked_modules": list(blocked_modules),
    }


def _build_compaction_request(
    session: GameSession,
    plan: MemoryCompactionPlan,
) -> MemoryCompactionRequest:
    """Snapshot one immutable request; safe to hand to another thread."""
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
    return MemoryCompactionRequest(
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


def _record_compaction_failure(
    session: GameSession,
    recorder: TraceRecorder,
    plan: MemoryCompactionPlan,
    common: dict[str, Any],
    exc: Exception,
) -> MemoryCompactionOutcome:
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


def _finish_compaction(
    session: GameSession,
    recorder: TraceRecorder,
    plan: MemoryCompactionPlan,
    common: dict[str, Any],
    response: MemoryCompactionResponse,
) -> MemoryCompactionOutcome:
    """Validate the boundary and apply the digest on the caller's thread."""
    try:
        if response.digest.compacted_through_turn != plan.through_turn:
            raise ValueError(
                "memory provider returned an unexpected compaction boundary"
            )
        session.apply_memory_digest(response.digest)
    except Exception as exc:
        return _record_compaction_failure(session, recorder, plan, common, exc)

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


def maybe_compact_memory(
    session: GameSession,
    provider: LLMProvider,
    recorder: TraceRecorder,
) -> MemoryCompactionOutcome:
    """Compact an eligible old-event batch without endangering its commit."""
    plan, blocked_modules, planning_error = _plan_compaction(session, recorder)
    if planning_error is not None:
        return planning_error
    if plan is None:
        return MemoryCompactionOutcome()

    common = _compaction_trace_payload(session, plan, blocked_modules)
    try:
        request = _build_compaction_request(session, plan)
        _record_safely(recorder, "memory_compaction_requested", common)
        response = provider.compact_memory(request)
    except Exception as exc:
        return _record_compaction_failure(session, recorder, plan, common, exc)
    return _finish_compaction(session, recorder, plan, common, response)


class _PendingCompaction:
    """One in-flight background call plus everything needed to apply it."""

    def __init__(
        self,
        plan: MemoryCompactionPlan,
        common: dict[str, Any],
        branch_id: str,
    ) -> None:
        self.plan = plan
        self.common = common
        self.branch_id = branch_id
        self.done = threading.Event()
        self.response: MemoryCompactionResponse | None = None
        self.error: Exception | None = None


class BackgroundMemoryCompactor:
    """Run the slow compaction call off the gameplay thread.

    ``kick`` and ``poll`` must both be called from the gameplay thread:
    kick plans the batch and snapshots one immutable request, the worker
    thread only talks to the provider, and poll applies the finished digest
    at the next safe point. The session object is never touched off-thread,
    so all gameplay semantics stay single-threaded. At most one call is in
    flight; while it runs, further kicks are no-ops and the batch simply
    waits for a later turn, exactly like a skipped synchronous attempt.
    """

    def __init__(self, provider: LLMProvider, recorder: TraceRecorder) -> None:
        self._provider = provider
        self._recorder = recorder
        self._pending: _PendingCompaction | None = None

    @property
    def busy(self) -> bool:
        return self._pending is not None and not self._pending.done.is_set()

    def kick(self, session: GameSession) -> MemoryCompactionOutcome:
        """Plan and launch one background batch if due; never blocks."""
        if self._pending is not None:
            if not self._pending.done.is_set():
                return MemoryCompactionOutcome(mode="background_busy")
            # 上一个结果已完成但尚未应用；先应用它，再考虑新批次。
            self.poll(session)
        plan, blocked_modules, planning_error = _plan_compaction(
            session, self._recorder
        )
        if planning_error is not None:
            return replace(planning_error, mode="background")
        if plan is None:
            return MemoryCompactionOutcome(mode="background")

        common = {
            **_compaction_trace_payload(session, plan, blocked_modules),
            "mode": "background",
        }
        try:
            request = _build_compaction_request(session, plan)
        except Exception as exc:
            return replace(
                _record_compaction_failure(
                    session, self._recorder, plan, common, exc
                ),
                mode="background",
            )
        _record_safely(self._recorder, "memory_compaction_requested", common)
        pending = _PendingCompaction(
            plan=plan,
            common=common,
            branch_id=session.current_branch_id,
        )
        self._pending = pending
        thread = threading.Thread(
            target=self._run,
            args=(pending, request),
            name="memory-compaction",
            daemon=True,
        )
        thread.start()
        return MemoryCompactionOutcome(
            attempted=True,
            trigger=plan.trigger,
            through_turn=plan.through_turn,
            mode="background_scheduled",
        )

    def _run(
        self,
        pending: _PendingCompaction,
        request: MemoryCompactionRequest,
    ) -> None:
        """Worker body: provider call only; no session or recorder access."""
        try:
            pending.response = self._provider.compact_memory(request)
        except Exception as exc:
            pending.error = exc
        finally:
            pending.done.set()

    def poll(self, session: GameSession) -> MemoryCompactionOutcome | None:
        """Apply a finished result on the caller's thread; never blocks."""
        pending = self._pending
        if pending is None or not pending.done.is_set():
            return None
        self._pending = None
        common = {**pending.common, "applied_turn": session.turn_no}
        if session.current_branch_id != pending.branch_id:
            # 撤回/恢复总是切换分支；跨分支结果直接丢弃，不记失败冷却。
            _record_safely(self._recorder, "memory_compaction_stale", {
                **common,
                "reason": "branch_changed",
                "kick_branch_id": pending.branch_id,
                "current_branch_id": session.current_branch_id,
            })
            return MemoryCompactionOutcome(
                attempted=True,
                trigger=pending.plan.trigger,
                through_turn=pending.plan.through_turn,
                error="stale_branch",
                mode="background_stale",
            )
        if pending.error is not None:
            return replace(
                _record_compaction_failure(
                    session, self._recorder, pending.plan, common, pending.error
                ),
                mode="background",
            )
        return replace(
            _finish_compaction(
                session, self._recorder, pending.plan, common, pending.response
            ),
            mode="background",
        )

    def flush(
        self,
        session: GameSession,
        timeout: float | None = None,
    ) -> MemoryCompactionOutcome | None:
        """Wait for the in-flight call, then apply it (tests and shutdown)."""
        pending = self._pending
        if pending is None:
            return None
        pending.done.wait(timeout)
        return self.poll(session)
