from __future__ import annotations

import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.llm import HeuristicMockProvider, ScriptedProvider
from server.engine.llm_loop import commit_action, run_action_loop
from server.engine.llm_protocol import ActionPlan, CapabilityAction
from server.engine.session import GameSession
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "rooftop_supper.yaml"


def request_plan(revision: int, purpose: str = "table_cover") -> ActionPlan:
    text = "我问罗叔有没有能借来铺桌的干净东西。"
    return ActionPlan(
        plan_id=f"plan_request_{purpose}",
        perception_revision=revision,
        player_text=text,
        interpretation="你想向罗叔询问一件能铺桌的物品。",
        goal="request_table_cover",
        steps=(CapabilityAction(
            capability="social",
            action="request_item",
            arguments={"owner_id": "clerk_luo", "purpose": purpose},
            purpose="请求一件能铺桌的物品",
        ),),
        references=("clerk_luo",),
    )


class SocialRequestItemCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)
        self.recorder = TraceRecorder(None, self.session.session_id)
        self.session.resolve(
            intent_id="talk",
            objects=["aunt_chen"],
            generic_patch={"aunt_chen.rapport": 1},
        )
        self.session.resolve(intent_id="move", objects=["convenience_store"])

    def test_tool_exposes_a_purpose_without_leaking_the_hidden_item(self) -> None:
        tools = {
            tool.tool_id: tool for tool in self.session.perception().capability_tools
        }

        self.assertIn("social.request_item", tools)
        schema = tools["social.request_item"].arguments_schema
        self.assertEqual(
            schema["properties"]["purpose"]["enum"], ["table_cover"]
        )
        self.assertNotIn("item_id", schema["properties"])
        self.assertNotIn("picnic_mat", str(schema))

    def test_request_transfers_item_without_an_authored_interaction_card(self) -> None:
        loop_result = run_action_loop(
            self.session,
            ScriptedProvider([request_plan(self.session.state_revision)]),
            "我问罗叔有没有能借来铺桌的干净东西。",
            self.recorder,
        )

        self.assertTrue(loop_result.can_execute)
        self.assertFalse(loop_result.confirmation_required)
        outcome = commit_action(self.session, loop_result, self.recorder)

        self.assertEqual(
            self.session.state["item_locations"]["picnic_mat"],
            {"type": "carried_by", "id": "player"},
        )
        self.assertNotIn("clerk_lends_picnic_mat", outcome.fired_storylets)
        self.assertEqual(outcome.primary_goal_status, "achieved")
        self.assertIn("social.request_item", outcome.resolution_sources)
        self.assertFalse(outcome.cost_only)
        self.assertNotIn(
            "social.request_item",
            {tool.tool_id for tool in self.session.perception().capability_tools},
        )

    def test_free_text_prefers_the_specialized_capability_over_talk(self) -> None:
        loop_result = run_action_loop(
            self.session,
            HeuristicMockProvider(),
            "我问罗叔能不能借个铺桌的干净东西",
            self.recorder,
        )

        self.assertEqual(
            loop_result.plan.steps[0].tool_id,
            "social.request_item",
        )
        self.assertTrue(loop_result.can_execute)

    def test_refusal_is_a_committed_result_not_a_validation_error(self) -> None:
        self.session.state["characters"]["clerk_luo"]["rapport"] = 0
        loop_result = run_action_loop(
            self.session,
            ScriptedProvider([request_plan(self.session.state_revision)]),
            "我问罗叔有没有能借来铺桌的干净东西。",
            self.recorder,
        )

        self.assertTrue(loop_result.can_execute)
        outcome = commit_action(self.session, loop_result, self.recorder)

        self.assertEqual(
            self.session.state["item_locations"]["picnic_mat"],
            {"type": "carried_by", "id": "clerk_luo"},
        )
        self.assertEqual(outcome.primary_goal_status, "not_achieved")
        self.assertIn("social.request_item", outcome.resolution_sources)
        self.assertFalse(outcome.cost_only)

    def test_unknown_purpose_is_rejected_before_spending_a_turn(self) -> None:
        turn_before = self.session.turn_no
        validation = self.session.validate_plan(
            request_plan(self.session.state_revision, "unknown_use")
        )

        self.assertFalse(validation.can_execute)
        self.assertIn(
            "social.request_item_invalid",
            {issue.code for issue in validation.issues},
        )
        self.assertEqual(self.session.turn_no, turn_before)


if __name__ == "__main__":
    unittest.main()
