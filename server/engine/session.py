"""Narrative-first session state, atomic commits and retained branches."""

from __future__ import annotations

import copy
import random
import uuid
from dataclasses import dataclass, replace
from typing import Any

from .content import Story
from .llm_protocol import (
    CommittedChange,
    CommittedTurn,
    FactBatch,
    MemoryContext,
    PerceptionAudience,
    PerceptionSnapshot,
)
from .logger import TurnLogger
from .memory import (
    MemoryChange,
    MemoryDigest,
    MemoryEvent,
    MemoryState,
    build_memory_context,
)
from .perception import build_player_perception, build_subject_perception
from .resolver import TurnResult, commit_facts
from .state import build_initial_state


def _committed_changes(result: TurnResult) -> tuple[CommittedChange, ...]:
    return tuple(
        CommittedChange(
            path=path,
            previous=previous,
            new=new,
        )
        for path, previous, new in result.changes
        if previous != new
    )


@dataclass(frozen=True)
class SessionCheckpoint:
    checkpoint_id: str
    parent_id: str | None
    branch_id: str
    state_revision: int
    turn_no: int
    state: dict[str, Any]
    memory: MemoryState
    last_result: TurnResult | None
    rng_state: object


@dataclass(frozen=True)
class SessionBranch:
    branch_id: str
    fork_checkpoint_id: str
    head_checkpoint_id: str


@dataclass(frozen=True)
class StateRestore:
    source_checkpoint_id: str
    restored_checkpoint_id: str
    branch_id: str
    state_revision: int
    turn_no: int


class SessionError(Exception):
    pass


