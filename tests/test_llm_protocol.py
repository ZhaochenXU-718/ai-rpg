from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from server.engine.llm_protocol import (
    PROTOCOL_VERSION,
    ActionPlan,
    AuthorityLevel,
    CapabilityAction,
    CapabilityTool,
    ChangeOperation,
    CommittedChange,
    CommittedDirectorBeat,
    CommittedOutcome,
    DirectorBeat,
    DirectorBeatKind,
    EntityKind,
    IssueSeverity,
    PerceivedEntity,
    PlayerPerception,
    RiskLikelihood,
    RiskProposal,
    StateChangeProposal,
    SuggestedAction,
    SuggestedActionDraft,
    SuggestedActionSet,
    ValidationIssue,
    ValidationResult,
    action_plan_json_schema,
    new_protocol_id,
)


class LLMProtocolTests(unittest.TestCase):
    def make_change(self) -> StateChangeProposal:
        return StateChangeProposal(
            path="guard.attention",
            operation=ChangeOperation.SET,
            value="east_window",
            authority=AuthorityLevel.SOFT_STATE,
            reason="闪电反光把守卫的注意力引向东窗",
            step_index=0,
            duration_turns=1,
        )

    def make_plan(self) -> ActionPlan:
        return ActionPlan(
            plan_id="plan_demo",
            perception_revision=6,
            player_text="用酒盘反光把守卫引向东窗",
            interpretation="玩家想制造一次短暂的视觉误导",
            goal="redirect_guard_attention",
            intent_id="create_distraction",
            references=("wine_tray", "east_window", "guard"),
            steps=(
                CapabilityAction(
                    capability="creative_resolution",
                    action="create_visual_distraction",
                    arguments={
                        "source": "wine_tray",
                        "target": "guard",
                        "direction": "east_window",
                    },
                    purpose="让守卫暂时看向东窗",
                ),
            ),
            proposed_changes=(self.make_change(),),
            risks=(
                RiskProposal(
                    description="守卫可能发现反光来自玩家",
                    likelihood=RiskLikelihood.POSSIBLE,
                    changes=(
                        StateChangeProposal(
                            path="guard.alertness",
                            operation=ChangeOperation.INCREMENT,
                            value=1,
                            authority=AuthorityLevel.MECHANICAL,
                            reason="异常反光可能提高警觉",
                            step_index=0,
                        ),
                    ),
                    mitigation="先移动到阴影角落可以降低暴露风险",
                ),
            ),
            assumptions=("酒盘能形成足够明显的反光",),
            confidence=0.82,
        )

    def test_action_plan_round_trip_and_json_schema(self) -> None:
        plan = self.make_plan()
        payload = plan.to_dict()

        self.assertEqual(ActionPlan.from_dict(payload), plan)
        self.assertEqual(json.loads(json.dumps(payload, ensure_ascii=False)), payload)
        self.assertEqual(payload["protocol_version"], PROTOCOL_VERSION)

        schema = action_plan_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("steps", schema["properties"])
        self.assertEqual(
            schema["properties"]["protocol_version"]["const"], PROTOCOL_VERSION
        )

    def test_clarification_plan_may_have_no_steps(self) -> None:
        plan = ActionPlan(
            plan_id="plan_question",
            perception_revision=1,
            player_text="用那个东西把他引开",
            interpretation="玩家没有明确物品和目标人物",
            goal="clarify_references",
            steps=(),
            needs_clarification=True,
            clarification_question="你想使用哪个物品、引开哪一位角色？",
            confidence=0.3,
        )

        self.assertTrue(plan.needs_clarification)
        with self.assertRaises(ValidationError):
            ActionPlan(
                plan_id="plan_invalid",
                perception_revision=1,
                player_text="继续",
                interpretation="缺少行动",
                goal="continue",
                steps=(),
            )

    def test_invalid_protocol_values_are_rejected(self) -> None:
        payload = self.make_plan().to_dict()
        payload["protocol_version"] = "9.9"
        with self.assertRaises(ValidationError):
            ActionPlan.from_dict(payload)

        payload = self.make_plan().to_dict()
        payload["confidence"] = 1.5
        with self.assertRaises(ValidationError):
            ActionPlan.from_dict(payload)

        with self.assertRaises(ValidationError):
            StateChangeProposal(
                path="guard.alertness",
                operation=ChangeOperation.INCREMENT,
                value="high",
                authority=AuthorityLevel.MECHANICAL,
                reason="错误的数值增量",
            )
        with self.assertRaises(ValidationError):
            new_protocol_id("Invalid Prefix")

    def test_player_perception_contains_only_explicit_public_information(self) -> None:
        perception = PlayerPerception(
            story_id="midnight_archive",
            session_id="session_demo",
            turn_no=3,
            state_revision=3,
            location_id="archive_door",
            location_name="档案室门口",
            current_goal="进入档案室",
            visible_entities=(
                PerceivedEntity(
                    entity_id="guard",
                    label="布兰特守卫",
                    kind=EntityKind.CHARACTER,
                    actionable=True,
                    public_state={"alertness": 2},
                ),
            ),
            inventory=(
                PerceivedEntity(
                    entity_id="servant_key",
                    label="仆役侧门钥匙",
                    kind=EntityKind.ITEM,
                    actionable=True,
                ),
            ),
            known_facts=("钥匙可以打开仆役侧门",),
            available_intents=("observe", "use", "custom"),
            capability_tools=(
                CapabilityTool(
                    capability="inventory",
                    action="use_item",
                    description="使用持有物作用于当前目标",
                    arguments_schema={
                        "type": "object",
                        "required": ["item", "target"],
                    },
                    allowed_authority_levels=(AuthorityLevel.MECHANICAL,),
                ),
            ),
            public_state={"world.time_left": 4},
        )

        payload = perception.to_dict()
        self.assertNotIn("secret", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(PlayerPerception.from_dict(payload), perception)

        payload["visible_entities"].append(payload["visible_entities"][0])
        with self.assertRaises(ValidationError):
            PlayerPerception.from_dict(payload)

    def test_validation_result_enforces_binding_decision(self) -> None:
        change = self.make_change()
        accepted = ValidationResult(
            validation_id="validation_demo",
            plan_id="plan_demo",
            state_revision=6,
            can_execute=True,
            can_replan=False,
            accepted_step_indices=(0,),
            accepted_changes=(change,),
        )
        self.assertTrue(accepted.can_execute)
        self.assertEqual(ValidationResult.from_dict(accepted.to_dict()), accepted)

        with self.assertRaises(ValidationError):
            ValidationResult(
                validation_id="validation_invalid",
                plan_id="plan_demo",
                state_revision=6,
                can_execute=True,
                can_replan=True,
                accepted_step_indices=(0,),
                issues=(
                    ValidationIssue(
                        code="object.missing",
                        severity=IssueSeverity.ERROR,
                        message="酒盘不在当前场景",
                        retryable=True,
                    ),
                ),
            )

    def test_suggested_action_binds_card_text_plan_and_validation(self) -> None:
        plan = self.make_plan()
        validation = ValidationResult(
            validation_id="validation_suggestion",
            plan_id=plan.plan_id,
            state_revision=plan.perception_revision,
            can_execute=True,
            can_replan=False,
            accepted_step_indices=(0,),
        )
        draft = SuggestedActionDraft(
            suggestion_id="suggestion_redirect",
            perception_revision=plan.perception_revision,
            title="借反光引开视线",
            action_text=plan.player_text,
            focus="creative",
            rationale="从环境入手，避免直接冲突。",
            plan=plan,
        )
        action = SuggestedAction(**draft.to_dict(), validation=validation)
        suggestion_set = SuggestedActionSet(
            suggestion_set_id="suggestions_demo",
            perception_revision=plan.perception_revision,
            actions=(action,),
        )

        self.assertEqual(
            SuggestedActionSet.from_dict(suggestion_set.to_dict()),
            suggestion_set,
        )
        invalid = draft.to_dict()
        invalid["action_text"] = "换一条没有重新规划的文字"
        with self.assertRaises(ValidationError):
            SuggestedActionDraft.from_dict(invalid)

    def test_director_beat_is_only_a_reference_to_an_actor(self) -> None:
        beat = DirectorBeat(
            beat_id="beat_guard_enters",
            state_revision=6,
            kind=DirectorBeatKind.ENTER_SCENE,
            actor_id="guard",
            target_location_id="archive_door",
            target_ids=("player",),
            summary="守卫循声来到档案室门口。",
            motivation="确认刚才的异常响动。",
        )

        self.assertEqual(DirectorBeat.from_dict(beat.to_dict()), beat)
        invalid = beat.to_dict()
        invalid["target_ids"] = ["player", "player"]
        with self.assertRaises(ValidationError):
            DirectorBeat.from_dict(invalid)

    def test_committed_outcome_round_trip(self) -> None:
        outcome = CommittedOutcome(
            outcome_id="outcome_demo",
            plan_id="plan_demo",
            validation_id="validation_demo",
            turn_no=7,
            state_revision_before=6,
            state_revision_after=7,
            scene_before="archive_door",
            scene_after="archive_door",
            result_tier="partial_success",
            primary_goal_status="partial",
            resolution_sources=("creative_resolution",),
            accepted_step_indices=(0,),
            committed_changes=(
                CommittedChange(
                    path="guard.attention",
                    previous="archive_door",
                    new="east_window",
                    authority=AuthorityLevel.SOFT_STATE,
                    source="creative_resolution",
                    reason="已验证的视觉误导",
                ),
            ),
            director_beats=(CommittedDirectorBeat(
                beat_id="beat_guard_reacts",
                validation_id="beat_validation_demo",
                kind=DirectorBeatKind.REACT,
                actor_id="guard",
                target_location_id="archive_door",
                narrative_hint="守卫侧过身，继续盯着东窗的动静。",
            ),),
            world_events=("guard_attention_redirected",),
            new_facts=("守卫会对东窗方向的异常反光作出反应",),
        )

        self.assertEqual(CommittedOutcome.from_dict(outcome.to_dict()), outcome)
        invalid = outcome.to_dict()
        invalid["state_revision_after"] = 5
        with self.assertRaises(ValidationError):
            CommittedOutcome.from_dict(invalid)


if __name__ == "__main__":
    unittest.main()
