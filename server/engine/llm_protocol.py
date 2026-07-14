"""Structured contract between LLM cognition and the AIRPG world engine.

The LLM may understand, plan, propose and replan, but these models never
mutate runtime state.  The engine validates an ActionPlan and only committed
changes become world truth.  Renderers and directors consume the resulting
CommittedOutcome, not the original proposal.
"""

from __future__ import annotations

import copy
import uuid
from enum import Enum
from typing import Any, Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)


PROTOCOL_VERSION = "0.1"
ProtocolVersion = Literal["0.1"]
PrimaryGoalStatus = Literal["achieved", "partial", "not_achieved", "unverified"]
MachineId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_.-]*$"),
]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
_MACHINE_ID_ADAPTER = TypeAdapter(MachineId)


class ProtocolModel(BaseModel):
    """Strict, immutable and JSON-serializable protocol model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, value: Any):
        return cls.model_validate(value)


class AuthorityLevel(str, Enum):
    """How much engine authority a proposed state change requires."""

    PRESENTATION = "presentation"
    SOFT_STATE = "soft_state"
    MECHANICAL = "mechanical"
    # Validated generative facts (authoring-contract section 4): proposed by
    # the Director channel, admitted only through archetype, budget and
    # conflict checks. Never writable from an ActionPlan proposal.
    LOCAL_CANON = "local_canon"
    CANON = "canon"


class EntityKind(str, Enum):
    CHARACTER = "character"
    ITEM = "item"
    ENVIRONMENT = "environment"
    STATE = "state"
    EXIT = "exit"
    LOCATION = "location"
    OTHER = "other"


class ChangeOperation(str, Enum):
    SET = "set"
    INCREMENT = "increment"
    APPEND = "append"
    REMOVE = "remove"


class RiskLikelihood(str, Enum):
    UNLIKELY = "unlikely"
    POSSIBLE = "possible"
    LIKELY = "likely"
    CERTAIN = "certain"


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    ADJUSTMENT = "adjustment"


class DirectorBeatKind(str, Enum):
    ENTER_SCENE = "enter_scene"
    REACT = "react"
    ADVANCE_PLAN = "advance_plan"


def new_protocol_id(prefix: MachineId) -> str:
    prefix = _MACHINE_ID_ADAPTER.validate_python(prefix)
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _ensure_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


class PerceivedEntity(ProtocolModel):
    entity_id: NonEmptyStr
    label: NonEmptyStr
    kind: EntityKind
    actionable: bool = False
    description: str = ""
    public_state: dict[str, Any] = Field(default_factory=dict)


class CapabilityTool(ProtocolModel):
    capability: MachineId
    action: MachineId
    description: NonEmptyStr
    arguments_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_authority_levels: tuple[AuthorityLevel, ...] = ()

    @property
    def tool_id(self) -> str:
        return f"{self.capability}.{self.action}"

    @field_validator("allowed_authority_levels")
    @classmethod
    def authority_levels_are_unique(cls, values: tuple[AuthorityLevel, ...]):
        _ensure_unique([value.value for value in values], "allowed_authority_levels")
        return values


class PlayerPerception(ProtocolModel):
    """Player-visible snapshot supplied to the action-understanding LLM."""

    story_id: NonEmptyStr
    session_id: NonEmptyStr
    turn_no: NonNegativeInt
    state_revision: NonNegativeInt
    location_id: NonEmptyStr
    location_name: NonEmptyStr
    current_goal: str = ""
    visible_entities: tuple[PerceivedEntity, ...] = ()
    inventory: tuple[PerceivedEntity, ...] = ()
    known_facts: tuple[NonEmptyStr, ...] = ()
    available_intents: tuple[MachineId, ...] = ()
    capability_tools: tuple[CapabilityTool, ...] = ()
    recent_events: tuple[NonEmptyStr, ...] = ()
    public_state: dict[str, Any] = Field(default_factory=dict)
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def entries_are_unique(self):
        _ensure_unique(
            [entity.entity_id for entity in self.visible_entities], "visible_entities"
        )
        _ensure_unique([entity.entity_id for entity in self.inventory], "inventory")
        _ensure_unique([tool.tool_id for tool in self.capability_tools], "capability_tools")
        _ensure_unique(list(self.known_facts), "known_facts")
        _ensure_unique(list(self.available_intents), "available_intents")
        return self


class CapabilityAction(ProtocolModel):
    capability: MachineId
    action: MachineId
    arguments: dict[str, Any]
    purpose: NonEmptyStr
    optional: bool = False

    @property
    def tool_id(self) -> str:
        return f"{self.capability}.{self.action}"


class StateChangeProposal(ProtocolModel):
    path: NonEmptyStr
    operation: ChangeOperation
    value: Any
    authority: AuthorityLevel
    reason: NonEmptyStr
    step_index: NonNegativeInt | None = None
    duration_turns: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def increment_requires_number(self):
        if self.operation == ChangeOperation.INCREMENT and (
            not isinstance(self.value, (int, float)) or isinstance(self.value, bool)
        ):
            raise ValueError("increment value must be a number")
        return self


class RiskProposal(ProtocolModel):
    description: NonEmptyStr
    likelihood: RiskLikelihood
    changes: tuple[StateChangeProposal, ...] = ()
    mitigation: NonEmptyStr | None = None


class ActionPlan(ProtocolModel):
    """LLM interpretation and capability-tool plan; never executable directly."""

    plan_id: NonEmptyStr
    perception_revision: NonNegativeInt
    player_text: NonEmptyStr
    interpretation: NonEmptyStr
    goal: NonEmptyStr
    steps: tuple[CapabilityAction, ...]
    intent_id: NonEmptyStr | None = None
    references: tuple[NonEmptyStr, ...] = ()
    proposed_changes: tuple[StateChangeProposal, ...] = ()
    risks: tuple[RiskProposal, ...] = ()
    assumptions: tuple[NonEmptyStr, ...] = ()
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0
    needs_clarification: bool = False
    clarification_question: NonEmptyStr | None = None
    revision: NonNegativeInt = 0
    parent_plan_id: NonEmptyStr | None = None
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def plan_is_coherent(self):
        _ensure_unique(list(self.references), "references")
        _ensure_unique(list(self.assumptions), "assumptions")
        if self.needs_clarification:
            if not self.clarification_question:
                raise ValueError(
                    "clarification_question is required when needs_clarification=true"
                )
        elif self.clarification_question is not None:
            raise ValueError(
                "clarification_question requires needs_clarification=true"
            )
        elif not self.steps:
            raise ValueError("steps must not be empty for an executable plan")
        if self.revision > 0 and not self.parent_plan_id:
            raise ValueError("parent_plan_id is required when revision > 0")
        all_changes = list(self.proposed_changes)
        for risk in self.risks:
            all_changes.extend(risk.changes)
        for change in all_changes:
            if change.step_index is not None and change.step_index >= len(self.steps):
                raise ValueError("proposed change step_index is outside steps")
        return self


class ValidationIssue(ProtocolModel):
    code: MachineId
    severity: IssueSeverity
    message: NonEmptyStr
    retryable: bool
    step_index: NonNegativeInt | None = None
    path: NonEmptyStr | None = None


class ValidationResult(ProtocolModel):
    """Engine decision over an ActionPlan at a specific state revision."""

    validation_id: NonEmptyStr
    plan_id: NonEmptyStr
    state_revision: NonNegativeInt
    can_execute: bool
    can_replan: bool
    accepted_step_indices: tuple[NonNegativeInt, ...] = ()
    accepted_changes: tuple[StateChangeProposal, ...] = ()
    adjusted_costs: tuple[StateChangeProposal, ...] = ()
    issues: tuple[ValidationIssue, ...] = ()
    missing_information: tuple[NonEmptyStr, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def decision_is_coherent(self):
        _ensure_unique(
            [str(index) for index in self.accepted_step_indices],
            "accepted_step_indices",
        )
        has_error = any(issue.severity == IssueSeverity.ERROR for issue in self.issues)
        if self.can_execute and has_error:
            raise ValueError("can_execute cannot be true with error issues")
        if self.can_execute and not self.accepted_step_indices:
            raise ValueError("accepted_step_indices is required when executable")
        if not self.can_execute and not has_error and not self.missing_information:
            raise ValueError("a rejected result requires an error or missing information")
        accepted = set(self.accepted_step_indices)
        if any(
            change.step_index is not None and change.step_index not in accepted
            for change in self.accepted_changes
        ):
            raise ValueError("accepted change refers to a step that was not accepted")
        return self


class SuggestedActionDraft(ProtocolModel):
    """LLM-authored card before deterministic plan validation."""

    suggestion_id: NonEmptyStr
    perception_revision: NonNegativeInt
    title: NonEmptyStr
    action_text: NonEmptyStr
    focus: MachineId
    rationale: NonEmptyStr
    plan: ActionPlan
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def draft_matches_frozen_plan(self):
        if self.plan.perception_revision != self.perception_revision:
            raise ValueError("suggestion and plan perception revisions must match")
        if self.plan.player_text != self.action_text:
            raise ValueError("action_text must equal the frozen plan player_text")
        return self


class SuggestedAction(SuggestedActionDraft):
    """Player-facing card whose exact frozen plan already passed validation."""

    validation: ValidationResult

    @model_validator(mode="after")
    def validation_matches_plan(self):
        if self.validation.plan_id != self.plan.plan_id:
            raise ValueError("suggestion validation must belong to its plan")
        if self.validation.state_revision != self.perception_revision:
            raise ValueError("suggestion validation revision must match perception")
        if not self.validation.can_execute:
            raise ValueError("player-facing suggestions must be executable")
        return self


class SuggestedActionSet(ProtocolModel):
    suggestion_set_id: NonEmptyStr
    perception_revision: NonNegativeInt
    actions: tuple[SuggestedAction, ...]
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def set_is_coherent(self):
        if not 1 <= len(self.actions) <= 5:
            raise ValueError("a suggestion set must contain 1 to 5 actions")
        _ensure_unique(
            [action.suggestion_id for action in self.actions],
            "suggestion ids",
        )
        if any(
            action.perception_revision != self.perception_revision
            for action in self.actions
        ):
            raise ValueError("all suggestions must use the set perception revision")
        return self


class DirectorBeat(ProtocolModel):
    """A proposed beat over an authored character; never creates an entity."""

    beat_id: MachineId
    state_revision: NonNegativeInt
    kind: DirectorBeatKind
    actor_id: NonEmptyStr
    target_location_id: NonEmptyStr
    target_ids: tuple[NonEmptyStr, ...] = ()
    summary: NonEmptyStr
    motivation: NonEmptyStr
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def targets_are_unique(self):
        _ensure_unique(list(self.target_ids), "target_ids")
        return self


class DirectorBeatValidation(ProtocolModel):
    validation_id: NonEmptyStr
    beat_id: NonEmptyStr
    state_revision: NonNegativeInt
    can_schedule: bool
    issues: tuple[ValidationIssue, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def decision_is_coherent(self):
        has_error = any(
            issue.severity == IssueSeverity.ERROR for issue in self.issues
        )
        if self.can_schedule == has_error:
            raise ValueError(
                "can_schedule must be true exactly when validation has no errors"
            )
        return self


class LocalCanonKind(str, Enum):
    """v1 generative fact kinds; characters are deliberately not includable."""

    LOCATION = "location"
    SITUATION = "situation"


GENERATED_ENTITY_PREFIX = "gen_"


class LocalCanonProposal(ProtocolModel):
    """A proposed persistent local fact; only the engine may admit it.

    Entity ids live in a mandatory ``gen_`` namespace so generated facts can
    never shadow an authored id and their provenance stays visible in every
    trace and state dump.
    """

    proposal_id: NonEmptyStr
    state_revision: NonNegativeInt
    kind: LocalCanonKind
    archetype_id: MachineId
    entity_id: MachineId
    name: NonEmptyStr
    description: NonEmptyStr
    # For locations: the authored location it attaches to.
    # For situations: the authored location where the situation holds.
    parent_location_id: NonEmptyStr
    # Situations only: relative lifetime in turns; None follows the archetype.
    expires_after_turns: Annotated[int, Field(ge=1)] | None = None
    reason: NonEmptyStr
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def proposal_is_coherent(self):
        if not self.entity_id.startswith(GENERATED_ENTITY_PREFIX):
            raise ValueError(
                f"generated entity ids must start with '{GENERATED_ENTITY_PREFIX}'"
            )
        if self.kind == LocalCanonKind.LOCATION and self.expires_after_turns is not None:
            raise ValueError("v1 generated locations are persistent-local; no expiry")
        return self


class LocalCanonValidation(ProtocolModel):
    validation_id: NonEmptyStr
    proposal_id: NonEmptyStr
    state_revision: NonNegativeInt
    can_commit: bool
    issues: tuple[ValidationIssue, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def decision_is_coherent(self):
        has_error = any(
            issue.severity == IssueSeverity.ERROR for issue in self.issues
        )
        if self.can_commit == has_error:
            raise ValueError(
                "can_commit must be true exactly when validation has no errors"
            )
        return self


class CommittedChange(ProtocolModel):
    path: NonEmptyStr
    previous: Any
    new: Any
    authority: AuthorityLevel
    source: MachineId
    reason: str = ""


class CommittedDirectorBeat(ProtocolModel):
    """One validated Director beat included in the surrounding turn commit."""

    beat_id: MachineId
    validation_id: NonEmptyStr
    kind: DirectorBeatKind
    actor_id: NonEmptyStr
    target_location_id: NonEmptyStr
    narrative_hint: NonEmptyStr
    committed_changes: tuple[CommittedChange, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION


class CommittedLocalCanon(ProtocolModel):
    """One admitted generative fact included in the surrounding turn commit."""

    proposal_id: NonEmptyStr
    validation_id: NonEmptyStr
    kind: LocalCanonKind
    entity_id: MachineId
    archetype_id: MachineId
    name: NonEmptyStr
    parent_location_id: NonEmptyStr
    narrative_hint: NonEmptyStr
    committed_changes: tuple[CommittedChange, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION


class CommittedOutcome(ProtocolModel):
    """Actual world result; the only payload that result narration may trust."""

    outcome_id: NonEmptyStr
    plan_id: NonEmptyStr
    validation_id: NonEmptyStr
    turn_no: NonNegativeInt
    state_revision_before: NonNegativeInt
    state_revision_after: NonNegativeInt
    scene_before: NonEmptyStr
    scene_after: NonEmptyStr
    result_tier: NonEmptyStr
    primary_goal_status: PrimaryGoalStatus = "unverified"
    cost_only: bool = False
    resolution_sources: tuple[MachineId, ...] = ()
    accepted_step_indices: tuple[NonNegativeInt, ...] = ()
    committed_changes: tuple[CommittedChange, ...] = ()
    director_beats: tuple[CommittedDirectorBeat, ...] = ()
    local_canon: tuple[CommittedLocalCanon, ...] = ()
    fired_storylets: tuple[NonEmptyStr, ...] = ()
    world_events: tuple[NonEmptyStr, ...] = ()
    new_facts: tuple[NonEmptyStr, ...] = ()
    rejected_effects: tuple[ValidationIssue, ...] = ()
    ending: NonEmptyStr | None = None
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def revisions_are_monotonic(self):
        if self.state_revision_after < self.state_revision_before:
            raise ValueError("state_revision_after must be >= state_revision_before")
        _ensure_unique(
            [str(index) for index in self.accepted_step_indices],
            "accepted_step_indices",
        )
        _ensure_unique(list(self.resolution_sources), "resolution_sources")
        return self


def action_plan_json_schema() -> dict[str, Any]:
    """Provider-neutral JSON Schema for structured ActionPlan generation."""
    return copy.deepcopy(ActionPlan.model_json_schema())


def suggested_action_draft_json_schema() -> dict[str, Any]:
    """Provider-neutral schema for one unvalidated suggestion card."""
    return copy.deepcopy(SuggestedActionDraft.model_json_schema())
