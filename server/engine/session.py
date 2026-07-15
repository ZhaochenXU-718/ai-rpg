"""Narrative-first session state, atomic commits and retained branches."""

from __future__ import annotations

import copy
import random
import uuid
from dataclasses import dataclass
from typing import Any

from .conditions import evaluate_endings
from .content import Story
from .director import run_director_cycle
from .llm import LLMProvider
from .llm_protocol import (
    CommittedChange,
    CommittedTurn,
    FactAuthority,
    FactBatch,
    PerceptionAudience,
    PerceptionSnapshot,
)
from .logger import TurnLogger
from .perception import build_player_perception, build_subject_perception
from .resolver import TurnResult, commit_facts
from .state import build_initial_state

RECENT_EVENT_WINDOW = 5


def _fact_authority(source: str) -> FactAuthority:
    if source.startswith("anchor."):
        return FactAuthority.CANON_ANCHOR
    if source.startswith("local_canon."):
        return FactAuthority.LOCAL_CANON
    return FactAuthority.IRON_LAW


def _committed_changes(result: TurnResult) -> tuple[CommittedChange, ...]:
    return tuple(
        CommittedChange(
            path=path,
            previous=previous,
            new=new,
            authority=_fact_authority(source),
            source=source,
            reason=f"accepted by {source}",
        )
        for (path, previous, new), source in zip(
            result.changes, result.change_sources
        )
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
    consumed: frozenset[str]
    ending: str | None
    last_result: TurnResult | None
    recent_events: tuple[str, ...]
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
        self.consumed: set[str] = set()
        self.turn_no = 0
        self.ending: str | None = None
        self.state_revision = 0
        self.last_result: TurnResult | None = None
        self._recent_events: list[str] = []
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
    def is_over(self) -> bool:
        return self.ending is not None

    @property
    def recent_events(self) -> tuple[str, ...]:
        return tuple(self._recent_events)

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
            recent_events=self.recent_events,
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
        self.consumed = set(checkpoint.consumed)
        self.turn_no = checkpoint.turn_no
        self.ending = checkpoint.ending
        self.last_result = copy.deepcopy(checkpoint.last_result)
        self._recent_events = list(checkpoint.recent_events)
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
            consumed=frozenset(self.consumed),
            ending=self.ending,
            last_result=copy.deepcopy(self.last_result),
            recent_events=self.recent_events,
            rng_state=self.rng.getstate(),
        )
        self._checkpoints[checkpoint_id] = checkpoint
        self._current_checkpoint_id = checkpoint_id
        self._branch_heads[self._current_branch_id] = checkpoint_id
        return checkpoint

    def commit_narrative(
        self,
        player_text: str,
        narrative: str,
        *,
        references: tuple[str, ...] = (),
        director_provider: LLMProvider | None = None,
    ) -> TurnResult:
        """Low-level prose-only commit for anchors/tests; the CLI uses Phase 2."""
        return self.commit_fact_batch(
            FactBatch(
                state_revision=self.state_revision,
                player_text=player_text,
                narrative=narrative,
                references=references,
            ),
            director_provider=director_provider,
        )

    def commit_fact_batch(
        self,
        batch: FactBatch,
        *,
        director_provider: LLMProvider | None = None,
    ) -> TurnResult:
        if self.is_over:
            raise SessionError("session is over")
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
        working_consumed = set(self.consumed)
        result = commit_facts(
            self.story,
            working_state,
            working_consumed,
            next_turn,
            batch,
        )
        if director_provider is not None:
            boundaries = tuple(
                str(rule)
                for rule in (
                    list((self.story.data.get("player_role") or {}).get("constraints") or [])
                    + list((self.story.data.get("global_rules") or {}).get("boundaries") or [])
                )
            )
            run_director_cycle(
                self.story,
                working_state,
                result,
                director_provider,
                state_revision=self.state_revision + 1,
                player_action=batch.player_text,
                boundaries=boundaries,
            )
            result.ending = evaluate_endings(working_state, self.story.endings)
            result.scene_after = self.story.current_location(working_state)

        result.prior_events = list(self._recent_events)
        self.state = working_state
        self.consumed = working_consumed
        self.turn_no = next_turn
        self.ending = result.ending
        self.state_revision = revision_before + 1
        result.committed_turn = CommittedTurn(
            batch_id=batch.batch_id,
            turn_no=next_turn,
            state_revision_before=revision_before,
            state_revision_after=self.state_revision,
            scene_before=result.scene_before,
            scene_after=result.scene_after,
            narrative=result.narrative,
            committed_changes=_committed_changes(result),
            director_beats=tuple(result.director_beats),
            local_canon=tuple(result.local_canon),
            anchor_ids=tuple(result.fired),
            new_facts=tuple(result.new_facts),
            ending=result.ending,
        )
        self.last_result = result
        new_events = [result.narrative, *result.narrative_hints]
        self._recent_events.extend(event for event in new_events if event)
        del self._recent_events[:-RECENT_EVENT_WINDOW]
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
            "new_facts": list(result.new_facts),
            "anchors": list(result.fired),
            "commit_id": result.committed_turn.commit_id,
            "extracted_fact_ids": [
                fact.fact_id for fact in batch.extracted_facts
            ],
        })
        return result
