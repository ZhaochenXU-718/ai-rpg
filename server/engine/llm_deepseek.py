"""DeepSeek implementation of the narrative-first provider surface."""

from __future__ import annotations

import json
import os
import re
import threading
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
    NarrativeStream,
    ProseEditRequest,
    ProseEditResponse,
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
NARRATIVE_PROMPT_VERSION = "deepseek-narrate-v17"
PROSE_EDIT_PROMPT_VERSION = "deepseek-prose-edit-v1"
SUGGESTION_PROMPT_VERSION = "deepseek-suggestions-v6"
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
PROSE_EDIT_CALL_POLICY = DeepSeekCallPolicy(
    capability="prose_edit",
    thinking="disabled",
    temperature=0.2,
    max_tokens=1000,
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
3. 【作者私有上下文】是故事蓝图与在场人物的私有人物卡，仅用于扮演人物和把握长线方向；它不代表玩家已知。在场人物按各自的动机、压力、口吻和行为模式行动；人物秘密可以驱动回避、迟疑或撒谎，但在玩家尚未探明前不得直接说破，也不得写成玩家已知的事实。例外是 opening_narration：若提供，它是已经展示给玩家的开场正文，续写时自然承接它的文风、节奏和既定事实，不要复述它。
4. 【作者私有上下文】中的 critical_reminders 是作者最高优先级的少量规则，每回合都必须遵守。
5. candidate_modules 是当前可选的剧情素材：只在自然贴合玩家行动时编织其钩子，一回合至多推进一个；玩家忽略过的钩子（offers_count 大于 0）应换一种更轻的方式或干脆不提；不得强推模块，也不得替玩家接受钩子。
6. 人物换场或关键物品转手时写清楚实际发生的变化，不跳过当前在场和物品归属。
7. 若事实清单含冲突反馈，修正冲突部分，不在正文解释校验过程。
8. 服从给定视角与文风；玩家视角使用第二人称“你”。
9. 根据本回合实际发生的内容决定长度，通常为 80-300 字；复杂对话或重大事件可以更长。完成一个清楚的叙事节拍后立即停笔，不为达到字数补写气氛、动作、解释或总结。对话回合让在场人物充分表达。不提协议、阶段、数值或系统；只输出叙事文本。
10. 【软叙事记忆】中的未压缩事件是本故事刚刚发生的原文，你的输出必须像同一篇小说的下一段那样承接最近一回合的收尾。小结与笔记可能有概括误差；当前事实清单与当前感知优先。开放事项、提议和人物小结不能被擅自写成已经完成的事实，记忆中的远处人物也不算当前在场。
"""


LANGUAGE_STYLE_SYSTEM_PROMPT = """\
生成叙事时，同时遵守以下行文原则：
1. 每句话都应承担至少一种作用：推进动作、提供信息、塑造人物、维持视角、建立空间、增加或释放张力。不能承担任何作用的表达才应删除。简短不是目标，有效才是目标。
2. 保护场景必要的连接组织。人物处理信息时可以停顿、重复关键词、改变措辞或暂时回避；旁白可以保留帮助读者理解人物反应、空间关系和节奏变化的内容。不要把正文压缩成动作提纲、对白字幕或事实摘要。
3. 避免同一种效果被连续表达多次。动作、对白、心理判断、比喻若都在说明同一种情绪，只保留其中最有效的一层或两层。删除的是重复效果，不是所有情绪、观察和心理运动。
4. 人物回应另一人的发言时，不机械复述已经明确的信息。若重复能够表现思考、质疑、误解、确认关键词、施压或人物习惯，可以保留；重复之后必须产生新的信息、态度或关系变化。
5. 允许视角人物根据可见行为作出有限判断，但不要把推测写成全知事实。抽象情绪说明只有在动作和对白无法传达、且该判断影响当前关系或行动时才保留。
6. 比喻、排比、长句、短句和转折句都是可用的写作手段，不按词形禁止。只有在修辞没有增加信息、连续堆叠、替代具体内容或强迫读者感受时才删除。不要为了避开所谓 AI 高频词而把自然句子改得生硬。
7. 人物特征主要通过判断、选择、措辞和对关系的回应体现。习惯动作、外貌和随身物件可以在当前场景确实相关时出现，不作为每回合固定标签，也不因曾经出现过就永久禁用。
8. 对白服从人物的 voice，允许省略、误解、打断、试探、答非所问和言不由衷。不要把人物写成设定讲解员，也不要把含糊的话自动补全成完整、理性、礼貌的声明。
9. 段落可以停在动作、对白、新信息、未回答的问题或仍在发展的心理反应上。删除泛化的主题总结和关系升华，但不要为了避免总结而强行使用突兀的短句或悬念句。
10. 人物卡中的 dialogue_examples 只用于理解句长、语气和表达习惯，不是已经发生的对白。不得直接复制样例中的具体事件、数字和物件。

编辑和生成都应遵循同一判断：删除一句话前，先判断删除后是否损失人物、张力、节奏、视角、空间或信息；若有实际损失，应当保留或做局部修改。
"""


PROSE_EDIT_SYSTEM_PROMPT = """\
你是中文互动小说的行编辑。输入是叙事器已经完成的初稿，故事内容已经确定。
你的职责不是续写、重构或缩写故事，而是去除无功能的表达，同时保护节奏、人物声音、心理运动、叙事视角和阅读体验。

最高原则：
1. 经济性不等于极简。不要以更短为目标；某种表达像常见 AI 模式，只能成为检查信号，不能成为删除理由。
2. 删除或改写前，先判断是否会损失新信息、人物特征、心理变化、节奏或强调、空间与感官定位、潜台词、悬念或视角。只要会损失其中任何一项，就保留或只做局部修改。
3. 保持事件、事实、发生顺序、动作执行者、对话说话者、因果和行动结果不变；保持角色的知识边界、线索披露程度、不确定性、人物立场和玩家能动性不变。
4. 不添加新事实、新动作、新台词、新意图、新比喻或新专名；【必须保留的词】中凡是在初稿出现的词都必须继续出现，数字不得增删或改写。
5. 如果初稿没有明确问题，逐字原样返回。

优先检查：
- 同一意义被再次解释，却没有增加信息、态度或关系变化；
- 动作已经完整表现情绪，随后又机械命名同一情绪；
- 多个近义修饰语只增加音量，不增加层次；
- 气氛描写没有承担空间、人物、节奏或伏笔功能；
- 连续同形短句、整齐排比或固定转折只是在制造虚假戏剧感；
- 对话复述双方与读者都已知道的信息。

特别保护：
- 能表现思考、质疑、误解、施压、回声或人物习惯的重复；
- 必要的停顿、过渡、内心处理和 POV 对可见行为的有限判断；
- 人物声音、潜台词、有意留下的歧义和信息空缺；
- 长短句形成的节奏，以及同时承担多种叙事功能的细节。

失败编辑示例：把“她的指尖在绒布边缘停了一瞬，那动作太快，不像是犹豫，更像是在克制某种更深的反应”压成“她的手停在绒布上”。后者更短，却丢失了 POV 判断、人物心理与悬念，因此不能这样修改。

只输出编辑后的正文，不输出标题、分析、修改说明、引号包裹或 Markdown 围栏。
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
        {
            "role": "system",
            "content": (
                f"{RENDER_SYSTEM_PROMPT.rstrip()}\n\n"
                f"{LANGUAGE_STYLE_SYSTEM_PROMPT}"
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_prose_edit_messages(request: ProseEditRequest) -> list[dict[str, str]]:
    payload = {
        "玩家本回合行动": request.player_text,
        "上一回合正文": request.previous_narrative or None,
        "必须保留的词": list(request.protected_terms),
        "待编辑初稿": request.draft,
    }
    return [
        {"role": "system", "content": PROSE_EDIT_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_suggestion_messages(request: SuggestionRequest) -> list[dict[str, str]]:
    perception = request.perception.to_dict()
    perception.pop("current_goal", None)
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
    display_name = "DeepSeek"
    narrative_prompt_version = NARRATIVE_PROMPT_VERSION
    prose_edit_prompt_version = PROSE_EDIT_PROMPT_VERSION
    suggestion_prompt_version = SUGGESTION_PROMPT_VERSION
    fact_extraction_prompt_version = FACT_EXTRACTION_PROMPT_VERSION
    memory_compaction_prompt_version = MEMORY_COMPACTION_PROMPT_VERSION

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
            "prose_edit": PROSE_EDIT_CALL_POLICY,
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
        # 后台记忆压缩与前台生成可能并发使用同一 provider；client 初始化需要互斥。
        self._client_lock = threading.Lock()
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

    def _completion_kwargs(
        self,
        messages: list[dict[str, str]],
        *,
        policy: DeepSeekCallPolicy,
        json_mode: bool,
    ) -> dict[str, Any]:
        """Map one call policy onto provider-specific SDK arguments."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": policy.max_tokens,
            "extra_body": {"thinking": {"type": policy.thinking}},
        }
        if policy.thinking == "disabled" and policy.temperature is not None:
            kwargs["temperature"] = policy.temperature
        if policy.thinking == "enabled" and policy.reasoning_effort is not None:
            kwargs["reasoning_effort"] = policy.reasoning_effort
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def _call(
        self,
        messages: list[dict[str, str]],
        *,
        policy: DeepSeekCallPolicy,
        json_mode: bool | None = None,
        stream: NarrativeStream | None = None,
    ) -> DeepSeekCallResult:
        effective_json_mode = policy.json_mode if json_mode is None else json_mode
        options: dict[str, Any] = {
            "model": self.model,
            **policy.to_dict(),
            "json_mode": effective_json_mode,
        }
        if self._transport is not None:
            result = self._normalize_transport_result(
                self._transport(messages, options)
            )
            # 测试与脚本 transport 不分块；整段作为一个 delta 保持监听语义一致。
            if stream is not None and result.content:
                stream.delta(result.content)
            return result
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    try:
                        from openai import OpenAI
                    except ImportError as exc:
                        raise LLMProviderError(
                            f"{self.display_name} provider 需要 openai SDK："
                            "pip install openai"
                        ) from exc
                    self._client = OpenAI(
                        api_key=self._api_key, base_url=self.base_url
                    )
        kwargs = self._completion_kwargs(
            messages,
            policy=policy,
            json_mode=effective_json_mode,
        )
        if stream is not None:
            return self._streaming_completion(kwargs, stream)
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

    def _streaming_completion(
        self,
        kwargs: dict[str, Any],
        stream: NarrativeStream,
    ) -> DeepSeekCallResult:
        """Forward content deltas live, then return the assembled result."""
        response = self._client.chat.completions.create(
            **kwargs,
            stream=True,
            stream_options={"include_usage": True},
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        for chunk in response:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage.model_dump(mode="json")
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                piece = str(getattr(delta, "content", "") or "")
                if piece:
                    content_parts.append(piece)
                    stream.delta(piece)
                reasoning = str(getattr(delta, "reasoning_content", "") or "")
                if reasoning:
                    reasoning_parts.append(reasoning)
            if choice.finish_reason:
                finish_reason = str(choice.finish_reason)
        return DeepSeekCallResult(
            content="".join(content_parts),
            reasoning_content="".join(reasoning_parts),
            finish_reason=finish_reason,
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
                    f"{self.display_name} {error_label}调用失败：{exc}",
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
            f"{self.display_name} {error_label}连续无法解析：{last_error}",
            diagnostics=diagnostics,
        )

    def render_narrative(
        self,
        request: NarrativeRequest,
        *,
        stream: NarrativeStream | None = None,
    ) -> NarrativeResponse | None:
        started = time.monotonic()
        messages = build_narrative_messages(request)
        policy = self.call_policies["narration"]
        usage_total: dict[str, Any] = {}
        diagnostics = self._new_diagnostics(policy)
        last_error = "empty_content"
        for attempt in range(1, MAX_REPAIR_ROUNDS + 2):
            try:
                result = self._call(messages, policy=policy, stream=stream)
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
                    f"{self.display_name} 旁白调用失败：{exc}",
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
                    prompt_version=self.narrative_prompt_version,
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
                if stream is not None and result.content.strip():
                    # 已流出的候选被撤回，重写候选随后到达。
                    stream.restart(last_error)
                messages.append({
                    "role": "user",
                    "content": (
                        f"上一条叙事无法使用（{last_error}）。"
                        "请根据同一事实清单输出完整的 2-5 句叙事。"
                    ),
                })

        self._finalize_diagnostics(diagnostics, usage_total, last_error)
        raise LLMProviderError(
            f"{self.display_name} 旁白连续无法生成：{last_error}",
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
            prompt_version=self.suggestion_prompt_version,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            usage=usage,
            diagnostics=diagnostics,
        )

    def edit_narrative(self, request: ProseEditRequest) -> ProseEditResponse:
        messages = build_prose_edit_messages(request)
        started = time.monotonic()
        policy = self.call_policies["prose_edit"]
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
                    f"{self.display_name} 行编辑调用失败：{exc}",
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
                return ProseEditResponse(
                    text=result.content.strip(),
                    model=self.model,
                    prompt_version=self.prose_edit_prompt_version,
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
                        f"上一条编辑结果无法使用（{last_error}）。"
                        "请重新阅读全文，只输出完整的编辑后正文；"
                        "若无需修改，逐字返回初稿。"
                    ),
                })

        self._finalize_diagnostics(diagnostics, usage_total, last_error)
        raise LLMProviderError(
            f"{self.display_name} 行编辑连续无法生成：{last_error}",
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
            prompt_version=self.fact_extraction_prompt_version,
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
            prompt_version=self.memory_compaction_prompt_version,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            usage=usage,
            diagnostics=diagnostics,
        )
