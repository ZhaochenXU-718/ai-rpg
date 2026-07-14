"""Static capability router: the tool server between ActionPlan and engine.

v0.1 is deliberately a hand-written table, not a plugin system (dev-notes
2026-07-10): tools are derived from story content (intents, exits), every
plan step and proposed change is validated here, and only the resulting
execution payload reaches the deterministic resolver.  The LLM never writes
state; module boundaries get decided after real traces exist.

v0.1 honoured subset of the protocol (also documented in
docs/llm-action-plan-protocol.md section 10):

- exactly one turn-consuming ``intent.*`` step per plan (one plan = one turn
  = one intent cost); extra steps are rejected as retryable issues
- SOFT_STATE proposals with SET/INCREMENT pass through resolution_limits
- PRESENTATION proposals are narration-only and never touch state
- MECHANICAL/CANON proposals are engine/storylet-derived; LLM copies are
  dropped with an adjustment issue
- APPEND/REMOVE operations and duration_turns are not honoured yet
"""

from __future__ import annotations

from typing import Any

from .content import Story
from .director import suggested_intents
from .limits import clamp_generic_patch
from .llm_protocol import (
    ActionPlan,
    AuthorityLevel,
    CapabilityTool,
    ChangeOperation,
    CommittedChange,
    CommittedOutcome,
    IssueSeverity,
    StateChangeProposal,
    ValidationIssue,
    ValidationResult,
    new_protocol_id,
)
from .quote import default_proposal
from .resolver import TurnResult, _has_matching_action_storylet, validate_action
from .social import REQUEST_ITEM_TOOL_ID, request_item_tool, resolve_request_item

INTENT_CAPABILITY = "intent"


def available_tools(story: Story, state: dict[str, Any]) -> tuple[CapabilityTool, ...]:
    """Capability tools the player (and thus the LLM) may use right now."""
    tools = []
    for intent_id in suggested_intents(story, state):
        intent = story.intent(intent_id)
        if intent.get("engine_action") == "move":
            allowed = (AuthorityLevel.PRESENTATION, AuthorityLevel.MECHANICAL)
            targets = sorted(story.exit_labels(state))
        else:
            allowed = (AuthorityLevel.PRESENTATION, AuthorityLevel.SOFT_STATE)
            targets = sorted(story.actionable_objects(state))
        tools.append(CapabilityTool(
            capability=INTENT_CAPABILITY,
            action=intent_id,
            description=f"{intent.get('label', intent_id)}：{intent.get('description', '')}",
            arguments_schema={
                "type": "object",
                "properties": {
                    "objects": {
                        "type": "array",
                        "items": {"type": "string", "enum": targets},
                    },
                },
            },
            allowed_authority_levels=allowed,
        ))
    social_tool = request_item_tool(story, state)
    if social_tool is not None:
        tools.append(social_tool)
    return tuple(tools)


def _issue(
    code: str,
    severity: IssueSeverity,
    message: str,
    *,
    retryable: bool,
    step_index: int | None = None,
    path: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=severity,
        message=message,
        retryable=retryable,
        step_index=step_index,
        path=path,
    )


def _soft_changes_to_patch(
    proposals: list[tuple[int | None, StateChangeProposal]],
    issues: list[ValidationIssue],
) -> dict[str, Any]:
    """Convert honoured SOFT_STATE proposals into a generic patch mapping."""
    patch: dict[str, Any] = {}
    for _, change in proposals:
        if change.operation == ChangeOperation.INCREMENT:
            patch[change.path] = patch.get(change.path, 0) + change.value
        elif change.operation == ChangeOperation.SET:
            patch[change.path] = change.value
        else:
            issues.append(_issue(
                "change.operation_unsupported",
                IssueSeverity.ADJUSTMENT,
                f"v0.1 不支持 {change.operation.value} 操作，已忽略 '{change.path}'。",
                retryable=True,
                path=change.path,
            ))
    return patch


