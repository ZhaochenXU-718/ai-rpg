#!/usr/bin/env python3
"""Minimal web client for AIRPG: stdlib HTTP + SSE, no new dependencies.

``WebGame`` is the transport-free core (testable without sockets); the
HTTP layer below it only translates requests into WebGame calls. One
server process hosts one session, mirroring the CLI. Narrative turns
stream over Server-Sent Events using the same ``NarrativeStream``
contract as the terminal client.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.cli import discover_story_paths
from server.engine.content import Story
from server.engine.fact_pipeline import resolve_player_turn
from server.engine.llm import (
    LLMProviderError,
    NarrativeStream,
    create_provider,
)
from server.engine.memory_pipeline import BackgroundMemoryCompactor
from server.engine.renderer import render_intro, render_turn_tail
from server.engine.session import GameSession, SessionError
from server.engine.suggestions import (
    generate_action_suggestions,
    prepare_suggested_action,
)
from server.engine.trace import TraceRecorder


ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = ROOT / "web" / "index.html"

Emit = Callable[[dict[str, Any]], None]


class _EmitterStream(NarrativeStream):
    """Bridge narrative streaming onto one turn's event emitter."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit

    def delta(self, text: str) -> None:
        self._emit({"type": "delta", "text": text})

    def restart(self, reason: str) -> None:
        self._emit({"type": "restart", "reason": reason})


