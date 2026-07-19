"""Player-facing rendering for committed narrative-first turns."""

from __future__ import annotations

from typing import Any

from .content import Story
from .memory import MemoryState
from .resolver import TurnResult


def render_scene_entry(
    story: Story, scene_id: str, state: dict[str, Any] | None = None
) -> str:
    scene = story.scene(scene_id)
    name = story.location_name(state, scene_id) if state is not None else scene.get(
        "name", scene_id
    )
    lines = [f"—— {name} ——"]
    entry = str(scene.get("entry_text") or "").strip()
    if entry:
        lines.append(entry)
    return "\n".join(lines)


def render_intro(story: Story, state: dict[str, Any]) -> str:
    parts = [f"《{story.title}》", ""]
    if story.premise:
        parts += [story.premise, ""]
    parts.append(render_scene_entry(story, story.current_location(state), state))
    if story.opening_narration:
        parts += ["", story.opening_narration]
    return "\n".join(parts)


def render_status(story: Story, state: dict[str, Any]) -> str:
    header = f"场景：{story.location_name(state, story.current_location(state))}"
    lines = [header, f"当前目标：{story.current_goal(state)}"]
    inventory = story.inventory(state)
    if inventory:
        lines.append(
            "持有：" + "、".join(story.item_labels().get(item, item) for item in inventory)
        )
    return "\n".join(lines)


def render_characters(story: Story, state: dict[str, Any]) -> str:
    lines = []
    for char_id in story.characters_at(state):
        char = story.characters.get(char_id) or {}
        profile = " ".join(str(char.get("public_profile") or "").split())
        lines.append(f"● {char.get('name', char_id)}［{char_id}］：{profile}")
    return "\n".join(lines) or "（这里没有别人。）"


def render_memory(story: Story, memory: MemoryState, *, raw: bool = False) -> str:
    """Render soft memory without presenting it as authoritative state."""
    if raw:
        lines = ["【原始叙事事件｜非权威状态】"]
        events = memory.events[-10:]
        if not events:
            return "\n".join([*lines, "（还没有已提交回合。）"])
        for event in events:
            before = story.location_name({}, event.scene_before)
            after = story.location_name({}, event.scene_after)
            location = before if before == after else f"{before}→{after}"
            lines.append(f"回合 {event.turn_no}｜{location}")
            lines.append(f"  玩家：{event.player_text}")
            lines.append(f"  叙事：{event.narrative}")
            for change in event.physical_changes:
                lines.append(
                    f"  物理变化：{change.path}: {change.previous}→{change.new}"
                )
        return "\n".join(lines)

    lines = ["【叙事记忆｜非权威状态】"]
    if memory.rolling_summary:
        lines.append(
            f"滚动小结（截至回合 {memory.compacted_through_turn}）："
            + memory.rolling_summary
        )
    else:
        lines.append("滚动小结：（尚未达到压缩条件。）")
    if memory.last_compaction_error:
        lines.append(
            "小结状态：上次小结失败（"
            + memory.last_compaction_error
            + "）；原始事件仍完整保留。"
        )
    if memory.open_loops:
        lines.append("开放事项：")
        lines.extend(f"- {item}" for item in memory.open_loops)
    if memory.recently_resolved:
        lines.append("最近解决：")
        lines.extend(f"- {item}" for item in memory.recently_resolved)
    if memory.character_notes:
        lines.append("人物小结：")
        for group in memory.character_notes:
            name = story.character_name(group.subject_id)
            lines.append(f"- {name}：" + "；".join(group.notes))
    if memory.scene_notes:
        lines.append("场景小结：")
        for group in memory.scene_notes:
            name = story.location_name({}, group.subject_id)
            lines.append(f"- {name}：" + "；".join(group.notes))
    if memory.module_states:
        lines.append("剧情模块（软状态）：")
        for record in memory.module_states:
            spec = story.modules.get(record.module_id) or {}
            title = str(spec.get("title") or record.module_id)
            lines.append(
                f"- {title}［{record.module_id}］：{record.status}"
                f"（抛出 {record.offers_count} 次）"
            )
    lines.append("最近经历：")
    if not memory.recent_events:
        lines.append("（还没有已提交回合。）")
    else:
        lines.extend(
            f"- 回合 {event.turn_no}：{event.narrative}"
            for event in memory.recent_events
        )
    return "\n".join(lines)


def render_turn(story: Story, result: TurnResult, state: dict[str, Any]) -> str:
    lines = [
        result.narrative.strip()
        or "你把想法付诸尝试；当前回合没有产生需要单独记录的物理变化。"
    ]
    collapsed: dict[str, tuple[Any, Any]] = {}
    order: list[str] = []
    for path, previous, new in result.changes:
        if path not in collapsed:
            collapsed[path] = (previous, new)
            order.append(path)
        else:
            collapsed[path] = (collapsed[path][0], new)
    receipts = [
        f"{path}: {collapsed[path][0]}→{collapsed[path][1]}"
        for path in order
        if collapsed[path][0] != collapsed[path][1]
    ]
    if receipts:
        lines.append("（物理事实提交：" + "，".join(receipts) + "）")
    if result.scene_after != result.scene_before:
        lines.extend(["", render_scene_entry(story, result.scene_after, state)])
    return "\n".join(lines)
