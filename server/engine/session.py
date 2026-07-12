"""Game session: quote workflow + turn resolution + logging.

This is the object the CLI drives now and the HTTP API will wrap in stage 4.
Quote contract (plan section 4.4): low-risk intents resolve directly; others
must present a quote_id from this turn; requotes are free but capped.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .capabilities import build_committed_outcome, validate_plan
from .content import Story
from .director import current_goal, goal_achieved
from .effects import TemporaryEffects
from .llm_protocol import ActionPlan, CommittedOutcome, ValidationResult
from .logger import TurnLogger
from .limits import clamp_generic_patch
from .perception import build_player_perception
from .quote import MAX_REQUOTES_PER_TURN, build_quote, default_proposal
from .resolver import TurnResult, run_turn, validate_action
from .state import build_initial_state

RECENT_EVENT_WINDOW = 5


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
        self._logger = TurnLogger(log_dir, self.session_id)
        self._logger.log({"event": "session_start", "story": story.id, "session": self.session_id})

    @property
    def is_over(self) -> bool:
        return self.ending is not None

    @property
    def recent_events(self) -> tuple[str, ...]:
        return tuple(self._recent_events)

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

    def resolve_plan(self, plan: ActionPlan, validation: ValidationResult) -> CommittedOutcome:
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
            "committed_paths": [change.path for change in outcome.committed_changes],
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
    ) -> TurnResult:
        if self.is_over:
            raise SessionError("session is over")

        if quote_id is not None:
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
        )
        self.ending = result.ending
        self.state_revision += 1
        self.last_result = result
        self.pending_clarification = None
        self._recent_events.extend(result.narrative_hints)
        del self._recent_events[:-RECENT_EVENT_WINDOW]
        self._requotes = 0
        self._pending_quotes.clear()
        self._plan_payloads.clear()
        self._logger.log({
            "event": "resolve",
            "turn": self.turn_no,
            "intent": intent_id,
            "objects": objects or [],
            "generic_patch": generic_patch or {},
            "fired": result.fired,
            "world_rules": result.world_rules,
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
