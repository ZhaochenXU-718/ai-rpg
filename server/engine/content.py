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
    def intents(self) -> dict[str, Any]:
        return self.data.get("intents") or {}

    @property
    def endings(self) -> dict[str, Any]:
        return self.data.get("endings") or {}

    @property
    def characters(self) -> dict[str, Any]:
        return self.data.get("characters") or {}

    @property
    def resolution_limits(self) -> dict[str, Any] | None:
        return self.data.get("resolution_limits")

    def scene(self, scene_id: str) -> dict[str, Any]:
        return self.scenes.get(scene_id) or {}

    def scene_objects(self, scene_id: str) -> set[str]:
        objects: set[str] = set()
        for group in (self.scene(scene_id).get("available_objects") or {}).values():
            if isinstance(group, list):
                objects.update(str(item) for item in group)
            elif isinstance(group, dict):
                objects.update(str(key) for key in group)
        return objects

    def object_labels(self, scene_id: str) -> dict[str, str]:
        """Display labels for the scene's objects; ids double as targets."""
        labels: dict[str, str] = {}
        for group in (self.scene(scene_id).get("available_objects") or {}).values():
            if isinstance(group, dict):
                for obj_id, label in group.items():
                    if isinstance(label, str) and label:
                        labels[str(obj_id)] = label
            elif isinstance(group, list):
                for obj_id in group:
                    labels.setdefault(str(obj_id), self.character_name(str(obj_id)))
        return labels

    def object_label(self, scene_id: str, object_id: str) -> str:
        if object_id in self.characters:
            return self.character_name(object_id)
        return self.object_labels(scene_id).get(object_id, object_id)

    def intent(self, intent_id: str) -> dict[str, Any]:
        return self.intents.get(intent_id) or {}

    def quote_required(self, intent_id: str) -> bool:
        """Explicit quote_required, else derived from base_risk (schema section 6)."""
        intent = self.intent(intent_id)
        if "quote_required" in intent:
            return bool(intent["quote_required"])
        return intent.get("base_risk", "medium") != "low"

    def character_name(self, char_id: str) -> str:
        return (self.characters.get(char_id) or {}).get("name", char_id)
