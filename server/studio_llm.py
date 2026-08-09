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
from server.studio_scale import describe_scale_ranges, get_scale_template
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
CONCEPT_POLICY = DeepSeekCallPolicy(
    capability="studio_concept",
    thinking="disabled",
    temperature=0.7,
    max_tokens=3200,
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


CONCEPT_SYSTEM_PROMPT = """\
你是 AIRPG 故事工作室中的概念策划助手。创作者提供一段初步创意简报，你提出 2—3 个可讨论的故事方向供创作者挑选；创作者拥有最终决定权，你的提案只是候选。

提案原则：
1. 每个提案必须是实质不同的故事方向，不是同一方向换措辞；差异应体现在核心冲突、隐藏真相或情绪基调上。
2. 尊重简报中已经确定的元素：简报里明确给出的设定、人物或基调必须保留，不得替换成你偏好的版本。
3. 故事前提写玩家进入故事时已经成立的处境，不剧透隐藏真相；隐藏真相单独表述，两者不能互相泄漏。
4. 保留玩家主体性：主要目标写成玩家可以主动追求的方向，不预设玩家的感情、立场或结局。
5. 不使用分支脚本、剧情旗标、数值好感度或条件结局来描述故事；冲突和张力来自人物动机与处境。
6. 规模建议参考给定的篇幅档位区间；可以偏离区间，但必须在理由中说明为什么这个故事需要更多或更少。
7. 不提 YAML、schema、机器 ID、prompt 或系统实现；使用创作者能直接阅读的中文。

只输出以下 JSON，不输出 Markdown：
{
  "proposals": [{
    "title": "故事名称",
    "premise": "故事前提（2—4 句，玩家开局时已成立的处境）",
    "main_goal": "主要目标与核心矛盾",
    "opposition": "对抗力量或阻力",
    "hidden_truth": "隐藏真相方向（不会出现在玩家可见内容中）",
    "emotional_contract": "希望玩家持续获得的感受",
    "genre_tags": ["题材标签"],
    "scale": {"characters": 人物数, "scenes": 场景数, "modules": 剧情模块数, "rationale": "规模理由"},
    "rationale": "这个方向的取舍与适合的创作者"
  }]
}
"""


@dataclass(frozen=True)
class ConceptBrief:
    brief: str
    scale_key: str
    genre_tags: tuple[str, ...] = ()
    language: str = "zh-CN"
    instruction: str = ""

    def __post_init__(self) -> None:
        if not self.brief.strip():
            raise ValueError("创意简报不能为空")
        if len(self.brief) > 4000:
            raise ValueError("创意简报请控制在 4000 字以内")
        if get_scale_template(self.scale_key) is None:
            raise ValueError(f"未知的篇幅档位 '{self.scale_key}'")


@dataclass(frozen=True)
class ConceptProposal:
    title: str
    premise: str
    main_goal: str
    opposition: str
    hidden_truth: str
    emotional_contract: str
    genre_tags: tuple[str, ...]
    scale: dict[str, Any]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "premise": self.premise,
            "main_goal": self.main_goal,
            "opposition": self.opposition,
            "hidden_truth": self.hidden_truth,
            "emotional_contract": self.emotional_contract,
            "genre_tags": list(self.genre_tags),
            "scale": dict(self.scale),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class ConceptResult:
    proposals: tuple[ConceptProposal, ...]
    model: str
    usage: dict[str, Any]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposals": [proposal.to_dict() for proposal in self.proposals],
            "model": self.model,
            "usage": self.usage,
        }


class ConceptAssistant:
    name = "abstract"

    def propose(self, brief: ConceptBrief) -> ConceptResult:
        raise NotImplementedError


