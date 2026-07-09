"""resolution_limits enforcement: the Validate step of Plan-Validate-Apply.

Two modes:
- clamp_generic_patch: gameplay path. Filters protected/unlisted paths and
  clamps values to bounds; the rejected parts are reported, never applied.
- validate_generic_patch: strict path used by walkthrough checking, where a
  declared patch must already be fully legal.
"""

from __future__ import annotations

from typing import Any

from .state import get_value, is_number


def _bounds_for(limits: dict[str, Any] | None, path: str) -> dict[str, Any] | None:
    if not limits:
        return None
    return (limits.get("patchable") or {}).get(path)


def _is_protected(limits: dict[str, Any] | None, path: str) -> bool:
    for entry in (limits or {}).get("protected") or []:
        entry = str(entry)
        if entry == path:
            return True
        if entry.endswith(".*") and path.startswith(entry[:-1]):
            return True
    return False


def clamp_generic_patch(
    patch: dict[str, Any],
    state: dict[str, Any],
    limits: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Return (applicable patch, rejection notes). Never raises."""
    accepted: dict[str, Any] = {}
    notes: list[str] = []
    if not patch:
        return accepted, notes
    if limits is None:
        return {}, ["故事未定义 resolution_limits，兜底判定不改变任何状态。"]

    max_paths = limits.get("max_paths_per_action")
    entries = list(patch.items())
    if isinstance(max_paths, int) and len(entries) > max_paths:
        for key, _ in entries[max_paths:]:
            notes.append(f"超出单次行动路径上限（{max_paths}），忽略 '{key}'。")
        entries = entries[:max_paths]

    for raw_path, value in entries:
        path = str(raw_path)
        if _is_protected(limits, path):
            notes.append(f"'{path}' 是受保护状态，只能由事件卡改变，已拒绝。")
            continue
        bounds = _bounds_for(limits, path)
        if bounds is None:
            notes.append(f"'{path}' 不在兜底判定白名单内，已拒绝。")
            continue
        if "values" in bounds:
            if value in bounds["values"]:
                accepted[path] = value
            else:
                notes.append(f"'{path}' 的取值 {value!r} 不在 {bounds['values']} 内，已拒绝。")
            continue
        if not is_number(value):
            notes.append(f"数值路径 '{path}' 收到非数值 {value!r}，已拒绝。")
            continue
        step = value
        step_limit = bounds.get("max_step")
        if step_limit is not None and abs(step) > step_limit:
            clamped = step_limit if step > 0 else -step_limit
            notes.append(f"'{path}' 的步长 {step} 超过 max_step {step_limit}，裁剪为 {clamped}。")
            step = clamped
        current = get_value(state, path)
        base = current if is_number(current) else 0
        result = base + step
        if "min" in bounds and result < bounds["min"]:
            step = bounds["min"] - base
            notes.append(f"'{path}' 触及下限 {bounds['min']}，实际变化 {step}。")
        if "max" in bounds and result > bounds["max"]:
            step = bounds["max"] - base
            notes.append(f"'{path}' 触及上限 {bounds['max']}，实际变化 {step}。")
        if step:
            accepted[path] = step
    return accepted, notes


def validate_generic_patch(
    patch: dict[str, Any],
    state: dict[str, Any],
    limits: dict[str, Any] | None,
    label: str,
) -> list[str]:
    """Strict mode: return errors instead of clamping. Used by walkthroughs."""
    errors: list[str] = []
    if not patch:
        return errors
    if limits is None:
        return [f"{label}: generic_patch used but the story has no resolution_limits."]
    patchable = limits.get("patchable") or {}
    max_paths = limits.get("max_paths_per_action")
    if isinstance(max_paths, int) and len(patch) > max_paths:
        errors.append(f"{label}: generic_patch touches {len(patch)} paths, max is {max_paths}.")
    for raw_path, value in patch.items():
        path = str(raw_path)
        bounds = patchable.get(path)
        if bounds is None:
            errors.append(f"{label}: generic_patch path '{path}' is not in resolution_limits.patchable.")
            continue
        if "values" in bounds:
            if value not in bounds["values"]:
                errors.append(f"{label}: generic_patch value {value!r} for '{path}' not in {bounds['values']}.")
            continue
        if not is_number(value):
            errors.append(f"{label}: generic_patch value for numeric path '{path}' must be a number.")
            continue
        step_limit = bounds.get("max_step")
        if step_limit is not None and abs(value) > step_limit:
            errors.append(f"{label}: generic_patch step {value} for '{path}' exceeds max_step {step_limit}.")
        current = get_value(state, path)
        base = current if is_number(current) else 0
        result = base + value
        if "min" in bounds and result < bounds["min"]:
            errors.append(f"{label}: '{path}' would drop to {result}, below min {bounds['min']}.")
        if "max" in bounds and result > bounds["max"]:
            errors.append(f"{label}: '{path}' would rise to {result}, above max {bounds['max']}.")
    return errors
