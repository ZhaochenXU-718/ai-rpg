"""Narrative-first protocols shared by providers and the world engine.

Protocol 0.5 keeps only the provider-neutral surfaces that are exercised by the
current narrative loop.  Providers may write prose, propose editable cards and
extract physical facts, but only a code-admitted FactBatch may cross into
authoritative state.
"""

from __future__ import annotations

import copy
import uuid
from enum import Enum
from typing import Any, Annotated, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)


PROTOCOL_VERSION = "0.5"
ProtocolVersion = Literal["0.5"]
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


class EntityKind(str, Enum):
    CHARACTER = "character"
    ITEM = "item"
    ENVIRONMENT = "environment"
    STATE = "state"
    EXIT = "exit"
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

    The same schema can carry player or NPC views; the builder that supplies it
    owns the visibility policy. A snapshot never contains a
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


class AuthorCharacterCard(ProtocolModel):
    """Author-private card for portraying one in-scene character.

    Card fields exist for portrayal and direction only; none of them counts
    as something the player already knows.
    """

    character_id: NonEmptyStr
    name: NonEmptyStr
    motivation: str = ""
    voice: str = ""
    initial_relationship: str = ""
    secret: str = ""
    pressure: str = ""
    behavior: str = ""
    mannerisms: str = ""
    narration_notes: str = ""
    dialogue_examples: tuple[NonEmptyStr, ...] = ()