class GameSession:
    def __init__(self, story: Story, log_dir: str | None = "data/sessions") -> None:
        if story.data.get("content_profile") != "narrative_first":
            raise SessionError("only narrative_first content can start a new session")
        self.story = story
        self.session_id = uuid.uuid4().hex[:12]
        self.state = build_initial_state(story.data)
        self.turn_no = 0
        self.state_revision = 0
        self.last_result: TurnResult | None = None
        self.memory = MemoryState()
        self.rng = random.Random()

        self._checkpoint_seq = 0
        self._branch_seq = 0
        self._checkpoints: dict[str, SessionCheckpoint] = {}
        self._branch_forks: dict[str, str] = {}
        self._branch_heads: dict[str, str] = {}
        self._current_branch_id = "main"
        self._current_checkpoint_id = ""
        root = self._append_checkpoint(parent_id=None)
        self._branch_forks["main"] = root.checkpoint_id

        self._logger = TurnLogger(log_dir, self.session_id)
        self._logger.log({
            "event": "session_start",
            "story": story.id,
            "session": self.session_id,
            "checkpoint_id": root.checkpoint_id,
            "branch_id": self._current_branch_id,
        })

    @property
    def recent_events(self) -> tuple[str, ...]:
        """Compatibility view retained on the perception protocol."""
        return self.memory.recent_narratives

    def memory_context(
        self,
        subject_id: str | None = None,
    ) -> MemoryContext | None:
        """Return player-owned soft context; NPC memory remains isolated."""
        subject_id = subject_id or self.story.player_id
        if subject_id != self.story.player_id:
            if subject_id not in self.story.characters:
                raise SessionError(f"unknown memory subject '{subject_id}'")
            return None
        try:
            return build_memory_context(
                self.memory,
                subject_id=subject_id,
                turn_no=self.turn_no,
                state_revision=self.state_revision,
            )
        except (TypeError, ValueError):
            # A malformed soft digest must not block generation. Raw events are
            # retained separately and provide the last-resort context.
            raw_only = MemoryState(events=self.memory.events)
            try:
                return build_memory_context(
                    raw_only,
                    subject_id=subject_id,
                    turn_no=self.turn_no,
                    state_revision=self.state_revision,
                )
            except (TypeError, ValueError):
                return None

    @property
    def current_checkpoint_id(self) -> str:
        return self._current_checkpoint_id

    @property
    def current_branch_id(self) -> str:
        return self._current_branch_id

    def perception(self) -> PerceptionSnapshot:
        return build_player_perception(
            self.story,
            self.state,
            session_id=self.session_id,
            turn_no=self.turn_no,
            state_revision=self.state_revision,
            recent_events=self.recent_events,
        )

    def subject_perception(
        self,
        subject_id: str,
        *,
        known_facts: tuple[str, ...] = (),
        subject_context: dict[str, Any] | None = None,
    ) -> PerceptionSnapshot:
        """Build an NPC-scoped snapshot without exposing it to the player."""
        if subject_id == self.story.player_id:
            return self.perception()
        if subject_id not in self.story.characters:
            raise SessionError(f"unknown perception subject '{subject_id}'")
        return build_subject_perception(
            self.story,
            self.state,
            audience=PerceptionAudience.NPC,
            subject_id=subject_id,
            session_id=self.session_id,
            turn_no=self.turn_no,
            state_revision=self.state_revision,
            recent_events=(),
            known_facts=known_facts,
            subject_context=subject_context,
        )

    def get_checkpoint(self, checkpoint_id: str) -> SessionCheckpoint:
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is None:
            raise SessionError(f"unknown checkpoint '{checkpoint_id}'")
        return copy.deepcopy(checkpoint)

    def checkpoint_history(self) -> tuple[SessionCheckpoint, ...]:
        history: list[SessionCheckpoint] = []
        checkpoint_id: str | None = self._current_checkpoint_id
        while checkpoint_id is not None:
            checkpoint = self._checkpoints[checkpoint_id]
            history.append(copy.deepcopy(checkpoint))
            checkpoint_id = checkpoint.parent_id
        history.reverse()
        return tuple(history)

    def branches(self) -> tuple[SessionBranch, ...]:
        return tuple(
            SessionBranch(
                branch_id=branch_id,
                fork_checkpoint_id=self._branch_forks[branch_id],
                head_checkpoint_id=head,
            )
            for branch_id, head in self._branch_heads.items()
        )

    def undo(self) -> StateRestore:
        current = self._checkpoints[self._current_checkpoint_id]
        if current.parent_id is None:
            raise SessionError("已经在故事起点，没有更早的提交可以撤回")
        return self.restore_checkpoint(current.parent_id)

    def restore_checkpoint(self, checkpoint_id: str) -> StateRestore:
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is None:
            raise SessionError(f"unknown checkpoint '{checkpoint_id}'")
        source_checkpoint_id = self._current_checkpoint_id
        if checkpoint_id == source_checkpoint_id:
            raise SessionError("已经位于该 checkpoint")

        self.state = copy.deepcopy(checkpoint.state)
        self.memory = copy.deepcopy(checkpoint.memory)
        self.turn_no = checkpoint.turn_no
        self.last_result = copy.deepcopy(checkpoint.last_result)
        self.rng.setstate(checkpoint.rng_state)
        self.state_revision += 1

        self._branch_seq += 1
        branch_id = f"branch_{self._branch_seq}"
        self._current_branch_id = branch_id
        self._current_checkpoint_id = checkpoint_id
        self._branch_forks[branch_id] = checkpoint_id
        self._branch_heads[branch_id] = checkpoint_id
        self._logger.log({
            "event": "checkpoint_restored",
            "source_checkpoint_id": source_checkpoint_id,
            "restored_checkpoint_id": checkpoint_id,
            "branch_id": branch_id,
            "state_revision": self.state_revision,
            "turn": self.turn_no,
        })
        return StateRestore(
            source_checkpoint_id=source_checkpoint_id,
            restored_checkpoint_id=checkpoint_id,
            branch_id=branch_id,
            state_revision=self.state_revision,
            turn_no=self.turn_no,
        )

    def _append_checkpoint(self, parent_id: str | None) -> SessionCheckpoint:
        checkpoint_id = f"cp_{self._checkpoint_seq}"
        self._checkpoint_seq += 1
        checkpoint = SessionCheckpoint(
            checkpoint_id=checkpoint_id,
            parent_id=parent_id,
            branch_id=self._current_branch_id,
            state_revision=self.state_revision,
            turn_no=self.turn_no,
            state=copy.deepcopy(self.state),
            memory=copy.deepcopy(self.memory),
            last_result=copy.deepcopy(self.last_result),
            rng_state=self.rng.getstate(),
        )
        self._checkpoints[checkpoint_id] = checkpoint
        self._current_checkpoint_id = checkpoint_id
        self._branch_heads[self._current_branch_id] = checkpoint_id
        return checkpoint

    def _replace_current_checkpoint_memory(self) -> None:
        """Attach a soft-memory update to the current committed checkpoint."""
        checkpoint = self._checkpoints[self._current_checkpoint_id]
        self._checkpoints[self._current_checkpoint_id] = replace(
            checkpoint,
            memory=copy.deepcopy(self.memory),
        )

    def apply_memory_digest(self, digest: MemoryDigest) -> None:
        """Store a best-effort digest without advancing physical revision."""
        self.memory = self.memory.apply_digest(
            digest,
            attempt_turn=self.turn_no,
        )
        self._replace_current_checkpoint_memory()
        try:
            self._logger.log({
                "event": "memory_compacted",
                "turn": self.turn_no,
                "state_revision": self.state_revision,
                "checkpoint_id": self.current_checkpoint_id,
                "branch_id": self.current_branch_id,
                "digest": digest.to_dict(),
            })
        except Exception:
            pass

    def record_memory_compaction_failure(self, error: str) -> None:
        """Persist retry cooldown metadata while retaining every raw event."""
        self.memory = self.memory.record_compaction_failure(
            attempt_turn=self.turn_no,
            error=error,
        )
        self._replace_current_checkpoint_memory()
        try:
            self._logger.log({
                "event": "memory_compaction_failed",
                "turn": self.turn_no,
                "state_revision": self.state_revision,
                "checkpoint_id": self.current_checkpoint_id,
                "branch_id": self.current_branch_id,
                "error": self.memory.last_compaction_error,
            })
        except Exception:
            pass

    def commit_narrative(
        self,
        player_text: str,
        narrative: str,
        *,
        references: tuple[str, ...] = (),
    ) -> TurnResult:
        """Low-level prose-only commit used by tests; the CLI uses Phase 2."""
        return self.commit_fact_batch(
            FactBatch(
                state_revision=self.state_revision,
                player_text=player_text,
                narrative=narrative,
                references=references,
            ),
        )

    def commit_fact_batch(
        self,
        batch: FactBatch,
    ) -> TurnResult:
        if not batch.player_text.strip():
            raise SessionError("player text must not be empty")
        if batch.state_revision != self.state_revision:
            raise SessionError(
                f"fact batch is stale: revision {batch.state_revision}, "
                f"current {self.state_revision}"
            )

        revision_before = self.state_revision
        next_turn = self.turn_no + 1
        working_state = copy.deepcopy(self.state)
        result = commit_facts(
            self.story,
            working_state,
            next_turn,
            batch,
        )
        result.committed_turn = CommittedTurn(
            batch_id=batch.batch_id,
            turn_no=next_turn,
            state_revision_before=revision_before,
            state_revision_after=revision_before + 1,
            scene_before=result.scene_before,
            scene_after=result.scene_after,
            narrative=result.narrative,
            committed_changes=_committed_changes(result),
        )
        positions_before = self.state.get("positions") or {}
        positions_after = working_state.get("positions") or {}
        participants = tuple(
            character_id
            for character_id in self.story.characters
            if (
                positions_before.get(character_id) == result.scene_before
                or positions_after.get(character_id) == result.scene_after
            )
        )
        memory_event = MemoryEvent(
            turn_no=next_turn,
            commit_id=result.committed_turn.commit_id,
            player_text=batch.player_text,
            narrative=result.narrative,
            scene_before=result.scene_before,
            scene_after=result.scene_after,
            participants=participants,
            references=tuple(batch.references),
            physical_changes=tuple(
                MemoryChange(
                    path=path,
                    previous=copy.deepcopy(previous),
                    new=copy.deepcopy(new),
                )
                for path, previous, new in result.changes
            ),
        )
        working_memory = self.memory.append(memory_event)

        self.state = working_state
        self.turn_no = next_turn
        self.state_revision = revision_before + 1
        self.last_result = result
        self.memory = working_memory
        checkpoint = self._append_checkpoint(parent_id=self._current_checkpoint_id)
        self._logger.log({
            "event": "fact_batch_committed",
            "turn": self.turn_no,
            "revision_before": revision_before,
            "state_revision": self.state_revision,
            "checkpoint_id": checkpoint.checkpoint_id,
            "parent_checkpoint_id": checkpoint.parent_id,
            "branch_id": checkpoint.branch_id,
            "player_text": batch.player_text,
            "committed_paths": [path for path, _, _ in result.changes],
            "commit_id": result.committed_turn.commit_id,
            "memory_event": memory_event.to_dict(),
            "extracted_fact_ids": [
                fact.fact_id for fact in batch.extracted_facts
            ],
        })
        return result
