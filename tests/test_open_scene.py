from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from server.engine.content import Story
from server.engine.llm import ScriptedProvider
from server.engine.llm_protocol import (
    ActionPlan,
    CapabilityAction,
    DirectorBeat,
    DirectorBeatKind,
)
from server.engine.session import GameSession
from tools.validate_content import validate_content


ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


class OpenSceneFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(FIXTURE_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def test_fixture_has_no_ordinary_storylets_and_validates_cleanly(self) -> None:
        data = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))
        report = validate_content(data)

        self.assertEqual(data["storylets"], [])
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_request_item_achieves_the_goal_without_an_authored_storylet(self) -> None:
        text = "我问周师傅有没有能临时盖住长桌的东西。"
        plan = ActionPlan(
            plan_id="plan_request_canvas",
            perception_revision=0,
            player_text=text,
            interpretation="向周师傅请求一件能临时盖住长桌的东西。",
            goal="request_table_cover",
            steps=(CapabilityAction(
                capability="social",
                action="request_item",
                arguments={
                    "owner_id": "keeper_zhou",
                    "purpose": "table_cover",
                },
                purpose="请求遮盖物",
            ),),
            references=("keeper_zhou",),
        )

        validation = self.session.validate_plan(plan)
        outcome = self.session.resolve_plan(plan, validation)

        self.assertEqual(outcome.fired_storylets, ())
        self.assertEqual(outcome.primary_goal_status, "achieved")
        self.assertEqual(outcome.ending, "canvas_borrowed")
        self.assertEqual(
            self.session.state["item_locations"]["rain_canvas"],
            {"type": "carried_by", "id": "player"},
        )

    def test_director_can_only_move_the_authored_adjacent_neighbor(self) -> None:
        text = "我看看工具墙和半开的门。"
        plan = ActionPlan(
            plan_id="plan_observe_workshop",
            perception_revision=0,
            player_text=text,
            interpretation="观察修理铺当前可见的环境。",
            goal="inspect_workshop",
            intent_id="observe",
            steps=(CapabilityAction(
                capability="intent",
                action="observe",
                arguments={"objects": ["tool_wall"]},
                purpose="观察工具墙",
            ),),
            references=("tool_wall",),
        )
        beat = DirectorBeat(
            beat_id="beat_neighbor_enters",
            state_revision=1,
            kind=DirectorBeatKind.ENTER_SCENE,
            actor_id="neighbor_lin",
            target_location_id="workshop",
            target_ids=("player",),
            summary="林姐从院子走到门口，先看了看长桌需要遮住的范围。",
            motivation="确认是否确实需要搭手，不替别人做决定。",
        )
        provider = ScriptedProvider([], director_batches=[(beat,)])

        validation = self.session.validate_plan(plan)
        outcome = self.session.resolve_plan(
            plan,
            validation,
            director_provider=provider,
        )

        self.assertEqual(outcome.fired_storylets, ())
        self.assertEqual(
            self.session.state["positions"]["neighbor_lin"],
            "workshop",
        )
        self.assertEqual(outcome.director_beats[0].actor_id, "neighbor_lin")
        self.assertNotIn("invented_neighbor", self.session.state["positions"])


if __name__ == "__main__":
    unittest.main()
