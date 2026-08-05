"""Draft storage and content operations for the AIRPG story studio.

The studio deliberately keeps YAML behind this service boundary.  Browser
clients edit business objects represented as JSON; this module owns stable
story identity, normalized serialization, optimistic revisions, and reuse of
the runtime content validator.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tools.validate_content import validate_content


STORY_ID = re.compile(r"^[a-z][a-z0-9_]*$")
GENRE_SEPARATOR = " · "
MAX_GENRE_TAGS = 8
MAX_GENRE_TAG_LENGTH = 24


class StudioError(Exception):
    """A user-actionable story studio failure."""


class StoryNotFound(StudioError):
    pass


class RevisionConflict(StudioError):
    pass


class DuplicateStory(StudioError):
    pass


def _normalize_genre_tags(
    genre: Any,
    genre_tags: Any = None,
    *,
    allow_default: bool = False,
) -> list[str]:
    """Return stable, creator-facing classification tags.

    ``genre`` remains a readable compatibility summary.  ``genre_tags`` is the
    structured source that a future story catalog can filter without treating
    a creator's vocabulary as an engine enum.
    """
    if genre_tags is None:
        text = str(genre or "").strip()
        source = re.split(r"\s*(?:·|、|,|，|/|\|)\s*", text) if text else []
    elif isinstance(genre_tags, list):
        source = genre_tags
    else:
        raise StudioError("题材标签必须是一个列表")

    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in source:
        if not isinstance(raw_tag, str):
            raise StudioError("每个题材标签都必须是文字")
        tag = " ".join(raw_tag.strip().split())
        if not tag:
            continue
        if len(tag) > MAX_GENRE_TAG_LENGTH:
            raise StudioError(
                f"题材标签“{tag[:12]}…”不能超过 {MAX_GENRE_TAG_LENGTH} 个字符"
            )
        folded = tag.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        tags.append(tag)
    if len(tags) > MAX_GENRE_TAGS:
        raise StudioError(f"题材标签最多填写 {MAX_GENRE_TAGS} 个")
    if not tags:
        if allow_default:
            return ["未分类"]
        raise StudioError("请至少填写一个题材或类型标签")
    return tags


def _apply_genre_tags(
    data: dict[str, Any],
    *,
    allow_default: bool = False,
) -> None:
    tags = _normalize_genre_tags(
        data.get("genre"),
        data.get("genre_tags"),
        allow_default=allow_default,
    )
    data["genre"] = GENRE_SEPARATOR.join(tags)
    data["genre_tags"] = tags


def _catalog_genre_tags(data: dict[str, Any]) -> list[str]:
    """Keep one malformed imported draft from breaking the story catalog."""
    try:
        return _normalize_genre_tags(
            data.get("genre"),
            data.get("genre_tags"),
            allow_default=True,
        )
    except StudioError:
        legacy = str(data.get("genre") or "").strip()
        return [legacy] if legacy else ["未分类"]


@dataclass(frozen=True)
class StoredStory:
    story_id: str
    path: Path
    data: dict[str, Any]
    revision: str


def _revision(data: dict[str, Any]) -> str:
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _story_stats(data: dict[str, Any]) -> dict[str, int]:
    return {
        "characters": len(data.get("characters") or {}),
        "scenes": len(data.get("scenes") or {}),
        "items": len(data.get("items") or {}),
        "modules": len(data.get("modules") or {}),
    }


def _report_payload(data: dict[str, Any]) -> dict[str, Any]:
    report = validate_content(data)
    return {
        "errors": list(report.errors),
        "warnings": list(report.warnings),
        "playable": not report.errors,
    }


def _blank_story(
    story_id: str,
    title: str,
    genre: str,
    genre_tags: list[str],
    language: str,
) -> dict[str, Any]:
    """Return an intentionally incomplete but structurally editable draft."""
    return {
        "content_profile": "narrative_first",
        "schema_version": 2,
        "id": story_id,
        "title": title,
        "version": "0.1.0",
        "language": language,
        "genre": genre,
        "genre_tags": genre_tags,
        "premise": "",
        "player_role": {
            "id": "player",
            "name": "玩家",
            "public_identity": "",
            "private_goal": "",
            "constraints": [],
        },
        "style_bible": {},
        "global_rules": {},
        "characters": {
            "player": {
                "name": "玩家",
                "role": "player",
                "public_profile": "",
            }
        },
        "scenes": {
            "opening_scene": {
                "name": "起始场景",
                "purpose": "",
                "goal": "",
                "entry_text": "",
                "exits": [],
                "available_objects": {},
            }
        },
        "items": {},
        "initial_state": {
            "positions": {"player": "opening_scene"},
            "item_locations": {},
        },
    }


def _normalize_story(data: dict[str, Any], story_id: str) -> dict[str, Any]:
    """Normalize editor output without inventing authored prose."""
    normalized = copy.deepcopy(data)
    normalized["id"] = story_id
    normalized["content_profile"] = "narrative_first"
    normalized["schema_version"] = 2
    _apply_genre_tags(normalized)

    for key in ("characters", "scenes", "items", "style_bible", "global_rules"):
        if not isinstance(normalized.get(key), dict):
            normalized[key] = {}
    if not isinstance(normalized.get("initial_state"), dict):
        normalized["initial_state"] = {}
    initial = normalized["initial_state"]
    if not isinstance(initial.get("positions"), dict):
        initial["positions"] = {}
    if not isinstance(initial.get("item_locations"), dict):
        initial["item_locations"] = {}

    for optional_mapping in ("ai_plot", "modules", "openings"):
        if optional_mapping in normalized and not normalized.get(optional_mapping):
            normalized.pop(optional_mapping, None)
    for optional_list in ("narrative_guidelines", "critical_reminders"):
        if optional_list in normalized and not normalized.get(optional_list):
            normalized.pop(optional_list, None)
    return normalized


class StoryStudioWorkspace:
    """A content-directory workspace with optimistic, atomic draft saves."""

    def __init__(self, content_dir: str | Path) -> None:
        self.content_dir = Path(content_dir)
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir = self.content_dir / ".studio"
        self._lock = threading.RLock()

    def _active_stories(self) -> dict[str, Path]:
        stories: dict[str, Path] = {}
        for path in sorted(self.content_dir.glob("*.yaml")):
            if not path.is_file():
                continue
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("content_profile") != "narrative_first":
                continue
            story_id = str(data.get("id") or "")
            if STORY_ID.fullmatch(story_id):
                stories[story_id] = path
        return stories

    def _load_locked(self, story_id: str) -> StoredStory:
        path = self._active_stories().get(story_id)
        if path is None:
            raise StoryNotFound(f"找不到故事“{story_id}”")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise StudioError(f"故事无法读取：{exc}") from exc
        if not isinstance(data, dict):
            raise StudioError("故事内容必须是一个对象")
        return StoredStory(story_id, path, data, _revision(data))

    def load(self, story_id: str) -> dict[str, Any]:
        with self._lock:
            stored = self._load_locked(story_id)
            return self._detail_payload(stored)

    def list_stories(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for story_id in self._active_stories():
                stored = self._load_locked(story_id)
                report = _report_payload(stored.data)
                result.append({
                    "id": story_id,
                    "title": str(stored.data.get("title") or story_id),
                    "genre": str(stored.data.get("genre") or ""),
                    "genre_tags": _catalog_genre_tags(stored.data),
                    "version": str(stored.data.get("version") or ""),
                    "revision": stored.revision,
                    "stats": _story_stats(stored.data),
                    "error_count": len(report["errors"]),
                    "warning_count": len(report["warnings"]),
                    "playable": report["playable"],
                })
            return result

    def create(
        self,
        *,
        title: str,
        genre: str = "未分类",
        genre_tags: list[str] | None = None,
        language: str = "zh-CN",
    ) -> dict[str, Any]:
        title = title.strip()
        tags = _normalize_genre_tags(genre, genre_tags, allow_default=True)
        genre = GENRE_SEPARATOR.join(tags)
        language = language.strip() or "zh-CN"
        if not title:
            raise StudioError("故事名称不能为空")
        with self._lock:
            known = self._active_stories()
            for _ in range(16):
                story_id = f"story_{uuid.uuid4().hex[:8]}"
                if story_id not in known:
                    break
            else:  # pragma: no cover - effectively impossible
                raise DuplicateStory("无法生成唯一的故事标识")
            data = _blank_story(story_id, title, genre, tags, language)
            path = self.content_dir / f"{story_id}.yaml"
            self._write_locked(path, data)
            stored = StoredStory(story_id, path, data, _revision(data))
            self._write_review_locked(stored, {"fields": {}})
            return self._detail_payload(stored)

    def save(
        self,
        story_id: str,
        data: dict[str, Any],
        *,
        expected_revision: str,
        review_edits: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise StudioError("故事内容必须是一个对象")
        with self._lock:
            stored = self._load_locked(story_id)
            if expected_revision != stored.revision:
                raise RevisionConflict(
                    "故事已经在其他位置发生变化，请刷新后再保存"
                )
            normalized = _normalize_story(data, story_id)
            self._write_locked(stored.path, normalized)
            updated = StoredStory(
                story_id,
                stored.path,
                normalized,
                _revision(normalized),
            )
            review = self._read_review_locked(stored)
            fields = review.setdefault("fields", {})
            for edit in review_edits or []:
                path = str(edit.get("path") or "").strip()
                source = str(edit.get("source") or "").strip()
                status = str(edit.get("status") or "").strip()
                if not path or source not in {"manual", "llm", "imported"}:
                    continue
                if status not in {"manual", "unreviewed", "reviewed", "needs_review"}:
                    continue
                fields[path] = {
                    "source": source,
                    "status": status,
                    "operation": str(edit.get("operation") or "").strip() or None,
                    "updated_at": _now_iso(),
                }
            review["fields"] = {
                path: entry
                for path, entry in fields.items()
                if _path_exists(normalized, path)
            }
            self._write_review_locked(updated, review)
            return self._detail_payload(updated)

    def mark_reviewed(
        self,
        story_id: str,
        path: str,
        *,
        expected_revision: str,
    ) -> dict[str, Any]:
        path = path.strip()
        if not path:
            raise StudioError("审阅字段不能为空")
        with self._lock:
            stored = self._load_locked(story_id)
            if expected_revision != stored.revision:
                raise RevisionConflict(
                    "故事已经在其他位置发生变化，请刷新后再审阅"
                )
            review = self._read_review_locked(stored)
            entry = (review.get("fields") or {}).get(path)
            if not isinstance(entry, dict) or entry.get("source") != "llm":
                raise StudioError("这个字段没有待确认的 LLM 内容")
            entry["status"] = "reviewed"
            entry["reviewed_at"] = _now_iso()
            self._write_review_locked(stored, review)
            return self._review_payload(stored)

    def _write_locked(self, path: Path, data: dict[str, Any]) -> None:
        text = yaml.safe_dump(
            data,
            allow_unicode=True,
            sort_keys=False,
            width=100,
            default_flow_style=False,
        )
        self._atomic_write_text(path, text)

    def _atomic_write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _review_path(self, story_id: str) -> Path:
        return self.metadata_dir / f"{story_id}.json"

    def _read_review_locked(self, stored: StoredStory) -> dict[str, Any]:
        path = self._review_path(stored.story_id)
        if not path.exists():
            return {
                "story_id": stored.story_id,
                "content_revision": stored.revision,
                "default_source": "imported",
                "fields": {},
            }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        fields = data.get("fields")
        if not isinstance(fields, dict):
            fields = {}
        if data.get("content_revision") != stored.revision:
            for entry in fields.values():
                if not isinstance(entry, dict) or entry.get("source") != "llm":
                    continue
                if entry.get("status") == "reviewed":
                    entry["status"] = "needs_review"
        return {
            "story_id": stored.story_id,
            "content_revision": stored.revision,
            "default_source": str(data.get("default_source") or "imported"),
            "fields": fields,
        }

    def _write_review_locked(
        self,
        stored: StoredStory,
        review: dict[str, Any],
    ) -> None:
        payload = {
            "story_id": stored.story_id,
            "content_revision": stored.revision,
            "default_source": str(review.get("default_source") or "manual"),
            "fields": review.get("fields") or {},
        }
        self._atomic_write_text(
            self._review_path(stored.story_id),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def _review_payload(self, stored: StoredStory) -> dict[str, Any]:
        review = self._read_review_locked(stored)
        fields = review.get("fields") or {}
        unreviewed = [
            path
            for path, entry in fields.items()
            if isinstance(entry, dict)
            and entry.get("status") in {"unreviewed", "needs_review"}
        ]
        return {
            "default_source": review.get("default_source", "imported"),
            "fields": copy.deepcopy(fields),
            "unreviewed_paths": sorted(unreviewed),
            "unreviewed_count": len(unreviewed),
        }

    def _detail_payload(self, stored: StoredStory) -> dict[str, Any]:
        return {
            "id": stored.story_id,
            "revision": stored.revision,
            "story": copy.deepcopy(stored.data),
            "validation": _report_payload(stored.data),
            "stats": _story_stats(stored.data),
            "review": self._review_payload(stored),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path_exists(data: dict[str, Any], path: str) -> bool:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True
