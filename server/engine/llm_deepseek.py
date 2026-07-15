"""DeepSeek implementation of the narrative-first provider surface."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from .llm import (
    DirectorRequest,
    DirectorResponse,
    FactExtractionRequest,
    FactExtractionResponse,
    LLMProvider,
    LLMProviderError,
    NarrativeRequest,
    NarrativeResponse,
    SuggestionRequest,
    SuggestionResponse,
)
from .llm_protocol import (
    GENERATED_ENTITY_PREFIX,
    CharacterMoveFact,
    CommitmentFact,
    CommitmentUpdateFact,
    DirectorBeat,
    DirectorPlan,
    FactExtraction,
    ItemPlacement,
    ItemTransferFact,
    LocalCanonProposal,
    SecretDisclosureFact,
    SuggestedAction,
    new_protocol_id,
)


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
NARRATIVE_PROMPT_VERSION = "deepseek-narrate-v3"
SUGGESTION_PROMPT_VERSION = "deepseek-suggestions-v2"
DIRECTOR_PROMPT_VERSION = "deepseek-director-v3"
FACT_EXTRACTION_PROMPT_VERSION = "deepseek-fact-extraction-v1"
MAX_REPAIR_ROUNDS = 1

Transport = Callable[
    [list[dict[str, str]], dict[str, Any]],
    tuple[str, dict[str, Any]],
]


RENDER_SYSTEM_PROMPT = """\
你是互动叙事游戏的旁白。根据【事实清单】写出刚发生的一回合。

硬性规则：
1. 只能陈述事实清单中的人物、物品、地点与事件，不得补写未提供的秘密或结果。
2. 可以让行动成功、失败或得到人物回应；任何位置、物品、秘密或承诺变化都必须在散文中明确写出，不能含糊带过。
3. 不得跳过空间距离、物品当前归属或人物在场条件；若事实清单含冲突反馈，必须重写冲突部分。
4. 近期叙事只能作为背景，不得写成当前仍在发生。
5. 服从给定视角与文风；玩家视角使用第二人称“你”。
6. 输出 2-4 句连贯散文，不提协议、规则、阶段、数值或系统。
7. 只输出叙事文本本身。
"""


SUGGESTION_SYSTEM_PROMPT = """\
你是互动叙事游戏的行动提案器。根据当前感知快照生成不同侧重点的可编辑行动卡，并输出 json。

硬性规则：
1. 只能引用感知快照中可见的人物、物品、环境和出口；不得利用隐藏事实。
2. 卡片只写玩家准备说什么或做什么，不保证尚未提交的结果。
3. 不得创建人物、替 NPC 作承诺、宣告获得物品、抵达地点或披露秘密。
4. action_text 使用第一人称自然语言，可由玩家直接采用或任意改写。
5. 卡片之间应在目标、方式或侧重点上有实质差异；没有合适卡片时宁可少给。
6. 不得输出 plan、steps、capability、intent、状态 patch 或验证结果。
7. 只输出 {"suggestions": [...]}。

单张卡格式：
{
  "title": "短标题",
  "action_text": "我……",
  "focus": "practical|social|investigate|cautious|creative",
  "rationale": "为什么这个提案适合当前可见局面"
}
"""


DIRECTOR_SYSTEM_PROMPT = """\
你是互动叙事游戏的 Director。读取已提交回合、人物候选与生成边界，提出一个非权威 DirectorPlan，并输出 json。

硬性规则：
1. beats 只能使用候选列表中的 actor_id；不得创建、改名或暗示新人物。
2. status=present 的人物只能 react 或 advance_plan；status=adjacent 且 can_enter=true 的人物才可 enter_scene。
3. target_location_id 必须是当前地点；target_ids 只能引用输入已有 ID。
4. summary 是审计用候选摘要，只描述可见动作、神态、短对白或进场，不得擅自改变物品、秘密、承诺、关系、任务或玩家行动结果；当前保守 Director 不直接展示这段自由文本。
5. 每个人物最多一个节拍；没有必要就返回空 beats。
6. local_canon 仅在输入明确给出生成边界且预算有余量时可提议，最多一条；只能使用声明的原型，禁止创建人物。
7. 只输出 {"beats": [...], "local_canon": [...]}。

