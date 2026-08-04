"""Kimi (Moonshot AI) implementation of the narrative-first provider surface.

Kimi 的 chat completions 接口与 OpenAI SDK 兼容（见
https://platform.kimi.com/docs/api/chat ），prompt、重试与解析逻辑与
DeepSeek provider 完全共享；差异只在端点、默认模型、认证环境变量，
以及各模型系列对 thinking / 温度参数的约束：

- kimi-k2.x：支持 ``thinking: {"type": "enabled"|"disabled"}``，温度固定不可修改；
- kimi-k3：始终推理，用 ``reasoning_effort``（low/high/max）代替 thinking 开关；
- moonshot-v1：无思考模式，接受可修改的 temperature。
"""

from __future__ import annotations

import os
from typing import Any

from .llm import LLMProviderError
from .llm_deepseek import (
    FACT_EXTRACTION_PROMPT_VERSION,
    MEMORY_COMPACTION_PROMPT_VERSION,
    NARRATIVE_PROMPT_VERSION,
    PROSE_EDIT_PROMPT_VERSION,
    SUGGESTION_PROMPT_VERSION,
    DeepSeekCallPolicy,
    DeepSeekProvider,
    Transport,
)


DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MODEL = "kimi-k3"


def _kimi_prompt_version(deepseek_version: str) -> str:
    """Shared prompt templates keep one version number across providers."""
    return deepseek_version.replace("deepseek-", "kimi-", 1)


class KimiProvider(DeepSeekProvider):
    name = "kimi"
    display_name = "Kimi"
    narrative_prompt_version = _kimi_prompt_version(NARRATIVE_PROMPT_VERSION)
    prose_edit_prompt_version = _kimi_prompt_version(PROSE_EDIT_PROMPT_VERSION)
    suggestion_prompt_version = _kimi_prompt_version(SUGGESTION_PROMPT_VERSION)
    fact_extraction_prompt_version = _kimi_prompt_version(
        FACT_EXTRACTION_PROMPT_VERSION
    )
    memory_compaction_prompt_version = _kimi_prompt_version(
        MEMORY_COMPACTION_PROMPT_VERSION
    )

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        transport: Transport | None = None,
    ) -> None:
        api_key = (
            api_key
            or os.environ.get("KIMI_API_KEY")
            or os.environ.get("MOONSHOT_API_KEY")
        )
        if transport is None and not api_key:
            raise LLMProviderError(
                "缺少 Kimi API key：请设置环境变量 KIMI_API_KEY（或 MOONSHOT_API_KEY）"
            )
        super().__init__(
            api_key=api_key,
            model=model or os.environ.get("KIMI_MODEL", DEFAULT_MODEL),
            base_url=base_url or os.environ.get("KIMI_BASE_URL", DEFAULT_BASE_URL),
            temperature=temperature,
            max_tokens=max_tokens,
            transport=transport,
        )

    def _completion_kwargs(
        self,
        messages: list[dict[str, str]],
        *,
        policy: DeepSeekCallPolicy,
        json_mode: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": policy.max_tokens,
        }
        if self.model.startswith("kimi-k3"):
            # K3 无法关闭推理；非推理型 policy 降级为最省的 low。
            kwargs["reasoning_effort"] = (
                policy.reasoning_effort
                if policy.thinking == "enabled" and policy.reasoning_effort
                else "low"
            )
        elif self.model.startswith("moonshot-v1"):
            if policy.temperature is not None:
                kwargs["temperature"] = policy.temperature
        else:
            kwargs["extra_body"] = {"thinking": {"type": policy.thinking}}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs
