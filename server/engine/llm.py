"""LLM provider abstraction for the action-understanding loop.

v0.1 ships no real model call on purpose (dev-notes 2026-07-10: pick the
provider after the vertical loop works).  Three providers cover development:

- ScriptedProvider: predetermined plans, for tests and demos
- HeuristicMockProvider: generic label matching over PlayerPerception —
  deliberately story-agnostic (it only knows labels the perception exposes)
- ReplayProvider: replays parsed plans from a recorded trace file

A real provider plugs in by implementing ``LLMProvider.propose_plan`` and
registering in ``PROVIDERS``; everything downstream (validation, quoting,
commit, tracing) is provider-independent.
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
    PlayerPerception,
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


class LLMProviderError(Exception):
    pass


class LLMProvider(ABC):
    """Turns (perception, player text) into an ActionPlan proposal."""

    name = "abstract"

    @abstractmethod
    def propose_plan(self, request: PlanRequest) -> PlanResponse: ...

    def render_narrative(self, request: NarrativeRequest) -> NarrativeResponse | None:
        """Optional narrative rendering; None means fall back to templates."""
        return None


class ScriptedProvider(LLMProvider):
    """Pops predetermined plans; raises when the script runs dry."""

    name = "scripted"

    def __init__(self, plans: list[ActionPlan]) -> None:
        self._plans = list(plans)

    def propose_plan(self, request: PlanRequest) -> PlanResponse:
        if not self._plans:
            raise LLMProviderError("scripted provider has no plans left")
        plan = self._plans.pop(0)
        return PlanResponse(
            plan=plan,
            raw=json.dumps(plan.to_dict(), ensure_ascii=False),
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
