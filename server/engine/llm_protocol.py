"""Narrative-first protocols shared by providers and the world engine.

Protocol 0.2 removes the pre-pivot ActionPlan/capability/validation surface.
Providers may write prose and propose cards, Director plans or future NPC
turns, but only a validated FactBatch may cross into authoritative state.
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


PROTOCOL_VERSION = "0.2"
ProtocolVersion = Literal["0.2"]
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


def new_protocol_id(prefix: MachineId) -> str:
    prefix = _MACHINE_ID_ADAPTER.validate_python(prefix)
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _ensure_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


class PerceptionAudience(str, Enum):
    """Whose knowledge boundary produced a perception snapshot."""

    PLAYER = "player"
    NPC = "npc"
    DIRECTOR = "director"


class EntityKind(str, Enum):
    CHARACTER = "character"
    ITEM = "item"
    ENVIRONMENT = "environment"
    STATE = "state"
    EXIT = "exit"
    LOCATION = "location"
    OTHER = "other"


class PerceivedEntity(ProtocolModel):
    entity_id: NonEmptyStr
    label: NonEmptyStr
    kind: EntityKind
    actionable: bool = False
    description: str = ""
    public_state: dict[str, Any] = Field(default_factory=dict)


class PerceptionSnapshot(ProtocolModel):
    """One subject-scoped view of the world.

    The same schema can carry player, NPC or Director views; the builder that
    supplies it owns the disclosure policy. A snapshot never contains a
    capability menu or an implicit permission to mutate state.
    """

    audience: PerceptionAudience
    subject_id: NonEmptyStr
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
    recent_events: tuple[NonEmptyStr, ...] = ()
    public_state: dict[str, Any] = Field(default_factory=dict)
    subject_context: dict[str, Any] = Field(default_factory=dict)
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def entries_are_unique(self):
        _ensure_unique(
            [entity.entity_id for entity in self.visible_entities],
            "visible_entities",
        )
        _ensure_unique(
            [entity.entity_id for entity in self.inventory],
            "inventory",
        )
        _ensure_unique(list(self.known_facts), "known_facts")
        return self


class SuggestedAction(ProtocolModel):
    """Editable prose inspiration, never a frozen execution plan."""

    suggestion_id: MachineId
    perception_revision: NonNegativeInt
    title: NonEmptyStr
    action_text: NonEmptyStr
    focus: MachineId
    rationale: NonEmptyStr
    expected_iron_law_touches: tuple[NonEmptyStr, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @field_validator("expected_iron_law_touches")
    @classmethod
    def touches_are_unique(cls, values: tuple[str, ...]):
        _ensure_unique(list(values), "expected_iron_law_touches")
        return values


class SuggestedActionSet(ProtocolModel):
    suggestion_set_id: MachineId
    perception_revision: NonNegativeInt
    actions: tuple[SuggestedAction, ...]
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def actions_match_snapshot(self):
        if not self.actions:
            raise ValueError("actions must not be empty")
        _ensure_unique(
            [action.suggestion_id for action in self.actions],
            "actions.suggestion_id",
        )
        for action in self.actions:
            if action.perception_revision != self.perception_revision:
                raise ValueError("all actions must use the set perception_revision")
        return self


class FactBatch(ProtocolModel):
    """Post-generation facts waiting at the authoritative commit boundary.

    During Phase 1 only prose-only batches are constructed by the CLI. Phase
    2 will populate state_changes after extraction and iron-law validation.
    """

    batch_id: MachineId = Field(default_factory=lambda: new_protocol_id("batch"))
    state_revision: NonNegativeInt
    player_text: NonEmptyStr
    narrative: NonEmptyStr
    state_changes: dict[str, Any] = Field(default_factory=dict)
    facts: tuple[NonEmptyStr, ...] = ()
    references: tuple[NonEmptyStr, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def entries_are_unique(self):
        _ensure_unique(list(self.facts), "facts")
        _ensure_unique(list(self.references), "references")
        return self


class IronLawDomain(str, Enum):
    PRESENCE = "presence"
    ITEM_CUSTODY = "item_custody"
    DISCLOSURE = "disclosure"
    COMMITMENT = "commitment"
    ANCHOR = "anchor"
    IRREVERSIBLE = "irreversible"
    WORLD_BOUNDARY = "world_boundary"


class IronLawViolation(ProtocolModel):
    """Code-level rejection emitted before a FactBatch may commit."""

    code: MachineId
    domain: IronLawDomain
    message: NonEmptyStr
    retryable: bool = True
    path: NonEmptyStr | None = None
    evidence: NonEmptyStr | None = None
    protocol_version: ProtocolVersion = PROTOCOL_VERSION


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    ADJUSTMENT = "adjustment"


class ValidationIssue(ProtocolModel):
    """Generic engine admission issue used by Director and Local Canon."""

    code: MachineId
    severity: IssueSeverity
    message: NonEmptyStr
    retryable: bool
    path: NonEmptyStr | None = None


class DirectorBeatKind(str, Enum):
    ENTER_SCENE = "enter_scene"
    REACT = "react"
    ADVANCE_PLAN = "advance_plan"


class DirectorBeat(ProtocolModel):
    beat_id: MachineId
    state_revision: NonNegativeInt
    kind: DirectorBeatKind
    actor_id: NonEmptyStr
    target_location_id: NonEmptyStr
    target_ids: tuple[NonEmptyStr, ...] = ()
    summary: NonEmptyStr
    motivation: NonEmptyStr
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @field_validator("target_ids")
    @classmethod
    def targets_are_unique(cls, values: tuple[str, ...]):
        _ensure_unique(list(values), "target_ids")
        return values


class DirectorBeatValidation(ProtocolModel):
    validation_id: MachineId
    beat_id: MachineId
    state_revision: NonNegativeInt
    can_schedule: bool
    issues: tuple[ValidationIssue, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def decision_is_coherent(self):
        has_error = any(issue.severity == IssueSeverity.ERROR for issue in self.issues)
        if self.can_schedule == has_error:
            raise ValueError(
                "can_schedule must be true exactly when validation has no errors"
            )
        return self


class LocalCanonKind(str, Enum):
    """Generated fact kinds; important characters remain author-owned."""

    LOCATION = "location"
    SITUATION = "situation"


GENERATED_ENTITY_PREFIX = "gen_"


class LocalCanonProposal(ProtocolModel):
    proposal_id: MachineId
    state_revision: NonNegativeInt
    kind: LocalCanonKind
    archetype_id: MachineId
    entity_id: MachineId
    name: NonEmptyStr
    description: NonEmptyStr
    parent_location_id: NonEmptyStr
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
            raise ValueError("generated locations are persistent-local; no expiry")
        return self


class LocalCanonValidation(ProtocolModel):
    validation_id: MachineId
    proposal_id: MachineId
    state_revision: NonNegativeInt
    can_commit: bool
    issues: tuple[ValidationIssue, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def decision_is_coherent(self):
        has_error = any(issue.severity == IssueSeverity.ERROR for issue in self.issues)
        if self.can_commit == has_error:
            raise ValueError(
                "can_commit must be true exactly when validation has no errors"
            )
        return self


class DirectorPlan(ProtocolModel):
    """Non-authoritative Director proposal revalidated by the engine."""

    plan_id: MachineId = Field(default_factory=lambda: new_protocol_id("director"))
    state_revision: NonNegativeInt
    beats: tuple[DirectorBeat, ...] = ()
    local_canon: tuple[LocalCanonProposal, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def entries_are_unique(self):
        _ensure_unique([beat.beat_id for beat in self.beats], "beats.beat_id")
        _ensure_unique(
            [proposal.proposal_id for proposal in self.local_canon],
            "local_canon.proposal_id",
        )
        if any(beat.state_revision != self.state_revision for beat in self.beats):
            raise ValueError("all beats must use the plan state_revision")
        if any(
            proposal.state_revision != self.state_revision
            for proposal in self.local_canon
        ):
            raise ValueError("all local canon proposals must use the plan state_revision")
        return self


class NpcTurn(ProtocolModel):
    """Future NPC-agent output; facts remain proposals until extraction."""

    npc_turn_id: MachineId = Field(default_factory=lambda: new_protocol_id("npc"))
    state_revision: NonNegativeInt
    actor_id: NonEmptyStr
    utterance: str = ""
    action: str = ""
    target_ids: tuple[NonEmptyStr, ...] = ()
    proposed_facts: tuple[NonEmptyStr, ...] = ()
    memory_notes: tuple[NonEmptyStr, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def turn_is_coherent(self):
        if not self.utterance.strip() and not self.action.strip():
            raise ValueError("an NPC turn needs an utterance or action")
        _ensure_unique(list(self.target_ids), "target_ids")
        _ensure_unique(list(self.proposed_facts), "proposed_facts")
        return self


class FactAuthority(str, Enum):
    """Provenance of committed truth, not permission requested by an LLM."""

    IRON_LAW = "iron_law"
    LOCAL_CANON = "local_canon"
    CANON_ANCHOR = "canon_anchor"


class CommittedChange(ProtocolModel):
    path: NonEmptyStr
    previous: Any
    new: Any
    authority: FactAuthority
    source: MachineId
    reason: str = ""


class CommittedDirectorBeat(ProtocolModel):
    beat_id: MachineId
    validation_id: MachineId
    kind: DirectorBeatKind
    actor_id: NonEmptyStr
    target_location_id: NonEmptyStr
    narrative_hint: NonEmptyStr
    committed_changes: tuple[CommittedChange, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION


class CommittedLocalCanon(ProtocolModel):
    proposal_id: MachineId
    validation_id: MachineId
    kind: LocalCanonKind
    entity_id: MachineId
    archetype_id: MachineId
    name: NonEmptyStr
    parent_location_id: NonEmptyStr
    narrative_hint: NonEmptyStr
    committed_changes: tuple[CommittedChange, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION


class CommittedTurn(ProtocolModel):
    """Provider-neutral record of truth after one successful atomic commit."""

    commit_id: MachineId = Field(default_factory=lambda: new_protocol_id("commit"))
    batch_id: MachineId
    turn_no: NonNegativeInt
    state_revision_before: NonNegativeInt
    state_revision_after: NonNegativeInt
    scene_before: NonEmptyStr
    scene_after: NonEmptyStr
    narrative: NonEmptyStr
    committed_changes: tuple[CommittedChange, ...] = ()
    director_beats: tuple[CommittedDirectorBeat, ...] = ()
    local_canon: tuple[CommittedLocalCanon, ...] = ()
    anchor_ids: tuple[MachineId, ...] = ()
    new_facts: tuple[NonEmptyStr, ...] = ()
    ending: NonEmptyStr | None = None
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def revisions_are_monotonic(self):
        if self.state_revision_after <= self.state_revision_before:
            raise ValueError("a committed turn must advance state_revision")
        _ensure_unique(list(self.anchor_ids), "anchor_ids")
        _ensure_unique(list(self.new_facts), "new_facts")
        return self


def fact_batch_json_schema() -> dict[str, Any]:
    return copy.deepcopy(FactBatch.model_json_schema())


def suggested_action_json_schema() -> dict[str, Any]:
    return copy.deepcopy(SuggestedAction.model_json_schema())


def director_plan_json_schema() -> dict[str, Any]:
    return copy.deepcopy(DirectorPlan.model_json_schema())


def npc_turn_json_schema() -> dict[str, Any]:
    return copy.deepcopy(NpcTurn.model_json_schema())
