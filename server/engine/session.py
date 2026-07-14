"""Game session: action resolution, optional Director cycle and state timeline.

This is the object the CLI drives now and the HTTP API will wrap in stage 4.
Quote contract (plan section 4.4): low-risk intents resolve directly; others
must present a quote_id from this turn; requotes are free but capped.
"""

from __future__ import annotations

import copy
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .capability_effects import CapabilityResolution
from .capabilities import build_committed_outcome, validate_plan
from .conditions import evaluate_endings
from .content import Story
from .director import current_goal, goal_achieved, run_director_cycle
from .effects import TemporaryEffects
from .llm import LLMProvider
from .llm_protocol import ActionPlan, CommittedOutcome, ValidationResult
from .logger import TurnLogger
from .limits import clamp_generic_patch
from .perception import build_player_perception
from .quote import MAX_REQUOTES_PER_TURN, build_quote, default_proposal
from .resolver import TurnResult, run_turn, validate_action
from .state import build_initial_state

RECENT_EVENT_WINDOW = 5


@dataclass(frozen=True)
class SessionCheckpoint:
    """Immutable-in-practice snapshot behind one committed timeline node."""

    checkpoint_id: str
    parent_id: str | None
    branch_id: str
    state_revision: int
    turn_no: int
    state: dict[str, Any]
    temporary_effects: tuple[dict[str, Any], ...]
    consumed: frozenset[str]
    ending: str | None
    last_result: TurnResult | None
    recent_events: tuple[str, ...]
    rng_state: object


@dataclass(frozen=True)
class SessionBranch:
    """One retained line of history; branches share immutable ancestors."""

    branch_id: str
    fork_checkpoint_id: str
    head_checkpoint_id: str


@dataclass(frozen=True)
class StateRestore:
    """Receipt for restoring a checkpoint into a newly created branch."""

    source_checkpoint_id: str
    restored_checkpoint_id: str
    branch_id: str
    state_revision: int
    turn_no: int


class SessionError(Exception):
    pass


