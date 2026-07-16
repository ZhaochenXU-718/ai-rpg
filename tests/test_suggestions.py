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
STORY_PATH = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


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

    def test_exit_card_remains_a_proposal_without_execution_metadata(self) -> None:
        suggestions = generate_action_suggestions(
            self.session, self.provider, self.recorder
        )
        movement = next(
            action for action in suggestions.actions
            if "动身" in action.action_text
        )
        self.assertNotIn("expected_changes", movement.to_dict())

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

    def test_hidden_authored_entities_are_removed_from_prompt_and_cards(self) -> None:
        class CapturingProvider(ScriptedProvider):
            request = None

            def propose_suggestions(self, request):
                self.request = request
                return super().propose_suggestions(request)

        visible_dialogue = SuggestedAction(
            suggestion_id="suggestion_visible",
            perception_revision=self.session.state_revision,
            title="直接询问",
            action_text="周师傅，我把防雨布的用途说明一下。",
            focus="social",
            rationale="只回应当前可见人物。",
        )
        hidden_character = SuggestedAction(
            suggestion_id="suggestion_hidden",
            perception_revision=self.session.state_revision,
            title="询问林姐",
            action_text="我问问林姐院子里的长桌该怎么遮。",
            focus="investigate",
            rationale="引用当前不可见的作者人物。",
        )
        provider = CapturingProvider(
            suggestion_batches=[(visible_dialogue, hidden_character)]
        )

        suggestions = generate_action_suggestions(
            self.session, provider, self.recorder
        )

        prompt_boundaries = "\n".join(provider.request.boundaries)
        self.assertNotIn("林姐", prompt_boundaries)
        rendered = "\n".join(
            action.title + action.action_text + action.rationale
            for action in suggestions.actions
        )
        self.assertNotIn("林姐", rendered)
        self.assertTrue(all(
            action.action_text.startswith("我")
            for action in suggestions.actions
        ))
        self.assertIn(
            "我对周师傅说：“我把防雨布的用途说明一下。”",
            {action.action_text for action in suggestions.actions},
        )


if __name__ == "__main__":
    unittest.main()
