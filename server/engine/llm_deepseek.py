"""DeepSeek implementation of the narrative-first provider surface."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal, Union

from .llm import (
    FactExtractionRequest,
    FactExtractionResponse,
    LLMProvider,
    LLMProviderError,
    MemoryCompactionRequest,
    MemoryCompactionResponse,
    NarrativeRequest,
    NarrativeResponse,
    SuggestionRequest,
    SuggestionResponse,
)
from .memory import MemoryDigest, MemoryNoteGroup
from .llm_protocol import (
    CharacterMoveFact,
    FactExtraction,
    ItemPlacement,
    ItemTransferFact,
    SuggestedAction,
    new_protocol_id,
)


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
NARRATIVE_PROMPT_VERSION = "deepseek-narrate-v10"
SUGGESTION_PROMPT_VERSION = "deepseek-suggestions-v5"
FACT_EXTRACTION_PROMPT_VERSION = "deepseek-fact-extraction-v2"
MEMORY_COMPACTION_PROMPT_VERSION = "deepseek-memory-compaction-v2"
MAX_REPAIR_ROUNDS = 1


@dataclass(frozen=True)
class DeepSeekCallPolicy:
    """Explicit generation settings for one provider capability."""

    capability: str
    thinking: Literal["enabled", "disabled"]
    max_tokens: int
    json_mode: bool
    temperature: float | None = None
    reasoning_effort: Literal["high", "max"] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "json_mode": self.json_mode,
        }


@dataclass(frozen=True)
class DeepSeekCallResult:
    """Transport-neutral completion envelope used by production and tests."""

    content: str = ""
    reasoning_content: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


NARRATIVE_CALL_POLICY = DeepSeekCallPolicy(
    capability="narration",
    thinking="disabled",
    temperature=0.7,
    max_tokens=900,
    json_mode=False,
)
SUGGESTION_CALL_POLICY = DeepSeekCallPolicy(
    capability="suggestions",
    thinking="disabled",
    temperature=0.3,
    max_tokens=2048,
    json_mode=True,
)
FACT_EXTRACTION_CALL_POLICY = DeepSeekCallPolicy(
    capability="fact_extraction",
    thinking="enabled",
    reasoning_effort="high",
    max_tokens=1200,
    json_mode=True,
)
MEMORY_COMPACTION_CALL_POLICY = DeepSeekCallPolicy(
    capability="memory_compaction",
    thinking="disabled",
    temperature=0.1,
    max_tokens=800,
    json_mode=True,
)
TransportResult = Union[DeepSeekCallResult, tuple[str, dict[str, Any]]]
Transport = Callable[[list[dict[str, str]], dict[str, Any]], TransportResult]


RENDER_SYSTEM_PROMPT = """\
你是互动叙事游戏的旁白。根据【事实清单】写出刚发生的一回合。

写作要求：
1. 自然承接玩家行动、当前感知和软叙事记忆；可以补充不改变连续性的动作、对白和感官细节。
2. 不使用远处人物，也不替玩家决定下一步行动。
3. 【作者私有上下文】是故事蓝图与在场人物的私有人物卡，仅用于扮演人物和把握长线方向；它不代表玩家已知。在场人物按各自的动机、压力、口吻和行为模式行动；人物秘密可以驱动回避、迟疑或撒谎，但在玩家尚未探明前不得直接说破，也不得写成玩家已知的事实。例外是 opening_narration：它是已经展示给玩家的开场正文，续写时自然承接它的文风、节奏和既定事实，不要复述它。
4. 【作者私有上下文】中的 critical_reminders 是作者最高优先级的少量规则，每回合都必须遵守。
5. candidate_modules 是当前可选的剧情素材：只在自然贴合玩家行动时编织其钩子，一回合至多推进一个；玩家忽略过的钩子（offers_count 大于 0）应换一种更轻的方式或干脆不提；不得强推模块，也不得替玩家接受钩子。
6. 人物换场或关键物品转手时写清楚实际发生的变化，不跳过当前在场和物品归属。
7. 若事实清单含冲突反馈，修正冲突部分，不在正文解释校验过程。
8. 服从给定视角与文风；玩家视角使用第二人称“你”。
9. 根据剧情密度输出一段 150-500 字的连贯散文；对话回合让在场人物充分表达。不提协议、阶段、数值或系统；只输出叙事文本。
10. 【软叙事记忆】中的未压缩事件是本故事刚刚发生的原文，你的输出必须像同一篇小说的下一段那样承接最近一回合的收尾。小结与笔记可能有概括误差；当前事实清单与当前感知优先。开放事项、提议和人物小结不能被擅自写成已经完成的事实，记忆中的远处人物也不算当前在场。
"""


SUGGESTION_SYSTEM_PROMPT = """\
你是互动叙事游戏的行动提案器。这是一个连续的故事：【软叙事记忆】中最近一回合的结尾就是当前时刻，每张行动卡都是玩家在这个时刻的下一步。先读懂最近发生了什么，再生成不同侧重点的可编辑行动卡，并输出 json。

