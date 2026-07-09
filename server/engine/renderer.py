"""Template narrative rendering: the stage-2 stand-in for the LLM renderer.

Everything the player reads comes from author-written text (scene entry_text,
storylet narrative_hint) plus mechanical summaries. Stage 3 swaps this for an
LLM fed by the context spec in mvp-implementation-plan.md section 8.2.
"""

from __future__ import annotations

from typing import Any

from .content import Story
from .director import current_goal, current_scene_id
from .resolver import RESULT_FAIL_FORWARD, RESULT_PARTIAL, RESULT_SUCCESS, TurnResult
from .state import get_value

TIER_TEXT = {
    RESULT_SUCCESS: "成功",
    RESULT_PARTIAL: "部分成功",
    RESULT_FAIL_FORWARD: "未能如愿，但局面在变化",
}


def render_scene_entry(story: Story, scene_id: str) -> str:
    scene = story.scene(scene_id)
    lines = [f"—— {scene.get('name', scene_id)} ——"]
    entry = (scene.get("entry_text") or "").strip()
    if entry:
        lines.append(entry)
    return "\n".join(lines)


def render_intro(story: Story, state: dict[str, Any]) -> str:
    parts = [f"《{story.title}》", ""]
    if story.premise:
        parts += [story.premise, ""]
    parts.append(render_scene_entry(story, current_scene_id(state)))
    return "\n".join(parts)


def render_status(story: Story, state: dict[str, Any]) -> str:
    world = state["world"]
    lines = [
        f"场景：{story.scene(current_scene_id(state)).get('name', current_scene_id(state))}"
        f" ｜ 剩余时间：{world.get('time_left')}",
        f"当前目标：{current_goal(story, state)}",
    ]
    # 只显示当前场景在场的人物（presence 即场景 available_characters）
    scene_chars = story.scene(current_scene_id(state)).get("available_characters") or []
    watches = []
    for char_id in scene_chars:
        parts = []
        for key, tag in (("suspicion", "疑"), ("alertness", "警"), ("trust", "信")):
            value = get_value(state, f"{char_id}.{key}")
            if value is not None:
                parts.append(f"{tag}{value}")
        if parts:
            watches.append(f"{story.character_name(char_id)}({'/'.join(parts)})")
    if watches:
        lines.append("在场：" + "  ".join(watches))
    if state["clues"]:
        lines.append(f"已获线索 {len(state['clues'])} 条（输入 clues 查看）")
    return "\n".join(lines)


def render_objects(story: Story, state: dict[str, Any]) -> str:
    scene_id = current_scene_id(state)
    groups = story.scene(scene_id).get("available_objects") or {}
    group_names = {"people": "人物", "items": "物品", "environment": "环境", "states": "状态"}
    lines = []
    for group, items in groups.items():
        if not items:
            continue
        shown = []
        for obj in items:  # list yields ids; dict yields ids (keys)
            obj = str(obj)
            label = story.object_label(scene_id, obj)
            shown.append(f"{label}[{obj}]" if label != obj else obj)
        lines.append(f"{group_names.get(group, group)}：{ '，'.join(shown) }")
    return "\n".join(lines)


def render_characters(story: Story, state: dict[str, Any]) -> str:
    """`who` panel: public profiles of characters present in the scene."""
    scene = story.scene(current_scene_id(state))
    lines = []
    for char_id in scene.get("available_characters") or []:
        char = story.characters.get(char_id) or {}
        profile = " ".join((char.get("public_profile") or "").split())
        lines.append(f"● {char.get('name', char_id)}［{char_id}］：{profile}")
    return "\n".join(lines) or "（这里没有别人。）"


def render_quote(quote: dict[str, Any]) -> str:
    lines = ["◆ 行动报价", quote["understanding"]]
    if quote["risks"]:
        lines.append("风险：" + "；".join(quote["risks"]))
    if quote["costs"]:
        costs = "，".join(f"{path} {value:+}" if isinstance(value, (int, float)) else f"{path}→{value}"
                          for path, value in quote["costs"].items())
        lines.append(f"预计代价：{costs}")
    for note in quote["notes"]:
        lines.append(f"（{note}）")
    if not quote["can_execute"]:
        lines.append(f"无法执行：{quote['rejection_reason_in_world']}")
    return "\n".join(lines)


# Engine-default fallback lines for turns where no storylet fired.
# Generic on purpose; stage 3 replaces this whole layer with LLM rendering.
INTENT_FALLBACK = {
    "negotiate": "对话在试探中结束。没有立刻的突破，但对方记住了你的态度。",
    "observe": "你看得很仔细，暂时没有发现新的异常。",
    "sneak": "你悄悄换了位置，没有引起注意，也还没找到突破口。",
    "create_distraction": "动静起来了，但还没有形成真正的机会。",
    "threaten": "你的施压没有得到想要的反应。",
    "custom": "你的尝试产生了一些影响，但没有引出新的事件。",
}


def render_turn(story: Story, result: TurnResult) -> str:
    lines: list[str] = []
    meaningful = [c for c in result.changes if c[0] != "world.time_left" and c[1] != c[2]]
    if result.narrative_hints:
        lines.extend(result.narrative_hints)
    elif meaningful:
        lines.append(INTENT_FALLBACK.get(result.intent, "行动产生了影响，但没有引出新的事件。"))
    else:
        lines.append("这一步没有掀起波澜，但庄园的钟摆没有停。")
        lines.append("（提示：把意图和具体对象组合起来，例如 `observe family_portrait`；输入 objects 查看当前场景对象。）")
    for clue in result.new_clues:
        lines.append(f"◇ 新线索：{clue}")
    interesting = [
        (f"{path} {previous}→{new}" if previous is not None else f"{path} = {new}")
        for path, previous, new in result.changes
        if previous != new and not str(path).startswith("world.current_goal")
    ]
    if interesting:
        lines.append("（状态变化：" + "，".join(interesting) + "）")
    for note in result.notes:
        lines.append(f"（{note}）")
    lines.append(f"【判定：{TIER_TEXT.get(result.result_tier, result.result_tier)}】")
    if result.scene_after != result.scene_before:
        lines.append("")
        lines.append(render_scene_entry(story, result.scene_after))
    return "\n".join(lines)


def render_ending(story: Story, ending_id: str) -> str:
    ending = story.endings.get(ending_id) or {}
    lines = [f"══ 结局：{ending.get('title', ending_id)} ══"]
    outcome = (ending.get("outcome") or "").strip()
    if outcome:
        lines.append(outcome)
    return "\n".join(lines)
