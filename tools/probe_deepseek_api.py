#!/usr/bin/env python3
"""Probe DeepSeek policies with synthetic prompts and sanitized output.

This intentionally sends no story, character, player, or repository prompt data.
It exercises the same model and call policies used by the AIRPG provider while
printing only response metadata, never response or reasoning content.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.engine.llm import LLMProviderError
from server.engine.llm_deepseek import (
    FACT_EXTRACTION_CALL_POLICY,
    MEMORY_COMPACTION_CALL_POLICY,
    NARRATIVE_CALL_POLICY,
    SUGGESTION_CALL_POLICY,
    DeepSeekCallPolicy,
    DeepSeekProvider,
)


POLICIES = (
    NARRATIVE_CALL_POLICY,
    SUGGESTION_CALL_POLICY,
    FACT_EXTRACTION_CALL_POLICY,
    MEMORY_COMPACTION_CALL_POLICY,
)


def _messages(policy: DeepSeekCallPolicy) -> list[dict[str, str]]:
    if policy.json_mode:
        return [
            {
                "role": "system",
                "content": "Return only one complete json object and no commentary.",
            },
            {
                "role": "user",
                "content": (
                    '{"ok":true,"probe":"'
                    + policy.capability
                    + '"}'
                ),
            },
        ]
    return [
        {"role": "system", "content": "Reply with exactly OK."},
        {"role": "user", "content": "OK"},
    ]


def _reasoning_tokens(usage: dict) -> int | float:
    details = usage.get("completion_tokens_details") or {}
    if isinstance(details, dict):
        value = details.get("reasoning_tokens")
        if isinstance(value, (int, float)):
            return value
    return 0


def main() -> int:
    try:
        provider = DeepSeekProvider()
    except LLMProviderError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1

    failed = False
    for policy in POLICIES:
        started = time.monotonic()
        try:
            result = provider._call(_messages(policy), policy=policy)
        except Exception as exc:
            failed = True
            payload = {
                "capability": policy.capability,
                "model": provider.model,
                "policy": policy.to_dict(),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        else:
            usage = dict(result.usage or {})
            payload = {
                "capability": policy.capability,
                "model": provider.model,
                "policy": policy.to_dict(),
                "finish_reason": result.finish_reason,
                "content_state": (
                    "present" if result.content.strip() else "empty"
                ),
                "content_chars": len(result.content),
                "reasoning_state": (
                    "present" if result.reasoning_content.strip() else "empty"
                ),
                "reasoning_chars": len(result.reasoning_content),
                "reasoning_tokens": _reasoning_tokens(usage),
                "usage": usage,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
            }
        print(json.dumps(payload, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