单个节拍：
{
  "kind": "enter_scene|react|advance_plan",
  "actor_id": "候选人物 ID",
  "target_location_id": "当前地点 ID",
  "target_ids": ["已有目标 ID"],
  "summary": "审计用的克制节拍摘要",
  "motivation": "如何服从人物既有动机"
}

单条 local_canon：
{
  "kind": "location|situation",
  "archetype_id": "生成边界中的原型 ID",
  "entity_id": "gen_ 开头的新 ID",
  "name": "简短名称",
  "description": "玩家可见的局部事实",
  "parent_location_id": "作者定义的地点 ID",
  "expires_after_turns": 3,
  "reason": "此刻需要该局部事实的原因"
}
"""


FACT_EXTRACTION_SYSTEM_PROMPT = """\
你是互动叙事引擎的事实抽取器。你不续写故事，只从【本回合散文】抽取已经明确发生的铁律事实并输出 json。

硬性规则：
1. 只抽取散文明确宣告已经完成的变化；“想、问、尝试、准备、可能、拒绝”不算完成。
2. 每条 evidence 必须逐字复制本回合散文中的一个非空连续片段。
3. 只能使用账本给出的 ID、位置、物品放置、秘密与已有承诺；不得创造人物、地点、物品或秘密。
4. 玩家提出请求不等于 NPC 承诺；只有人物明确答应未来要做某事才抽取 commitment。
5. item_transfer 必须同时抄写账本中的 from_placement，并写出散文明确完成的 to_placement。
6. commitment_update 只能引用账本中的已有 commitment_id；物品承诺的 fulfilled 必须同时有完成交付的 item_transfer。
7. 没有铁律变化时返回空 facts。不得输出普通情绪、动作、环境描写或状态 patch。
8. 只输出 {"facts": [...]}。

