"""WebGame facade: turn streaming events, ideas lifecycle, history restore."""

from __future__ import annotations

import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.llm import ScriptedProvider
from server.engine.llm_protocol import SuggestedAction, new_protocol_id
from server.web import WebGame


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


def suggestion(action_text: str, revision: int = 0) -> SuggestedAction:
    return SuggestedAction(
        suggestion_id=new_protocol_id("suggestion"),
        perception_revision=revision,
        title="先说明用途",
        action_text=action_text,
        focus="social",
        rationale="让对方听清计划。",
    )


class WebGameTest(unittest.TestCase):
    def game(
        self,
        provider: ScriptedProvider,
        *,
        prose_editor_mode: str = "off",
        show_editor_comparison: bool = False,
    ) -> WebGame:
        return WebGame(
            Story.load(FIXTURE),
            provider,
            prose_editor_mode=prose_editor_mode,
            show_editor_comparison=show_editor_comparison,
        )

    def test_turn_streams_delta_then_committed_and_updates_snapshot(self) -> None:
        game = self.game(ScriptedProvider(
            narratives=["周师傅点点头，答应把防雨布借给你。"],
            fact_extractions=[()],
        ))
        events: list[dict] = []
        game.run_turn("我说明来意。", events.append)

        self.assertEqual(
            [event["type"] for event in events], ["delta", "committed"]
        )
        self.assertEqual(events[0]["text"], "周师傅点点头，答应把防雨布借给你。")
        committed = events[1]
        self.assertEqual(
            committed["narrative"], "周师傅点点头，答应把防雨布借给你。"
        )
        self.assertEqual(committed["snapshot"]["turn_no"], 1)
        self.assertEqual(game.session.turn_no, 1)

    def test_provider_failure_emits_error_and_commits_nothing(self) -> None:
        game = self.game(ScriptedProvider(
            narratives=["周师傅点点头。"],
            fact_extractions=[],  # 抽取队列为空 → LLMProviderError
        ))
        events: list[dict] = []
        game.run_turn("我说明来意。", events.append)

        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("本回合未提交", events[-1]["message"])
        self.assertEqual(game.session.turn_no, 0)

    def test_editor_on_emits_only_the_selected_candidate(self) -> None:
        draft = "周师傅点了点头，又再次答应把防雨布借给你。"
        edited = "周师傅点了点头，答应把防雨布借给你。"
        game = self.game(
            ScriptedProvider(
                narratives=[draft],
                prose_edits=[edited],
                fact_extractions=[(), ()],
            ),
            prose_editor_mode="on",
        )
        events: list[dict] = []
        game.run_turn("我说明来意。", events.append)

        self.assertEqual(
            [event["type"] for event in events], ["delta", "committed"]
        )
        self.assertEqual(events[0]["text"], edited)
        self.assertEqual(events[1]["narrative"], edited)

    def test_development_panel_event_exposes_shadow_comparison(self) -> None:
        draft = (
            "周师傅没有立刻回答。他把扳手搁在桌边，等你把用途说完，"
            "才慢慢点了点头。"
        )
        edited = (
            "周师傅没有立刻回答。他把扳手搁在桌边，听完你的用途，"
            "才慢慢点头。"
        )
        game = self.game(
            ScriptedProvider(
                narratives=[draft],
                prose_edits=[edited],
                fact_extractions=[()],
            ),
            prose_editor_mode="shadow",
            show_editor_comparison=True,
        )
        events: list[dict] = []
        game.run_turn("我说明来意。", events.append)

        self.assertEqual(
            [event["type"] for event in events],
            ["delta", "editor_comparison", "committed"],
        )
        comparison = events[1]
        self.assertEqual(comparison["draft"], draft)
        self.assertEqual(comparison["candidate"], edited)
        self.assertEqual(comparison["mode"], "shadow")
        self.assertEqual(comparison["selected"], "draft")
        self.assertEqual(comparison["reason"], "shadow_mode")
        self.assertTrue(comparison["guard"]["accepted"])

        state = game.state()
        self.assertEqual(state["developer"]["prose_editor_mode"], "shadow")
        self.assertEqual(state["editor_comparisons"][0]["candidate"], edited)

    def test_development_panel_marks_adopted_on_candidate(self) -> None:
        draft = "周师傅点了点头，又再次答应把防雨布借给你。"
        edited = "周师傅点了点头，答应把防雨布借给你。"
        game = self.game(
            ScriptedProvider(
                narratives=[draft],
                prose_edits=[edited],
                fact_extractions=[(), ()],
            ),
            prose_editor_mode="on",
            show_editor_comparison=True,
        )
        events: list[dict] = []
        game.run_turn("我说明来意。", events.append)

        self.assertEqual(
            [event["type"] for event in events],
            ["delta", "editor_comparison", "committed"],
        )
        self.assertEqual(events[0]["text"], edited)
        self.assertEqual(events[1]["selected"], "edited")
        self.assertEqual(events[1]["reason"], "edited_candidate_accepted")
        self.assertEqual(events[2]["narrative"], edited)

    def test_comparison_data_is_absent_without_development_flag(self) -> None:
        game = self.game(ScriptedProvider())
        state = game.state()
        self.assertNotIn("developer", state)
        self.assertNotIn("editor_comparisons", state)

    def test_ideas_are_cached_until_refresh(self) -> None:
        provider = ScriptedProvider(
            suggestion_batches=[
                (suggestion("我先说明防雨布的用途。"),),
                (suggestion("我改问周师傅的顾虑。"),),
            ],
        )
        game = self.game(provider)

        first = game.ideas()
        again = game.ideas()
        self.assertEqual(
            first["suggestion_set_id"], again["suggestion_set_id"]
        )

        refreshed = game.ideas(refresh=True)
        self.assertNotEqual(
            first["suggestion_set_id"], refreshed["suggestion_set_id"]
        )
        self.assertTrue(
            any(
                "顾虑" in action["action_text"]
                for action in refreshed["actions"]
            )
        )

    def test_select_idea_executes_action_and_clears_the_set(self) -> None:
        provider = ScriptedProvider(
            narratives=["周师傅听完，把防雨布的事应了下来。"],
            suggestion_batches=[
                (suggestion("我先说明防雨布的用途。"),),
                (suggestion("我改问周师傅的顾虑。"),),
            ],
            fact_extractions=[()],
        )
        game = self.game(provider)
        cards = game.ideas()["actions"]

        events: list[dict] = []
        game.select_idea(cards[0]["suggestion_id"], events.append)

        self.assertEqual(
            [event["type"] for event in events],
            ["player_text", "delta", "committed"],
        )
        self.assertEqual(events[0]["text"], "我先说明防雨布的用途。")
        self.assertEqual(game.session.turn_no, 1)

        # 选用后集合被清空；再次请求会生成新的一批。
        renewed = game.ideas()
        self.assertTrue(
            any(
                "顾虑" in action["action_text"]
                for action in renewed["actions"]
            )
        )

    def test_select_unknown_idea_emits_error_without_a_turn(self) -> None:
        game = self.game(ScriptedProvider(
            suggestion_batches=[(suggestion("我先说明防雨布的用途。"),)],
        ))
        game.ideas()
        events: list[dict] = []
        game.select_idea("suggestion_missing", events.append)

        self.assertEqual([event["type"] for event in events], ["error"])
        self.assertEqual(game.session.turn_no, 0)

    def test_state_returns_intro_and_committed_history(self) -> None:
        game = self.game(ScriptedProvider(
            narratives=["周师傅点点头，答应把防雨布借给你。"],
            fact_extractions=[()],
        ))
        game.run_turn("我说明来意。", lambda event: None)

        payload = game.state()
        self.assertTrue(payload["intro"])
        self.assertIn("修理铺", payload["snapshot"]["scene_name"])
        self.assertEqual(len(payload["history"]), 1)
        self.assertEqual(payload["history"][0]["player_text"], "我说明来意。")
        self.assertIn("防雨布", payload["history"][0]["narrative"])


if __name__ == "__main__":
    unittest.main()
