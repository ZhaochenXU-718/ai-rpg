"""Story content loading and typed accessors over the content contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _authored_lines(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        str(item).strip()
        for item in raw
        if isinstance(item, str) and item.strip()
    )


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
    def player_facing_summary(self) -> str:
        """Player-facing pitch; never compiled into model prompts."""
        return (self.data.get("player_facing_summary") or "").strip()

    @property
    def emotional_contract(self) -> str:
        return (self.data.get("emotional_contract") or "").strip()

    @property
    def opening_narration(self) -> str:
        """Authored first prose passage; shown once and anchors the narrator."""
        return (self.data.get("opening_narration") or "").strip()

    @property
    def ai_plot(self) -> dict[str, str]:
        raw = self.data.get("ai_plot")
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): str(value).strip()
            for key, value in raw.items()
            if isinstance(value, str) and value.strip()
        }

    @property
    def narrative_guidelines(self) -> tuple[str, ...]:
        return _authored_lines(self.data.get("narrative_guidelines"))

    @property
    def critical_reminders(self) -> tuple[str, ...]:
        return _authored_lines(self.data.get("critical_reminders"))

    @property
    def modules(self) -> dict[str, Any]:
        raw = self.data.get("modules")
        if not isinstance(raw, dict):
            return {}
        return {
            str(module_id): spec
            for module_id, spec in raw.items()
            if isinstance(spec, dict)
        }

    @property
    def openings(self) -> dict[str, Any]:
        raw = self.data.get("openings")
        if not isinstance(raw, dict):
            return {}
        return {
            str(opening_id): spec
            for opening_id, spec in raw.items()
            if isinstance(spec, dict)
        }

    @property
    def scenes(self) -> dict[str, Any]:
        return self.data.get("scenes") or {}

    @property
    def characters(self) -> dict[str, Any]:
        return self.data.get("characters") or {}

    @property
    def items(self) -> dict[str, Any]:
        return self.data.get("items") or {}

    @property
    def player_id(self) -> str:
        return (self.data.get("player_role") or {}).get("id", "player")

    def scene(self, scene_id: str) -> dict[str, Any]:
        return self.scenes.get(scene_id) or {}

    def location_name(self, state: dict[str, Any], location_id: str) -> str:
        scene_name = self.scene(location_id).get("name")
        if scene_name:
            return str(scene_name)
        return location_id

    def current_location(self, state: dict[str, Any]) -> str:
        return str((state.get("positions") or {}).get(self.player_id, ""))

    def current_goal(self, state: dict[str, Any]) -> str:
        scene = self.scene(self.current_location(state))
        return str(scene.get("goal") or "")

    def characters_at(self, state: dict[str, Any], node_id: str | None = None) -> list[str]:
        """Physical presence derived from the authoritative positions map."""
        node_id = node_id or self.current_location(state)
        positions = state.get("positions") or {}
        if positions:
            return [
                char_id for char_id in self.characters
                if char_id != self.player_id and positions.get(char_id) == node_id
            ]
        return []

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

    def visible_items_at(
        self, state: dict[str, Any], node_id: str | None = None
    ) -> list[str]:
        """Return board items plus explicitly public items carried in-scene.

        A carried item is private by default. Authors opt in with
        ``visible_when_carried: true`` when its presence/custody is obvious.
        """
        node_id = node_id or self.current_location(state)
        visible = list(self.items_at(state, node_id))
        positions = state.get("positions") or {}
        for item_id, item in self.items.items():
            placement = (state.get("item_locations") or {}).get(item_id)
            if (
                isinstance(item, dict)
                and item.get("visible_when_carried") is True
                and isinstance(placement, dict)
                and placement.get("type") == "carried_by"
                and positions.get(placement.get("id")) == node_id
            ):
                visible.append(item_id)
        return list(dict.fromkeys(visible))

    def available_exits(
        self,
        state: dict[str, Any],
        node_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the scene's authored navigation options."""
        current = node_id or self.current_location(state)
        return [
            exit_spec
            for exit_spec in self.scene(current).get("exits") or []
            if isinstance(exit_spec, dict)
        ]

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
        """Return static authored scene objects.

        Physical characters and tracked items are added by ``scene_objects``.
        """
        groups: dict[str, list[str]] = {}
        for group_name, group in (self.scene(scene_id).get("available_objects") or {}).items():
            if group_name == "people" and state is not None and state.get("positions"):
                continue
            entries: list[tuple[str, Any]] = []
            if isinstance(group, list):
                entries = [(str(item), None) for item in group]
            elif isinstance(group, dict):
                entries = [(str(obj_id), spec) for obj_id, spec in group.items()]

            selected = [obj_id for obj_id, _ in entries]
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
        objects.update(self.visible_items_at(state, scene_id))
        return objects

    def scene_objects(self, scene_id: str, state: dict[str, Any] | None = None) -> set[str]:
        groups = self.scene_object_groups(scene_id, state, actionable_only=state is not None)
        objects = {obj_id for group in groups.values() for obj_id in group}
        if state is not None:
            objects.update(self.characters_at(state, scene_id))
            objects.update(self.visible_items_at(state, scene_id))
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
        return self.object_labels(scene_id, state).get(object_id, object_id)

    def character_name(self, char_id: str) -> str:
        return (self.characters.get(char_id) or {}).get("name", char_id)
