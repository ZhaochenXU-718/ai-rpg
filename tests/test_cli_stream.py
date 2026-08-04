"""CLI streaming display: live deltas, withdrawal notices, no double print."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from server.cli import ConsoleNarrativeStream, execute_narrative_turn
from server.engine.content import Story
from server.engine.llm import ScriptedProvider
from server.engine.llm_protocol import CharacterMoveFact
from server.engine.session import GameSession
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


class ConsoleNarrativeStreamTest(unittest.TestCase):
    def test_deltas_print_inline_and_close_terminates_the_line(self) -> None:
        stream = ConsoleNarrativeStream()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            stream.delta("周师傅")
            stream.delta("点点头。")
            stream.close()
        self.assertEqual(buffer.getvalue(), "周师傅点点头。\n")

    def test_restart_prints_notice_only_after_visible_text(self) -> None:
        stream = ConsoleNarrativeStream()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            stream.restart("empty_content")  # 尚无可见文本，应保持沉默
            stream.delta("你走进不存在的密室。")
            stream.restart("physical_conflict")
            stream.delta("你停在门口。")
            stream.close()
        output = buffer.getvalue()
        self.assertIn(
            "你走进不存在的密室。\n"
            "（上面这段与物理事实核对冲突，已撤回，正在重写……）",
            output,
        )
        self.assertTrue(output.endswith("你停在门口。\n"))
        self.assertEqual(output.count("撤回"), 1)

    def test_close_is_silent_when_nothing_was_streamed(self) -> None:
        stream = ConsoleNarrativeStream()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            stream.close()
        self.assertEqual(buffer.getvalue(), "")


class ExecuteTurnStreamingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GameSession(Story.load(FIXTURE), log_dir=None)
        self.recorder = TraceRecorder(None, self.session.session_id)

    def test_streamed_prose_is_not_printed_twice(self) -> None:
        provider = ScriptedProvider(
            narratives=["周师傅点点头，答应把防雨布借给你。"],
            fact_extractions=[()],
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            execute_narrative_turn(
                self.session, provider, self.recorder, "我说明来意。"
            )
        output = buffer.getvalue()
        self.assertEqual(
            output.count("周师傅点点头，答应把防雨布借给你。"), 1
        )
        self.assertEqual(self.session.turn_no, 1)

    def test_scene_change_tail_still_prints_after_streamed_prose(self) -> None:
        narrative = "你从修理铺门口走进相邻的公共院子。"
        provider = ScriptedProvider(
            narratives=[narrative],
            fact_extractions=[(CharacterMoveFact(
                actor_id="player",
                destination_id="courtyard",
                evidence=narrative,
            ),)],
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            execute_narrative_turn(
                self.session, provider, self.recorder, "我走进公共院子。"
            )
        output = buffer.getvalue()
        self.assertEqual(output.count(narrative), 1)
        self.assertIn("物理事实提交", output)
        self.assertIn("positions.player", output)

    def test_editor_on_prints_only_the_selected_candidate(self) -> None:
        draft = "周师傅点了点头，又再次答应把防雨布借给你。"
        edited = "周师傅点了点头，答应把防雨布借给你。"
        provider = ScriptedProvider(
            narratives=[draft],
            prose_edits=[edited],
            fact_extractions=[(), ()],
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            execute_narrative_turn(
                self.session,
                provider,
                self.recorder,
                "我说明来意。",
                prose_editor_mode="on",
            )
        output = buffer.getvalue()
        self.assertNotIn(draft, output)
        self.assertEqual(output.count(edited), 1)
        self.assertEqual(self.session.turn_no, 1)


if __name__ == "__main__":
    unittest.main()
