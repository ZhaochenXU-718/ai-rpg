from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.fact_pipeline import resolve_player_turn
from server.engine.llm import ScriptedProvider
from server.engine.llm_protocol import FactBatch
from server.engine.memory import MemoryEvent, MemoryState
from server.engine.renderer import render_memory
from server.engine.session import GameSession
from server.engine.state import StatePathError
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "rooftop_supper.yaml"


class NarrativeMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def commit(self, label: str, changes: dict | None = None):
        return self.session.commit_fact_batch(FactBatch(
            state_revision=self.session.state_revision,
            player_text=f"玩家行动 {label}",
            narrative=f"已提交叙事 {label}",
            state_changes=changes or {},
            references=("aunt_chen",) if label == "一" else (),
        ))

    def test_successful_turn_appends_exact_memory_event(self) -> None:
        result = self.commit("一")

        self.assertEqual(len(self.session.memory.events), 1)
        event = self.session.memory.events[0]
        self.assertEqual(event.turn_no, 1)
        self.assertEqual(event.commit_id, result.committed_turn.commit_id)
        self.assertEqual(event.player_text, "玩家行动 一")
        self.assertEqual(event.narrative, "已提交叙事 一")
        self.assertEqual(event.scene_before, "building_lobby")
        self.assertEqual(event.scene_after, "building_lobby")
        self.assertEqual(event.references, ("aunt_chen",))
        self.assertEqual(event.participants, ("player", "aunt_chen"))
        self.assertEqual(event.physical_changes, ())

    def test_physical_change_is_copied_into_memory_without_becoming_summary(self) -> None:
        self.commit("移动", {"positions.player": "convenience_store"})

        event = self.session.memory.events[-1]
        self.assertEqual(event.scene_after, "convenience_store")
        self.assertEqual(len(event.physical_changes), 1)
        self.assertEqual(event.physical_changes[0].path, "positions.player")
        self.assertEqual(event.physical_changes[0].previous, "building_lobby")
        self.assertEqual(event.physical_changes[0].new, "convenience_store")
        self.assertEqual(self.session.memory.rolling_summary, "")
        self.assertEqual(self.session.memory.open_loops, ())

    def test_full_journal_is_retained_while_prompt_view_uses_four_recent_turns(self) -> None:
        for index in range(1, 7):
            self.commit(str(index))

        self.assertEqual(len(self.session.memory.events), 6)
        self.assertEqual(
            [event.turn_no for event in self.session.memory.recent_events],
            [3, 4, 5, 6],
        )
        self.assertEqual(
            self.session.perception().recent_events,
            tuple(f"已提交叙事 {index}" for index in range(3, 7)),
        )

    def test_player_memory_is_not_reused_as_an_npc_private_memory(self) -> None:
        self.commit("玩家经历")
        self.assertEqual(
            self.session.perception().recent_events,
            ("已提交叙事 玩家经历",),
        )
        self.assertEqual(
            self.session.subject_perception("xiaoyu").recent_events,
            (),
        )

    def test_undo_restores_memory_and_new_branch_does_not_leak_abandoned_event(self) -> None:
        self.commit("A")
        fork_checkpoint = self.session.current_checkpoint_id
        self.commit("B")
        abandoned_checkpoint = self.session.current_checkpoint_id

        self.session.undo()
        self.assertEqual(
            [event.narrative for event in self.session.memory.events],
            ["已提交叙事 A"],
        )

        self.commit("C")
        self.assertEqual(
            [event.narrative for event in self.session.memory.events],
            ["已提交叙事 A", "已提交叙事 C"],
        )
        self.assertEqual(
            [event.narrative for event in self.session.get_checkpoint(
                abandoned_checkpoint
            ).memory.events],
            ["已提交叙事 A", "已提交叙事 B"],
        )
        self.assertEqual(
            len(self.session.get_checkpoint(fork_checkpoint).memory.events),
            1,
        )

    def test_failed_commit_does_not_append_memory(self) -> None:
        memory_before = copy.deepcopy(self.session.memory)
        with self.assertRaises(StatePathError):
            self.commit("坏批次", {"unknown.path": True})
        self.assertEqual(self.session.memory, memory_before)

    def test_memory_rendering_is_explicitly_non_authoritative(self) -> None:
        empty = render_memory(self.story, self.session.memory)
        self.assertIn("叙事记忆｜非权威状态", empty)
        self.assertIn("尚未达到压缩条件", empty)

        self.commit("一")
        raw = render_memory(self.story, self.session.memory, raw=True)
        self.assertIn("原始叙事事件｜非权威状态", raw)
        self.assertIn("玩家行动 一", raw)
        self.assertIn("已提交叙事 一", raw)

    def test_memory_rejects_out_of_order_events(self) -> None:
        event = MemoryEvent(
            turn_no=1,
            commit_id="commit_one",
            player_text="行动",
            narrative="叙事",
            scene_before="building_lobby",
            scene_after="building_lobby",
        )
        memory = MemoryState().append(event)
        with self.assertRaisesRegex(ValueError, "advance turn_no"):
            memory.append(event)

    def test_successful_event_is_written_to_session_log_and_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = GameSession(
                self.story,
                log_dir=str(Path(temp_dir) / "sessions"),
            )
            recorder = TraceRecorder(
                Path(temp_dir) / "traces",
                session.session_id,
            )
            resolve_player_turn(
                session,
                ScriptedProvider(
                    narratives=["你停下来听陈阿姨把话说完。"],
                    fact_extractions=[()],
                ),
                recorder,
                "我先听她说完。",
            )
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

        session_commit = next(
            record for record in session_records
            if record["event"] == "fact_batch_committed"
        )
        trace_commit = next(
            record for record in trace_records
            if record["event"] == "turn_committed"
        )
        self.assertEqual(session_commit["memory_event"]["turn_no"], 1)
        self.assertEqual(
            trace_commit["memory_event"]["narrative"],
            "你停下来听陈阿姨把话说完。",
        )


if __name__ == "__main__":
    unittest.main()