支持的事实格式：
- {"kind":"character_move","actor_id":"...","destination_id":"...","evidence":"原文"}
- {"kind":"item_transfer","item_id":"...","from_placement":{"type":"carried_by|board","id":"..."},"to_placement":{"type":"carried_by|board","id":"..."},"evidence":"原文"}
- {"kind":"secret_disclosure","secret_id":"...","owner_id":"...","disclosed_by_id":"...","audience_ids":["..."],"summary":"披露内容","evidence":"原文"}
- {"kind":"commitment","commitment_id":"稳定小写 ID","promisor_id":"...","promisee_id":"...","description":"承诺内容","related_item_id":"可选","due":"可选时间","evidence":"原文"}
- {"kind":"commitment_update","commitment_id":"...","status":"fulfilled|broken|cancelled","evidence":"原文"}
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
        "文风约束": request.style,
    }
    return [
        {"role": "system", "content": RENDER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_suggestion_messages(request: SuggestionRequest) -> list[dict[str, str]]:
    payload = {
        "数量上限": request.count,
        "感知快照": request.perception.to_dict(),
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
        "铁律账本": request.ledger,
    }
    return [
        {"role": "system", "content": FACT_EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_director_messages(request: DirectorRequest) -> list[dict[str, str]]:
    payload = {
        "故事": request.story_id,
        "状态版本": request.state_revision,
        "当前地点": {"id": request.location_id, "name": request.location_name},
        "当前目标": request.current_goal,
        "player_id": request.player_id,
        "玩家行动": request.player_action,
        "玩家行动目标": list(request.action_targets),
        "已提交回合": request.committed_turn,
        "候选人物": list(request.candidates),
        "世界边界": list(request.boundaries),
        "节拍上限": request.max_beats,
    }
    if request.generation:
        payload["生成边界"] = request.generation
    return [
        {"role": "system", "content": DIRECTOR_SYSTEM_PROMPT},
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


def _sanitize_state_key(value: Any, fallback: str) -> str:
    state_key = re.sub(r"[^a-z0-9_-]+", "_", str(value or "").strip().lower())
    state_key = state_key.strip("_-")
    if not state_key or not state_key[0].isalpha():
        state_key = fallback
    return state_key


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
            elif kind == "secret_disclosure":
                fact = SecretDisclosureFact(
                    secret_id=str(raw.get("secret_id") or ""),
                    owner_id=str(raw.get("owner_id") or ""),
                    disclosed_by_id=str(raw.get("disclosed_by_id") or ""),
                    audience_ids=tuple(
                        str(item) for item in raw.get("audience_ids") or []
                    ),
                    summary=str(raw.get("summary") or "").strip(),
                    evidence=evidence,
                )
            elif kind == "commitment":
                related_item = raw.get("related_item_id")
                fact = CommitmentFact(
                    commitment_id=_sanitize_state_key(
                        raw.get("commitment_id"), f"promise_{index + 1}"
                    ),
                    promisor_id=str(raw.get("promisor_id") or ""),
                    promisee_id=str(raw.get("promisee_id") or ""),
                    description=str(raw.get("description") or "").strip(),
                    related_item_id=(
                        str(related_item) if related_item not in (None, "") else None
                    ),
                    due=str(raw.get("due") or "").strip(),
                    evidence=evidence,
                )
            elif kind == "commitment_update":
                fact = CommitmentUpdateFact(
                    commitment_id=_sanitize_state_key(
                        raw.get("commitment_id"), f"promise_{index + 1}"
                    ),
                    status=raw.get("status"),
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


def _sanitize_generated_id(value: Any) -> str:
    entity_id = re.sub(r"[^a-z0-9_]", "_", str(value or "").strip().lower())
    if not entity_id:
        entity_id = new_protocol_id("gen")
    if not entity_id.startswith(GENERATED_ENTITY_PREFIX):
        entity_id = f"{GENERATED_ENTITY_PREFIX}{entity_id.lstrip('_')}"
    return entity_id


def _coerce_local_canon(
    data: dict[str, Any],
    request: DirectorRequest,
) -> tuple[LocalCanonProposal, ...]:
    if not request.generation:
        return ()
    proposals: list[LocalCanonProposal] = []
    for raw in (data.get("local_canon") or [])[:1]:
        if not isinstance(raw, dict):
            continue
        expires = raw.get("expires_after_turns")
        try:
            proposal = LocalCanonProposal(
                proposal_id=new_protocol_id("lcp"),
                state_revision=request.state_revision,
                kind=str(raw.get("kind") or ""),
                archetype_id=str(raw.get("archetype_id") or ""),
                entity_id=_sanitize_generated_id(raw.get("entity_id")),
                name=str(raw.get("name") or "").strip(),
                description=str(raw.get("description") or "").strip(),
                parent_location_id=str(
                    raw.get("parent_location_id") or request.location_id
                ),
                expires_after_turns=(
                    int(expires) if isinstance(expires, (int, float)) else None
                ),
                reason=str(raw.get("reason") or "").strip() or "Director 提议",
            )
        except Exception:
            continue
        proposals.append(proposal)
    return tuple(proposals)


def coerce_director_plan(content: str, request: DirectorRequest) -> DirectorPlan:
    data = _load_json_object(content)
    if not isinstance(data.get("beats"), list):
        raise ValueError("Director response must contain a beats array")

    beats: list[DirectorBeat] = []
    for raw in data["beats"][: request.max_beats]:
        if not isinstance(raw, dict):
            continue
        try:
            beat = DirectorBeat(
                beat_id=new_protocol_id("beat"),
                state_revision=request.state_revision,
                kind=raw.get("kind"),
                actor_id=str(raw.get("actor_id") or ""),
                target_location_id=str(raw.get("target_location_id") or ""),
                target_ids=tuple(str(item) for item in raw.get("target_ids") or []),
                summary=str(raw.get("summary") or "").strip(),
                motivation=str(raw.get("motivation") or "").strip(),
            )
        except Exception:
            continue
        beats.append(beat)
    if data["beats"] and not beats:
        raise ValueError("no valid Director beats")
    return DirectorPlan(
        state_revision=request.state_revision,
        beats=tuple(beats),
        local_canon=_coerce_local_canon(data, request),
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
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        self._transport = transport
        self._client = None
        if transport is None and not self._api_key:
            raise LLMProviderError(
                "缺少 DeepSeek API key：请设置环境变量 DEEPSEEK_API_KEY"
            )

    def _call(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        options = {
            "model": self.model,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "json_mode": json_mode,
        }
        if self._transport is not None:
            return self._transport(messages, options)
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
            "temperature": options["temperature"],
            "max_tokens": options["max_tokens"],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = self._client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        usage = response.usage.model_dump() if response.usage is not None else {}
        return content, usage

    @staticmethod
    def _merge_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
        for key, value in (usage or {}).items():
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0) + value

    def render_narrative(self, request: NarrativeRequest) -> NarrativeResponse | None:
        started = time.monotonic()
        messages = build_narrative_messages(request)
        usage_total: dict[str, Any] = {}
        content = ""
        for attempt in range(2):
            content, usage = self._call(
                messages,
                json_mode=False,
                temperature=0.7,
                max_tokens=400,
            )
            self._merge_usage(usage_total, usage)
            if content.strip():
                break
            if attempt == 0:
                messages.append({
                    "role": "user",
                    "content": "你返回了空内容。请根据同一事实清单输出 2-4 句叙事。",
                })
        if not content.strip():
            return None
        return NarrativeResponse(
            text=content.strip(),
            model=self.model,
            prompt_version=NARRATIVE_PROMPT_VERSION,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            usage=usage_total,
        )

    def propose_suggestions(self, request: SuggestionRequest) -> SuggestionResponse:
        messages = build_suggestion_messages(request)
        started = time.monotonic()
        usage_total: dict[str, Any] = {}
        last_error = "empty response"
        for _ in range(MAX_REPAIR_ROUNDS + 1):
            content, usage = self._call(messages)
            self._merge_usage(usage_total, usage)
            if not content.strip():
                last_error = "empty content"
            else:
                try:
                    suggestions = coerce_suggestions(content, request)
                except ValueError as exc:
                    last_error = str(exc)
                else:
                    return SuggestionResponse(
                        suggestions=suggestions,
                        raw=content,
                        model=self.model,
                        prompt_version=SUGGESTION_PROMPT_VERSION,
                        latency_ms=round((time.monotonic() - started) * 1000, 2),
                        usage=usage_total,
                    )
            messages.append({
                "role": "user",
                "content": f"上一条提案无法解析（{last_error}）。请只输出 suggestions json。",
            })
        raise LLMProviderError(f"DeepSeek 行动提案连续无法解析：{last_error}")

    def extract_facts(
        self,
        request: FactExtractionRequest,
    ) -> FactExtractionResponse:
        messages = build_fact_extraction_messages(request)
        started = time.monotonic()
        usage_total: dict[str, Any] = {}
        last_error = "empty response"
        for _ in range(MAX_REPAIR_ROUNDS + 1):
            content, usage = self._call(
                messages,
                temperature=0.0,
                max_tokens=900,
            )
            self._merge_usage(usage_total, usage)
            if not content.strip():
                last_error = "empty content"
            else:
                try:
                    extraction = coerce_fact_extraction(content, request)
                except ValueError as exc:
                    last_error = str(exc)
                else:
                    return FactExtractionResponse(
                        extraction=extraction,
                        raw=content,
                        model=self.model,
                        prompt_version=FACT_EXTRACTION_PROMPT_VERSION,
                        latency_ms=round((time.monotonic() - started) * 1000, 2),
                        usage=usage_total,
                    )
            messages.append({
                "role": "user",
                "content": f"上一条事实抽取无法解析（{last_error}）。请只输出 facts json。",
            })
        raise LLMProviderError(f"DeepSeek 事实抽取连续无法解析：{last_error}")

    def propose_director(self, request: DirectorRequest) -> DirectorResponse:
        messages = build_director_messages(request)
        started = time.monotonic()
        usage_total: dict[str, Any] = {}
        last_error = "empty response"
        for _ in range(MAX_REPAIR_ROUNDS + 1):
            content, usage = self._call(
                messages,
                temperature=0.5,
                max_tokens=700,
            )
            self._merge_usage(usage_total, usage)
            if not content.strip():
                last_error = "empty content"
            else:
                try:
                    plan = coerce_director_plan(content, request)
                except ValueError as exc:
                    last_error = str(exc)
                else:
                    return DirectorResponse(
                        plan=plan,
                        raw=content,
                        model=self.model,
                        prompt_version=DIRECTOR_PROMPT_VERSION,
                        latency_ms=round((time.monotonic() - started) * 1000, 2),
                        usage=usage_total,
                    )
            messages.append({
                "role": "user",
                "content": f"上一条 DirectorPlan 无法解析（{last_error}）。请只输出 json。",
            })
        raise LLMProviderError(f"DeepSeek Director 连续无法解析：{last_error}")
