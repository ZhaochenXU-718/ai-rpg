"""Optional, conservative line editing for completed narrative drafts.

The editor is untrusted. This module only performs cheap deterministic checks;
the fact pipeline separately compares physical state changes before an edited
candidate can be committed in ``on`` mode.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .llm import LLMProvider, ProseEditRequest, ProseEditResponse
from .trace import TraceRecorder


MIN_RETAINED_CHAR_RATIO = 0.65
MAX_EXPANDED_CHAR_RATIO = 1.20

_NUMBER_PATTERN = re.compile(r"\d+(?:[.:/\-]\d+)*(?:[%％])?")
_OUTPUT_ARTIFACTS = (
    "```",
    "修改后：",
    "修改后:",
    "编辑后：",
    "编辑后:",
    "最终正文：",
    "最终正文:",
    "以下是修改",
    "以下是编辑",
)


class ProseEditorMode(str, Enum):
    OFF = "off"
    SHADOW = "shadow"
    ON = "on"

    @classmethod
    def coerce(cls, value: ProseEditorMode | str) -> ProseEditorMode:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            choices = ", ".join(mode.value for mode in cls)
            raise ValueError(
                f"unknown prose editor mode '{value}' (available: {choices})"
            ) from exc


@dataclass(frozen=True)
class ProseEditGuard:
    accepted: bool
    changed: bool
    char_ratio: float
    reasons: tuple[str, ...] = ()
    missing_terms: tuple[str, ...] = ()
    added_terms: tuple[str, ...] = ()
    missing_numbers: tuple[str, ...] = ()
    added_numbers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "changed": self.changed,
            "char_ratio": self.char_ratio,
            "reasons": list(self.reasons),
            "missing_terms": list(self.missing_terms),
            "added_terms": list(self.added_terms),
            "missing_numbers": list(self.missing_numbers),
            "added_numbers": list(self.added_numbers),
        }


@dataclass(frozen=True)
class ProseEditOutcome:
    draft: str
    candidate: str | None
    status: str
    guard: ProseEditGuard
    response: ProseEditResponse | None = None
    error: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def usable_candidate(self) -> bool:
        return (
            self.candidate is not None
            and self.guard.accepted
            and self.guard.changed
        )


@dataclass(frozen=True)
class ProseEditorComparison:
    """Developer-facing comparison emitted only through an explicit observer."""

    turn_no: int
    mode: ProseEditorMode
    status: str
    draft: str
    candidate: str | None
    guard: ProseEditGuard
    selected: str
    reason: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn_no,
            "mode": self.mode.value,
            "status": self.status,
            "draft": self.draft,
            "candidate": self.candidate,
            "guard": self.guard.to_dict(),
            "selected": self.selected,
            "reason": self.reason,
            "error": self.error,
        }


class ProseEditorObserver:
    """Optional sink for development UI; production callers use the no-op."""

    def comparison(self, comparison: ProseEditorComparison) -> None:
        pass


def _compact_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def _present_terms(text: str, protected_terms: tuple[str, ...]) -> set[str]:
    return {term for term in protected_terms if term and term in text}


def guard_prose_edit(
    draft: str,
    candidate: str,
    *,
    protected_terms: tuple[str, ...] = (),
) -> ProseEditGuard:
    """Reject gross scope drift without pretending to prove literary fidelity."""
    draft = draft.strip()
    candidate = candidate.strip()
    reasons: list[str] = []
    draft_length = _compact_length(draft)
    candidate_length = _compact_length(candidate)
    ratio = candidate_length / draft_length if draft_length else 0.0

    if not candidate:
        reasons.append("empty_candidate")
    if candidate and any(
        artifact in candidate[:40] for artifact in _OUTPUT_ARTIFACTS
    ):
        reasons.append("output_artifact")
    if candidate and ratio < MIN_RETAINED_CHAR_RATIO:
        reasons.append("over_compressed")
    if candidate and ratio > MAX_EXPANDED_CHAR_RATIO:
        reasons.append("scope_expanded")

    unique_terms = tuple(dict.fromkeys(
        term.strip()
        for term in protected_terms
        if term.strip()
    ))
    draft_terms = _present_terms(draft, unique_terms)
    candidate_terms = _present_terms(candidate, unique_terms)
    missing_terms = tuple(sorted(draft_terms - candidate_terms))
    added_terms = tuple(sorted(candidate_terms - draft_terms))
    if missing_terms:
        reasons.append("protected_term_removed")
    if added_terms:
        reasons.append("protected_term_added")

    draft_numbers = Counter(_NUMBER_PATTERN.findall(draft))
    candidate_numbers = Counter(_NUMBER_PATTERN.findall(candidate))
    missing_numbers = tuple(sorted((draft_numbers - candidate_numbers).elements()))
    added_numbers = tuple(sorted((candidate_numbers - draft_numbers).elements()))
    if missing_numbers:
        reasons.append("number_removed")
    if added_numbers:
        reasons.append("number_added")

    changed = candidate != draft
    return ProseEditGuard(
        accepted=not reasons,
        changed=changed,
        char_ratio=round(ratio, 4),
        reasons=tuple(reasons),
        missing_terms=missing_terms,
        added_terms=added_terms,
        missing_numbers=missing_numbers,
        added_numbers=added_numbers,
    )


def run_prose_editor(
    provider: LLMProvider,
    recorder: TraceRecorder,
    *,
    mode: ProseEditorMode,
    turn_no: int,
    draft: str,
    player_text: str,
    previous_narrative: str,
    protected_terms: tuple[str, ...],
) -> ProseEditOutcome:
    """Generate and cheaply guard one editor candidate; never raise."""
    try:
        response = provider.edit_narrative(
            ProseEditRequest(
                draft=draft,
                player_text=player_text,
                previous_narrative=previous_narrative,
                protected_terms=protected_terms,
            )
        )
    except Exception as exc:
        diagnostics = dict(getattr(exc, "diagnostics", {}) or {})
        guard = guard_prose_edit(
            draft,
            "",
            protected_terms=protected_terms,
        )
        recorder.record("prose_editor", {
            "turn": turn_no,
            "mode": mode.value,
            "status": "error",
            "error": str(exc),
            "diagnostics": diagnostics,
            "draft": draft,
            "candidate": None,
            "guard": guard.to_dict(),
        })
        return ProseEditOutcome(
            draft=draft,
            candidate=None,
            status="error",
            guard=guard,
            error=str(exc),
            diagnostics=diagnostics,
        )

    candidate = response.text.strip()
    guard = guard_prose_edit(
        draft,
        candidate,
        protected_terms=protected_terms,
    )
    if not guard.accepted:
        status = "rejected"
    elif not guard.changed:
        status = "unchanged"
    else:
        status = "candidate"
    recorder.record("prose_editor", {
        "turn": turn_no,
        "mode": mode.value,
        "status": status,
        "model": response.model,
        "prompt_version": response.prompt_version,
        "latency_ms": response.latency_ms,
        "usage": response.usage,
        "diagnostics": response.diagnostics,
        "draft": draft,
        "candidate": candidate,
        "guard": guard.to_dict(),
    })
    return ProseEditOutcome(
        draft=draft,
        candidate=candidate,
        status=status,
        guard=guard,
        response=response,
    )
