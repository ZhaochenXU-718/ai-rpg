from __future__ import annotations

import copy
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.fact_pipeline import TurnResolutionError, resolve_player_turn
from server.engine.llm import ScriptedProvider
from server.engine.llm_protocol import (
    CharacterMoveFact,
    CommitmentFact,
    CommitmentUpdateFact,
    FactAuthority,
    FactExtraction,
    ItemPlacement,
    ItemTransferFact,
)
from server.engine.session import GameSession
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


class FactPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GameSession(Story.load(FIXTURE), log_dir=None)
        self.recorder = TraceRecorder(None, self.session.session_id)

    def test_promise_is_remembered_then_fulfilled_by_item_delivery(self) -> None:
        first_narrative = "周师傅点点头，明确答应明天把旧防雨布交给你。"
        second_narrative = "周师傅把旧防雨布递到你手里，兑现了昨天的承诺。"
        provider = ScriptedProvider(
            narratives=[first_narrative, second_narrative],
            fact_extractions=[
                (CommitmentFact(
                    commitment_id="promise_canvas",
                    promisor_id="keeper_zhou",
                    promisee_id="player",
                    description="明天把旧防雨布交给玩家",
                    related_item_id="rain_canvas",
                    due="明天",
                    evidence="明确答应明天把旧防雨布交给你",
                ),),
                (
                    ItemTransferFact(
                        item_id="rain_canvas",
                        from_placement=ItemPlacement(
                            type="carried_by", id="keeper_zhou"
                        ),
                        to_placement=ItemPlacement(
                            type="carried_by", id="player"
                        ),
                        evidence="周师傅把旧防雨布递到你手里",
                    ),
                    CommitmentUpdateFact(
                        commitment_id="promise_canvas",
                        status="fulfilled",
                        evidence="兑现了昨天的承诺",
                    ),
                ),
            ],
        )

        promised = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "我说明用途，并问周师傅明天能否把防雨布给我。",
        )
        self.assertEqual(
            self.session.state["commitments"]["promise_canvas"]["status"],
            "open",
        )
        self.assertEqual(
            self.session.state["item_locations"]["rain_canvas"]["id"],
            "keeper_zhou",
        )
        self.assertIn("承诺", "".join(promised.new_facts))

        fulfilled = resolve_player_turn(
            self.session,
            provider,
            self.recorder,
            "第二天我再来找周师傅。",
        )
        self.assertEqual(
            self.session.state["commitments"]["promise_canvas"]["status"],
            "fulfilled",
        )
        self.assertEqual(
            self.session.state["item_locations"]["rain_canvas"],
            {"type": "carried_by", "id": "player"},
        )
        self.assertEqual(fulfilled.ending, "canvas_borrowed")
        self.assertEqual(self.session.turn_no, 2)
        self.assertEqual(self.session.state_revision, 2)
        self.assertTrue(all(
            change.authority == FactAuthority.IRON_LAW
            for change in fulfilled.committed_turn.committed_changes
        ))

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

    def test_exhausted_retries_leave_state_turn_and_checkpoint_unchanged(self) -> None:
        state_before = copy.deepcopy(self.session.state)
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


if __name__ == "__main__":
    unittest.main()
