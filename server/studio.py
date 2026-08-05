#!/usr/bin/env python3
"""Serve the AIRPG story studio and embedded author playtests."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.content import Story
from server.engine.llm import LLMProviderError, create_provider
from server.engine.session import SessionError
from server.studio_service import (
    RevisionConflict,
    StoryNotFound,
    StoryStudioWorkspace,
    StudioError,
)
from server.studio_llm import FieldAssistRequest, create_authoring_assistant
from server.web import WebGame


ROOT = Path(__file__).resolve().parent.parent
STUDIO_INDEX = ROOT / "web" / "studio.html"
PLAYER_INDEX = ROOT / "web" / "index.html"
Emit = Callable[[dict[str, Any]], None]


class StudioApplication:
    """Shared application state behind the HTTP transport."""

    def __init__(
        self,
        workspace: StoryStudioWorkspace,
        *,
        provider_name: str = "mock",
        no_log: bool = False,
    ) -> None:
        self.workspace = workspace
        self.provider_name = provider_name
        self.no_log = no_log
        self._games: dict[str, WebGame] = {}
        self._authoring_assistant = None
        self._lock = threading.RLock()

    def invalidate_playtest(self, story_id: str) -> None:
        with self._lock:
            self._games.pop(story_id, None)

    def start_playtest(self, story_id: str) -> dict[str, str]:
        detail = self.workspace.load(story_id)
        errors = detail["validation"]["errors"]
        if errors:
            raise StudioError(
                f"故事还有 {len(errors)} 个阻塞问题，修复后才能开始试玩"
            )
        provider = create_provider(self.provider_name)
        game = WebGame(
            Story(detail["story"]),
            provider,
            trace_dir=None if self.no_log else "data/traces",
            log_dir=None if self.no_log else "data/sessions",
        )
        with self._lock:
            self._games[story_id] = game
        return {"url": f"/play?api=/api/play/{story_id}"}

    def game(self, story_id: str) -> WebGame:
        with self._lock:
            game = self._games.get(story_id)
        if game is None:
            raise StudioError("试玩会话不存在或故事已经修改，请从工作室重新开始")
        return game

    def assist(self, story_id: str, body: dict[str, Any]) -> dict[str, Any]:
        detail = self.workspace.load(story_id)
        revision = str(body.get("revision") or "")
        if revision != detail["revision"]:
            raise RevisionConflict(
                "故事在请求创作建议前已经发生变化，请保存并重试"
            )
        path = str(body.get("path") or "").strip()
        value: Any = detail["story"]
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                value = ""
                break
            value = value[part]
        current_text = value if isinstance(value, str) else ""
        with self._lock:
            if self._authoring_assistant is None:
                self._authoring_assistant = create_authoring_assistant(
                    self.provider_name
                )
            assistant = self._authoring_assistant
        try:
            request = FieldAssistRequest(
                operation=str(body.get("operation") or ""),
                path=path,
                label=str(body.get("label") or ""),
                current_text=current_text,
                instruction=str(body.get("instruction") or ""),
                story=detail["story"],
            )
        except ValueError as exc:
            raise StudioError(str(exc)) from exc
        payload = assistant.assist(request).to_dict()
        payload["revision"] = revision
        payload["original_text"] = current_text
        return payload


class _StudioHandler(BaseHTTPRequestHandler):
    app: StudioApplication

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json(500, {"error": f"{path.name} 缺失"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
                "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            ).encode("utf-8")
            try:
                self.wfile.write(frame)
                self.wfile.flush()
            except OSError:
                client_gone = True

        run(emit)

    @staticmethod
    def _segments(path: str) -> list[str]:
        return [unquote(part) for part in path.split("/") if part]

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, StoryNotFound):
            self._json(404, {"error": str(exc)})
        elif isinstance(exc, RevisionConflict):
            self._json(409, {"error": str(exc), "code": "revision_conflict"})
        elif isinstance(exc, (StudioError, SessionError, LLMProviderError)):
            self._json(400, {"error": str(exc)})
        else:
            self._json(500, {"error": f"服务端错误：{exc}"})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        segments = self._segments(path)
        try:
            if path in {"/", "/studio", "/studio.html"}:
                self._html(STUDIO_INDEX)
                return
            if path in {"/play", "/play.html"}:
                self._html(PLAYER_INDEX)
                return
            if segments == ["api", "stories"]:
                self._json(200, {"stories": self.app.workspace.list_stories()})
                return
            if len(segments) == 3 and segments[:2] == ["api", "stories"]:
                self._json(200, self.app.workspace.load(segments[2]))
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "play"]
                and segments[3] == "state"
            ):
                self._json(200, self.app.game(segments[2]).state())
                return
            self._json(404, {"error": "not found"})
        except Exception as exc:  # HTTP boundary keeps errors user-actionable
            self._handle_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        segments = self._segments(parsed.path)
        try:
            body = self._read_body()
            if segments == ["api", "stories"]:
                detail = self.app.workspace.create(
                    title=str(body.get("title") or ""),
                    genre=str(body.get("genre") or "未分类"),
                    genre_tags=body.get("genre_tags"),
                    language=str(body.get("language") or "zh-CN"),
                )
                self._json(201, detail)
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "stories"]
                and segments[3] == "playtest"
            ):
                self._json(201, self.app.start_playtest(segments[2]))
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "stories"]
                and segments[3] == "assist"
            ):
                self._json(200, self.app.assist(segments[2], body))
                return
            if (
                len(segments) == 4
                and segments[:2] == ["api", "stories"]
                and segments[3] == "review"
            ):
                review = self.app.workspace.mark_reviewed(
                    segments[2],
                    str(body.get("path") or ""),
                    expected_revision=str(body.get("revision") or ""),
                )
                self._json(200, {"review": review})
                return
            if len(segments) == 4 and segments[:2] == ["api", "play"]:
                game = self.app.game(segments[2])
                action = segments[3]
                if action == "turn":
                    text = str(body.get("text") or "").strip()
                    if not text:
                        raise StudioError("行动内容不能为空")
                    self._sse(lambda emit: game.run_turn(text, emit))
                    return
                if action == "ideas":
                    self._json(
                        200,
                        game.ideas(refresh=bool(body.get("refresh"))),
                    )
                    return
                if action == "idea":
                    suggestion_id = str(body.get("suggestion_id") or "")
                    self._sse(
                        lambda emit: game.select_idea(suggestion_id, emit)
                    )
                    return
            self._json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "请求体必须是 JSON 对象"})
        except Exception as exc:
            self._handle_error(exc)

    def do_PUT(self) -> None:
        segments = self._segments(urlparse(self.path).path)
        try:
            body = self._read_body()
            if len(segments) == 3 and segments[:2] == ["api", "stories"]:
                story_id = segments[2]
                revision = str(body.get("revision") or "")
                story = body.get("story")
                if not isinstance(story, dict):
                    raise StudioError("story 必须是一个对象")
                detail = self.app.workspace.save(
                    story_id,
                    story,
                    expected_revision=revision,
                    review_edits=(
                        body.get("review_edits")
                        if isinstance(body.get("review_edits"), list)
                        else None
                    ),
                )
                self.app.invalidate_playtest(story_id)
                self._json(200, detail)
                return
            self._json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "请求体必须是 JSON 对象"})
        except Exception as exc:
            self._handle_error(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the AIRPG story studio.")
    parser.add_argument("--content-dir", default="content")
    parser.add_argument("--llm", choices=["mock", "deepseek", "kimi"], default="mock")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8643)
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args()

    workspace = StoryStudioWorkspace(args.content_dir)
    app = StudioApplication(
        workspace,
        provider_name=args.llm,
        no_log=args.no_log,
    )
    _StudioHandler.app = app
    server = ThreadingHTTPServer((args.host, args.port), _StudioHandler)
    server.daemon_threads = True
    print(f"AIRPG 故事工作室 http://{args.host}:{args.port} （Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n（故事工作室已关闭。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
