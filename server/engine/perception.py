"""Player perception: the single wall between authoritative state and eyes.

Builds the protocol ``PlayerPerception`` from full state, and provides the
disclosure filter used by every player-facing surface (quote card, status
bar, LLM prompt).  The rule is structural: anything that is not derivable
from this module is not player-visible, no matter which UI renders it.

Which state keys count as public is declared by the story's ``perception``
block, never hardcoded here — the engine knows namespaces, not story
vocabulary.
"""

from __future__ import annotations

from typing import Any

from .content import Story
from .director import current_goal, suggested_intents
from .llm_protocol import (
    EntityKind,
    PerceivedEntity,
    PlayerPerception,
)
from .state import get_value

GROUP_KINDS = {
    "people": EntityKind.CHARACTER,
    "items": EntityKind.ITEM,
    "environment": EntityKind.ENVIRONMENT,
    "states": EntityKind.STATE,
}


def perception_config(story: Story) -> dict[str, Any]:
    """Story-declared public state keys with display labels."""
    config = story.data.get("perception") or {}
    return {
        "character_state": dict(config.get("character_state") or {}),
        "world_state": dict(config.get("world_state") or {}),
        "scene_state": dict(config.get("scene_state") or {}),
        # What the player's accumulated knowledge log is called in this
        # story ("线索" in a mystery, "回忆" in a romance, ...). The engine
        # concept is just "facts"; the word belongs to the story.
        "facts_label": str(config.get("facts_label") or "发现"),
    }


def public_state_paths(story: Story, state: dict[str, Any]) -> set[str]:
    """Dotted paths whose values the player can currently perceive."""
    config = perception_config(story)
    paths = {f"world.{key}" for key in config["world_state"]}
    paths.update(f"scene.{key}" for key in config["scene_state"])
    for char_id in story.characters_at(state):
        for key in config["character_state"]:
            paths.add(f"{char_id}.{key}")
            paths.add(f"characters.{char_id}.{key}")
    return paths


def filter_changes_for_player(
    story: Story,
    state: dict[str, Any],
    changes: list[tuple[str, Any, Any]],
) -> list[tuple[str, Any, Any]]:
    """Keep only changes on paths the player can already perceive.

    This is deliberately stricter than "visible after the action": a quote
    must not become an oracle.  Reveals, item acquisitions, movements and
    endings stay behind the wall until they are committed and narrated.
    """
    public = public_state_paths(story, state)
    return [
        (path, previous, new)
        for path, previous, new in changes
        if path in public and previous != new
    ]


def character_public_state(story: Story, state: dict[str, Any], char_id: str) -> dict[str, Any]:
    config = perception_config(story)["character_state"]
    values = {}
    for key in config:
        value = get_value(state, f"{char_id}.{key}")
        if value is not None:
            values[key] = value
    return values


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


def build_player_perception(
    story: Story,
    state: dict[str, Any],
    *,
    session_id: str,
    turn_no: int,
    state_revision: int,
    recent_events: tuple[str, ...] = (),
) -> PlayerPerception:
    """Assemble the player-visible snapshot from authoritative state."""
    # Local import: capabilities imports resolver, which must stay optional
    # for pure perception consumers.
    from .capabilities import available_tools

    location_id = story.current_location(state)
    actionable = story.actionable_objects(state)
    entities: list[PerceivedEntity] = []
    seen: set[str] = set()

    for char_id in story.characters_at(state):
        char = story.characters.get(char_id) or {}
        entities.append(_entity(
            char_id,
            story.character_name(char_id),
            EntityKind.CHARACTER,
            actionable=char_id in actionable,
            description=" ".join((char.get("public_profile") or "").split()),
            public_state=character_public_state(story, state, char_id),
        ))
        seen.add(char_id)

    for item_id in story.items_at(state):
        item = story.items.get(item_id) or {}
        entities.append(_entity(
            item_id,
            story.item_labels().get(item_id, item_id),
            EntityKind.ITEM,
            actionable=item_id in actionable,
            description=str(item.get("description") or ""),
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

    for situation_id, record in story.generated_situations_at(state, location_id).items():
        if situation_id in seen:
            continue
        entities.append(_entity(
            situation_id,
            str(record.get("name") or situation_id),
            EntityKind.STATE,
            actionable=situation_id in actionable,
            description=str(record.get("description") or ""),
        ))
        seen.add(situation_id)

    for node_id, label in story.exit_labels(state).items():
        if node_id in seen:
            continue
        entities.append(_entity(
            node_id, label, EntityKind.EXIT, actionable=True,
        ))
        seen.add(node_id)

    inventory = tuple(
        _entity(
            item_id,
            story.item_labels().get(item_id, item_id),
            EntityKind.ITEM,
            actionable=True,
            description=str((story.items.get(item_id) or {}).get("description") or ""),
        )
        for item_id in story.inventory(state)
    )

    config = perception_config(story)
    public_state: dict[str, Any] = {}
    for key in config["world_state"]:
        value = get_value(state, f"world.{key}")
        if value is not None:
            public_state[f"world.{key}"] = value
    for key in config["scene_state"]:
        value = get_value(state, f"scene.{key}")
        if value is not None:
            public_state[f"scene.{key}"] = value

    location_name = story.location_name(state, location_id)

    return PlayerPerception(
        story_id=story.id,
        session_id=session_id,
        turn_no=turn_no,
        state_revision=state_revision,
        location_id=location_id,
        location_name=location_name,
        current_goal=current_goal(story, state),
        visible_entities=tuple(entities),
        inventory=inventory,
        known_facts=tuple(dict.fromkeys(str(fact) for fact in state.get("facts") or [])),
        available_intents=tuple(suggested_intents(story, state)),
        capability_tools=available_tools(story, state),
        recent_events=tuple(event for event in recent_events if event),
        public_state=public_state,
    )
