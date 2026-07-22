"""Branch-scoped raw events and non-authoritative narrative digests."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, replace
from typing import Any

from .llm_protocol import MemoryContext, MemoryContextEvent


RECENT_MEMORY_WINDOW = 4
COMPACTION_BATCH_SIZE = 4
COMPACTION_CHAR_BUDGET = 2400
MAX_COMPACTION_EVENTS = 6
COMPACTION_RETRY_TURNS = 3
MEMORY_CONTEXT_CHAR_BUDGET = 4800
MEMORY_CONTEXT_EVENT_LIMIT = 8
MEMORY_CONTEXT_PLAYER_TEXT_LIMIT = 80
MEMORY_CONTEXT_NARRATIVE_LIMIT = 280
MEMORY_CONTEXT_SUMMARY_LIMIT = 900
MEMORY_CONTEXT_NOTE_LIMIT = 220


@dataclass(frozen=True)
class MemoryChange:
    """One physical change copied into a narrative event for context."""

    path: str
    previous: Any
    new: Any


@dataclass(frozen=True)
class MemoryNoteGroup:
    """Soft notes scoped to one authored character or scene."""

    subject_id: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModuleRecord:
    """Soft lifecycle state of one authored story module.

    Never authoritative: it gates which modules the orchestrator may offer
    and is advanced by code (offered/faded) or by M2 compaction
    (engaged/resolved/dropped).
    """

    module_id: str
    status: str = "unseen"
    offers_count: int = 0
    last_offered_turn: int = 0


@dataclass(frozen=True)
class MemoryDigest:
    """Model-produced soft context derived from committed events.

    ``module_updates`` are transient transition commands (module_id, status)
    parsed from one compaction call; they are applied through the module
    transition rules and never stored verbatim.
    """

    compacted_through_turn: int
    rolling_summary: str
    open_loops: tuple[str, ...] = ()
    character_notes: tuple[MemoryNoteGroup, ...] = ()
    scene_notes: tuple[MemoryNoteGroup, ...] = ()
    recently_resolved: tuple[str, ...] = ()
    module_updates: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryEvent:
    """Exact, deterministic record of one successfully committed turn."""

    turn_no: int
    commit_id: str
    player_text: str
    narrative: str
    scene_before: str
    scene_after: str
    participants: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    physical_changes: tuple[MemoryChange, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryState:
    """Soft memory snapshot stored alongside every session checkpoint."""

    events: tuple[MemoryEvent, ...] = ()
    compacted_through_turn: int = 0
    rolling_summary: str = ""
    open_loops: tuple[str, ...] = ()
    character_notes: tuple[MemoryNoteGroup, ...] = ()
    scene_notes: tuple[MemoryNoteGroup, ...] = ()
    recently_resolved: tuple[str, ...] = ()
    module_states: tuple[ModuleRecord, ...] = ()
    last_compaction_attempt_turn: int = 0
    last_compaction_error: str = ""

    def append(self, event: MemoryEvent) -> "MemoryState":
        if self.events and event.turn_no <= self.events[-1].turn_no:
            raise ValueError("memory events must advance turn_no")
        return replace(self, events=(*self.events, copy.deepcopy(event)))

    @property
    def recent_events(self) -> tuple[MemoryEvent, ...]:
        return self.events[-RECENT_MEMORY_WINDOW:]

    @property
    def recent_narratives(self) -> tuple[str, ...]:
        return tuple(event.narrative for event in self.recent_events)

    @property
    def digest(self) -> MemoryDigest:
        return MemoryDigest(
            compacted_through_turn=self.compacted_through_turn,
            rolling_summary=self.rolling_summary,
            open_loops=self.open_loops,
            character_notes=self.character_notes,
            scene_notes=self.scene_notes,
            recently_resolved=self.recently_resolved,
        )

    def apply_digest(
        self,
        digest: MemoryDigest,
        *,
        attempt_turn: int,
    ) -> "MemoryState":
        event_turns = {event.turn_no for event in self.events}
        if digest.compacted_through_turn not in event_turns:
            raise ValueError("memory digest boundary is not a committed turn")
        if digest.compacted_through_turn <= self.compacted_through_turn:
            raise ValueError("memory digest must advance compacted_through_turn")
        return replace(
            self,
            compacted_through_turn=digest.compacted_through_turn,
            rolling_summary=digest.rolling_summary,
            open_loops=digest.open_loops,
            character_notes=digest.character_notes,
            scene_notes=digest.scene_notes,
            recently_resolved=digest.recently_resolved,
            last_compaction_attempt_turn=attempt_turn,
            last_compaction_error="",
        )

    def module_record(self, module_id: str) -> ModuleRecord:
        for record in self.module_states:
            if record.module_id == module_id:
                return record
        return ModuleRecord(module_id=module_id)

    def with_module_states(
        self,
        records: tuple[ModuleRecord, ...],
    ) -> "MemoryState":
        """Replace module lifecycle records; transition policy lives elsewhere."""
        seen = [record.module_id for record in records]
        if len(seen) != len(set(seen)):
            raise ValueError("module states must not contain duplicates")
        return replace(self, module_states=tuple(records))

    def record_compaction_failure(
        self,
        *,
        attempt_turn: int,
        error: str,
    ) -> "MemoryState":
        return replace(
            self,
            last_compaction_attempt_turn=attempt_turn,
            last_compaction_error=str(error).strip() or "unknown error",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryCompactionPlan:
    """A bounded batch of old events eligible for one summary call."""

    events: tuple[MemoryEvent, ...]
    through_turn: int
    trigger: str


class _ContextBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.used = 0
        self.truncated = False

    def take(self, value: str, *, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            return ""
        allowed = min(limit, self.remaining)
        if allowed <= 0:
            self.truncated = True
            return ""
        selected = text[:allowed].rstrip()
        if len(selected) < len(text):
            self.truncated = True
        self.remaining -= len(selected)
        self.used += len(selected)
        return selected


def build_memory_context(
    memory: MemoryState,
    *,
    subject_id: str,
    turn_no: int,
    state_revision: int,
) -> MemoryContext:
    """Build one bounded prompt view without mutating or trusting the digest."""
    budget = _ContextBudget(MEMORY_CONTEXT_CHAR_BUDGET)
    pending = tuple(
        event
        for event in memory.events
        if event.turn_no > memory.compacted_through_turn
    )
    if len(pending) > MEMORY_CONTEXT_EVENT_LIMIT:
        budget.truncated = True
        pending = pending[-MEMORY_CONTEXT_EVENT_LIMIT:]

    context_events: list[MemoryContextEvent] = []
    for event in pending:
        player_text = budget.take(
            event.player_text,
            limit=MEMORY_CONTEXT_PLAYER_TEXT_LIMIT,
        )
        narrative = budget.take(
            event.narrative,
            limit=MEMORY_CONTEXT_NARRATIVE_LIMIT,
        )
        if player_text and narrative:
            context_events.append(MemoryContextEvent(
                turn_no=event.turn_no,
                player_text=player_text,
                narrative=narrative,
                scene_before=event.scene_before,
                scene_after=event.scene_after,
            ))

    rolling_summary = budget.take(
        memory.rolling_summary,
        limit=MEMORY_CONTEXT_SUMMARY_LIMIT,
    )

    def take_items(values: tuple[str, ...], *, limit: int) -> tuple[str, ...]:
        if len(values) > limit:
            budget.truncated = True
        selected: list[str] = []
        for value in values[:limit]:
            text = budget.take(value, limit=MEMORY_CONTEXT_NOTE_LIMIT)
            if text:
                selected.append(text)
        return tuple(selected)

    open_loops = take_items(memory.open_loops, limit=6)

    def take_groups(
        groups: tuple[MemoryNoteGroup, ...],
        *,
        group_limit: int,
    ) -> dict[str, tuple[str, ...]]:
        if len(groups) > group_limit:
            budget.truncated = True
        selected: dict[str, tuple[str, ...]] = {}
        for group in groups[:group_limit]:
            notes = take_items(group.notes, limit=2)
            if notes:
                selected[str(group.subject_id)] = notes
        return selected

    character_notes = take_groups(memory.character_notes, group_limit=6)
    scene_notes = take_groups(memory.scene_notes, group_limit=4)
    recently_resolved = take_items(memory.recently_resolved, limit=4)

    return MemoryContext(
        subject_id=subject_id,
        turn_no=turn_no,
        state_revision=state_revision,
        compacted_through_turn=memory.compacted_through_turn,
        rolling_summary=rolling_summary,
        open_loops=open_loops,
        character_notes=character_notes,
        scene_notes=scene_notes,
        recently_resolved=recently_resolved,
        uncompacted_events=tuple(context_events),
        text_chars=budget.used,
        truncated=budget.truncated,
    )


def plan_memory_compaction(
    memory: MemoryState,
    *,
    urgent: bool = False,
) -> MemoryCompactionPlan | None:
    """Return one bounded compaction batch, or ``None`` when not due.

    ``urgent`` lets module bookkeeping bypass the batch-size threshold when
    a requires-gated module is waiting on an M2 status judgement. The recent
    protection window and the retry cooldown still apply unchanged.
    """
    if len(memory.events) <= RECENT_MEMORY_WINDOW:
        return None
    latest_turn = memory.events[-1].turn_no
    if (
        memory.last_compaction_attempt_turn
        and latest_turn - memory.last_compaction_attempt_turn
        < COMPACTION_RETRY_TURNS
    ):
        return None

    eligible = tuple(
        event
        for event in memory.events[:-RECENT_MEMORY_WINDOW]
        if event.turn_no > memory.compacted_through_turn
    )
    if not eligible:
        return None
    character_count = sum(
        len(event.player_text) + len(event.narrative)
        for event in eligible
    )
    if len(eligible) >= COMPACTION_BATCH_SIZE:
        trigger = "event_window"
    elif character_count >= COMPACTION_CHAR_BUDGET:
        trigger = "character_budget"
    elif urgent:
        trigger = "module_bookkeeping"
    else:
        return None

    selected = eligible[:MAX_COMPACTION_EVENTS]
    return MemoryCompactionPlan(
        events=selected,
        through_turn=selected[-1].turn_no,
        trigger=trigger,
    )
