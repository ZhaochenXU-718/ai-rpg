#!/usr/bin/env python3
"""Validate AIRPG story YAML files against the v1 content contract."""

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
    "available_characters",
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
)
EFFECT_MUTATION_KEYS = ("set_flags", "set_world", "state_patch")


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
        for namespace in ("world", "player", "scene", "flags"):
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
    if root in {"world", "player", "scene", "flags", "characters"}:
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
    if data.get("schema_version") != 1:
        report.error("schema_version must be 1 for the current validator.")


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
        if "quote_required" not in intent:
            report.warn(
                f"intents.{intent_id} has no quote_required; engine will derive it from base_risk."
            )


def validate_condition_block(
    block: dict[str, Any],
    known_paths: set[str],
    character_ids: set[str],
    report: ValidationReport,
    context: str,
    allow_ending_reached: bool = False,
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
        if not isinstance(value, dict):
            report.error(f"{context}.{group} must be a mapping.")
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
        )


def validate_scenes(
    scenes: dict[str, Any],
    characters: dict[str, Any],
    intents: dict[str, Any],
    known_paths: set[str],
    report: ValidationReport,
) -> None:
    character_ids = set(characters)
    intent_ids = set(intents)
    for scene_id, scene in scenes.items():
        if not isinstance(scene, dict):
            report.error(f"scenes.{scene_id} must be a mapping.")
            continue
        for field in sorted(REQUIRED_SCENE_FIELDS - scene.keys()):
            report.error(f"scenes.{scene_id} missing required field: {field}")

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
                if isinstance(values, dict):
                    for obj_id, label in values.items():
                        if not isinstance(label, str) or not label:
                            report.error(f"{context}.{obj_id}: label must be a non-empty string.")
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
                scene_id, scene["exit_conditions"], known_paths, character_ids, report
            )


def validate_trigger(
    storylet_id: str,
    trigger: dict[str, Any],
    scene_ids: set[str],
    intent_ids: set[str],
    object_ids: set[str],
    known_paths: set[str],
    character_ids: set[str],
    report: ValidationReport,
) -> None:
    context = f"storylets.{storylet_id}.trigger"
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

    for object_id in trigger.get("object_any", []) or []:
        if object_id not in object_ids and object_id not in character_ids:
            report.warn(f"{context}.object_any references object '{object_id}' not listed in any scene.")

    condition_groups = (
        "world_state",
        "player_state",
        "npc_state",
        "flags",
        "state_gte",
        "state_lte",
    )
    for group in condition_groups:
        value = trigger.get(group)
        if value is None:
            continue
        if not isinstance(value, dict):
            report.error(f"{context}.{group} must be a mapping.")
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


def validate_effect(
    storylet_id: str,
    effect: dict[str, Any],
    scene_ids: set[str],
    known_paths: set[str],
    character_ids: set[str],
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
        elif "scene" in effect["set_world"] and effect["set_world"]["scene"] not in scene_ids:
            report.error(f"{context}.set_world.scene references unknown scene '{effect['set_world']['scene']}'.")
    if "state_patch" in effect:
        if not isinstance(effect["state_patch"], dict):
            report.error(f"{context}.state_patch must be a mapping.")
        else:
            for path in effect["state_patch"]:
                validate_state_path(str(path), known_paths, character_ids, report, f"{context}.state_patch")
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
            report,
            context=f"{context}.temporary",
        )


def validate_storylets(
    storylets: list[Any],
    scenes: dict[str, Any],
    intents: dict[str, Any],
    characters: dict[str, Any],
    known_paths: set[str],
    report: ValidationReport,
) -> None:
    scene_ids = set(scenes)
    intent_ids = set(intents)
    character_ids = set(characters)
    object_ids = collect_objects(scenes)
    seen_ids: set[str] = set()

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

        trigger = storylet.get("trigger")
        if isinstance(trigger, dict):
            validate_trigger(storylet_id, trigger, scene_ids, intent_ids, object_ids, known_paths, character_ids, report)
        elif trigger is not None:
            report.error(f"storylets.{storylet_id}.trigger must be a mapping.")

        effect = storylet.get("effect")
        if isinstance(effect, dict):
            validate_effect(storylet_id, effect, scene_ids, known_paths, character_ids, report)
        elif effect is not None:
            report.error(f"storylets.{storylet_id}.effect must be a mapping.")


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
    for path in protected:
        if not isinstance(path, str):
            report.error(f"resolution_limits.protected entry {path!r} must be a string.")
            continue
        if path.endswith(".*"):
            continue
        validate_state_path(path, known_paths, character_ids, report, "resolution_limits.protected")
        if path in patchable:
            report.error(f"resolution_limits: '{path}' is listed as both patchable and protected.")


def collect_effect_mutations(effect: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return (flags set by the effect, scenes targeted via set_world.scene)."""
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
    for storylet in storylets:
        if not isinstance(storylet, dict):
            continue
        effect = storylet.get("effect")
        if isinstance(effect, dict):
            flags, scene_targets = collect_effect_mutations(effect)
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
    initial_scene = world.get("scene") if isinstance(world, dict) else None
    for scene_id in scenes:
        if scene_id != initial_scene and scene_id not in reachable_scenes:
            report.warn(
                f"scenes.{scene_id} is unreachable: no storylet sets world.scene to it "
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
        _, scene_targets = collect_effect_mutations(effect)
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

    validate_player_role(data, characters, report)
    validate_characters(characters, report)
    validate_intents(intents, report)

    known_paths = collect_state_paths(data)
    validate_scenes(scenes, characters, intents, known_paths, report)
    validate_storylets(storylets, scenes, intents, characters, known_paths, report)
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
