"""Engine-owned effects produced by deterministic capability modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .llm_protocol import PrimaryGoalStatus


@dataclass(frozen=True)
class CapabilityMutation:
    path: str
    value: Any
    operation: Literal["set", "increment"] = "set"


@dataclass(frozen=True)
class CapabilityResolution:
    capability_id: str
    objects: tuple[str, ...]
    mutations: tuple[CapabilityMutation, ...]
    response_hint: str
    primary_goal_status: PrimaryGoalStatus
    result_tier: str
    confirmation_required: bool = False
