"""Local Canon: validated generative facts (authoring-contract section 4).

v1 admits exactly two kinds — generated locations and local situations —
proposed through the Director channel. Admission is deterministic: archetype
membership, hard budgets, id-namespace and canon-conflict checks, then an
atomic commit into the state's ``generated`` namespace, which participates in
checkpoints and branches like any other state. Characters can never be
created here: important people belong to the author (contract section 2.5),
and the ``LocalCanonKind`` enum simply has no character member.

The engine knows archetype *shapes* (labels, parents, budgets, lifetimes);
every word inside them belongs to the story.
"""

from __future__ import annotations

from typing import Any

from .content import Story
from .llm_protocol import (
    CommittedChange,
    CommittedLocalCanon,
    GENERATED_ENTITY_PREFIX,
    FactAuthority,
    IssueSeverity,
    LocalCanonKind,
    LocalCanonProposal,
    LocalCanonValidation,
    ValidationIssue,
    new_protocol_id,
)
from .state import set_value

# Hard throttle: at most this many admitted proposals per committed turn.
MAX_LOCAL_CANON_PER_TURN = 1

_BUDGET_KEYS = {
    LocalCanonKind.LOCATION: "locations",
    LocalCanonKind.SITUATION: "situations",
}


def generated_locations(state: dict[str, Any]) -> dict[str, Any]:
    return dict(((state.get("generated") or {}).get("locations") or {}))


def generated_situations(state: dict[str, Any]) -> dict[str, Any]:
    return dict(((state.get("generated") or {}).get("situations") or {}))


def active_situations_at(state: dict[str, Any], location_id: str) -> dict[str, Any]:
    return {
        situation_id: record
        for situation_id, record in generated_situations(state).items()
        if isinstance(record, dict)
        and record.get("active")
        and record.get("parent") == location_id
    }


def _archetypes(story: Story, kind: LocalCanonKind) -> dict[str, Any]:
    if kind == LocalCanonKind.LOCATION:
        return story.location_archetypes
    return story.situation_archetypes


def used_budget(state: dict[str, Any], kind: LocalCanonKind) -> int:
    """Every record ever admitted on this branch counts; expiry frees nothing.

    A soft-recycling budget would let churn manufacture unlimited entities;
    the hard total is the promise the author actually configured.
    """
    if kind == LocalCanonKind.LOCATION:
        return len(generated_locations(state))
    return len(generated_situations(state))


def remaining_budget(story: Story, state: dict[str, Any], kind: LocalCanonKind) -> int:
    total = story.generation_budget(_BUDGET_KEYS[kind])
    return max(0, total - used_budget(state, kind))


def generation_context(story: Story, state: dict[str, Any]) -> dict[str, Any]:
    """Provider-facing summary of what may be generated right now.

    Empty when the author declared no generation block: no declaration means
    no generative facts, not a default allowance.
    """
    if not story.generation:
        return {}
    context: dict[str, Any] = {
        "entity_id_prefix": GENERATED_ENTITY_PREFIX,
        "max_per_turn": MAX_LOCAL_CANON_PER_TURN,
        "budgets": {},
        "location_archetypes": [],
        "situation_archetypes": [],
        "existing_entities": sorted(
            list(generated_locations(state)) + list(generated_situations(state))
        ),
    }
    for kind in LocalCanonKind:
        key = _BUDGET_KEYS[kind]
        context["budgets"][key] = {
            "total": story.generation_budget(key),
            "remaining": remaining_budget(story, state, kind),
        }
    for archetype_id, spec in story.location_archetypes.items():
        spec = spec if isinstance(spec, dict) else {}
        context["location_archetypes"].append({
            "archetype_id": str(archetype_id),
            "label": str(spec.get("label") or archetype_id),
            "description_hint": str(spec.get("description_hint") or ""),
            "allowed_parents": [str(p) for p in (spec.get("allowed_parents") or [])],
        })
    for archetype_id, spec in story.situation_archetypes.items():
        spec = spec if isinstance(spec, dict) else {}
        context["situation_archetypes"].append({
            "archetype_id": str(archetype_id),
            "label": str(spec.get("label") or archetype_id),
            "description_hint": str(spec.get("description_hint") or ""),
            "allowed_locations": [str(p) for p in (spec.get("allowed_locations") or [])],
            "max_duration_turns": spec.get("max_duration_turns"),
        })
    return context


def _issue(code: str, message: str) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=IssueSeverity.ERROR,
        message=message,
        retryable=True,
    )


def _authored_location_ids(story: Story) -> set[str]:
    return set(story.world_nodes) | set(story.scenes)


def _authored_names(story: Story) -> set[str]:
    names: set[str] = set()
    for char_id in story.characters:
        names.add(story.character_name(char_id))
    names.update(story.item_labels().values())
    for node in story.world_nodes.values():
        if isinstance(node, dict) and node.get("name"):
            names.add(str(node["name"]))
    for scene in story.scenes.values():
        if isinstance(scene, dict) and scene.get("name"):
            names.add(str(scene["name"]))
    return names


def effective_lifetime(story: Story, proposal: LocalCanonProposal) -> int | None:
    """Turns a situation stays active; None means persistent-local."""
    if proposal.kind != LocalCanonKind.SITUATION:
        return None
    archetype = story.situation_archetypes.get(proposal.archetype_id) or {}
    maximum = archetype.get("max_duration_turns")
    if proposal.expires_after_turns is not None:
        return int(proposal.expires_after_turns)
    return int(maximum) if isinstance(maximum, int) else None


