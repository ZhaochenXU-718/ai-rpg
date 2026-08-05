"""Provider-neutral LLM assistance for story-authoring fields.

The assistant only returns candidates and observations.  It has no storage
access and cannot mutate a story; applying a candidate remains an explicit
studio action followed by the normal review workflow.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from server.engine.llm import LLMProviderError, create_provider
from server.engine.llm_deepseek import DeepSeekCallPolicy, DeepSeekProvider
from tools.validate_content import validate_content


AUTHORING_OPERATIONS = {"ideas", "complete", "polish", "check"}
FIELD_PATH = re.compile(r"^[a-zA-Z0-9_]+(?:\.[a-zA-Z0-9_]+)*$")
AUTHORING_POLICY = DeepSeekCallPolicy(
    capability="studio_authoring",
    thinking="disabled",
    temperature=0.45,
    max_tokens=1800,
    json_mode=True,
)


AUTHORING_SYSTEM_PROMPT = """\
你是 AIRPG 故事工作室中的创作助手。你帮助创作者思考和打磨互动故事，但创作者始终拥有最终决定权。

共同原则：
1. 只针对请求中的当前字段工作，不擅自重写其他内容，也不把建议描述成已经采用。
2. 延续给定故事前提、人物动机、世界边界和文风；信息不足时提出方向，不虚构为既定事实。
3. 保留玩家主体性：剧情钩子可以制造压力和选择，不能替玩家决定感情、对白、牺牲或结局。
4. 人物建议优先说明“动机遇到压力后会怎样行动”，避免只堆叠性格标签。
5. 剧情模块只描述目的、钩子、适用时机、升级和收尾，不生成状态效果、flags、条件脚本或状态 patch。
6. 润色必须保留原文事实、含义、信息边界和专名；如果原文已经清楚，可以原样返回。
7. 不提 YAML、schema、机器 ID、prompt 或系统实现；使用创作者能直接阅读的中文。

操作规则：
- ideas：提供 3 个实质不同的短候选方向，每个都说明取舍。
- complete：根据已有上下文提供 1—3 个可以直接填入当前字段的完整候选。
- polish：只提供 1 个润色稿，保留原意，不扩展剧情事实。
- check：不提供替换稿；列出具体、可操作的一致性观察。没有明显问题时明确说明。

