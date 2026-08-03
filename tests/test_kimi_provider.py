"""Kimi provider: shared protocol reuse plus Moonshot-specific call mapping."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.engine.llm import (
    LLMProviderError,
    NarrativeRequest,
    create_provider,
)
from server.engine.llm_deepseek import (
    FACT_EXTRACTION_CALL_POLICY,
    MEMORY_COMPACTION_CALL_POLICY,
    NARRATIVE_CALL_POLICY,
)
from server.engine.llm_kimi import KimiProvider
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
        return (self.replies.pop(0), {"total_tokens": 50})


def perception() -> PerceptionSnapshot:
    return PerceptionSnapshot(
        audience=PerceptionAudience.PLAYER,
        subject_id="player",
        story_id="open_neighbor_scene",
        session_id="session_demo",
        turn_no=0,
        state_revision=0,
        location_id="workshop",
        location_name="修理铺",
        current_goal="说明防雨布的用途",
    )


def narrative_request() -> NarrativeRequest:
    return NarrativeRequest(
        kind="turn",
        perception=perception(),
        facts={"玩家输入": "我先问问周师傅。", "在场可见实体": ["周师傅"]},
        style={"tone": "克制"},
    )


class KimiNarrativeTest(unittest.TestCase):
    def test_narration_reuses_shared_prompt_with_kimi_versioning(self) -> None:
        transport = FakeTransport(["周师傅放下扳手，等你把用途说完。"])
        response = KimiProvider(transport=transport).render_narrative(
            narrative_request()
        )
        self.assertIn("放下扳手", response.text)
        self.assertEqual(response.prompt_version, "kimi-narrate-v17")
        self.assertEqual(response.model, "kimi-k3")
        self.assertIn("可以补充不改变连续性", transport.calls[0][0][0]["content"])
        self.assertIn("简短不是目标，有效才是目标", transport.calls[0][0][0]["content"])

    def test_provider_failure_is_reported_as_kimi(self) -> None:
        transport = FakeTransport(["", ""])
        with self.assertRaisesRegex(LLMProviderError, "Kimi 旁白连续无法生成"):
            KimiProvider(transport=transport).render_narrative(narrative_request())


class KimiCompletionKwargsTest(unittest.TestCase):
    def kwargs(self, model: str, policy, json_mode: bool = False) -> dict:
        provider = KimiProvider(api_key="test-key", model=model)
        return provider._completion_kwargs(
            [{"role": "user", "content": "hi"}],
            policy=policy,
            json_mode=json_mode,
        )

    def test_k2_models_use_thinking_switch_and_fixed_temperature(self) -> None:
        kwargs = self.kwargs("kimi-k2.6", NARRATIVE_CALL_POLICY)
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(kwargs["max_completion_tokens"], 900)
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("max_tokens", kwargs)

    def test_k3_maps_thinking_policies_onto_reasoning_effort(self) -> None:
        thinking = self.kwargs("kimi-k3", FACT_EXTRACTION_CALL_POLICY, json_mode=True)
        self.assertEqual(thinking["reasoning_effort"], "high")
        self.assertNotIn("extra_body", thinking)
        self.assertEqual(thinking["response_format"], {"type": "json_object"})

        non_thinking = self.kwargs("kimi-k3", NARRATIVE_CALL_POLICY)
        self.assertEqual(non_thinking["reasoning_effort"], "low")
        self.assertNotIn("temperature", non_thinking)

    def test_moonshot_v1_accepts_temperature_without_thinking(self) -> None:
        kwargs = self.kwargs("moonshot-v1-32k", MEMORY_COMPACTION_CALL_POLICY, True)
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertNotIn("extra_body", kwargs)
        self.assertNotIn("reasoning_effort", kwargs)

    def test_sdk_call_path_uses_kimi_kwargs(self) -> None:
        class FakeCompletions:
            def __init__(self) -> None:
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="好的。"),
                    )],
                    usage=None,
                )

        completions = FakeCompletions()
        provider = KimiProvider(api_key="test-key")
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        provider._call(
            [{"role": "user", "content": "hi"}],
            policy=NARRATIVE_CALL_POLICY,
        )
        request = completions.calls[0]
        self.assertEqual(request["model"], "kimi-k3")
        self.assertEqual(request["reasoning_effort"], "low")
        self.assertNotIn("extra_body", request)
        self.assertEqual(request["max_completion_tokens"], 900)


class KimiConstructionTest(unittest.TestCase):
    def test_missing_api_key_fails_at_construction(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(LLMProviderError, "KIMI_API_KEY"):
                create_provider("kimi")

    def test_env_configuration_covers_key_model_and_base_url(self) -> None:
        env = {
            "MOONSHOT_API_KEY": "moonshot-key",
            "KIMI_MODEL": "kimi-k2.6",
            "KIMI_BASE_URL": "https://api.example.com/v1",
        }
        with patch.dict(os.environ, env, clear=True):
            provider = create_provider("kimi")
        self.assertIsInstance(provider, KimiProvider)
        self.assertEqual(provider.model, "kimi-k2.6")
        self.assertEqual(provider.base_url, "https://api.example.com/v1")
        self.assertEqual(provider._api_key, "moonshot-key")

    def test_kimi_key_never_falls_back_to_deepseek_credentials(self) -> None:
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "ds-key"}, clear=True):
            with self.assertRaises(LLMProviderError):
                create_provider("kimi")


if __name__ == "__main__":
    unittest.main()
