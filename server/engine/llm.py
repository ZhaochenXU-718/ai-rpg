"""Provider-neutral interfaces for narrative-first generation.

Providers write prose, propose editable action cards, extract a narrow set of
physical facts and compact old narrative events. None of these methods can
mutate authoritative state; fact validation sits behind this interface.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .memory import MemoryDigest, MemoryEvent
from .llm_protocol import (
    EntityKind,
    ExtractedFact,
    FactExtraction,
    MemoryContext,
    NarrativeAuthorContext,
    PerceptionSnapshot,
    SuggestedAction,
    new_protocol_id,
)


@dataclass(frozen=True)
class NarrativeRequest:
    """Facts-only prose request scoped by one perception snapshot.

    ``author_context`` is the only channel that may carry authored private
    material (blueprint, in-scene character cards); it exists for portrayal
    and direction, not as player knowledge.
    """

    kind: str
    perception: PerceptionSnapshot
    facts: dict[str, Any]
    style: dict[str, Any] = field(default_factory=dict)
    memory_context: MemoryContext | None = None
    author_context: NarrativeAuthorContext | None = None

    def __post_init__(self) -> None:
        _validate_memory_context(self.perception, self.memory_context)


@dataclass(frozen=True)
class NarrativeResponse:
    text: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SuggestionRequest:
    """Proposal request; ``story_direction`` is desensitized author direction.

    Suggestion cards reach the player verbatim, so this field must never
    carry ``ai_plot.hidden_truth`` or character-card private fields.
    """

    perception: PerceptionSnapshot
    count: int = 5
    boundaries: tuple[str, ...] = ()
    memory_context: MemoryContext | None = None
    story_direction: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_memory_context(self.perception, self.memory_context)


def _validate_memory_context(
    perception: PerceptionSnapshot,
    memory_context: MemoryContext | None,
) -> None:
    if memory_context is None:
        return
    if memory_context.subject_id != perception.subject_id:
        raise ValueError("memory context subject does not match perception")
    if memory_context.turn_no != perception.turn_no:
        raise ValueError("memory context turn does not match perception")
    if memory_context.state_revision != perception.state_revision:
        raise ValueError("memory context revision does not match perception")


@dataclass(frozen=True)
class SuggestionResponse:
    suggestions: tuple[SuggestedAction, ...]
    raw: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


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
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryCompactionRequest:
    """Old committed events and the previous soft digest."""

    story_id: str
    state_revision: int
    previous_digest: MemoryDigest
    events: tuple[MemoryEvent, ...]
    character_catalog: tuple[tuple[str, str], ...]
    scene_catalog: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MemoryCompactionResponse:
    digest: MemoryDigest
    raw: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)


class LLMProviderError(Exception):
    """Provider failure with structured, trace-safe diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


class LLMProvider:
    """Narrative-first provider surface; every capability is optional."""

    name = "abstract"

    def render_narrative(self, request: NarrativeRequest) -> NarrativeResponse | None:
        return None

    def propose_suggestions(self, request: SuggestionRequest) -> SuggestionResponse:
        raise LLMProviderError(f"provider '{self.name}' does not support suggestions")

    def extract_facts(self, request: FactExtractionRequest) -> FactExtractionResponse:
        raise LLMProviderError(f"provider '{self.name}' does not support fact extraction")

    def compact_memory(
        self,
        request: MemoryCompactionRequest,
    ) -> MemoryCompactionResponse:
        raise LLMProviderError(f"provider '{self.name}' does not support memory compaction")


class ScriptedProvider(LLMProvider):
    """Deterministic prose, card and fact queues for tests and demos."""

    name = "scripted"

    def __init__(
        self,
        narratives: list[str | None] | None = None,
        suggestion_batches: list[tuple[SuggestedAction, ...]] | None = None,
        fact_extractions: list[
            FactExtraction | tuple[ExtractedFact, ...]
        ] | None = None,
        memory_digests: list[MemoryDigest] | None = None,
    ) -> None:
        self._narratives = list(narratives or [])
        self._suggestion_batches = list(suggestion_batches or [])
        self._fact_extractions = list(fact_extractions or [])
        self._memory_digests = list(memory_digests or [])

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

    def compact_memory(
        self,
        request: MemoryCompactionRequest,
    ) -> MemoryCompactionResponse:
        if not self._memory_digests:
            raise LLMProviderError("scripted provider has no memory digest left")
        digest = self._memory_digests.pop(0)
        return MemoryCompactionResponse(
            digest=digest,
            raw=json.dumps(digest.to_dict(), ensure_ascii=False),
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

    def compact_memory(
        self,
        request: MemoryCompactionRequest,
    ) -> MemoryCompactionResponse:
        """Deterministic extractive fallback for offline M2 testing."""
        started = time.monotonic()
        parts = [request.previous_digest.rolling_summary.strip()]
        parts.extend(event.narrative.strip() for event in request.events)
        summary = " ".join(part for part in parts if part).strip()
        digest = MemoryDigest(
            compacted_through_turn=request.events[-1].turn_no,
            rolling_summary=summary[-1200:],
            open_loops=request.previous_digest.open_loops,
            character_notes=request.previous_digest.character_notes,
            scene_notes=request.previous_digest.scene_notes,
            recently_resolved=request.previous_digest.recently_resolved,
        )
        return MemoryCompactionResponse(
            digest=digest,
            raw=json.dumps(digest.to_dict(), ensure_ascii=False),
            model="heuristic-mock-memory-0.1",
            prompt_version="extractive-memory-v1",
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
