"""Vertical action loop tests: understand → validate → replan → quote → commit.

Invariants: rejected or clarification plans never spend time or turns; the
committed payload equals the quoted one; every phase lands in the trace so
a session can be replayed offline.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.content import Story
from server.engine.llm import (
    HeuristicMockProvider,
    ReplayProvider,
    ScriptedProvider,
)
from server.engine.llm_loop import commit_action, run_action_loop
from server.engine.llm_protocol import (
    ActionPlan,
    AuthorityLevel,
    CapabilityAction,
    ChangeOperation,
    StateChangeProposal,
)
from server.engine.session import GameSession
from server.engine.trace import TraceRecorder

STORY_PATH = Path(__file__).resolve().parent.parent / "content" / "midnight_archive.yaml"


def intent_step(intent_id: str, objects: list[str]) -> CapabilityAction:
    return CapabilityAction(
        capability="intent",
        action=intent_id,
        arguments={"objects": objects},
        purpose="测试步骤",
    )


def make_plan(plan_id: str, steps, *, revision=0, changes=(), **kwargs) -> ActionPlan:
    return ActionPlan(
        plan_id=plan_id,
        perception_revision=revision,
        player_text=kwargs.pop("player_text", "测试方案"),
        interpretation=kwargs.pop("interpretation", "测试解释"),
        goal="test_goal",
        steps=tuple(steps),
        proposed_changes=tuple(changes),
        **kwargs,
    )


class ActionLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)
        self.trace_dir = tempfile.mkdtemp()
        self.recorder = TraceRecorder(self.trace_dir, self.session.session_id)

    def _trace_events(self) -> list[dict]:
        path = Path(self.trace_dir) / f"{self.session.session_id}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_valid_plan_quotes_and_commits_with_full_trace(self) -> None:
        plan = make_plan(
            "plan_negotiate",
            [intent_step("negotiate", ["heir"])],
            changes=[StateChangeProposal(
                path="heir.trust",
                operation=ChangeOperation.INCREMENT,
                value=1,
                authority=AuthorityLevel.SOFT_STATE,
                reason="表明立场",
            )],
            interpretation="你想赢得薇拉的初步信任。",
        )
        loop_result = run_action_loop(
            self.session, ScriptedProvider([plan]), "我坦诚地和薇拉聊聊", self.recorder
        )
        self.assertTrue(loop_result.can_execute)
        self.assertEqual(loop_result.quote["understanding"], "你想赢得薇拉的初步信任。")
        # The quoted proposal is exactly what commit will apply.
        self.assertEqual(loop_result.quote["proposal"], {"heir.trust": 1})

        outcome = commit_action(self.session, loop_result, self.recorder)
        self.assertEqual(self.session.state["characters"]["heir"]["trust"], 1)
        self.assertEqual(outcome.plan_id, "plan_negotiate")

        events = [record["event"] for record in self._trace_events()]
        for expected in ("perception", "llm_response", "validation", "quote_confirmed", "committed_outcome"):
            self.assertIn(expected, events)

    def test_rejected_plan_replans_once_without_spending_time(self) -> None:
        bad = make_plan("plan_bad", [intent_step("threaten", ["butler"])])
        good = make_plan(
            "plan_good",
            [intent_step("observe", ["family_portrait"])],
            revision=0,
        )
        loop_result = run_action_loop(
            self.session, ScriptedProvider([bad, good]), "我瞪着管家", self.recorder
        )
        self.assertTrue(loop_result.replanned)
        self.assertTrue(loop_result.can_execute)
        self.assertEqual(loop_result.plan.plan_id, "plan_good")
        self.assertEqual(self.session.turn_no, 0)
        self.assertEqual(self.session.state["world"]["time_left"], 8)

    def test_two_rejections_stop_the_loop_before_any_cost(self) -> None:
        plans = [
            make_plan("plan_bad1", [intent_step("threaten", ["butler"])]),
            make_plan("plan_bad2", [intent_step("threaten", ["maid"])]),
        ]
        loop_result = run_action_loop(
            self.session, ScriptedProvider(plans), "我威胁所有人", self.recorder
        )
        self.assertFalse(loop_result.can_execute)
        self.assertTrue(loop_result.replanned)
        self.assertTrue(loop_result.rejection_messages())
        self.assertEqual(self.session.turn_no, 0)
        self.assertEqual(self.session.state["world"]["time_left"], 8)

    def test_clarification_plan_reaches_player_without_cost(self) -> None:
        plan = ActionPlan(
            plan_id="plan_clarify",
            perception_revision=0,
            player_text="想想办法",
            interpretation="目标不明确",
            goal="clarify",
            steps=(),
            needs_clarification=True,
            clarification_question="你想先获取谁的信任？",
        )
        loop_result = run_action_loop(
            self.session, ScriptedProvider([plan]), "想想办法", self.recorder
        )
        self.assertEqual(loop_result.clarification, "你想先获取谁的信任？")
        self.assertFalse(loop_result.can_execute)
        self.assertEqual(self.session.turn_no, 0)

    def test_heuristic_mock_grounds_plan_in_perception_labels(self) -> None:
        loop_result = run_action_loop(
            self.session,
            HeuristicMockProvider(),
            "我借着雨声观察一下全家画像",
            self.recorder,
        )
        self.assertTrue(loop_result.can_execute)
        payload = self.session.plan_payload(loop_result.validation)
        self.assertEqual(payload["intent_id"], "observe")
        self.assertIn("family_portrait", payload["objects"])
        self.assertIn("rain_noise", payload["objects"])
        outcome = commit_action(self.session, loop_result, self.recorder)
        self.assertIn("observe_family_portrait", outcome.fired_storylets)

    def test_replay_provider_reproduces_the_recorded_plan(self) -> None:
        original = run_action_loop(
            self.session, HeuristicMockProvider(), "我观察全家画像", self.recorder
        )
        commit_action(self.session, original, self.recorder)

        replay_session = GameSession(self.story, log_dir=None)
        replay_recorder = TraceRecorder(self.trace_dir, replay_session.session_id)
        provider = ReplayProvider(
            Path(self.trace_dir) / f"{self.session.session_id}.jsonl"
        )
        replayed = run_action_loop(
            replay_session, provider, "我观察全家画像", replay_recorder
        )
        self.assertTrue(replayed.can_execute)
        self.assertEqual(replayed.plan.player_text, original.plan.player_text)
        outcome = commit_action(replay_session, replayed, replay_recorder)
        self.assertEqual(
            outcome.fired_storylets,
            ("opening_pressure", "observe_family_portrait", "maid_warning"),
        )


if __name__ == "__main__":
    unittest.main()
