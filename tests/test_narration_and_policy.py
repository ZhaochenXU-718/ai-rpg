"""Narrative-first prose stays behind perception and never invents facts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.llm import NarrativeResponse, ScriptedProvider
from server.engine.narration import (
    build_narrative_facts,
    match_references,
    narrate_player_turn,
)
from server.engine.renderer import render_turn
from server.engine.session import GameSession


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "rooftop_supper.yaml"


class NarrativeProvider(ScriptedProvider):
    def __init__(self, text: str | None) -> None:
        super().__init__([])
        self.text = text
        self.requests = []

    def render_narrative(self, request):
        self.requests.append(request)
        if self.text is None:
            return None
        return NarrativeResponse(
            text=self.text,
            model="narrative-test",
            prompt_version="test-v1",
        )


class NarrationFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def test_reference_matching_only_grounds_currently_visible_entities(self) -> None:
        references = match_references(
            self.session, "我先问问陈阿姨，再看看蓝白格布。"
        )
        self.assertEqual(references, ("aunt_chen",))

    def test_fact_sheet_hides_remote_items_and_requires_explicit_changes(self) -> None:
        references = match_references(self.session, "我和陈阿姨聊聊。")
        facts = build_narrative_facts(
            self.session, "我和陈阿姨聊聊。", references
        )
        flat = json.dumps(facts, ensure_ascii=False)
        self.assertIn("陈阿姨", flat)
        self.assertNotIn("蓝白格布", flat)
        self.assertNotIn("checked_cloth", flat)
        self.assertIn("必须在散文中明确写出", facts["事实表达要求"])
        self.assertNotIn("secret", flat)

    def test_provider_prose_and_honest_fallback(self) -> None:
        provider = NarrativeProvider("陈阿姨把手机收进口袋，等你把话说完。")
        narrative, references = narrate_player_turn(
            self.session, provider, "我请陈阿姨先说说她的打算。"
        )
        self.assertIn("把手机收进口袋", narrative)
        self.assertEqual(references, ("aunt_chen",))
        self.assertEqual(provider.requests[0].kind, "turn")

        fallback, _ = narrate_player_turn(
            self.session, NarrativeProvider(None), "我拿起饭篮走上天台。"
        )
        self.assertIn("开始尝试", fallback)
        self.assertIn("没有出现足以写入账本的变化", fallback)

    def test_prose_only_turn_has_no_empty_mechanics_receipt(self) -> None:
        result = self.session.commit_narrative(
            "我先听陈阿姨说完。", "你在门厅里停下脚步，认真听她把话说完。"
        )
        rendered = render_turn(self.story, result, self.session.state)
        self.assertIn("认真听她把话说完", rendered)
        self.assertNotIn("物理事实提交", rendered)
        self.assertNotIn("判定", rendered)
        self.assertNotIn("意图", rendered)
        self.assertNotIn("报价", rendered)

    def test_perception_is_player_scoped_without_a_capability_menu(self) -> None:
        perception = self.session.perception()
        self.assertEqual(perception.audience.value, "player")
        self.assertEqual(perception.subject_id, "player")
        self.assertFalse(hasattr(perception, "available_intents"))
        self.assertFalse(hasattr(perception, "capability_tools"))
        visible = {entity.entity_id for entity in perception.visible_entities}
        self.assertIn("aunt_chen", visible)
        self.assertNotIn("xiaoyu", visible)

    def test_npc_perception_has_its_own_location_and_private_context(self) -> None:
        perception = self.session.subject_perception("xiaoyu")
        self.assertEqual(perception.audience.value, "npc")
        self.assertEqual(perception.subject_id, "xiaoyu")
        self.assertEqual(perception.location_id, "laundromat")
        self.assertIn("secret", perception.subject_context)
        self.assertNotIn(
            perception.subject_context["secret"],
            json.dumps(self.session.perception().to_dict(), ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