class HeuristicConceptAssistant(ConceptAssistant):
    """Deterministic offline proposals for development and tests."""

    name = "mock"

    def propose(self, brief: ConceptBrief) -> ConceptResult:
        template = get_scale_template(brief.scale_key) or {}
        anchor = " ".join(brief.brief.split())[:80]
        tags = list(brief.genre_tags) or ["未分类"]

        def midpoint(field: str) -> int:
            low, high = template.get(field, (3, 5))
            return (low + high) // 2

        scale = {
            "characters": midpoint("characters"),
            "scenes": midpoint("scenes"),
            "modules": midpoint("modules"),
            "rationale": "离线开发模式按档位区间中点建议规模。",
        }
        directions = (
            (
                "揭示向",
                "把简报中的处境写成一个可以被逐步验证的谜题，玩家通过观察和对质接近真相。",
                "真相被某个在场人物主动维护，而不是单纯被隐藏。",
                "克制、渐进的紧张感。",
            ),
            (
                "关系代价向",
                "让简报中的目标必须以某段关系为代价才能达成，玩家在推进中不断面对取舍。",
                "最大的阻力来自玩家在意的人，而不是外部敌人。",
                "亲近与愧疚交替的情感压力。",
            ),
            (
                "处境升级向",
                "让简报中的处境按自身逻辑持续恶化，玩家的每次介入都改变局势的走向。",
                "对抗力量是失控的局势本身，人物只是被卷入的各方。",
                "步步紧逼的时间与空间压力。",
            ),
        )
        proposals = tuple(
            ConceptProposal(
                title=f"{anchor[:12] or '未命名故事'}·{label}",
                premise=f"围绕“{anchor}”：{premise_hint}",
                main_goal=f"玩家需要在局势失控前弄清并回应“{anchor}”中的核心矛盾。",
                opposition=opposition_hint,
                hidden_truth=(
                    "离线开发模式不虚构隐藏真相，只提示方向：真相应当解释简报中"
                    "最反常的细节，并且揭开后能改变玩家对已有人物的判断。"
                ),
                emotional_contract=mood,
                genre_tags=tuple(tags),
                scale=dict(scale),
                rationale=f"{label}方向的结构示意；使用 DeepSeek 或 Kimi 可生成正式提案。",
            )
            for label, premise_hint, opposition_hint, mood in directions
        )
        return ConceptResult(
            proposals=proposals,
            model="heuristic-studio-concept-0.1",
            usage={},
            diagnostics={},
        )


class ProviderConceptAssistant(ConceptAssistant):
    def __init__(self, provider: DeepSeekProvider) -> None:
        self.provider = provider
        self.name = provider.name

    def propose(self, brief: ConceptBrief) -> ConceptResult:
        messages = build_concept_messages(brief)
        proposals, _raw, usage, diagnostics = self.provider._run_structured(
            messages,
            policy=CONCEPT_POLICY,
            coerce=coerce_concept_result,
            repair_label="概念提案",
            error_label="概念策划助手",
        )
        return ConceptResult(
            proposals=proposals,
            model=self.provider.model,
            usage=usage,
            diagnostics=diagnostics,
        )


def create_concept_assistant(name: str) -> ConceptAssistant:
    if name == "mock":
        return HeuristicConceptAssistant()
    provider = create_provider(name)
    if not isinstance(provider, DeepSeekProvider):  # pragma: no cover - registry guard
        raise LLMProviderError(f"provider '{name}' does not support studio concepts")
    return ProviderConceptAssistant(provider)


def build_concept_messages(brief: ConceptBrief) -> list[dict[str, str]]:
    template = get_scale_template(brief.scale_key) or {}
    payload = {
        "创意简报": brief.brief.strip(),
        "篇幅档位": describe_scale_ranges(template) if template else brief.scale_key,
        "创作者已选题材标签": list(brief.genre_tags) or None,
        "语言": brief.language,
        "创作者补充要求": brief.instruction or None,
    }
    return [
        {"role": "system", "content": CONCEPT_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def coerce_concept_result(content: str) -> tuple[ConceptProposal, ...]:
    try:
        data = json.loads(_strip_fences(content))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("response must be a json object")
    proposals: list[ConceptProposal] = []
    for raw in data.get("proposals") or []:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        premise = str(raw.get("premise") or "").strip()
        if not title or not premise:
            continue
        raw_scale = raw.get("scale") if isinstance(raw.get("scale"), dict) else {}
        scale: dict[str, Any] = {}
        for field in ("characters", "scenes", "modules"):
            value = raw_scale.get(field)
            if isinstance(value, (int, float)) and 1 <= int(value) <= 99:
                scale[field] = int(value)
        scale["rationale"] = str(raw_scale.get("rationale") or "").strip()[:800]
        tags = tuple(
            tag_text[:24]
            for tag in (raw.get("genre_tags") or [])[:8]
            if (tag_text := str(tag or "").strip())
        )
        proposals.append(ConceptProposal(
            title=title[:60],
            premise=premise[:2000],
            main_goal=str(raw.get("main_goal") or "").strip()[:2000],
            opposition=str(raw.get("opposition") or "").strip()[:2000],
            hidden_truth=str(raw.get("hidden_truth") or "").strip()[:2000],
            emotional_contract=str(raw.get("emotional_contract") or "").strip()[:800],
            genre_tags=tags,
            scale=scale,
            rationale=str(raw.get("rationale") or "").strip()[:800],
        ))
        if len(proposals) >= 3:
            break
    if not proposals:
        raise ValueError("concept response needs proposals")
    return tuple(proposals)


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
