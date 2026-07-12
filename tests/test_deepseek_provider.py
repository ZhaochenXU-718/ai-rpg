"""DeepSeek provider tests with an injected fake transport (no network).

Covers: prompt grounding (perception + world rules + proposal space reach
the model), lenient plan coercion, the JSON repair round, empty-content
retry, the no-steps → clarification fairness fallback, and end-to-end flow
through the action loop.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.content import Story
from server.engine.llm import LLMProviderError, PlanRequest, create_provider
from server.engine.llm_deepseek import DeepSeekProvider, build_messages, coerce_plan
from server.engine.llm_loop import commit_action, run_action_loop
from server.engine.session import GameSession
from server.engine.trace import TraceRecorder

STORY_PATH = Path(__file__).resolve().parent.parent / "content" / "midnight_archive.yaml"


class FakeTransport:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages, options):
        self.calls.append([dict(message) for message in messages])
        if not self.replies:
            raise AssertionError("fake transport exhausted")
        return self.replies.pop(0), {"total_tokens": 100}


def plan_json(**overrides) -> str:
    data = {
        "interpretation": "你想观察全家画像上的细节。",
        "goal": "inspect_portrait",
        "intent_id": "observe",
        "steps": [{
            "capability": "intent",
            "action": "observe",
            "arguments": {"objects": ["family_portrait"]},
            "purpose": "查看画像",
        }],
        "references": ["family_portrait"],
        "proposed_changes": [],
        "risks": [{"description": "管家可能注意到你的兴趣", "likelihood": "possible"}],
        "confidence": 0.9,
        "needs_clarification": False,
        "clarification_question": None,
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


class PromptBuildingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def _request(self, text: str) -> PlanRequest:
        from server.engine.llm_loop import _proposal_space, _world_rules

        return PlanRequest(
            perception=self.session.perception(),
            player_text=text,
            world_rules=_world_rules(self.session),
            proposal_space=_proposal_space(self.session),
        )

    def test_prompt_contains_grounding_and_json_keyword(self) -> None:
        messages = build_messages(self._request("我观察全家画像"))
        system, user = messages[0]["content"], messages[1]["content"]
        # DeepSeek JSON mode requires the word "json" in the prompt.
        self.assertIn("json", system.lower())
        payload = json.loads(user)
        self.assertEqual(payload["玩家方案"], "我观察全家画像")
        flat = json.dumps(payload, ensure_ascii=False)
        self.assertIn("family_portrait", flat)          # perception entity
        self.assertIn("不能凭空制造", flat)              # world boundary
        self.assertIn("butler.suspicion", flat)          # proposal space
        # The perception wall holds inside the prompt too.
        self.assertNotIn("forgery_evidence", flat)
        self.assertNotIn("truth_exposed", flat)

    def test_replan_request_carries_validation_feedback(self) -> None:
        request = self._request("我威胁管家")
        first = coerce_plan(plan_json(), request)
        validation = self.session.validate_plan(first)
        retry = PlanRequest(
            perception=self.session.perception(),
            player_text="我威胁管家",
            attempt=1,
            previous_plan=first,
            previous_validation=validation,
        )
        payload = json.loads(build_messages(retry)[1]["content"])
        self.assertIn("上一轮验证反馈", payload)


class CoercionTest(unittest.TestCase):
    def setUp(self) -> None:
        story = Story.load(STORY_PATH)
        session = GameSession(story, log_dir=None)
        self.request = PlanRequest(perception=session.perception(), player_text="测试")

    def test_fenced_and_noisy_output_is_coerced(self) -> None:
        noisy = "```json\n" + plan_json(extra_field="dropped", confidence="0.9") + "\n```"
        plan = coerce_plan(noisy, self.request)
        self.assertEqual(plan.steps[0].action, "observe")
        self.assertEqual(plan.confidence, 0.9)
        self.assertEqual(plan.perception_revision, 0)
        self.assertEqual(plan.player_text, "测试")

    def test_executable_plan_without_steps_degrades_to_clarification(self) -> None:
        plan = coerce_plan(plan_json(steps=[]), self.request)
        self.assertTrue(plan.needs_clarification)
        self.assertTrue(plan.clarification_question)

    def test_invalid_json_raises_for_repair(self) -> None:
        with self.assertRaises(ValueError):
            coerce_plan("这不是 json", self.request)


class ProviderLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)
        self.recorder = TraceRecorder(None, self.session.session_id)

    def test_full_loop_commits_a_deepseek_plan(self) -> None:
        transport = FakeTransport([plan_json()])
        provider = DeepSeekProvider(transport=transport)
        loop_result = run_action_loop(self.session, provider, "我观察全家画像", self.recorder)
        self.assertTrue(loop_result.can_execute)
        self.assertEqual(loop_result.quote["understanding"], "你想观察全家画像上的细节。")
        outcome = commit_action(self.session, loop_result, self.recorder)
        self.assertIn("observe_family_portrait", outcome.fired_storylets)

    def test_repair_round_recovers_from_bad_json(self) -> None:
        transport = FakeTransport(["oops not json", plan_json()])
        provider = DeepSeekProvider(transport=transport)
        response = provider.propose_plan(PlanRequest(
            perception=self.session.perception(), player_text="我观察全家画像",
        ))
        self.assertEqual(response.plan.steps[0].action, "observe")
        self.assertEqual(len(transport.calls), 2)
        repair_text = transport.calls[1][-1]["content"]
        self.assertIn("无法解析", repair_text)
        self.assertEqual(response.usage["total_tokens"], 200)

    def test_empty_content_retries_then_fails_cleanly(self) -> None:
        transport = FakeTransport(["", ""])
        provider = DeepSeekProvider(transport=transport)
        with self.assertRaises(LLMProviderError):
            provider.propose_plan(PlanRequest(
                perception=self.session.perception(), player_text="测试",
            ))
        self.assertEqual(self.session.turn_no, 0)

    def test_missing_api_key_fails_at_construction(self) -> None:
        import os

        previous = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            with self.assertRaises(LLMProviderError):
                create_provider("deepseek")
        finally:
            if previous is not None:
                os.environ["DEEPSEEK_API_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
