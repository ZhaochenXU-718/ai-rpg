"""DeepSeek provider for action plans, suggestions, narration and Director beats.

DeepSeek exposes an OpenAI-compatible API (https://api-docs.deepseek.com)
with JSON output mode, so the transport is the ``openai`` SDK pointed at
``https://api.deepseek.com``.  Known JSON-mode caveats handled here: the
prompt must contain the word "json", the API occasionally returns empty
content, and the model may wrap output in code fences — one repair round
retries with the parse error before giving up.

The provider only builds prompts and parses plans.  Perception filtering,
validation, clamping and commit stay in the engine; a hallucinated entity
or oversized proposal is rejected downstream like any other bad plan.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from .llm import (
    DirectorRequest,
    DirectorResponse,
    LLMProvider,
    LLMProviderError,
    NarrativeRequest,
    NarrativeResponse,
    PlanRequest,
    PlanResponse,
    SuggestionRequest,
    SuggestionResponse,
)
from .llm_protocol import (
    GENERATED_ENTITY_PREFIX,
    ActionPlan,
    CapabilityAction,
    DirectorBeat,
    LocalCanonProposal,
    RiskProposal,
    StateChangeProposal,
    SuggestedActionDraft,
    new_protocol_id,
)

DEFAULT_BASE_URL = "https://api.deepseek.com"
# deepseek-chat / deepseek-reasoner are deprecated on 2026-07-24; flash is
# the fast non-thinking tier, right for per-turn understanding latency.
DEFAULT_MODEL = "deepseek-v4-flash"
PROMPT_VERSION = "deepseek-plan-v1"
SUGGESTION_PROMPT_VERSION = "deepseek-suggestions-v1"
DIRECTOR_PROMPT_VERSION = "deepseek-director-v2"
MAX_REPAIR_ROUNDS = 1

# Transport: (messages, options) -> (content, usage). Injectable for tests.
Transport = Callable[[list[dict[str, str]], dict[str, Any]], tuple[str, dict[str, Any]]]

SYSTEM_PROMPT = """\
你是互动叙事游戏中的行动理解器。玩家用自然语言描述行动方案，你把它转成一个 json 行动计划（ActionPlan）。你不是裁判：计划能否执行、产生什么后果由游戏引擎判定。

硬性规则：
1. 只能引用【感知快照】中出现的实体 ID、意图和工具。玩家看不到的东西，你也不知道。
2. steps 恰好一个元素，必须从 capability_tools 复制 capability/action，并让 arguments 符合该工具的 arguments_schema。
3. 优先使用能直接表达玩家目标的专用能力，例如“向某人请求物品”应使用 social.request_item，而不是降级成 talk。只有没有专用能力时才从 available_intents 选择 intent 工具；"custom" 是最后手段。
4. proposed_changes 只能提议【可提议状态空间】patchable 里列出的路径：authority 一律 "soft_state"；数字用 operation "increment" 且幅度不超过该路径的 max_step，枚举值用 "set"。确定性能力（例如 social.request_item）的物品转移由引擎裁决，不得由模型提议。
5. 以下情况输出 needs_clarification=true、steps=[]，并在 clarification_question 里用世界内的口吻向玩家解释或提问：
   - 方案违反【世界边界】（例如凭空造物、离开被封锁的区域）——解释为什么行不通；
   - 目标或做法无法从文本中确定——问清楚。
6. 若有【上一轮验证反馈】，据此修正计划，不要重复同样的错误。
7. interpretation 用一两句话复述你对玩家意图的理解，玩家会据此确认；risks 用世界内语言描述可能的代价。
8. 只输出一个 json 对象，不要输出任何其它文本。

输出 json 格式：
{
  "interpretation": "对玩家意图的复述",
  "goal": "机器可读短目标，小写下划线",
  "intent_id": "所选意图 ID",
  "steps": [{"capability": "...", "action": "...", "arguments": {}, "purpose": "..."}],
  "references": ["涉及的实体 ID"],
  "proposed_changes": [{"path": "...", "operation": "increment", "value": 1, "authority": "soft_state", "reason": "..."}],
  "risks": [{"description": "...", "likelihood": "unlikely|possible|likely|certain"}],
  "assumptions": ["计划依赖但未证实的判断"],
  "confidence": 0.8,
  "needs_clarification": false,
  "clarification_question": null
}
"""


RENDER_SYSTEM_PROMPT = """\
你是互动叙事游戏的旁白。根据【事实清单】把刚发生的一回合写成叙事文本。

