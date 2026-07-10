"""Walkthrough runner: drives the turn resolver with scripted steps.

    Walkthroughs are the acceptance cases for this engine: every ending must
    stay reachable within the time budget, including inventory/use and
    world-step movement, and each step's fired storylets must match exactly.
"""

from __future__ import annotations

from typing import Any

from .content import Story
from .director import current_scene_id
from .effects import TemporaryEffects
from .resolver import run_turn
from .state import build_initial_state


def run_walkthrough(walkthrough: dict[str, Any], story: Story) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one scripted walkthrough."""
    errors: list[str] = []
    warnings: list[str] = []
    state = build_initial_state(story.data)
    temporaries = TemporaryEffects()
    consumed: set[str] = set()
    target = walkthrough.get("target_ending")
    steps = walkthrough.get("steps") or []
    reached: str | None = None

    for index, step in enumerate(steps):
        turn_no = index + 1
        label = f"step {turn_no}"
        if reached is not None:
            errors.append(f"{label}: ending '{reached}' was already reached before the final step.")
            break

        intent = step.get("intent")
        if intent not in story.intents:
            errors.append(f"{label}: unknown intent '{intent}'.")
            break
        objects = [str(obj) for obj in step.get("objects") or []]
        scene_id = current_scene_id(state)
        available = story.actionable_objects(state)
        for obj in objects:
            if obj not in available:
                warnings.append(f"{label}: object '{obj}' not available in scene '{scene_id}'.")

        result = run_turn(
            story,
            state,
            temporaries,
            consumed,
            turn_no,
            intent,
            objects,
            step.get("generic_patch") or {},
            strict_patch=True,
        )
        errors.extend(result.errors)

        expected = [str(sid) for sid in step.get("expect_storylets") or []]
        if result.fired != expected:
            errors.append(f"{label}: fired storylets {result.fired} != expected {expected}.")
        if "expect_world_rules" in step:
            expected_rules = [str(rule_id) for rule_id in step.get("expect_world_rules") or []]
            if result.world_rules != expected_rules:
                errors.append(
                    f"{label}: world rules {result.world_rules} != expected {expected_rules}."
                )
        reached = result.ending

    if not errors:
        if reached is None:
            errors.append(f"walkthrough ended after {len(steps)} steps without reaching any ending.")
        elif reached != target:
            errors.append(f"reached ending '{reached}', expected '{target}'.")
    time_left = state["world"].get("time_left")
    if isinstance(time_left, (int, float)) and time_left < 0:
        warnings.append(f"time_left ended at {time_left}; budget math is worth rechecking.")
    return errors, warnings
