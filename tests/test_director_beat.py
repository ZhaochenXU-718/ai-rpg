from __future__ import annotations

import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.director import validate_director_beat
from server.engine.llm import ScriptedProvider
from server.engine.llm_protocol import (
    ActionPlan,
    CapabilityAction,
    DirectorBeat,
    DirectorBeatKind,
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
) -> DirectorBeat:
    return DirectorBeat(
        beat_id=f"beat_{actor_id}_{kind.value}",
        state_revision=revision,
        kind=kind,
        actor_id=actor_id,
        target_location_id=location,
        target_ids=("player",),
        summary="既有人物根据自己的安排介入当前场景。",
        motivation="响应当前局势，同时保持原有动机。",
    )


class DirectorBeatValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.state = build_initial_state(self.story.data)

    def test_authored_present_character_may_react(self) -> None:
        result = validate_director_beat(
            self.story,
            self.state,
            beat("aunt_chen", DirectorBeatKind.REACT, "building_lobby"),
            state_revision=0,
        )

        self.assertTrue(result.can_schedule)
        self.assertFalse(result.issues)

        environment_result = validate_director_beat(
            self.story,
            self.state,
            beat(
                "aunt_chen",
                DirectorBeatKind.REACT,
                "building_lobby",
            ).model_copy(update={"target_ids": ("notice_board",)}),
            state_revision=0,
        )
        self.assertTrue(environment_result.can_schedule)

    def test_authored_adjacent_character_may_be_proposed_for_entry(self) -> None:
        self.state["positions"]["player"] = "convenience_store"
        result = validate_director_beat(
            self.story,
            self.state,
            beat("aunt_chen", DirectorBeatKind.ENTER_SCENE, "convenience_store"),
            state_revision=0,
        )

        self.assertTrue(result.can_schedule)

    def test_unknown_actor_cannot_be_created_by_a_beat(self) -> None:
        result = validate_director_beat(
            self.story,
            self.state,
            beat("invented_mentor", DirectorBeatKind.ENTER_SCENE, "building_lobby"),
            state_revision=0,
        )

        self.assertFalse(result.can_schedule)
        self.assertIn(
            "director.actor_not_authored",
            {issue.code for issue in result.issues},
        )

    def test_stale_beat_is_rejected(self) -> None:
        result = validate_director_beat(
            self.story,
            self.state,
            beat(
                "aunt_chen",
                DirectorBeatKind.REACT,
                "building_lobby",
                revision=2,
            ),
            state_revision=3,
        )

        self.assertFalse(result.can_schedule)
        self.assertIn(
            "director.stale_beat",
            {issue.code for issue in result.issues},
        )

    def test_actor_cannot_move_twice_in_one_commit(self) -> None:
        self.state["positions"]["player"] = "convenience_store"
        result = validate_director_beat(
            self.story,
            self.state,
            beat("aunt_chen", DirectorBeatKind.ENTER_SCENE, "convenience_store"),
            state_revision=0,
            moved_actor_ids={"aunt_chen"},
        )

        self.assertFalse(result.can_schedule)
        self.assertIn(
            "director.actor_already_moved",
            {issue.code for issue in result.issues},
        )


class DirectorBeatCommitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)
        self.session.resolve(intent_id="move", objects=["convenience_store"])

    def observe_plan(self) -> ActionPlan:
        text = "我看了看冰柜里的冷饮。"
        return ActionPlan(
            plan_id="plan_observe_cold_drinks",
            perception_revision=self.session.state_revision,
            player_text=text,
            interpretation="观察便利店冰柜里的冷饮。",
            goal="inspect_cold_drinks",
            intent_id="observe",
            steps=(CapabilityAction(
                capability="intent",
                action="observe",
                arguments={"objects": ["cold_drinks"]},
                purpose="观察冷饮",
            ),),
            references=("cold_drinks",),
        )

    def test_entry_beat_is_part_of_the_same_checkpoint_and_outcome(self) -> None:
        plan = self.observe_plan()
        validation = self.session.validate_plan(plan)
        scheduled = beat(
            "aunt_chen",
            DirectorBeatKind.ENTER_SCENE,
            "convenience_store",
            revision=self.session.state_revision + 1,
        )
        provider = ScriptedProvider(
            [],
            director_batches=[(scheduled,)],
        )
        checkpoint_count = len(self.session.checkpoint_history())

        outcome = self.session.resolve_plan(
            plan,
            validation,
            director_provider=provider,
        )

        self.assertEqual(
            self.session.state["positions"]["aunt_chen"],
            "convenience_store",
        )
        self.assertEqual(len(outcome.director_beats), 1)
        self.assertIn(
            f"director.{scheduled.beat_id}",
            outcome.world_events,
        )
        self.assertTrue(any(
            change.path == "positions.aunt_chen"
            and change.source == f"director.{scheduled.beat_id}"
            for change in outcome.committed_changes
        ))
        self.assertEqual(
            len(self.session.checkpoint_history()),
            checkpoint_count + 1,
        )

        self.session.undo()
        self.assertEqual(
            self.session.state["positions"]["player"],
            "convenience_store",
        )
        self.assertEqual(
            self.session.state["positions"]["aunt_chen"],
            "building_lobby",
        )

    def test_invalid_director_beat_does_not_cancel_the_player_action(self) -> None:
        plan = self.observe_plan()
        validation = self.session.validate_plan(plan)
        invalid = beat(
            "invented_mentor",
            DirectorBeatKind.ENTER_SCENE,
            "convenience_store",
            revision=self.session.state_revision + 1,
        )
        provider = ScriptedProvider([], director_batches=[(invalid,)])

        outcome = self.session.resolve_plan(
            plan,
            validation,
            director_provider=provider,
        )

        self.assertEqual(outcome.turn_no, 2)
        self.assertFalse(outcome.director_beats)
        self.assertTrue(self.session.last_result.director_trace["rejected"])
        self.assertNotIn("invented_mentor", self.session.state["positions"])

    def test_director_provider_failure_does_not_strand_the_player_commit(self) -> None:
        class FailingDirectorProvider(ScriptedProvider):
            def propose_director_beats(self, request):
                raise RuntimeError("temporary network failure")

        plan = self.observe_plan()
        validation = self.session.validate_plan(plan)
        checkpoint_count = len(self.session.checkpoint_history())

        outcome = self.session.resolve_plan(
            plan,
            validation,
            director_provider=FailingDirectorProvider([]),
        )

        self.assertEqual(outcome.turn_no, 2)
        self.assertEqual(self.session.state_revision, 2)
        self.assertEqual(
            len(self.session.checkpoint_history()),
            checkpoint_count + 1,
        )
        self.assertIn(
            "temporary network failure",
            self.session.last_result.director_trace["error"],
        )


if __name__ == "__main__":
    unittest.main()
