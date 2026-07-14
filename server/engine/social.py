"""Deterministic social capabilities shared by stories.

The first vertical slice is ``social.request_item``. Content declares an
item-level request policy and purposes, while the module owns presence,
ownership, willingness and transfer semantics. Authors do not enumerate an
intent × character × item storylet for ordinary requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .capability_effects import CapabilityMutation, CapabilityResolution
from .conditions import check_condition_block
from .content import Story
from .llm_protocol import AuthorityLevel, CapabilityTool
from .state import get_value


REQUEST_ITEM_TOOL_ID = "social.request_item"


@dataclass(frozen=True)
class RequestItemOption:
    owner_id: str
    item_id: str
    purpose_id: str
    purpose_label: str
    policy: dict[str, Any]


def _purposes(policy: dict[str, Any]) -> list[tuple[str, str]]:
    raw = policy.get("purposes") or {}
    if isinstance(raw, dict):
        return [
            (str(purpose_id), str(label))
            for purpose_id, label in raw.items()
            if purpose_id and label
        ]
    if isinstance(raw, list):
        return [(str(purpose), str(purpose)) for purpose in raw if purpose]
    return []


def request_item_options(
    story: Story,
    state: dict[str, Any],
) -> tuple[RequestItemOption, ...]:
    """Return requestable purposes without exposing the hidden item itself."""
    present = set(story.characters_at(state))
    options: list[RequestItemOption] = []
    for item_id, item in story.items.items():
        if not isinstance(item, dict) or not item.get("portable", False):
            continue
        policy = item.get("request_policy")
        if not isinstance(policy, dict) or policy.get("enabled", True) is False:
            continue
        if not check_condition_block(state, policy.get("when") or {}):
            continue
        placement = (state.get("item_locations") or {}).get(item_id)
        if not isinstance(placement, dict) or placement.get("type") != "carried_by":
            continue
        owner_id = str(placement.get("id") or "")
        if owner_id == story.player_id or owner_id not in present:
            continue
        for purpose_id, purpose_label in _purposes(policy):
            options.append(RequestItemOption(
                owner_id=owner_id,
                item_id=str(item_id),
                purpose_id=purpose_id,
                purpose_label=purpose_label,
                policy=dict(policy),
            ))
    return tuple(options)


def request_item_tool(
    story: Story,
    state: dict[str, Any],
) -> CapabilityTool | None:
    options = request_item_options(story, state)
    if not options:
        return None
    owner_ids = sorted({option.owner_id for option in options})
    purpose_labels = {
        option.purpose_id: option.purpose_label for option in options
    }
    return CapabilityTool(
        capability="social",
        action="request_item",
        description=(
            "请求物品：向在场人物询问能否借出或交付某种用途的物品；"
            "不预先保证对方同意，也不揭示其具体持有物。"
        ),
        arguments_schema={
            "type": "object",
            "required": ["owner_id", "purpose"],
            "properties": {
                "owner_id": {"type": "string", "enum": owner_ids},
                "purpose": {
                    "type": "string",
                    "enum": sorted(purpose_labels),
                },
            },
            "x-owner-labels": {
                owner_id: story.character_name(owner_id) for owner_id in owner_ids
            },
            "x-purpose-labels": purpose_labels,
        },
        allowed_authority_levels=(
            AuthorityLevel.PRESENTATION,
            AuthorityLevel.MECHANICAL,
        ),
    )


def resolve_request_item(
    story: Story,
    state: dict[str, Any],
    arguments: dict[str, Any],
) -> tuple[CapabilityResolution | None, tuple[str, ...]]:
    """Adjudicate one request; a socially valid refusal still consumes a turn."""
    owner_id = str(arguments.get("owner_id") or "")
    purpose_id = str(arguments.get("purpose") or "")
    if not owner_id or not purpose_id:
        return None, ("请求物品需要指定在场人物和用途。",)

    matches = [
        option
        for option in request_item_options(story, state)
        if option.owner_id == owner_id and option.purpose_id == purpose_id
    ]
    if not matches:
        return None, ("当前人物没有可按该用途请求的物品。",)
    if len(matches) > 1:
        return None, ("这个用途对应多件物品，需要先说明得更具体。",)

    option = matches[0]
    policy = option.policy
    rapport_key = str(policy.get("rapport_key") or "rapport")
    rapport = get_value(state, f"{owner_id}.{rapport_key}")
    rapport = rapport if isinstance(rapport, (int, float)) else 0
    minimum = policy.get("min_rapport", 1)
    minimum = minimum if isinstance(minimum, (int, float)) else 1
    granted = bool(policy.get("always_grant")) or (
        not policy.get("never_grant") and rapport >= minimum
    )

    owner_name = story.character_name(owner_id)
    item_name = story.item_labels().get(option.item_id, option.item_id)
    if granted:
        response = str(
            policy.get("grant_narrative")
            or f"{owner_name}听明白你的用途后，把{item_name}交给你暂时使用。"
        )
        return CapabilityResolution(
            capability_id=REQUEST_ITEM_TOOL_ID,
            objects=(owner_id, option.item_id),
            mutations=(CapabilityMutation(
                path=f"item_locations.{option.item_id}",
                value={"type": "carried_by", "id": story.player_id},
            ),),
            response_hint=response,
            primary_goal_status="achieved",
            result_tier="success",
        ), ()

    response = str(
        policy.get("refusal_narrative")
        or f"{owner_name}听完请求后摇了摇头；现在的关系和情境还不足以让对方答应。"
    )
    return CapabilityResolution(
        capability_id=REQUEST_ITEM_TOOL_ID,
        objects=(owner_id,),
        mutations=(),
        response_hint=response,
        primary_goal_status="not_achieved",
        result_tier="fail_forward",
    ), ()
