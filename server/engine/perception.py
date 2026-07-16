"""Subject-scoped perception: the wall between authority and model context.

Player and future NPC prompts share ``PerceptionSnapshot`` but are built
through explicit audience scopes. The builder exposes only facts that belong
to that subject; no snapshot carries an execution capability menu.
"""

from __future__ import annotations

from typing import Any

from .content import Story
from .llm_protocol import (
    EntityKind,
    PerceivedEntity,
    PerceptionAudience,
    PerceptionSnapshot,
)


GROUP_KINDS = {
    "people": EntityKind.CHARACTER,
    "items": EntityKind.ITEM,
    "environment": EntityKind.ENVIRONMENT,
    "states": EntityKind.STATE,
}


def character_public_state(
    story: Story,
    state: dict[str, Any],
    char_id: str,
) -> dict[str, Any]:
    return {}


def _entity(
    entity_id: str,
    label: str,
    kind: EntityKind,
    *,
    actionable: bool,
    description: str = "",
    public_state: dict[str, Any] | None = None,
) -> PerceivedEntity:
    return PerceivedEntity(
        entity_id=entity_id,
        label=label or entity_id,
        kind=kind,
        actionable=actionable,
        description=description,
        public_state=public_state or {},
    )


def _characters_at_for_subject(
    story: Story,
    state: dict[str, Any],
    location_id: str,
    subject_id: str,
) -> list[str]:
    positions = state.get("positions") or {}
    return [
        char_id
        for char_id in story.characters
        if char_id != subject_id and positions.get(char_id) == location_id
    ]


def _default_subject_context(
    story: Story,
    audience: PerceptionAudience,
    subject_id: str,
) -> dict[str, Any]:
    if audience != PerceptionAudience.NPC:
        return {}
    card = story.characters.get(subject_id) or {}
    return {
        key: card[key]
        for key in (
            "motivation",
            "voice",
            "initial_relationship",
            "secret",
        )
        if card.get(key)
    }


def build_subject_perception(
    story: Story,
    state: dict[str, Any],
    *,
    audience: PerceptionAudience,
    subject_id: str,
    session_id: str,
    turn_no: int,
    state_revision: int,
    recent_events: tuple[str, ...] = (),
    known_facts: tuple[str, ...] = (),
    subject_context: dict[str, Any] | None = None,
) -> PerceptionSnapshot:
    """Build a scoped view for one physically placed subject."""
    location_id = str((state.get("positions") or {}).get(subject_id) or "")
    if not location_id:
        raise ValueError(f"perception subject '{subject_id}' has no position")

    characters = _characters_at_for_subject(
        story, state, location_id, subject_id
    )
    scene_objects = story.scene_objects(location_id, state)
    inventory_ids = story.inventory(state, subject_id)
    exit_labels = story.exit_labels(state, location_id)
    actionable = scene_objects | set(characters) | set(inventory_ids) | set(exit_labels)
    entities: list[PerceivedEntity] = []
    seen: set[str] = set()

    for char_id in characters:
        char = story.characters.get(char_id) or {}
        entities.append(_entity(
            char_id,
            story.character_name(char_id),
            EntityKind.CHARACTER,
            actionable=char_id in actionable,
            description=" ".join(str(char.get("public_profile") or "").split()),
            public_state=character_public_state(story, state, char_id),
        ))
        seen.add(char_id)

    for item_id in story.visible_items_at(state, location_id):
        if item_id in inventory_ids:
            continue
        item = story.items.get(item_id) or {}
        placement = (state.get("item_locations") or {}).get(item_id)
        entities.append(_entity(
            item_id,
            story.item_labels().get(item_id, item_id),
            EntityKind.ITEM,
            actionable=item_id in actionable,
            description=str(item.get("description") or ""),
            public_state={"placement": placement} if placement else {},
        ))
        seen.add(item_id)

    labels = story.object_labels(location_id, state)
    for group_name, group in story.scene_object_groups(location_id, state).items():
        kind = GROUP_KINDS.get(group_name, EntityKind.OTHER)
        for obj_id in group:
            if obj_id in seen:
                continue
            entities.append(_entity(
                obj_id,
                labels.get(obj_id, obj_id),
                kind,
                actionable=obj_id in actionable,
            ))
            seen.add(obj_id)

    for node_id, label in exit_labels.items():
        if node_id not in seen:
            entities.append(_entity(
                node_id,
                label,
                EntityKind.EXIT,
                actionable=True,
            ))
            seen.add(node_id)

    inventory = tuple(
        _entity(
            item_id,
            story.item_labels().get(item_id, item_id),
            EntityKind.ITEM,
            actionable=True,
            description=str((story.items.get(item_id) or {}).get("description") or ""),
            public_state={
                "placement": (state.get("item_locations") or {}).get(item_id)
            },
        )
        for item_id in inventory_ids
    )

    if audience == PerceptionAudience.PLAYER:
        goal = story.current_goal(state)
    else:
        goal = str((story.characters.get(subject_id) or {}).get("motivation") or "")

    return PerceptionSnapshot(
        audience=audience,
        subject_id=subject_id,
        story_id=story.id,
        session_id=session_id,
        turn_no=turn_no,
        state_revision=state_revision,
        location_id=location_id,
        location_name=story.location_name(state, location_id),
        current_goal=goal,
        visible_entities=tuple(entities),
        inventory=inventory,
        known_facts=tuple(dict.fromkeys(str(fact) for fact in known_facts)),
        recent_events=tuple(event for event in recent_events if event),
        public_state={},
        subject_context=(
            dict(subject_context)
            if subject_context is not None
            else _default_subject_context(story, audience, subject_id)
        ),
    )


def build_player_perception(
    story: Story,
    state: dict[str, Any],
    *,
    session_id: str,
    turn_no: int,
    state_revision: int,
    recent_events: tuple[str, ...] = (),
) -> PerceptionSnapshot:
    return build_subject_perception(
        story,
        state,
        audience=PerceptionAudience.PLAYER,
        subject_id=story.player_id,
        session_id=session_id,
        turn_no=turn_no,
        state_revision=state_revision,
        recent_events=recent_events,
        known_facts=(),
    )
