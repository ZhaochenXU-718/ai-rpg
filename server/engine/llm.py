"""Provider-neutral LLM interfaces for plans, suggestions and Director beats.

The deterministic engine consumes the same protocol regardless of provider.
Development and replay use:

- ScriptedProvider: predetermined plans, for tests and demos
- HeuristicMockProvider: generic label matching over PlayerPerception —
  deliberately story-agnostic (it only knows labels the perception exposes)
- ReplayProvider: replays parsed plans from a recorded trace file

DeepSeek is loaded lazily as the real provider. Action understanding is the
required interface; suggestion and Director methods are optional capabilities.
Everything downstream (validation, quoting, scheduling, commit and tracing)
remains provider-independent.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm_protocol import (
    ActionPlan,
    CapabilityAction,
    DirectorBeat,
    DirectorBeatKind,
    EntityKind,
    PlayerPerception,
    SuggestedActionDraft,
    ValidationResult,
    new_protocol_id,
)


@dataclass
class PlanRequest:
    perception: PlayerPerception
    player_text: str
    attempt: int = 0
    previous_plan: ActionPlan | None = None
    previous_validation: ValidationResult | None = None
    # Understanding context beyond raw perception: the world boundaries the
    # player already knows, and the soft-state space open to proposals.
    # Both are mechanics-surface information, not spoilers.
    world_rules: tuple[str, ...] = ()
    proposal_space: dict[str, Any] = field(default_factory=dict)
    # When the previous exchange ended in a clarification, the question the
    # player is now answering. previous_plan carries the plan it belongs to.
    pending_clarification: str | None = None


@dataclass
class NarrativeRequest:
    """Facts-only rendering input. The fact sheet is built behind the
    perception wall; whatever is not in it must not appear in the prose."""

    kind: str  # "turn" | "rejection"
    facts: dict[str, Any]
    style: dict[str, Any] = field(default_factory=dict)


@dataclass
class NarrativeResponse:
    text: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanResponse:
    plan: ActionPlan
    raw: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuggestionRequest:
    perception: PlayerPerception
    count: int = 5
    world_rules: tuple[str, ...] = ()
    proposal_space: dict[str, Any] = field(default_factory=dict)


@dataclass
class SuggestionResponse:
    suggestions: tuple[SuggestedActionDraft, ...]
    raw: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class DirectorRequest:
    """Post-action, pre-commit context for proposing optional NPC beats."""

    story_id: str
    state_revision: int
    turn_no: int
    location_id: str
    location_name: str
    current_goal: str
    player_id: str
    player_action: str
    action_targets: tuple[str, ...] = ()
    committed_result: dict[str, Any] = field(default_factory=dict)
    candidates: tuple[dict[str, Any], ...] = ()
    world_rules: tuple[str, ...] = ()
    max_beats: int = 2


@dataclass
class DirectorResponse:
    beats: tuple[DirectorBeat, ...]
    raw: str
    model: str
    prompt_version: str = "n/a"
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)


class LLMProviderError(Exception):
    pass


class LLMProvider(ABC):
    """Turns (perception, player text) into an ActionPlan proposal."""

    name = "abstract"

    @abstractmethod
    def propose_plan(self, request: PlanRequest) -> PlanResponse: ...

    def propose_suggestions(self, request: SuggestionRequest) -> SuggestionResponse:
        raise LLMProviderError(f"provider '{self.name}' does not support suggestions")

    def propose_director_beats(self, request: DirectorRequest) -> DirectorResponse:
        raise LLMProviderError(f"provider '{self.name}' does not support Director beats")

    def render_narrative(self, request: NarrativeRequest) -> NarrativeResponse | None:
        """Optional narrative rendering; None means fall back to templates."""
        return None


class ScriptedProvider(LLMProvider):
    """Pops predetermined plans; raises when the script runs dry."""

    name = "scripted"

    def __init__(
        self,
        plans: list[ActionPlan],
        suggestion_batches: list[tuple[SuggestedActionDraft, ...]] | None = None,
        director_batches: list[tuple[DirectorBeat, ...]] | None = None,
    ) -> None:
        self._plans = list(plans)
        self._suggestion_batches = list(suggestion_batches or [])
        self._director_batches = list(director_batches or [])

    def propose_plan(self, request: PlanRequest) -> PlanResponse:
        if not self._plans:
            raise LLMProviderError("scripted provider has no plans left")
        plan = self._plans.pop(0)
        return PlanResponse(
            plan=plan,
            raw=json.dumps(plan.to_dict(), ensure_ascii=False),
            model="scripted",
        )

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

    def propose_director_beats(self, request: DirectorRequest) -> DirectorResponse:
        beats = self._director_batches.pop(0) if self._director_batches else ()
        return DirectorResponse(
            beats=beats,
            raw=json.dumps([beat.to_dict() for beat in beats], ensure_ascii=False),
            model="scripted",
        )


class HeuristicMockProvider(LLMProvider):
    """Story-agnostic mock understanding: match perception labels in text.

    Knows nothing about any particular story — entities and intents come
    entirely from the PlayerPerception it is given, so it stays honest to
    the perception wall: it cannot reference what the player cannot see.
    """

    name = "mock"

    def propose_plan(self, request: PlanRequest) -> PlanResponse:
        started = time.monotonic()
        perception = request.perception
        text = request.player_text

        objects = self._match_objects(perception, text)
        social = self._match_social_request(perception, text, objects)
        if social is not None:
            owner_id, purpose_id = social
            plan = ActionPlan(
                plan_id=new_protocol_id("plan"),
                perception_revision=perception.state_revision,
                player_text=text,
                interpretation="你想向在场人物请求一件符合当前用途的物品。",
                goal="request_item",
                steps=(CapabilityAction(
                    capability="social",
                    action="request_item",
                    arguments={"owner_id": owner_id, "purpose": purpose_id},
                    purpose="请求物品",
                ),),
                references=(owner_id,),
                confidence=0.7,
                revision=request.attempt,
                parent_plan_id=(
                    request.previous_plan.plan_id
                    if request.attempt and request.previous_plan
                    else None
                ),
            )
            return PlanResponse(
                plan=plan,
                raw=json.dumps(plan.to_dict(), ensure_ascii=False),
                model="heuristic-mock-0.2",
                latency_ms=round((time.monotonic() - started) * 1000, 2),
            )
        intent_id = self._match_intent(perception, text, request)

        if intent_id is None:
            plan = ActionPlan(
                plan_id=new_protocol_id("plan"),
                perception_revision=perception.state_revision,
                player_text=text,
                interpretation="无法从方案中识别出明确的行动方式。",
                goal="clarify",
                steps=(),
                references=tuple(objects),
                needs_clarification=True,
                clarification_question="你想用哪种方式行动？可以说明是观察、交谈还是别的做法，并指出具体目标。",
                confidence=0.2,
            )
        else:
            plan = ActionPlan(
                plan_id=new_protocol_id("plan"),
                perception_revision=perception.state_revision,
                player_text=text,
                interpretation=self._interpretation(perception, intent_id, objects, text),
                goal=f"attempt_{intent_id}",
                intent_id=intent_id,
                steps=(CapabilityAction(
                    capability="intent",
                    action=intent_id,
                    arguments={"objects": objects},
                    purpose=f"以 {intent_id} 方式执行玩家方案",
                ),),
                references=tuple(objects),
                confidence=0.6,
                revision=request.attempt,
                parent_plan_id=(
                    request.previous_plan.plan_id if request.attempt and request.previous_plan else None
                ),
            )
        return PlanResponse(
            plan=plan,
            raw=json.dumps(plan.to_dict(), ensure_ascii=False),
            model="heuristic-mock-0.1",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
        )

    def propose_suggestions(self, request: SuggestionRequest) -> SuggestionResponse:
        started = time.monotonic()
        perception = request.perception
        drafts: list[SuggestedActionDraft] = []
        focus_cycle = ("practical", "social", "investigate", "cautious", "creative")

        social_tool = next(
            (
                tool for tool in perception.capability_tools
                if tool.tool_id == "social.request_item"
            ),
            None,
        )
        if social_tool is not None:
            schema = social_tool.arguments_schema
            owners = ((schema.get("properties") or {}).get("owner_id") or {}).get("enum") or []
            purposes = ((schema.get("properties") or {}).get("purpose") or {}).get("enum") or []
            if owners and purposes:
                owner_id = str(owners[0])
                purpose_id = str(purposes[0])
                owner_label = (schema.get("x-owner-labels") or {}).get(owner_id, owner_id)
                purpose_label = (schema.get("x-purpose-labels") or {}).get(
                    purpose_id, purpose_id
                )
                text = f"我问{owner_label}，有没有{purpose_label}。"
                plan = ActionPlan(
                    plan_id=new_protocol_id("plan"),
                    perception_revision=perception.state_revision,
                    player_text=text,
                    interpretation=f"向{owner_label}提出一个不预设答案的物品请求。",
                    goal="request_item",
                    steps=(CapabilityAction(
                        capability="social",
                        action="request_item",
                        arguments={"owner_id": owner_id, "purpose": purpose_id},
                        purpose="请求符合用途的物品",
                    ),),
                    references=(owner_id,),
                    confidence=0.8,
                )
                drafts.append(SuggestedActionDraft(
                    suggestion_id=new_protocol_id("suggestion"),
                    perception_revision=perception.state_revision,
                    title="直接问问能否借用",
                    action_text=text,
                    focus="practical",
                    rationale="把具体需求说清楚，但不假定对方一定答应。",
                    plan=plan,
                ))

        entities = [
            entity
            for entity in perception.visible_entities
            if entity.actionable
        ]
        for tool in perception.capability_tools:
            if len(drafts) >= request.count or tool.capability != "intent":
                continue
            targets: list[str] = []
            if tool.action == "move":
                target = next(
                    (entity for entity in entities if entity.kind == EntityKind.EXIT),
                    None,
                )
            else:
                target = next(
                    (entity for entity in entities if entity.kind != EntityKind.EXIT),
                    None,
                )
            if target is not None:
                targets.append(target.entity_id)
            label = tool.description.split("：", 1)[0]
            target_label = target.label if target is not None else "眼前的局面"
            text = f"我尝试用「{label}」的方式处理{target_label}。"
            plan = ActionPlan(
                plan_id=new_protocol_id("plan"),
                perception_revision=perception.state_revision,
                player_text=text,
                interpretation=f"针对{target_label}采取「{label}」行动。",
                goal=f"attempt_{tool.action}",
                intent_id=tool.action,
                steps=(CapabilityAction(
                    capability="intent",
                    action=tool.action,
                    arguments={"objects": targets},
                    purpose=f"执行{label}行动",
                ),),
                references=tuple(targets),
                confidence=0.55,
            )
            drafts.append(SuggestedActionDraft(
                suggestion_id=new_protocol_id("suggestion"),
                perception_revision=perception.state_revision,
                title=f"从{label}入手",
                action_text=text,
                focus=focus_cycle[len(drafts) % len(focus_cycle)],
                rationale="根据当前可见对象给出一个可编辑的行动起点。",
                plan=plan,
            ))

        selected = tuple(drafts[: request.count])
        return SuggestionResponse(
            suggestions=selected,
            raw=json.dumps(
                [suggestion.to_dict() for suggestion in selected],
                ensure_ascii=False,
            ),
            model="heuristic-mock-suggestions-0.1",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
        )

    def propose_director_beats(self, request: DirectorRequest) -> DirectorResponse:
        """Conservative mock: only let an already-targeted, present NPC react."""
        started = time.monotonic()
        target_ids = set(request.action_targets)
        candidate = next(
            (
                item for item in request.candidates
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
                summary=f"{actor_name}针对眼前这次行动作出简短回应，没有越过自己原有的立场。",
                motivation=str(candidate.get("motivation") or "回应眼前的行动。"),
            ),)
        return DirectorResponse(
            beats=beats,
            raw=json.dumps([beat.to_dict() for beat in beats], ensure_ascii=False),
            model="heuristic-mock-director-0.1",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
        )

    @staticmethod
    def _match_social_request(
        perception: PlayerPerception,
        text: str,
        objects: list[str],
    ) -> tuple[str, str] | None:
        if not any(token in text for token in ("借", "给我", "有没有", "请求")):
            return None
        tool = next(
            (
                candidate for candidate in perception.capability_tools
                if candidate.tool_id == "social.request_item"
            ),
            None,
        )
        if tool is None:
            return None
        schema = tool.arguments_schema
        owners = ((schema.get("properties") or {}).get("owner_id") or {}).get("enum") or []
        purposes = ((schema.get("properties") or {}).get("purpose") or {}).get("enum") or []
        owner_id = next((str(owner) for owner in owners if owner in objects), None)
        if owner_id is None and len(owners) == 1:
            owner_id = str(owners[0])
        labels = schema.get("x-purpose-labels") or {}
        purpose_id = next(
            (
                str(purpose)
                for purpose in purposes
                if str(labels.get(purpose, purpose)) in text
            ),
            None,
        )
        if purpose_id is None and len(purposes) == 1:
            purpose_id = str(purposes[0])
        if owner_id and purpose_id:
            return owner_id, purpose_id
        return None

    @staticmethod
    def _match_objects(perception: PlayerPerception, text: str) -> list[str]:
        matched: list[tuple[int, str]] = []
        for entity in tuple(perception.visible_entities) + tuple(perception.inventory):
            for token in (entity.label, entity.entity_id):
                position = text.find(token)
                if position >= 0:
                    matched.append((position, entity.entity_id))
                    break
        seen: set[str] = set()
        ordered = []
        for _, entity_id in sorted(matched):
            if entity_id not in seen:
                seen.add(entity_id)
                ordered.append(entity_id)
        return ordered

    def _match_intent(
        self, perception: PlayerPerception, text: str, request: PlanRequest
    ) -> str | None:
        available = set(perception.available_intents)
        # Replanning after a rejection: degrade to the free-form intent.
        if request.attempt and "custom" in available:
            return "custom"
        for tool in perception.capability_tools:
            if tool.capability != "intent" or tool.action not in available:
                continue
            label = tool.description.split("：", 1)[0]
            if label and label in text:
                return tool.action
        if "custom" in available:
            return "custom"
        return None

    @staticmethod
    def _interpretation(
        perception: PlayerPerception, intent_id: str, objects: list[str], text: str
    ) -> str:
        labels = {
            entity.entity_id: entity.label
            for entity in tuple(perception.visible_entities) + tuple(perception.inventory)
        }
        target_text = "、".join(labels.get(obj, obj) for obj in objects) or "未指明的目标"
        return f"你想针对{target_text}行动，方式最接近「{intent_id}」。"


class ReplayProvider(LLMProvider):
    """Replays parsed plans from a recorded trace (protocol section 11)."""

    name = "replay"

    def __init__(self, trace_path: str | Path) -> None:
        self._plans: list[ActionPlan] = []
        for line in Path(trace_path).read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("event") == "llm_response" and record.get("plan"):
                self._plans.append(ActionPlan.from_dict(record["plan"]))

    def propose_plan(self, request: PlanRequest) -> PlanResponse:
        if not self._plans:
            raise LLMProviderError("replay trace has no plans left")
        plan = self._plans.pop(0)
        return PlanResponse(
            plan=plan,
            raw=json.dumps(plan.to_dict(), ensure_ascii=False),
            model="replay",
        )


PROVIDERS: dict[str, type[LLMProvider]] = {
    HeuristicMockProvider.name: HeuristicMockProvider,
}


def create_provider(name: str, **kwargs: Any) -> LLMProvider:
    if name == "deepseek":
        # Lazy import: the deterministic engine must not require the SDK.
        from .llm_deepseek import DeepSeekProvider

        return DeepSeekProvider(**kwargs)
    try:
        return PROVIDERS[name](**kwargs)
    except KeyError:
        raise LLMProviderError(
            f"unknown provider '{name}' (available: {sorted(PROVIDERS) + ['deepseek']})"
        ) from None
