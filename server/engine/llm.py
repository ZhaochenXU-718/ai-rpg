"""Provider-neutral interfaces for narrative-first generation.

Providers write prose or propose editable action cards, Director plans and
future NPC turns. None of these methods can mutate authoritative state; fact
extraction and iron-law validation sit behind this interface.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .llm_protocol import (
    DirectorBeat,
    DirectorBeatKind,
    DirectorPlan,
    EntityKind,
    ExtractedFact,
    FactExtraction,
    LocalCanonProposal,
    NpcTurn,
    PerceptionSnapshot,
    SuggestedAction,
    new_protocol_id,
)


@dataclass(frozen=True)
class NarrativeRequest:
    """Facts-only prose request scoped by one perception snapshot."""

    kind: str
    perception: PerceptionSnapshot
    facts: dict[str, Any]
    style: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NarrativeResponse:
    text: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SuggestionRequest:
    perception: PerceptionSnapshot
    count: int = 5
    boundaries: tuple[str, ...] = ()


@dataclass(frozen=True)
class SuggestionResponse:
    suggestions: tuple[SuggestedAction, ...]
    raw: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FactExtractionRequest:
    """A prose candidate plus the minimum ledger needed to ground facts."""

    perception: PerceptionSnapshot
    player_text: str
    narrative: str
    ledger: dict[str, Any]


@dataclass(frozen=True)
class FactExtractionResponse:
    extraction: FactExtraction
    raw: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DirectorRequest:
    """Post-fact context for a non-authoritative Director proposal."""

    story_id: str
    state_revision: int
    turn_no: int
    location_id: str
    location_name: str
    current_goal: str
    player_id: str
    player_action: str
    action_targets: tuple[str, ...] = ()
    committed_turn: dict[str, Any] = field(default_factory=dict)
    candidates: tuple[dict[str, Any], ...] = ()
    boundaries: tuple[str, ...] = ()
    max_beats: int = 2
    generation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DirectorResponse:
    plan: DirectorPlan
    raw: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NpcRequest:
    """Reserved Phase 4 request for a single authored NPC agent."""

    perception: PerceptionSnapshot
    character_card: dict[str, Any]
    scene_direction: str = ""


@dataclass(frozen=True)
class NpcResponse:
    turn: NpcTurn
    raw: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


class LLMProviderError(Exception):
    pass


class LLMProvider:
    """Narrative-first provider surface; every capability is optional."""

    name = "abstract"

    def render_narrative(self, request: NarrativeRequest) -> NarrativeResponse | None:
        return None

    def propose_suggestions(self, request: SuggestionRequest) -> SuggestionResponse:
        raise LLMProviderError(f"provider '{self.name}' does not support suggestions")

    def extract_facts(self, request: FactExtractionRequest) -> FactExtractionResponse:
        raise LLMProviderError(f"provider '{self.name}' does not support fact extraction")

    def propose_director(self, request: DirectorRequest) -> DirectorResponse:
        raise LLMProviderError(f"provider '{self.name}' does not support Director plans")

    def propose_npc_turn(self, request: NpcRequest) -> NpcResponse:
        raise LLMProviderError(f"provider '{self.name}' does not support NPC turns")


class ScriptedProvider(LLMProvider):
    """Deterministic prose/card/Director queues for tests and demos."""

    name = "scripted"

    def __init__(
        self,
        narratives: list[str | None] | None = None,
        suggestion_batches: list[tuple[SuggestedAction, ...]] | None = None,
        director_batches: list[tuple[DirectorBeat, ...]] | None = None,
        local_canon_batches: list[tuple[LocalCanonProposal, ...]] | None = None,
        fact_extractions: list[
            FactExtraction | tuple[ExtractedFact, ...]
        ] | None = None,
    ) -> None:
        self._narratives = list(narratives or [])
        self._suggestion_batches = list(suggestion_batches or [])
        self._director_batches = list(director_batches or [])
        self._local_canon_batches = list(local_canon_batches or [])
        self._fact_extractions = list(fact_extractions or [])

    def render_narrative(self, request: NarrativeRequest) -> NarrativeResponse | None:
        if not self._narratives:
            return None
        text = self._narratives.pop(0)
        if text is None:
            return None
        return NarrativeResponse(text=text, model="scripted")

    def propose_suggestions(self, request: SuggestionRequest) -> SuggestionResponse:
        if not self._suggestion_batches:
            raise LLMProviderError("scripted provider has no suggestions left")
        suggestions = self._suggestion_batches.pop(0)
        return SuggestionResponse(
            suggestions=suggestions,
            raw=json.dumps(
                [suggestion.to_dict() for suggestion in suggestions],
                ensure_ascii=False,
            ),
            model="scripted",
        )

    def extract_facts(self, request: FactExtractionRequest) -> FactExtractionResponse:
        if not self._fact_extractions:
            raise LLMProviderError("scripted provider has no fact extraction left")
        queued = self._fact_extractions.pop(0)
        extraction = queued if isinstance(queued, FactExtraction) else FactExtraction(
            state_revision=request.perception.state_revision,
            facts=queued,
        )
        return FactExtractionResponse(
            extraction=extraction,
            raw=json.dumps(extraction.to_dict(), ensure_ascii=False),
            model="scripted",
        )

    def propose_director(self, request: DirectorRequest) -> DirectorResponse:
        beats = self._director_batches.pop(0) if self._director_batches else ()
        proposals = (
            self._local_canon_batches.pop(0) if self._local_canon_batches else ()
        )
        plan = DirectorPlan(
            state_revision=request.state_revision,
            beats=beats,
            local_canon=proposals,
        )
        return DirectorResponse(
            plan=plan,
            raw=json.dumps(plan.to_dict(), ensure_ascii=False),
            model="scripted",
        )


class HeuristicMockProvider(LLMProvider):
    """Story-neutral development provider using only scoped perception."""

    name = "mock"

    def extract_facts(self, request: FactExtractionRequest) -> FactExtractionResponse:
        """Fail closed: the development mock never invents authoritative facts."""
        started = time.monotonic()
        extraction = FactExtraction(
            state_revision=request.perception.state_revision,
            facts=(),
        )
        return FactExtractionResponse(
            extraction=extraction,
            raw=json.dumps(extraction.to_dict(), ensure_ascii=False),
            model="heuristic-mock-extractor-0.1",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
        )

    def propose_suggestions(self, request: SuggestionRequest) -> SuggestionResponse:
        started = time.monotonic()
        perception = request.perception
        characters = [
            entity
            for entity in perception.visible_entities
            if entity.kind == EntityKind.CHARACTER
        ]
        environments = [
            entity
            for entity in perception.visible_entities
            if entity.kind in {EntityKind.ENVIRONMENT, EntityKind.STATE}
        ]
        exits = [
            entity
            for entity in perception.visible_entities
            if entity.kind == EntityKind.EXIT
        ]
        drafts: list[tuple[str, str, str, str]] = []
        if characters:
            target = characters[0]
            drafts.append((
                f"先听听{target.label}怎么说",
                f"我先和{target.label}聊聊，听清对方眼下真正顾虑的事。",
                "social",
                "从人物动机入手，不预设对方一定答应。",
            ))
        if environments:
            target = environments[0]
            drafts.append((
                f"仔细看看{target.label}",
                f"我停下来仔细观察{target.label}，确认眼前已经存在的条件。",
                "investigate",
                "先补足可见信息，再决定下一步。",
            ))
        if exits:
            target = exits[0]
            drafts.append((
                target.label,
                f"我按「{target.label}」指明的方向动身，同时留意沿途的变化。",
                "practical",
                "提出移动尝试，不预先宣告已经到达。",
            ))
        drafts.append((
            "换一种自己的做法",
            "我不急着照现成路线行动，先用自己的方式回应眼前局面，并把目标说具体。",
            "creative",
            "提案只是灵感，玩家始终可以完全改写。",
        ))
        suggestions = tuple(
            SuggestedAction(
                suggestion_id=new_protocol_id("suggestion"),
                perception_revision=perception.state_revision,
                title=title,
                action_text=action_text,
                focus=focus,
                rationale=rationale,
            )
            for title, action_text, focus, rationale in drafts[: request.count]
        )
        return SuggestionResponse(
            suggestions=suggestions,
            raw=json.dumps(
                [suggestion.to_dict() for suggestion in suggestions],
                ensure_ascii=False,
            ),
            model="heuristic-mock-suggestions-0.2",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
        )

    def propose_director(self, request: DirectorRequest) -> DirectorResponse:
        """Only an explicitly referenced, present authored NPC may react."""
        started = time.monotonic()
        target_ids = set(request.action_targets)
        candidate = next(
            (
                item
                for item in request.candidates
                if item.get("status") == "present"
                and item.get("actor_id") in target_ids
            ),
            None,
        )
        beats: tuple[DirectorBeat, ...] = ()
        if candidate is not None:
            actor_id = str(candidate["actor_id"])
            actor_name = str(candidate.get("name") or actor_id)
            beats = (DirectorBeat(
                beat_id=new_protocol_id("beat"),
                state_revision=request.state_revision,
                kind=DirectorBeatKind.REACT,
                actor_id=actor_id,
                target_location_id=request.location_id,
                target_ids=tuple(
                    target for target in request.action_targets if target != actor_id
                ),
                summary=(
                    f"{actor_name}针对眼前这次行动作出简短回应，"
                    "没有越过自己原有的立场。"
                ),
                motivation=str(candidate.get("motivation") or "回应眼前的行动。"),
            ),)
        plan = DirectorPlan(state_revision=request.state_revision, beats=beats)
        return DirectorResponse(
            plan=plan,
            raw=json.dumps(plan.to_dict(), ensure_ascii=False),
            model="heuristic-mock-director-0.2",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
        )


PROVIDERS: dict[str, type[LLMProvider]] = {
    HeuristicMockProvider.name: HeuristicMockProvider,
}


def create_provider(name: str, **kwargs: Any) -> LLMProvider:
    if name == "deepseek":
        from .llm_deepseek import DeepSeekProvider

        return DeepSeekProvider(**kwargs)
    try:
        return PROVIDERS[name](**kwargs)
    except KeyError:
        raise LLMProviderError(
            f"unknown provider '{name}' (available: {sorted(PROVIDERS) + ['deepseek']})"
        ) from None
