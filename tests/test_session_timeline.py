from __future__ import annotations

import copy
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.llm_protocol import FactBatch
from server.engine.session import GameSession, SessionError
from server.engine.state import StatePathError


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "content" / "rooftop_supper.yaml"


class SessionTimelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GameSession(Story.load(STORY_PATH), log_dir=None)

    def commit(self, text: str, changes: dict | None = None):
        return self.session.commit_fact_batch(FactBatch(
            state_revision=self.session.state_revision,
            player_text=text,
            narrative=f"叙事：{text}",
            state_changes=changes or {},
        ))

    def test_undo_restores_state_and_creates_a_retained_branch(self) -> None:
        self.commit("先听完开场")
        first_checkpoint = self.session.current_checkpoint_id
        self.commit(
            "走向便利店",
            {"positions.player": "convenience_store"},
        )
        abandoned_head = self.session.current_checkpoint_id
        revision_before = self.session.state_revision

        restored = self.session.undo()

        self.assertEqual(restored.restored_checkpoint_id, first_checkpoint)
        self.assertEqual(self.session.state["positions"]["player"], "building_lobby")
        self.assertEqual(self.session.turn_no, 1)
        self.assertEqual(self.session.state_revision, revision_before + 1)
        branches = {branch.branch_id: branch for branch in self.session.branches()}
        self.assertEqual(branches["main"].head_checkpoint_id, abandoned_head)
        self.assertEqual(
            branches[restored.branch_id].head_checkpoint_id,
            first_checkpoint,
        )

    def test_commit_after_undo_uses_restored_node_as_parent(self) -> None:
        self.commit("第一步")
        restored_parent = self.session.current_checkpoint_id
        self.commit("第二步", {"positions.player": "convenience_store"})
        self.session.undo()

        self.commit("改走洗衣店", {"positions.player": "laundromat"})

        head = self.session.get_checkpoint(self.session.current_checkpoint_id)
        self.assertEqual(head.parent_id, restored_parent)
        self.assertEqual(self.session.state["positions"]["player"], "laundromat")

    def test_root_cannot_be_undone_and_checkpoint_reads_are_isolated(self) -> None:
        root_id = self.session.current_checkpoint_id
        checkpoint = self.session.get_checkpoint(root_id)
        checkpoint.state["positions"]["player"] = "tampered"
        self.assertEqual(
            self.session.get_checkpoint(root_id).state["positions"]["player"],
            "building_lobby",
        )
        with self.assertRaisesRegex(SessionError, "故事起点"):
            self.session.undo()

    def test_failed_fact_batch_is_atomic(self) -> None:
        state_before = copy.deepcopy(self.session.state)
        memory_before = copy.deepcopy(self.session.memory)
        checkpoint_before = self.session.current_checkpoint_id

        with self.assertRaisesRegex(StatePathError, "unknown state path root"):
            self.commit("坏批次", {"unknown.path": True})

        self.assertEqual(self.session.state, state_before)
        self.assertEqual(self.session.memory, memory_before)
        self.assertEqual(self.session.turn_no, 0)
        self.assertEqual(self.session.state_revision, 0)
        self.assertEqual(self.session.current_checkpoint_id, checkpoint_before)

    def test_stale_fact_batch_cannot_cross_a_new_revision(self) -> None:
        stale = FactBatch(
            state_revision=0,
            player_text="稍后再执行的旧批次",
            narrative="这段叙事基于旧状态生成。",
        )
        self.session.commit_narrative("先做别的。", "你先处理了另一件事。")

        with self.assertRaisesRegex(SessionError, "fact batch is stale"):
            self.session.commit_fact_batch(stale)

        self.assertEqual(self.session.turn_no, 1)
        self.assertEqual(self.session.state_revision, 1)


if __name__ == "__main__":
    unittest.main()