硬性规则：
1. 只能陈述事实清单里出现的事情，不得发明新的物品、人物、空间结构或事件。
2. 不得暗示或剧透事实清单之外的信息（未发现的秘密、他人的位置、结局走向）。
3. 若清单标注"本回合无预设事件"或"行动未执行"，如实写出尝试与落空，不得虚构成功或新发现。
4. 严格区分"玩家行动回应提示"、"世界节拍提示"和"世界反应提示"；世界节拍不是玩家行动直接造成的，不得写成玩家行动的成功结果。
5. "过去回合发生"只能作为背景，不能写成当前仍在发生；行动目标的位置描述必须服从事实清单。
6. 第二人称"你"，2-4 句，遵循【文风约束】；不要提及规则、数值或系统。
7. 只输出叙事文本本身。
"""


SUGGESTION_SYSTEM_PROMPT = """\
你是互动叙事游戏的行动提案器。根据玩家当前可见信息生成 3-5 张不同侧重点的玩家行动卡，并输出 json。

硬性规则：
1. 只能引用感知快照里的实体和 capability_tools；每张卡恰好使用一个工具，arguments 必须符合该工具 schema。
2. 卡片只描述玩家准备说什么或做什么，不保证尚未裁决的结果，不泄露具体隐藏物品、秘密或人物位置。
3. 不得创造新人物。人物进场和世界事件属于 Director Beat，不得伪装成玩家行动卡。
4. action_text 是可直接执行的第一人称行动或对白；plan.player_text 必须与其完全相同。
5. 每张卡应在工具、目标或侧重点上有实质差异；自由输入始终与卡片等价。
6. proposed_changes 只允许 soft_state；social.request_item 等确定性能力不要自行提议物品转移。
7. 只输出 {"suggestions": [...]}，不要输出其它文本。

单张卡格式：
{
  "title": "短标题",
  "action_text": "我……",
  "focus": "practical|social|investigate|cautious|creative",
  "rationale": "这一提案的侧重点",
  "plan": {
    "interpretation": "对行动的复述",
    "goal": "机器可读短目标",
    "steps": [{"capability": "...", "action": "...", "arguments": {}, "purpose": "..."}],
    "references": [],
    "proposed_changes": [],
    "risks": [],
    "assumptions": [],
    "confidence": 0.8
  }
}
"""


DIRECTOR_SYSTEM_PROMPT = """\
你是互动叙事游戏的 Director。玩家行动和确定性世界规则已经执行完毕；你只能从候选列表中选择 0-2 个既有人物，为同一次提交补充一个克制的进场、反应或计划节拍，并输出 json。

硬性规则：
1. 只能使用【候选人物】里的 actor_id，不得创造、改名或暗示任何新人物。
2. status=present 的人物只能 react 或 advance_plan；status=adjacent 且 can_enter=true 的人物才可 enter_scene。
3. target_location_id 必须等于当前地点；target_ids 只能引用输入中的 player_id、玩家行动目标或候选人物。
4. summary 只能描述当前可见的动作、神态、短对白或人物进场，不得发明物品、伤害、关系变化、秘密披露、任务完成或其他机械结果。
5. 人物行为应符合其 motivation、role 和当前局势。没有必要的介入就返回空 beats；不要为了热闹强行让人物说话或进场。
6. 每个人物最多一个节拍。你不是裁判，不能改变玩家行动结果。
7. 只输出 {"beats": [...], "local_canon": [...]}，不要输出其它文本；没有内容的数组留空。

单个节拍格式：
{
  "kind": "enter_scene|react|advance_plan",
  "actor_id": "候选人物 ID",
  "target_location_id": "当前地点 ID",
  "target_ids": ["已有目标 ID"],
  "summary": "玩家可见的克制节拍",
  "motivation": "该节拍如何服从人物既有动机"
}

