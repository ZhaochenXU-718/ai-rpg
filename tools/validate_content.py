#!/usr/bin/env python3
"""Validate AIRPG story YAML files against the v1/v2 content contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


REQUIRED_TOP_LEVEL = {
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
    "intents",
    "scenes",
    "storylets",
    "endings",
}

REQUIRED_CHARACTER_FIELDS = {"name", "role", "public_profile"}
REQUIRED_INTENT_FIELDS = {"label", "description"}
REQUIRED_SCENE_FIELDS = {
    "name",
    "purpose",
    "goal",
    "entry_text",
    "available_objects",
    "suggested_intents",
}
REQUIRED_STORYLET_FIELDS = {
    "id",
    "title",
    "type",
    "once",
    "trigger",
    "effect",
    "narrative_hint",
}
REQUIRED_ENDING_FIELDS = {"title", "priority", "conditions", "outcome"}
COMPARISON_SUFFIXES = ("_gte", "_lte", "_gt", "_lt", "_ne")
KNOWN_STORYLET_TYPES = {
    "pressure",
    "reveal",
    "opportunity",
    "opportunity_with_cost",
    "consequence",
    "reward",
    "progress",
    "partial_progress",
    "failure_pressure",
    "ending_route",
}
CONDITION_GROUPS = (
    "world_state",
    "player_state",
    "npc_state",
    "flags",
    "state_gte",
    "state_lte",
    "positions",
    "same_location",
    "inventory_all",
    "inventory_any",
    "inventory_none",
)
EFFECT_MUTATION_KEYS = (
    "set_flags", "set_world", "state_patch", "move_entities", "move_items",
)


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


def require_mapping(
    data: dict[str, Any],
    key: str,
    report: ValidationReport,
    context: str = "root",
) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        report.error(f"{context}.{key} must be a mapping.")
        return {}
    return value


def require_list(
    data: dict[str, Any],
    key: str,
    report: ValidationReport,
    context: str = "root",
) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        report.error(f"{context}.{key} must be a list.")
        return []
    return value


def strip_comparison_suffix(key: str) -> str:
    for suffix in COMPARISON_SUFFIXES:
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def collect_objects(scenes: dict[str, Any]) -> set[str]:
    objects: set[str] = set()
    for scene in scenes.values():
        if not isinstance(scene, dict):
            continue
        available = scene.get("available_objects", {})
        if not isinstance(available, dict):
            continue
        for group_values in available.values():
            if isinstance(group_values, list):
                objects.update(item for item in group_values if isinstance(item, str))
            elif isinstance(group_values, dict):
                objects.update(str(key) for key in group_values)
    return objects


def collect_state_paths(data: dict[str, Any]) -> set[str]:
    paths: set[str] = set()

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_prefix = f"{prefix}.{key}" if prefix else str(key)
                paths.add(child_prefix)
                walk(child_prefix, child)

    initial_state = data.get("initial_state", {})
    if isinstance(initial_state, dict):
        for namespace in (
            "world", "player", "scene", "flags", "positions", "item_locations"
        ):
            if namespace in initial_state:
                paths.add(namespace)
                walk(namespace, initial_state[namespace])

    characters = data.get("characters", {})
    if isinstance(characters, dict):
        for char_id, char in characters.items():
            if not isinstance(char, dict):
                continue
            state = char.get("initial_state", {})
            if not isinstance(state, dict):
                continue
            paths.add(f"characters.{char_id}")
            paths.add(str(char_id))
            walk(f"characters.{char_id}", state)
            walk(str(char_id), state)

    return paths


def validate_state_path(
    path: str,
    known_paths: set[str],
    character_ids: set[str],
    report: ValidationReport,
    context: str,
) -> None:
    base_path = strip_comparison_suffix(path)
    if base_path in known_paths:
        return

    root = base_path.split(".", 1)[0]
    if root in {
        "world", "player", "scene", "flags", "positions", "item_locations", "characters"
    }:
        report.warn(f"{context}: state path '{path}' is not initialized; it will be created at runtime.")
        return
    if root in character_ids:
        report.warn(f"{context}: character state path '{path}' is not initialized; it will be created at runtime.")
        return
    report.warn(f"{context}: state path '{path}' has an unknown root.")


def validate_top_level(data: dict[str, Any], report: ValidationReport) -> None:
    missing = sorted(REQUIRED_TOP_LEVEL - data.keys())
    for key in missing:
        report.error(f"Missing required top-level field: {key}")
    schema_version = data.get("schema_version")
    if schema_version not in (1, 2):
        report.error("schema_version must be 1 or 2.")
    if schema_version == 2 and "world_board" not in data:
        report.error("schema v2 requires world_board.")


def validate_world_board(
    data: dict[str, Any],
    scenes: dict[str, Any],
    characters: dict[str, Any],
    report: ValidationReport,
) -> tuple[set[str], set[tuple[str, str]]]:
    """Validate schema-v2 board and authoritative initial positions."""
    if data.get("schema_version") != 2:
        return set(scenes), set()
    board = data.get("world_board")
    if not isinstance(board, dict):
        report.error("world_board must be a mapping.")
        return set(), set()
    nodes = board.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        report.error("world_board.nodes must be a non-empty mapping.")
        nodes = {}
    node_ids = set(nodes)
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            report.error(f"world_board.nodes.{node_id} must be a mapping.")
        elif not isinstance(node.get("name"), str) or not node.get("name"):
            report.error(f"world_board.nodes.{node_id}.name must be a non-empty string.")

    directed_edges: set[tuple[str, str]] = set()
    edges = board.get("edges")
    if not isinstance(edges, list):
        report.error("world_board.edges must be a list.")
        edges = []
    for index, edge in enumerate(edges):
        context = f"world_board.edges[{index}]"
        if not isinstance(edge, dict):
            report.error(f"{context} must be a mapping.")
            continue
        source, target = edge.get("from"), edge.get("to")
        if source not in node_ids:
            report.error(f"{context}.from references unknown node '{source}'.")
        if target not in node_ids:
            report.error(f"{context}.to references unknown node '{target}'.")
        if source == target:
            report.error(f"{context} must connect two different nodes.")
        if "bidirectional" in edge and not isinstance(edge["bidirectional"], bool):
            report.error(f"{context}.bidirectional must be a boolean.")
        if source in node_ids and target in node_ids and source != target:
            directed_edges.add((source, target))
            if edge.get("bidirectional"):
                directed_edges.add((target, source))

    initial = data.get("initial_state") or {}
    world = initial.get("world") if isinstance(initial, dict) else {}
    if isinstance(world, dict) and "scene" in world:
        report.error(
            "initial_state.world.scene is forbidden in schema v2; "
            "initial_state.positions.<player_id> is the only location source."
        )
    positions = initial.get("positions") if isinstance(initial, dict) else None
    if not isinstance(positions, dict):
        report.error("schema v2 requires initial_state.positions mapping.")
        positions = {}
    if isinstance(world, dict):
        step = world.get("step")
        if not isinstance(step, int) or step < 0:
            report.error("schema v2 requires initial_state.world.step integer >= 0.")
    for entity_id in characters:
        if entity_id not in positions:
            report.error(f"initial_state.positions is missing character '{entity_id}'.")
    for entity_id, node_id in positions.items():
        if entity_id not in characters:
            report.error(f"initial_state.positions references unknown entity '{entity_id}'.")
        if node_id not in node_ids:
            report.error(
                f"initial_state.positions.{entity_id} references unknown node '{node_id}'."
            )
    return node_ids, directed_edges


def validate_item_placement(
    item_id: str,
    placement: Any,
    node_ids: set[str],
    character_ids: set[str],
    container_ids: set[str],
    report: ValidationReport,
    context: str,
) -> None:
    if not isinstance(placement, dict):
        report.error(f"{context} must be a placement mapping.")
        return
    placement_type = placement.get("type")
    placement_id = placement.get("id")
    if placement_type not in {"board", "carried_by", "container", "removed"}:
        report.error(
            f"{context}.type must be board, carried_by, container, or removed."
        )
        return
    if placement_type == "removed":
        if placement_id is not None:
            report.error(f"{context} with type removed must not define id.")
        return
    if not isinstance(placement_id, str) or not placement_id:
        report.error(f"{context}.id must be a non-empty string.")
        return
    if placement_type == "board" and placement_id not in node_ids:
        report.error(f"{context} references unknown board node '{placement_id}'.")
    if placement_type == "carried_by" and placement_id not in character_ids:
        report.error(f"{context} references unknown character '{placement_id}'.")
    if placement_type == "container" and placement_id not in container_ids:
        report.error(f"{context} references unknown container object '{placement_id}'.")


def validate_items(
    data: dict[str, Any],
    scenes: dict[str, Any],
    characters: dict[str, Any],
    node_ids: set[str],
    report: ValidationReport,
) -> set[str]:
    items = data.get("items", {})
    if not isinstance(items, dict):
        report.error("items must be a mapping.")
        return set()
    item_ids = set(items)
    for item_id, item in items.items():
        context = f"items.{item_id}"
        if not isinstance(item, dict):
            report.error(f"{context} must be a mapping.")
            continue
        if not isinstance(item.get("name"), str) or not item.get("name"):
            report.error(f"{context}.name must be a non-empty string.")
        for field in ("portable", "consumable"):
            if not isinstance(item.get(field), bool):
                report.error(f"{context}.{field} must be a boolean.")

    initial = data.get("initial_state") or {}
    locations = initial.get("item_locations") if isinstance(initial, dict) else None
    if item_ids and not isinstance(locations, dict):
        report.error("stories with items require initial_state.item_locations mapping.")
        locations = {}
    elif not isinstance(locations, dict):
        locations = {}
    for item_id in item_ids:
        if item_id not in locations:
            report.error(f"initial_state.item_locations is missing item '{item_id}'.")
    for item_id, placement in locations.items():
        if item_id not in item_ids:
            report.error(f"initial_state.item_locations references unknown item '{item_id}'.")
            continue
        validate_item_placement(
            item_id,
            placement,
            node_ids,
            set(characters),
            collect_objects(scenes),
            report,
            f"initial_state.item_locations.{item_id}",
        )
    return item_ids


def validate_world_rules(
    data: dict[str, Any],
    scenes: dict[str, Any],
    characters: dict[str, Any],
    known_paths: set[str],
    node_ids: set[str],
    directed_edges: set[tuple[str, str]],
    item_ids: set[str],
    report: ValidationReport,
) -> None:
    rules = data.get("world_rules", [])
    if not isinstance(rules, list):
        report.error("world_rules must be a list.")
        return
    seen: set[str] = set()
    character_ids = set(characters)
    for index, rule in enumerate(rules):
        context = f"world_rules[{index}]"
        if not isinstance(rule, dict):
            report.error(f"{context} must be a mapping.")
            continue
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            report.error(f"{context}.id must be a non-empty string.")
        elif rule_id in seen:
            report.error(f"duplicate world rule id: {rule_id}")
        else:
            seen.add(rule_id)
        actor = rule.get("actor")
        if actor not in character_ids:
            report.error(f"{context}.actor references unknown character '{actor}'.")
        priority = rule.get("priority", 0)
        if not isinstance(priority, int):
            report.error(f"{context}.priority must be an integer.")

        when = rule.get("when")
        if not isinstance(when, dict):
            report.error(f"{context}.when must be a mapping.")
            when = {}
        scene = when.get("scene")
        if scene is not None and scene not in scenes:
            report.error(f"{context}.when.scene references unknown scene '{scene}'.")
        scene_any = when.get("scene_any", [])
        if not isinstance(scene_any, list):
            report.error(f"{context}.when.scene_any must be a list.")
        else:
            for scene_id in scene_any:
                if scene_id not in scenes:
                    report.error(
                        f"{context}.when.scene_any references unknown scene '{scene_id}'."
                    )
        condition_block = {
            key: value for key, value in when.items() if key not in {"scene", "scene_any"}
        }
        validate_condition_block(
            condition_block,
            known_paths,
            character_ids,
            report,
            f"{context}.when",
            node_ids=node_ids,
            item_ids=item_ids,
        )

        move = rule.get("move")
        if not isinstance(move, dict) or set(move) not in ({"to"}, {"toward"}):
            report.error(f"{context}.move must contain exactly one of 'to' or 'toward'.")
            continue
        if "to" in move and move["to"] not in node_ids:
            report.error(f"{context}.move.to references unknown node '{move['to']}'.")
        if "toward" in move and move["toward"] not in character_ids:
            report.error(
                f"{context}.move.toward references unknown character '{move['toward']}'."
            )
        source = (when.get("positions") or {}).get(actor) if isinstance(when.get("positions"), dict) else None
        if source in node_ids and "to" in move and move["to"] in node_ids:
            if (source, move["to"]) not in directed_edges:
                report.error(
                    f"{context}.move.to '{move['to']}' is not adjacent to declared source '{source}'."
                )
        hint = rule.get("narrative_hint")
        if hint is not None and (not isinstance(hint, str) or not hint):
            report.error(f"{context}.narrative_hint must be a non-empty string.")


def validate_player_role(data: dict[str, Any], characters: dict[str, Any], report: ValidationReport) -> None:
    player_role = require_mapping(data, "player_role", report)
    player_id = player_role.get("id")
    if not player_id:
        report.error("player_role.id is required.")
    elif player_id not in characters:
        report.error(f"player_role.id '{player_id}' is not defined in characters.")


def validate_characters(characters: dict[str, Any], report: ValidationReport) -> None:
    for char_id, char in characters.items():
        if not isinstance(char, dict):
            report.error(f"characters.{char_id} must be a mapping.")
            continue
        for field in sorted(REQUIRED_CHARACTER_FIELDS - char.keys()):
            report.error(f"characters.{char_id} missing required field: {field}")
        if "initial_state" in char and not isinstance(char["initial_state"], dict):
            report.error(f"characters.{char_id}.initial_state must be a mapping.")


def validate_intents(intents: dict[str, Any], report: ValidationReport) -> None:
    for intent_id, intent in intents.items():
        if not isinstance(intent, dict):
            report.error(f"intents.{intent_id} must be a mapping.")
            continue
        for field in sorted(REQUIRED_INTENT_FIELDS - intent.keys()):
            report.error(f"intents.{intent_id} missing required field: {field}")
        if "quote_required" in intent and not isinstance(intent["quote_required"], bool):
            report.error(f"intents.{intent_id}.quote_required must be a boolean.")
        if "requires_storylet_match" in intent and not isinstance(
            intent["requires_storylet_match"], bool
        ):
            report.error(
                f"intents.{intent_id}.requires_storylet_match must be a boolean."
            )
        if "quote_required" not in intent:
            report.warn(
                f"intents.{intent_id} has no quote_required; engine will derive it from base_risk."
            )
        engine_action = intent.get("engine_action")
        if engine_action is not None and engine_action not in {"move"}:
            report.error(
                f"intents.{intent_id}.engine_action '{engine_action}' is not supported."
            )
        for field in ("min_objects", "max_objects"):
            value = intent.get(field)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                report.error(f"intents.{intent_id}.{field} must be an integer >= 0.")
        minimum, maximum = intent.get("min_objects"), intent.get("max_objects")
        if isinstance(minimum, int) and isinstance(maximum, int) and minimum > maximum:
            report.error(
                f"intents.{intent_id}.min_objects must not exceed max_objects."
            )


def validate_condition_block(
    block: dict[str, Any],
    known_paths: set[str],
    character_ids: set[str],
    report: ValidationReport,
    context: str,
    allow_ending_reached: bool = False,
    node_ids: set[str] | None = None,
    item_ids: set[str] | None = None,
) -> None:
    """Validate one AND-block of state conditions (shared by exit_conditions)."""
    for group, value in block.items():
        if group == "ending_reached":
            if not allow_ending_reached:
                report.error(f"{context}: ending_reached is only allowed in exit_conditions.")
            elif value is not True:
                report.error(f"{context}: ending_reached only supports the value true.")
            continue
        if group not in CONDITION_GROUPS:
            report.error(f"{context}: unknown condition group '{group}'.")
            continue
        if group == "same_location":
            if not isinstance(value, list) or len(value) < 2:
                report.error(f"{context}.same_location must contain at least two entity ids.")
            else:
                for entity_id in value:
                    if entity_id not in character_ids:
                        report.error(
                            f"{context}.same_location references unknown entity '{entity_id}'."
                        )
            continue
        if group in {"inventory_all", "inventory_any", "inventory_none"}:
            if not isinstance(value, list) or not value:
                report.error(f"{context}.{group} must be a non-empty item id list.")
            else:
                for item_id in value:
                    if item_ids is not None and item_id not in item_ids:
                        report.error(f"{context}.{group} references unknown item '{item_id}'.")
            continue
        if not isinstance(value, dict):
            report.error(f"{context}.{group} must be a mapping.")
            continue
        if group == "positions":
            for entity_id, node_id in value.items():
                if entity_id not in character_ids:
                    report.error(f"{context}.positions references unknown entity '{entity_id}'.")
                if node_ids is not None and node_id not in node_ids:
                    report.error(f"{context}.positions references unknown node '{node_id}'.")
            continue
        for key in value:
            if group == "world_state":
                path = f"world.{strip_comparison_suffix(str(key))}"
            elif group == "player_state":
                path = f"player.{strip_comparison_suffix(str(key))}"
            elif group == "flags":
                path = f"flags.{strip_comparison_suffix(str(key))}"
            else:
                path = str(key)
            validate_state_path(path, known_paths, character_ids, report, f"{context}.{group}")


def validate_exit_conditions(
    scene_id: str,
    exit_conditions: Any,
    known_paths: set[str],
    character_ids: set[str],
    node_ids: set[str],
    item_ids: set[str],
    report: ValidationReport,
) -> None:
    context = f"scenes.{scene_id}.exit_conditions"
    if isinstance(exit_conditions, list):
        report.error(
            f"{context}: expression-string lists are no longer supported; "
            "use the structured form 'any: [<condition block>, ...]' (schema section 9.4)."
        )
        return
    if not isinstance(exit_conditions, dict) or set(exit_conditions) != {"any"}:
        report.error(f"{context} must be a mapping with a single 'any' key.")
        return
    blocks = exit_conditions["any"]
    if not isinstance(blocks, list) or not blocks:
        report.error(f"{context}.any must be a non-empty list of condition blocks.")
        return
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            report.error(f"{context}.any[{index}] must be a mapping.")
            continue
        validate_condition_block(
            block,
            known_paths,
            character_ids,
            report,
            f"{context}.any[{index}]",
            allow_ending_reached=True,
            node_ids=node_ids,
            item_ids=item_ids,
        )


def validate_scenes(
    scenes: dict[str, Any],
    characters: dict[str, Any],
    intents: dict[str, Any],
    known_paths: set[str],
    report: ValidationReport,
    schema_version: int,
    node_ids: set[str],
    directed_edges: set[tuple[str, str]],
    item_ids: set[str],
) -> None:
    character_ids = set(characters)
    intent_ids = set(intents)
    for scene_id, scene in scenes.items():
        if not isinstance(scene, dict):
            report.error(f"scenes.{scene_id} must be a mapping.")
            continue
        for field in sorted(REQUIRED_SCENE_FIELDS - scene.keys()):
            report.error(f"scenes.{scene_id} missing required field: {field}")

        if schema_version == 1 and "available_characters" not in scene:
            report.error(f"scenes.{scene_id} missing required field: available_characters")
        if schema_version == 2 and "available_characters" in scene:
            report.error(
                f"scenes.{scene_id}.available_characters is forbidden in schema v2; "
                "derive presence from initial_state.positions."
            )
        if schema_version == 2 and scene_id not in node_ids:
            report.error(f"scenes.{scene_id} has no matching world_board node.")

        exits = scene.get("exits", [])
        if not isinstance(exits, list):
            report.error(f"scenes.{scene_id}.exits must be a list.")
        else:
            seen_targets: set[str] = set()
            for index, exit_spec in enumerate(exits):
                context = f"scenes.{scene_id}.exits[{index}]"
                if not isinstance(exit_spec, dict):
                    report.error(f"{context} must be a mapping.")
                    continue
                target = exit_spec.get("to")
                if target not in scenes:
                    report.error(f"{context}.to references unknown scene '{target}'.")
                elif schema_version == 2 and (scene_id, target) not in directed_edges:
                    report.error(
                        f"{context}.to '{target}' is not connected by a world_board edge."
                    )
                if target in seen_targets:
                    report.error(f"{context}.to duplicates exit target '{target}'.")
                elif isinstance(target, str):
                    seen_targets.add(target)
                if not isinstance(exit_spec.get("label"), str) or not exit_spec.get("label"):
                    report.error(f"{context}.label must be a non-empty string.")
                when = exit_spec.get("when", {})
                if not isinstance(when, dict):
                    report.error(f"{context}.when must be a mapping.")
                else:
                    validate_condition_block(
                        when, known_paths, character_ids, report, f"{context}.when",
                        node_ids=node_ids, item_ids=item_ids,
                    )

        for char_id in scene.get("available_characters", []) or []:
            if char_id not in character_ids:
                report.error(f"scenes.{scene_id}.available_characters references unknown character '{char_id}'.")

        for intent_id in scene.get("suggested_intents", []) or []:
            if intent_id not in intent_ids:
                report.error(f"scenes.{scene_id}.suggested_intents references unknown intent '{intent_id}'.")

        available_objects = scene.get("available_objects")
        if not isinstance(available_objects, dict):
            report.error(f"scenes.{scene_id}.available_objects must be a mapping.")
        else:
            for group, values in available_objects.items():
                context = f"scenes.{scene_id}.available_objects.{group}"
                if schema_version == 2 and group == "people":
                    report.error(
                        f"{context} is forbidden in schema v2; people are derived from positions."
                    )
                if isinstance(values, dict):
                    for obj_id, spec in values.items():
                        if schema_version == 2 and obj_id in item_ids:
                            report.error(
                                f"{context}.{obj_id} duplicates a tracked item; "
                                "place it through initial_state.item_locations instead."
                            )
                        if isinstance(spec, str):
                            if not spec:
                                report.error(
                                    f"{context}.{obj_id}: label must be a non-empty string."
                                )
                            continue
                        if not isinstance(spec, dict):
                            report.error(
                                f"{context}.{obj_id} must be a label string or object spec mapping."
                            )
                            continue
                        unknown_fields = set(spec) - {
                            "label", "visible_when", "actionable_when"
                        }
                        for field in sorted(unknown_fields):
                            report.error(f"{context}.{obj_id}: unknown field '{field}'.")
                        if not isinstance(spec.get("label"), str) or not spec.get("label"):
                            report.error(
                                f"{context}.{obj_id}.label must be a non-empty string."
                            )
                        for field in ("visible_when", "actionable_when"):
                            condition = spec.get(field, {})
                            if not isinstance(condition, dict):
                                report.error(f"{context}.{obj_id}.{field} must be a mapping.")
                                continue
                            validate_condition_block(
                                condition,
                                known_paths,
                                character_ids,
                                report,
                                f"{context}.{obj_id}.{field}",
                                node_ids=node_ids,
                                item_ids=item_ids,
                            )
                elif isinstance(values, list):
                    if group != "people":
                        report.warn(
                            f"{context}: bare id list has no display labels; "
                            "players cannot tell these are actionable (use 'id: label' mapping)."
                        )
                else:
                    report.error(f"{context} must be a list or an 'id: label' mapping.")

        if "exit_conditions" in scene:
            validate_exit_conditions(
                scene_id, scene["exit_conditions"], known_paths, character_ids,
                node_ids, item_ids, report
            )


def validate_trigger(
    storylet_id: str,
    trigger: dict[str, Any],
    scene_ids: set[str],
    intent_ids: set[str],
    object_ids: set[str],
    known_paths: set[str],
    character_ids: set[str],
    node_ids: set[str],
    item_ids: set[str],
    report: ValidationReport,
) -> None:
    context = f"storylets.{storylet_id}.trigger"
    allowed_groups = {
        "scene", "scene_any", "intent", "intent_any", "object_any", "object_all",
        *CONDITION_GROUPS
    }
    for group in trigger:
        if group not in allowed_groups:
            report.error(f"{context}: unknown trigger group '{group}'.")
    scene = trigger.get("scene")
    if scene is not None and scene not in scene_ids:
        report.error(f"{context}.scene references unknown scene '{scene}'.")
    for scene_id in trigger.get("scene_any", []) or []:
        if scene_id not in scene_ids:
            report.error(f"{context}.scene_any references unknown scene '{scene_id}'.")

    intent = trigger.get("intent")
    if intent is not None and intent not in intent_ids:
        report.error(f"{context}.intent references unknown intent '{intent}'.")
    for intent_id in trigger.get("intent_any", []) or []:
        if intent_id not in intent_ids:
            report.error(f"{context}.intent_any references unknown intent '{intent_id}'.")

    for object_group in ("object_any", "object_all"):
        values = trigger.get(object_group, []) or []
        if not isinstance(values, list):
            report.error(f"{context}.{object_group} must be a list.")
            continue
        for object_id in values:
            if (
                object_id not in object_ids
                and object_id not in character_ids
                and object_id not in item_ids
            ):
                report.warn(
                    f"{context}.{object_group} references unknown object '{object_id}'."
                )

    validate_condition_block(
        {group: trigger[group] for group in CONDITION_GROUPS if group in trigger},
        known_paths,
        character_ids,
        report,
        context,
        node_ids=node_ids,
        item_ids=item_ids,
    )


def validate_effect(
    storylet_id: str,
    effect: dict[str, Any],
    scene_ids: set[str],
    known_paths: set[str],
    character_ids: set[str],
    node_ids: set[str],
    schema_version: int,
    player_id: str,
    item_ids: set[str],
    container_ids: set[str],
    report: ValidationReport,
    context: str | None = None,
) -> None:
    context = context or f"storylets.{storylet_id}.effect"
    if "duration_turns" in effect:
        report.error(
            f"{context}.duration_turns is not allowed at the effect top level; "
            "wrap expiring changes in a 'temporary' block (schema section 10.2)."
        )
    if "set_flags" in effect and not isinstance(effect["set_flags"], dict):
        report.error(f"{context}.set_flags must be a mapping.")
    if "set_world" in effect:
        if not isinstance(effect["set_world"], dict):
            report.error(f"{context}.set_world must be a mapping.")
        elif "scene" in effect["set_world"]:
            if schema_version == 2:
                report.error(
                    f"{context}.set_world.scene is forbidden in schema v2; use move_entities."
                )
            elif effect["set_world"]["scene"] not in scene_ids:
                report.error(f"{context}.set_world.scene references unknown scene '{effect['set_world']['scene']}'.")
    if "state_patch" in effect:
        if not isinstance(effect["state_patch"], dict):
            report.error(f"{context}.state_patch must be a mapping.")
        else:
            for path in effect["state_patch"]:
                if schema_version == 2 and str(path).startswith("positions."):
                    report.error(
                        f"{context}.state_patch may not mutate '{path}'; use move_entities."
                    )
                if schema_version == 2 and str(path).startswith("item_locations."):
                    report.error(
                        f"{context}.state_patch may not mutate '{path}'; use move_items."
                    )
                validate_state_path(str(path), known_paths, character_ids, report, f"{context}.state_patch")
    if "move_entities" in effect:
        moves = effect["move_entities"]
        if not isinstance(moves, dict) or not moves:
            report.error(f"{context}.move_entities must be a non-empty mapping.")
        else:
            for entity_id, node_id in moves.items():
                if entity_id not in character_ids:
                    report.error(
                        f"{context}.move_entities references unknown entity '{entity_id}'."
                    )
                if node_id not in node_ids:
                    report.error(
                        f"{context}.move_entities references unknown node '{node_id}'."
                    )
                if entity_id == player_id and node_id not in scene_ids:
                    report.error(
                        f"{context}.move_entities moves the player to node '{node_id}' "
                        "which has no scene definition."
                    )
    if "move_items" in effect:
        moves = effect["move_items"]
        if not isinstance(moves, dict) or not moves:
            report.error(f"{context}.move_items must be a non-empty mapping.")
        else:
            for item_id, placement in moves.items():
                if item_id not in item_ids:
                    report.error(f"{context}.move_items references unknown item '{item_id}'.")
                    continue
                validate_item_placement(
                    item_id,
                    placement,
                    node_ids,
                    character_ids,
                    container_ids,
                    report,
                    f"{context}.move_items.{item_id}",
                )
    if "add_clues" in effect and not isinstance(effect["add_clues"], list):
        report.error(f"{context}.add_clues must be a list.")
    if "temporary" in effect:
        temporary = effect["temporary"]
        if not isinstance(temporary, dict):
            report.error(f"{context}.temporary must be a mapping.")
            return
        duration = temporary.get("duration_turns")
        if not isinstance(duration, int) or duration < 1:
            report.error(f"{context}.temporary.duration_turns must be an integer >= 1.")
        if not any(key in temporary for key in EFFECT_MUTATION_KEYS):
            report.error(f"{context}.temporary must contain at least one of {EFFECT_MUTATION_KEYS}.")
        if "temporary" in temporary:
            report.error(f"{context}.temporary must not nest another temporary block.")
        validate_effect(
            storylet_id,
            {k: v for k, v in temporary.items() if k in EFFECT_MUTATION_KEYS},
            scene_ids,
            known_paths,
            character_ids,
            node_ids,
            schema_version,
            player_id,
            item_ids,
            container_ids,
            report,
            context=f"{context}.temporary",
        )


def validate_storylets(
    storylets: list[Any],
    scenes: dict[str, Any],
    intents: dict[str, Any],
    characters: dict[str, Any],
    known_paths: set[str],
    node_ids: set[str],
    schema_version: int,
    player_id: str,
    item_ids: set[str],
    report: ValidationReport,
) -> None:
    scene_ids = set(scenes)
    intent_ids = set(intents)
    character_ids = set(characters)
    object_ids = collect_objects(scenes)
    seen_ids: set[str] = set()
    authored_action_intents: set[str] = set()

    for index, storylet in enumerate(storylets):
        if not isinstance(storylet, dict):
            report.error(f"storylets[{index}] must be a mapping.")
            continue
        storylet_id = storylet.get("id", f"<index:{index}>")
        if storylet_id in seen_ids:
            report.error(f"Duplicate storylet id: {storylet_id}")
        seen_ids.add(storylet_id)

        for field in sorted(REQUIRED_STORYLET_FIELDS - storylet.keys()):
            report.error(f"storylets.{storylet_id} missing required field: {field}")

        storylet_type = storylet.get("type")
        if storylet_type is not None and storylet_type not in KNOWN_STORYLET_TYPES:
            report.warn(f"storylets.{storylet_id}.type '{storylet_type}' is not in the recommended vocabulary.")
        phase = storylet.get("phase", "action")
        if phase not in {"action", "after_world"}:
            report.error(
                f"storylets.{storylet_id}.phase must be 'action' or 'after_world'."
            )
        director_hint = storylet.get("director_hint")
        if director_hint is not None and not isinstance(director_hint, (bool, str)):
            report.error(
                f"storylets.{storylet_id}.director_hint must be a boolean or string."
            )
        if isinstance(director_hint, str) and not director_hint:
            report.error(
                f"storylets.{storylet_id}.director_hint must not be an empty string."
            )
        if director_hint and phase != "action":
            report.error(
                f"storylets.{storylet_id}.director_hint is only valid in the action phase."
            )

        trigger = storylet.get("trigger")
        if isinstance(trigger, dict):
            if phase == "action":
                if isinstance(trigger.get("intent"), str):
                    authored_action_intents.add(trigger["intent"])
                if isinstance(trigger.get("intent_any"), list):
                    authored_action_intents.update(
                        value for value in trigger["intent_any"] if isinstance(value, str)
                    )
            validate_trigger(
                storylet_id, trigger, scene_ids, intent_ids, object_ids,
                known_paths, character_ids, node_ids, item_ids, report,
            )
        elif trigger is not None:
            report.error(f"storylets.{storylet_id}.trigger must be a mapping.")

        effect = storylet.get("effect")
        if isinstance(effect, dict):
            validate_effect(
                storylet_id, effect, scene_ids, known_paths, character_ids,
                node_ids, schema_version, player_id, item_ids, object_ids, report,
            )
        elif effect is not None:
            report.error(f"storylets.{storylet_id}.effect must be a mapping.")

    for intent_id, intent in intents.items():
        if (
            isinstance(intent, dict)
            and intent.get("requires_storylet_match") is True
            and intent_id not in authored_action_intents
        ):
            report.error(
                f"intents.{intent_id}.requires_storylet_match is true but no action storylet "
                "declares that intent."
            )


def validate_resolution_limits(
    data: dict[str, Any],
    known_paths: set[str],
    character_ids: set[str],
    report: ValidationReport,
) -> None:
    limits = data.get("resolution_limits")
    if limits is None:
        if "custom" in (data.get("intents") or {}):
            report.warn(
                "resolution_limits is missing but the story allows the 'custom' intent; "
                "generic resolution will not be able to change any state."
            )
        return
    if not isinstance(limits, dict):
        report.error("resolution_limits must be a mapping.")
        return

    max_paths = limits.get("max_paths_per_action")
    if max_paths is not None and (not isinstance(max_paths, int) or max_paths < 1):
        report.error("resolution_limits.max_paths_per_action must be an integer >= 1.")

    patchable = limits.get("patchable", {})
    if not isinstance(patchable, dict):
        report.error("resolution_limits.patchable must be a mapping.")
        patchable = {}
    for path, bounds in patchable.items():
        context = f"resolution_limits.patchable.{path}"
        validate_state_path(str(path), known_paths, character_ids, report, "resolution_limits.patchable")
        if not isinstance(bounds, dict):
            report.error(f"{context} must be a mapping of bounds.")
            continue
        has_range = "min" in bounds or "max" in bounds or "max_step" in bounds
        has_values = "values" in bounds
        if has_range == has_values:
            report.error(f"{context} must define either numeric bounds (min/max/max_step) or an enum 'values' list.")
            continue
        if has_values and (not isinstance(bounds["values"], list) or not bounds["values"]):
            report.error(f"{context}.values must be a non-empty list.")
        if has_range:
            minimum, maximum = bounds.get("min"), bounds.get("max")
            if minimum is not None and maximum is not None and minimum > maximum:
                report.error(f"{context}: min ({minimum}) must not exceed max ({maximum}).")
            step = bounds.get("max_step")
            if step is not None and (not isinstance(step, (int, float)) or step <= 0):
                report.error(f"{context}.max_step must be a positive number.")

    protected = limits.get("protected", [])
    if not isinstance(protected, list):
        report.error("resolution_limits.protected must be a list.")
        protected = []
    if data.get("schema_version") == 2 and "positions.*" not in protected:
        report.error(
            "schema v2 requires resolution_limits.protected to contain 'positions.*'."
        )
    if data.get("items") and "item_locations.*" not in protected:
        report.error(
            "stories with items require resolution_limits.protected to contain "
            "'item_locations.*'."
        )
    for path in protected:
        if not isinstance(path, str):
            report.error(f"resolution_limits.protected entry {path!r} must be a string.")
            continue
        if path.endswith(".*"):
            continue
        validate_state_path(path, known_paths, character_ids, report, "resolution_limits.protected")
        if path in patchable:
            report.error(f"resolution_limits: '{path}' is listed as both patchable and protected.")


def collect_effect_mutations(
    effect: dict[str, Any], player_id: str = "player"
) -> tuple[set[str], set[str]]:
    """Return (flags set by the effect, player destinations targeted by it)."""
    flags: set[str] = set()
    scenes: set[str] = set()

    def scan(block: dict[str, Any]) -> None:
        set_flags = block.get("set_flags")
        if isinstance(set_flags, dict):
            flags.update(str(key) for key in set_flags)
        set_world = block.get("set_world")
        if isinstance(set_world, dict) and isinstance(set_world.get("scene"), str):
            scenes.add(set_world["scene"])
        state_patch = block.get("state_patch")
        if isinstance(state_patch, dict):
            for path in state_patch:
                path = str(path)
                if path.startswith("flags."):
                    flags.add(path.split(".", 1)[1])
                elif path == "world.scene":
                    scenes.add(str(state_patch[path]))
        move_entities = block.get("move_entities")
        if isinstance(move_entities, dict) and isinstance(move_entities.get(player_id), str):
            scenes.add(move_entities[player_id])

    scan(effect)
    temporary = effect.get("temporary")
    if isinstance(temporary, dict):
        scan(temporary)
    return flags, scenes


def validate_content_graph(
    data: dict[str, Any],
    scenes: dict[str, Any],
    storylets: list[Any],
    report: ValidationReport,
) -> None:
    """Dead-flag and scene-reachability checks over the whole content graph."""
    set_flags: set[str] = set()
    reachable_scenes: set[str] = set()
    player_id = (data.get("player_role") or {}).get("id", "player")
    for storylet in storylets:
        if not isinstance(storylet, dict):
            continue
        effect = storylet.get("effect")
        if isinstance(effect, dict):
            flags, scene_targets = collect_effect_mutations(effect, player_id)
            set_flags.update(flags)
            reachable_scenes.update(scene_targets)

    initial_state = data.get("initial_state", {})
    declared_flags = initial_state.get("flags", {}) if isinstance(initial_state, dict) else {}
    if isinstance(declared_flags, dict):
        for flag in declared_flags:
            if flag not in set_flags:
                report.warn(
                    f"initial_state.flags.{flag} is never set by any storylet effect; "
                    "it is either dead or a storylet is missing."
                )

    world = initial_state.get("world", {}) if isinstance(initial_state, dict) else {}
    positions = initial_state.get("positions", {}) if isinstance(initial_state, dict) else {}
    if data.get("schema_version") == 2 and isinstance(positions, dict):
        initial_scene = positions.get(player_id)
    else:
        initial_scene = world.get("scene") if isinstance(world, dict) else None
    for scene_id in scenes:
        if scene_id != initial_scene and scene_id not in reachable_scenes:
            report.warn(
                f"scenes.{scene_id} is unreachable: no storylet moves the player to it "
                "and it is not the initial scene."
            )

    # A transition the scene's intent menu cannot express is invisible to the
    # player: every intent-gated scene-changing storylet must share at least
    # one intent with each trigger scene's suggested_intents.
    for storylet in storylets:
        if not isinstance(storylet, dict):
            continue
        effect = storylet.get("effect")
        if not isinstance(effect, dict):
            continue
        _, scene_targets = collect_effect_mutations(effect, player_id)
        if not scene_targets:
            continue
        trigger = storylet.get("trigger") or {}
        if not isinstance(trigger, dict):
            continue
        intents = [trigger["intent"]] if "intent" in trigger else list(trigger.get("intent_any") or [])
        if not intents:
            continue
        trigger_scenes = [trigger["scene"]] if "scene" in trigger else list(trigger.get("scene_any") or [])
        for scene_id in trigger_scenes:
            scene = scenes.get(scene_id)
            if not isinstance(scene, dict):
                continue
            suggested = scene.get("suggested_intents") or []
            if suggested and not set(intents) & set(suggested):
                report.warn(
                    f"storylets.{storylet.get('id')}: transition requires intents {intents} "
                    f"but scenes.{scene_id}.suggested_intents offers none of them; "
                    "the player cannot discover this transition from the menu."
                )


def validate_endings(
    endings: dict[str, Any],
    known_paths: set[str],
    character_ids: set[str],
    report: ValidationReport,
) -> None:
    for ending_id, ending in endings.items():
        if not isinstance(ending, dict):
            report.error(f"endings.{ending_id} must be a mapping.")
            continue
        for field in sorted(REQUIRED_ENDING_FIELDS - ending.keys()):
            report.error(f"endings.{ending_id} missing required field: {field}")
        conditions = ending.get("conditions")
        if not isinstance(conditions, dict):
            report.error(f"endings.{ending_id}.conditions must be a mapping.")
            continue
        for path in conditions:
            validate_state_path(str(path), known_paths, character_ids, report, f"endings.{ending_id}.conditions")


def validate_content(data: dict[str, Any]) -> ValidationReport:
    report = ValidationReport()
    validate_top_level(data, report)

    characters = require_mapping(data, "characters", report)
    intents = require_mapping(data, "intents", report)
    scenes = require_mapping(data, "scenes", report)
    storylets = require_list(data, "storylets", report)
    endings = require_mapping(data, "endings", report)
    schema_version = data.get("schema_version") if data.get("schema_version") in (1, 2) else 1

    validate_player_role(data, characters, report)
    validate_characters(characters, report)
    validate_intents(intents, report)

    known_paths = collect_state_paths(data)
    node_ids, directed_edges = validate_world_board(data, scenes, characters, report)
    item_ids = validate_items(data, scenes, characters, node_ids, report)
    validate_world_rules(
        data, scenes, characters, known_paths, node_ids, directed_edges, item_ids, report
    )
    validate_scenes(
        scenes, characters, intents, known_paths, report, schema_version,
        node_ids, directed_edges, item_ids,
    )
    validate_storylets(
        storylets, scenes, intents, characters, known_paths,
        node_ids, schema_version, (data.get("player_role") or {}).get("id", "player"),
        item_ids, report,
    )
    validate_endings(endings, known_paths, set(characters), report)
    validate_resolution_limits(data, known_paths, set(characters), report)
    validate_content_graph(data, scenes, storylets, report)

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
            print(f"  failed: {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
        else:
            print(f"  ok: {len(report.warnings)} warning(s)")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
