"""Per-turn JSONL session logging for the metrics in plan section 12."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class TurnLogger:
    def __init__(self, log_dir: str | Path | None, session_id: str) -> None:
        self.path: Path | None = None
        if log_dir is not None:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            self.path = directory / f"{session_id}.jsonl"

    def log(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        record = {"ts": round(time.time(), 3), **record}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
