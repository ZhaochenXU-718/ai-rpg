from __future__ import annotations

import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.llm import HeuristicMockProvider
from server.engine.llm_loop import commit_action
from server.engine.session import GameSession, SessionError
from server.engine.suggestions import (
    generate_action_suggestions,
    prepare_suggested_action,
)
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "rooftop_supper.yaml"


class SuggestedActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)
        self.provider = HeuristicMockProvider()
        self.recorder = TraceRecorder(None, self.session.session_id)
        self.session.resolve(
            intent_id="talk",
            objects=["aunt_chen"],
            generic_patch={"aunt_chen.rapport": 1},
        )
        self.session.resolve(intent_id="move", objects=["convenience_store"])

    def test_generation_exposes_only_distinct_validated_cards(self) -> None:
        suggestions = generate_action_suggestions(
            self.session, self.provider, self.recorder
        )

        self.assertGreaterEqual(len(suggestions.actions), 3)
        self.assertLessEqual(len(suggestions.actions), 5)
        self.assertEqual(
            suggestions.perception_revision,
            self.session.state_revision,
        )
        signatures = {
            (
                action.plan.steps[0].tool_id,
                str(action.plan.steps[0].arguments),
            )
            for action in suggestions.actions
        }
        self.assertEqual(len(signatures), len(suggestions.actions))
        self.assertTrue(all(
            action.validation.can_execute for action in suggestions.actions
        ))

    def test_selecting_card_executes_its_frozen_plan_without_reinterpretation(self) -> None:
        suggestions = generate_action_suggestions(
            self.session, self.provider, self.recorder
        )
        request_card = next(
            action
            for action in suggestions.actions
            if action.plan.steps[0].tool_id == "social.request_item"
        )

        loop_result = prepare_suggested_action(self.session, request_card)
        self.assertEqual(loop_result.plan, request_card.plan)
        self.assertFalse(loop_result.confirmation_required)
        outcome = commit_action(self.session, loop_result, self.recorder)

        self.assertEqual(
            self.session.state["item_locations"]["picnic_mat"],
            {"type": "carried_by", "id": "player"},
        )
        self.assertEqual(outcome.primary_goal_status, "achieved")

    def test_cards_expire_after_any_state_revision_change(self) -> None:
        suggestions = generate_action_suggestions(
            self.session, self.provider, self.recorder
        )
        old_card = suggestions.actions[0]
        self.session.resolve(intent_id="observe", objects=["order_map"])

        with self.assertRaisesRegex(SessionError, "已经过期"):
            prepare_suggested_action(self.session, old_card)


if __name__ == "__main__":
    unittest.main()
