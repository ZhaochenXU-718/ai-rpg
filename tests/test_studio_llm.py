"""Creator-facing LLM candidates stay structured and mutation-free."""

from __future__ import annotations

import json
import unittest

from server.engine.llm_deepseek import DeepSeekCallResult, DeepSeekProvider
from server.studio_llm import (
    FieldAssistRequest,
    HeuristicAuthoringAssistant,
    ProviderAuthoringAssistant,
    build_authoring_messages,
    coerce_authoring_result,
)


def story() -> dict:
    return {
        "title": "雾港最后一班船",
        "genre": "coastal_mystery",
        "premise": "末班船离港前，值夜员发现一位乘客没有登船记录。",
        "emotional_contract": "潮湿、克制而紧迫。",
        "ai_plot": {"hidden_truth": "乘客已经改用了另一个名字。"},
        "player_role": {"name": "值夜员"},
        "style_bible": {"tone": "克制"},
        "global_rules": {"boundaries": ["不替玩家作决定。"]},
        "characters": {
            "ferryman": {
                "name": "老船工",
                "motivation": "让船准时离港。",
            },
            "remote_character": {"name": "远处人物"},
        },
        "scenes": {"pier": {"name": "码头"}},
    }


class RecordingTransport:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = []

    def __call__(self, messages, options):
        self.calls.append((messages, options))
        return DeepSeekCallResult(
            content=self.reply,
            finish_reason="stop",
            usage={"total_tokens": 60},
        )


class StudioAuthoringLLMTest(unittest.TestCase):
    def request(self, operation: str = "ideas") -> FieldAssistRequest:
        return FieldAssistRequest(
            operation=operation,
            path="characters.ferryman.motivation",
            label="人物动机",
            current_text="让船准时离港。",
            instruction="增加一点私人代价",
            story=story(),
        )

    def test_prompt_scopes_context_to_the_current_entity(self) -> None:
        messages = build_authoring_messages(self.request())
        payload = json.loads(messages[1]["content"])

        self.assertEqual(payload["当前字段"]["名称"], "人物动机")
        self.assertEqual(
            payload["相关故事上下文"]["当前对象"]["name"], "老船工"
        )
        self.assertNotIn("人物目录", payload["相关故事上下文"])
        self.assertIn("不擅自重写", messages[0]["content"])
        self.assertIn("不生成状态效果", messages[0]["content"])

    def test_structured_candidate_is_coerced_and_limited(self) -> None:
        content = json.dumps({
            "candidates": [
                {"text": "他必须准时离港，否则会失去最后一次领航资格。", "rationale": "加入私人代价"},
                {"text": "第二个润色稿不应保留", "rationale": "多余"},
            ],
            "observations": [],
        }, ensure_ascii=False)

        candidates, observations = coerce_authoring_result(
            content, operation="polish"
        )

        self.assertEqual(len(candidates), 1)
        self.assertIn("领航资格", candidates[0].text)
        self.assertEqual(observations, ())

    def test_check_requires_observations_and_returns_no_replacement(self) -> None:
        content = json.dumps({
            "candidates": [{"text": "不应采用", "rationale": ""}],
            "observations": ["人物动机与场景时限一致。"],
        }, ensure_ascii=False)

        candidates, observations = coerce_authoring_result(
            content, operation="check"
        )

        self.assertEqual(candidates, ())
        self.assertEqual(observations, ("人物动机与场景时限一致。",))

    def test_deepseek_adapter_uses_structured_authoring_policy(self) -> None:
        transport = RecordingTransport(json.dumps({
            "candidates": [{
                "text": "他必须赶在潮位改变前离港，否则会失去领航资格。",
                "rationale": "把公共职责和私人代价连在一起。",
            }],
            "observations": [],
        }, ensure_ascii=False))
        assistant = ProviderAuthoringAssistant(
            DeepSeekProvider(transport=transport)
        )

        result = assistant.assist(self.request("complete"))

        self.assertEqual(result.operation, "complete")
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(transport.calls[0][1]["capability"], "studio_authoring")
        self.assertTrue(transport.calls[0][1]["json_mode"])
        self.assertEqual(result.usage["total_tokens"], 60)

    def test_offline_assistant_returns_candidates_without_mutating_story(self) -> None:
        original = story()
        request = FieldAssistRequest(
            operation="ideas",
            path="premise",
            label="故事前提",
            current_text=original["premise"],
            instruction="",
            story=original,
        )

        result = HeuristicAuthoringAssistant().assist(request)

        self.assertEqual(len(result.candidates), 3)
        self.assertEqual(
            original["premise"],
            "末班船离港前，值夜员发现一位乘客没有登船记录。",
        )


if __name__ == "__main__":
    unittest.main()