class GameSession:
    def __init__(self, story: Story, log_dir: str | None = "data/sessions") -> None:
        self.story = story
        self.session_id = uuid.uuid4().hex[:12]
        self.state = build_initial_state(story.data)
        self.temporaries = TemporaryEffects()
        self.consumed: set[str] = set()
        self.turn_no = 0
        self.ending: str | None = None
        # Committed-turn counter; ActionPlans must be validated against it.
        self.state_revision = 0
        self.last_result: TurnResult | None = None
        # Unanswered clarification from the understanding loop: the next
        # free-text input is read as a reply to it (cleared on commit).
        self.pending_clarification: dict[str, Any] | None = None
        self._recent_events: list[str] = []
        self._requotes = 0
        self._pending_quotes: dict[str, dict[str, Any]] = {}
        self._plan_payloads: dict[str, dict[str, Any]] = {}
        # Future stochastic capability modules must use this source so its
        # state participates in checkpoints and deterministic replay.
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

    def get_checkpoint(self, checkpoint_id: str) -> SessionCheckpoint:
        """Return an isolated copy so callers cannot mutate retained history."""
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is None:
            raise SessionError(f"unknown checkpoint '{checkpoint_id}'")
        return copy.deepcopy(checkpoint)

    def checkpoint_history(self) -> tuple[SessionCheckpoint, ...]:
        """Return the root-to-current ancestry for the active branch."""
        history: list[SessionCheckpoint] = []
        checkpoint_id: str | None = self._current_checkpoint_id
        while checkpoint_id is not None:
            checkpoint = self._checkpoints[checkpoint_id]
            history.append(copy.deepcopy(checkpoint))
            checkpoint_id = checkpoint.parent_id
        history.reverse()
        return tuple(history)

    def branches(self) -> tuple[SessionBranch, ...]:
        """Return every retained branch and its latest committed checkpoint."""
        return tuple(
            SessionBranch(
                branch_id=branch_id,
                fork_checkpoint_id=self._branch_forks[branch_id],
                head_checkpoint_id=head,
            )
            for branch_id, head in self._branch_heads.items()
        )

    def undo(self) -> StateRestore:
        """Restore the parent commit and fork a new branch from that point."""
        current = self._checkpoints[self._current_checkpoint_id]
        if current.parent_id is None:
            raise SessionError("已经在故事起点，没有更早的行动可以撤回")
        return self.restore_checkpoint(current.parent_id)

    def restore_checkpoint(self, checkpoint_id: str) -> StateRestore:
        """Restore a retained node without erasing history, always forking."""
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is None:
            raise SessionError(f"unknown checkpoint '{checkpoint_id}'")
        source_checkpoint_id = self._current_checkpoint_id
        if checkpoint_id == source_checkpoint_id:
            raise SessionError("已经位于该 checkpoint")

        next_revision = self.state_revision + 1
        self.state = copy.deepcopy(checkpoint.state)
        self.temporaries.restore(checkpoint.temporary_effects)
        self.consumed = set(checkpoint.consumed)
        self.turn_no = checkpoint.turn_no
        self.ending = checkpoint.ending
        self.last_result = copy.deepcopy(checkpoint.last_result)
        self._recent_events = list(checkpoint.recent_events)
        self.rng.setstate(checkpoint.rng_state)
        self.state_revision = next_revision

        self.pending_clarification = None
        self._requotes = 0
        self._pending_quotes.clear()
        self._plan_payloads.clear()

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
            temporary_effects=self.temporaries.snapshot(),
            consumed=frozenset(self.consumed),
            ending=self.ending,
            last_result=copy.deepcopy(self.last_result),
            recent_events=tuple(self._recent_events),
            rng_state=self.rng.getstate(),
        )
        self._checkpoints[checkpoint_id] = checkpoint
        self._current_checkpoint_id = checkpoint_id
        self._branch_heads[self._current_branch_id] = checkpoint_id
        return checkpoint

    def perception(self):
        """The player-visible snapshot (also the LLM understanding input)."""
        return build_player_perception(
            self.story,
            self.state,
            session_id=self.session_id,
            turn_no=self.turn_no,
            state_revision=self.state_revision,
            recent_events=tuple(self._recent_events),
        )

    def validate_plan(self, plan: ActionPlan) -> ValidationResult:
        """Adjudicate an ActionPlan without touching live state."""
        validation, payload = validate_plan(
            self.story,
            self.state,
            self.consumed,
            plan,
            state_revision=self.state_revision,
        )
        if validation.can_execute:
            self._plan_payloads[validation.validation_id] = payload
        self._logger.log({
            "event": "plan_validated",
            "turn": self.turn_no + 1,
            "plan_id": plan.plan_id,
            "validation_id": validation.validation_id,
            "can_execute": validation.can_execute,
            "can_replan": validation.can_replan,
            "issues": [issue.to_dict() for issue in validation.issues],
        })
        return validation

    def plan_payload(self, validation: ValidationResult) -> dict[str, Any] | None:
        """The resolver payload a validated plan would execute (read-only)."""
        payload = self._plan_payloads.get(validation.validation_id)
        return dict(payload) if payload is not None else None

    def resolve_plan(
        self,
        plan: ActionPlan,
        validation: ValidationResult,
        director_provider: LLMProvider | None = None,
    ) -> CommittedOutcome:
        """Commit a validated plan through the deterministic resolver."""
        if self.is_over:
            raise SessionError("session is over")
        if not validation.can_execute:
            raise SessionError("plan was not validated as executable")
        if validation.state_revision != self.state_revision:
            raise SessionError(
                f"validation is stale: revision {validation.state_revision} != {self.state_revision}"
            )
        payload = self._plan_payloads.get(validation.validation_id)
        if payload is None:
            raise SessionError(f"unknown validation '{validation.validation_id}'")
        revision_before = self.state_revision
        result = self.resolve(
            intent_id=payload["intent_id"],
            objects=payload["objects"],
            generic_patch=payload["generic_patch"],
            _allow_quoted=True,
            _allow_unauthored=bool(payload.get("allow_unauthored")),
            _capability_resolution=payload.get("capability_resolution"),
            _director_provider=director_provider,
            _player_action=plan.player_text,
        )
        outcome = build_committed_outcome(
            self.story,
            plan,
            validation,
            result,
            revision_before=revision_before,
            revision_after=self.state_revision,
        )
        self._logger.log({
            "event": "plan_committed",
            "turn": result.turn_no,
            "plan_id": plan.plan_id,
            "outcome_id": outcome.outcome_id,
            "checkpoint_id": self.current_checkpoint_id,
            "branch_id": self.current_branch_id,
            "committed_paths": [change.path for change in outcome.committed_changes],
            "resolution_sources": list(outcome.resolution_sources),
            "primary_goal_status": outcome.primary_goal_status,
            "cost_only": outcome.cost_only,
        })
        return outcome

    def quote(self, intent_id: str, objects: list[str] | None = None, player_text: str = "") -> dict[str, Any]:
        if self.is_over:
            raise SessionError("session is over")
        if intent_id not in self.story.intents:
            raise SessionError(f"unknown intent '{intent_id}'")
        proposal, _ = clamp_generic_patch(
            default_proposal(self.story, self.state, intent_id, objects or []),
            self.state,
            self.story.resolution_limits,
        )
        errors = validate_action(
            self.story,
            self.state,
            intent_id,
            objects or [],
            proposal,
            self.consumed,
        )
        if errors:
            self._logger.log({
                "event": "action_rejected",
                "stage": "quote",
                "turn": self.turn_no + 1,
                "intent": intent_id,
                "objects": objects or [],
                "errors": errors,
            })
            raise SessionError("；".join(errors))
        if self._requotes >= MAX_REQUOTES_PER_TURN:
            raise SessionError(f"本回合报价次数已达上限（{MAX_REQUOTES_PER_TURN}），请执行或换个回合再试。")
        started = time.monotonic()
        quote = build_quote(
            self.story,
            self.state,
            intent_id,
            objects or [],
            player_text,
            self._requotes,
            temporaries=self.temporaries,
            consumed=self.consumed,
            turn_no=self.turn_no + 1,
        )
        self._requotes += 1
        self._pending_quotes[quote["quote_id"]] = quote
        self._logger.log({
            "event": "quote",
            "turn": self.turn_no + 1,
            "intent": intent_id,
            "objects": objects or [],
            "player_text": player_text,
            "requote_count": quote["requote_count"],
            "quote_latency_ms": round((time.monotonic() - started) * 1000, 1),
            "llm_calls": 0,
        })
        return quote

    def resolve(
        self,
        intent_id: str | None = None,
        objects: list[str] | None = None,
        quote_id: str | None = None,
        generic_patch: dict[str, Any] | None = None,
        _allow_quoted: bool = False,
        _allow_unauthored: bool = False,
        _capability_resolution: CapabilityResolution | None = None,
        _director_provider: LLMProvider | None = None,
        _player_action: str = "",
    ) -> TurnResult:
        if self.is_over:
            raise SessionError("session is over")

        if _capability_resolution is not None:
            intent_id = _capability_resolution.capability_id
            objects = list(_capability_resolution.objects)
            generic_patch = {}
        elif quote_id is not None:
            quote = self._pending_quotes.get(quote_id)
            if quote is None:
                raise SessionError(f"unknown or expired quote '{quote_id}'")
            intent_id = quote["intent"]
            objects = quote["objects"]
            # Binding quote: the resolver applies exactly the quoted proposal.
            generic_patch = quote["proposal"]
        elif intent_id is None:
            raise SessionError("resolve needs an intent or a quote_id")
        elif not _allow_quoted and self.story.quote_required(intent_id):
            raise SessionError(f"intent '{intent_id}' requires a quote before resolving")

        errors = []
        if _capability_resolution is None:
            errors = validate_action(
                self.story,
                self.state,
                intent_id,
                objects or [],
                generic_patch,
                self.consumed,
                allow_unauthored=_allow_unauthored,
            )
        if errors:
            self._logger.log({
                "event": "action_rejected",
                "stage": "resolve",
                "turn": self.turn_no + 1,
                "intent": intent_id,
                "objects": objects or [],
                "errors": errors,
            })
            raise SessionError("；".join(errors))

        started = time.monotonic()
        self.turn_no += 1
        result = run_turn(
            self.story,
            self.state,
            self.temporaries,
            self.consumed,
            self.turn_no,
            intent_id,
            objects,
            generic_patch,
            allow_unauthored=_allow_unauthored,
            capability_resolution=_capability_resolution,
        )
        if _director_provider is not None:
            boundaries = tuple(
                str(rule)
                for rule in (
                    list((self.story.data.get("player_role") or {}).get("constraints") or [])
                    + list((self.story.data.get("global_rules") or {}).get("boundaries") or [])
                )
            )
            run_director_cycle(
                self.story,
                self.state,
                result,
                _director_provider,
                # The beat reads the post-action state that is about to become
                # the next committed revision, not the pre-action perception.
                state_revision=self.state_revision + 1,
                player_action=_player_action,
                world_rules=boundaries,
            )
            result.ending = evaluate_endings(self.state, self.story.endings)
        result.prior_events = list(self._recent_events)
        self.ending = result.ending
        self.state_revision += 1
        self.last_result = result
        self.pending_clarification = None
        self._recent_events.extend(result.narrative_hints)
        del self._recent_events[:-RECENT_EVENT_WINDOW]
        self._requotes = 0
        self._pending_quotes.clear()
        self._plan_payloads.clear()
        checkpoint = self._append_checkpoint(parent_id=self._current_checkpoint_id)
        self._logger.log({
            "event": "resolve",
            "turn": self.turn_no,
            "state_revision": self.state_revision,
            "checkpoint_id": checkpoint.checkpoint_id,
            "parent_checkpoint_id": checkpoint.parent_id,
            "branch_id": checkpoint.branch_id,
            "intent": intent_id,
            "objects": objects or [],
            "generic_patch": generic_patch or {},
            "fired": result.fired,
            "world_rules": result.world_rules,
            "director_beats": [beat.to_dict() for beat in result.director_beats],
            "director_trace": result.director_trace,
            "world_step": self.state["world"].get("step"),
            "result_tier": result.result_tier,
            "errors": result.errors,
            "changes": [f"{p}: {a}->{b}" for p, a, b in result.changes if a != b],
            "scene": result.scene_after,
            "time_left": self.state["world"].get("time_left"),
            "goal_achieved": goal_achieved(self.story, self.state),
            "current_goal": current_goal(self.story, self.state),
            "ending": result.ending,
            "resolve_latency_ms": round((time.monotonic() - started) * 1000, 1),
            "llm_calls": 0,
        })
        return result
