#!/usr/bin/env python3
"""Play a story in the terminal against the stage-2 rule engine (no LLM).

Usage:
    python3 server/cli.py [content/midnight_archive.yaml]

Each turn: pick an intent (number or id), optionally followed by object ids,
e.g. `2 heir` or `observe family_portrait`. Medium/high-risk intents show a
quote card first and ask for confirmation.

Commands: objects / state / clues / help / quit
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.content import Story
from server.engine.director import (
    current_scene_id,
    goal_achieved,
    suggested_intents,
    transition_options,
)
from server.engine.renderer import (
    render_characters,
    render_ending,
    render_intro,
    render_inventory,
    render_objects,
    render_quote,
    render_status,
    render_turn,
)
from server.engine.llm import LLMProviderError, create_provider
from server.engine.llm_loop import commit_action, run_action_loop
from server.engine.narration import narrate_rejection, narrate_turn
from server.engine.session import GameSession, SessionError
from server.engine.trace import TraceRecorder

PROMPT = "> "


def extract_free_text(line: str, head: str) -> str:
    """The raw remainder after the intent token, kept verbatim for the LLM."""
    stripped = line.strip()
    if stripped.startswith(head):
        return stripped[len(head):].strip()
    match = re.match(r"^\s*\d+\s*(.*)$", stripped)
    return (match.group(1) if match else "").strip()


def print_turn(session: GameSession, provider, recorder, result, player_text: str = "") -> None:
    """LLM 叙事优先，失败静默回退模板；机械行（线索/状态/判定）恒显示。"""
    prose = narrate_turn(session, provider, result, recorder, player_text)
    print(render_turn(session.story, result, prose=prose))


def handle_free_action(session: GameSession, provider, recorder, free_text: str) -> None:
    """理解 → 验证 →（一次重规划）→ 报价 → 确认 → 提交。"""
    loop_result = run_action_loop(session, provider, free_text, recorder)
    if loop_result.clarification:
        print(f"（系统想先确认：{loop_result.clarification}）")
        print("（本次不消耗时间，请补充后重试。）")
        return
    if not loop_result.can_execute:
        reasons = loop_result.rejection_messages()
        prose = narrate_rejection(session, provider, reasons, free_text, recorder)
        if prose:
            print(prose)
        print("（方案未被接受，本次不消耗时间：）")
        for message in reasons:
            print(f"  - {message}")
        if loop_result.replanned:
            print("（系统已自动调整过一次方案，仍未通过；请换一种做法。）")
        return
    print(render_quote(loop_result.quote))
    for message in loop_result.adjustment_messages():
        print(f"（{message}）")
    answer = read_line("执行？[y=确认 / n=放弃] ")
    if answer is None or answer.lower() not in ("y", "yes", "确认", "执行"):
        print("（已放弃，本次不消耗时间。）")
        return
    commit_action(session, loop_result, recorder)
    if session.last_result is not None:
        print_turn(session, provider, recorder, session.last_result, free_text)


def tokenize_action_line(line: str) -> list[str]:
    """Split a command while accepting common Chinese target separators."""
    normalized = re.sub(r"[、，,]+", " ", line.strip())
    normalized = re.sub(r"^(\d+)(?=\D)", r"\1 ", normalized)
    return normalized.split()


def print_help() -> None:
    print(
        "输入格式：意图编号或 ID + 目标对象，例如 `1 全家画像`（观察）、`2 薇拉小姐`（交涉）。\n"
        "使用物品时同时输入口袋物品和目标，例如 `使用 仆役侧门钥匙 仆役窄门`；\n"
        "移动时选择【出口】中的名称或 ID，例如 `移动 返回仆役走廊`。\n"
        "目标来自【可用对象】、【出口】或【口袋】——中文名或 ID 都可以直接用。\n"
        "命令：objects 对象面板；inventory 口袋；who 人物介绍；state 状态；clues 线索；help 帮助；quit 退出。"
    )


def resolve_objects(story: Story, state, tokens: list[str]) -> list[str]:
    """Map player tokens (ids or display names) to object ids; warn on misses."""
    scene_id = current_scene_id(state)
    aliases = {}
    for char_id in story.characters:
        aliases[char_id] = char_id
        aliases[story.character_name(char_id)] = char_id
    for obj_id, label in story.object_labels(scene_id, state).items():
        aliases.setdefault(label, obj_id)
    item_labels = story.item_labels()
    for item_id in story.inventory(state) + story.items_at(state):
        aliases.setdefault(item_labels.get(item_id, item_id), item_id)
    for node_id, label in story.exit_labels(state).items():
        aliases.setdefault(label, node_id)
    scene_objects = story.actionable_objects(state)
    visible_objects = story.visible_scene_objects(scene_id, state)

    valid: list[str] = []
    for token in tokens:
        cleaned = "".join(ch for ch in token if ch.isprintable()).strip()
        for prefix in ("在场：", "口袋："):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
        if not cleaned:
            continue
        obj = aliases.get(cleaned, cleaned)
        if obj in scene_objects:
            valid.append(obj)
        elif obj in story.characters:
            print(f"（{story.character_name(obj)} 不在当前场景，已忽略。）")
        elif obj in story.items:
            print(f"（物品 {story.item_labels().get(obj, obj)} 当前未持有或不可见，已忽略。）")
        elif obj in story.world_nodes:
            print(f"（当前不能从这里前往 {story.world_nodes[obj].get('name', obj)}，已忽略。）")
        elif obj in visible_objects:
            print(f"（{story.object_label(scene_id, obj, state)} 当前可见但不可交互。）")
        else:
            print(f"（未识别对象 '{cleaned}'，已忽略；输入 objects 查看当前场景对象。）")
    return valid


def print_intents(story: Story, intent_ids: list[str]) -> None:
    entries = []
    for index, intent_id in enumerate(intent_ids, start=1):
        intent = story.intent(intent_id)
        marker = "" if not story.quote_required(intent_id) else "*"
        entries.append(f"[{index}] {intent.get('label', intent_id)}{marker}")
    print("可用意图（* 需先报价）：" + "  ".join(entries))


def read_line(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except EOFError:
        return None


def confirm_quote(session: GameSession, intent_id: str, objects: list[str], provider=None, recorder=None) -> bool:
    """Quote-confirm loop; returns True if the action was executed."""
    while True:
        try:
            quote = session.quote(intent_id, objects)
        except SessionError as exc:
            print(f"({exc})")
            return False
        print(render_quote(quote))
        if not quote.get("can_execute", True):
            return False
        answer = read_line("执行？[y=确认 / n=放弃 / r=重新报价] ")
        if answer is None or answer.lower() in ("n", "no", "放弃"):
            print("（已放弃，本次不消耗时间。）")
            return False
        if answer.lower() in ("r", "requote", "重新报价"):
            continue
        result = session.resolve(quote_id=quote["quote_id"])
        print_turn(session, provider, recorder, result)
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Play an AIRPG story in the terminal.")
    parser.add_argument("story", nargs="?", default="content/midnight_archive.yaml")
    parser.add_argument("--no-log", action="store_true", help="Do not write session logs.")
    parser.add_argument(
        "--llm",
        choices=["mock", "deepseek", "off"],
        default="mock",
        help="自由方案（custom）的理解 provider；deepseek 需设置 DEEPSEEK_API_KEY；off 退回结构化对象输入。",
    )
    args = parser.parse_args()

    story = Story.load(args.story)
    session = GameSession(story, log_dir=None if args.no_log else "data/sessions")
    try:
        provider = create_provider(args.llm) if args.llm != "off" else None
    except LLMProviderError as exc:
        print(f"（{exc}）")
        return 1
    recorder = TraceRecorder(None if args.no_log else "data/traces", session.session_id)

    print(render_intro(story, session.state))
    print()
    print_help()

    last_scene = None
    while not session.is_over:
        print()
        scene_now = current_scene_id(session.state)
        if scene_now != last_scene:
            print("【在场人物】")
            print(render_characters(story, session.state))
            print("【可用对象】")
            print(render_objects(story, session.state))
            print("【口袋】")
            print(render_inventory(story, session.state))
            print()
            last_scene = scene_now
        print(render_status(story, session.state))
        options = transition_options(story, session.state, session.consumed)
        if goal_achieved(story, session.state) and not options:
            print("（导演提示：本场景目标已达成，可以寻找推进的方式。）")
        for option in options:
            intent_labels = "/".join(story.intent(i).get("label", i) for i in option["intents"])
            hint = f"（可尝试：{option['title']}——意图［{intent_labels}］"
            if option["objects"]:
                labels = [story.object_label(scene_now, obj, session.state) for obj in option["objects"]]
                hint += f"，相关对象：{ '、'.join(labels) }"
            print(hint + "）")
        intent_ids = suggested_intents(story, session.state)
        print_intents(story, intent_ids)

        line = read_line(PROMPT)
        if line is None or line.lower() in ("quit", "exit", "q"):
            print("（离开庄园——会话结束。）")
            break
        if not line:
            continue
        if line.lower() in ("help", "h", "?"):
            print_help()
            continue
        if line.lower() == "objects":
            print(render_objects(story, session.state))
            continue
        if line.lower() in ("inventory", "inv", "bag", "口袋", "背包"):
            print(render_inventory(story, session.state))
            continue
        if line.lower() == "who":
            print(render_characters(story, session.state))
            continue
        if line.lower() == "state":
            import json

            print(json.dumps(session.state, ensure_ascii=False, indent=2, default=str))
            continue
        if line.lower() == "clues":
            for clue in session.state["clues"] or ["（还没有线索）"]:
                print(f"◇ {clue}")
            continue

        tokens = tokenize_action_line(line)
        head, raw_objects = tokens[0], tokens[1:]
        intent_aliases = {
            intent.get("label"): candidate_id
            for candidate_id, intent in story.intents.items()
            if isinstance(intent, dict) and intent.get("label")
        }
        if head.isdigit() and 1 <= int(head) <= len(intent_ids):
            intent_id = intent_ids[int(head) - 1]
        elif head in story.intents:
            intent_id = head
        elif head in intent_aliases:
            intent_id = intent_aliases[head]
        else:
            print(f"（未知意图 '{head}'，输入 help 查看格式。）")
            continue

        # 自由方案走 LLM 纵向链路：理解 → 验证 → 报价 → 提交。
        if intent_id == "custom" and provider is not None:
            free_text = extract_free_text(line, head)
            if not free_text:
                free_text = (read_line("描述你的方案：") or "").strip()
                if not free_text:
                    continue
            try:
                handle_free_action(session, provider, recorder, free_text)
            except LLMProviderError as exc:
                print(f"（理解服务暂时不可用：{exc}；可用结构化输入继续，或稍后重试。）")
            except SessionError as exc:
                print(f"({exc})")
            continue

        objects = resolve_objects(story, session.state, raw_objects)

        # Explicit but wholly invalid targets are rejected before quoting or
        # resolution, so an absent entity never costs a world step.
        if raw_objects and len(objects) != len(raw_objects):
            continue

        # 低风险意图没有报价环节兜底，不带目标几乎必然打空：给提示，不消耗回合。
        if not objects and not story.quote_required(intent_id):
            intent_label = story.intent(intent_id).get("label", intent_id)
            print(f"（「{intent_label}」需要一个具体目标才有效，本次未执行、不消耗时间。当前可选：）")
            print(render_objects(story, session.state))
            continue

        try:
            if story.quote_required(intent_id):
                confirm_quote(session, intent_id, objects, provider, recorder)
            else:
                result = session.resolve(intent_id=intent_id, objects=objects)
                print_turn(session, provider, recorder, result)
        except SessionError as exc:
            print(f"({exc})")
        except LLMProviderError as exc:
            print(f"（叙事服务暂时不可用，已用模板文本继续：{exc}）")

    if session.ending:
        print()
        print(render_ending(story, session.ending))
        print(f"（本局回合数：{session.turn_no}，剩余时间：{session.state['world'].get('time_left')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
