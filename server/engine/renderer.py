"""Template narrative rendering: the stage-2 stand-in for the LLM renderer.

Everything the player reads comes from author-written text (scene entry_text,
storylet narrative_hint) plus mechanical summaries. Stage 3 swaps this for an
LLM fed by the context spec in mvp-implementation-plan.md section 8.2.
"""

from __future__ import annotations

from typing import Any

from .content import Story
from .director import current_goal, current_scene_id
from .llm_protocol import LocalCanonKind
from .resolver import RESULT_FAIL_FORWARD, RESULT_PARTIAL, RESULT_SUCCESS, TurnResult
from .state import get_value

TIER_TEXT = {
    RESULT_SUCCESS: "成功",
    RESULT_PARTIAL: "部分成功",
    RESULT_FAIL_FORWARD: "未能如愿，但局面在变化",
}


def render_scene_entry(
    story: Story, scene_id: str, state: dict[str, Any] | None = None
) -> str:
    scene = story.scene(scene_id)
    name = story.location_name(state, scene_id) if state is not None else scene.get(
        "name", scene_id
    )
    lines = [f"—— {name} ——"]
    entry = (scene.get("entry_text") or "").strip()
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
    # What may be shown and how it is labelled comes from the story's
    # `perception` block; the engine carries no genre vocabulary here.
    from .perception import perception_config

    config = perception_config(story)
    header = f"场景：{story.location_name(state, current_scene_id(state))}"
    for key, label in config["world_state"].items():
        value = get_value(state, f"world.{key}")
        if value is not None:
            header += f" ｜ {label}：{value}"
    lines = [header, f"当前目标：{current_goal(story, state)}"]
    # Physical presence is derived from the authoritative world positions.
    watches = []
    for char_id in story.characters_at(state):
        parts = []
        for key, tag in config["character_state"].items():
            value = get_value(state, f"{char_id}.{key}")
            if value is not None:
                parts.append(f"{tag}{value}")
        if parts:
            watches.append(f"{story.character_name(char_id)}({'/'.join(parts)})")
    if watches:
        lines.append("在场：" + "  ".join(watches))
    if state["facts"]:
        lines.append(f"已获{config['facts_label']} {len(state['facts'])} 条（输入 facts 查看）")
    inventory = story.inventory(state)
    if inventory:
        lines.append("口袋：" + "、".join(story.item_labels().get(item, item) for item in inventory))
    return "\n".join(lines)


def render_objects(story: Story, state: dict[str, Any]) -> str:
    scene_id = current_scene_id(state)
    groups = story.scene_object_groups(scene_id, state)
    actionable = story.scene_objects(scene_id, state)
    group_names = {"people": "人物", "items": "物品", "environment": "环境", "states": "状态"}
    lines = []
    if state.get("positions"):
        people = story.characters_at(state, scene_id)
        if people:
            shown = [f"{story.character_name(obj)}[{obj}]" for obj in people]
            lines.append(f"人物：{ '，'.join(shown) }")
    board_items = story.items_at(state, scene_id)
    if board_items:
        shown = [f"{story.item_labels().get(obj, obj)}[{obj}]" for obj in board_items]
        lines.append(f"可拾取物品：{ '，'.join(shown) }")
    for group, items in groups.items():
        if group == "people" and state.get("positions"):
            continue
        if not items:
            continue
        shown = []
        for obj in items:
            obj = str(obj)
            label = story.object_label(scene_id, obj, state)
            if obj not in actionable:
                label += "（当前不可交互）"
            shown.append(f"{label}[{obj}]" if label != obj else obj)
        lines.append(f"{group_names.get(group, group)}：{ '，'.join(shown) }")
    exits = story.exit_labels(state)
    if exits:
        shown = [f"{label}[{node_id}]" for node_id, label in exits.items()]
        lines.append(f"出口：{ '，'.join(shown) }")
    return "\n".join(lines)


def render_inventory(story: Story, state: dict[str, Any]) -> str:
    items = story.inventory(state)
    if not items:
        return "（口袋是空的。）"
    shown = []
    for item_id in items:
        item = story.items.get(item_id) or {}
        name = item.get("name", item_id)
        description = item.get("description")
        entry = f"◆ {name}［{item_id}］"
        if description:
            entry += f"：{description}"
        shown.append(entry)
    return "\n".join(shown)


def render_characters(story: Story, state: dict[str, Any]) -> str:
    """`who` panel: public profiles of characters present in the scene."""
    lines = []
    for char_id in story.characters_at(state):
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
    # Disclosure wall (perception.py): only consequences on already-visible
    # public state reach the card. The full preview stays internal — a quote
    # is a risk estimate, not an oracle of reveals and endings.
    disclosed = quote.get("disclosed_changes") or []
    if disclosed:
        effects = "，".join(
            f"{path} {previous}→{new}"
            for path, previous, new in disclosed
        )
        lines.append(f"预计影响：{effects}")
    move = quote.get("expected_move")
    if move:
        lines.append(f"预计移动：{move['from']} → {move['to']}")
    for note in quote["notes"]:
        lines.append(f"（{note}）")
    if not quote["can_execute"]:
        lines.append(f"无法执行：{quote['rejection_reason_in_world']}")
    return "\n".join(lines)


