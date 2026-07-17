"""Deterministic story-module orchestration: eligibility, ranking, quota.

Modules are authored narrative material — "what could happen" plus "when it
fits" — never scripts, quests or effects. This orchestrator is pure code: it
narrows the module pool to a few candidates for the narrator and advances
soft lifecycle records. It makes no LLM call, evaluates no natural-language
trigger (the narrator does that), and holds no state-patch authority.

Category behaviour is a registry: adding a module category means adding one
``CategoryPolicy`` entry here and the mirrored name in the content validator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .content import Story
from .llm_protocol import CandidateModule
from .memory import MemoryState, ModuleRecord


MODULE_UNSEEN = "unseen"
MODULE_OFFERED = "offered"
MODULE_ENGAGED = "engaged"
MODULE_RESOLVED = "resolved"
MODULE_DROPPED = "dropped"

MODULE_STATUSES = (
    MODULE_UNSEEN,
    MODULE_OFFERED,
    MODULE_ENGAGED,
    MODULE_RESOLVED,
    MODULE_DROPPED,
)

# Statuses M2 compaction may assign, and which prior statuses allow them.
# ``resolved`` is terminal; ``dropped`` hooks may still be revived to
# ``engaged`` when the player picks them up late.
COMPACTION_TRANSITIONS: dict[str, set[str]] = {
    MODULE_ENGAGED: {MODULE_OFFERED, MODULE_DROPPED},
    MODULE_RESOLVED: {MODULE_OFFERED, MODULE_ENGAGED},
    MODULE_DROPPED: {MODULE_OFFERED, MODULE_ENGAGED},
}

MAX_CANDIDATE_MODULES = 3
MAX_MODULE_OFFERS = 3

_PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}


@dataclass(frozen=True)
class CategoryPolicy:
    """How the orchestrator treats one module category.

    ``rank`` orders categories when priorities tie (lower first);
    ``min_turn`` is the earliest upcoming turn a module may first appear;
    ``guarantee_when_eligible`` reserves one slot whenever a module of the
    category is eligible (used to keep the mainline supplied).
    """

    default_cooldown_turns: int = 3
    min_turn: int = 0
    max_per_batch: int = 2
    guarantee_when_eligible: bool = False
    rank: int = 1


CATEGORY_POLICIES: dict[str, CategoryPolicy] = {
    "main": CategoryPolicy(
        default_cooldown_turns=2,
        max_per_batch=1,
        guarantee_when_eligible=True,
        rank=0,
    ),
    "character": CategoryPolicy(default_cooldown_turns=3, rank=1),
    "pressure": CategoryPolicy(
        default_cooldown_turns=6,
        min_turn=6,
        max_per_batch=1,
        rank=2,
    ),
    "aftermath": CategoryPolicy(default_cooldown_turns=4, max_per_batch=1, rank=3),
    "side": CategoryPolicy(default_cooldown_turns=5, max_per_batch=1, rank=4),
}
_DEFAULT_POLICY = CategoryPolicy()


def _policy(spec: dict[str, Any]) -> CategoryPolicy:
    return CATEGORY_POLICIES.get(str(spec.get("category") or ""), _DEFAULT_POLICY)


def _present_entity_ids(story: Story, state: dict[str, Any]) -> set[str]:
    scene_id = story.current_location(state)
    return {
        scene_id,
        *story.characters_at(state, scene_id),
        *story.visible_items_at(state, scene_id),
        *story.inventory(state),
    }


def _requires_satisfied(
    spec: dict[str, Any],
    memory: MemoryState,
) -> bool:
    for requirement in spec.get("requires") or []:
        if not isinstance(requirement, dict):
            return False
        required_module = str(requirement.get("module") or "")
        required_status = str(requirement.get("status") or "")
        if memory.module_record(required_module).status != required_status:
            return False
    return True


def _is_eligible(
    spec: dict[str, Any],
    record: ModuleRecord,
    *,
    upcoming_turn: int,
    present: set[str],
    memory: MemoryState,
) -> bool:
    if record.status == MODULE_DROPPED:
        return False
    if record.status == MODULE_RESOLVED and spec.get("repeatable") is not True:
        return False
    policy = _policy(spec)
    min_turn = int(spec.get("min_turn") or policy.min_turn)
    if upcoming_turn < min_turn:
        return False
    cooldown = int(spec.get("cooldown_turns") or policy.default_cooldown_turns)
    if (
        record.last_offered_turn
        and upcoming_turn - record.last_offered_turn <= cooldown
    ):
        return False
    involves = [str(entity) for entity in spec.get("involves") or []]
    if involves and not set(involves) & present:
        return False
    return _requires_satisfied(spec, memory)


def _candidate(module_id: str, spec: dict[str, Any], record: ModuleRecord) -> CandidateModule:
    def text(key: str) -> str:
        return " ".join(str(spec.get(key) or "").split())

    return CandidateModule(
        module_id=module_id,
        category=str(spec.get("category") or "character"),
        title=text("title") or module_id,
        purpose=text("purpose"),
        hook=text("hook"),
        trigger=text("trigger"),
        escalation=text("escalation"),
        resolution=text("resolution"),
        fallback=text("fallback"),
        status=record.status,
        offers_count=record.offers_count,
    )


def select_candidate_modules(
    story: Story,
    state: dict[str, Any],
    memory: MemoryState,
    turn_no: int,
    *,
    limit: int = MAX_CANDIDATE_MODULES,
) -> tuple[CandidateModule, ...]:
    """Pick a few modules the narrator may weave in this turn.

    Pure function of its inputs, so prompt assembly and post-commit marking
    can recompute the same selection within one turn resolution.
    """
    specs = story.modules
    if not specs:
        return ()
    upcoming_turn = turn_no + 1
    present = _present_entity_ids(story, state)

    eligible: list[tuple[str, dict[str, Any], ModuleRecord]] = []
    for module_id, spec in specs.items():
        record = memory.module_record(module_id)
        if _is_eligible(
            spec,
            record,
            upcoming_turn=upcoming_turn,
            present=present,
            memory=memory,
        ):
            eligible.append((module_id, spec, record))

    def sort_key(entry: tuple[str, dict[str, Any], ModuleRecord]):
        module_id, spec, record = entry
        return (
            _PRIORITY_ORDER.get(str(spec.get("priority") or "normal"), 1),
            _policy(spec).rank,
            record.offers_count,
            record.last_offered_turn,
            module_id,
        )

    eligible.sort(key=sort_key)

    selected: list[tuple[str, dict[str, Any], ModuleRecord]] = []
    category_counts: dict[str, int] = {}

    def take(entry: tuple[str, dict[str, Any], ModuleRecord]) -> None:
        category = str(entry[1].get("category") or "")
        selected.append(entry)
        category_counts[category] = category_counts.get(category, 0) + 1

    for entry in eligible:
        if _policy(entry[1]).guarantee_when_eligible:
            take(entry)
            break
    for entry in eligible:
        if len(selected) >= max(1, limit):
            break
        if any(entry[0] == chosen[0] for chosen in selected):
            continue
        category = str(entry[1].get("category") or "")
        if category_counts.get(category, 0) >= _policy(entry[1]).max_per_batch:
            continue
        take(entry)

    return tuple(
        _candidate(module_id, spec, record)
        for module_id, spec, record in selected[: max(1, limit)]
    )


def offered_module_states(
    memory: MemoryState,
    candidate_ids: tuple[str, ...],
    committed_turn: int,
) -> tuple[ModuleRecord, ...]:
    """Mark committed-turn candidates as offered and fade ignored hooks.

    A module offered ``MAX_MODULE_OFFERS`` times without engagement fades to
    ``dropped``; M2 compaction may still revive it to ``engaged`` if the
    player picks the hook up late.
    """
    records = {
        record.module_id: record for record in memory.module_states
    }
    for module_id in candidate_ids:
        record = records.get(module_id) or ModuleRecord(module_id=module_id)
        offers = record.offers_count
        if record.last_offered_turn != committed_turn:
            offers += 1
        status = record.status
        if status == MODULE_UNSEEN:
            status = MODULE_OFFERED
        if status == MODULE_OFFERED and offers >= MAX_MODULE_OFFERS:
            status = MODULE_DROPPED
        records[module_id] = ModuleRecord(
            module_id=module_id,
            status=status,
            offers_count=offers,
            last_offered_turn=committed_turn,
        )
    return tuple(records.values())


def compaction_module_updates(
    memory: MemoryState,
    updates: tuple[tuple[str, str], ...],
) -> tuple[ModuleRecord, ...] | None:
    """Apply M2 transition commands under the allowed-transition rules.

    Returns the new record tuple, or ``None`` when nothing valid changed.
    """
    records = {
        record.module_id: record for record in memory.module_states
    }
    changed = False
    for module_id, new_status in updates:
        record = records.get(str(module_id))
        if record is None:
            continue
        allowed_from = COMPACTION_TRANSITIONS.get(str(new_status))
        if allowed_from is None or record.status not in allowed_from:
            continue
        records[record.module_id] = ModuleRecord(
            module_id=record.module_id,
            status=str(new_status),
            offers_count=record.offers_count,
            last_offered_turn=record.last_offered_turn,
        )
        changed = True
    if not changed:
        return None
    return tuple(records.values())


def compaction_module_catalog(
    story: Story,
    memory: MemoryState,
) -> tuple[tuple[str, str, str], ...]:
    """Modules the compactor may update: already surfaced ones only.

    Unseen modules stay invisible so unused authored material cannot leak
    into summaries, and ``resolved`` is terminal.
    """
    surfaced = {MODULE_OFFERED, MODULE_ENGAGED, MODULE_DROPPED}
    catalog = []
    for record in memory.module_states:
        if record.status not in surfaced:
            continue
        spec = story.modules.get(record.module_id) or {}
        title = " ".join(str(spec.get("title") or record.module_id).split())
        catalog.append((record.module_id, title, record.status))
    return tuple(catalog)
