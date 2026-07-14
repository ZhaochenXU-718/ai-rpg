"""Perception wall and static capability router tests.

The two invariants under test:
1. Nothing the player has not earned leaks through PlayerPerception or the
   quote card (the disclosure wall reads the story's `perception` block).
2. LLM ActionPlans only reach the resolver through router validation, and
   canon/mechanical proposals are stripped rather than committed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.content import Story
from server.engine.llm_protocol import (
    ActionPlan,
    AuthorityLevel,
    CapabilityAction,
    ChangeOperation,
    IssueSeverity,
    StateChangeProposal,
)
from server.engine.renderer import render_quote
from server.engine.session import GameSession

STORY_PATH = Path(__file__).resolve().parent.parent / "content" / "midnight_archive.yaml"


def make_plan(steps, changes=(), *, revision=0, plan_id="plan_test", **kwargs):
    return ActionPlan(
        plan_id=plan_id,
        perception_revision=revision,
        player_text="测试输入",
        interpretation="测试解释",
        goal="test_goal",
        steps=tuple(steps),
        proposed_changes=tuple(changes),
        **kwargs,
    )


def intent_step(intent_id, objects):
    return CapabilityAction(
        capability="intent",
        action=intent_id,
        arguments={"objects": list(objects)},
        purpose="测试步骤",
    )


class PerceptionWallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def _walk_to_archive_room_with_badge(self) -> None:
        session, story = self.session, self.story
        steps = [
            ("observe", ["family_portrait"]),
            ("negotiate", ["heir"]),
            ("sneak", ["side_stair"]),
            ("negotiate", ["maid", "medicine_box"]),
            ("sneak", ["back_stairs"]),
            ("use", ["servant_key", "service_door"]),
        ]
        for intent, objects in steps:
            if story.quote_required(intent):
                quote = session.quote(intent, objects)
                session.resolve(quote_id=quote["quote_id"])
            else:
                session.resolve(intent_id=intent, objects=objects)

    def test_perception_hides_undiscovered_and_absent_state(self) -> None:
        perception = self.session.perception()
        entity_ids = {entity.entity_id for entity in perception.visible_entities}
        self.assertNotIn("hidden_compartment", entity_ids)
        self.assertNotIn("locked_cabinet", entity_ids)  # other scene
        self.assertNotIn("guard", entity_ids)  # posted at archive_door
        self.assertIn("butler", entity_ids)
        self.assertNotIn("world.evidence_status", perception.public_state)
        self.assertIn("world.time_left", perception.public_state)
        flat = str(perception.to_dict())
        self.assertNotIn("truth_exposed", flat)
        self.assertNotIn("forgery_evidence", flat)

    def test_perception_tracks_inventory_and_exits(self) -> None:
        self._walk_to_archive_room_with_badge()
        perception = self.session.perception()
        inventory_ids = {entity.entity_id for entity in perception.inventory}
        self.assertIn("old_badge", inventory_ids)
        exit_ids = {
            entity.entity_id
            for entity in perception.visible_entities
            if entity.kind.value == "exit"
        }
        self.assertIn("archive_door", exit_ids)
        self.assertEqual(perception.state_revision, self.session.state_revision)

    def test_final_quote_card_discloses_no_oracle(self) -> None:
        """Regression: the pre-commit quote must not reveal the future."""
        self._walk_to_archive_room_with_badge()
        quote = self.session.quote("use", ["old_badge", "locked_cabinet"])

        # Internal preview keeps full fidelity for binding/logs...
        preview_paths = {path for path, _, _ in quote["expected_changes"]}
        self.assertIn("flags.hidden_compartment_found", preview_paths)
        self.assertEqual(quote["expected_ending"], "truth_exposed")

        # ...but the player-facing card only shows public state.
        disclosed_paths = {path for path, _, _ in quote["disclosed_changes"]}
        for path in disclosed_paths:
            self.assertFalse(path.startswith(("flags.", "item_locations.", "positions.")))
        card = render_quote(quote)
        self.assertNotIn("truth_exposed", card)
        self.assertNotIn("结局", card)
        self.assertNotIn("hidden_compartment", card)
        self.assertNotIn("item_locations", card)
        self.assertNotIn("线索", card)


class CapabilityRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def test_valid_plan_commits_through_resolver(self) -> None:
        plan = make_plan(
            [intent_step("negotiate", ["heir"])],
            [StateChangeProposal(
                path="heir.trust",
                operation=ChangeOperation.INCREMENT,
                value=1,
                authority=AuthorityLevel.SOFT_STATE,
                reason="真诚说明来意",
            )],
        )
        validation = self.session.validate_plan(plan)
        self.assertTrue(validation.can_execute)
        outcome = self.session.resolve_plan(plan, validation)
        self.assertEqual(self.session.state["characters"]["heir"]["trust"], 1)
        committed = {change.path: change for change in outcome.committed_changes}
        self.assertEqual(committed["heir.trust"].authority, AuthorityLevel.SOFT_STATE)
        self.assertEqual(committed["world.time_left"].authority, AuthorityLevel.CANON)
        self.assertEqual(self.session.state_revision, 1)
        self.assertEqual(outcome.state_revision_after, 1)

    def test_canon_proposal_is_stripped_not_committed(self) -> None:
        plan = make_plan(
            [intent_step("negotiate", ["heir"])],
            [StateChangeProposal(
                path="item_locations.forgery_evidence",
                operation=ChangeOperation.SET,
                value={"type": "carried_by", "id": "player"},
                authority=AuthorityLevel.CANON,
                reason="我说服薇拉直接把证据给我",
            )],
        )
        validation = self.session.validate_plan(plan)
        self.assertTrue(validation.can_execute)
        codes = {issue.code for issue in validation.issues}
        self.assertIn("change.authority_reserved", codes)
        self.session.resolve_plan(plan, validation)
        self.assertEqual(
            self.session.state["item_locations"]["forgery_evidence"],
            {"type": "container", "id": "hidden_compartment"},
        )

    def test_mislabelled_soft_proposal_on_protected_path_is_clamped_out(self) -> None:
        plan = make_plan(
            [intent_step("negotiate", ["heir"])],
            [StateChangeProposal(
                path="player.exposed",
                operation=ChangeOperation.SET,
                value=False,
                authority=AuthorityLevel.SOFT_STATE,
                reason="谎称自己没被看见",
            )],
        )
        validation = self.session.validate_plan(plan)
        self.assertTrue(validation.can_execute)
        self.assertIn("change.clamped", {issue.code for issue in validation.issues})
        self.assertEqual(validation.accepted_changes, ())

    def test_multi_intent_plan_is_rejected_as_retryable(self) -> None:
        plan = make_plan([
            intent_step("negotiate", ["heir"]),
            intent_step("observe", ["family_portrait"]),
        ])
        validation = self.session.validate_plan(plan)
        self.assertFalse(validation.can_execute)
        self.assertTrue(validation.can_replan)
        self.assertIn(
            "plan.single_capability_step_required",
            {issue.code for issue in validation.issues},
        )
        self.assertEqual(self.session.turn_no, 0)

    def test_unavailable_tool_and_stale_revision_are_rejected(self) -> None:
        plan = make_plan([intent_step("threaten", ["butler"])], revision=7)
        validation = self.session.validate_plan(plan)
        self.assertFalse(validation.can_execute)
        codes = {issue.code for issue in validation.issues}
        # threaten is not in great_hall's suggested intents, and the plan
        # was made against a stale perception revision.
        self.assertIn("capability.unavailable", codes)
        self.assertIn("plan.stale_perception", codes)
        self.assertTrue(all(
            issue.retryable for issue in validation.issues
            if issue.severity == IssueSeverity.ERROR
        ))

    def test_clarification_plan_is_not_executable(self) -> None:
        plan = ActionPlan(
            plan_id="plan_clarify",
            perception_revision=0,
            player_text="我想办法离开这里",
            interpretation="目标不明确",
            goal="unknown",
            steps=(),
            needs_clarification=True,
            clarification_question="你想通过侧梯离开，还是先在人群里再观察一下？",
        )
        validation = self.session.validate_plan(plan)
        self.assertFalse(validation.can_execute)
        self.assertTrue(validation.can_replan)
        self.assertTrue(validation.missing_information)


if __name__ == "__main__":
    unittest.main()