关于 local_canon（生成式局部事实）：
1. 仅当输入包含【生成边界】且其中预算有剩余时，才可以提议；否则 local_canon 必须为空数组。
2. 只能使用【生成边界】列出的 archetype_id；kind 只能是 location 或 situation。禁止创造人物——重要人物只能来自作者角色池。
3. entity_id 必须以 gen_ 开头且全新；name 不得与任何既有人物、物品、地点重名。
4. parent_location_id 必须是作者定义的地点，且在原型允许范围内；situation 的持续回合不得超过原型上限。
5. description 是玩家将看到的事实描述，不得泄露秘密、不得宣称机械结果（不能替玩家获得物品或改变数值）。
6. 每次最多提议 1 条，且只在它让当前场景明显更可信、可玩时才提议；宁缺毋滥。

单条 local_canon 格式：
{
  "kind": "location|situation",
  "archetype_id": "生成边界中的原型 ID",
  "entity_id": "gen_ 开头的新 ID",
  "name": "简短名称",
  "description": "玩家可见的事实描述",
  "parent_location_id": "作者定义的地点 ID",
  "expires_after_turns": 3,
  "reason": "为什么此刻需要这个局部事实"
}
"""


def build_messages(request: PlanRequest) -> list[dict[str, str]]:
    payload: dict[str, Any] = {
        "玩家方案": request.player_text,
        "感知快照": request.perception.to_dict(),
        "世界边界": list(request.world_rules),
        "可提议状态空间": request.proposal_space,
    }
    if request.pending_clarification:
        payload["待澄清问题"] = request.pending_clarification
        payload["说明"] = (
            "玩家的这次输入是在回答上面的待澄清问题；结合上一轮计划理解它。"
            "若输入明显换了话题，则按全新方案理解。"
        )
        if request.previous_plan is not None:
            payload["上一轮计划"] = {
                "理解": request.previous_plan.interpretation,
                "原话": request.previous_plan.player_text,
                "涉及": list(request.previous_plan.references),
            }
    if request.previous_validation is not None:
        payload["上一轮验证反馈"] = {
            "被拒原因": [issue.message for issue in request.previous_validation.issues],
            "上一轮计划目标": request.previous_plan.goal if request.previous_plan else None,
        }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_suggestion_messages(request: SuggestionRequest) -> list[dict[str, str]]:
    payload = {
        "数量上限": request.count,
        "感知快照": request.perception.to_dict(),
        "世界边界": list(request.world_rules),
        "可提议状态空间": request.proposal_space,
    }
    return [
        {"role": "system", "content": SUGGESTION_SYSTEM_PROMPT},
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
        "已提交结果": request.committed_result,
        "候选人物": list(request.candidates),
        "世界边界": list(request.world_rules),
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


def _filter_fields(model_cls, data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key in model_cls.model_fields}


def coerce_plan(content: str, request: PlanRequest) -> ActionPlan:
    """Parse model output into a valid ActionPlan; raise ValueError to repair.

    Lenient on shape (drops unknown fields, fills defaults), strict on
    meaning (protocol validators still run).  An executable plan with no
    steps degrades to a clarification instead of a guess — fairness rule:
    when understanding fails, ask, don't improvise.
    """
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("plan must be a json object")

    steps = []
    for step in data.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step = _filter_fields(CapabilityAction, step)
        step.setdefault("capability", "intent")
        step.setdefault("arguments", {})
        step.setdefault("purpose", "执行玩家方案")
        if step.get("action"):
            steps.append(step)

    changes = []
    for change in data.get("proposed_changes") or []:
        if not isinstance(change, dict) or not change.get("path"):
            continue
        change = _filter_fields(StateChangeProposal, change)
        value = change.get("value")
        change.setdefault(
            "operation",
            "increment" if isinstance(value, (int, float)) and not isinstance(value, bool) else "set",
        )
        change.setdefault("authority", "soft_state")
        change.setdefault("reason", "模型提议")
        changes.append(change)

    risks = []
    for risk in data.get("risks") or []:
        if not isinstance(risk, dict) or not risk.get("description"):
            continue
        risk = _filter_fields(RiskProposal, risk)
        risk.setdefault("likelihood", "possible")
        risk.pop("changes", None)  # v0.1: prose risks only from the model
        risks.append(risk)

    needs_clarification = bool(data.get("needs_clarification"))
    question = data.get("clarification_question")
    if not needs_clarification and not steps:
        needs_clarification = True
        question = question or "我没有把握执行这个方案，能说得更具体一点吗？"
    if needs_clarification:
        steps = []
        changes = []
        question = question or "能把你的做法说得更具体一点吗？"
    else:
        question = None

    try:
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0.7))))
    except (TypeError, ValueError):
        confidence = 0.7

    plan_data: dict[str, Any] = {
        "plan_id": new_protocol_id("plan"),
        "perception_revision": request.perception.state_revision,
        "player_text": request.player_text,
        "interpretation": str(data.get("interpretation") or "").strip() or "（模型未给出解释）",
        "goal": str(data.get("goal") or "player_action"),
        "intent_id": data.get("intent_id"),
        "steps": steps,
        "references": [str(ref) for ref in data.get("references") or []],
        "proposed_changes": changes,
        "risks": risks,
        "assumptions": [str(item) for item in data.get("assumptions") or []],
        "confidence": confidence,
        "needs_clarification": needs_clarification,
        "clarification_question": question,
        # Revision chains cover both in-loop replans and clarification
        # replies across inputs: any previous plan makes this a revision.
        "revision": (
            request.previous_plan.revision + 1
            if request.previous_plan is not None
            else 0
        ),
        "parent_plan_id": (
            request.previous_plan.plan_id if request.previous_plan is not None else None
        ),
    }
    try:
        return ActionPlan.model_validate(plan_data)
    except Exception as exc:
        raise ValueError(f"plan failed protocol validation: {exc}") from exc


def coerce_suggestions(
    content: str,
    request: SuggestionRequest,
) -> tuple[SuggestedActionDraft, ...]:
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid json: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("suggestions"), list):
        raise ValueError("suggestion response must contain a suggestions array")

    drafts: list[SuggestedActionDraft] = []
    for card in data["suggestions"][: request.count]:
        if not isinstance(card, dict):
            continue
        action_text = str(card.get("action_text") or "").strip()
        title = str(card.get("title") or "").strip()
        rationale = str(card.get("rationale") or "").strip()
        if not action_text or not title or not rationale:
            continue
        raw_plan = card.get("plan") or {}
        if not isinstance(raw_plan, dict):
            continue
        raw_plan = dict(raw_plan)
        raw_plan.setdefault("interpretation", action_text)
        plan = coerce_plan(
            json.dumps(raw_plan, ensure_ascii=False),
            PlanRequest(
                perception=request.perception,
                player_text=action_text,
                world_rules=request.world_rules,
                proposal_space=request.proposal_space,
            ),
        )
        if plan.needs_clarification:
            continue
        focus = re.sub(
            r"[^a-z0-9_.-]+",
            "_",
            str(card.get("focus") or "practical").strip().lower(),
        ).strip("_") or "practical"
        if not focus[0].isalpha():
            focus = f"focus_{focus}"
        drafts.append(SuggestedActionDraft(
            suggestion_id=new_protocol_id("suggestion"),
            perception_revision=request.perception.state_revision,
            title=title,
            action_text=action_text,
            focus=focus,
            rationale=rationale,
            plan=plan,
        ))
    if not drafts:
        raise ValueError("no valid suggestion cards")
    return tuple(drafts)


def _sanitize_generated_id(value: Any) -> str:
    entity_id = re.sub(r"[^a-z0-9_]", "_", str(value or "").strip().lower())
    if not entity_id:
        entity_id = new_protocol_id("gen")
    if not entity_id.startswith(GENERATED_ENTITY_PREFIX):
        entity_id = f"{GENERATED_ENTITY_PREFIX}{entity_id.lstrip('_')}"
    return entity_id


def coerce_local_canon_proposals(
    data: dict[str, Any],
    request: DirectorRequest,
) -> tuple[LocalCanonProposal, ...]:
    """Best-effort parse; anything malformed is dropped, never repaired into
    authority (the engine's admission checks are the real gate)."""
    if not request.generation:
        return ()
    proposals: list[LocalCanonProposal] = []
    for raw in data.get("local_canon") or []:
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


def coerce_director_response(
    content: str,
    request: DirectorRequest,
) -> tuple[tuple[DirectorBeat, ...], tuple[LocalCanonProposal, ...]]:
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid json: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("beats"), list):
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
    return tuple(beats), coerce_local_canon_proposals(data, request)