def validate_plan(
    story: Story,
    state: dict[str, Any],
    consumed: set[str],
    plan: ActionPlan,
    *,
    state_revision: int,
) -> tuple[ValidationResult, dict[str, Any]]:
    """Adjudicate an ActionPlan; return the decision and an execution payload.

    The payload (intent_id / objects / generic_patch) is what the session
    hands to the deterministic resolver on commit.  Nothing here mutates
    live state.
    """
    issues: list[ValidationIssue] = []
    payload: dict[str, Any] = {}
    missing: list[str] = []

    if plan.needs_clarification:
        missing.append(plan.clarification_question or "需要玩家澄清")
        result = ValidationResult(
            validation_id=new_protocol_id("val"),
            plan_id=plan.plan_id,
            state_revision=state_revision,
            can_execute=False,
            can_replan=True,
            missing_information=tuple(missing),
        )
        return result, payload

    if plan.perception_revision != state_revision:
        issues.append(_issue(
            "plan.stale_perception",
            IssueSeverity.ERROR,
            f"计划基于修订 {plan.perception_revision}，世界已在修订 {state_revision}。",
            retryable=True,
        ))

    tool_ids = {tool.tool_id for tool in available_tools(story, state)}
    accepted_tool_steps: list[int] = []
    for index, step in enumerate(plan.steps):
        if step.tool_id not in tool_ids:
            issues.append(_issue(
                "capability.unavailable",
                IssueSeverity.ERROR,
                f"当前无法使用工具 '{step.tool_id}'。",
                retryable=True,
                step_index=index,
            ))
            continue
        accepted_tool_steps.append(index)

    if len(plan.steps) != 1:
        issues.append(_issue(
            "plan.single_capability_step_required",
            IssueSeverity.ERROR,
            "一个行动提案只支持恰好一个消耗回合的能力步骤，请拆分或合并方案。",
            retryable=True,
        ))
    selected_step_index = (
        accepted_tool_steps[0]
        if len(plan.steps) == 1 and len(accepted_tool_steps) == 1
        else None
    )
    selected_step = (
        plan.steps[selected_step_index]
        if selected_step_index is not None
        else None
    )
    is_intent_step = bool(
        selected_step is not None
        and selected_step.capability == INTENT_CAPABILITY
    )

    # Sort proposed changes by authority; only soft state may pass through.
    soft: list[tuple[int | None, StateChangeProposal]] = []
    for change in plan.proposed_changes:
        if change.authority == AuthorityLevel.SOFT_STATE:
            if change.duration_turns is not None:
                issues.append(_issue(
                    "change.duration_unsupported",
                    IssueSeverity.ADJUSTMENT,
                    f"v0.1 兜底判定不支持临时效果，'{change.path}' 将按永久变化处理。",
                    retryable=False,
                    path=change.path,
                ))
            soft.append((change.step_index, change))
        elif change.authority == AuthorityLevel.PRESENTATION:
            issues.append(_issue(
                "change.presentation_is_narration",
                IssueSeverity.ADJUSTMENT,
                f"'{change.path}' 是表现层内容，不进入世界状态。",
                retryable=False,
                path=change.path,
            ))
        else:
            issues.append(_issue(
                "change.authority_reserved",
                IssueSeverity.ADJUSTMENT,
                f"'{change.path}' 属于 {change.authority.value} 层，只能由引擎或作者事件卡产生，已忽略。",
                retryable=False,
                path=change.path,
            ))

    proposed_patch = _soft_changes_to_patch(soft, issues)
    # A missing LLM proposal must not make the natural-language path weaker
    # than the equivalent structured/menu action.  Content already declares a
    # deterministic fallback per intent; use it only when the model proposed
    # no changes at all (never to replace an explicit but invalid proposal).
    if not plan.proposed_changes and is_intent_step and selected_step is not None:
        step = selected_step
        objects = [str(obj) for obj in (step.arguments.get("objects") or [])]
        fallback_patch = default_proposal(story, state, step.action, objects)
        if fallback_patch:
            proposed_patch = fallback_patch
            issues.append(_issue(
                "change.fallback_proposal_applied",
                IssueSeverity.ADJUSTMENT,
                "模型没有提议软状态变化，已采用故事声明的同意图兜底效果。",
                retryable=False,
                step_index=selected_step_index,
            ))
    if selected_step is not None and not is_intent_step and proposed_patch:
        proposed_patch = {}
        issues.append(_issue(
            "change.capability_owned_effects",
            IssueSeverity.ADJUSTMENT,
            "该能力的状态效果由确定性模块裁决，模型提议的附加变化已忽略。",
            retryable=False,
            step_index=selected_step_index,
        ))
    accepted_patch, clamp_notes = clamp_generic_patch(
        proposed_patch, state, story.resolution_limits
    )
    for note in clamp_notes:
        issues.append(_issue(
            "change.clamped",
            IssueSeverity.ADJUSTMENT,
            note,
            retryable=False,
        ))

    has_error = any(issue.severity == IssueSeverity.ERROR for issue in issues)
    accepted_steps: tuple[int, ...] = ()
    accepted_changes: list[StateChangeProposal] = []
    adjusted_costs: list[StateChangeProposal] = []

    if not has_error and selected_step is not None and selected_step_index is not None:
        step_index = selected_step_index
        step = selected_step
        if step.tool_id == REQUEST_ITEM_TOOL_ID:
            resolution, capability_errors = resolve_request_item(
                story, state, step.arguments
            )
            for message in capability_errors:
                issues.append(_issue(
                    "social.request_item_invalid",
                    IssueSeverity.ERROR,
                    message,
                    retryable=True,
                    step_index=step_index,
                ))
            has_error = bool(capability_errors)
            if not has_error and resolution is not None:
                accepted_steps = (step_index,)
                payload = {
                    "intent_id": resolution.capability_id,
                    "objects": list(resolution.objects),
                    "generic_patch": {},
                    "allow_unauthored": False,
                    "capability_resolution": resolution,
                    "confirmation_required": resolution.confirmation_required,
                }
                for mutation in resolution.mutations:
                    accepted_changes.append(StateChangeProposal(
                        path=mutation.path,
                        operation=(
                            ChangeOperation.INCREMENT
                            if mutation.operation == "increment"
                            else ChangeOperation.SET
                        ),
                        value=mutation.value,
                        authority=AuthorityLevel.MECHANICAL,
                        reason="social.request_item 确定性裁决",
                        step_index=step_index,
                    ))
        elif is_intent_step:
            intent_id = step.action
            objects = [str(obj) for obj in (step.arguments.get("objects") or [])]
            # LLM-path policy: a well-formed attempt with no authored interaction
            # remains executable through the v0.1 compatibility path.
            action_errors = validate_action(
                story, state, intent_id, objects, accepted_patch, consumed,
                allow_unauthored=True,
            )
            for message in action_errors:
                issues.append(_issue(
                    "action.invalid",
                    IssueSeverity.ERROR,
                    message,
                    retryable=True,
                    step_index=step_index,
                ))
            has_error = bool(action_errors)
            if not has_error:
                requires_authored_match = bool(
                    story.intent(intent_id).get("requires_storylet_match")
                ) or intent_id == "custom"
                unauthored = requires_authored_match and not _has_matching_action_storylet(
                    story, state, set(consumed), intent_id, objects, accepted_patch
                )
                if unauthored:
                    issues.append(_issue(
                        "action.unauthored_attempt",
                        IssueSeverity.ADJUSTMENT,
                        "这不是一条已知可行的路径；v0.1 兼容层只能提交受限结果。",
                        retryable=False,
                        step_index=step_index,
                    ))
                accepted_steps = (step_index,)
                payload = {
                    "intent_id": intent_id,
                    "objects": objects,
                    "generic_patch": accepted_patch,
                    "allow_unauthored": unauthored,
                }
                for path, value in accepted_patch.items():
                    accepted_changes.append(StateChangeProposal(
                        path=path,
                        operation=(
                            ChangeOperation.INCREMENT
                            if isinstance(value, (int, float)) and not isinstance(value, bool)
                            else ChangeOperation.SET
                        ),
                        value=value,
                        authority=AuthorityLevel.SOFT_STATE,
                        reason="白名单裁剪后接受的软状态提议",
                        step_index=step_index,
                    ))
                for key, value in (story.intent(intent_id).get("typical_cost") or {}).items():
                    path = str(key) if "." in str(key) else f"world.{key}"
                    adjusted_costs.append(StateChangeProposal(
                        path=path,
                        operation=ChangeOperation.INCREMENT,
                        value=value,
                        authority=AuthorityLevel.MECHANICAL,
                        reason="意图固定代价",
                        step_index=step_index,
                    ))
        else:
            issues.append(_issue(
                "capability.unsupported",
                IssueSeverity.ERROR,
                f"能力 '{step.tool_id}' 尚未接入执行器。",
                retryable=True,
                step_index=step_index,
            ))
            has_error = True

    result = ValidationResult(
        validation_id=new_protocol_id("val"),
        plan_id=plan.plan_id,
        state_revision=state_revision,
        can_execute=not has_error,
        can_replan=any(issue.retryable for issue in issues),
        accepted_step_indices=accepted_steps,
        accepted_changes=tuple(accepted_changes),
        adjusted_costs=tuple(adjusted_costs),
        issues=tuple(issues),
        missing_information=tuple(missing),
    )
    return result, payload


