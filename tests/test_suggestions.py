from __future__ import annotations

import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.llm import HeuristicMockProvider, ScriptedProvider
from server.engine.llm_protocol import SuggestedAction
from server.engine.session import GameSession, SessionError
from server.engine.suggestions import generate_action_suggestions, prepare_suggested_action
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "rooftop_supper.yaml"


class SuggestedActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GameSession(Story.load(STORY_PATH), log_dir=None)
        self.provider = HeuristicMockProvider()
        self.recorder = TraceRecorder(None, self.session.session_id)

    def test_cards_are_distinct_editable_prose_not_frozen_plans(self) -> None:
        suggestions = generate_action_suggestions(
            self.session, self.provider, self.recorder
        )
        self.assertGreaterEqual(len(suggestions.actions), 3)
        self.assertLessEqual(len(suggestions.actions), 5)
        self.assertEqual(suggestions.perception_revision, self.session.state_revision)
        self.assertEqual(
            len({action.action_text for action in suggestions.actions}),
            len(suggestions.actions),
        )
        self.assertTrue(all(not hasattr(action, "plan") for action in suggestions.actions))
        self.assertTrue(all(not hasattr(action, "validation") for action in suggestions.actions))

    def test_exit_card_discloses_possible_position_touch(self) -> None:
        suggestions = generate_action_suggestions(
            self.session, self.provider, self.recorder
        )
        movement = next(
            action for action in suggestions.actions
            if "人物位置" in action.expected_iron_law_touches
        )
        self.assertIn("动身", movement.action_text)

    def test_selection_returns_editable_action_text_and_cards_expire(self) -> None:
        suggestions = generate_action_suggestions(
            self.session, self.provider, self.recorder
        )
        card = suggestions.actions[0]
        self.assertEqual(prepare_suggested_action(self.session, card), card.action_text)

        self.session.commit_narrative("我先看看。", "你停下来观察四周。")
        with self.assertRaisesRegex(SessionError, "已经过期"):
            prepare_suggested_action(self.session, card)

    def test_stale_provider_card_is_not_rewrapped_as_current(self) -> None:
        stale_text = "我采用一张基于陈旧世界视图的卡片。"
        provider = ScriptedProvider(suggestion_batches=[(SuggestedAction(
            suggestion_id="suggestion_stale",
            perception_revision=self.session.state_revision + 1,
            title="陈旧卡片",
            action_text=stale_text,
            focus="cautious",
            rationale="测试陈旧 revision。",
        ),)])
        suggestions = generate_action_suggestions(
            self.session, provider, self.recorder
        )
        self.assertNotIn(stale_text, {
            action.action_text for action in suggestions.actions
        })


if __name__ == "__main__":
    unittest.main()
