"""Prompt snapshot tests: which authored layer enters which model call.

Each sentinel below is authored in exactly one fixture layer, so its
presence in a rendered prompt proves the routing decision for that layer:

- narrator gets blueprint, guidelines, reminders and in-scene private cards;
- suggestions get only the desensitized direction (no hidden_truth, no cards);
- fact extraction and player perception get no authored private layer;
- player_facing_summary enters no model call at all.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from server.engine.author_context import build_author_context
from server.engine.content import Story
from server.engine.llm import (
    FactExtractionRequest,
    LLMProviderError,
    NarrativeResponse,
    ScriptedProvider,
)
from server.engine.llm_deepseek import (
    RENDER_SYSTEM_PROMPT,
    build_fact_extraction_messages,
    build_narrative_messages,
    build_suggestion_messages,
)
from server.engine.narration import narrate_player_turn
from server.engine.session import GameSession
from server.engine.suggestions import generate_action_suggestions
from server.engine.trace import TraceRecorder
from tools.validate_content import validate_content


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"

SECRET_MARK = "亡妻"  # keeper_zhou.secret 与 ai_plot.hidden_truth
PRESSURE_MARK = "寒暄上耗太久"  # keeper_zhou.pressure
MOTIVATION_MARK = "不耽误营业"  # keeper_zhou.motivation
PLAYER_SUMMARY_MARK = "看似小事"  # player_facing_summary
GUIDELINE_MARK = "戏剧腔"  # narrative_guidelines
REMINDER_MARK = "防雨布的来历"  # critical_reminders
CONTRACT_MARK = "分寸感"  # emotional_contract
BLUEPRINT_MARK = "合适的遮盖物"  # ai_plot.main_goal


class CapturingNarrator(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.requests = []

    def render_narrative(self, request):
        self.requests.append(request)
        return NarrativeResponse(
            text="周师傅擦了擦手，等你把话说完。",
            model="capture",
        )


class CapturingSuggester(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.requests = []

    def propose_suggestions(self, request):
        self.requests.append(request)
        raise LLMProviderError("capture only")


class AuthorContextRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def _narrative_request(self):
        provider = CapturingNarrator()
        narrate_player_turn(self.session, provider, "我向周师傅说明借防雨布的用途。")
        return provider.requests[0]

    def _suggestion_request(self):
        provider = CapturingSuggester()
        recorder = TraceRecorder(None, self.session.session_id)
        generate_action_suggestions(self.session, provider, recorder)
        return provider.requests[0]

    def test_author_context_holds_only_in_scene_private_cards(self) -> None:
        request = self._narrative_request()
        context = request.author_context
        self.assertIsNotNone(context)
        card_ids = [card.character_id for card in context.in_scene_character_cards]
        self.assertEqual(card_ids, ["keeper_zhou"])
        card = context.in_scene_character_cards[0]
        self.assertIn(SECRET_MARK, card.secret)
        self.assertIn(PRESSURE_MARK, card.pressure)
        self.assertTrue(card.dialogue_examples)

    def test_player_perception_never_carries_author_private_layers(self) -> None:
        flat = json.dumps(self.session.perception().to_dict(), ensure_ascii=False)
        for mark in (
            SECRET_MARK,
            PRESSURE_MARK,
            MOTIVATION_MARK,
            PLAYER_SUMMARY_MARK,
            GUIDELINE_MARK,
            REMINDER_MARK,
            CONTRACT_MARK,
            BLUEPRINT_MARK,
        ):
            self.assertNotIn(mark, flat)

    def test_narrator_prompt_gets_blueprint_cards_and_reminders(self) -> None:
        request = self._narrative_request()
        user_payload = build_narrative_messages(request)[1]["content"]
        for mark in (
            SECRET_MARK,
            PRESSURE_MARK,
            MOTIVATION_MARK,
            GUIDELINE_MARK,
            REMINDER_MARK,
            CONTRACT_MARK,
            BLUEPRINT_MARK,
        ):
            self.assertIn(mark, user_payload)
        self.assertNotIn(PLAYER_SUMMARY_MARK, user_payload)

    def test_narrator_system_prompt_guards_disclosure_not_knowledge(self) -> None:
        self.assertIn("作者私有上下文", RENDER_SYSTEM_PROMPT)
        self.assertIn("不得直接说破", RENDER_SYSTEM_PROMPT)
        self.assertNotIn("未披露私密信息", RENDER_SYSTEM_PROMPT)

    def test_suggestion_prompt_gets_desensitized_direction_only(self) -> None:
        request = self._suggestion_request()
        flat = json.dumps(
            build_suggestion_messages(request), ensure_ascii=False
        )
        self.assertIn("故事方向", flat)
        self.assertIn(BLUEPRINT_MARK, flat)
        self.assertIn(CONTRACT_MARK, flat)
        for mark in (
            SECRET_MARK,
            PRESSURE_MARK,
            MOTIVATION_MARK,
            PLAYER_SUMMARY_MARK,
            REMINDER_MARK,
        ):
            self.assertNotIn(mark, flat)

    def test_fact_extraction_prompt_carries_no_author_layer(self) -> None:
        request = FactExtractionRequest(
            perception=self.session.perception(),
            player_text="我向周师傅说明用途。",
            narrative="周师傅点点头，把防雨布递给你。",
            ledger={"positions": dict(self.session.state.get("positions") or {})},
        )
        flat = json.dumps(
            build_fact_extraction_messages(request), ensure_ascii=False
        )
        for mark in (
            SECRET_MARK,
            PRESSURE_MARK,
            GUIDELINE_MARK,
            REMINDER_MARK,
            CONTRACT_MARK,
            BLUEPRINT_MARK,
            PLAYER_SUMMARY_MARK,
        ):
            self.assertNotIn(mark, flat)

    def test_story_without_authored_layers_builds_no_context(self) -> None:
        bare = Story({
            "id": "bare",
            "player_role": {"id": "player"},
            "characters": {"player": {"name": "旅人", "role": "player"}},
        })
        self.assertIsNone(build_author_context(bare, ()))
        self.assertIsNone(build_author_context(bare, ("player",)))

    def test_legacy_cards_still_reach_the_narrator_without_new_fields(self) -> None:
        legacy = Story({
            "id": "legacy",
            "player_role": {"id": "player"},
            "characters": {
                "player": {"name": "旅人", "role": "player"},
                "guard": {
                    "name": "守门人",
                    "role": "npc",
                    "public_profile": "守着门。",
                    "motivation": "查清来客身份。",
                    "voice": "简短。",
                    "initial_relationship": "陌生。",
                },
            },
        })
        context = build_author_context(legacy, ("guard",))
        self.assertIsNotNone(context)
        self.assertEqual(
            context.in_scene_character_cards[0].motivation, "查清来客身份。"
        )


class AuthorLayerValidationTest(unittest.TestCase):
    def _data(self) -> dict:
        return yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))

    def test_fixture_with_all_author_layers_validates(self) -> None:
        report = validate_content(self._data())
        self.assertEqual(report.errors, [])

    def test_unknown_ai_plot_field_is_rejected(self) -> None:
        data = self._data()
        data["ai_plot"]["hiden_truth"] = "拼错的键不能静默通过。"
        report = validate_content(data)
        self.assertTrue(
            any("hiden_truth" in error for error in report.errors)
        )

    def test_more_than_four_reminders_are_rejected(self) -> None:
        data = self._data()
        data["critical_reminders"] = [f"规则{index}" for index in range(5)]
        report = validate_content(data)
        self.assertTrue(
            any("critical_reminders" in error for error in report.errors)
        )

    def test_blank_optional_card_field_is_rejected(self) -> None:
        data = self._data()
        data["characters"]["keeper_zhou"]["secret"] = "  "
        report = validate_content(data)
        self.assertTrue(
            any("keeper_zhou.secret" in error for error in report.errors)
        )

    def test_dialogue_examples_must_be_a_list_of_strings(self) -> None:
        data = self._data()
        data["characters"]["keeper_zhou"]["dialogue_examples"] = "单句字符串"
        report = validate_content(data)
        self.assertTrue(
            any("dialogue_examples" in error for error in report.errors)
        )


if __name__ == "__main__":
    unittest.main()
