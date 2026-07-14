#!/usr/bin/env python3
"""Play a content-defined story in the terminal.

Usage:
    python3 server/cli.py [path/to/story.yaml]

Each turn: pick an intent (number or id), optionally followed by object ids,
e.g. `2 <target>` or `<intent_id> <target_id>`. Medium/high-risk intents show
a quote card first and ask for confirmation. When no story path is supplied,
the CLI discovers content files and asks the player to choose one.

Commands: objects / state / facts / undo / timeline / help / quit
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
from server.engine.suggestions import (
    generate_action_suggestions,
    prepare_suggested_action,
)
from server.engine.trace import TraceRecorder

PROMPT = "> "
CONTENT_DIR = Path("content")


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
    print(render_turn(
        session.story,
        result,
        prose=prose,
        free_text_mode=provider is not None,
    ))


def execute_prepared_action(
    session: GameSession,
    provider,
    recorder,
    loop_result,
    player_text: str,
) -> bool:
    """Confirm when required, then commit an already validated frozen plan."""
    for message in loop_result.adjustment_messages():
        print(f"（{message}）")
    if loop_result.confirmation_required:
        print(render_quote(loop_result.quote))
        answer = read_line("执行？[y=确认 / n=放弃] ")
        if answer is None or answer.lower() not in ("y", "yes", "确认", "执行"):
            print("（已放弃，本次不消耗时间。）")
            return False
    else:
        print(f"（理解：{loop_result.plan.interpretation}）")
    commit_action(
        session,
        loop_result,
        recorder,
        director_provider=provider,
    )
    if session.last_result is not None:
        print_turn(session, provider, recorder, session.last_result, player_text)
    return True


def handle_free_action(session: GameSession, provider, recorder, free_text: str) -> bool:
    """理解 → 验证 → 可逆性策略 → 提交。"""
    loop_result = run_action_loop(session, provider, free_text, recorder)
    if loop_result.clarification:
        print(f"（系统想先确认：{loop_result.clarification}）")
        print("（本次不消耗时间，请补充后重试。）")
        return False
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
        return False
    return execute_prepared_action(
        session, provider, recorder, loop_result, free_text
    )


def tokenize_action_line(line: str) -> list[str]:
    """Split a command while accepting common Chinese target separators."""
    normalized = re.sub(r"[、，,]+", " ", line.strip())
    normalized = re.sub(r"^(\d+)(?=\D)", r"\1 ", normalized)
    return normalized.split()


def print_help() -> None:
    print(
        "LLM 模式可直接输入完整的自然语言方案；也可使用下面的结构化快捷方式。\n"
        "输入格式：`<意图编号或 ID> <目标名称或 ID>`，目标可以有多个。\n"
        "使用物品时同时输入【口袋】物品和作用目标，例如 `使用 <物品> <目标>`；\n"
        "移动时选择【出口】中的名称或 ID，例如 `移动 <出口>`。\n"
        "目标来自【可用对象】、【出口】或【口袋】——中文名或 ID 都可以直接用。\n"
        "命令：objects 对象面板；inventory 口袋；who 人物介绍；state 状态；facts 已知记录；"
        "ideas 生成动态行动提案；idea <编号> 直接执行已验证提案；"
        "undo 撤回上次行动并创建分支；timeline 查看状态分支；help 帮助；quit 退出。"
    )


def print_suggestions(suggestion_set) -> None:
    print("【行动提案｜也可以完全忽略并自由输入】")
    for index, suggestion in enumerate(suggestion_set.actions, start=1):
        print(f"[{index}] {suggestion.title}（{suggestion.focus}）")
        print(f"    {suggestion.action_text}")
        print(f"    侧重点：{suggestion.rationale}")


def print_timeline(session: GameSession) -> None:
    """Render the retained branch heads and active ancestry."""
    print("【状态分支】")
    for branch in session.branches():
        head = session.get_checkpoint(branch.head_checkpoint_id)
        marker = "*" if branch.branch_id == session.current_branch_id else " "
        print(
            f"{marker} {branch.branch_id}: {branch.head_checkpoint_id} "
            f"（回合 {head.turn_no}，从 {branch.fork_checkpoint_id} 分出）"
        )
    ancestry = " → ".join(
        checkpoint.checkpoint_id for checkpoint in session.checkpoint_history()
    )
    print(f"当前路径：{ancestry}；状态修订：{session.state_revision}")


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


def discover_story_paths(content_dir: Path = CONTENT_DIR) -> list[Path]:
    """Return loadable-looking story files without preferring any story id."""
    if not content_dir.is_dir():
        return []
    return sorted(path for path in content_dir.glob("*.yaml") if path.is_file())


def choose_story_path(argument: str | None, content_dir: Path = CONTENT_DIR) -> Path | None:
    """Resolve an explicit story path or ask the player to choose discovered content."""
    if argument:
        return Path(argument)

    candidates = discover_story_paths(content_dir)
    if not candidates:
        print(f"（未在 {content_dir} 中发现故事 YAML；请在命令行指定故事文件。）")
        return None
    if len(candidates) == 1:
        return candidates[0]

    print("请选择要游玩的故事：")
    for index, path in enumerate(candidates, start=1):
        try:
            title = Story.load(path).title
        except (OSError, ValueError):
            title = path.stem
        print(f"[{index}] {title}（{path}）")

    while True:
        answer = read_line("故事编号（q=退出）：")
        if answer is None or answer.lower() in ("q", "quit", "exit"):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print("（请输入列表中的故事编号。）")


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
        result = session.resolve(
            quote_id=quote["quote_id"],
            _director_provider=provider,
        )
        print_turn(session, provider, recorder, result)
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Play an AIRPG story in the terminal.")
    parser.add_argument(
        "story",
        nargs="?",
        help="故事 YAML 路径；省略时从 content 目录发现并选择。",
    )
    parser.add_argument("--no-log", action="store_true", help="Do not write session logs.")
    parser.add_argument(
        "--llm",
        choices=["mock", "deepseek", "off"],
        default="mock",
        help="自由方案（custom）的理解 provider；deepseek 需设置 DEEPSEEK_API_KEY；off 退回结构化对象输入。",
    )
    args = parser.parse_args()

    story_path = choose_story_path(args.story)
    if story_path is None:
        return 0
    try:
        story = Story.load(story_path)
    except (OSError, ValueError) as exc:
        print(f"（无法载入故事：{exc}）")
        return 1
    session = GameSession(story, log_dir=None if args.no_log else "data/sessions")
    try:
        provider = create_provider(args.llm) if args.llm != "off" else None
    except LLMProviderError as exc:
        print(f"（{exc}）")
        return 1
    recorder = TraceRecorder(None if args.no_log else "data/traces", session.session_id)
    suggestion_set = None

    print(render_intro(story, session.state))
    print()
    print_help()

    last_scene = None
    while not session.is_over:
        if (
            suggestion_set is not None
            and suggestion_set.perception_revision != session.state_revision
        ):
            suggestion_set = None
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
            print("（本次游玩结束。）")
            break
        if not line:
            continue
        if line.lower() in ("help", "h", "?"):
            print_help()
            continue
        if line.lower() in ("undo", "撤回", "回退"):
            try:
                restored = session.undo()
            except SessionError as exc:
                print(f"（{exc}。）")
            else:
                recorder.record("state_branch_created", {
                    "source_checkpoint_id": restored.source_checkpoint_id,
                    "restored_checkpoint_id": restored.restored_checkpoint_id,
                    "branch_id": restored.branch_id,
                    "state_revision": restored.state_revision,
                    "turn": restored.turn_no,
                })
                last_scene = None
                print(
                    f"（已撤回到 {restored.restored_checkpoint_id}，并创建 "
                    f"{restored.branch_id}；原历史仍保留。）"
                )
                suggestion_set = None
            continue
        if line.lower() in ("timeline", "branches", "时间线", "分支"):
            print_timeline(session)
            continue
        if line.lower() in ("ideas", "suggestions", "提案", "建议"):
            if provider is None:
                print("（当前未启用 LLM，无法生成动态行动提案。）")
                continue
            try:
                suggestion_set = generate_action_suggestions(
                    session, provider, recorder
                )
            except (LLMProviderError, SessionError) as exc:
                print(f"（行动提案暂时不可用：{exc}。）")
            else:
                print_suggestions(suggestion_set)
            continue
        suggestion_match = re.match(
            r"^(?:idea|suggestion|提案|建议)\s*(\d+)$",
            line,
            re.IGNORECASE,
        )
        if suggestion_match:
            if suggestion_set is None:
                print("（请先输入 ideas 生成当前局势的行动提案。）")
                continue
            index = int(suggestion_match.group(1)) - 1
            if not 0 <= index < len(suggestion_set.actions):
                print("（提案编号不在当前列表中。）")
                continue
            suggestion = suggestion_set.actions[index]
            try:
                loop_result = prepare_suggested_action(session, suggestion)
                recorder.record("suggestion_selected", {
                    "turn": session.turn_no + 1,
                    "suggestion_set_id": suggestion_set.suggestion_set_id,
                    "suggestion_id": suggestion.suggestion_id,
                    "plan_id": suggestion.plan.plan_id,
                    "state_revision": session.state_revision,
                })
                committed = execute_prepared_action(
                    session,
                    provider,
                    recorder,
                    loop_result,
                    suggestion.action_text,
                )
                if committed:
                    suggestion_set = None
            except (LLMProviderError, SessionError) as exc:
                print(f"（提案无法执行：{exc}。）")
                suggestion_set = None
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
        if line.lower() in ("facts", "clues"):
            from server.engine.perception import perception_config

            label = perception_config(story)["facts_label"]
            for fact in session.state["facts"] or [f"（还没有{label}）"]:
                print(f"◇ {fact}")
            continue

        # A clarification reply is always free text.  Route it before the
        # legacy intent parser so answers such as "用发簪" are not rejected as
        # unknown commands.
        if provider is not None and session.pending_clarification is not None:
            try:
                handle_free_action(session, provider, recorder, line)
            except LLMProviderError as exc:
                print(f"（理解服务暂时不可用：{exc}；可用结构化输入继续，或稍后重试。）")
            except SessionError as exc:
                print(f"({exc})")
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
            # In LLM mode an unmatched line is a natural-language action, not
            # a malformed stage-2 command.  Structured mode retains the old
            # explicit error as its deterministic fallback interface.
            if provider is not None:
                try:
                    handle_free_action(session, provider, recorder, line)
                except LLMProviderError as exc:
                    print(f"（理解服务暂时不可用：{exc}；可用结构化输入继续，或稍后重试。）")
                except SessionError as exc:
                    print(f"({exc})")
                continue
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
                result = session.resolve(
                    intent_id=intent_id,
                    objects=objects,
                    _director_provider=provider,
                    _player_action=line,
                )
                print_turn(session, provider, recorder, result)
        except SessionError as exc:
            print(f"({exc})")
        except LLMProviderError as exc:
            print(f"（叙事服务暂时不可用，已用模板文本继续：{exc}）")

    if session.ending:
        print()
        print(render_ending(story, session.ending))
        from server.engine.perception import perception_config

        closing = [f"本局回合数：{session.turn_no}"]
        config = perception_config(story)
        for key, label in config["world_state"].items():
            value = session.state["world"].get(key)
            if value is not None:
                closing.append(f"{label}：{value}")
        print(f"（{'，'.join(closing)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
