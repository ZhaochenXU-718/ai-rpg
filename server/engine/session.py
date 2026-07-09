"""Game session: quote workflow + turn resolution + logging.

This is the object the CLI drives now and the HTTP API will wrap in stage 4.
Quote contract (plan section 4.4): low-risk intents resolve directly; others
must present a quote_id from this turn; requotes are free but capped.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from .content import Story
from .director import current_goal, goal_achieved
from .effects import TemporaryEffects
from .logger import TurnLogger
from .quote import MAX_REQUOTES_PER_TURN, build_quote
from .resolver import TurnResult, run_turn
from .state import build_initial_state


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
        self._requotes = 0
        self._pending_quotes: dict[str, dict[str, Any]] = {}
        self._logger = TurnLogger(log_dir, self.session_id)
        self._logger.log({"event": "session_start", "story": story.id, "session": self.session_id})

    @property
    def is_over(self) -> bool:
        return self.ending is not None

    def quote(self, intent_id: str, objects: list[str] | None = None, player_text: str = "") -> dict[str, Any]:
        if self.is_over:
            raise SessionError("session is over")
        if intent_id not in self.story.intents:
            raise SessionError(f"unknown intent '{intent_id}'")
        if self._requotes >= MAX_REQUOTES_PER_TURN:
            raise SessionError(f"本回合报价次数已达上限（{MAX_REQUOTES_PER_TURN}），请执行或换个回合再试。")
        started = time.monotonic()
        quote = build_quote(self.story, self.state, intent_id, objects or [], player_text, self._requotes)
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
        elif self.story.quote_required(intent_id):
            raise SessionError(f"intent '{intent_id}' requires a quote before resolving")

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
        )
        self.ending = result.ending
        self._requotes = 0
        self._pending_quotes.clear()
        self._logger.log({
            "event": "resolve",
            "turn": self.turn_no,
            "intent": intent_id,
            "objects": objects or [],
            "generic_patch": generic_patch or {},
            "fired": result.fired,
            "result_tier": result.result_tier,
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
