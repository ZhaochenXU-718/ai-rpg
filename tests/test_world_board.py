from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from server.engine.content import Story
from server.engine.director import transition_options
from server.engine.effects import TemporaryEffects
from server.engine.renderer import render_turn
from server.engine.resolver import run_turn
from server.engine.session import GameSession, SessionError
from server.engine.state import build_initial_state
from server.engine.world import advance_world
from server.cli import tokenize_action_line
from tools.validate_content import validate_content


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "midnight_archive.yaml"


class WorldStepTests(unittest.TestCase):
    def test_all_movement_rules_read_the_same_snapshot(self) -> None:
        data = {
            "player_role": {"id": "hero"},
            "characters": {"hero": {}, "alpha": {}, "beta": {}},
            "world_board": {
                "nodes": {"n1": {}, "n2": {}, "n3": {}},
                "edges": [
                    {"from": "n1", "to": "n2", "bidirectional": True},
                    {"from": "n2", "to": "n3", "bidirectional": True},
                ],
            },
            "world_rules": [
                {
                    "id": "alpha_moves",
                    "actor": "alpha",
                    "when": {"positions": {"beta": "n2"}},
                    "move": {"to": "n2"},
                },
                {
                    "id": "beta_moves",
                    "actor": "beta",
                    "when": {"positions": {"alpha": "n1"}},
                    "move": {"to": "n3"},
                },
            ],
            "initial_state": {
                "world": {"step": 0},
                "positions": {"hero": "n1", "alpha": "n1", "beta": "n2"},
            },
        }
        story = Story(data)
        state = build_initial_state(data)

        result = advance_world(story, state)

        self.assertEqual(result.step_no, 1)
        self.assertEqual(state["positions"]["alpha"], "n2")
        self.assertEqual(state["positions"]["beta"], "n3")
        self.assertEqual(result.rules, ["alpha_moves", "beta_moves"])


class PresenceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.state = build_initial_state(self.story.data)
        self.temporaries = TemporaryEffects()
        self.consumed: set[str] = set()

    def turn(
        self,
        number: int,
        intent: str,
        objects: list[str],
        patch: dict | None = None,
    ):
        return run_turn(
            self.story,
            self.state,
            self.temporaries,
            self.consumed,
            number,
            intent,
            objects,
            patch or {},
            strict_patch=True,
        )

    def test_presence_is_derived_and_butler_cannot_be_in_two_places(self) -> None:
        self.turn(1, "observe", ["family_portrait"])
        self.turn(2, "sneak", ["side_stair"])
        self.assertEqual(self.state["positions"]["player"], "servant_corridor")
        self.assertEqual(self.state["positions"]["maid"], "servant_corridor")
        self.assertEqual(self.state["positions"]["butler"], "great_hall")
        self.assertEqual(self.story.characters_at(self.state), ["maid"])

        self.turn(3, "sneak", ["back_stairs"])
        self.assertEqual(self.state["positions"]["player"], "archive_door")
        self.assertEqual(self.story.characters_at(self.state), ["guard"])
        self.assertNotIn("butler", self.story.scene_objects("archive_door", self.state))

        step_before = self.state["world"]["step"]
        time_before = self.state["world"]["time_left"]
        rejected = self.turn(4, "negotiate", ["butler"])
        self.assertTrue(rejected.errors)
        self.assertEqual(self.state["world"]["step"], step_before)
        self.assertEqual(self.state["world"]["time_left"], time_before)

        result = self.turn(4, "create_distraction", ["candle_stand"])
        self.assertEqual(self.state["positions"]["butler"], "archive_door")
        self.assertIn("butler_pursues_intruder", result.world_rules)
        self.assertIn("butler_catches_player", result.fired)
        self.assertIn("butler", self.story.characters_at(self.state))

    def test_inventory_items_are_acquired_and_used_explicitly(self) -> None:
        self.turn(1, "observe", ["family_portrait"])
        self.assertEqual(self.story.inventory(self.state), ["maid_note"])

        self.turn(2, "negotiate", ["heir"], {"heir.trust": 1})
        self.assertEqual(self.story.inventory(self.state), ["old_badge", "maid_note"])

        self.turn(3, "sneak", ["side_stair"])
        self.turn(4, "negotiate", ["maid"], {"maid.trust": 1})
        self.assertEqual(
            self.story.inventory(self.state),
            ["servant_key", "old_badge", "maid_note"],
        )

        self.turn(5, "sneak", ["back_stairs"])
        entered = self.turn(6, "use", ["servant_key", "service_door"])
        self.assertIn("archive_entry_with_key", entered.fired)
        self.assertEqual(self.state["positions"]["player"], "archive_room")

        solved = self.turn(7, "use", ["old_badge", "locked_cabinet"])
        self.assertIn("hidden_compartment", solved.fired)
        self.assertIn("forgery_evidence", self.story.inventory(self.state))
        self.assertEqual(solved.ending, "truth_exposed")

    def test_unowned_item_is_rejected_without_cost_or_world_step(self) -> None:
        time_before = self.state["world"]["time_left"]
        step_before = self.state["world"]["step"]

        rejected = self.turn(1, "use", ["old_badge", "family_portrait"])

        self.assertTrue(rejected.errors)
        self.assertEqual(self.state["world"]["time_left"], time_before)
        self.assertEqual(self.state["world"]["step"], step_before)

    def test_player_can_return_along_an_authored_board_exit(self) -> None:
        self.turn(1, "observe", ["family_portrait"])
        self.turn(2, "sneak", ["side_stair"])
        self.assertEqual(self.state["positions"]["player"], "servant_corridor")

        result = self.turn(3, "move", ["great_hall"])

        self.assertEqual(result.errors, [])
        self.assertEqual(self.state["positions"]["player"], "great_hall")
        self.assertEqual(result.scene_after, "great_hall")

    def test_opened_archive_can_be_exited_and_reentered(self) -> None:
        self.state["positions"]["player"] = "archive_room"
        self.state["world"]["archive_access"] = "open"

        left = self.turn(1, "move", ["archive_door"])
        self.assertEqual(left.errors, [])
        self.assertEqual(self.state["positions"]["player"], "archive_door")
        self.assertIn("archive_room", self.story.exit_labels(self.state))

        returned = self.turn(2, "move", ["archive_room"])
        self.assertEqual(returned.errors, [])
        self.assertEqual(self.state["positions"]["player"], "archive_room")

    def test_hidden_object_visibility_tracks_discovery_state(self) -> None:
        self.state["positions"]["player"] = "archive_room"

        self.assertNotIn(
            "hidden_compartment",
            self.story.visible_scene_objects("archive_room", self.state),
        )
        self.assertNotIn("hidden_compartment", self.story.actionable_objects(self.state))

        result = self.turn(1, "observe", ["locked_cabinet"])

        self.assertIn("evidence_without_badge", result.fired)
        self.assertEqual(result.result_tier, "partial_success")
        self.assertIn(
            "hidden_compartment",
            self.story.visible_scene_objects("archive_room", self.state),
        )
        self.assertIn("hidden_compartment", self.story.actionable_objects(self.state))

    def test_unrelated_observation_does_not_discover_compartment(self) -> None:
        self.state["positions"]["player"] = "archive_room"

        result = self.turn(1, "observe", ["fireplace"])

        self.assertNotIn("evidence_without_badge", result.fired)
        self.assertFalse(self.state["flags"]["cabinet_weakness_found"])

    def test_key_uses_discovered_compartment_as_canonical_target(self) -> None:
        self.state["positions"]["player"] = "archive_room"
        self.state["positions"]["butler"] = "archive_room"
        self.state["item_locations"]["servant_key"] = {
            "type": "carried_by",
            "id": "player",
        }
        self.state["flags"]["cabinet_weakness_found"] = True
        self.state["world"]["time_left"] = 1
        self.state["characters"]["butler"]["suspicion"] = 5
        self.state["player"]["exposed"] = True

        result = self.turn(1, "use", ["servant_key", "hidden_compartment"])

        self.assertIn("force_compartment_with_key", result.fired)
        self.assertIn("forgery_evidence", self.story.inventory(self.state))
        self.assertEqual(result.ending, "costly_victory")
        self.assertEqual(self.state["world"]["time_left"], 0)

    def test_nonmatching_use_is_rejected_without_cost_or_world_step(self) -> None:
        self.state["positions"]["player"] = "archive_room"
        self.state["item_locations"]["servant_key"] = {
            "type": "carried_by",
            "id": "player",
        }
        self.state["flags"]["cabinet_weakness_found"] = True
        time_before = self.state["world"]["time_left"]
        step_before = self.state["world"]["step"]

        result = self.turn(1, "use", ["servant_key", "locked_cabinet"])

        self.assertTrue(result.errors)
        self.assertIn("没有与「使用」", result.errors[0])
        self.assertEqual(self.state["world"]["time_left"], time_before)
        self.assertEqual(self.state["world"]["step"], step_before)
        self.assertIn("行动未执行", render_turn(self.story, result))

    def test_director_surfaces_unlocked_non_transition_interaction(self) -> None:
        self.state["positions"]["player"] = "archive_room"
        self.state["item_locations"]["servant_key"] = {
            "type": "carried_by",
            "id": "player",
        }
        self.state["flags"]["cabinet_weakness_found"] = True

        options = transition_options(self.story, self.state, self.consumed)

        force = next(option for option in options if option["title"] == "用仆役钥匙撬开暗格")
        self.assertEqual(force["intents"], ["use"])
        self.assertEqual(force["objects"], ["servant_key", "hidden_compartment"])


class SessionValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def prepare_compartment(self) -> None:
        self.session.state["positions"]["player"] = "archive_room"
        self.session.state["item_locations"]["servant_key"] = {
            "type": "carried_by",
            "id": "player",
        }
        self.session.state["flags"]["cabinet_weakness_found"] = True

    def test_invalid_quote_does_not_increment_turn_or_time(self) -> None:
        self.prepare_compartment()
        time_before = self.session.state["world"]["time_left"]
        step_before = self.session.state["world"]["step"]

        with self.assertRaisesRegex(SessionError, "至少需要 2 个目标"):
            self.session.quote("use", ["servant_key"])

        self.assertEqual(self.session.turn_no, 0)
        self.assertEqual(self.session.state["world"]["time_left"], time_before)
        self.assertEqual(self.session.state["world"]["step"], step_before)

    def test_bare_sneak_without_opening_is_rejected_before_quote(self) -> None:
        self.session.state["positions"]["player"] = "archive_door"

        with self.assertRaisesRegex(SessionError, "没有与「潜入」"):
            self.session.quote("sneak", [])

        self.assertEqual(self.session.turn_no, 0)
        self.assertEqual(self.session.state["world"]["time_left"], 8)

    def test_quote_previews_storylet_effects_without_mutating_state(self) -> None:
        self.session.state["positions"]["player"] = "servant_corridor"
        self.session.state["positions"]["maid"] = "servant_corridor"
        self.session.state["flags"]["maid_warned_player"] = True
        self.session.consumed.add("maid_warning")

        quote = self.session.quote("negotiate", ["maid"])
        changes = {path: (previous, new) for path, previous, new in quote["expected_changes"]}

        self.assertEqual(changes["maid.trust"], (2, 3))
        self.assertEqual(
            changes["item_locations.servant_key"][1],
            {"type": "carried_by", "id": "player"},
        )
        self.assertEqual(self.session.state["characters"]["maid"]["trust"], 2)
        self.assertEqual(
            self.session.state["item_locations"]["servant_key"],
            {"type": "carried_by", "id": "maid"},
        )

        result = self.session.resolve(quote_id=quote["quote_id"])
        actual: dict[str, tuple[object, object]] = {}
        for path, previous, new in result.changes:
            if path == "world.time_left":
                continue
            if path not in actual:
                actual[path] = (previous, new)
            else:
                actual[path] = (actual[path][0], new)
        actual = {path: change for path, change in actual.items() if change[0] != change[1]}

        self.assertEqual(actual, changes)
        self.assertEqual(self.session.state["characters"]["maid"]["trust"], 3)


class CliParsingTests(unittest.TestCase):
    def test_chinese_punctuation_separates_targets(self) -> None:
        self.assertEqual(
            tokenize_action_line("6 仆役侧门钥匙、仆役窄门"),
            ["6", "仆役侧门钥匙", "仆役窄门"],
        )

    def test_numeric_intent_can_be_attached_to_first_target(self) -> None:
        self.assertEqual(
            tokenize_action_line("6仆役侧门钥匙"),
            ["6", "仆役侧门钥匙"],
        )


class SchemaV2ValidationTests(unittest.TestCase):
    def test_current_story_is_valid_schema_v2(self) -> None:
        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        report = validate_content(data)
        self.assertEqual(report.errors, [])

    def test_static_presence_is_rejected_in_schema_v2(self) -> None:
        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(data)
        broken["scenes"]["great_hall"]["available_characters"] = ["butler"]
        report = validate_content(broken)
        self.assertTrue(any("available_characters is forbidden" in error for error in report.errors))

    def test_every_character_requires_one_initial_position(self) -> None:
        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(data)
        del broken["initial_state"]["positions"]["guard"]
        report = validate_content(broken)
        self.assertTrue(any("missing character 'guard'" in error for error in report.errors))

    def test_position_patch_must_use_move_entities(self) -> None:
        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(data)
        broken["storylets"][0]["effect"]["state_patch"]["positions.butler"] = "archive_door"
        report = validate_content(broken)
        self.assertTrue(any("use move_entities" in error for error in report.errors))

    def test_every_item_requires_one_initial_location(self) -> None:
        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(data)
        del broken["initial_state"]["item_locations"]["old_badge"]
        report = validate_content(broken)
        self.assertTrue(any("missing item 'old_badge'" in error for error in report.errors))

    def test_item_location_patch_must_use_move_items(self) -> None:
        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(data)
        broken["storylets"][0]["effect"]["state_patch"]["item_locations.old_badge"] = {
            "type": "carried_by",
            "id": "player",
        }
        report = validate_content(broken)
        self.assertTrue(any("use move_items" in error for error in report.errors))

    def test_object_visibility_condition_must_be_a_mapping(self) -> None:
        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(data)
        broken["scenes"]["archive_room"]["available_objects"]["items"][
            "hidden_compartment"
        ]["visible_when"] = "always"

        report = validate_content(broken)

        self.assertTrue(any("visible_when must be a mapping" in error for error in report.errors))

    def test_required_storylet_match_needs_an_authored_interaction(self) -> None:
        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(data)
        broken["intents"]["strict_action"] = {
            "label": "严格动作",
            "description": "测试动作",
            "quote_required": True,
            "requires_storylet_match": True,
        }

        report = validate_content(broken)

        self.assertTrue(
            any(
                "requires_storylet_match is true but no action storylet" in error
                for error in report.errors
            )
        )


if __name__ == "__main__":
    unittest.main()
