"""DeepSeek's native narrative, card and Director protocols use no ActionPlan."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from server.engine.llm import (
    DirectorRequest,
    LLMProviderError,
    NarrativeRequest,
    SuggestionRequest,
    create_provider,
)
from server.engine.llm_deepseek import (
    DeepSeekProvider,
    build_director_messages,
    build_suggestion_messages,
    coerce_director_plan,
    coerce_suggestions,
)
from server.engine.llm_protocol import (
    PerceptionAudience,
    PerceptionSnapshot,
)


class FakeTransport:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], dict]] = []

    def __call__(self, messages, options):
        self.calls.append(([dict(message) for message in messages], dict(options)))
        if not self.replies:
            raise AssertionError("fake transport exhausted")
        return self.replies.pop(0), {"total_tokens": 50}


def perception() -> PerceptionSnapshot:
    return PerceptionSnapshot(
        audience=PerceptionAudience.PLAYER,
        subject_id="player",
        story_id="rooftop_supper",
        session_id="session_demo",
        turn_no=0,
        state_revision=0,
        location_id="building_lobby",
        location_name="九号楼门厅",
        current_goal="把饭菜妥善摆上桌",
    )


def director_request(*, generation=None) -> DirectorRequest:
    return DirectorRequest(
        story_id="rooftop_supper",
        state_revision=1,
        turn_no=1,
        location_id="building_lobby",
        location_name="九号楼门厅",
        current_goal="把饭菜妥善摆上桌",
        player_id="player",
        player_action="我先问问陈阿姨。",
        action_targets=("aunt_chen",),
        committed_turn={"narrative": "你停下来问她的打算。"},
        candidates=({
            "actor_id": "aunt_chen",
            "name": "陈阿姨",
            "role": "accidental_host",
            "motivation": "不浪费刚做好的饭菜。",
            "status": "present",
            "can_enter": False,
        },),
        generation=generation or {},
    )


def director_json() -> str:
    return json.dumps({
        "beats": [{
            "kind": "react",
            "actor_id": "aunt_chen",
            "target_location_id": "building_lobby",
            "target_ids": ["player"],
            "summary": "陈阿姨把饭篮往长椅里侧挪了挪，认真回答你的问题。",
            "motivation": "不愿浪费饭菜，也不强迫别人接手。",
        }],
        "local_canon": [],
    }, ensure_ascii=False)


def suggestions_json() -> str:
    return json.dumps({
        "suggestions": [{
            "title": "先说明用途",
            "action_text": "我先向陈阿姨说明自己打算怎样安排这篮饭。",
            "focus": "social",
            "rationale": "让对方听清计划，但不替她作决定。",
        }]
    }, ensure_ascii=False)


class NarrativeProviderTest(unittest.TestCase):
    def request(self) -> NarrativeRequest:
        return NarrativeRequest(
            kind="turn",
            perception=perception(),
            facts={"玩家输入": "我先问问陈阿姨。", "在场可见实体": ["陈阿姨"]},
            style={"tone": "克制"},
        )

    def test_narration_uses_plain_text_mode_and_scoped_prompt(self) -> None:
        transport = FakeTransport(["陈阿姨把饭篮扶稳，等你把问题说完。"])
        response = DeepSeekProvider(transport=transport).render_narrative(self.request())
        self.assertIn("饭篮扶稳", response.text)
        self.assertFalse(transport.calls[0][1]["json_mode"])
        self.assertEqual(response.prompt_version, "deepseek-narrate-v2")
        self.assertIn("subject_id", transport.calls[0][0][1]["content"])

    def test_empty_narration_retries_once_then_succeeds(self) -> None:
        transport = FakeTransport(["", "第二次生成成功。"])
        response = DeepSeekProvider(transport=transport).render_narrative(self.request())
        self.assertEqual(response.text, "第二次生成成功。")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(response.usage["total_tokens"], 100)

    def test_two_empty_replies_return_none(self) -> None:
        transport = FakeTransport(["", ""])
        self.assertIsNone(
            DeepSeekProvider(transport=transport).render_narrative(self.request())
        )


class SuggestionProviderTest(unittest.TestCase):
    def request(self) -> SuggestionRequest:
        return SuggestionRequest(
            perception=perception(),
            count=3,
            boundaries=("不能替别人承诺。",),
        )

    def test_cards_have_no_plan_capability_or_validation_payload(self) -> None:
        cards = coerce_suggestions(suggestions_json(), self.request())
        payload = cards[0].to_dict()
        self.assertEqual(payload["action_text"], "我先向陈阿姨说明自己打算怎样安排这篮饭。")
        self.assertNotIn("plan", payload)
        prompt = build_suggestion_messages(self.request())
        flat = json.dumps(prompt, ensure_ascii=False)
        self.assertNotIn("capability_tools", flat)
        self.assertNotIn("proposed_changes", flat)

    def test_provider_repairs_invalid_card_json(self) -> None:
        transport = FakeTransport(["not json", suggestions_json()])
        response = DeepSeekProvider(transport=transport).propose_suggestions(
            self.request()
        )
        self.assertEqual(len(response.suggestions), 1)
        self.assertEqual(response.prompt_version, "deepseek-suggestions-v2")
        self.assertEqual(response.usage["total_tokens"], 100)


class DirectorProviderTest(unittest.TestCase):
    def test_director_json_is_a_revalidatable_plan(self) -> None:
        request = director_request()
        plan = coerce_director_plan(director_json(), request)
        self.assertEqual(plan.beats[0].actor_id, "aunt_chen")
        self.assertEqual(plan.state_revision, 1)
        self.assertEqual(plan.local_canon, ())
        prompt = build_director_messages(request)
        self.assertIn("不得创建", prompt[0]["content"])
        self.assertIn("aunt_chen", prompt[1]["content"])

    def test_provider_repairs_invalid_director_json(self) -> None:
        transport = FakeTransport(["not json", director_json()])
        response = DeepSeekProvider(transport=transport).propose_director(
            director_request()
        )
        self.assertEqual(response.plan.beats[0].actor_id, "aunt_chen")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(response.usage["total_tokens"], 100)


class ProviderConstructionTest(unittest.TestCase):
    def test_missing_api_key_fails_at_construction(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LLMProviderError):
                create_provider("deepseek")


if __name__ == "__main__":
    unittest.main()
