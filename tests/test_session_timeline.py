from __future__ import annotations

import copy
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.llm_protocol import ActionPlan, CapabilityAction
from server.engine.session import GameSession, SessionError


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "midnight_archive.yaml"


def observe_plan(revision: int) -> ActionPlan:
    return ActionPlan(
        plan_id="plan_before_undo",
        perception_revision=revision,
        player_text="我再看看画像",
        interpretation="观察全家画像",
        goal="inspect_portrait",
        steps=(CapabilityAction(
            capability="intent",
            action="observe",
            arguments={"objects": ["family_portrait"]},
            purpose="观察画像",
        ),),
    )


class SessionTimelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def observe_portrait(self) -> None:
        self.session.resolve(intent_id="observe", objects=["family_portrait"])

    def test_undo_restores_full_snapshot_and_invalidates_old_authority(self) -> None:
        root_id = self.session.current_checkpoint_id
        self.session.temporaries.add(
            99,
            [("flags.old_badge_hint_seen", False, True)],
        )
        self.observe_portrait()
        first_id = self.session.current_checkpoint_id
        first = self.session.get_checkpoint(first_id)
        state_after_first = copy.deepcopy(self.session.state)

        self.session.rng.random()
        self.observe_portrait()
        second_id = self.session.current_checkpoint_id
        second_state = copy.deepcopy(self.session.state)
        self.assertEqual(self.session.state_revision, 2)

        plan = observe_plan(self.session.state_revision)
        validation = self.session.validate_plan(plan)
        self.assertTrue(validation.can_execute)
        quote = self.session.quote("negotiate", ["heir"])
        self.session.pending_clarification = {"question": "继续吗？"}

        restored = self.session.undo()

        self.assertEqual(restored.source_checkpoint_id, second_id)
        self.assertEqual(restored.restored_checkpoint_id, first_id)
        self.assertEqual(restored.branch_id, "branch_1")
        self.assertEqual(self.session.state_revision, 3)
        self.assertEqual(self.session.state, state_after_first)
        self.assertEqual(self.session.turn_no, first.turn_no)
        self.assertEqual(self.session.consumed, set(first.consumed))
        self.assertEqual(self.session.temporaries.snapshot(), first.temporary_effects)
        self.assertEqual(self.session.recent_events, first.recent_events)
        self.assertEqual(self.session.rng.getstate(), first.rng_state)
        self.assertIsNone(self.session.pending_clarification)

        with self.assertRaisesRegex(SessionError, "validation is stale"):
            self.session.resolve_plan(plan, validation)
        with self.assertRaisesRegex(SessionError, "unknown or expired quote"):
            self.session.resolve(quote_id=quote["quote_id"])

        # Restoring an abandoned head does not erase either line of history.
        restored_again = self.session.restore_checkpoint(second_id)
        self.assertEqual(restored_again.branch_id, "branch_2")
        self.assertEqual(self.session.state_revision, 4)
        self.assertEqual(self.session.state, second_state)
        self.assertEqual(
            {branch.branch_id: branch.head_checkpoint_id for branch in self.session.branches()},
            {"main": second_id, "branch_1": first_id, "branch_2": second_id},
        )
        self.assertEqual(root_id, "cp_0")

    def test_commit_after_undo_gets_the_restored_node_as_parent(self) -> None:
        self.observe_portrait()
        fork_id = self.session.current_checkpoint_id
        self.observe_portrait()
        abandoned_id = self.session.current_checkpoint_id

        self.session.undo()
        revision_after_undo = self.session.state_revision
        self.observe_portrait()
        branch_head = self.session.current_checkpoint_id

        self.assertGreater(self.session.state_revision, revision_after_undo)
        self.assertEqual(self.session.current_branch_id, "branch_1")
        self.assertEqual(self.session.get_checkpoint(branch_head).parent_id, fork_id)
        self.assertEqual(
            [checkpoint.checkpoint_id for checkpoint in self.session.checkpoint_history()],
            ["cp_0", fork_id, branch_head],
        )
        branches = {branch.branch_id: branch for branch in self.session.branches()}
        self.assertEqual(branches["main"].head_checkpoint_id, abandoned_id)
        self.assertEqual(branches["branch_1"].head_checkpoint_id, branch_head)

    def test_root_cannot_be_undone_and_checkpoint_reads_are_isolated(self) -> None:
        checkpoint = self.session.get_checkpoint(self.session.current_checkpoint_id)
        checkpoint.state["world"]["time_left"] = -100

        self.assertNotEqual(self.session.state["world"]["time_left"], -100)
        self.assertNotEqual(
            self.session.get_checkpoint(self.session.current_checkpoint_id).state["world"]["time_left"],
            -100,
        )
        with self.assertRaisesRegex(SessionError, "故事起点"):
            self.session.undo()


if __name__ == "__main__":
    unittest.main()
