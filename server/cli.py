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
    render_objects,
    render_quote,
    render_status,
    render_turn,
)
from server.engine.session import GameSession, SessionError

PROMPT = "> "


def print_help() -> None:
    print(
        "输入格式：意图编号或 ID + 目标对象，例如 `1 全家画像`（观察）、`2 薇拉小姐`（交涉）。\n"
        "目标一律来自【可用对象】面板——面板里列出的中文名或 ID 都可以直接用。\n"
        "命令：objects 对象面板；who 人物介绍；state 状态；clues 线索；help 帮助；quit 退出。"
    )


def resolve_objects(story: Story, state, tokens: list[str]) -> list[str]:
    """Map player tokens (ids or display names) to object ids; warn on misses."""
    scene_id = current_scene_id(state)
    aliases = {}
    for char_id in story.characters:
        aliases[char_id] = char_id
        aliases[story.character_name(char_id)] = char_id
    for obj_id, label in story.object_labels(scene_id).items():
        aliases.setdefault(label, obj_id)
    scene_objects = story.scene_objects(scene_id)

    valid: list[str] = []
    for token in tokens:
        cleaned = "".join(ch for ch in token if ch.isprintable()).strip()
        if not cleaned:
            continue
        obj = aliases.get(cleaned, cleaned)
        if obj in scene_objects:
            valid.append(obj)
        elif obj in story.characters:
            print(f"（{story.character_name(obj)} 不在当前场景，已忽略。）")
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


def confirm_quote(session: GameSession, intent_id: str, objects: list[str]) -> bool:
    """Quote-confirm loop; returns True if the action was executed."""
    while True:
        try:
            quote = session.quote(intent_id, objects)
        except SessionError as exc:
            print(f"({exc})")
            return False
        print(render_quote(quote))
        answer = read_line("执行？[y=确认 / n=放弃 / r=重新报价] ")
        if answer is None or answer.lower() in ("n", "no", "放弃"):
            print("（已放弃，本次不消耗时间。）")
            return False
        if answer.lower() in ("r", "requote", "重新报价"):
            continue
        result = session.resolve(quote_id=quote["quote_id"])
        print(render_turn(session.story, result))
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Play an AIRPG story in the terminal.")
    parser.add_argument("story", nargs="?", default="content/midnight_archive.yaml")
    parser.add_argument("--no-log", action="store_true", help="Do not write session logs.")
    args = parser.parse_args()

    story = Story.load(args.story)
    session = GameSession(story, log_dir=None if args.no_log else "data/sessions")

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
                hint += f"，相关对象：{ '、'.join(option['objects']) }"
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

        tokens = line.split()
        head, raw_objects = tokens[0], tokens[1:]
        if head.isdigit() and 1 <= int(head) <= len(intent_ids):
            intent_id = intent_ids[int(head) - 1]
        elif head in story.intents:
            intent_id = head
        else:
            print(f"（未知意图 '{head}'，输入 help 查看格式。）")
            continue
        objects = resolve_objects(story, session.state, raw_objects)

        # 低风险意图没有报价环节兜底，不带目标几乎必然打空：给提示，不消耗回合。
        if not objects and not story.quote_required(intent_id):
            intent_label = story.intent(intent_id).get("label", intent_id)
            print(f"（「{intent_label}」需要一个具体目标才有效，本次未执行、不消耗时间。当前可选：）")
            print(render_objects(story, session.state))
            continue

        try:
            if story.quote_required(intent_id):
                confirm_quote(session, intent_id, objects)
            else:
                result = session.resolve(intent_id=intent_id, objects=objects)
                print(render_turn(story, result))
        except SessionError as exc:
            print(f"({exc})")

    if session.ending:
        print()
        print(render_ending(story, session.ending))
        print(f"（本局回合数：{session.turn_no}，剩余时间：{session.state['world'].get('time_left')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
