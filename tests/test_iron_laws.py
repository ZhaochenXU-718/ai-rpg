from __future__ import annotations

import copy
import unittest
from pathlib import Path

from server.engine.content import Story
from server.engine.iron_laws import (
    secret_id_for_character,
    validate_fact_extraction,
)
from server.engine.llm_protocol import (
    CharacterMoveFact,
    FactExtraction,
    ItemPlacement,
    ItemTransferFact,
    SecretDisclosureFact,
)
from server.engine.session import GameSession


ROOT = Path(__file__).resolve().parent.parent
OPEN_FIXTURE = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"
ROOFTOP = ROOT / "content" / "rooftop_supper.yaml"


class IronLawValidationTest(unittest.TestCase):
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
            turn_no=self.session.turn_no + 1,
        )

    def test_adjacent_player_move_becomes_a_validated_batch(self) -> None:
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
            "iron.route_not_adjacent",
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
            "iron.item_not_co_present",
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
            "iron.item_custody_stale",
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

    def test_secret_can_only_be_disclosed_by_its_owner_to_present_audience(self) -> None:
        session = GameSession(Story.load(ROOFTOP), log_dir=None)
        narrative = "陈阿姨说，女儿临时加班让她有些失落。"
        extraction = FactExtraction(
            state_revision=0,
            facts=(SecretDisclosureFact(
                secret_id=secret_id_for_character("aunt_chen"),
                owner_id="aunt_chen",
                disclosed_by_id="player",
                audience_ids=("player",),
                summary="女儿临时加班让陈阿姨有些失落",
                evidence=narrative,
            ),),
        )
        validation = validate_fact_extraction(
            session.story,
            session.state,
            extraction,
            player_text="我问她是不是有心事。",
            narrative=narrative,
            references=("aunt_chen",),
            perception=session.perception(),
            turn_no=1,
        )
        self.assertFalse(validation.accepted)
        self.assertEqual(
            validation.violations[0].code,
            "iron.secret_discloser_unauthorized",
        )

        valid_extraction = FactExtraction(
            state_revision=0,
            facts=(SecretDisclosureFact(
                secret_id=secret_id_for_character("aunt_chen"),
                owner_id="aunt_chen",
                disclosed_by_id="aunt_chen",
                audience_ids=("player",),
                summary="女儿临时加班让陈阿姨有些失落",
                evidence=narrative,
            ),),
        )
        valid = validate_fact_extraction(
            session.story,
            session.state,
            valid_extraction,
            player_text="我问她是不是有心事。",
            narrative=narrative,
            references=("aunt_chen",),
            perception=session.perception(),
            turn_no=1,
        )
        self.assertTrue(valid.accepted)
        session.commit_fact_batch(valid.batch)
        disclosure = session.state["disclosures"]["secret_aunt_chen"]
        self.assertIn("player", disclosure["audience_ids"])
        self.assertIn("女儿临时加班", "".join(session.perception().known_facts))


if __name__ == "__main__":
    unittest.main()
