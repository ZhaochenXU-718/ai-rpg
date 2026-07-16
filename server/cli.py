#!/usr/bin/env python3
"""Narrative-first terminal client for AIRPG."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.content import Story
from server.engine.fact_pipeline import resolve_player_turn
from server.engine.llm import LLMProviderError, create_provider
from server.engine.renderer import (
    render_characters,
    render_intro,
    render_memory,
    render_status,
    render_turn,
)
from server.engine.session import GameSession, SessionError
from server.engine.suggestions import (
    generate_action_suggestions,
    prepare_suggested_action,
)
from server.engine.trace import TraceRecorder

PROMPT = "> "
CONTENT_DIR = Path("content")


def read_line(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except EOFError:
        return None


def discover_story_paths(content_dir: Path = CONTENT_DIR) -> list[Path]:
    """Discover active narrative-first stories only."""
    if not content_dir.is_dir():
        return []
    paths: list[Path] = []
    for path in sorted(content_dir.glob("*.yaml")):
        if not path.is_file():
            continue
        try:
            story = Story.load(path)
        except (OSError, ValueError):
            continue
        if story.data.get("content_profile") == "narrative_first":
            paths.append(path)
    return paths


def choose_story_path(
    argument: str | None,
    content_dir: Path = CONTENT_DIR,
) -> Path | None:
    if argument:
        return Path(argument)
    candidates = discover_story_paths(content_dir)
    if not candidates:
        print(f"（未在 {content_dir} 中发现 narrative_first 故事。）")
        return None
    if len(candidates) == 1:
        return candidates[0]

    print("请选择要游玩的故事：")
    for index, path in enumerate(candidates, start=1):
        print(f"[{index}] {Story.load(path).title}（{path}）")
    while True:
        answer = read_line("故事编号（q=退出）：")
        if answer is None or answer.lower() in {"q", "quit", "exit"}:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print("（请输入列表中的故事编号。）")


def print_help() -> None:
    print(
        "直接输入自然语言行动；也可输入 ideas，再用 idea <编号> 采用一张提案卡。\n"
        "命令：who 在场人物；state 物理状态；memory 叙事记忆；memory raw 原始事件；"
        "undo 撤回并创建分支；timeline 查看分支；help 帮助；quit 退出。\n"
        "当前只对人物换场和关键物品转手做后验事实检查；普通对话、约定、"
        "关系和情绪保留在叙事记忆中，按需整理后供旁白和行动提案参考。"
        "冲突候选不会消耗回合。"
    )


def print_timeline(session: GameSession) -> None:
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


def print_suggestions(suggestion_set) -> None:
    print("【行动提案｜可完全忽略或改写】")
    for index, suggestion in enumerate(suggestion_set.actions, start=1):
        print(f"[{index}] {suggestion.title}（{suggestion.focus}）")
        print(f"    {suggestion.action_text}")
        print(f"    侧重点：{suggestion.rationale}")


def execute_narrative_turn(
    session: GameSession,
    provider,
    recorder: TraceRecorder,
    player_text: str,
) -> None:
    recorder.record("turn_input", {
        "turn": session.turn_no + 1,
        "state_revision": session.state_revision,
        "player_text": player_text,
    })
    result = resolve_player_turn(
        session,
        provider,
        recorder,
        player_text,
    )
    print(render_turn(session.story, result, session.state))


def main() -> int:
    parser = argparse.ArgumentParser(description="Play an AIRPG narrative-first story.")
    parser.add_argument(
        "story",
        nargs="?",
        help="故事 YAML 路径；省略时从 content 目录选择活动故事。",
    )
    parser.add_argument("--no-log", action="store_true", help="Do not write session logs.")
    parser.add_argument(
        "--llm",
        choices=["mock", "deepseek"],
        default="mock",
        help="叙事 provider；deepseek 需设置 DEEPSEEK_API_KEY。",
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
    if story.data.get("content_profile") != "narrative_first":
        print("（该内容是 pre-pivot 历史样本，不能由叙事优先运行时启动。）")
        return 1
    try:
        session = GameSession(
            story,
            log_dir=None if args.no_log else "data/sessions",
        )
        provider = create_provider(args.llm)
    except (SessionError, LLMProviderError) as exc:
        print(f"（{exc}）")
        return 1
    recorder = TraceRecorder(
        None if args.no_log else "data/traces",
        session.session_id,
    )
    suggestion_set = None

    print(render_intro(story, session.state))
    print()
    print_help()

    last_scene = None
    while True:
        if (
            suggestion_set is not None
            and suggestion_set.perception_revision != session.state_revision
        ):
            suggestion_set = None
        scene_now = story.current_location(session.state)
        print()
        if scene_now != last_scene:
            print("【在场人物】")
            print(render_characters(story, session.state))
            print()
            last_scene = scene_now
        print(render_status(story, session.state))

        line = read_line(PROMPT)
        if line is None or line.lower() in {"quit", "exit", "q"}:
            print("（本次游玩结束。）")
            break
        if not line:
            continue
        lowered = line.lower()
        if lowered in {"help", "h", "?"}:
            print_help()
            continue
        if lowered in {"undo", "撤回", "回退"}:
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
                print(
                    f"（已撤回到 {restored.restored_checkpoint_id} 并创建 "
                    f"{restored.branch_id}；原历史仍保留。）"
                )
                suggestion_set = None
                last_scene = None
            continue
        if lowered in {"timeline", "branches", "时间线", "分支"}:
            print_timeline(session)
            continue
        if lowered in {"memory", "记忆"}:
            print(render_memory(story, session.memory))
            continue
        if lowered in {"memory raw", "记忆 原文", "记忆 原始"}:
            print(render_memory(story, session.memory, raw=True))
            continue
        if lowered in {"ideas", "suggestions", "提案", "建议"}:
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
                action_text = prepare_suggested_action(session, suggestion)
                recorder.record("suggestion_selected", {
                    "turn": session.turn_no + 1,
                    "suggestion_set_id": suggestion_set.suggestion_set_id,
                    "suggestion_id": suggestion.suggestion_id,
                    "state_revision": session.state_revision,
                })
                execute_narrative_turn(
                    session, provider, recorder, action_text
                )
            except (LLMProviderError, SessionError) as exc:
                print(f"（提案无法执行：{exc}。）")
            suggestion_set = None
            continue
        if lowered == "who":
            print(render_characters(story, session.state))
            continue
        if lowered == "state":
            print(json.dumps(session.state, ensure_ascii=False, indent=2, default=str))
            continue
        try:
            execute_narrative_turn(session, provider, recorder, line)
        except (LLMProviderError, SessionError) as exc:
            print(f"（本回合未提交：{exc}。）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
