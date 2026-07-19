"""DeepSeek's native narrative, card, and physical-fact protocols."""

from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.engine.llm import (
    FactExtractionRequest,
    LLMProviderError,
    MemoryCompactionRequest,
    NarrativeRequest,
    SuggestionRequest,
    create_provider,
)
from server.engine.llm_deepseek import (
    FACT_EXTRACTION_CALL_POLICY,
    MEMORY_COMPACTION_CALL_POLICY,
    NARRATIVE_CALL_POLICY,
    SUGGESTION_CALL_POLICY,
    DeepSeekCallResult,
    DeepSeekProvider,
    build_fact_extraction_messages,
    build_memory_compaction_messages,
    build_suggestion_messages,
    coerce_fact_extraction,
    coerce_memory_digest,
    coerce_suggestions,
)
from server.engine.llm_protocol import (
    PerceptionAudience,
    PerceptionSnapshot,
)
from server.engine.memory import MemoryDigest, MemoryEvent


class FakeTransport:
    def __init__(self, replies: list[str | DeepSeekCallResult | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], dict]] = []

    def __call__(self, messages, options):
        self.calls.append(([dict(message) for message in messages], dict(options)))
        if not self.replies:
            raise AssertionError("fake transport exhausted")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, DeepSeekCallResult):
            return reply
        return DeepSeekCallResult(
            content=reply,
            finish_reason="stop",
            usage={"total_tokens": 50},
        )


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


def suggestions_json() -> str:
    return json.dumps({
        "suggestions": [{
            "title": "先说明用途",
            "action_text": "我先向周师傅说明防雨布要盖住院子里的长桌。",
            "focus": "social",
            "rationale": "让对方听清计划，但不替她作决定。",
        }]
    }, ensure_ascii=False)


