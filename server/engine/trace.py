"""Append-only JSONL trace recording for generation and fact commits."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class TraceRecorder:
    def __init__(self, trace_dir: str | Path | None, session_id: str) -> None:
        self.path: Path | None = None
        if trace_dir is not None:
            directory = Path(trace_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self.path = directory / f"{session_id}.jsonl"

    def record(self, event: str, payload: dict[str, Any]) -> None:
        if self.path is None:
            return
        record = {"ts": round(time.time(), 3), "event": event, **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