class NarrativeAuthorContext(ProtocolModel):
    """Author blueprint compiled for the narrator, apart from perception.

    It travels only on narrative requests: player perception, action
    suggestions, fact extraction and memory compaction must not receive it.
    """

    story_brief: dict[str, str] = Field(default_factory=dict)
    emotional_contract: str = ""
    active_guidelines: tuple[NonEmptyStr, ...] = ()
    critical_reminders: tuple[NonEmptyStr, ...] = ()
    in_scene_character_cards: tuple[AuthorCharacterCard, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def cards_are_unique(self):
        _ensure_unique(
            [card.character_id for card in self.in_scene_character_cards],
            "in_scene_character_cards",
        )
        return self


class MemoryContextEvent(ProtocolModel):
    """One cropped raw event carried only as soft generation context."""

    turn_no: NonNegativeInt
    player_text: NonEmptyStr
    narrative: NonEmptyStr
    scene_before: NonEmptyStr
    scene_after: NonEmptyStr


class MemoryContext(ProtocolModel):
    """Revision-bound soft memory for prose and suggestion generation only."""

    subject_id: NonEmptyStr
    turn_no: NonNegativeInt
    state_revision: NonNegativeInt
    compacted_through_turn: NonNegativeInt = 0
    rolling_summary: str = ""
    open_loops: tuple[NonEmptyStr, ...] = ()
    character_notes: dict[str, tuple[NonEmptyStr, ...]] = Field(
        default_factory=dict
    )
    scene_notes: dict[str, tuple[NonEmptyStr, ...]] = Field(
        default_factory=dict
    )
    recently_resolved: tuple[NonEmptyStr, ...] = ()
    uncompacted_events: tuple[MemoryContextEvent, ...] = ()
    text_chars: NonNegativeInt = 0
    truncated: bool = False
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def event_window_is_ordered_and_current(self):
        turns = [event.turn_no for event in self.uncompacted_events]
        if turns != sorted(set(turns)):
            raise ValueError("memory context events must be unique and ordered")
        if self.compacted_through_turn > self.turn_no:
            raise ValueError("memory digest cannot extend past the current turn")
        if any(
            turn <= self.compacted_through_turn or turn > self.turn_no
            for turn in turns
        ):
            raise ValueError("memory context event is outside its turn window")
        return self


class SuggestedAction(ProtocolModel):
    """Editable prose inspiration, never a frozen execution plan."""

    suggestion_id: MachineId
    perception_revision: NonNegativeInt
    title: NonEmptyStr
    action_text: NonEmptyStr
    focus: MachineId
    rationale: NonEmptyStr
    protocol_version: ProtocolVersion = PROTOCOL_VERSION


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


class ItemPlacement(ProtocolModel):
    """One authoritative item placement used for optimistic custody checks."""

    type: Literal["board", "carried_by"]
    id: NonEmptyStr


class CharacterMoveFact(ProtocolModel):
    fact_id: MachineId = Field(default_factory=lambda: new_protocol_id("fact"))
    kind: Literal["character_move"] = "character_move"
    actor_id: NonEmptyStr
    destination_id: NonEmptyStr
    evidence: NonEmptyStr


class ItemTransferFact(ProtocolModel):
    fact_id: MachineId = Field(default_factory=lambda: new_protocol_id("fact"))
    kind: Literal["item_transfer"] = "item_transfer"
    item_id: NonEmptyStr
    from_placement: ItemPlacement
    to_placement: ItemPlacement
    evidence: NonEmptyStr

    @model_validator(mode="after")
    def placement_changes(self):
        if self.from_placement == self.to_placement:
            raise ValueError("item transfer needs different placements")
        return self


ExtractedFact = Annotated[
    Union[
        CharacterMoveFact,
        ItemTransferFact,
    ],
    Field(discriminator="kind"),
]


class FactExtraction(ProtocolModel):
    """Untrusted facts extracted from one prose candidate."""

    extraction_id: MachineId = Field(default_factory=lambda: new_protocol_id("extract"))
    state_revision: NonNegativeInt
    facts: tuple[ExtractedFact, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def fact_ids_are_unique(self):
        _ensure_unique([fact.fact_id for fact in self.facts], "facts.fact_id")
        return self


class FactBatch(ProtocolModel):
    """Facts admitted by code and waiting at the atomic commit boundary.

    Providers return ``FactExtraction``. Only the iron-law checker may turn
    that untrusted extraction into state changes carried by this model.
    """

    batch_id: MachineId = Field(default_factory=lambda: new_protocol_id("batch"))
    state_revision: NonNegativeInt
    player_text: NonEmptyStr
    narrative: NonEmptyStr
    state_changes: dict[str, Any] = Field(default_factory=dict)
    references: tuple[NonEmptyStr, ...] = ()
    extracted_facts: tuple[ExtractedFact, ...] = ()
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def entries_are_unique(self):
        _ensure_unique(list(self.references), "references")
        _ensure_unique(
            [fact.fact_id for fact in self.extracted_facts],
            "extracted_facts.fact_id",
        )
        return self


class PhysicalFactDomain(str, Enum):
    PRESENCE = "presence"
    ITEM_CUSTODY = "item_custody"
    REVISION = "revision"


class PhysicalFactViolation(ProtocolModel):
    """Code-level rejection emitted before a FactBatch may commit."""

    code: MachineId
    domain: PhysicalFactDomain
    message: NonEmptyStr
    retryable: bool = True
    path: NonEmptyStr | None = None
    evidence: NonEmptyStr | None = None
    protocol_version: ProtocolVersion = PROTOCOL_VERSION


class CommittedChange(ProtocolModel):
    path: NonEmptyStr
    previous: Any
    new: Any


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
    protocol_version: ProtocolVersion = PROTOCOL_VERSION

    @model_validator(mode="after")
    def revisions_are_monotonic(self):
        if self.state_revision_after <= self.state_revision_before:
            raise ValueError("a committed turn must advance state_revision")
        return self


def fact_batch_json_schema() -> dict[str, Any]:
    return copy.deepcopy(FactBatch.model_json_schema())


def fact_extraction_json_schema() -> dict[str, Any]:
    return copy.deepcopy(FactExtraction.model_json_schema())


def suggested_action_json_schema() -> dict[str, Any]:
    return copy.deepcopy(SuggestedAction.model_json_schema())
