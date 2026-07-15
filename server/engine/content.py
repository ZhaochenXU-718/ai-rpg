"""Story content loading and typed accessors over the content contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class Story:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    @classmethod
    def load(cls, path: str | Path) -> "Story":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"story file {path} must contain a mapping")
        return cls(data)

    @property
    def id(self) -> str:
        return self.data.get("id", "")

    @property
    def title(self) -> str:
        return self.data.get("title", self.id)

    @property
    def premise(self) -> str:
        return (self.data.get("premise") or "").strip()

    @property
    def scenes(self) -> dict[str, Any]:
        return self.data.get("scenes") or {}

    @property
    def storylets(self) -> list[dict[str, Any]]:
        return self.data.get("storylets") or []

    @property
    def endings(self) -> dict[str, Any]:
        return self.data.get("endings") or {}

    @property
    def characters(self) -> dict[str, Any]:
        return self.data.get("characters") or {}

    @property
    def items(self) -> dict[str, Any]:
        return self.data.get("items") or {}

    @property
    def player_id(self) -> str:
        return (self.data.get("player_role") or {}).get("id", "player")

    @property
    def world_board(self) -> dict[str, Any]:
        return self.data.get("world_board") or {}

    @property
    def world_nodes(self) -> dict[str, Any]:
        return self.world_board.get("nodes") or {}

    @property
    def world_edges(self) -> list[dict[str, Any]]:
        return self.world_board.get("edges") or []

    @property
    def generation(self) -> dict[str, Any]:
        """Author-declared generative boundary; absent means nothing may be
        generated (no declaration is not a default allowance)."""
        return self.data.get("generation") or {}

    @property
    def location_archetypes(self) -> dict[str, Any]:
        return self.generation.get("location_archetypes") or {}

    @property
    def situation_archetypes(self) -> dict[str, Any]:
        return self.generation.get("situation_archetypes") or {}

    def generation_budget(self, budget_key: str) -> int:
        try:
            return int((self.generation.get("budgets") or {}).get(budget_key, 0))
        except (TypeError, ValueError):
            return 0

    def scene(self, scene_id: str) -> dict[str, Any]:
        return self.scenes.get(scene_id) or {}

    def generated_locations(self, state: dict[str, Any]) -> dict[str, Any]:
        return (state.get("generated") or {}).get("locations") or {}

    def generated_situations_at(
        self, state: dict[str, Any], node_id: str | None = None
    ) -> dict[str, Any]:
        node_id = node_id or self.current_location(state)
        return {
            situation_id: record
            for situation_id, record in (
                (state.get("generated") or {}).get("situations") or {}
            ).items()
            if isinstance(record, dict)
            and record.get("active")
            and record.get("parent") == node_id
        }

    def location_name(self, state: dict[str, Any], location_id: str) -> str:
        node = self.world_nodes.get(location_id) or {}
        if node.get("name"):
            return str(node["name"])
        scene_name = self.scene(location_id).get("name")
        if scene_name:
            return str(scene_name)
        record = self.generated_locations(state).get(location_id)
        if isinstance(record, dict) and record.get("name"):
            return str(record["name"])
        return location_id

    def current_location(self, state: dict[str, Any]) -> str:
        return (state.get("positions") or {}).get(
            self.player_id, (state.get("world") or {}).get("scene", "")
        )

    def characters_at(self, state: dict[str, Any], node_id: str | None = None) -> list[str]:
        """Physical presence derived from the authoritative positions map."""
        node_id = node_id or self.current_location(state)
        positions = state.get("positions") or {}
        if positions:
            return [
                char_id for char_id in self.characters
                if char_id != self.player_id and positions.get(char_id) == node_id
            ]
        # v1 compatibility only; schema v2 forbids this duplicate source.
        return list(self.scene(node_id).get("available_characters") or [])

    def inventory(self, state: dict[str, Any], owner_id: str | None = None) -> list[str]:
        owner_id = owner_id or self.player_id
        owned = []
        for item_id in self.items:
            placement = (state.get("item_locations") or {}).get(item_id)
            if (
                isinstance(placement, dict)
                and placement.get("type") == "carried_by"
                and placement.get("id") == owner_id
            ):
                owned.append(item_id)
        return owned

    def items_at(self, state: dict[str, Any], node_id: str | None = None) -> list[str]:
        node_id = node_id or self.current_location(state)
        found = []
        for item_id in self.items:
            placement = (state.get("item_locations") or {}).get(item_id)
            if (
                isinstance(placement, dict)
                and placement.get("type") == "board"
                and placement.get("id") == node_id
            ):
                found.append(item_id)
        return found

    def available_exits(
        self,
        state: dict[str, Any],
        node_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Subject-local exits whose state conditions currently hold.

        Authored exits come from the scene declaration. Committed generated
        locations (Local Canon) contribute derived exits: parent → generated
        child, and generated location → its parent. Both directions exist
        exactly because the commit admitted the attachment.
        """
        from .conditions import check_condition_block

        current = node_id or self.current_location(state)
        exits = []
        for exit_spec in self.scene(current).get("exits") or []:
            if isinstance(exit_spec, dict) and check_condition_block(
                state, exit_spec.get("when") or {}
            ):
                exits.append(exit_spec)

        generated = self.generated_locations(state)
        declared = {str(spec.get("to")) for spec in exits if isinstance(spec, dict)}
        for generated_id, record in generated.items():
            if not isinstance(record, dict):
                continue
            if record.get("parent") == current and generated_id not in declared:
                exits.append({
                    "to": generated_id,
                    "label": str(record.get("name") or generated_id),
                })
        current_record = generated.get(current)
        if isinstance(current_record, dict):
            parent = str(current_record.get("parent") or "")
            if parent and parent not in declared:
                exits.append({
                    "to": parent,
                    "label": self.location_name(state, parent),
                })
        return exits

    def exit_labels(
        self,
        state: dict[str, Any],
        node_id: str | None = None,
    ) -> dict[str, str]:
        return {
            str(exit_spec["to"]): str(exit_spec.get("label") or exit_spec["to"])
            for exit_spec in self.available_exits(state, node_id)
            if isinstance(exit_spec.get("to"), str)
        }

    def exit_for_target(self, state: dict[str, Any], target: str) -> dict[str, Any] | None:
        return next(
            (exit_spec for exit_spec in self.available_exits(state) if exit_spec.get("to") == target),
            None,
        )

    def scene_object_groups(
        self,
        scene_id: str,
        state: dict[str, Any] | None = None,
        *,
        actionable_only: bool = False,
    ) -> dict[str, list[str]]:
        """Return authored scene objects filtered by visibility/actionability.

        A mapping entry may remain the compact ``id: label`` form or use the
        conditional form::

            hidden_panel:
              label: "Hidden panel"
              visible_when: {flags: {panel_found: true}}
              actionable_when: {flags: {panel_unlocked: true}}

        Missing conditions default to true.  An invisible object is never
        actionable.  Physical characters and tracked board items are added by
        ``scene_objects`` rather than duplicated in authored object groups.
        """
        from .conditions import check_condition_block

        groups: dict[str, list[str]] = {}
        for group_name, group in (self.scene(scene_id).get("available_objects") or {}).items():
            if group_name == "people" and state is not None and state.get("positions"):
                continue
            entries: list[tuple[str, Any]] = []
            if isinstance(group, list):
                entries = [(str(item), None) for item in group]
            elif isinstance(group, dict):
                entries = [(str(obj_id), spec) for obj_id, spec in group.items()]

            selected: list[str] = []
            for obj_id, spec in entries:
                visible_when = spec.get("visible_when") if isinstance(spec, dict) else None
                actionable_when = spec.get("actionable_when") if isinstance(spec, dict) else None
                visible = state is None or check_condition_block(state, visible_when or {})
                actionable = visible and (
                    state is None or check_condition_block(state, actionable_when or {})
                )
                if visible and (not actionable_only or actionable):
                    selected.append(obj_id)
            if selected:
                groups[str(group_name)] = selected
        return groups

    def visible_scene_objects(self, scene_id: str, state: dict[str, Any]) -> set[str]:
        objects = {
            obj_id
            for group in self.scene_object_groups(scene_id, state).values()
            for obj_id in group
        }
        objects.update(self.characters_at(state, scene_id))
        objects.update(self.items_at(state, scene_id))
        objects.update(self.generated_situations_at(state, scene_id))
        return objects

    def scene_objects(self, scene_id: str, state: dict[str, Any] | None = None) -> set[str]:
        groups = self.scene_object_groups(scene_id, state, actionable_only=state is not None)
        objects = {obj_id for group in groups.values() for obj_id in group}
        if state is not None:
            objects.update(self.characters_at(state, scene_id))
            objects.update(self.items_at(state, scene_id))
            objects.update(self.generated_situations_at(state, scene_id))
        return objects

    def actionable_objects(self, state: dict[str, Any]) -> set[str]:
        scene_id = self.current_location(state)
        return (
            self.scene_objects(scene_id, state)
            | set(self.inventory(state))
            | set(self.exit_labels(state))
        )

    def object_labels(
        self,
        scene_id: str,
        state: dict[str, Any] | None = None,
        *,
        actionable_only: bool = False,
    ) -> dict[str, str]:
        """Display labels for the scene's objects; ids double as targets."""
        labels: dict[str, str] = {}
        selected = self.scene_object_groups(
            scene_id, state, actionable_only=actionable_only
        ) if state is not None else None
        for group_name, group in (self.scene(scene_id).get("available_objects") or {}).items():
            allowed = set(selected.get(group_name, [])) if selected is not None else None
            if isinstance(group, dict):
                for obj_id, spec in group.items():
                    if allowed is not None and str(obj_id) not in allowed:
                        continue
                    label = spec.get("label") if isinstance(spec, dict) else spec
                    if isinstance(label, str) and label:
                        labels[str(obj_id)] = label
            elif isinstance(group, list):
                for obj_id in group:
                    if allowed is not None and str(obj_id) not in allowed:
                        continue
                    labels.setdefault(str(obj_id), self.character_name(str(obj_id)))
        return labels

    def item_labels(self) -> dict[str, str]:
        return {
            str(item_id): str(item.get("name", item_id))
            for item_id, item in self.items.items()
            if isinstance(item, dict)
        }

    def object_label(
        self,
        scene_id: str,
        object_id: str,
        state: dict[str, Any] | None = None,
    ) -> str:
        if object_id in self.characters:
            return self.character_name(object_id)
        if object_id in self.items:
            return self.item_labels().get(object_id, object_id)
        if state is not None:
            generated = state.get("generated") or {}
            for namespace in ("locations", "situations"):
                record = (generated.get(namespace) or {}).get(object_id)
                if isinstance(record, dict) and record.get("name"):
                    return str(record["name"])
        return self.object_labels(scene_id, state).get(object_id, object_id)

    def character_name(self, char_id: str) -> str:
        return (self.characters.get(char_id) or {}).get("name", char_id)
