from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.llm import (
    FactExtractionRequest,
    LLMProvider,
    NarrativeRequest,
    NarrativeResponse,
    SuggestionRequest,
    SuggestionResponse,
)
from server.engine.llm_deepseek import (
    build_fact_extraction_messages,
    build_narrative_messages,
    build_suggestion_messages,
)
from server.engine.llm_protocol import (
    MemoryContext,
    SuggestedAction,
)
from server.engine.memory import (
    MEMORY_CONTEXT_CHAR_BUDGET,
    MemoryDigest,
    MemoryEvent,
    MemoryNoteGroup,
    MemoryState,
    build_memory_context,
)
from server.engine.narration import narrate_player_turn
from server.engine.session import GameSession, SessionError
from server.engine.suggestions import generate_action_suggestions
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


def event(turn_no: int, *, size: int = 0) -> MemoryEvent:
    suffix = "长" * size
    return MemoryEvent(
        turn_no=turn_no,
        commit_id=f"commit_{turn_no}",
        player_text=f"玩家行动 {turn_no}{suffix}",
        narrative=f"已提交叙事 {turn_no}{suffix}",
        scene_before="workshop",
        scene_after="workshop",
    )


def digest() -> MemoryDigest:
    return MemoryDigest(
        compacted_through_turn=4,
        rolling_summary="前四回合里，大家一直在商量怎样使用遮雨布。",
        open_loops=("遮雨布的用途仍未确定",),
        character_notes=(
            MemoryNoteGroup("keeper_zhou", ("愿意继续听玩家说明用途",)),
        ),
        scene_notes=(
            MemoryNoteGroup("workshop", ("防雨布暂时还在修理铺",)),
        ),
        recently_resolved=("已经确认架子上有旧防雨布",),
    )


class CapturingGenerationProvider(LLMProvider):
    def __init__(self) -> None:
        self.narrative_request = None
        self.suggestion_request = None

    def render_narrative(self, request):
        self.narrative_request = request
        return NarrativeResponse(
            text="你接着先前的话题，把遮雨布的用途又说得具体了一些。",
            model="capture",
        )

    def propose_suggestions(self, request):
        self.suggestion_request = request
        card = SuggestedAction(
            suggestion_id="suggestion_continue",
            perception_revision=request.perception.state_revision,
            title="继续当前话题",
            action_text="我继续把眼前的遮雨布用途说具体。",
            focus="social",
            rationale="承接尚未解决的当前话题。",
        )
        return SuggestionResponse(
            suggestions=(card,),
            raw=json.dumps({"suggestions": [card.to_dict()]}, ensure_ascii=False),
            model="capture",
        )


class MemoryContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)

    def session_with_digest(self) -> GameSession:
        session = GameSession(self.story, log_dir=None)
        for turn_no in range(1, 9):
            session.commit_narrative(
                f"玩家行动 {turn_no}",
                f"已提交叙事 {turn_no}",
            )
        session.apply_memory_digest(digest())
        return session

    def test_uncompacted_buffer_closes_the_pre_summary_memory_gap(self) -> None:
        session = GameSession(self.story, log_dir=None)
        for turn_no in range(1, 8):
            session.commit_narrative(
                f"玩家行动 {turn_no}",
                f"已提交叙事 {turn_no}",
            )

        context = session.memory_context()
        self.assertEqual(
            [item.turn_no for item in context.uncompacted_events],
            [1, 2, 3, 4, 5, 6, 7],
        )
        self.assertEqual(
            session.perception().recent_events,
            tuple(f"已提交叙事 {turn_no}" for turn_no in range(4, 8)),
        )

    def test_digest_and_only_newer_raw_events_form_the_context(self) -> None:
        session = self.session_with_digest()
        context = session.memory_context()

        self.assertEqual(context.compacted_through_turn, 4)
        self.assertIn("前四回合", context.rolling_summary)
        self.assertEqual(
            [item.turn_no for item in context.uncompacted_events],
            [5, 6, 7, 8],
        )
        self.assertEqual(context.open_loops, ("遮雨布的用途仍未确定",))
        self.assertEqual(
            context.character_notes["keeper_zhou"],
            ("愿意继续听玩家说明用途",),
        )
        self.assertEqual(MemoryContext.from_dict(context.to_dict()), context)

    def test_context_has_a_hard_text_budget_and_reports_truncation(self) -> None:
        memory = MemoryState(
            events=tuple(event(turn_no, size=1000) for turn_no in range(1, 21)),
            rolling_summary="总" * 3000,
            open_loops=tuple("线" * 500 for _ in range(10)),
            character_notes=(
                MemoryNoteGroup("person", tuple("人" * 500 for _ in range(5))),
            ),
            scene_notes=(
                MemoryNoteGroup("scene", tuple("景" * 500 for _ in range(5))),
            ),
            recently_resolved=tuple("解" * 500 for _ in range(8)),
        )
        context = build_memory_context(
            memory,
            subject_id="player",
            turn_no=20,
            state_revision=20,
        )
        actual_chars = len(context.rolling_summary)
        actual_chars += sum(len(item) for item in context.open_loops)
        actual_chars += sum(
            len(note)
            for notes in context.character_notes.values()
            for note in notes
        )
        actual_chars += sum(
            len(note)
            for notes in context.scene_notes.values()
            for note in notes
        )
        actual_chars += sum(len(item) for item in context.recently_resolved)
        actual_chars += sum(
            len(item.player_text) + len(item.narrative)
            for item in context.uncompacted_events
        )

        self.assertLessEqual(context.text_chars, MEMORY_CONTEXT_CHAR_BUDGET)
        self.assertEqual(context.text_chars, actual_chars)
        self.assertTrue(context.truncated)
        self.assertEqual(
            [item.turn_no for item in context.uncompacted_events],
            list(range(13, 21)),
        )

    def test_narration_and_ideas_receive_the_same_context(self) -> None:
        session = self.session_with_digest()
        provider = CapturingGenerationProvider()
        recorder = TraceRecorder(None, session.session_id)

        narrate_player_turn(session, provider, "我继续说明用途。", recorder)
        generate_action_suggestions(session, provider, recorder)

        expected = session.memory_context()
        self.assertEqual(provider.narrative_request.memory_context, expected)
        self.assertEqual(provider.suggestion_request.memory_context, expected)
        self.assertNotIn("近期叙事", provider.narrative_request.facts)

    def test_deepseek_prompts_use_one_context_without_perception_duplication(self) -> None:
        session = self.session_with_digest()
        provider = CapturingGenerationProvider()
        recorder = TraceRecorder(None, session.session_id)
        narrate_player_turn(session, provider, "我继续说明用途。", recorder)
        generate_action_suggestions(session, provider, recorder)

        narrative_messages = build_narrative_messages(
            provider.narrative_request
        )
        suggestion_messages = build_suggestion_messages(
            provider.suggestion_request
        )
        narrative_payload = json.loads(narrative_messages[1]["content"])
        suggestion_payload = json.loads(suggestion_messages[1]["content"])

        self.assertEqual(
            narrative_payload["软叙事记忆"],
            suggestion_payload["软叙事记忆"],
        )
        self.assertNotIn("recent_events", suggestion_payload["感知快照"])
        self.assertIn("当前事实清单与当前感知优先", narrative_messages[0]["content"])
        self.assertIn("当前感知优先", suggestion_messages[0]["content"])

        extraction = FactExtractionRequest(
            perception=session.perception(),
            player_text="我继续说明用途。",
            narrative="你继续把遮雨布的用途说得具体。",
            ledger={},
        )
        extraction_prompt = json.dumps(
            build_fact_extraction_messages(extraction),
            ensure_ascii=False,
        )
        context_text = digest().rolling_summary
        self.assertNotIn(context_text, extraction_prompt)
        self.assertFalse(hasattr(extraction, "memory_context"), context_text)

    def test_npc_scope_and_compaction_errors_never_leak_player_memory(self) -> None:
        session = self.session_with_digest()
        session.record_memory_compaction_failure("provider private transport detail")

        player_context = session.memory_context()
        serialized = json.dumps(player_context.to_dict(), ensure_ascii=False)
        self.assertNotIn("private transport detail", serialized)
        self.assertIsNone(session.memory_context("neighbor_lin"))
        self.assertEqual(session.subject_perception("neighbor_lin").recent_events, ())
        with self.assertRaises(SessionError):
            session.memory_context("unknown_subject")

    def test_generation_request_rejects_stale_or_cross_subject_context(self) -> None:
        session = self.session_with_digest()
        context = session.memory_context()
        stale = context.model_copy(update={"state_revision": 999})
        wrong_subject = context.model_copy(update={"subject_id": "neighbor_lin"})

        with self.assertRaisesRegex(ValueError, "revision"):
            NarrativeRequest(
                kind="turn",
                perception=session.perception(),
                facts={},
                memory_context=stale,
            )
        with self.assertRaisesRegex(ValueError, "subject"):
            SuggestionRequest(
                perception=session.perception(),
                memory_context=wrong_subject,
            )

    def test_trace_records_the_context_used_by_both_generation_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self.session_with_digest()
            provider = CapturingGenerationProvider()
            recorder = TraceRecorder(Path(temp_dir), session.session_id)
            narrate_player_turn(session, provider, "我继续说明用途。", recorder)
            generate_action_suggestions(session, provider, recorder)
            records = [
                json.loads(line)
                for line in recorder.path.read_text(encoding="utf-8").splitlines()
            ]

        narration = next(record for record in records if record["event"] == "narration")
        suggestions = next(
            record for record in records
            if record["event"] == "suggestions_response"
        )
        self.assertEqual(
            narration["memory_context"],
            suggestions["memory_context"],
        )
        self.assertEqual(
            narration["memory_context"]["compacted_through_turn"],
            4,
        )


if __name__ == "__main__":
    unittest.main()
