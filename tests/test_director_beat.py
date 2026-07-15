from __future__ import annotations

import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.director import validate_director_beat
from server.engine.llm import DirectorResponse, ScriptedProvider
from server.engine.llm_protocol import (
    DirectorBeat,
    DirectorBeatKind,
    DirectorPlan,
    FactBatch,
)
from server.engine.session import GameSession
from server.engine.state import build_initial_state


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "rooftop_supper.yaml"


def beat(
    actor_id: str,
    kind: DirectorBeatKind,
    location: str,
    *,
    revision: int = 0,
    summary: str = "既有人物根据自己的安排介入当前场景。",
) -> DirectorBeat:
    return DirectorBeat(
        beat_id=f"beat_{actor_id}_{kind.value}",
        state_revision=revision,
        kind=kind,
        actor_id=actor_id,
        target_location_id=location,
        target_ids=("player",),
        summary=summary,
        motivation="响应当前局势，同时保持原有动机。",
    )


class DirectorBeatValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.state = build_initial_state(self.story.data)

    def test_present_character_may_react(self) -> None:
        result = validate_director_beat(
            self.story,
            self.state,
            beat("aunt_chen", DirectorBeatKind.REACT, "building_lobby"),
            state_revision=0,
        )
        self.assertTrue(result.can_schedule)

    def test_adjacent_authored_character_may_enter(self) -> None:
        self.state["positions"]["player"] = "convenience_store"
        result = validate_director_beat(
            self.story,
            self.state,
            beat("aunt_chen", DirectorBeatKind.ENTER_SCENE, "convenience_store"),
            state_revision=0,
        )
        self.assertTrue(result.can_schedule)

    def test_unknown_actor_and_stale_revision_are_rejected(self) -> None:
        unknown = validate_director_beat(
            self.story,
            self.state,
            beat("invented_mentor", DirectorBeatKind.ENTER_SCENE, "building_lobby"),
            state_revision=0,
        )
        stale = validate_director_beat(
            self.story,
            self.state,
            beat("aunt_chen", DirectorBeatKind.REACT, "building_lobby", revision=2),
            state_revision=3,
        )
        self.assertIn("director.actor_not_authored", {i.code for i in unknown.issues})
        self.assertIn("director.stale_beat", {i.code for i in stale.issues})

    def test_actor_cannot_move_twice_in_one_commit(self) -> None:
        self.state["positions"]["player"] = "convenience_store"
        result = validate_director_beat(
            self.story,
            self.state,
            beat("aunt_chen", DirectorBeatKind.ENTER_SCENE, "convenience_store"),
            state_revision=0,
            moved_actor_ids={"aunt_chen"},
        )
        self.assertIn("director.actor_already_moved", {i.code for i in result.issues})


class DirectorBeatCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def move_batch(self) -> FactBatch:
        return FactBatch(
            state_revision=self.session.state_revision,
            player_text="我走进便利店。",
            narrative="你穿过门帘走进便利店。",
            state_changes={"positions.player": "convenience_store"},
        )

    def test_entry_beat_is_in_the_same_checkpoint(self) -> None:
        scheduled = beat(
            "aunt_chen",
            DirectorBeatKind.ENTER_SCENE,
            "convenience_store",
            revision=1,
            summary="陈阿姨来到便利店，并答应把雨布送给玩家。",
        )
        result = self.session.commit_fact_batch(
            self.move_batch(),
            director_provider=ScriptedProvider([], director_batches=[(scheduled,)]),
        )

        self.assertEqual(self.session.state["positions"]["aunt_chen"], "convenience_store")
        self.assertEqual(result.director_beats[0].actor_id, "aunt_chen")
        self.assertIn(
            f"director.{scheduled.beat_id}",
            result.change_sources,
        )
        self.assertEqual(result.narrative_hints, ["陈阿姨来到梧桐便利店。"])
        self.assertNotIn("答应", result.director_beats[0].narrative_hint)
        self.assertEqual(len(self.session.checkpoint_history()), 2)

        self.session.undo()
        self.assertEqual(self.session.state["positions"]["player"], "building_lobby")
        self.assertEqual(self.session.state["positions"]["aunt_chen"], "building_lobby")

    def test_unextracted_reaction_summary_stays_out_of_player_memory(self) -> None:
        scheduled = beat(
            "aunt_chen",
            DirectorBeatKind.REACT,
            "building_lobby",
            revision=1,
            summary="陈阿姨答应明天把雨布送给玩家。",
        )
        batch = FactBatch(
            state_revision=self.session.state_revision,
            player_text="我向陈阿姨点点头。",
            narrative="你向陈阿姨点了点头。",
        )

        result = self.session.commit_fact_batch(
            batch,
            director_provider=ScriptedProvider([], director_batches=[(scheduled,)]),
        )

        self.assertEqual(len(result.director_beats), 1)
        self.assertNotIn(scheduled.summary, result.narrative_hints)
        self.assertNotEqual(result.director_beats[0].narrative_hint, scheduled.summary)
        self.assertIn(scheduled.summary, result.director_trace["raw"])
        self.assertNotIn(scheduled.summary, self.session.recent_events)

    def test_invalid_beat_does_not_cancel_fact_commit(self) -> None:
        invalid = beat(
            "invented_mentor",
            DirectorBeatKind.ENTER_SCENE,
            "convenience_store",
            revision=1,
        )
        result = self.session.commit_fact_batch(
            self.move_batch(),
            director_provider=ScriptedProvider([], director_batches=[(invalid,)]),
        )
        self.assertEqual(self.session.state["positions"]["player"], "convenience_store")
        self.assertEqual(result.director_beats, [])
        self.assertTrue(result.director_trace["rejected"])

    def test_provider_failure_does_not_strand_fact_commit(self) -> None:
        class FailingDirectorProvider(ScriptedProvider):
            def propose_director(self, request):
                raise RuntimeError("temporary network failure")

        result = self.session.commit_fact_batch(
            self.move_batch(),
            director_provider=FailingDirectorProvider([]),
        )
        self.assertEqual(self.session.state_revision, 1)
        self.assertEqual(self.session.state["positions"]["player"], "convenience_store")
        self.assertIn("temporary network failure", result.director_trace["error"])

    def test_stale_plan_envelope_is_rejected_without_canceling_commit(self) -> None:
        class StaleDirectorProvider(ScriptedProvider):
            def propose_director(self, request):
                plan = DirectorPlan(state_revision=request.state_revision - 1)
                return DirectorResponse(plan=plan, raw="{}", model="stale-test")

        result = self.session.commit_fact_batch(
            self.move_batch(),
            director_provider=StaleDirectorProvider([]),
        )
        self.assertEqual(self.session.state_revision, 1)
        self.assertEqual(self.session.state["positions"]["player"], "convenience_store")
        self.assertEqual(
            result.director_trace["rejected"][0]["issues"][0]["code"],
            "director.stale_plan",
        )


if __name__ == "__main__":
    unittest.main()