提案要求：
1. 每张卡都必须像同一篇小说的下一句那样自然承接最近一回合的收尾；侧重差异体现在玩家选择哪个方向回应当下，而不是脱离当前情节另起炉灶。
2. 只能引用感知快照中可见的人物、物品、环境和出口；不得利用隐藏事实。
3. 卡片只写玩家准备说什么或做什么，不保证尚未提交的结果。
4. 不得创建人物或替玩家宣告尚未发生的结果。
5. action_text 使用第一人称自然语言，可由玩家直接采用或任意改写。
6. 卡片之间应在目标、方式或侧重点上有实质差异；没有合适卡片时宁可少给。
7. 不得输出 plan、steps、capability、intent、状态 patch 或验证结果。
8. 只输出 {"suggestions": [...]}。
9. 【软叙事记忆】的小结部分用于延续开放事项、避免重复已解决内容，但当前感知优先；记忆不能让远处人物、旧物品或旧出口变成当前可行动对象。
10. 【故事方向】是脱敏后的作者蓝图，只用于让提案贴近长线目标与情绪基调；不得引用其中未出现在感知快照里的人物、地点或物品，也不得把方向中的事件写成已经发生。

单张卡格式：
{
  "title": "短标题",
  "action_text": "我……",
  "focus": "practical|social|investigate|cautious|creative",
  "rationale": "为什么这个提案适合当前可见局面"
}
"""


FACT_EXTRACTION_SYSTEM_PROMPT = """\
你是互动叙事引擎的物理事实抽取器。你不续写故事，只从【本回合散文】抽取已经明确发生的人物换场和关键物品转手，并输出 json。

硬性规则：
1. 只抽取散文明确宣告已经完成的变化；“想、问、尝试、准备、可能、拒绝”不算完成。
2. 每条 evidence 必须逐字复制本回合散文中的一个非空连续片段。
3. 只能使用账本给出的人物、地点、物品和当前放置；不得创造实体。
4. item_transfer 必须同时抄写账本中的 from_placement，并写出散文明确完成的 to_placement。
5. 对话、承诺、秘密、态度、关系、情绪和普通动作不属于物理事实，不要抽取。
6. 没有人物换场或关键物品转手时返回空 facts；不得输出状态 patch。
7. 只输出 {"facts": [...]}。

支持的事实格式：
- {"kind":"character_move","actor_id":"...","destination_id":"...","evidence":"原文"}
- {"kind":"item_transfer","item_id":"...","from_placement":{"type":"carried_by|board","id":"..."},"to_placement":{"type":"carried_by|board","id":"..."},"evidence":"原文"}
"""


MEMORY_COMPACTION_SYSTEM_PROMPT = """\
你是互动叙事的软记忆编辑器。把【旧小结】和【待压缩的已提交回合】整理成新的叙事小结，并输出 json。

