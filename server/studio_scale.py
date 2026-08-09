"""Creator-facing story scale templates.

档位区间是创作预算建议，不是校验：概念提案阶段随简报提供给 LLM 作为
规模参照，创作质量提示用它生成非阻塞提醒。数值锚定在现有故事上
（长篇=霍格沃茨 13 人物/5 场景/16 模块，中篇=尘缘 7/8/9），时长换算
是待校准的假设，来源与校准计划见 dev-notes-v2/2026-08-09.md。

关键物品不设档位区间：现有两个故事（长篇 2 件、中篇 6 件）说明物品
数量由题材决定，与篇幅无关。
"""

from __future__ import annotations

import copy
from typing import Any


SCALE_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "key": "short",
        "label": "短篇",
        "duration_label": "约 20–45 分钟",
        "duration_minutes": (20, 45),
        "characters": (3, 6),
        "scenes": (1, 3),
        "modules": (3, 7),
        "summary": "一场聚会、一个夜晚或一次对峙的浓度，围绕单一冲突展开。",
    },
    {
        "key": "medium",
        "label": "中篇",
        "duration_label": "约 1–2.5 小时",
        "duration_minutes": (60, 150),
        "characters": (6, 10),
        "scenes": (3, 8),
        "modules": (7, 12),
        "summary": "一条主线加上两三条人物支线，有空间铺垫代价和转折。",
    },
    {
        "key": "long",
        "label": "长篇",
        "duration_label": "约 2.5–5 小时",
        "duration_minutes": (150, 300),
        "characters": (9, 16),
        "scenes": (4, 10),
        "modules": (12, 22),
        "summary": "完整的世界与人物网络，多条剧情线交织推进。",
    },
)

DEFAULT_SCALE_KEY = "medium"

RANGE_FIELDS = ("characters", "scenes", "modules")


def get_scale_template(key: str) -> dict[str, Any] | None:
    for template in SCALE_TEMPLATES:
        if template["key"] == key:
            return copy.deepcopy(template)
    return None


def scale_payload() -> list[dict[str, Any]]:
    """JSON-friendly template list for the studio brief form."""
    payload = []
    for template in SCALE_TEMPLATES:
        entry = copy.deepcopy(template)
        for field in RANGE_FIELDS + ("duration_minutes",):
            entry[field] = list(entry[field])
        payload.append(entry)
    return payload


def describe_scale_ranges(template: dict[str, Any]) -> str:
    """One-line Chinese description used in concept prompts."""
    return (
        f"{template['label']}（{template['duration_label']}）："
        f"人物（含玩家）{template['characters'][0]}–{template['characters'][1]} 个、"
        f"场景 {template['scenes'][0]}–{template['scenes'][1]} 个、"
        f"剧情模块 {template['modules'][0]}–{template['modules'][1]} 个"
    )
