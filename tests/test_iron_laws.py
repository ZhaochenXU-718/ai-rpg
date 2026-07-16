from __future__ import annotations

import copy
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.iron_laws import validate_fact_extraction
from server.engine.llm_protocol import (
    CharacterMoveFact,
    FactExtraction,
    ItemPlacement,
    ItemTransferFact,
)
from server.engine.session import GameSession


ROOT = Path(__file__).resolve().parent.parent
OPEN_FIXTURE = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


class PhysicalFactValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GameSession(Story.load(OPEN_FIXTURE), log_dir=None)

    def validate(self, narrative: str, *facts):
        return validate_fact_extraction(
            self.session.story,
            self.session.state,
            FactExtraction(
                state_revision=self.session.state_revision,
                facts=facts,
            ),
            player_text="测试行动",
            narrative=narrative,
            references=(),
            perception=self.session.perception(),
        )

    def test_known_player_destination_becomes_a_validated_batch(self) -> None:
        validation = self.validate(
            "你走进公共院子，停在长桌旁。",
            CharacterMoveFact(
                actor_id="player",
                destination_id="courtyard",
                evidence="你走进公共院子",
            ),
        )
        self.assertTrue(validation.accepted)
        self.assertEqual(
            validation.batch.state_changes,
            {"positions.player": "courtyard"},
        )
        result = self.session.commit_fact_batch(validation.batch)
        self.assertEqual(self.session.state["positions"]["player"], "courtyard")
        self.assertIsNotNone(result.committed_turn)
        self.assertEqual(result.committed_turn.state_revision_after, 1)

    def test_unknown_route_rejects_the_whole_extraction_without_mutation(self) -> None:
        before = copy.deepcopy(self.session.state)
        validation = self.validate(
            "你推门走进地下密室。",
            CharacterMoveFact(
                actor_id="player",
                destination_id="secret_vault",
                evidence="你推门走进地下密室",
            ),
        )
        self.assertFalse(validation.accepted)
        self.assertIn(
            "physical.location_unknown",
            {violation.code for violation in validation.violations},
        )
        self.assertEqual(self.session.state, before)

    def test_later_violation_discards_earlier_valid_fact_in_the_same_batch(self) -> None:
        before = copy.deepcopy(self.session.state)
        validation = self.validate(
            "你走进公共院子。周师傅隔空把旧防雨布递给了你。",
            CharacterMoveFact(
                actor_id="player",
                destination_id="courtyard",
                evidence="你走进公共院子",
            ),
            ItemTransferFact(
                item_id="rain_canvas",
                from_placement=ItemPlacement(
                    type="carried_by", id="keeper_zhou"
                ),
                to_placement=ItemPlacement(type="carried_by", id="player"),
                evidence="周师傅隔空把旧防雨布递给了你",
            ),
        )

        self.assertFalse(validation.accepted)
        self.assertIn(
            "physical.item_not_co_present",
            {violation.code for violation in validation.violations},
        )
        self.assertEqual(self.session.state, before)

    def test_item_transfer_requires_visible_item_and_exact_current_custody(self) -> None:
        visible = {
            entity.entity_id for entity in self.session.perception().visible_entities
        }
        self.assertIn("rain_canvas", visible)
        validation = self.validate(
            "周师傅把旧防雨布递给了你。",
            ItemTransferFact(
                item_id="rain_canvas",
                from_placement=ItemPlacement(type="board", id="workshop"),
                to_placement=ItemPlacement(type="carried_by", id="player"),
                evidence="周师傅把旧防雨布递给了你",
            ),
        )
        self.assertFalse(validation.accepted)
        self.assertEqual(
            validation.violations[0].code,
            "physical.item_custody_stale",
        )

    def test_valid_item_transfer_uses_co_presence_and_portability(self) -> None:
        validation = self.validate(
            "周师傅把旧防雨布递给了你。",
            ItemTransferFact(
                item_id="rain_canvas",
                from_placement=ItemPlacement(
                    type="carried_by", id="keeper_zhou"
                ),
                to_placement=ItemPlacement(type="carried_by", id="player"),
                evidence="周师傅把旧防雨布递给了你",
            ),
        )
        self.assertTrue(validation.accepted)
        self.assertEqual(
            validation.batch.state_changes["item_locations.rain_canvas"],
            {"type": "carried_by", "id": "player"},
        )

    def test_evidence_is_audit_metadata_not_a_commit_gate(self) -> None:
        validation = self.validate(
            "你从修理铺走进公共院子。",
            CharacterMoveFact(
                actor_id="player",
                destination_id="courtyard",
                evidence="模型没有逐字复制散文",
            ),
        )
        self.assertTrue(validation.accepted)
        self.assertEqual(
            validation.batch.state_changes,
            {"positions.player": "courtyard"},
        )


if __name__ == "__main__":
    unittest.main()