def validate_local_canon(
    story: Story,
    state: dict[str, Any],
    proposal: LocalCanonProposal,
    *,
    state_revision: int,
) -> LocalCanonValidation:
    """Deterministic admission check; nothing here mutates live state."""
    issues: list[ValidationIssue] = []

    if proposal.state_revision != state_revision:
        issues.append(_issue(
            "local_canon.stale_proposal",
            f"提议基于修订 {proposal.state_revision}，当前是 {state_revision}。",
        ))

    archetype = _archetypes(story, proposal.kind).get(proposal.archetype_id)
    if not isinstance(archetype, dict):
        issues.append(_issue(
            "local_canon.archetype_unknown",
            f"原型 '{proposal.archetype_id}' 不在作者声明的可生成清单中。",
        ))
        archetype = {}

    if remaining_budget(story, state, proposal.kind) <= 0:
        issues.append(_issue(
            "local_canon.budget_exhausted",
            f"'{_BUDGET_KEYS[proposal.kind]}' 的生成预算已用完。",
        ))

    taken = (
        set(story.characters)
        | set(story.items)
        | _authored_location_ids(story)
        | set(generated_locations(state))
        | set(generated_situations(state))
    )
    if proposal.entity_id in taken:
        issues.append(_issue(
            "local_canon.entity_id_taken",
            f"实体 ID '{proposal.entity_id}' 已存在，不能重复创建。",
        ))

    if proposal.name in _authored_names(story) or any(
        isinstance(record, dict) and record.get("name") == proposal.name
        for record in list(generated_locations(state).values())
        + list(generated_situations(state).values())
    ):
        issues.append(_issue(
            "local_canon.name_conflicts_canon",
            f"名称「{proposal.name}」与既有实体冲突，会造成叙事混淆。",
        ))

    authored = _authored_location_ids(story)
    if proposal.parent_location_id not in authored:
        # v1 depth limit: generated facts attach only to authored locations,
        # never to other generated ones.
        issues.append(_issue(
            "local_canon.parent_not_authored",
            "生成实体只能挂在作者定义的地点上。",
        ))
    else:
        allowed_key = (
            "allowed_parents"
            if proposal.kind == LocalCanonKind.LOCATION
            else "allowed_locations"
        )
        allowed = [str(p) for p in (archetype.get(allowed_key) or [])]
        if allowed and proposal.parent_location_id not in allowed:
            issues.append(_issue(
                "local_canon.parent_not_allowed",
                f"原型 '{proposal.archetype_id}' 不允许出现在 "
                f"'{proposal.parent_location_id}'。",
            ))

    if proposal.kind == LocalCanonKind.SITUATION:
        maximum = archetype.get("max_duration_turns")
        if (
            isinstance(maximum, int)
            and proposal.expires_after_turns is not None
            and proposal.expires_after_turns > maximum
        ):
            issues.append(_issue(
                "local_canon.lifetime_exceeds_archetype",
                f"局势持续 {proposal.expires_after_turns} 回合，超过原型上限 {maximum}。",
            ))

    return LocalCanonValidation(
        validation_id=new_protocol_id("lcval"),
        proposal_id=proposal.proposal_id,
        state_revision=state_revision,
        can_commit=not issues,
        issues=tuple(issues),
    )


def commit_local_canon(
    story: Story,
    state: dict[str, Any],
    proposal: LocalCanonProposal,
    validation: LocalCanonValidation,
    *,
    turn_no: int,
    source: str = "director",
) -> CommittedLocalCanon:
    """Atomically admit one validated proposal into the generated namespace."""
    if not validation.can_commit or validation.proposal_id != proposal.proposal_id:
        raise ValueError("local canon commit requires its own passing validation")

    record: dict[str, Any] = {
        "kind": proposal.kind.value,
        "archetype": proposal.archetype_id,
        "name": proposal.name,
        "description": proposal.description,
        "parent": proposal.parent_location_id,
        "created_turn": turn_no,
        "source": source,
        "reason": proposal.reason,
    }
    if proposal.kind == LocalCanonKind.SITUATION:
        lifetime = effective_lifetime(story, proposal)
        record["active"] = True
        record["expires_at_turn"] = (
            turn_no + lifetime if lifetime is not None else None
        )
        path = f"generated.situations.{proposal.entity_id}"
    else:
        path = f"generated.locations.{proposal.entity_id}"
    set_value(state, path, record)

    committed_change = CommittedChange(
        path=path,
        previous=None,
        new=record,
        authority=FactAuthority.LOCAL_CANON,
        source=f"local_canon.{proposal.entity_id}",
        reason=proposal.reason,
    )
    return CommittedLocalCanon(
        proposal_id=proposal.proposal_id,
        validation_id=validation.validation_id,
        kind=proposal.kind,
        entity_id=proposal.entity_id,
        archetype_id=proposal.archetype_id,
        name=proposal.name,
        parent_location_id=proposal.parent_location_id,
        narrative_hint=proposal.description,
        committed_changes=(committed_change,),
    )


def expire_situations(
    state: dict[str, Any],
    turn_no: int,
) -> list[tuple[str, dict[str, Any]]]:
    """Deactivate situations whose lifetime ended; records stay as provenance."""
    expired: list[tuple[str, dict[str, Any]]] = []
    situations = (state.get("generated") or {}).get("situations") or {}
    for situation_id, record in situations.items():
        if not isinstance(record, dict) or not record.get("active"):
            continue
        expires_at = record.get("expires_at_turn")
        if isinstance(expires_at, int) and turn_no >= expires_at:
            record["active"] = False
            expired.append((situation_id, record))
    return expired
