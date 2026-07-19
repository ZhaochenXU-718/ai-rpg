#!/usr/bin/env python3
"""Validate the minimal AIRPG authoring contract.

The validator checks authored identity, scene references, character positions,
and key-item custody. Narrative outcomes, relationships, promises, memories,
and scene progression deliberately have no deterministic schema here.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


NARRATIVE_FIRST_PROFILE = "narrative_first"
PRE_PIVOT_ARCHIVE_PROFILE = "pre_pivot_archive"
KNOWN_CONTENT_PROFILES = {
    NARRATIVE_FIRST_PROFILE,
    PRE_PIVOT_ARCHIVE_PROFILE,
}

REQUIRED_TOP_LEVEL = {
    "content_profile",
    "id",
    "title",
    "version",
    "schema_version",
    "language",
    "genre",
    "premise",
    "player_role",
    "style_bible",
    "global_rules",
    "initial_state",
    "characters",
    "scenes",
}
RETIRED_NARRATIVE_TOP_LEVEL = {
    "intents",
    "quote_warnings",
    "resolution_limits",
    "world_rules",
    "world_board",
    "generation",
    "storylets",
    "endings",
    "genre_system",
    "perception",
}
REQUIRED_CHARACTER_FIELDS = {"name", "role", "public_profile"}
REQUIRED_NARRATIVE_NPC_FIELDS = {
    "motivation",
    "voice",
    "initial_relationship",
}
OPTIONAL_CHARACTER_TEXT_FIELDS = (
    "secret",
    "pressure",
    "behavior",
    "mannerisms",
    "narration_notes",
)
AI_PLOT_FIELDS = {
    "player_role",
    "main_goal",
    "opposition",
    "world_rules",
    "hidden_truth",
}
MAX_CRITICAL_REMINDERS = 4
# Mirrors server/engine/modules.py CATEGORY_POLICIES; a test keeps them equal.
MODULE_CATEGORIES = {"main", "character", "pressure", "aftermath", "side"}
REQUIRED_MODULE_FIELDS = {"category", "title", "purpose", "hook", "trigger"}
OPTIONAL_MODULE_TEXT_FIELDS = ("escalation", "resolution", "fallback")
MODULE_PRIORITIES = {"low", "normal", "high"}
REQUIRES_STATUSES = {"engaged", "resolved", "dropped"}
OPENING_FIELDS = {"title", "intro", "positions"}
REQUIRED_SCENE_FIELDS = {
    "name",
    "purpose",
    "goal",
    "entry_text",
    "available_objects",
}
MACHINE_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class ValidationReport:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def load_yaml(path: Path, report: ValidationReport) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - user-facing CLI
        report.error(f"Failed to read YAML: {exc}")
        return None
    if not isinstance(data, dict):
        report.error("Root document must be a mapping.")
        return None
    return data


def _mapping(
    data: dict[str, Any],
    key: str,
    report: ValidationReport,
    *,
    context: str = "root",
) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        report.error(f"{context}.{key} must be a mapping.")
        return {}
    return value


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_machine_id(value: Any, context: str, report: ValidationReport) -> None:
    if not _non_empty_string(value) or MACHINE_ID.fullmatch(str(value)) is None:
        report.error(f"{context} must be a lowercase machine id.")


def _validate_archive(data: dict[str, Any], report: ValidationReport) -> None:
    """Recognize historical fixtures without maintaining their retired DSL."""
    for key in ("id", "title", "content_profile"):
        if not _non_empty_string(data.get(key)):
            report.error(f"root.{key} must be a non-empty string.")
    if data.get("content_profile") != PRE_PIVOT_ARCHIVE_PROFILE:
        report.error("archive content must declare content_profile: pre_pivot_archive.")


def _validate_characters(
    characters: dict[str, Any],
    player_id: str,
    report: ValidationReport,
) -> None:
    if player_id not in characters:
        report.error(f"player_role.id '{player_id}' is missing from characters.")
    for character_id, card in characters.items():
        context = f"characters.{character_id}"
        _validate_machine_id(character_id, context, report)
        if not isinstance(card, dict):
            report.error(f"{context} must be a mapping.")
            continue
        for field in REQUIRED_CHARACTER_FIELDS:
            if not _non_empty_string(card.get(field)):
                report.error(f"{context} missing required field: {field}")
        if character_id != player_id:
            for field in REQUIRED_NARRATIVE_NPC_FIELDS:
                if not _non_empty_string(card.get(field)):
                    report.error(f"{context} missing required field: {field}")
        for field in OPTIONAL_CHARACTER_TEXT_FIELDS:
            if field in card and not _non_empty_string(card.get(field)):
                report.error(
                    f"{context}.{field} must be a non-empty string when present."
                )
        if "dialogue_examples" in card:
            examples = card.get("dialogue_examples")
            if (
                not isinstance(examples, list)
                or not examples
                or not all(_non_empty_string(item) for item in examples)
            ):
                report.error(
                    f"{context}.dialogue_examples must be a non-empty list of strings."
                )
        if "initial_state" in card:
            report.error(
                f"{context}.initial_state is retired; character development belongs to memory."
            )


def _validate_scene_objects(
    scene_id: str,
    value: Any,
    report: ValidationReport,
) -> None:
    context = f"scenes.{scene_id}.available_objects"
    if not isinstance(value, dict):
        report.error(f"{context} must be a mapping.")
        return
    seen: set[str] = set()
    for group_id, group in value.items():
        if not isinstance(group, (dict, list)):
            report.error(f"{context}.{group_id} must be a mapping or list.")
            continue
        entries = group.items() if isinstance(group, dict) else ((item, item) for item in group)
        for object_id, label in entries:
            object_id = str(object_id)
            _validate_machine_id(object_id, f"{context}.{group_id}.{object_id}", report)
            if object_id in seen:
                report.error(f"{context} duplicates object id '{object_id}'.")
            seen.add(object_id)
            if not _non_empty_string(label):
                if isinstance(label, dict):
                    report.error(
                        f"{context}.{group_id}.{object_id} uses retired conditional object syntax."
                    )
                else:
                    report.error(f"{context}.{group_id}.{object_id} needs a label.")


def _validate_scenes(scenes: dict[str, Any], report: ValidationReport) -> None:
    for scene_id, scene in scenes.items():
        context = f"scenes.{scene_id}"
        _validate_machine_id(scene_id, context, report)
        if not isinstance(scene, dict):
            report.error(f"{context} must be a mapping.")
            continue
        for field in REQUIRED_SCENE_FIELDS:
            if field not in scene:
                report.error(f"{context} missing required field: {field}")
            elif field != "available_objects" and not _non_empty_string(scene[field]):
                report.error(f"{context}.{field} must be a non-empty string.")
        _validate_scene_objects(scene_id, scene.get("available_objects"), report)

        if "suggested_intents" in scene:
            report.error(f"{context}.suggested_intents is retired.")
        if "exit_conditions" in scene:
            report.error(f"{context}.exit_conditions is retired; exits are narrative options.")
        exits = scene.get("exits") or []
        if not isinstance(exits, list):
            report.error(f"{context}.exits must be a list.")
            continue
        destinations: set[str] = set()
        for index, exit_spec in enumerate(exits):
            exit_context = f"{context}.exits[{index}]"
            if not isinstance(exit_spec, dict):
                report.error(f"{exit_context} must be a mapping.")
                continue
            destination = exit_spec.get("to")
            if destination not in scenes:
                report.error(f"{exit_context}.to references unknown scene '{destination}'.")
            if destination in destinations:
                report.error(f"{context}.exits duplicates destination '{destination}'.")
            destinations.add(destination)
            if not _non_empty_string(exit_spec.get("label")):
                report.error(f"{exit_context}.label must be a non-empty string.")
            for retired in ("when", "narrative_hint"):
                if retired in exit_spec:
                    report.error(f"{exit_context}.{retired} is retired.")


def _validate_items(
    items: dict[str, Any],
    report: ValidationReport,
) -> None:
    for item_id, item in items.items():
        context = f"items.{item_id}"
        _validate_machine_id(item_id, context, report)
        if not isinstance(item, dict):
            report.error(f"{context} must be a mapping.")
            continue
        for field in ("name", "description"):
            if not _non_empty_string(item.get(field)):
                report.error(f"{context}.{field} must be a non-empty string.")
        if not isinstance(item.get("portable"), bool):
            report.error(f"{context}.portable must be a boolean.")
        if "request_policy" in item:
            report.error(f"{context}.request_policy is retired.")


def _validate_initial_state(
    initial: dict[str, Any],
    characters: dict[str, Any],
    scenes: dict[str, Any],
    items: dict[str, Any],
    report: ValidationReport,
) -> None:
    retired = set(initial) - {"positions", "item_locations"}
    for namespace in sorted(retired):
        report.error(
            f"initial_state.{namespace} is retired; authoritative state only tracks positions and item_locations."
        )

    positions = _mapping(initial, "positions", report, context="initial_state")
    for character_id in characters:
        if character_id not in positions:
            report.error(f"initial_state.positions missing character '{character_id}'.")
    for character_id, scene_id in positions.items():
        if character_id not in characters:
            report.error(f"initial_state.positions has unknown character '{character_id}'.")
        if scene_id not in scenes:
            report.error(
                f"initial_state.positions.{character_id} references unknown scene '{scene_id}'."
            )

    placements = _mapping(
        initial, "item_locations", report, context="initial_state"
    )
    for item_id in items:
        if item_id not in placements:
            report.error(f"initial_state.item_locations missing item '{item_id}'.")
    for item_id, placement in placements.items():
        context = f"initial_state.item_locations.{item_id}"
        if item_id not in items:
            report.error(f"{context} references an unknown item.")
        if not isinstance(placement, dict):
            report.error(f"{context} must be a placement mapping.")
            continue
        if set(placement) != {"type", "id"}:
            report.error(f"{context} must contain exactly type and id.")
            continue
        placement_type = placement.get("type")
        target = placement.get("id")
        if placement_type == "carried_by" and target not in characters:
            report.error(f"{context} names unknown carrier '{target}'.")
        elif placement_type == "board" and target not in scenes:
            report.error(f"{context} names unknown scene '{target}'.")
        elif placement_type not in {"carried_by", "board"}:
            report.error(f"{context}.type must be carried_by or board.")


def _validate_author_layers(data: dict[str, Any], report: ValidationReport) -> None:
    """Optional narrative layers: player pitch, AI blueprint, guidelines, reminders."""
    for key in ("player_facing_summary", "emotional_contract", "opening_narration"):
        if key in data and not _non_empty_string(data.get(key)):
            report.error(f"root.{key} must be a non-empty string when present.")

    if "ai_plot" in data:
        plot = data.get("ai_plot")
        if not isinstance(plot, dict) or not plot:
            report.error("root.ai_plot must be a non-empty mapping when present.")
        else:
            for key, value in plot.items():
                if key not in AI_PLOT_FIELDS:
                    report.error(
                        f"ai_plot.{key} is not a recognized blueprint field; "
                        f"allowed: {', '.join(sorted(AI_PLOT_FIELDS))}."
                    )
                elif not _non_empty_string(value):
                    report.error(f"ai_plot.{key} must be a non-empty string.")

    for key in ("narrative_guidelines", "critical_reminders"):
        if key not in data:
            continue
        value = data.get(key)
        if (
            not isinstance(value, list)
            or not value
            or not all(_non_empty_string(item) for item in value)
        ):
            report.error(
                f"root.{key} must be a non-empty list of non-empty strings when present."
            )
    reminders = data.get("critical_reminders")
    if isinstance(reminders, list) and len(reminders) > MAX_CRITICAL_REMINDERS:
        report.error(
            f"critical_reminders must keep at most {MAX_CRITICAL_REMINDERS} "
            "high-priority rules."
        )


def _validate_modules(
    data: dict[str, Any],
    characters: dict[str, Any],
    scenes: dict[str, Any],
    items: dict[str, Any],
    report: ValidationReport,
) -> None:
    """Modules carry narrative semantics only; no effects, no state patches."""
    if "modules" not in data:
        return
    modules = data.get("modules")
    if not isinstance(modules, dict) or not modules:
        report.error("root.modules must be a non-empty mapping when present.")
        return
    known_entities = set(characters) | set(scenes) | set(items)
    for module_id, spec in modules.items():
        context = f"modules.{module_id}"
        _validate_machine_id(module_id, context, report)
        if not isinstance(spec, dict):
            report.error(f"{context} must be a mapping.")
            continue
        for field in sorted(REQUIRED_MODULE_FIELDS):
            if not _non_empty_string(spec.get(field)):
                report.error(f"{context} missing required field: {field}")
        category = spec.get("category")
        if _non_empty_string(category) and category not in MODULE_CATEGORIES:
            report.error(
                f"{context}.category '{category}' is not recognized; "
                f"allowed: {', '.join(sorted(MODULE_CATEGORIES))}."
            )
        for field in OPTIONAL_MODULE_TEXT_FIELDS:
            if field in spec and not _non_empty_string(spec.get(field)):
                report.error(
                    f"{context}.{field} must be a non-empty string when present."
                )
        if "repeatable" in spec and not isinstance(spec.get("repeatable"), bool):
            report.error(f"{context}.repeatable must be a boolean.")
        for field in ("cooldown_turns", "min_turn"):
            if field in spec and (
                not isinstance(spec.get(field), int) or spec[field] < 1
            ):
                report.error(f"{context}.{field} must be a positive integer.")
        if "priority" in spec and spec.get("priority") not in MODULE_PRIORITIES:
            report.error(
                f"{context}.priority must be one of: "
                f"{', '.join(sorted(MODULE_PRIORITIES))}."
            )
        if "tags" in spec:
            tags = spec.get("tags")
            if not isinstance(tags, list) or not all(
                _non_empty_string(tag) for tag in tags
            ):
                report.error(f"{context}.tags must be a list of non-empty strings.")
        involves = spec.get("involves")
        if involves is not None:
            if not isinstance(involves, list):
                report.error(f"{context}.involves must be a list of entity ids.")
            else:
                for entity_id in involves:
                    if str(entity_id) not in known_entities:
                        report.error(
                            f"{context}.involves references unknown entity "
                            f"'{entity_id}'."
                        )
        for index, requirement in enumerate(spec.get("requires") or []):
            requirement_context = f"{context}.requires[{index}]"
            if not isinstance(requirement, dict) or set(requirement) != {
                "module",
                "status",
            }:
                report.error(
                    f"{requirement_context} must contain exactly module and status."
                )
                continue
            target = requirement.get("module")
            if target not in modules or target == module_id:
                report.error(
                    f"{requirement_context}.module references unknown or self "
                    f"module '{target}'."
                )
            if requirement.get("status") not in REQUIRES_STATUSES:
                report.error(
                    f"{requirement_context}.status must be one of: "
                    f"{', '.join(sorted(REQUIRES_STATUSES))}."
                )
        for retired in ("effects", "when", "state_patch", "conditions"):
            if retired in spec:
                report.error(
                    f"{context}.{retired} is not allowed; modules carry "
                    "narrative semantics only."
                )


def _validate_openings(
    data: dict[str, Any],
    characters: dict[str, Any],
    scenes: dict[str, Any],
    report: ValidationReport,
) -> None:
    if "openings" not in data:
        return
    openings = data.get("openings")
    if not isinstance(openings, dict) or not openings:
        report.error("root.openings must be a non-empty mapping when present.")
        return
    for opening_id, spec in openings.items():
        context = f"openings.{opening_id}"
        _validate_machine_id(opening_id, context, report)
        if not isinstance(spec, dict):
            report.error(f"{context} must be a mapping.")
            continue
        for key in set(spec) - OPENING_FIELDS:
            report.error(f"{context}.{key} is not a recognized opening field.")
        if not _non_empty_string(spec.get("title")):
            report.error(f"{context}.title must be a non-empty string.")
        if "intro" in spec and not _non_empty_string(spec.get("intro")):
            report.error(f"{context}.intro must be a non-empty string when present.")
        positions = spec.get("positions")
        if positions is None:
            continue
        if not isinstance(positions, dict) or not positions:
            report.error(f"{context}.positions must be a non-empty mapping.")
            continue
        for character_id, scene_id in positions.items():
            if character_id not in characters:
                report.error(
                    f"{context}.positions has unknown character '{character_id}'."
                )
            if scene_id not in scenes:
                report.error(
                    f"{context}.positions.{character_id} references unknown "
                    f"scene '{scene_id}'."
                )


def validate_content(data: dict[str, Any]) -> ValidationReport:
    report = ValidationReport()
    profile = data.get("content_profile")
    if profile not in KNOWN_CONTENT_PROFILES:
        report.error(f"unknown content_profile '{profile}'.")
        return report
    if profile == PRE_PIVOT_ARCHIVE_PROFILE:
        _validate_archive(data, report)
        return report

    for key in sorted(REQUIRED_TOP_LEVEL - set(data)):
        report.error(f"root missing required field: {key}")
    for key in sorted(RETIRED_NARRATIVE_TOP_LEVEL & set(data)):
        report.error(f"{key} is retired from narrative_first content.")

    _validate_machine_id(data.get("id"), "root.id", report)
    for key in ("title", "version", "language", "genre", "premise"):
        if not _non_empty_string(data.get(key)):
            report.error(f"root.{key} must be a non-empty string.")
    if data.get("schema_version") != 2:
        report.error("root.schema_version must be 2.")

    player_role = _mapping(data, "player_role", report)
    player_id = str(player_role.get("id") or "")
    _validate_machine_id(player_id, "player_role.id", report)
    for field in ("name", "public_identity", "private_goal"):
        if not _non_empty_string(player_role.get(field)):
            report.error(f"player_role.{field} must be a non-empty string.")
    constraints = player_role.get("constraints")
    if not isinstance(constraints, list) or not all(
        _non_empty_string(item) for item in constraints
    ):
        report.error("player_role.constraints must be a list of non-empty strings.")

    _mapping(data, "style_bible", report)
    _mapping(data, "global_rules", report)
    characters = _mapping(data, "characters", report)
    scenes = _mapping(data, "scenes", report)
    items = data.get("items") or {}
    if not isinstance(items, dict):
        report.error("root.items must be a mapping when present.")
        items = {}
    initial = _mapping(data, "initial_state", report)

    _validate_author_layers(data, report)
    _validate_characters(characters, player_id, report)
    _validate_scenes(scenes, report)
    _validate_items(items, report)
    _validate_initial_state(initial, characters, scenes, items, report)
    _validate_modules(data, characters, scenes, items, report)
    _validate_openings(data, characters, scenes, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate AIRPG story YAML.")
    parser.add_argument("paths", nargs="+", help="Story YAML file(s) to validate.")
    args = parser.parse_args()

    exit_code = 0
    for raw_path in args.paths:
        path = Path(raw_path)
        report = ValidationReport()
        data = load_yaml(path, report)
        if data is not None:
            report = validate_content(data)

        print(f"Validating {path}")
        for warning in report.warnings:
            print(f"  warning: {warning}")
        for error in report.errors:
            print(f"  error: {error}")
        if report.errors:
            exit_code = 1
            print(
                f"  failed: {len(report.errors)} error(s), "
                f"{len(report.warnings)} warning(s)"
            )
        else:
            print(f"  ok: {len(report.warnings)} warning(s)")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
