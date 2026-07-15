"""Player-facing rendering for committed narrative-first turns."""

from __future__ import annotations

from typing import Any

from .content import Story
from .director import current_goal, current_scene_id
from .llm_protocol import LocalCanonKind
from .resolver import TurnResult
from .state import get_value


def render_scene_entry(
    story: Story, scene_id: str, state: dict[str, Any] | None = None
) -> str:
    scene = story.scene(scene_id)
    name = story.location_name(state, scene_id) if state is not None else scene.get(
        "name", scene_id
    )
    lines = [f"—— {name} ——"]
    entry = str(scene.get("entry_text") or "").strip()
    if not entry and state is not None:
        record = story.generated_locations(state).get(scene_id)
        if isinstance(record, dict):
            entry = str(record.get("description") or "").strip()
    if entry:
        lines.append(entry)
    return "\n".join(lines)


def render_intro(story: Story, state: dict[str, Any]) -> str:
    parts = [f"《{story.title}》", ""]
    if story.premise:
        parts += [story.premise, ""]
    parts.append(render_scene_entry(story, current_scene_id(state), state))
    return "\n".join(parts)


def render_status(story: Story, state: dict[str, Any]) -> str:
    from .perception import perception_config

    config = perception_config(story)
    header = f"场景：{story.location_name(state, current_scene_id(state))}"
    for key, label in config["world_state"].items():
        value = get_value(state, f"world.{key}")
        if value is not None:
            header += f" ｜ {label}：{value}"
    lines = [header, f"当前目标：{current_goal(story, state)}"]
    if state["facts"]:
        lines.append(f"已记录{config['facts_label']} {len(state['facts'])} 条（输入 facts 查看）")
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


def render_turn(story: Story, result: TurnResult, state: dict[str, Any]) -> str:
    lines = [
        result.narrative.strip()
        or "你把想法付诸尝试；当前回合没有产生可提交的铁律事实。"
    ]
    lines.extend(f"与此同时，{hint}" for hint in result.narrative_hints if hint)

    from .perception import perception_config

    facts_label = perception_config(story)["facts_label"]
    for fact in result.new_facts:
        lines.append(f"◇ 新{facts_label}：{fact}")

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
        and not path.startswith("generated.")
    ]
    if receipts:
        lines.append("（铁律事实提交：" + "，".join(receipts) + "）")
    for record in result.local_canon:
        kind = "地点" if record.kind == LocalCanonKind.LOCATION else "局势"
        lines.append(f"（新增局部事实：{record.name}〔{kind}〕）")
    for note in result.notes:
        lines.append(f"（{note}）")
    if result.scene_after != result.scene_before:
        lines.extend(["", render_scene_entry(story, result.scene_after, state)])
    return "\n".join(lines)


def render_ending(story: Story, ending_id: str) -> str:
    ending = story.endings.get(ending_id) or {}
    lines = [f"══ 结局：{ending.get('title', ending_id)} ══"]
    outcome = str(ending.get("outcome") or "").strip()
    if outcome:
        lines.append(outcome)
    return "\n".join(lines)
