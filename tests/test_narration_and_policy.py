"""Tests for LLM narration, clarification carryover and fail-forward policy.

Invariants:
1. The narration fact sheet is assembled behind the perception wall — prose
   input can only contain committed, player-visible facts.
2. A pending clarification makes the next free-text input a reply with the
   question and prior plan attached; commit clears it.
3. A well-formed but unauthored attempt on the LLM path costs a turn and
   time (fail-forward), while the structured path stays strictly rejected.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.content import Story
from server.engine.llm import LLMProvider, PlanResponse
from server.engine.llm_deepseek import DeepSeekProvider, build_messages
from server.engine.llm_loop import commit_action, run_action_loop
from server.engine.llm_protocol import ActionPlan, CapabilityAction, IssueSeverity
from server.engine.narration import build_turn_facts, narrate_rejection, narrate_turn
from server.engine.renderer import render_turn
from server.engine.resolver import TurnResult
from server.engine.session import GameSession
from server.engine.trace import TraceRecorder

STORY_PATH = Path(__file__).resolve().parent.parent / "content" / "midnight_archive.yaml"


def make_plan(plan_id, steps, **kwargs):
    return ActionPlan(
        plan_id=plan_id,
        perception_revision=kwargs.pop("revision", 0),
        player_text=kwargs.pop("player_text", "测试方案"),
        interpretation=kwargs.pop("interpretation", "测试解释"),
        goal="test_goal",
        steps=tuple(steps),
        **kwargs,
    )


def intent_step(intent_id, objects):
    return CapabilityAction(
        capability="intent",
        action=intent_id,
        arguments={"objects": objects},
        purpose="测试步骤",
    )


class RecordingProvider(LLMProvider):
    """Returns queued plans and records every PlanRequest it receives."""

    name = "recording"

    def __init__(self, plans):
        self.plans = list(plans)
        self.requests = []

    def propose_plan(self, request):
        self.requests.append(request)
        plan = self.plans.pop(0)
        return PlanResponse(plan=plan, raw="{}", model="recording")


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages, options):
        self.calls.append((messages, options))
        return self.replies.pop(0), {"total_tokens": 50}


class NarrationFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def test_fact_sheet_stays_behind_the_perception_wall(self) -> None:
        quote = self.session.quote("negotiate", ["maid"])
        result = self.session.resolve(quote_id=quote["quote_id"])
        facts = build_turn_facts(
            self.story, self.session.state, result,
            player_text="我询问艾拉是否知道什么",
            recent_events=result.prior_events,
        )
        flat = json.dumps(facts, ensure_ascii=False)
        self.assertIn("玩家行动回应提示", facts)
        self.assertTrue(facts["新线索"])
        # Committed narrative may mention discovered things; never these:
        self.assertNotIn("forgery_evidence", flat)
        self.assertNotIn("truth_exposed", flat)
        self.assertNotIn("positions", flat)

    def test_talking_to_heir_gets_heir_response_not_maid_reward(self) -> None:
        quote = self.session.quote("negotiate", ["heir"])
        result = self.session.resolve(quote_id=quote["quote_id"])

        facts = build_turn_facts(
            self.story,
            self.session.state,
            result,
            player_text="我和薇拉谈谈遗嘱",
            recent_events=result.prior_events,
        )

        self.assertNotIn("maid_warning", result.fired)
        self.assertNotIn("maid_note", self.story.inventory(self.session.state))
        self.assertEqual(self.session.state["characters"]["heir"]["trust"], 1)
        self.assertNotIn("玩家行动无专门回应", facts)
        self.assertTrue(any(
            "薇拉小姐" in hint
            for hint in facts["玩家行动回应提示"]
        ))
        beats = facts["世界节拍提示（非玩家行动直接造成）"]
        self.assertTrue(any("午夜封存" in hint for hint in beats))
        self.assertFalse(any("便签" in hint for hint in beats))
        self.assertNotIn("作者叙事提示", facts)

        rendered = render_turn(self.story, result, free_text_mode=True)
        self.assertIn("薇拉小姐", rendered)
        self.assertNotIn("侍女", rendered)
        self.assertLess(rendered.index("薇拉小姐"), rendered.index("与此同时"))

    def test_talking_to_maid_triggers_warning_and_acquires_note(self) -> None:
        quote = self.session.quote("negotiate", ["maid"])
        result = self.session.resolve(quote_id=quote["quote_id"])

        self.assertIn("maid_warning", result.fired)
        self.assertIn("maid_note", self.story.inventory(self.session.state))
        self.assertTrue(any("便签" in hint for hint in result.action_response_hints))
        self.assertFalse(any("便签" in hint for hint in result.world_beat_hints))

    def test_target_location_and_past_events_are_explicit_facts(self) -> None:
        result = self.session.resolve(intent_id="observe", objects=["family_portrait"])
        facts = build_turn_facts(
            self.story,
            self.session.state,
            result,
            player_text="我观察画像",
            recent_events=("上一回合已经发生的事件",),
        )

        self.assertEqual(facts["行动目标"][0]["名称"], "全家画像")
        self.assertIn("当前场景", facts["行动目标"][0]["位置"])
        self.assertEqual(
            facts["过去回合发生（非本回合）"],
            ["上一回合已经发生的事件"],
        )

    def test_unauthored_no_effect_turn_is_flagged_for_honest_prose(self) -> None:
        state = self.session.state
        state["positions"]["player"] = "archive_room"
        state["item_locations"]["servant_key"] = {"type": "carried_by", "id": "player"}
        result = self.session.resolve(
            intent_id="use",
            objects=["servant_key", "locked_cabinet"],
            _allow_quoted=True,
            _allow_unauthored=True,
        )
        facts = build_turn_facts(self.story, state, result)
        self.assertIn("本回合无预设事件", facts)


class NarrationRenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def test_deepseek_renders_prose_in_plain_text_mode(self) -> None:
        transport = FakeTransport(["烛光在你身后晃了一下，画像上的徽记像是回望了你一眼。"])
        provider = DeepSeekProvider(transport=transport)
        result = self.session.resolve(intent_id="observe", objects=["family_portrait"])
        prose = narrate_turn(self.session, provider, result, player_text="我观察画像")
        self.assertIn("徽记", prose)
        messages, options = transport.calls[0]
        self.assertFalse(options["json_mode"])
        self.assertIn("只能陈述事实清单", messages[0]["content"])

    def test_empty_or_failing_render_falls_back_to_template(self) -> None:
        transport = FakeTransport(["", ""])
        provider = DeepSeekProvider(transport=transport)
        result = self.session.resolve(intent_id="observe", objects=["fireplace"])
        with tempfile.TemporaryDirectory() as trace_dir:
            recorder = TraceRecorder(trace_dir, self.session.session_id)
            self.assertIsNone(narrate_turn(self.session, provider, result, recorder))
            records = [
                json.loads(line)
                for line in (Path(trace_dir) / f"{self.session.session_id}.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
        self.assertIn("narration_fallback", {record["event"] for record in records})
        self.assertEqual(len(transport.calls), 2)

    def test_empty_render_retries_once_before_succeeding(self) -> None:
        transport = FakeTransport(["", "第二次生成成功。"])
        provider = DeepSeekProvider(transport=transport)
        result = self.session.resolve(intent_id="observe", objects=["fireplace"])

        self.assertEqual(
            narrate_turn(self.session, provider, result),
            "第二次生成成功。",
        )
        self.assertEqual(len(transport.calls), 2)

    def test_llm_mode_template_fallback_uses_no_structured_command_hint(self) -> None:
        result = TurnResult(
            turn_no=1,
            intent="custom",
            objects=[],
            scene_before="great_hall",
            scene_after="great_hall",
        )

        rendered = render_turn(self.story, result, free_text_mode=True)

        self.assertNotIn("observe family_portrait", rendered)
        self.assertIn("自然语言", rendered)

    def test_rejection_is_voiced_in_world_language(self) -> None:
        transport = FakeTransport(["锁芯纹丝不动，这把钥匙显然不是为它打造的。"])
        provider = DeepSeekProvider(transport=transport)
        prose = narrate_rejection(
            self.session, provider, ["没有匹配的交互"], "我用钥匙撬锁",
        )
        self.assertIn("锁芯", prose)


class ClarificationCarryoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)
        self.recorder = TraceRecorder(None, self.session.session_id)

    def test_reply_carries_question_and_parent_plan(self) -> None:
        clarify = ActionPlan(
            plan_id="plan_clarify",
            perception_revision=0,
            player_text="撬开柜子",
            interpretation="工具不明确",
            goal="clarify",
            steps=(),
            needs_clarification=True,
            clarification_question="你想用什么工具撬？",
        )
        answer_plan = make_plan(
            "plan_answer", [intent_step("observe", ["family_portrait"])],
        )
        provider = RecordingProvider([clarify, answer_plan])

        first = run_action_loop(self.session, provider, "撬开柜子", self.recorder)
        self.assertEqual(first.clarification, "你想用什么工具撬？")
        self.assertIsNotNone(self.session.pending_clarification)

        second = run_action_loop(self.session, provider, "用发簪试试", self.recorder)
        reply_request = provider.requests[1]
        self.assertEqual(reply_request.pending_clarification, "你想用什么工具撬？")
        self.assertIs(reply_request.previous_plan, clarify)
        # A non-clarification round consumes the pending exchange.
        self.assertIsNone(self.session.pending_clarification)
        self.assertTrue(second.can_execute)

    def test_deepseek_prompt_includes_the_exchange(self) -> None:
        clarify = ActionPlan(
            plan_id="plan_clarify",
            perception_revision=0,
            player_text="撬开柜子",
            interpretation="工具不明确",
            goal="clarify",
            steps=(),
            needs_clarification=True,
            clarification_question="你想用什么工具撬？",
        )
        from server.engine.llm import PlanRequest

        request = PlanRequest(
            perception=self.session.perception(),
            player_text="用发簪试试",
            previous_plan=clarify,
            pending_clarification="你想用什么工具撬？",
        )
        payload = json.loads(build_messages(request)[1]["content"])
        self.assertEqual(payload["待澄清问题"], "你想用什么工具撬？")
        self.assertEqual(payload["上一轮计划"]["原话"], "撬开柜子")


class CustomTriggerGroundingTest(unittest.TestCase):
    """Regression: 'chat with the heir' must not walk the player out of
    the hall just because a transition storylet listens on bare custom."""

    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)
        self.recorder = TraceRecorder(None, self.session.session_id)
        self.session.state["flags"]["side_door_found"] = True

    def test_unrelated_custom_action_does_not_trigger_transition(self) -> None:
        from server.engine.llm import ScriptedProvider

        plan = make_plan(
            "plan_chat",
            [intent_step("custom", ["heir"])],
            interpretation="你想和薇拉小姐聊聊遗嘱。",
        )
        loop_result = run_action_loop(
            self.session, ScriptedProvider([plan]), "和薇拉小姐聊遗嘱", self.recorder
        )
        self.assertTrue(loop_result.can_execute)
        self.assertIsNone(loop_result.quote.get("expected_move"))
        outcome = commit_action(self.session, loop_result, self.recorder)
        self.assertNotIn("slip_into_corridor", outcome.fired_storylets)
        self.assertEqual(self.session.state["positions"]["player"], "great_hall")

    def test_route_referencing_custom_action_still_transitions(self) -> None:
        from server.engine.llm import ScriptedProvider

        plan = make_plan(
            "plan_slip",
            [intent_step("custom", ["side_stair"])],
            interpretation="你想借侧梯溜出大厅。",
        )
        loop_result = run_action_loop(
            self.session, ScriptedProvider([plan]), "从侧梯溜出去", self.recorder
        )
        self.assertTrue(loop_result.can_execute)
        # The quote card now discloses the player's own movement.
        self.assertEqual(
            loop_result.quote["expected_move"],
            {"from": "大理石大厅", "to": "仆役走廊"},
        )
        outcome = commit_action(self.session, loop_result, self.recorder)
        self.assertIn("slip_into_corridor", outcome.fired_storylets)
        self.assertEqual(self.session.state["positions"]["player"], "servant_corridor")

    def test_validator_flags_ungrounded_custom_heavy_trigger(self) -> None:
        import copy

        import yaml

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from validate_content import validate_content

        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(data)
        for storylet in broken["storylets"]:
            if storylet["id"] == "slip_into_corridor":
                storylet["trigger"].pop("object_any")
        report = validate_content(broken)
        self.assertTrue(any(
            "slip_into_corridor" in warning and "custom" in warning
            for warning in report.warnings
        ))

    def test_validator_requires_automatic_heavy_events_to_declare_world_beat(self) -> None:
        import copy

        import yaml

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        from validate_content import validate_content

        data = yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))
        broken = copy.deepcopy(data)
        opening = next(
            storylet for storylet in broken["storylets"]
            if storylet["id"] == "opening_pressure"
        )
        opening.pop("attribution")

        report = validate_content(broken)

        self.assertTrue(any(
            "opening_pressure" in warning and "world_beat" in warning
            for warning in report.warnings
        ))


class FailForwardPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)
        self.recorder = TraceRecorder(None, self.session.session_id)
        state = self.session.state
        state["positions"]["player"] = "archive_room"
        state["item_locations"]["servant_key"] = {"type": "carried_by", "id": "player"}

    def test_unauthored_attempt_costs_a_turn_instead_of_free_rejection(self) -> None:
        plan = make_plan(
            "plan_pick_lock",
            [intent_step("use", ["servant_key", "locked_cabinet"])],
            interpretation="你想用侧门钥匙撬开文件柜的锁。",
        )
        from server.engine.llm import ScriptedProvider

        loop_result = run_action_loop(
            self.session, ScriptedProvider([plan]), "用钥匙撬锁", self.recorder
        )
        self.assertTrue(loop_result.can_execute)
        self.assertIn(
            "action.unauthored_attempt",
            {issue.code for issue in loop_result.validation.issues},
        )
        self.assertTrue(any("没有把握" in risk for risk in loop_result.quote["risks"]))

        outcome = commit_action(self.session, loop_result, self.recorder)
        self.assertEqual(outcome.fired_storylets, ())
        self.assertEqual(outcome.result_tier, "fail_forward")
        self.assertEqual(self.session.turn_no, 1)
        self.assertEqual(self.session.state["world"]["time_left"], 7)
        # The canon lock stays shut: no evidence, no compartment.
        self.assertEqual(
            self.session.state["item_locations"]["forgery_evidence"],
            {"type": "container", "id": "hidden_compartment"},
        )

    def test_structured_path_stays_strict(self) -> None:
        from server.engine.session import SessionError

        with self.assertRaisesRegex(SessionError, "没有与「使用」"):
            self.session.quote("use", ["servant_key", "locked_cabinet"])
        self.assertEqual(self.session.turn_no, 0)
        self.assertEqual(self.session.state["world"]["time_left"], 8)

    def test_custom_miss_discloses_uncertainty(self) -> None:
        self.session.state["positions"]["player"] = "great_hall"
        plan = make_plan(
            "plan_custom_chat",
            [intent_step("custom", ["heir"])],
            interpretation="你想用一种没有预写的方式试探薇拉。",
        )
        from server.engine.llm import ScriptedProvider

        loop_result = run_action_loop(
            self.session, ScriptedProvider([plan]), "我试探薇拉", self.recorder
        )

        self.assertTrue(loop_result.can_execute)
        self.assertIn(
            "action.unauthored_attempt",
            {issue.code for issue in loop_result.validation.issues},
        )
        self.assertTrue(any("没有把握" in risk for risk in loop_result.quote["risks"]))


if __name__ == "__main__":
    unittest.main()
