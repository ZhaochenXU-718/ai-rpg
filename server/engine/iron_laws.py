"""Code-level admission for facts extracted from generated prose.

The extractor is untrusted. This module is the only Phase 2 component allowed
to translate supported extracted facts into a ``FactBatch`` state patch.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from .content import Story
from .llm_protocol import (
    CharacterMoveFact,
    CommitmentFact,
    CommitmentUpdateFact,
    EntityKind,
    FactBatch,
    FactExtraction,
    IronLawDomain,
    IronLawViolation,
    ItemPlacement,
    ItemTransferFact,
    PerceptionSnapshot,
    SecretDisclosureFact,
)
from .state import set_value


@dataclass(frozen=True)
class FactValidationResult:
    batch: FactBatch | None
    violations: tuple[IronLawViolation, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.batch is not None and not self.violations


def secret_id_for_character(character_id: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_-]+", "_", character_id).strip("_-")
    return f"secret_{segment or 'character'}"


def _violation(
    code: str,
    domain: IronLawDomain,
    message: str,
    *,
    retryable: bool = True,
    path: str | None = None,
    evidence: str | None = None,
) -> IronLawViolation:
    return IronLawViolation(
        code=code,
        domain=domain,
        message=message,
        retryable=retryable,
        path=path,
        evidence=evidence,
    )


def _secret_catalog(story: Story) -> dict[str, dict[str, str]]:
    catalog: dict[str, dict[str, str]] = {}
    for owner_id, character in story.characters.items():
        secret = character.get("secret") if isinstance(character, dict) else None
        if isinstance(secret, str) and secret.strip():
            secret_id = secret_id_for_character(owner_id)
            catalog[secret_id] = {"owner_id": owner_id, "content": secret.strip()}
    return catalog


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
    secrets = _secret_catalog(story)
    relevant_secrets = [
        {
            "secret_id": secret_id,
            "owner_id": record["owner_id"],
            "content": record["content"],
        }
        for secret_id, record in secrets.items()
        if record["owner_id"] in visible_character_ids
    ]
    commitments = [
        {"commitment_id": commitment_id, **copy.deepcopy(record)}
        for commitment_id, record in (state.get("commitments") or {}).items()
        if isinstance(record, dict)
        and (
            record.get("promisor_id") in visible_character_ids
            or record.get("promisee_id") in visible_character_ids
        )
    ]
    return {
        "supported_fact_kinds": [
            "character_move",
            "item_transfer",
            "secret_disclosure",
            "commitment",
            "commitment_update",
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
        "secrets": relevant_secrets,
        "commitments": commitments,
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


def _fact_domain(fact: Any) -> IronLawDomain:
    if isinstance(fact, CharacterMoveFact):
        return IronLawDomain.PRESENCE
    if isinstance(fact, ItemTransferFact):
        return IronLawDomain.ITEM_CUSTODY
    if isinstance(fact, SecretDisclosureFact):
        return IronLawDomain.DISCLOSURE
    if isinstance(fact, (CommitmentFact, CommitmentUpdateFact)):
        return IronLawDomain.COMMITMENT
    return IronLawDomain.WORLD_BOUNDARY


def validate_fact_extraction(
    story: Story,
    state: dict[str, Any],
    extraction: FactExtraction,
    *,
    player_text: str,
    narrative: str,
    references: tuple[str, ...],
    perception: PerceptionSnapshot,
    turn_no: int,
) -> FactValidationResult:
    """Validate every extracted fact and build an all-or-nothing batch."""
    if extraction.state_revision != perception.state_revision:
        return FactValidationResult(
            batch=None,
            violations=(_violation(
                "iron.stale_extraction",
                IronLawDomain.IRREVERSIBLE,
                (
                    f"事实抽取基于修订 {extraction.state_revision}，"
                    f"当前快照是 {perception.state_revision}。"
                ),
                retryable=False,
            ),),
        )

    working = copy.deepcopy(state)
    changes: dict[str, Any] = {}
    player_facts: list[str] = []
    violations: list[IronLawViolation] = []
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
    secret_catalog = _secret_catalog(story)
    moved_actors: set[str] = set()
    transferred_items: set[str] = set()

    def add_reference(entity_id: str | None) -> None:
        if entity_id and entity_id not in reference_set:
            reference_set.add(entity_id)
            reference_list.append(entity_id)

    def add_player_fact(text: str) -> None:
        text = " ".join(text.split())
        if text and text not in player_facts:
            player_facts.append(text)

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
        "secret_disclosure": 2,
        "commitment": 3,
        "commitment_update": 4,
    }
    ordered = sorted(extraction.facts, key=lambda fact: ranks[fact.kind])
    for fact in ordered:
        if fact.evidence not in narrative:
            reject(
                fact,
                "iron.evidence_not_in_narrative",
                "抽取证据不是本回合散文的原文片段。",
                retryable=False,
            )
            continue

        if isinstance(fact, CharacterMoveFact):
            if fact.actor_id not in story.characters:
                reject(fact, "iron.character_unknown", "移动事实引用了未知人物。")
                continue
            if fact.actor_id not in visible_characters:
                reject(fact, "iron.character_not_visible", "人物不在本回合可见范围内。")
                continue
            if fact.actor_id in moved_actors:
                reject(fact, "iron.character_moved_twice", "同一人物不能在一批事实中移动两次。")
                continue
            source = (working.get("positions") or {}).get(fact.actor_id)
            if not isinstance(source, str):
                reject(fact, "iron.character_unplaced", "人物没有权威位置。")
                continue
            if fact.destination_id == source:
                reject(fact, "iron.move_noop", "人物已经在目标地点。")
                continue
            available = {
                str(exit_spec.get("to"))
                for exit_spec in story.available_exits(working, source)
                if isinstance(exit_spec, dict)
            }
            if fact.destination_id not in available:
                reject(
                    fact,
                    "iron.route_not_adjacent",
                    "目标地点不是人物当前位置可达的相邻出口。",
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
                reject(fact, "iron.item_unknown", "物品转移引用了未知关键物品。")
                continue
            if fact.item_id not in visible_items:
                reject(fact, "iron.item_not_visible", "关键物品不在本回合可见或持有范围内。")
                continue
            if fact.item_id in transferred_items:
                reject(fact, "iron.item_transferred_twice", "同一物品不能在一批事实中转移两次。")
                continue
            item = story.items.get(fact.item_id) or {}
            if item.get("portable") is not True:
                reject(fact, "iron.item_not_portable", "该物品被作者声明为不可携带。")
                continue
            current = _normalized_placement(
                (working.get("item_locations") or {}).get(fact.item_id)
            )
            expected = _placement_dict(fact.from_placement)
            if current != expected:
                reject(
                    fact,
                    "iron.item_custody_stale",
                    "物品当前归属与抽取声明的来源不一致。",
                    path=f"item_locations.{fact.item_id}",
                    retryable=False,
                )
                continue
            destination = _placement_dict(fact.to_placement)
            if destination["type"] not in {"board", "carried_by"}:
                reject(
                    fact,
                    "iron.item_destination_unsupported",
                    "Phase 2 最小闭环只允许转交人物或放到已知地点。",
                )
                continue
            positions = working.get("positions") or {}
            source_node = _placement_node(current or {}, positions)
            destination_node = _placement_node(destination, positions)
            if destination["type"] == "carried_by" and destination["id"] not in story.characters:
                reject(fact, "iron.item_recipient_unknown", "物品接收者不是作者人物。")
                continue
            known_locations = (
                set(story.world_nodes)
                | set(story.scenes)
                | set(story.generated_locations(working))
            )
            if destination["type"] == "board" and destination["id"] not in known_locations:
                reject(fact, "iron.item_location_unknown", "物品目标地点不存在。")
                continue
            if source_node is None or source_node != destination_node:
                reject(
                    fact,
                    "iron.item_not_co_present",
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

        if isinstance(fact, SecretDisclosureFact):
            secret = secret_catalog.get(fact.secret_id)
            if secret is None or secret["owner_id"] != fact.owner_id:
                reject(fact, "iron.secret_unknown", "披露事实没有对应的作者秘密。")
                continue
            if fact.disclosed_by_id != fact.owner_id:
                reject(
                    fact,
                    "iron.secret_discloser_unauthorized",
                    "当前最小闭环只允许人物本人披露自己的秘密。",
                )
                continue
            participants = {fact.owner_id, *fact.audience_ids}
            if not participants <= set(story.characters):
                reject(fact, "iron.secret_audience_unknown", "秘密披露包含未知人物。")
                continue
            positions = working.get("positions") or {}
            nodes = {positions.get(participant) for participant in participants}
            if len(nodes) != 1 or None in nodes or fact.owner_id not in visible_characters:
                reject(fact, "iron.secret_not_co_present", "披露者与听众没有同时在场。")
                continue
            path = f"disclosures.{fact.secret_id}"
            existing = copy.deepcopy((working.get("disclosures") or {}).get(fact.secret_id))
            existing_audiences = set(
                (existing or {}).get("audience_ids") or []
                if isinstance(existing, dict)
                else []
            )
            existing_audiences.update(fact.audience_ids)
            record = {
                "owner_id": fact.owner_id,
                "audience_ids": sorted(existing_audiences),
                "summary": fact.summary,
                "disclosed_turn": turn_no,
            }
            set_value(working, path, record)
            changes[path] = record
            if story.player_id in fact.audience_ids:
                add_player_fact(f"{story.character_name(fact.owner_id)}披露：{fact.summary}")
            add_reference(fact.owner_id)
            for audience_id in fact.audience_ids:
                add_reference(audience_id)
            continue

        if isinstance(fact, CommitmentFact):
            if fact.promisor_id == fact.promisee_id:
                reject(fact, "iron.commitment_same_party", "承诺双方不能是同一人物。")
                continue
            parties = {fact.promisor_id, fact.promisee_id}
            if not parties <= set(story.characters):
                reject(fact, "iron.commitment_party_unknown", "承诺引用了未知人物。")
                continue
            if not parties <= visible_characters:
                reject(fact, "iron.commitment_party_not_visible", "承诺双方没有同时进入本回合感知。")
                continue
            positions = working.get("positions") or {}
            if positions.get(fact.promisor_id) != positions.get(fact.promisee_id):
                reject(fact, "iron.commitment_party_absent", "承诺双方不在同一地点。")
                continue
            if fact.commitment_id in (working.get("commitments") or {}):
                reject(
                    fact,
                    "iron.commitment_id_conflict",
                    "承诺 ID 已存在，不能覆盖历史承诺。",
                    retryable=False,
                )
                continue
            if fact.related_item_id is not None:
                if fact.related_item_id not in story.items:
                    reject(fact, "iron.commitment_item_unknown", "承诺关联了未知物品。")
                    continue
                if fact.related_item_id not in visible_items:
                    reject(fact, "iron.commitment_item_not_visible", "承诺关联物品不在可见范围。")
                    continue
            record = {
                "promisor_id": fact.promisor_id,
                "promisee_id": fact.promisee_id,
                "description": fact.description,
                "related_item_id": fact.related_item_id,
                "due": fact.due,
                "status": "open",
                "created_turn": turn_no,
            }
            path = f"commitments.{fact.commitment_id}"
            set_value(working, path, record)
            changes[path] = record
            add_player_fact(
                f"{story.character_name(fact.promisor_id)}向"
                f"{story.character_name(fact.promisee_id)}承诺：{fact.description}"
            )
            add_reference(fact.promisor_id)
            add_reference(fact.promisee_id)
            add_reference(fact.related_item_id)
            continue

        if isinstance(fact, CommitmentUpdateFact):
            existing = copy.deepcopy(
                (working.get("commitments") or {}).get(fact.commitment_id)
            )
            if not isinstance(existing, dict):
                reject(fact, "iron.commitment_unknown", "要更新的承诺不存在。")
                continue
            if existing.get("status") != "open":
                reject(fact, "iron.commitment_already_resolved", "承诺已经有最终状态。")
                continue
            promisor_id = str(existing.get("promisor_id") or "")
            if promisor_id not in visible_characters:
                reject(fact, "iron.commitment_promisor_absent", "承诺人不在本回合可见范围。")
                continue
            related_item_id = existing.get("related_item_id")
            promisee_id = existing.get("promisee_id")
            if fact.status == "fulfilled" and related_item_id:
                placement = (working.get("item_locations") or {}).get(related_item_id)
                if placement != {"type": "carried_by", "id": promisee_id}:
                    reject(
                        fact,
                        "iron.commitment_fulfillment_unproven",
                        "关联物品尚未交给承诺对象，不能标记为已兑现。",
                    )
                    continue
            existing["status"] = fact.status
            existing["resolved_turn"] = turn_no
            path = f"commitments.{fact.commitment_id}"
            set_value(working, path, existing)
            changes[path] = existing
            status_text = {
                "fulfilled": "已兑现",
                "broken": "已爽约",
                "cancelled": "已取消",
            }[fact.status]
            add_player_fact(f"承诺「{existing.get('description', fact.commitment_id)}」{status_text}")
            add_reference(promisor_id)
            add_reference(str(promisee_id or ""))

    if violations:
        return FactValidationResult(batch=None, violations=tuple(violations))
    return FactValidationResult(
        batch=FactBatch(
            state_revision=perception.state_revision,
            player_text=player_text,
            narrative=narrative,
            state_changes=changes,
            facts=tuple(player_facts),
            references=tuple(reference_list),
            extracted_facts=extraction.facts,
        ),
    )