要求：
1. 只整理输入中已经出现的内容，不补写秘密、动机、因果或结果。
2. 保留“打算、猜测、拒绝、尚未确定”等不确定性，不把提议写成已完成事实。
3. 新回合明确纠正旧小结时，以新回合为准。
4. open_loops 只保存仍需后续承接的事项；已解决内容可放入 recently_resolved。
5. character_notes 和 scene_notes 只能使用给定 catalog 中的机器 ID。
6. module_updates 只在待压缩回合明确显示时标注：玩家清楚接住了某个钩子填 engaged；该线索已明确收尾填 resolved；玩家明确回绝或彻底翻篇填 dropped。只能使用 module_catalog 中的 ID；不确定时留空对象；不要把模块标题或内部状态写进 rolling_summary。
7. 所有字段都是软叙事记忆，不输出状态 patch、承诺 ID、生命周期或数值。
8. 只输出以下完整 json：
{
  "rolling_summary": "简洁连贯的故事回顾",
  "open_loops": ["仍待承接的事项"],
  "character_notes": {"character_id": ["人物目前表现出的立场或约定"]},
  "scene_notes": {"scene_id": ["场景中值得延续的叙事变化"]},
  "recently_resolved": ["最近已经解决、不要反复重提的事项"],
  "module_updates": {"module_id": "engaged|resolved|dropped"}
}
"""


def build_narrative_messages(request: NarrativeRequest) -> list[dict[str, str]]:
    payload = {
        "叙事类型": request.kind,
        "感知范围": {
            "audience": request.perception.audience.value,
            "subject_id": request.perception.subject_id,
            "state_revision": request.perception.state_revision,
        },
        "事实清单": request.facts,
        "作者私有上下文": (
            request.author_context.to_dict()
            if request.author_context is not None
            else None
        ),
        "软叙事记忆": (
            request.memory_context.to_dict()
            if request.memory_context is not None
            else None
        ),
        "文风约束": request.style,
    }
    return [
        {"role": "system", "content": RENDER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_suggestion_messages(request: SuggestionRequest) -> list[dict[str, str]]:
    perception = request.perception.to_dict()
    if request.memory_context is not None:
        perception.pop("recent_events", None)
    payload = {
        "数量上限": request.count,
        "感知快照": perception,
        "故事方向": request.story_direction or None,
        "软叙事记忆": (
            request.memory_context.to_dict()
            if request.memory_context is not None
            else None
        ),
        "世界边界": list(request.boundaries),
    }
    return [
        {"role": "system", "content": SUGGESTION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_fact_extraction_messages(
    request: FactExtractionRequest,
) -> list[dict[str, str]]:
    payload = {
        "状态版本": request.perception.state_revision,
        "玩家原话": request.player_text,
        "本回合散文": request.narrative,
        "物理账本": request.ledger,
    }
    return [
        {"role": "system", "content": FACT_EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_memory_compaction_messages(
    request: MemoryCompactionRequest,
) -> list[dict[str, str]]:
    payload = {
        "故事": request.story_id,
        "状态版本": request.state_revision,
        "旧小结": request.previous_digest.to_dict(),
        "待压缩的已提交回合": [event.to_dict() for event in request.events],
        "character_catalog": dict(request.character_catalog),
        "scene_catalog": dict(request.scene_catalog),
        "module_catalog": {
            module_id: {"title": title, "status": status}
            for module_id, title, status in request.module_catalog
        },
    }
    return [
        {"role": "system", "content": MEMORY_COMPACTION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _strip_fences(content: str) -> str:
    content = content.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", content, re.DOTALL)
    return match.group(1) if match else content


def _load_json_object(content: str) -> dict[str, Any]:
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("response must be a json object")
    return data


def _sanitize_machine_id(value: Any, fallback: str) -> str:
    machine_id = re.sub(r"[^a-z0-9_.-]+", "_", str(value or "").strip().lower())
    machine_id = machine_id.strip("_.-")
    if not machine_id or not machine_id[0].isalpha():
        machine_id = fallback
    return machine_id


def coerce_suggestions(
    content: str,
    request: SuggestionRequest,
) -> tuple[SuggestedAction, ...]:
    data = _load_json_object(content)
    if not isinstance(data.get("suggestions"), list):
        raise ValueError("suggestion response must contain a suggestions array")

    suggestions: list[SuggestedAction] = []
    for card in data["suggestions"][: request.count]:
        if not isinstance(card, dict):
            continue
        try:
            suggestion = SuggestedAction(
                suggestion_id=new_protocol_id("suggestion"),
                perception_revision=request.perception.state_revision,
                title=str(card.get("title") or "").strip(),
                action_text=str(card.get("action_text") or "").strip(),
                focus=_sanitize_machine_id(card.get("focus"), "creative"),
                rationale=str(card.get("rationale") or "").strip(),
            )
        except Exception:
            continue
        suggestions.append(suggestion)
    if not suggestions:
        raise ValueError("no valid suggestion cards")
    return tuple(suggestions)


def _coerce_item_placement(raw: Any) -> ItemPlacement:
    if not isinstance(raw, dict):
        raise ValueError("item placement must be an object")
    return ItemPlacement(type=raw.get("type"), id=raw.get("id"))


def coerce_fact_extraction(
    content: str,
    request: FactExtractionRequest,
) -> FactExtraction:
    data = _load_json_object(content)
    if not isinstance(data.get("facts"), list):
        raise ValueError("fact extraction response must contain a facts array")

    facts = []
    invalid_indexes: list[int] = []
    for index, raw in enumerate(data["facts"]):
        if not isinstance(raw, dict):
            invalid_indexes.append(index)
            continue
        kind = raw.get("kind")
        evidence = str(raw.get("evidence") or "").strip()
        try:
            if kind == "character_move":
                fact = CharacterMoveFact(
                    actor_id=str(raw.get("actor_id") or ""),
                    destination_id=str(raw.get("destination_id") or ""),
                    evidence=evidence,
                )
            elif kind == "item_transfer":
                fact = ItemTransferFact(
                    item_id=str(raw.get("item_id") or ""),
                    from_placement=_coerce_item_placement(raw.get("from_placement")),
                    to_placement=_coerce_item_placement(raw.get("to_placement")),
                    evidence=evidence,
                )
            else:
                invalid_indexes.append(index)
                continue
        except Exception:
            invalid_indexes.append(index)
            continue
        facts.append(fact)
    if invalid_indexes:
        joined = ", ".join(str(index) for index in invalid_indexes)
        raise ValueError(f"invalid extracted fact entries at indexes: {joined}")
    return FactExtraction(
        state_revision=request.perception.state_revision,
        facts=tuple(facts),
    )


def _coerce_text_list(raw: Any, *, limit: int) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    values: list[str] = []
    for item in raw:
        text = " ".join(str(item or "").split())
        if text and text not in values:
            values.append(text[:300])
        if len(values) >= limit:
            break
    return tuple(values)


def _coerce_note_groups(
    raw: Any,
    *,
    allowed_ids: set[str],
) -> tuple[MemoryNoteGroup, ...]:
    if not isinstance(raw, dict):
        return ()
    groups: list[MemoryNoteGroup] = []
    for subject_id, notes in raw.items():
        subject_id = str(subject_id)
        if subject_id not in allowed_ids:
            continue
        selected = _coerce_text_list(notes, limit=3)
        if selected:
            groups.append(MemoryNoteGroup(subject_id=subject_id, notes=selected))
    return tuple(groups)


def _coerce_module_updates(
    raw: Any,
    *,
    allowed_ids: set[str],
) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, dict):
        return ()
    allowed_statuses = {"engaged", "resolved", "dropped"}
    updates: list[tuple[str, str]] = []
    for module_id, status in raw.items():
        module_id = str(module_id)
        status = str(status or "").strip().lower()
        if module_id in allowed_ids and status in allowed_statuses:
            updates.append((module_id, status))
    return tuple(updates)


def coerce_memory_digest(
    content: str,
    request: MemoryCompactionRequest,
) -> MemoryDigest:
    data = _load_json_object(content)
    summary = " ".join(str(data.get("rolling_summary") or "").split())
    if not summary:
        raise ValueError("memory response needs a non-empty rolling_summary")
    return MemoryDigest(
        compacted_through_turn=request.events[-1].turn_no,
        rolling_summary=summary[:1200],
        open_loops=_coerce_text_list(data.get("open_loops"), limit=8),
        character_notes=_coerce_note_groups(
            data.get("character_notes"),
            allowed_ids={item[0] for item in request.character_catalog},
        ),
        scene_notes=_coerce_note_groups(
            data.get("scene_notes"),
            allowed_ids={item[0] for item in request.scene_catalog},
        ),
        recently_resolved=_coerce_text_list(
            data.get("recently_resolved"),
            limit=5,
        ),
        module_updates=_coerce_module_updates(
            data.get("module_updates"),
            allowed_ids={item[0] for item in request.module_catalog},
        ),
    )


class DeepSeekProvider(LLMProvider):
    name = "deepseek"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        transport: Transport | None = None,
    ) -> None:
        self.model = model or os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL)
        self.base_url = base_url or os.environ.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
        self.call_policies = {
            "narration": NARRATIVE_CALL_POLICY,
            "suggestions": replace(
                SUGGESTION_CALL_POLICY,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            "fact_extraction": FACT_EXTRACTION_CALL_POLICY,
            "memory_compaction": MEMORY_COMPACTION_CALL_POLICY,
        }
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._transport = transport
        self._client = None
        if transport is None and not self._api_key:
            raise LLMProviderError(
                "缺少 DeepSeek API key：请设置环境变量 DEEPSEEK_API_KEY"
            )

    @staticmethod
    def _normalize_transport_result(result: TransportResult) -> DeepSeekCallResult:
        if isinstance(result, DeepSeekCallResult):
            return DeepSeekCallResult(
                content=str(result.content or ""),
                reasoning_content=str(result.reasoning_content or ""),
                finish_reason=result.finish_reason,
                usage=dict(result.usage or {}),
            )
        content, usage = result
        return DeepSeekCallResult(
            content=str(content or ""),
            usage=dict(usage or {}),
        )

    def _call(
        self,
        messages: list[dict[str, str]],
        *,
        policy: DeepSeekCallPolicy,
        json_mode: bool | None = None,
    ) -> DeepSeekCallResult:
        effective_json_mode = policy.json_mode if json_mode is None else json_mode
        options: dict[str, Any] = {
            "model": self.model,
            **policy.to_dict(),
            "json_mode": effective_json_mode,
        }
        if self._transport is not None:
            return self._normalize_transport_result(self._transport(messages, options))
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise LLMProviderError(
                    "DeepSeek provider 需要 openai SDK：pip install openai"
                ) from exc
            self._client = OpenAI(api_key=self._api_key, base_url=self.base_url)
        kwargs: dict[str, Any] = {
            "model": options["model"],
            "messages": messages,
            "max_tokens": options["max_tokens"],
            "extra_body": {"thinking": {"type": policy.thinking}},
        }
        if policy.thinking == "disabled" and policy.temperature is not None:
            kwargs["temperature"] = policy.temperature
        if policy.thinking == "enabled" and policy.reasoning_effort is not None:
            kwargs["reasoning_effort"] = policy.reasoning_effort
        if effective_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(**kwargs)
        usage = (
            response.usage.model_dump(mode="json")
            if response.usage is not None
            else {}
        )
        if not response.choices:
            return DeepSeekCallResult(
                finish_reason="missing_choice",
                usage=usage,
            )
        choice = response.choices[0]
        return DeepSeekCallResult(
            content=str(choice.message.content or ""),
            reasoning_content=str(
                getattr(choice.message, "reasoning_content", "") or ""
            ),
            finish_reason=str(choice.finish_reason or "") or None,
            usage=usage,
        )

    @classmethod
    def _merge_usage(cls, total: dict[str, Any], usage: dict[str, Any]) -> None:
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0) + value
            elif isinstance(value, dict):
                nested = total.setdefault(key, {})
                if isinstance(nested, dict):
                    cls._merge_usage(nested, value)

    @staticmethod
    def _reasoning_tokens(usage: dict[str, Any]) -> int | float:
        details = usage.get("completion_tokens_details") or {}
        if isinstance(details, dict):
            value = details.get("reasoning_tokens")
            if isinstance(value, (int, float)):
                return value
        value = usage.get("reasoning_tokens")
        return value if isinstance(value, (int, float)) else 0

    def _new_diagnostics(self, policy: DeepSeekCallPolicy) -> dict[str, Any]:
        return {
            "capability": policy.capability,
            "model": self.model,
            "policy": policy.to_dict(),
            "attempts": [],
            "retry_reasons": [],
            "failed_usage": {},
            "total_usage": {},
            "final_content_state": "not_started",
        }

    def _record_attempt(
        self,
        diagnostics: dict[str, Any],
        result: DeepSeekCallResult,
        *,
        attempt: int,
        json_mode: bool,
    ) -> dict[str, Any]:
        usage = dict(result.usage or {})
        entry = {
            "attempt": attempt,
            "json_mode": json_mode,
            "finish_reason": result.finish_reason,
            "content_state": "present" if result.content.strip() else "empty",
            "content_chars": len(result.content),
            "reasoning_state": (
                "present" if result.reasoning_content.strip() else "empty"
            ),
            "reasoning_chars": len(result.reasoning_content),
            "reasoning_tokens": self._reasoning_tokens(usage),
            "usage": usage,
            "retry_reason": None,
        }
        diagnostics["attempts"].append(entry)
        return entry

    def _record_transport_error(
        self,
        diagnostics: dict[str, Any],
        *,
        attempt: int,
        json_mode: bool,
        error: Exception,
    ) -> None:
        entry = {
            "attempt": attempt,
            "json_mode": json_mode,
            "finish_reason": None,
            "content_state": "unavailable",
            "content_chars": 0,
            "reasoning_state": "unavailable",
            "reasoning_chars": 0,
            "reasoning_tokens": 0,
            "usage": {},
            "retry_reason": "transport_error",
            "error": str(error),
        }
        diagnostics["attempts"].append(entry)
        diagnostics["retry_reasons"].append("transport_error")
        diagnostics["final_content_state"] = "transport_error"

    def _mark_failed_attempt(
        self,
        diagnostics: dict[str, Any],
        entry: dict[str, Any],
        reason: str,
        *,
        detail: str | None = None,
    ) -> None:
        entry["retry_reason"] = reason
        if detail:
            entry["error"] = detail
        diagnostics["retry_reasons"].append(reason)
        self._merge_usage(diagnostics["failed_usage"], entry["usage"])

    @staticmethod
    def _empty_reason(result: DeepSeekCallResult, json_mode: bool) -> str:
        if result.finish_reason == "length":
            return "length_exhausted"
        if result.finish_reason and result.finish_reason != "stop":
            return f"empty_content_{result.finish_reason}"
        if json_mode:
            return "json_mode_empty"
        return "empty_content"

    @staticmethod
    def _finalize_diagnostics(
        diagnostics: dict[str, Any],
        usage_total: dict[str, Any],
        final_content_state: str,
    ) -> dict[str, Any]:
        diagnostics["total_usage"] = dict(usage_total)
        diagnostics["final_content_state"] = final_content_state
        return diagnostics

    def _run_structured(
        self,
        messages: list[dict[str, str]],
        *,
        policy: DeepSeekCallPolicy,
        coerce: Callable[[str], Any],
        repair_label: str,
        error_label: str,
        max_repair_rounds: int = MAX_REPAIR_ROUNDS,
    ) -> tuple[Any, str, dict[str, Any], dict[str, Any]]:
        usage_total: dict[str, Any] = {}
        diagnostics = self._new_diagnostics(policy)
        last_error = "empty_response"
        json_mode = policy.json_mode

        for attempt in range(1, max_repair_rounds + 2):
            try:
                result = self._call(
                    messages,
                    policy=policy,
                    json_mode=json_mode,
                )
            except Exception as exc:
                self._record_transport_error(
                    diagnostics,
                    attempt=attempt,
                    json_mode=json_mode,
                    error=exc,
                )
                self._finalize_diagnostics(
                    diagnostics, usage_total, "transport_error"
                )
                raise LLMProviderError(
                    f"DeepSeek {error_label}调用失败：{exc}",
                    diagnostics=diagnostics,
                ) from exc

            usage = dict(result.usage or {})
            self._merge_usage(usage_total, usage)
            entry = self._record_attempt(
                diagnostics,
                result,
                attempt=attempt,
                json_mode=json_mode,
            )

            if result.finish_reason == "length":
                last_error = "length_exhausted"
                self._mark_failed_attempt(diagnostics, entry, last_error)
            elif not result.content.strip():
                last_error = self._empty_reason(result, json_mode)
                self._mark_failed_attempt(diagnostics, entry, last_error)
            else:
                try:
                    parsed = coerce(result.content)
                except ValueError as exc:
                    last_error = "parse_error"
                    self._mark_failed_attempt(
                        diagnostics,
                        entry,
                        last_error,
                        detail=str(exc),
                    )
                else:
                    return (
                        parsed,
                        result.content,
                        usage_total,
                        self._finalize_diagnostics(
                            diagnostics, usage_total, "valid"
                        ),
                    )

            if attempt <= max_repair_rounds:
                if last_error == "json_mode_empty":
                    json_mode = False
                messages.append({
                    "role": "user",
                    "content": (
                        f"上一条{repair_label}无法使用（{last_error}）。"
                        "请只输出完整 json。"
                    ),
                })

        self._finalize_diagnostics(diagnostics, usage_total, last_error)
        raise LLMProviderError(
            f"DeepSeek {error_label}连续无法解析：{last_error}",
            diagnostics=diagnostics,
        )

    def render_narrative(self, request: NarrativeRequest) -> NarrativeResponse | None:
        started = time.monotonic()
        messages = build_narrative_messages(request)
        policy = self.call_policies["narration"]
        usage_total: dict[str, Any] = {}
        diagnostics = self._new_diagnostics(policy)
        last_error = "empty_content"
        for attempt in range(1, MAX_REPAIR_ROUNDS + 2):
            try:
                result = self._call(messages, policy=policy)
            except Exception as exc:
                self._record_transport_error(
                    diagnostics,
                    attempt=attempt,
                    json_mode=False,
                    error=exc,
                )
                self._finalize_diagnostics(
                    diagnostics, usage_total, "transport_error"
                )
                raise LLMProviderError(
                    f"DeepSeek 旁白调用失败：{exc}",
                    diagnostics=diagnostics,
                ) from exc
            usage = dict(result.usage or {})
            self._merge_usage(usage_total, usage)
            entry = self._record_attempt(
                diagnostics,
                result,
                attempt=attempt,
                json_mode=False,
            )
            if result.finish_reason == "length":
                last_error = "length_exhausted"
                self._mark_failed_attempt(diagnostics, entry, last_error)
            elif result.content.strip():
                return NarrativeResponse(
                    text=result.content.strip(),
                    model=self.model,
                    prompt_version=NARRATIVE_PROMPT_VERSION,
                    latency_ms=round((time.monotonic() - started) * 1000, 2),
                    usage=usage_total,
                    diagnostics=self._finalize_diagnostics(
                        diagnostics, usage_total, "valid"
                    ),
                )
            else:
                last_error = self._empty_reason(result, False)
                self._mark_failed_attempt(diagnostics, entry, last_error)
            if attempt <= MAX_REPAIR_ROUNDS:
                messages.append({
                    "role": "user",
                    "content": (
                        f"上一条叙事无法使用（{last_error}）。"
                        "请根据同一事实清单输出完整的 2-5 句叙事。"
                    ),
                })

        self._finalize_diagnostics(diagnostics, usage_total, last_error)
        raise LLMProviderError(
            f"DeepSeek 旁白连续无法生成：{last_error}",
            diagnostics=diagnostics,
        )

    def propose_suggestions(self, request: SuggestionRequest) -> SuggestionResponse:
        messages = build_suggestion_messages(request)
        started = time.monotonic()
        suggestions, content, usage, diagnostics = self._run_structured(
            messages,
            policy=self.call_policies["suggestions"],
            coerce=lambda raw: coerce_suggestions(raw, request),
            repair_label="提案",
            error_label="行动提案",
        )
        return SuggestionResponse(
            suggestions=suggestions,
            raw=content,
            model=self.model,
            prompt_version=SUGGESTION_PROMPT_VERSION,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            usage=usage,
            diagnostics=diagnostics,
        )

    def extract_facts(
        self,
        request: FactExtractionRequest,
    ) -> FactExtractionResponse:
        messages = build_fact_extraction_messages(request)
        started = time.monotonic()
        extraction, content, usage, diagnostics = self._run_structured(
            messages,
            policy=self.call_policies["fact_extraction"],
            coerce=lambda raw: coerce_fact_extraction(raw, request),
            repair_label="事实抽取",
            error_label="事实抽取",
        )
        return FactExtractionResponse(
            extraction=extraction,
            raw=content,
            model=self.model,
            prompt_version=FACT_EXTRACTION_PROMPT_VERSION,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            usage=usage,
            diagnostics=diagnostics,
        )

    def compact_memory(
        self,
        request: MemoryCompactionRequest,
    ) -> MemoryCompactionResponse:
        messages = build_memory_compaction_messages(request)
        started = time.monotonic()
        digest, content, usage, diagnostics = self._run_structured(
            messages,
            policy=self.call_policies["memory_compaction"],
            coerce=lambda raw: coerce_memory_digest(raw, request),
            repair_label="记忆小结",
            error_label="记忆小结",
            max_repair_rounds=0,
        )
        return MemoryCompactionResponse(
            digest=digest,
            raw=content,
            model=self.model,
            prompt_version=MEMORY_COMPACTION_PROMPT_VERSION,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            usage=usage,
            diagnostics=diagnostics,
        )