class WebGame:
    """One playable session behind a lock; every public method is atomic.

    All session access is serialized by an RLock, so whichever request
    thread holds it is "the gameplay thread" — the background compactor's
    kick/poll contract stays satisfied.
    """

    def __init__(
        self,
        story: Story,
        provider,
        *,
        trace_dir: str | None = None,
        log_dir: str | None = None,
        opening_id: str | None = None,
    ) -> None:
        self.story = story
        self.provider = provider
        self.session = GameSession(
            story, log_dir=log_dir, opening_id=opening_id
        )
        self.recorder = TraceRecorder(trace_dir, self.session.session_id)
        self.compactor = BackgroundMemoryCompactor(provider, self.recorder)
        self._lock = threading.RLock()
        self._suggestions = None
        self._intro_blocks = self._build_intro_blocks()

    def _build_intro_blocks(self) -> tuple[str, ...]:
        blocks = [render_intro(self.story, self.session.state)]
        if self.session.opening_id:
            opening = self.story.openings.get(self.session.opening_id) or {}
            intro = str(opening.get("intro") or "").strip()
            if intro:
                blocks.append(intro)
        return tuple(blocks)

    def _snapshot_locked(self) -> dict[str, Any]:
        state = self.session.state
        scene_id = self.story.current_location(state)
        labels = self.story.item_labels()
        return {
            "story_title": self.story.title,
            "scene_id": scene_id,
            "scene_name": self.story.location_name(state, scene_id),
            "characters": [
                {
                    "id": character_id,
                    "name": (
                        self.story.characters.get(character_id) or {}
                    ).get("name", character_id),
                }
                for character_id in self.story.characters_at(state)
            ],
            "inventory": [
                labels.get(item, item) for item in self.story.inventory(state)
            ],
            "turn_no": self.session.turn_no,
            "state_revision": self.session.state_revision,
        }

    def _drop_stale_suggestions_locked(self) -> None:
        if (
            self._suggestions is not None
            and self._suggestions.perception_revision
            != self.session.state_revision
        ):
            self._suggestions = None

    def state(self) -> dict[str, Any]:
        """Bootstrap payload: status, intro and committed history."""
        with self._lock:
            self.compactor.poll(self.session)
            return {
                "snapshot": self._snapshot_locked(),
                "intro": list(self._intro_blocks),
                "history": [
                    {
                        "turn_no": event.turn_no,
                        "player_text": event.player_text,
                        "narrative": event.narrative,
                    }
                    for event in self.session.memory.events
                ],
            }

    def run_turn(self, player_text: str, emit: Emit) -> None:
        """Resolve one input, mirroring prose to ``emit`` as it streams."""
        with self._lock:
            self.compactor.poll(self.session)
            self._drop_stale_suggestions_locked()
            self.recorder.record("turn_input", {
                "turn": self.session.turn_no + 1,
                "state_revision": self.session.state_revision,
                "player_text": player_text,
            })
            try:
                result = resolve_player_turn(
                    self.session,
                    self.provider,
                    self.recorder,
                    player_text,
                    stream=_EmitterStream(emit),
                    compactor=self.compactor,
                )
            except (LLMProviderError, SessionError) as exc:
                emit({"type": "error", "message": f"本回合未提交：{exc}"})
                return
            except Exception as exc:  # 服务端异常不让 SSE 半途断开
                emit({"type": "error", "message": f"服务端错误：{exc}"})
                return
            emit({
                "type": "committed",
                "narrative": result.narrative.strip(),
                "tail": render_turn_tail(
                    self.story, result, self.session.state
                ),
                "snapshot": self._snapshot_locked(),
            })

    def ideas(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return current proposal cards; regenerate when asked or stale."""
        with self._lock:
            self.compactor.poll(self.session)
            self._drop_stale_suggestions_locked()
            if refresh or self._suggestions is None:
                self._suggestions = generate_action_suggestions(
                    self.session, self.provider, self.recorder
                )
            return {
                "suggestion_set_id": self._suggestions.suggestion_set_id,
                "actions": [
                    {
                        "suggestion_id": action.suggestion_id,
                        "title": action.title,
                        "action_text": action.action_text,
                        "focus": action.focus,
                        "rationale": action.rationale,
                    }
                    for action in self._suggestions.actions
                ],
            }

    def select_idea(self, suggestion_id: str, emit: Emit) -> None:
        """Execute one proposal card as the player's action."""
        with self._lock:
            suggestion_set = self._suggestions
            suggestion = next(
                (
                    action
                    for action in (
                        suggestion_set.actions if suggestion_set else ()
                    )
                    if action.suggestion_id == suggestion_id
                ),
                None,
            )
            if suggestion is None:
                emit({
                    "type": "error",
                    "message": "提案不在当前列表中，请重新生成提案",
                })
                return
            try:
                action_text = prepare_suggested_action(
                    self.session, suggestion
                )
            except SessionError as exc:
                emit({"type": "error", "message": str(exc)})
                return
            self.recorder.record("suggestion_selected", {
                "turn": self.session.turn_no + 1,
                "suggestion_set_id": suggestion_set.suggestion_set_id,
                "suggestion_id": suggestion.suggestion_id,
                "state_revision": self.session.state_revision,
            })
            self._suggestions = None
            emit({"type": "player_text", "text": action_text})
            self.run_turn(action_text, emit)


class _Handler(BaseHTTPRequestHandler):
    game: WebGame  # class attribute injected by serve()

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("request body must be a json object")
        return data

    def _sse(self, run: Callable[[Emit], None]) -> None:
        """Stream events; a vanished client must not abort the turn."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        client_gone = False

        def emit(event: dict[str, Any]) -> None:
            nonlocal client_gone
            if client_gone:
                return
            frame = (
                "data: "
                + json.dumps(event, ensure_ascii=False)
                + "\n\n"
            ).encode("utf-8")
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except OSError:
                # 客户端断开：回合继续在服务端完成并提交，历史可恢复。
                client_gone = True

        run(emit)

    def do_GET(self) -> None:
        if self.path in {"/", "/index.html"}:
            try:
                body = INDEX_PATH.read_bytes()
            except OSError:
                self._json(500, {"error": "web/index.html 缺失"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state":
            self._json(200, self.game.state())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            body = self._read_body()
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "请求体必须是 json 对象"})
            return
        if self.path == "/api/turn":
            text = str(body.get("text") or "").strip()
            if not text:
                self._json(400, {"error": "行动内容不能为空"})
                return
            self._sse(lambda emit: self.game.run_turn(text, emit))
            return
        if self.path == "/api/ideas":
            try:
                payload = self.game.ideas(refresh=bool(body.get("refresh")))
            except (LLMProviderError, SessionError) as exc:
                self._json(409, {"error": str(exc)})
                return
            self._json(200, payload)
            return
        if self.path == "/api/idea":
            suggestion_id = str(body.get("suggestion_id") or "")
            self._sse(
                lambda emit: self.game.select_idea(suggestion_id, emit)
            )
            return
        self._json(404, {"error": "not found"})


def choose_story_path(argument: str | None) -> Path | None:
    """Non-interactive story pick: explicit path, or the single active one."""
    if argument:
        return Path(argument)
    candidates = discover_story_paths()
    if not candidates:
        print("（未在 content 中发现 narrative_first 故事。）")
        return None
    if len(candidates) > 1:
        listing = "；".join(str(path) for path in candidates)
        print(f"（发现多个故事，请用参数指定其一：{listing}）")
        return None
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Serve the AIRPG web client for one story session."
    )
    parser.add_argument(
        "story",
        nargs="?",
        help="故事 YAML 路径；省略时使用 content 目录中唯一的活动故事。",
    )
    parser.add_argument("--llm", choices=["mock", "deepseek", "kimi"], default="mock")
    parser.add_argument("--opening", help="开场 ID；故事定义 openings 时可选。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args()

    story_path = choose_story_path(args.story)
    if story_path is None:
        return 1
    try:
        story = Story.load(story_path)
    except (OSError, ValueError) as exc:
        print(f"（无法载入故事：{exc}）")
        return 1
    if story.data.get("content_profile") != "narrative_first":
        print("（该内容是 pre-pivot 历史样本，不能由叙事优先运行时启动。）")
        return 1
    try:
        provider = create_provider(args.llm)
        game = WebGame(
            story,
            provider,
            trace_dir=None if args.no_log else "data/traces",
            log_dir=None if args.no_log else "data/sessions",
            opening_id=args.opening,
        )
    except (SessionError, LLMProviderError) as exc:
        print(f"（{exc}）")
        return 1

    _Handler.game = game
    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    server.daemon_threads = True
    print(f"《{story.title}》 http://{args.host}:{args.port} （Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n（本次游玩结束。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