class NarrativeProviderTest(unittest.TestCase):
    def request(self) -> NarrativeRequest:
        return NarrativeRequest(
            kind="turn",
            perception=perception(),
            facts={"玩家输入": "我先问问周师傅。", "在场可见实体": ["周师傅"]},
            style={"tone": "克制"},
        )

    def test_narration_uses_plain_text_mode_and_scoped_prompt(self) -> None:
        transport = FakeTransport(["周师傅放下扳手，等你把用途说完。"])
        response = DeepSeekProvider(transport=transport).render_narrative(self.request())
        self.assertIn("放下扳手", response.text)
        self.assertFalse(transport.calls[0][1]["json_mode"])
        self.assertEqual(transport.calls[0][1]["thinking"], "disabled")
        self.assertEqual(transport.calls[0][1]["max_tokens"], 400)
        self.assertEqual(response.prompt_version, "deepseek-narrate-v9")
        self.assertEqual(response.diagnostics["final_content_state"], "valid")
        self.assertEqual(response.diagnostics["attempts"][0]["finish_reason"], "stop")
        self.assertIn("可以补充不改变连续性", transport.calls[0][0][0]["content"])
        self.assertIn("subject_id", transport.calls[0][0][1]["content"])

    def test_empty_narration_retries_once_then_succeeds(self) -> None:
        transport = FakeTransport(["", "第二次生成成功。"])
        response = DeepSeekProvider(transport=transport).render_narrative(self.request())
        self.assertEqual(response.text, "第二次生成成功。")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(response.usage["total_tokens"], 100)
        self.assertEqual(response.diagnostics["retry_reasons"], ["empty_content"])
        self.assertEqual(response.diagnostics["failed_usage"]["total_tokens"], 50)

    def test_two_empty_replies_raise_diagnostic_error(self) -> None:
        transport = FakeTransport(["", ""])
        with self.assertRaises(LLMProviderError) as caught:
            DeepSeekProvider(transport=transport).render_narrative(self.request())
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics["final_content_state"], "empty_content")
        self.assertEqual(diagnostics["failed_usage"]["total_tokens"], 100)

    def test_length_exhaustion_is_not_reported_as_empty_content(self) -> None:
        transport = FakeTransport([
            DeepSeekCallResult(
                content="没有写完的叙事",
                finish_reason="length",
                usage={"total_tokens": 50},
            ),
            "第二次生成完整。",
        ])
        response = DeepSeekProvider(transport=transport).render_narrative(self.request())
        self.assertEqual(response.text, "第二次生成完整。")
        self.assertEqual(
            response.diagnostics["retry_reasons"], ["length_exhausted"]
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
        self.assertEqual(
            payload["action_text"],
            "我先向周师傅说明防雨布要盖住院子里的长桌。",
        )
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
        self.assertEqual(response.prompt_version, "deepseek-suggestions-v4")
        self.assertEqual(response.usage["total_tokens"], 100)
        self.assertTrue(transport.calls[0][1]["json_mode"])
        self.assertTrue(transport.calls[1][1]["json_mode"])
        self.assertEqual(response.diagnostics["retry_reasons"], ["parse_error"])

    def test_json_mode_empty_response_falls_back_to_plain_text_json(self) -> None:
        transport = FakeTransport([
            DeepSeekCallResult(
                content="",
                reasoning_content="先分析可见局面",
                finish_reason="stop",
                usage={
                    "total_tokens": 60,
                    "completion_tokens_details": {"reasoning_tokens": 7},
                },
            ),
            suggestions_json(),
        ])
        response = DeepSeekProvider(transport=transport).propose_suggestions(
            self.request()
        )
        self.assertTrue(transport.calls[0][1]["json_mode"])
        self.assertFalse(transport.calls[1][1]["json_mode"])
        first = response.diagnostics["attempts"][0]
        self.assertEqual(first["retry_reason"], "json_mode_empty")
        self.assertEqual(first["reasoning_state"], "present")
        self.assertEqual(first["reasoning_tokens"], 7)
        self.assertNotIn(
            "先分析可见局面",
            json.dumps(response.diagnostics, ensure_ascii=False),
        )
        self.assertEqual(
            response.diagnostics["failed_usage"]["completion_tokens_details"][
                "reasoning_tokens"
            ],
            7,
        )

    def test_suggestion_policy_is_explicit_non_thinking_json(self) -> None:
        transport = FakeTransport([suggestions_json()])
        DeepSeekProvider(transport=transport).propose_suggestions(self.request())
        options = transport.calls[0][1]
        self.assertEqual(options["thinking"], "disabled")
        self.assertEqual(options["max_tokens"], 2048)
        self.assertTrue(options["json_mode"])


class FactExtractionProviderTest(unittest.TestCase):
    def request(self) -> FactExtractionRequest:
        return FactExtractionRequest(
            perception=perception(),
            player_text="我走进公共院子。",
            narrative="你跨过门槛走进公共院子。",
            ledger={
                "available_destinations": [
                    {"id": "courtyard", "label": "去公共院子"}
                ],
                "characters": [
                    {"id": "player", "location_id": "workshop"}
                ],
            },
        )

    def extraction_json(self) -> str:
        return json.dumps({
            "facts": [{
                "kind": "character_move",
                "actor_id": "player",
                "destination_id": "courtyard",
                "evidence": "你跨过门槛走进公共院子",
            }]
        }, ensure_ascii=False)

    def test_extractor_uses_ledger_ids_and_verbatim_evidence(self) -> None:
        extraction = coerce_fact_extraction(
            self.extraction_json(), self.request()
        )
        self.assertEqual(extraction.facts[0].destination_id, "courtyard")
        prompt = build_fact_extraction_messages(self.request())
        flat = json.dumps(prompt, ensure_ascii=False)
        self.assertIn("evidence", flat)
        self.assertIn("courtyard", flat)
        self.assertNotIn("state_changes", flat)

    def test_provider_repairs_invalid_fact_json(self) -> None:
        transport = FakeTransport(["not json", self.extraction_json()])
        response = DeepSeekProvider(transport=transport).extract_facts(
            self.request()
        )
        self.assertEqual(len(response.extraction.facts), 1)
        self.assertEqual(response.prompt_version, "deepseek-fact-extraction-v2")
        self.assertEqual(response.usage["total_tokens"], 100)
        self.assertEqual(transport.calls[0][1]["thinking"], "enabled")
        self.assertEqual(transport.calls[0][1]["reasoning_effort"], "high")
        self.assertEqual(transport.calls[0][1]["max_tokens"], 1200)

    def test_malformed_fact_cannot_be_silently_dropped_from_a_mixed_batch(self) -> None:
        mixed = json.loads(self.extraction_json())
        mixed["facts"].append({
            "kind": "item_transfer",
            "item_id": "rain_canvas",
            "evidence": "不存在完整 placement 的坏事实",
        })

        with self.assertRaisesRegex(ValueError, "indexes: 1"):
            coerce_fact_extraction(
                json.dumps(mixed, ensure_ascii=False),
                self.request(),
            )


class MemoryCompactionProviderTest(unittest.TestCase):
    def request(self) -> MemoryCompactionRequest:
        return MemoryCompactionRequest(
            story_id="open_neighbor_scene",
            state_revision=7,
            previous_digest=MemoryDigest(
                compacted_through_turn=2,
                rolling_summary="此前玩家在修理铺说明遮雨需求。",
                open_loops=("还没决定怎样使用防雨布",),
            ),
            events=(
                MemoryEvent(
                    turn_no=3,
                    commit_id="commit_3",
                    player_text="我看看防雨布。",
                    narrative="你看过架子上的旧防雨布，它已经洗净叠好。",
                    scene_before="workshop",
                    scene_after="workshop",
                ),
            ),
            character_catalog=(("keeper_zhou", "周师傅"),),
            scene_catalog=(("workshop", "修理铺"),),
        )

    def response_json(self) -> str:
        return json.dumps({
            "rolling_summary": "玩家在修理铺说明遮雨需求，并看过洗净的旧防雨布。",
            "open_loops": ["还没决定怎样使用防雨布"],
            "character_notes": {
                "keeper_zhou": ["在等玩家说明具体用途"],
                "unknown_person": ["不应进入小结"],
            },
            "scene_notes": {
                "workshop": ["防雨布已经洗净叠好"],
                "unknown_scene": ["不应进入小结"],
            },
            "recently_resolved": ["已经看过防雨布"],
            "compacted_through_turn": 999,
        }, ensure_ascii=False)

    def test_prompt_contains_only_previous_digest_events_and_catalogs(self) -> None:
        prompt = build_memory_compaction_messages(self.request())
        flat = json.dumps(prompt, ensure_ascii=False)
        self.assertIn("此前玩家在修理铺说明遮雨需求", flat)
        self.assertIn("你看过架子上的旧防雨布", flat)
        self.assertIn("keeper_zhou", flat)
        self.assertIn("workshop", flat)
        self.assertNotIn("状态 patch", prompt[1]["content"])

    def test_coercion_owns_boundary_and_filters_unknown_subject_ids(self) -> None:
        digest = coerce_memory_digest(self.response_json(), self.request())
        self.assertEqual(digest.compacted_through_turn, 3)
        self.assertEqual(
            [group.subject_id for group in digest.character_notes],
            ["keeper_zhou"],
        )
        self.assertEqual(
            [group.subject_id for group in digest.scene_notes],
            ["workshop"],
        )

    def test_provider_uses_low_cost_policy_and_no_repair_retry(self) -> None:
        transport = FakeTransport([self.response_json()])
        response = DeepSeekProvider(transport=transport).compact_memory(
            self.request()
        )
        options = transport.calls[0][1]
        self.assertEqual(options["thinking"], "disabled")
        self.assertEqual(options["temperature"], 0.1)
        self.assertEqual(options["max_tokens"], 800)
        self.assertTrue(options["json_mode"])
        self.assertEqual(
            response.prompt_version,
            "deepseek-memory-compaction-v2",
        )

        bad_transport = FakeTransport(["not json", self.response_json()])
        with self.assertRaises(LLMProviderError):
            DeepSeekProvider(transport=bad_transport).compact_memory(
                self.request()
            )
        self.assertEqual(len(bad_transport.calls), 1)


class CallPolicyTest(unittest.TestCase):
    def test_every_current_capability_has_an_explicit_policy(self) -> None:
        policies = (
            NARRATIVE_CALL_POLICY,
            SUGGESTION_CALL_POLICY,
            FACT_EXTRACTION_CALL_POLICY,
            MEMORY_COMPACTION_CALL_POLICY,
        )
        self.assertEqual(
            {policy.capability for policy in policies},
            {
                "narration",
                "suggestions",
                "fact_extraction",
                "memory_compaction",
            },
        )
        self.assertTrue(all(policy.thinking in {"enabled", "disabled"} for policy in policies))
        self.assertTrue(all(policy.max_tokens > 0 for policy in policies))

    def test_sdk_call_sends_thinking_and_reads_response_diagnostics(self) -> None:
        class FakeUsage:
            def model_dump(self, **kwargs):
                return {
                    "total_tokens": 12,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                }

        class FakeCompletions:
            def __init__(self) -> None:
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content='{"facts": []}',
                            reasoning_content="checked ledger",
                        ),
                    )],
                    usage=FakeUsage(),
                )

        completions = FakeCompletions()
        provider = DeepSeekProvider(api_key="test-key")
        provider._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )
        result = provider._call(
            [{"role": "user", "content": "json"}],
            policy=FACT_EXTRACTION_CALL_POLICY,
        )
        request = completions.calls[0]
        self.assertEqual(
            request["extra_body"], {"thinking": {"type": "enabled"}}
        )
        self.assertEqual(request["reasoning_effort"], "high")
        self.assertNotIn("temperature", request)
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.reasoning_content, "checked ledger")
        self.assertEqual(
            result.usage["completion_tokens_details"]["reasoning_tokens"], 5
        )


class ProviderConstructionTest(unittest.TestCase):
    def test_missing_api_key_fails_at_construction(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LLMProviderError):
                create_provider("deepseek")


if __name__ == "__main__":
    unittest.main()
