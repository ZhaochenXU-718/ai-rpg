"""Director-layer signals: scene goals and goal-achievement checks.

Stage 2 keeps the director thin: it reads exit_conditions as "the scene goal
is met, push toward a transition" and surfaces the current goal for the UI.
Event scheduling itself lives in the storylet pass.
"""

from __future__ import annotations

from typing import Any

from .conditions import check_condition_block, check_exit_conditions
from .content import Story
from .effects import effect_changes_scene


def current_scene_id(state: dict[str, Any]) -> str:
    return state["world"].get("scene", "")


def current_goal(story: Story, state: dict[str, Any]) -> str:
    scene = story.scene(current_scene_id(state))
    return scene.get("goal") or state["world"].get("current_goal", "")


def goal_achieved(story: Story, state: dict[str, Any]) -> bool:
    scene = story.scene(current_scene_id(state))
    return check_exit_conditions(state, scene.get("exit_conditions"), story.endings)


def suggested_intents(story: Story, state: dict[str, Any]) -> list[str]:
    scene = story.scene(current_scene_id(state))
    return list(scene.get("suggested_intents") or story.intents.keys())


def transition_options(
    story: Story,
    state: dict[str, Any],
    consumed: set[str],
) -> list[dict[str, Any]]:
    """Player-actionable transitions whose state preconditions already hold.

    Surfaces scene-changing storylets as concrete director hints ("可尝试：
    后楼梯上二楼——潜入"), so an earned opportunity is never invisible.
    Only intent/object requirements are left for the player to supply;
    auto-firing transitions (no intent requirement) are excluded.
    """
    options: list[dict[str, Any]] = []
    for storylet in story.storylets:
        if storylet.get("once") and storylet.get("id") in consumed:
            continue
        effect = storylet.get("effect") or {}
        if not effect_changes_scene(effect):
            continue
        trigger = storylet.get("trigger") or {}
        intents = [trigger["intent"]] if "intent" in trigger else list(trigger.get("intent_any") or [])
        if not intents:
            continue  # fires on state alone; nothing for the player to do
        state_conditions = {
            key: value for key, value in trigger.items()
            if key not in ("intent", "intent_any", "object_any")
        }
        if not check_condition_block(state, state_conditions):
            continue
        options.append({
            "title": storylet.get("title", storylet.get("id")),
            "intents": intents,
            "objects": list(trigger.get("object_any") or []),
        })
    return options