def classify_path_authority(story: Story, path: str) -> AuthorityLevel:
    """Authority level of a committed state path, derived from the contract."""
    root = path.split(".", 1)[0]
    if root in ("positions", "item_locations"):
        return AuthorityLevel.MECHANICAL
    limits = story.resolution_limits or {}
    if path in (limits.get("patchable") or {}):
        return AuthorityLevel.SOFT_STATE
    return AuthorityLevel.CANON


def build_committed_outcome(
    story: Story,
    plan: ActionPlan,
    validation: ValidationResult,
    result: TurnResult,
    *,
    revision_before: int,
    revision_after: int,
) -> CommittedOutcome:
    """Translate a TurnResult into the protocol's only trustworthy payload."""
    committed = tuple(
        CommittedChange(
            path=path,
            previous=previous,
            new=new,
            authority=classify_path_authority(story, path),
            source=(
                result.change_sources[index]
                if index < len(result.change_sources)
                else "resolver"
            ),
        )
        for index, (path, previous, new) in enumerate(result.changes)
        if previous != new
    )
    sources: list[str] = []
    for source in (
        [change.source for change in committed]
        + list(result.action_effect_sources)
        + [f"storylet.{storylet_id}" for storylet_id in result.fired]
        + [f"world_rule.{rule_id}" for rule_id in result.world_rules]
        + [f"director.{beat.beat_id}" for beat in result.director_beats]
    ):
        if source not in sources:
            sources.append(source)

    action_sources = set(result.action_effect_sources)
    matched_storylet = bool(result.matched_action_storylets)
    if result.primary_goal_status_override is not None:
        primary_goal_status = result.primary_goal_status_override
    elif not action_sources:
        primary_goal_status = "not_achieved"
    elif matched_storylet:
        primary_goal_status = {
            "success": "achieved",
            "partial_success": "partial",
            "fail_forward": "not_achieved",
        }.get(result.result_tier, "unverified")
    elif "engine.move" in action_sources:
        primary_goal_status = "achieved"
    else:
        # A generic soft patch can be meaningful, but v0.1 has no structured
        # success criterion that proves it satisfies the player's main goal.
        # Keep it out of the effective-action numerator until a capability
        # module supplies an explicit postcondition.
        primary_goal_status = "unverified"

    return CommittedOutcome(
        outcome_id=new_protocol_id("out"),
        plan_id=plan.plan_id,
        validation_id=validation.validation_id,
        turn_no=result.turn_no,
        state_revision_before=revision_before,
        state_revision_after=revision_after,
        scene_before=result.scene_before,
        scene_after=result.scene_after,
        result_tier=result.result_tier,
        primary_goal_status=primary_goal_status,
        cost_only=not action_sources,
        resolution_sources=tuple(sources),
        accepted_step_indices=validation.accepted_step_indices,
        committed_changes=committed,
        director_beats=tuple(result.director_beats),
        fired_storylets=tuple(result.fired),
        world_events=tuple(
            list(result.world_rules)
            + [f"director.{beat.beat_id}" for beat in result.director_beats]
        ),
        new_facts=tuple(result.new_facts),
        ending=result.ending,
    )