# Engine-default fallback lines for turns where no storylet fired. Stories
# override per intent via `intents.<id>.fallback_narrative`; these generic
# lines carry no genre vocabulary. Stage 3 replaces this layer with LLM
# rendering.
FALLBACK_WITH_EFFECT = "行动产生了影响，但没有引出新的事件。"
FALLBACK_NO_EFFECT = "这一步没有改变任何事，但时间仍在流逝。"


def intent_fallback_line(story: Story, intent_id: str) -> str:
    line = story.intent(intent_id).get("fallback_narrative")
    return str(line) if line else FALLBACK_WITH_EFFECT


def render_turn(
    story: Story,
    result: TurnResult,
    prose: str | None = None,
    *,
    free_text_mode: bool = False,
    state: dict[str, Any] | None = None,
) -> str:
    """Render one committed turn. ``prose`` (LLM narration) replaces the
    template text when provided; mechanical lines (facts, state changes,
    tier) always print — they are the fairness receipt, not flavour."""
    if result.errors:
        return "\n".join(
            ["（行动未执行：" + "；".join(result.errors) + "）", "【判定：未执行】"]
        )

    lines: list[str] = []
    by_path: dict[str, list[Any]] = {}
    path_order: list[str] = []
    for path, previous, new in result.changes:
        if path not in by_path:
            by_path[path] = [previous, new]
            path_order.append(path)
        else:
            by_path[path][1] = new
    collapsed = [
        (path, by_path[path][0], by_path[path][1])
        for path in path_order
        if by_path[path][0] != by_path[path][1]
    ]
    # "Did anything change beyond the action's declared cost?" — the cost
    # paths come from the intent's typical_cost, not a hardcoded clock key.
    cost_paths = {
        str(key) if "." in str(key) else f"world.{key}"
        for key in (story.intent(result.intent).get("typical_cost") or {})
    }
    meaningful = [c for c in collapsed if c[0] not in cost_paths]
    if prose:
        lines.append(prose)
    elif (
        result.action_response_hints
        or result.world_beat_hints
        or result.world_reaction_hints
    ):
        # Preserve the same attribution contract when LLM narration is empty:
        # the player's action is always answered first; concurrent director
        # beats and autonomous world reactions are explicitly separated.
        lines.extend(result.action_response_hints)
        lines.extend(f"与此同时，{hint}" for hint in result.world_beat_hints)
        lines.extend(f"随后，{hint}" for hint in result.world_reaction_hints)
    elif result.narrative_hints:
        # Compatibility for results produced before attribution was tracked.
        lines.extend(result.narrative_hints)
    elif meaningful:
        lines.append(intent_fallback_line(story, result.intent))
    else:
        lines.append(FALLBACK_NO_EFFECT)
        if free_text_mode:
            lines.append("（提示：可以继续用自然语言补充更具体的目标、做法或工具。）")
        else:
            lines.append("（提示：把意图和具体对象组合起来，例如 `<意图 ID> <对象 ID>`；输入 objects 查看当前场景对象。）")
    from .perception import perception_config

    facts_label = perception_config(story)["facts_label"]
    for fact in result.new_facts:
        lines.append(f"◇ 新{facts_label}：{fact}")
    interesting = [
        (f"{path} {previous}→{new}" if previous is not None else f"{path} = {new}")
        for path, previous, new in collapsed
        if not str(path).startswith("world.current_goal")
        # Local Canon records are dicts; their receipt is the dedicated
        # "新增局部事实" line below, not a raw state dump.
        and not str(path).startswith("generated.")
    ]
    if interesting:
        lines.append("（状态变化：" + "，".join(interesting) + "）")
    for record in result.local_canon:
        kind_label = "地点" if record.kind == LocalCanonKind.LOCATION else "局势"
        lines.append(f"（新增局部事实：{record.name}〔{kind_label}〕）")
    for note in result.notes:
        lines.append(f"（{note}）")
    lines.append(f"【判定：{TIER_TEXT.get(result.result_tier, result.result_tier)}】")
    if result.scene_after != result.scene_before:
        lines.append("")
        lines.append(render_scene_entry(story, result.scene_after, state))
    return "\n".join(lines)


def render_ending(story: Story, ending_id: str) -> str:
    ending = story.endings.get(ending_id) or {}
    lines = [f"══ 结局：{ending.get('title', ending_id)} ══"]
    outcome = (ending.get("outcome") or "").strip()
    if outcome:
        lines.append(outcome)
    return "\n".join(lines)
