from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from server.engine.content import Story
from server.engine.llm_protocol import FactBatch
from server.engine.session import GameSession, SessionError
from server.engine.world import board_neighbors, next_hop_toward
from tools.validate_content import validate_content


ROOT = Path(__file__).resolve().parent.parent
ROOFTOP = ROOT / "content" / "rooftop_supper.yaml"
ARCHIVE = ROOT / "content" / "midnight_archive.yaml"


class SpatialGraphTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(ROOFTOP)

    def test_board_neighbors_are_authored_and_bidirectional(self) -> None:
        self.assertEqual(
            board_neighbors(self.story, "building_lobby"),
            ["convenience_store", "laundromat", "rooftop"],
        )
        self.assertIn("building_lobby", board_neighbors(self.story, "rooftop"))

    def test_shortest_path_returns_one_adjacent_hop(self) -> None:
        self.assertEqual(
            next_hop_toward(self.story, "building_lobby", "rooftop"),
            "rooftop",
        )
        self.assertIsNone(next_hop_toward(self.story, "building_lobby", "missing"))


class FactCommitSkeletonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(ROOFTOP)
        self.session = GameSession(self.story, log_dir=None)

    def test_first_fact_commit_runs_the_opening_anchor_once(self) -> None:
        first = self.session.commit_fact_batch(FactBatch(
            state_revision=self.session.state_revision,
            player_text="我先听陈阿姨说完。",
            narrative="你在门厅里停下脚步。",
        ))
        second = self.session.commit_fact_batch(FactBatch(
            state_revision=self.session.state_revision,
            player_text="我再看看公告栏。",
            narrative="你抬头看了看旧公告。",
        ))

        self.assertEqual(first.fired, ["opening_offer"])
        self.assertTrue(self.session.state["flags"]["opening_delivered"])
        self.assertEqual(second.fired, [])
        self.assertEqual(self.session.turn_no, 2)
        self.assertEqual(self.session.state_revision, 2)

    def test_prose_only_commit_never_asserts_movement_or_item_transfer(self) -> None:
        before_positions = copy.deepcopy(self.session.state["positions"])
        before_items = copy.deepcopy(self.session.state["item_locations"])

        result = self.session.commit_narrative(
            "我拿起饭篮走向天台。",
            "你伸手示意自己的打算。",
        )

        self.assertEqual(self.session.state["positions"], before_positions)
        self.assertEqual(self.session.state["item_locations"], before_items)
        self.assertNotIn("positions.player", {path for path, _, _ in result.changes})
        self.assertNotIn("item_locations.food_basket", {
            path for path, _, _ in result.changes
        })

    def test_archive_cannot_start_a_new_runtime_session(self) -> None:
        with self.assertRaisesRegex(SessionError, "narrative_first"):
            GameSession(Story.load(ARCHIVE), log_dir=None)


class NarrativeContentValidationTest(unittest.TestCase):
    def load(self) -> dict:
        return yaml.safe_load(ROOFTOP.read_text(encoding="utf-8"))

    def test_active_and_archived_content_both_validate(self) -> None:
        active = validate_content(self.load())
        archived = validate_content(yaml.safe_load(ARCHIVE.read_text(encoding="utf-8")))
        self.assertEqual(active.errors, [])
        self.assertEqual(active.warnings, [])
        self.assertEqual(archived.errors, [])

    def test_retired_top_level_mechanics_are_rejected(self) -> None:
        for field, value in (
            ("intents", {}),
            ("quote_warnings", []),
            ("resolution_limits", {}),
            ("world_rules", []),
        ):
            broken = self.load()
            broken[field] = value
            report = validate_content(broken)
            self.assertTrue(
                any(f"{field} is retired" in error for error in report.errors),
                (field, report.errors),
            )

    def test_item_request_policy_and_scene_suggestions_are_rejected(self) -> None:
        broken = self.load()
        broken["items"]["picnic_mat"]["request_policy"] = {
            "purposes": {"cover": "铺桌"}
        }
        broken["scenes"]["building_lobby"]["suggested_intents"] = ["talk"]
        report = validate_content(broken)
        self.assertTrue(any("request_policy is retired" in e for e in report.errors))
        self.assertTrue(any("suggested_intents is retired" in e for e in report.errors))

    def test_npc_card_requires_motivation_voice_and_relationship(self) -> None:
        broken = self.load()
        for field in ("motivation", "voice", "initial_relationship"):
            del broken["characters"]["clerk_luo"][field]
        report = validate_content(broken)
        for field in ("motivation", "voice", "initial_relationship"):
            self.assertTrue(any(
                f"missing required field: {field}" in error
                for error in report.errors
            ))

    def test_anchor_cannot_route_an_action(self) -> None:
        broken = self.load()
        broken["storylets"][0]["trigger"]["intent"] = "talk"
        report = validate_content(broken)
        self.assertTrue(any("trigger.intent is retired" in e for e in report.errors))

    def test_schema_v2_still_requires_positions_and_item_locations(self) -> None:
        missing_character = self.load()
        del missing_character["initial_state"]["positions"]["ahe"]
        self.assertTrue(any(
            "missing character 'ahe'" in e
            for e in validate_content(missing_character).errors
        ))

        missing_item = self.load()
        del missing_item["initial_state"]["item_locations"]["food_basket"]
        self.assertTrue(any(
            "missing item 'food_basket'" in e
            for e in validate_content(missing_item).errors
        ))


if __name__ == "__main__":
    unittest.main()
