from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from server.engine.llm_protocol import (
    PROTOCOL_VERSION,
    CommittedChange,
    CommittedDirectorBeat,
    CommittedTurn,
    CommitmentFact,
    DirectorBeat,
    DirectorBeatKind,
    DirectorPlan,
    EntityKind,
    FactAuthority,
    FactBatch,
    FactExtraction,
    IronLawDomain,
    IronLawViolation,
    NpcTurn,
    PerceivedEntity,
    PerceptionAudience,
    PerceptionSnapshot,
    SuggestedAction,
    SuggestedActionSet,
    director_plan_json_schema,
    fact_batch_json_schema,
    fact_extraction_json_schema,
    new_protocol_id,
    npc_turn_json_schema,
    suggested_action_json_schema,
)


def player_perception() -> PerceptionSnapshot:
    return PerceptionSnapshot(
        audience=PerceptionAudience.PLAYER,
        subject_id="player",
        story_id="story_demo",
        session_id="session_demo",
        turn_no=3,
        state_revision=3,
        location_id="courtyard",
        location_name="公共院子",
        current_goal="把长桌遮好",
        visible_entities=(PerceivedEntity(
            entity_id="keeper",
            label="周师傅",
            kind=EntityKind.CHARACTER,
            actionable=True,
        ),),
        known_facts=("长桌怕雨",),
    )


