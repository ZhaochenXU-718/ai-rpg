"""Optional prose editor: conservative guards and turn-pipeline selection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.fact_pipeline import resolve_player_turn
from server.engine.llm import NarrativeStream, ScriptedProvider
from server.engine.llm_protocol import CharacterMoveFact
from server.engine.prose_editor import guard_prose_edit
from server.engine.session import GameSession
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


class RecordingStream(NarrativeStream):
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def delta(self, text: str) -> None:
        self.events.append(("delta", text))

    def restart(self, reason: str) -> None:
        self.events.append(("restart", reason))


class ProseEditGuardTest(unittest.TestCase):
    def test_local_edit_preserving_names_and_numbers_is_accepted(self) -> None:
        guard = guard_prose_edit(
            "周师傅停了一下。他在第2次确认后，又再次点了点头。",
            "周师傅停了一下。他第2次确认后，点了点头。",
            protected_terms=("周师傅", "旧防雨布"),
        )
        self.assertTrue(guard.accepted)
        self.assertTrue(guard.changed)

    def test_stage_direction_style_overcompression_is_rejected(self) -> None:
        draft = (
            "威克利夫教授的指尖在绒布边缘停了一瞬，那动作太快，"
            "不像是犹豫，更像是在克制某种更深的反应。"
        )
        guard = guard_prose_edit(
            draft,
            "威克利夫教授的手停在绒布上。",
            protected_terms=("威克利夫教授",),
        )
        self.assertFalse(guard.accepted)
        self.assertIn("over_compressed", guard.reasons)

    def test_protected_terms_numbers_and_output_contract_are_guarded(self) -> None:
        guard = guard_prose_edit(
            "周师傅说第2天把旧防雨布交给你。",
            "修改后：林姐说第3天把东西交给你。",
            protected_terms=("周师傅", "林姐", "旧防雨布"),
        )
        self.assertFalse(guard.accepted)
        self.assertIn("output_artifact", guard.reasons)
        self.assertIn("protected_term_removed", guard.reasons)
        self.assertIn("protected_term_added", guard.reasons)
        self.assertIn("number_removed", guard.reasons)
        self.assertIn("number_added", guard.reasons)


class ProseEditorPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GameSession(Story.load(FIXTURE), log_dir=None)
        self.recorder = TraceRecorder(None, self.session.session_id)

    def test_off_keeps_existing_stream_and_never_calls_editor(self) -> None:
        draft = "周师傅点点头，答应把防雨布借给你。"
        provider = ScriptedProvider(
            narratives=[draft],
            fact_extractions=[()],
        )
        stream = RecordingStream()

        result = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我说明来意。",
            stream=stream,
            prose_editor_mode="off",
        )

        self.assertEqual(result.narrative, draft)
        self.assertEqual(stream.events, [("delta", draft)])

    def test_shadow_records_candidate_but_commits_streamed_draft(self) -> None:
        draft = (
            "周师傅没有立刻回答。他把扳手搁在桌边，等你把用途说完，"
            "才慢慢点了点头。"
        )
        candidate = (
            "周师傅没有立刻回答。他把扳手搁在桌边，听完你的用途，"
            "才慢慢点头。"
        )
        provider = ScriptedProvider(
            narratives=[draft],
            prose_edits=[candidate],
            fact_extractions=[()],
        )
        stream = RecordingStream()

        with tempfile.TemporaryDirectory() as trace_dir:
            recorder = TraceRecorder(trace_dir, self.session.session_id)
            result = resolve_player_turn(
                self.session,
                provider,
                recorder,
                "我说明来意。",
                stream=stream,
                prose_editor_mode="shadow",
            )
            records = [
                json.loads(line)
                for line in recorder.path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result.narrative, draft)
        self.assertEqual(stream.events, [("delta", draft)])
        editor = next(record for record in records if record["event"] == "prose_editor")
        self.assertEqual(editor["candidate"], candidate)
        selection = next(
            record for record in records
            if record["event"] == "prose_editor_selection"
        )
        self.assertEqual(selection["selected"], "draft")
        self.assertEqual(selection["reason"], "shadow_mode")

    def test_on_buffers_draft_and_streams_physically_equivalent_edit(self) -> None:
        draft = (
            "周师傅没有立刻回答。他把扳手搁在桌边，等你把用途说完，"
            "才慢慢点了点头。"
        )
        candidate = (
            "周师傅没有立刻回答。他把扳手搁在桌边，听完你的用途，"
            "才慢慢点头。"
        )
        provider = ScriptedProvider(
            narratives=[draft],
            prose_edits=[candidate],
            # 原稿与编辑稿都没有物理变化；on 会分别抽取一次。
            fact_extractions=[(), ()],
        )
        stream = RecordingStream()

        result = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我说明来意。",
            stream=stream,
            prose_editor_mode="on",
        )

        self.assertEqual(result.narrative, candidate)
        self.assertEqual(stream.events, [("delta", candidate)])

    def test_on_rejects_overcompressed_candidate_before_second_extraction(self) -> None:
        draft = (
            "周师傅没有立刻回答。他把扳手搁在桌边，目光在旧防雨布上"
            "停了一瞬，等你把用途说完，才慢慢点了点头。"
        )
        provider = ScriptedProvider(
            narratives=[draft],
            prose_edits=["周师傅点了点头。"],
            fact_extractions=[()],
        )
        stream = RecordingStream()

        result = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我说明来意。",
            stream=stream,
            prose_editor_mode="on",
        )

        self.assertEqual(result.narrative, draft)
        self.assertEqual(stream.events, [("delta", draft)])

    def test_on_falls_back_when_editor_changes_physical_signature(self) -> None:
        draft = (
            "你和周师傅说完后，从修理铺门口走进相邻的公共院子，"
            "脚步在门槛外停了一瞬。"
        )
        candidate = (
            "你和周师傅说完后，仍站在修理铺门口望向相邻的公共院子，"
            "脚步在门槛内停了一瞬。"
        )
        provider = ScriptedProvider(
            narratives=[draft],
            prose_edits=[candidate],
            fact_extractions=[
                (CharacterMoveFact(
                    actor_id="player",
                    destination_id="courtyard",
                    evidence="你和周师傅说完后，从修理铺门口走进相邻的公共院子",
                ),),
                (),
            ],
        )
        stream = RecordingStream()

        result = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我走进公共院子。",
            stream=stream,
            prose_editor_mode="on",
        )

        self.assertEqual(result.narrative, draft)
        self.assertEqual(stream.events, [("delta", draft)])
        self.assertEqual(self.session.state["positions"]["player"], "courtyard")

    def test_on_editor_failure_never_blocks_valid_draft(self) -> None:
        draft = "周师傅听完你的来意，点了点头。"
        provider = ScriptedProvider(
            narratives=[draft],
            fact_extractions=[()],
        )
        stream = RecordingStream()

        result = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我说明来意。",
            stream=stream,
            prose_editor_mode="on",
        )

        self.assertEqual(result.narrative, draft)
        self.assertEqual(stream.events, [("delta", draft)])


if __name__ == "__main__":
    unittest.main()
