"""Code-level admission for facts extracted from generated prose.

The extractor is untrusted. This module is the only Phase 2 component allowed
to translate supported extracted facts into a ``FactBatch`` state patch.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .content import Story
from .llm_protocol import (
    CharacterMoveFact,
    EntityKind,
    FactBatch,
    FactExtraction,
    PhysicalFactDomain,
    PhysicalFactViolation,
    ItemPlacement,
    ItemTransferFact,
    PerceptionSnapshot,
)
from .state import set_value


@dataclass(frozen=True)
class FactValidationResult:
    batch: FactBatch | None
    violations: tuple[PhysicalFactViolation, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.batch is not None and not self.violations


def _violation(
    code: str,
    domain: PhysicalFactDomain,
    message: str,
    *,
    retryable: bool = True,
    path: str | None = None,
    evidence: str | None = None,
) -> PhysicalFactViolation:
    return PhysicalFactViolation(
        code=code,
        domain=domain,
        message=message,
        retryable=retryable,
        path=path,
        evidence=evidence,
    )


def build_fact_ledger(
    story: Story,
    state: dict[str, Any],
    perception: PerceptionSnapshot,
) -> dict[str, Any]:
    """Build the minimum engine ledger sent to the extraction-only prompt."""
    visible_character_ids = {
        entity.entity_id
        for entity in perception.visible_entities
        if entity.kind == EntityKind.CHARACTER
    }
    visible_character_ids.add(perception.subject_id)
    visible_item_ids = {
        entity.entity_id
        for entity in (*perception.visible_entities, *perception.inventory)
        if entity.kind == EntityKind.ITEM
    }
    positions = state.get("positions") or {}
    item_locations = state.get("item_locations") or {}
    return {
        "supported_fact_kinds": [
            "character_move",
            "item_transfer",
        ],
        "current_location": {
            "id": perception.location_id,
            "name": perception.location_name,
        },
        "available_destinations": [
            {"id": entity.entity_id, "label": entity.label}
            for entity in perception.visible_entities
            if entity.kind == EntityKind.EXIT
        ],
        "characters": [
            {
                "id": character_id,
                "name": story.character_name(character_id),
                "location_id": positions.get(character_id),
            }
            for character_id in sorted(visible_character_ids)
        ],
        "items": [
            {
                "id": item_id,
                "name": story.item_labels().get(item_id, item_id),
                "placement": copy.deepcopy(item_locations.get(item_id)),
            }
            for item_id in sorted(visible_item_ids)
        ],
        "world_boundaries": list(
            (story.data.get("player_role") or {}).get("constraints") or []
        ) + list((story.data.get("global_rules") or {}).get("boundaries") or []),
    }


def _placement_dict(placement: ItemPlacement) -> dict[str, Any]:
    return {"type": placement.type, "id": placement.id}


def _normalized_placement(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {"type": value.get("type"), "id": value.get("id")}


def _placement_node(
    placement: dict[str, Any],
    positions: dict[str, Any],
) -> str | None:
    placement_type = placement.get("type")
    placement_id = placement.get("id")
    if placement_type == "board" and isinstance(placement_id, str):
        return placement_id
    if placement_type == "carried_by" and isinstance(placement_id, str):
        node = positions.get(placement_id)
        return str(node) if isinstance(node, str) else None
    return None


def _fact_domain(fact: Any) -> PhysicalFactDomain:
    if isinstance(fact, CharacterMoveFact):
        return PhysicalFactDomain.PRESENCE
    if isinstance(fact, ItemTransferFact):
        return PhysicalFactDomain.ITEM_CUSTODY
    return PhysicalFactDomain.REVISION


def validate_fact_extraction(
    story: Story,
    state: dict[str, Any],
    extraction: FactExtraction,
    *,
    player_text: str,
    narrative: str,
    references: tuple[str, ...],
    perception: PerceptionSnapshot,
) -> FactValidationResult:
    """Validate every extracted fact and build an all-or-nothing batch."""
    if extraction.state_revision != perception.state_revision:
        return FactValidationResult(
            batch=None,
            violations=(_violation(
                "physical.stale_extraction",
                PhysicalFactDomain.REVISION,
                (
                    f"事实抽取基于修订 {extraction.state_revision}，"
                    f"当前快照是 {perception.state_revision}。"
                ),
                retryable=False,
            ),),
        )

    working = copy.deepcopy(state)
    changes: dict[str, Any] = {}
    violations: list[PhysicalFactViolation] = []
    reference_list = list(dict.fromkeys(references))
    reference_set = set(reference_list)
    visible_characters = {
        entity.entity_id
        for entity in perception.visible_entities
        if entity.kind == EntityKind.CHARACTER
    } | {perception.subject_id}
    visible_items = {
        entity.entity_id
        for entity in (*perception.visible_entities, *perception.inventory)
        if entity.kind == EntityKind.ITEM
    }
    moved_actors: set[str] = set()
    transferred_items: set[str] = set()

    def add_reference(entity_id: str | None) -> None:
        if entity_id and entity_id not in reference_set:
            reference_set.add(entity_id)
            reference_list.append(entity_id)

    def reject(
        fact: Any,
        code: str,
        message: str,
        *,
        path: str | None = None,
        retryable: bool = True,
    ) -> None:
        violations.append(_violation(
            code,
            _fact_domain(fact),
            message,
            retryable=retryable,
            path=path,
            evidence=getattr(fact, "evidence", None),
        ))

    ranks = {
        "character_move": 0,
        "item_transfer": 1,
    }
    ordered = sorted(extraction.facts, key=lambda fact: ranks[fact.kind])
    for fact in ordered:
        if isinstance(fact, CharacterMoveFact):
            if fact.actor_id not in story.characters:
                reject(fact, "physical.character_unknown", "移动事实引用了未知人物。")
                continue
            if fact.actor_id not in visible_characters:
                reject(fact, "physical.character_not_visible", "人物不在本回合可见范围内。")
                continue
            if fact.actor_id in moved_actors:
                reject(fact, "physical.character_moved_twice", "同一人物不能在一批事实中移动两次。")
                continue
            source = (working.get("positions") or {}).get(fact.actor_id)
            if not isinstance(source, str):
                reject(fact, "physical.character_unplaced", "人物没有权威位置。")
                continue
            if fact.destination_id == source:
                # A redundant extraction is not a narrative contradiction and
                # should not discard an otherwise valid turn.
                continue
            known_locations = set(story.scenes)
            if fact.destination_id not in known_locations:
                reject(
                    fact,
                    "physical.location_unknown",
                    "移动目标不是作者定义的地点。",
                    path=f"positions.{fact.actor_id}",
                )
                continue
            path = f"positions.{fact.actor_id}"
            set_value(working, path, fact.destination_id)
            changes[path] = fact.destination_id
            moved_actors.add(fact.actor_id)
            add_reference(fact.actor_id)
            add_reference(fact.destination_id)
            continue

        if isinstance(fact, ItemTransferFact):
            if fact.item_id not in story.items:
                reject(fact, "physical.item_unknown", "物品转移引用了未知关键物品。")
                continue
            if fact.item_id not in visible_items:
                reject(fact, "physical.item_not_visible", "关键物品不在本回合可见或持有范围内。")
                continue
            if fact.item_id in transferred_items:
                reject(fact, "physical.item_transferred_twice", "同一物品不能在一批事实中转移两次。")
                continue
            item = story.items.get(fact.item_id) or {}
            if item.get("portable") is not True:
                reject(fact, "physical.item_not_portable", "该物品被作者声明为不可携带。")
                continue
            current = _normalized_placement(
                (working.get("item_locations") or {}).get(fact.item_id)
            )
            expected = _placement_dict(fact.from_placement)
            if current != expected:
                reject(
                    fact,
                    "physical.item_custody_stale",
                    "物品当前归属与抽取声明的来源不一致。",
                    path=f"item_locations.{fact.item_id}",
                    retryable=False,
                )
                continue
            destination = _placement_dict(fact.to_placement)
            if destination["type"] not in {"board", "carried_by"}:
                reject(
                    fact,
                    "physical.item_destination_unsupported",
                    "Phase 2 最小闭环只允许转交人物或放到已知地点。",
                )
                continue
            positions = working.get("positions") or {}
            source_node = _placement_node(current or {}, positions)
            destination_node = _placement_node(destination, positions)
            if destination["type"] == "carried_by" and destination["id"] not in story.characters:
                reject(fact, "physical.item_recipient_unknown", "物品接收者不是作者人物。")
                continue
            known_locations = set(story.scenes)
            if destination["type"] == "board" and destination["id"] not in known_locations:
                reject(fact, "physical.item_location_unknown", "物品目标地点不存在。")
                continue
            if source_node is None or source_node != destination_node:
                reject(
                    fact,
                    "physical.item_not_co_present",
                    "物品与接收者不在同一地点，不能直接完成转移。",
                )
                continue
            path = f"item_locations.{fact.item_id}"
            set_value(working, path, destination)
            changes[path] = destination
            transferred_items.add(fact.item_id)
            add_reference(fact.item_id)
            add_reference(fact.from_placement.id)
            add_reference(fact.to_placement.id)
            continue

    if violations:
        return FactValidationResult(batch=None, violations=tuple(violations))
    return FactValidationResult(
        batch=FactBatch(
            state_revision=perception.state_revision,
            player_text=player_text,
            narrative=narrative,
            state_changes=changes,
            references=tuple(reference_list),
            extracted_facts=extraction.facts,
        ),
    )