只输出以下 JSON，不输出 Markdown：
{
  "candidates": [{"text": "候选文字", "rationale": "方向或修改理由"}],
  "observations": ["具体检查结果"]
}
"""


@dataclass(frozen=True)
class AuthoringCandidate:
    text: str
    rationale: str

    def to_dict(self) -> dict[str, str]:
        return {"text": self.text, "rationale": self.rationale}


@dataclass(frozen=True)
class AuthoringResult:
    operation: str
    path: str
    candidates: tuple[AuthoringCandidate, ...]
    observations: tuple[str, ...]
    model: str
    usage: dict[str, Any]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "path": self.path,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "observations": list(self.observations),
            "model": self.model,
            "usage": self.usage,
        }


@dataclass(frozen=True)
class FieldAssistRequest:
    operation: str
    path: str
    label: str
    current_text: str
    instruction: str
    story: dict[str, Any]

    def __post_init__(self) -> None:
        if self.operation not in AUTHORING_OPERATIONS:
            raise ValueError(f"unknown authoring operation '{self.operation}'")
        if FIELD_PATH.fullmatch(self.path) is None:
            raise ValueError("invalid authoring field path")
        if not self.label.strip():
            raise ValueError("authoring field label is required")


class AuthoringAssistant:
    name = "abstract"

    def assist(self, request: FieldAssistRequest) -> AuthoringResult:
        raise NotImplementedError


class HeuristicAuthoringAssistant(AuthoringAssistant):
    """Offline development assistant that never pretends to be final prose."""

    name = "mock"

    def assist(self, request: FieldAssistRequest) -> AuthoringResult:
        premise = str(request.story.get("premise") or "当前故事前提").strip()
        anchor = " ".join(premise.split())[:90] or "当前故事前提"
        candidates: tuple[AuthoringCandidate, ...] = ()
        observations: tuple[str, ...] = ()
        if request.operation == "ideas":
            candidates = (
                AuthoringCandidate(
                    f"让{request.label}直接回应“{anchor}”中的玩家目标。",
                    "强调玩家能够主动介入的方向。",
                ),
                AuthoringCandidate(
                    f"从人物关系的代价切入{request.label}，让选择同时改变信任与局势。",
                    "适合人物驱动的故事。",
                ),
                AuthoringCandidate(
                    f"把{request.label}设计成一条可以被观察和验证的线索，而不是直接说明答案。",
                    "适合悬疑、探索和隐藏真相。",
                ),
            )
        elif request.operation == "complete":
            candidates = (AuthoringCandidate(
                f"围绕“{anchor}”补充{request.label}，明确当前压力、玩家可采取的行动以及不应提前揭露的信息。",
                "离线开发模式只提供结构提示；使用 DeepSeek 或 Kimi 可生成正式候选。",
            ),)
        elif request.operation == "polish":
            candidates = (AuthoringCandidate(
                request.current_text,
                "离线开发模式保留原文，不伪造润色结果。",
            ),)
        else:
            report = validate_content(request.story)
            observations = tuple(
                [f"结构检查：{message}" for message in report.errors[:6]]
                or ["当前内容没有发现确定性的结构问题；仍建议通过试玩检查人物表现和节奏。"]
            )
        return AuthoringResult(
            operation=request.operation,
            path=request.path,
            candidates=candidates,
            observations=observations,
            model="heuristic-studio-assistant-0.1",
            usage={},
            diagnostics={},
        )


class ProviderAuthoringAssistant(AuthoringAssistant):
    """Use the existing DeepSeek/Kimi transport and diagnostics stack."""

    def __init__(self, provider: DeepSeekProvider) -> None:
        self.provider = provider
        self.name = provider.name

    def assist(self, request: FieldAssistRequest) -> AuthoringResult:
        messages = build_authoring_messages(request)
        parsed, _raw, usage, diagnostics = self.provider._run_structured(
            messages,
            policy=AUTHORING_POLICY,
            coerce=lambda content: coerce_authoring_result(
                content, operation=request.operation
            ),
            repair_label="创作候选",
            error_label="创作助手",
        )
        candidates, observations = parsed
        return AuthoringResult(
            operation=request.operation,
            path=request.path,
            candidates=candidates,
            observations=observations,
            model=self.provider.model,
            usage=usage,
            diagnostics=diagnostics,
        )


def create_authoring_assistant(name: str) -> AuthoringAssistant:
    if name == "mock":
        return HeuristicAuthoringAssistant()
    provider = create_provider(name)
    if not isinstance(provider, DeepSeekProvider):  # pragma: no cover - registry guard
        raise LLMProviderError(f"provider '{name}' does not support studio authoring")
    return ProviderAuthoringAssistant(provider)


def build_authoring_messages(request: FieldAssistRequest) -> list[dict[str, str]]:
    payload = {
        "操作": request.operation,
        "当前字段": {
            "名称": request.label,
            "当前文字": request.current_text or None,
            "创作者补充要求": request.instruction or None,
        },
        "相关故事上下文": authoring_context(
            request.story,
            request.path,
            include_catalog=request.operation == "check",
        ),
    }
    return [
        {"role": "system", "content": AUTHORING_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def authoring_context(
    story: dict[str, Any],
    path: str,
    *,
    include_catalog: bool = False,
) -> dict[str, Any]:
    parts = path.split(".")
    context: dict[str, Any] = {
        "故事标题": story.get("title"),
        "题材与类型": story.get("genre_tags") or story.get("genre"),
        "故事前提": story.get("premise"),
        "情绪承诺": story.get("emotional_contract"),
        "故事方向": copy.deepcopy(story.get("ai_plot") or {}),
        "玩家角色": copy.deepcopy(story.get("player_role") or {}),
        "文风": copy.deepcopy(story.get("style_bible") or {}),
        "全局边界": copy.deepcopy(story.get("global_rules") or {}),
    }
    if len(parts) >= 2 and parts[0] in {"characters", "scenes", "items", "modules"}:
        entity = (story.get(parts[0]) or {}).get(parts[1])
        if isinstance(entity, dict):
            context["当前对象"] = copy.deepcopy(entity)
    if include_catalog:
        context["人物目录"] = {
            entity_id: value.get("name", entity_id)
            for entity_id, value in (story.get("characters") or {}).items()
            if isinstance(value, dict)
        }
        context["场景目录"] = {
            entity_id: value.get("name", entity_id)
            for entity_id, value in (story.get("scenes") or {}).items()
            if isinstance(value, dict)
        }
        context["剧情模块目录"] = {
            entity_id: value.get("title", entity_id)
            for entity_id, value in (story.get("modules") or {}).items()
            if isinstance(value, dict)
        }
    return context


def _strip_fences(content: str) -> str:
    content = content.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", content, re.DOTALL)
    return match.group(1) if match else content


def coerce_authoring_result(
    content: str,
    *,
    operation: str,
) -> tuple[tuple[AuthoringCandidate, ...], tuple[str, ...]]:
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("response must be a json object")
    candidates: list[AuthoringCandidate] = []
    for raw in data.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        rationale = str(raw.get("rationale") or "").strip()
        if text:
            candidates.append(AuthoringCandidate(text[:6000], rationale[:800]))
        if len(candidates) >= 5:
            break
    observations = tuple(
        text[:1200]
        for item in (data.get("observations") or [])[:10]
        if (text := str(item or "").strip())
    )
    if operation == "check":
        if not observations:
            raise ValueError("check response needs observations")
        candidates = []
    elif not candidates:
        raise ValueError("authoring response needs candidates")
    elif operation == "polish":
        candidates = candidates[:1]
    return tuple(candidates), observations