def coerce_director_beats(
    content: str,
    request: DirectorRequest,
) -> tuple[DirectorBeat, ...]:
    return coerce_director_response(content, request)[0]


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

    def render_narrative(self, request: NarrativeRequest) -> NarrativeResponse | None:
        started = time.monotonic()
        payload = {"事实清单": request.facts, "文风约束": request.style}
        messages = [
            {"role": "system", "content": RENDER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        usage_total: dict[str, Any] = {}
        content = ""
        for attempt in range(2):
            content, usage = self._call(
                messages,
                json_mode=False,
                temperature=0.7,
                max_tokens=400,
            )
            for key, value in (usage or {}).items():
                if isinstance(value, (int, float)):
                    usage_total[key] = usage_total.get(key, 0) + value
            if content.strip():
                break
            if attempt == 0:
                messages.append({
                    "role": "user",
                    "content": "你返回了空内容。请根据同一份事实清单输出 2-4 句叙事文本。",
                })
        if not content.strip():
            return None
        return NarrativeResponse(
            text=content.strip(),
            model=self.model,
            prompt_version="deepseek-narrate-v1",
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            usage=usage_total,
        )

    def propose_plan(self, request: PlanRequest) -> PlanResponse:
        messages = build_messages(request)
        started = time.monotonic()
        usage_total: dict[str, Any] = {}
        last_error = "empty response"
        for _ in range(MAX_REPAIR_ROUNDS + 1):
            content, usage = self._call(messages)
            for key, value in (usage or {}).items():
                if isinstance(value, (int, float)):
                    usage_total[key] = usage_total.get(key, 0) + value
            if not content.strip():
                last_error = "empty content"
                messages.append({"role": "user", "content": "你返回了空内容。请只输出一个 json 对象。"})
                continue
            try:
                plan = coerce_plan(content, request)
            except ValueError as exc:
                last_error = str(exc)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": f"上一条输出无法解析（{exc}）。请修正并只输出一个 json 对象。",
                })
                continue
            return PlanResponse(
                plan=plan,
                raw=content,
                model=self.model,
                prompt_version=PROMPT_VERSION,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                usage=usage_total,
            )
        raise LLMProviderError(f"DeepSeek 连续输出无法解析：{last_error}")

    def propose_suggestions(self, request: SuggestionRequest) -> SuggestionResponse:
        messages = build_suggestion_messages(request)
        started = time.monotonic()
        usage_total: dict[str, Any] = {}
        last_error = "empty response"
        for _ in range(MAX_REPAIR_ROUNDS + 1):
            content, usage = self._call(messages)
            for key, value in (usage or {}).items():
                if isinstance(value, (int, float)):
                    usage_total[key] = usage_total.get(key, 0) + value
            if not content.strip():
                last_error = "empty content"
                messages.append({
                    "role": "user",
                    "content": "你返回了空内容。请只输出 suggestions json 对象。",
                })
                continue
            try:
                suggestions = coerce_suggestions(content, request)
            except ValueError as exc:
                last_error = str(exc)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": f"上一条提案无法解析（{exc}）。请修正并只输出 json。",
                })
                continue
            return SuggestionResponse(
                suggestions=suggestions,
                raw=content,
                model=self.model,
                prompt_version=SUGGESTION_PROMPT_VERSION,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                usage=usage_total,
            )
        raise LLMProviderError(f"DeepSeek 行动提案连续无法解析：{last_error}")

    def propose_director_beats(self, request: DirectorRequest) -> DirectorResponse:
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
            for key, value in (usage or {}).items():
                if isinstance(value, (int, float)):
                    usage_total[key] = usage_total.get(key, 0) + value
            if not content.strip():
                last_error = "empty content"
                messages.append({
                    "role": "user",
                    "content": "你返回了空内容。请只输出 beats json 对象。",
                })
                continue
            try:
                beats, local_canon = coerce_director_response(content, request)
            except ValueError as exc:
                last_error = str(exc)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": f"上一条导演节拍无法解析（{exc}）。请修正并只输出 json。",
                })
                continue
            return DirectorResponse(
                beats=beats,
                raw=content,
                model=self.model,
                prompt_version=DIRECTOR_PROMPT_VERSION,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
                usage=usage_total,
                local_canon=local_canon,
            )
        raise LLMProviderError(f"DeepSeek Director 连续无法解析：{last_error}")
