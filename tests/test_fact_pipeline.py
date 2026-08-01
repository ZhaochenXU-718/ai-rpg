from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.fact_pipeline import TurnResolutionError, resolve_player_turn
from server.engine.llm import LLMProviderError, NarrativeStream, ScriptedProvider
from server.engine.llm_protocol import (
    CharacterMoveFact,
    FactExtraction,
    ItemPlacement,
    ItemTransferFact,
)
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


class FactPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GameSession(Story.load(FIXTURE), log_dir=None)
        self.recorder = TraceRecorder(None, self.session.session_id)

    def test_dialogue_stays_in_prose_and_item_delivery_commits_later(self) -> None:
        first_narrative = "周师傅点点头，明确答应明天把旧防雨布交给你。"
        second_narrative = "周师傅把旧防雨布递到你手里，兑现了昨天的承诺。"
        provider = ScriptedProvider(
            narratives=[first_narrative, second_narrative],
            fact_extractions=[
                (),
                (ItemTransferFact(
                        item_id="rain_canvas",
                        from_placement=ItemPlacement(
                            type="carried_by", id="keeper_zhou"
                        ),
                        to_placement=ItemPlacement(
                            type="carried_by", id="player"
                        ),
                        evidence="周师傅把旧防雨布递到你手里",
                    ),),
            ],
        )

        promised = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我说明用途，并问周师傅明天能否把防雨布给我。",
        )
        self.assertNotIn("commitments", self.session.state)
        self.assertEqual(
            self.session.state["item_locations"]["rain_canvas"]["id"],
            "keeper_zhou",
        )
        self.assertIn("明确答应", promised.narrative)

        fulfilled = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "第二天我再来找周师傅。",
        )
        self.assertEqual(
            self.session.state["item_locations"]["rain_canvas"],
            {"type": "carried_by", "id": "player"},
        )
        self.assertEqual(self.session.turn_no, 2)
        self.assertEqual(self.session.state_revision, 2)
        self.assertEqual(
            {change.path for change in fulfilled.committed_turn.committed_changes},
            {"item_locations.rain_canvas"},
        )

    def test_retry_rewrites_conflicting_candidate_and_commits_only_once(self) -> None:
        provider = ScriptedProvider(
            narratives=[
                "你一步走进不存在的地下密室。",
                "你在修理铺门口停下，没有贸然离开。",
            ],
            fact_extractions=[
                (CharacterMoveFact(
                    actor_id="player",
                    destination_id="secret_vault",
                    evidence="你一步走进不存在的地下密室",
                ),),
                (),
            ],
        )
        result = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我找找有没有通往地下的门。",
            max_regenerations=1,
        )
        self.assertIn("没有贸然离开", result.narrative)
        self.assertEqual(self.session.state["positions"]["player"], "workshop")
        self.assertEqual(self.session.turn_no, 1)
        self.assertEqual(self.session.state_revision, 1)
        self.assertEqual(len(self.session.checkpoint_history()), 2)

    def test_stream_receives_restart_between_conflicting_candidates(self) -> None:
        provider = ScriptedProvider(
            narratives=[
                "你一步走进不存在的地下密室。",
                "你在修理铺门口停下，没有贸然离开。",
            ],
            fact_extractions=[
                (CharacterMoveFact(
                    actor_id="player",
                    destination_id="secret_vault",
                    evidence="你一步走进不存在的地下密室",
                ),),
                (),
            ],
        )
        stream = RecordingStream()
        result = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我找找有没有通往地下的门。",
            max_regenerations=1,
            stream=stream,
        )
        self.assertEqual(stream.events, [
            ("delta", "你一步走进不存在的地下密室。"),
            ("restart", "physical_conflict"),
            ("delta", "你在修理铺门口停下，没有贸然离开。"),
        ])
        self.assertIn("没有贸然离开", result.narrative)

    def test_valid_player_move_is_extracted_and_committed(self) -> None:
        narrative = "你从修理铺门口走进相邻的公共院子。"
        provider = ScriptedProvider(
            narratives=[narrative],
            fact_extractions=[(CharacterMoveFact(
                actor_id="player",
                destination_id="courtyard",
                evidence=narrative,
            ),)],
        )

        result = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我独自沿门口走进公共院子。",
        )

        self.assertEqual(self.session.state["positions"]["player"], "courtyard")
        self.assertEqual(result.scene_after, "courtyard")
        self.assertEqual(self.session.turn_no, 1)
        self.assertEqual(self.session.state_revision, 1)
        self.assertIn(
            "positions.player",
            {change.path for change in result.committed_turn.committed_changes},
        )

    def test_exhausted_retries_leave_state_turn_and_checkpoint_unchanged(self) -> None:
        state_before = copy.deepcopy(self.session.state)
        memory_before = copy.deepcopy(self.session.memory)
        checkpoint_before = self.session.current_checkpoint_id
        provider = ScriptedProvider(
            narratives=[
                "你一步走进不存在的地下密室。",
                "你又转身进入不存在的阁楼。",
            ],
            fact_extractions=[
                (CharacterMoveFact(
                    actor_id="player",
                    destination_id="secret_vault",
                    evidence="你一步走进不存在的地下密室",
                ),),
                (CharacterMoveFact(
                    actor_id="player",
                    destination_id="secret_attic",
                    evidence="你又转身进入不存在的阁楼",
                ),),
            ],
        )
        with self.assertRaises(TurnResolutionError):
            resolve_player_turn(
                self.session,
                provider,
                self.recorder,
                "我寻找一条不存在的路。",
                max_regenerations=1,
            )
        self.assertEqual(self.session.state, state_before)
        self.assertEqual(self.session.memory, memory_before)
        self.assertEqual(self.session.turn_no, 0)
        self.assertEqual(self.session.state_revision, 0)
        self.assertEqual(self.session.current_checkpoint_id, checkpoint_before)

    def test_stale_extraction_is_not_regenerated_or_committed(self) -> None:
        provider = ScriptedProvider(
            narratives=["你仍站在修理铺里。"],
            fact_extractions=[FactExtraction(state_revision=9, facts=())],
        )
        with self.assertRaisesRegex(TurnResolutionError, "修订"):
            resolve_player_turn(
                self.session,
                provider,
                self.recorder,
                "我先确认自己在哪里。",
            )
        self.assertEqual(self.session.turn_no, 0)
        self.assertEqual(self.session.state_revision, 0)

    def test_extractor_failure_is_traced_without_committing_the_turn(self) -> None:
        class FailingExtractor(ScriptedProvider):
            def extract_facts(self, request):
                raise LLMProviderError(
                    "length exhausted",
                    diagnostics={
                        "final_content_state": "length_exhausted",
                        "failed_usage": {"total_tokens": 1200},
                    },
                )

        state_before = copy.deepcopy(self.session.state)
        memory_before = copy.deepcopy(self.session.memory)
        checkpoint_before = self.session.current_checkpoint_id
        provider = FailingExtractor(narratives=["你仍站在修理铺门口。"])
        with tempfile.TemporaryDirectory() as trace_dir:
            recorder = TraceRecorder(trace_dir, self.session.session_id)
            with self.assertRaisesRegex(LLMProviderError, "length exhausted"):
                resolve_player_turn(
                    self.session,
                    provider,
                    recorder,
                    "我先看看周围。",
                )
            records = [
                json.loads(line)
                for line in recorder.path.read_text(encoding="utf-8").splitlines()
            ]

        failure = next(
            record for record in records
            if record["event"] == "fact_extraction_error"
        )
        self.assertEqual(
            failure["diagnostics"]["final_content_state"],
            "length_exhausted",
        )
        self.assertEqual(self.session.state, state_before)
        self.assertEqual(self.session.memory, memory_before)
        self.assertEqual(self.session.turn_no, 0)
        self.assertEqual(self.session.state_revision, 0)
        self.assertEqual(self.session.current_checkpoint_id, checkpoint_before)

if __name__ == "__main__":
    unittest.main()