class NarrativeFirstProtocolTest(unittest.TestCase):
    def test_fact_batch_round_trip_and_schema(self) -> None:
        extracted = CommitmentFact(
            commitment_id="promise_canvas",
            promisor_id="keeper",
            promisee_id="player",
            description="明天交付防雨布",
            related_item_id="rain_canvas",
            due="明天",
            evidence="周师傅答应明天交付防雨布",
        )
        batch = FactBatch(
            state_revision=3,
            player_text="我问周师傅防雨布能不能借用。",
            narrative="你把用途和归还时间都说清楚了。",
            references=("keeper",),
            extracted_facts=(extracted,),
        )
        payload = batch.to_dict()
        self.assertEqual(FactBatch.from_dict(payload), batch)
        self.assertEqual(json.loads(json.dumps(payload, ensure_ascii=False)), payload)
        self.assertEqual(payload["protocol_version"], PROTOCOL_VERSION)
        schema = fact_batch_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("state_changes", schema["properties"])

        extraction = FactExtraction(state_revision=3, facts=(extracted,))
        self.assertEqual(
            FactExtraction.from_dict(extraction.to_dict()), extraction
        )
        self.assertIn("facts", fact_extraction_json_schema()["properties"])
        untrusted_patch = extraction.to_dict()
        untrusted_patch["state_changes"] = {"positions.player": "elsewhere"}
        with self.assertRaises(ValidationError):
            FactExtraction.from_dict(untrusted_patch)

    def test_perception_is_subject_scoped_and_has_no_capability_menu(self) -> None:
        perception = player_perception()
        payload = perception.to_dict()
        self.assertEqual(payload["audience"], "player")
        self.assertEqual(payload["subject_id"], "player")
        self.assertNotIn("available_intents", payload)
        self.assertNotIn("capability_tools", payload)
        self.assertNotIn("secret", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(PerceptionSnapshot.from_dict(payload), perception)

        payload["visible_entities"].append(payload["visible_entities"][0])
        with self.assertRaises(ValidationError):
            PerceptionSnapshot.from_dict(payload)

    def test_suggestion_is_editable_prose_without_plan_or_validation(self) -> None:
        action = SuggestedAction(
            suggestion_id="suggestion_ask",
            perception_revision=3,
            title="说明用途",
            action_text="我先说明防雨布会铺在哪里、什么时候归还。",
            focus="social",
            rationale="给对方足够信息，但不预设对方答应。",
            expected_iron_law_touches=("人物承诺",),
        )
        action_set = SuggestedActionSet(
            suggestion_set_id="suggestions_demo",
            perception_revision=3,
            actions=(action,),
        )
        payload = action_set.to_dict()
        self.assertNotIn("plan", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("validation", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(SuggestedActionSet.from_dict(payload), action_set)
        self.assertIn("action_text", suggested_action_json_schema()["properties"])

        with self.assertRaises(ValidationError):
            SuggestedActionSet(
                suggestion_set_id="suggestions_bad",
                perception_revision=4,
                actions=(action,),
            )

    def test_iron_law_violation_is_structured_for_phase_two(self) -> None:
        violation = IronLawViolation(
            code="iron.item_owner_conflict",
            domain=IronLawDomain.ITEM_CUSTODY,
            message="防雨布仍由周师傅携带。",
            path="item_locations.rain_canvas",
            evidence="叙事声称玩家已经拿到防雨布。",
        )
        self.assertEqual(
            IronLawViolation.from_dict(violation.to_dict()), violation
        )

    def test_director_plan_contains_only_revalidatable_proposals(self) -> None:
        beat = DirectorBeat(
            beat_id="beat_keeper_reacts",
            state_revision=4,
            kind=DirectorBeatKind.REACT,
            actor_id="keeper",
            target_location_id="courtyard",
            target_ids=("player",),
            summary="周师傅看了看天色，没有立刻答应。",
            motivation="先确认借用条件。",
        )
        plan = DirectorPlan(state_revision=4, beats=(beat,))
        self.assertEqual(DirectorPlan.from_dict(plan.to_dict()), plan)
        self.assertIn("beats", director_plan_json_schema()["properties"])

        invalid = plan.to_dict()
        invalid["beats"].append(invalid["beats"][0])
        with self.assertRaises(ValidationError):
            DirectorPlan.from_dict(invalid)

        wrong_revision = plan.to_dict()
        wrong_revision["beats"][0]["state_revision"] = 3
        with self.assertRaises(ValidationError):
            DirectorPlan.from_dict(wrong_revision)

    def test_npc_turn_reserves_agent_output_without_state_authority(self) -> None:
        turn = NpcTurn(
            state_revision=4,
            actor_id="keeper",
            utterance="先说好什么时候还。",
            target_ids=("player",),
            proposed_facts=("周师傅询问归还时间",),
        )
        self.assertEqual(NpcTurn.from_dict(turn.to_dict()), turn)
        self.assertIn("proposed_facts", npc_turn_json_schema()["properties"])
        with self.assertRaises(ValidationError):
            NpcTurn(state_revision=4, actor_id="keeper")

    def test_committed_turn_no_longer_depends_on_plan_or_validation_ids(self) -> None:
        change = CommittedChange(
            path="positions.keeper",
            previous="workshop",
            new="courtyard",
            authority=FactAuthority.IRON_LAW,
            source="director.beat_keeper_enters",
            reason="通过位置和相邻路线复验",
        )
        committed = CommittedTurn(
            batch_id="batch_demo",
            turn_no=4,
            state_revision_before=3,
            state_revision_after=4,
            scene_before="courtyard",
            scene_after="courtyard",
            narrative="你站在长桌旁等雨势过去。",
            committed_changes=(change,),
            director_beats=(CommittedDirectorBeat(
                beat_id="beat_keeper_enters",
                validation_id="beatval_demo",
                kind=DirectorBeatKind.ENTER_SCENE,
                actor_id="keeper",
                target_location_id="courtyard",
                narrative_hint="周师傅从修理铺门口走了过来。",
                committed_changes=(change,),
            ),),
        )
        payload = committed.to_dict()
        self.assertNotIn("plan_id", payload)
        self.assertNotIn("validation_id", payload)
        self.assertEqual(CommittedTurn.from_dict(payload), committed)

        payload["state_revision_after"] = 3
        with self.assertRaises(ValidationError):
            CommittedTurn.from_dict(payload)

    def test_protocol_ids_reject_invalid_prefixes(self) -> None:
        with self.assertRaises(ValidationError):
            new_protocol_id("Invalid Prefix")


if __name__ == "__main__":
    unittest.main()
