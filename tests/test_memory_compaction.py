from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.fact_pipeline import resolve_player_turn
from server.engine.llm import LLMProviderError, ScriptedProvider
from server.engine.memory import (
    COMPACTION_CHAR_BUDGET,
    MemoryDigest,
    MemoryEvent,
    MemoryNoteGroup,
    MemoryState,
    plan_memory_compaction,
)
from server.engine.renderer import render_memory
from server.engine.session import GameSession
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "rooftop_supper.yaml"


def memory_event(turn_no: int, *, narrative: str | None = None) -> MemoryEvent:
    return MemoryEvent(
        turn_no=turn_no,
        commit_id=f"commit_{turn_no}",
        player_text=f"行动 {turn_no}",
        narrative=narrative or f"已提交叙事 {turn_no}",
        scene_before="building_lobby",
        scene_after="building_lobby",
    )


def digest(through_turn: int, label: str = "前四回合的小结") -> MemoryDigest:
    return MemoryDigest(
        compacted_through_turn=through_turn,
        rolling_summary=label,
        open_loops=("晚饭地点还没有最后确定",),
        character_notes=(
            MemoryNoteGroup("aunt_chen", ("愿意继续商量晚饭安排",)),
        ),
        scene_notes=(
            MemoryNoteGroup("building_lobby", ("大家暂时仍在门厅",)),
        ),
        recently_resolved=("已经看过饭篮",),
    )


class CountingFailingProvider(ScriptedProvider):
    def __init__(self, turns: int) -> None:
        super().__init__(
            narratives=[f"叙事 {index}" for index in range(1, turns + 1)],
            fact_extractions=[() for _ in range(turns)],
        )
        self.compaction_calls = 0

    def compact_memory(self, request):
        self.compaction_calls += 1
        raise LLMProviderError(
            "小结服务暂时不可用",
            diagnostics={"final_content_state": "transport_error"},
        )


class CapturingCompactionProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__(
            narratives=[f"叙事 {index}" for index in range(1, 9)],
            fact_extractions=[() for _ in range(8)],
        )
        self.compaction_request = None

    def compact_memory(self, request):
        self.compaction_request = request
        self._memory_digests.append(digest(request.events[-1].turn_no))
        return super().compact_memory(request)


class MemoryCompactionPlanningTest(unittest.TestCase):
    def test_event_window_keeps_four_recent_turns_uncompacted(self) -> None:
        memory = MemoryState()
        for turn_no in range(1, 8):
            memory = memory.append(memory_event(turn_no))
        self.assertIsNone(plan_memory_compaction(memory))

        memory = memory.append(memory_event(8))
        plan = plan_memory_compaction(memory)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.trigger, "event_window")
        self.assertEqual([event.turn_no for event in plan.events], [1, 2, 3, 4])
        self.assertEqual(plan.through_turn, 4)

    def test_character_budget_can_trigger_before_four_old_events(self) -> None:
        memory = MemoryState().append(memory_event(
            1,
            narrative="长叙事" * COMPACTION_CHAR_BUDGET,
        ))
        for turn_no in range(2, 6):
            memory = memory.append(memory_event(turn_no))

        plan = plan_memory_compaction(memory)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.trigger, "character_budget")
        self.assertEqual([event.turn_no for event in plan.events], [1])


class MemoryCompactionPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)

    def play_turns(
        self,
        session: GameSession,
        provider: ScriptedProvider,
        recorder: TraceRecorder,
        count: int,
    ) -> None:
        for index in range(1, count + 1):
            resolve_player_turn(
                session,
                provider,
                recorder,
                f"行动 {index}",
            )

    def test_due_batch_updates_soft_digest_and_current_checkpoint(self) -> None:
        session = GameSession(self.story, log_dir=None)
        provider = ScriptedProvider(
            narratives=[f"叙事 {index}" for index in range(1, 9)],
            fact_extractions=[() for _ in range(8)],
            memory_digests=[digest(4)],
        )
        self.play_turns(session, provider, TraceRecorder(None, session.session_id), 8)

        self.assertEqual(session.turn_no, 8)
        self.assertEqual(session.state_revision, 8)
        self.assertEqual(len(session.memory.events), 8)
        self.assertEqual(session.memory.compacted_through_turn, 4)
        self.assertEqual(session.memory.rolling_summary, "前四回合的小结")
        self.assertEqual(session.memory.last_compaction_attempt_turn, 8)
        checkpoint_memory = session.get_checkpoint(
            session.current_checkpoint_id
        ).memory
        self.assertEqual(checkpoint_memory, session.memory)

    def test_compactor_catalog_contains_only_player_encountered_entities(self) -> None:
        session = GameSession(self.story, log_dir=None)
        provider = CapturingCompactionProvider()
        self.play_turns(session, provider, TraceRecorder(None, session.session_id), 8)

        character_ids = {
            item[0] for item in provider.compaction_request.character_catalog
        }
        scene_ids = {
            item[0] for item in provider.compaction_request.scene_catalog
        }
        self.assertEqual(character_ids, {"player", "aunt_chen"})
        self.assertEqual(scene_ids, {"building_lobby"})
        self.assertNotIn("xiaoyu", character_ids)
        self.assertNotIn("rooftop", scene_ids)

    def test_model_cannot_move_the_engine_selected_boundary(self) -> None:
        session = GameSession(self.story, log_dir=None)
        provider = ScriptedProvider(
            narratives=[f"叙事 {index}" for index in range(1, 9)],
            fact_extractions=[() for _ in range(8)],
            memory_digests=[digest(5, "越界小结")],
        )
        self.play_turns(session, provider, TraceRecorder(None, session.session_id), 8)

        self.assertEqual(session.turn_no, 8)
        self.assertEqual(len(session.memory.events), 8)
        self.assertEqual(session.memory.compacted_through_turn, 0)
        self.assertEqual(session.memory.rolling_summary, "")
        self.assertIn("unexpected compaction boundary", session.memory.last_compaction_error)

    def test_success_is_written_to_session_log_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = GameSession(
                self.story,
                log_dir=str(Path(temp_dir) / "sessions"),
            )
            recorder = TraceRecorder(
                Path(temp_dir) / "traces",
                session.session_id,
            )
            provider = ScriptedProvider(
                narratives=[f"叙事 {index}" for index in range(1, 9)],
                fact_extractions=[() for _ in range(8)],
                memory_digests=[digest(4)],
            )
            self.play_turns(session, provider, recorder, 8)
            session_records = [
                json.loads(line)
                for line in session._logger.path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            trace_records = [
                json.loads(line)
                for line in recorder.path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        session_event = next(
            record for record in session_records
            if record["event"] == "memory_compacted"
        )
        trace_event = next(
            record for record in trace_records
            if record["event"] == "memory_compaction"
        )
        self.assertEqual(session_event["digest"]["compacted_through_turn"], 4)
        self.assertEqual(trace_event["through_turn"], 4)
        self.assertEqual(trace_event["model"], "scripted")

    def test_failure_never_rolls_back_turn_and_uses_three_turn_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = GameSession(self.story, log_dir=None)
            recorder = TraceRecorder(Path(temp_dir), session.session_id)
            provider = CountingFailingProvider(11)
            self.play_turns(session, provider, recorder, 8)

            self.assertEqual(session.turn_no, 8)
            self.assertEqual(len(session.memory.events), 8)
            self.assertEqual(session.memory.compacted_through_turn, 0)
            self.assertEqual(session.memory.last_compaction_attempt_turn, 8)
            self.assertIn("暂时不可用", session.memory.last_compaction_error)
            self.assertEqual(provider.compaction_calls, 1)

            self.play_turns(session, provider, recorder, 2)
            self.assertEqual(session.turn_no, 10)
            self.assertEqual(provider.compaction_calls, 1)

            self.play_turns(session, provider, recorder, 1)
            self.assertEqual(session.turn_no, 11)
            self.assertEqual(len(session.memory.events), 11)
            self.assertEqual(provider.compaction_calls, 2)
            self.assertEqual(session.memory.last_compaction_attempt_turn, 11)
            records = [
                json.loads(line)
                for line in recorder.path.read_text(encoding="utf-8").splitlines()
            ]

        errors = [
            record for record in records
            if record["event"] == "memory_compaction_error"
        ]
        self.assertEqual(len(errors), 2)
        self.assertEqual(
            errors[0]["diagnostics"]["final_content_state"],
            "transport_error",
        )
        turn_eight = next(
            record for record in records
            if record["event"] == "turn_committed" and record["turn"] == 8
        )
        self.assertTrue(turn_eight["memory_compaction"]["attempted"])
        self.assertFalse(turn_eight["memory_compaction"]["success"])

    def test_undo_restores_pre_compaction_memory_and_new_branch_is_independent(self) -> None:
        session = GameSession(self.story, log_dir=None)
        first_provider = ScriptedProvider(
            narratives=[f"原分支叙事 {index}" for index in range(1, 9)],
            fact_extractions=[() for _ in range(8)],
            memory_digests=[digest(4, "原分支小结")],
        )
        self.play_turns(
            session,
            first_provider,
            TraceRecorder(None, session.session_id),
            8,
        )
        abandoned = session.current_checkpoint_id

        session.undo()
        self.assertEqual(session.turn_no, 7)
        self.assertEqual(session.memory.rolling_summary, "")
        self.assertEqual(session.memory.compacted_through_turn, 0)

        second_provider = ScriptedProvider(
            narratives=["新分支叙事 8"],
            fact_extractions=[()],
            memory_digests=[digest(4, "新分支小结")],
        )
        resolve_player_turn(
            session,
            second_provider,
            TraceRecorder(None, session.session_id),
            "改走另一条路线",
        )
        self.assertEqual(session.memory.rolling_summary, "新分支小结")
        self.assertEqual(
            session.get_checkpoint(abandoned).memory.rolling_summary,
            "原分支小结",
        )

    def test_memory_command_displays_digest_boundary_and_failure_state(self) -> None:
        memory = MemoryState(events=tuple(memory_event(i) for i in range(1, 9)))
        memory = memory.apply_digest(digest(4), attempt_turn=8)
        rendered = render_memory(self.story, memory)
        self.assertIn("滚动小结（截至回合 4）", rendered)
        self.assertIn("前四回合的小结", rendered)

        failed = MemoryState(events=memory.events).record_compaction_failure(
            attempt_turn=8,
            error="临时失败",
        )
        failed_rendered = render_memory(self.story, failed)
        self.assertIn("上次小结失败", failed_rendered)
        self.assertIn("原始事件仍完整保留", failed_rendered)


if __name__ == "__main__":
    unittest.main()
