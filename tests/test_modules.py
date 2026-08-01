"""Story-module orchestration: selection, lifecycle, routing and openings."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from server.engine.content import Story
from server.engine.fact_pipeline import resolve_player_turn
from server.engine.llm import (
    HeuristicMockProvider,
    LLMProviderError,
    ScriptedProvider,
)
from server.engine.llm_deepseek import (
    build_narrative_messages,
    build_suggestion_messages,
)
from server.engine.memory import (
    MemoryDigest,
    MemoryEvent,
    MemoryState,
    ModuleRecord,
    plan_memory_compaction,
)
from server.engine.memory_pipeline import maybe_compact_memory
from server.engine.modules import (
    CATEGORY_POLICIES,
    MAX_MODULE_OFFERS,
    compaction_module_catalog,
    compaction_module_updates,
    offered_module_states,
    pending_requires_blockers,
    select_candidate_modules,
)
from server.engine.narration import narrate_player_turn
from server.engine.session import GameSession, SessionError
from server.engine.suggestions import generate_action_suggestions
from server.engine.trace import TraceRecorder
from tools.validate_content import MODULE_CATEGORIES, validate_content


ROOT = Path(__file__).resolve().parent.parent
STORY_PATH = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"

HOOK_MARK = "欲言又止"  # modules.zhou_hesitation.hook 独有


class CapturingNarrator(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.requests = []

    def render_narrative(self, request, *, stream=None):
        self.requests.append(request)
        return None


class CapturingSuggester(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.requests = []

    def propose_suggestions(self, request):
        self.requests.append(request)
        raise LLMProviderError("capture only")


class ModuleSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(STORY_PATH)
        self.session = GameSession(self.story, log_dir=None)

    def select(self):
        return select_candidate_modules(
            self.session.story,
            self.session.state,
            self.session.memory,
            self.session.turn_no,
        )

    def test_presence_and_requires_gate_eligibility(self) -> None:
        candidates = self.select()
        ids = [candidate.module_id for candidate in candidates]
        self.assertIn("canvas_request", ids)
        self.assertIn("zhou_hesitation", ids)
        # 林姐不在场，且 requires 未满足。
        self.assertNotIn("lin_aftermath", ids)

    def test_main_guarantee_puts_mainline_first(self) -> None:
        candidates = self.select()
        self.assertEqual(candidates[0].module_id, "canvas_request")
        self.assertEqual(candidates[0].category, "main")

    def test_committed_turn_marks_candidates_offered_with_cooldown(self) -> None:
        recorder = TraceRecorder(None, self.session.session_id)
        resolve_player_turn(
            self.session,
            HeuristicMockProvider(),
            recorder,
            "我先和周师傅打个招呼。",
        )
        states = {
            record.module_id: record
            for record in self.session.memory.module_states
        }
        self.assertEqual(states["canvas_request"].status, "offered")
        self.assertEqual(states["canvas_request"].offers_count, 1)
        self.assertEqual(states["canvas_request"].last_offered_turn, 1)
        # 冷却期内不再进入候选。
        self.assertEqual(self.select(), ())

    def test_module_states_follow_undo_branches(self) -> None:
        recorder = TraceRecorder(None, self.session.session_id)
        resolve_player_turn(
            self.session,
            HeuristicMockProvider(),
            recorder,
            "我先和周师傅打个招呼。",
        )
        self.assertTrue(self.session.memory.module_states)
        self.session.undo()
        self.assertEqual(self.session.memory.module_states, ())

    def test_ignored_hook_fades_after_max_offers(self) -> None:
        memory = MemoryState(module_states=(
            ModuleRecord(
                module_id="zhou_hesitation",
                status="offered",
                offers_count=MAX_MODULE_OFFERS - 1,
                last_offered_turn=1,
            ),
        ))
        records = {
            record.module_id: record
            for record in offered_module_states(memory, ("zhou_hesitation",), 6)
        }
        self.assertEqual(records["zhou_hesitation"].status, "dropped")
        self.assertEqual(
            records["zhou_hesitation"].offers_count, MAX_MODULE_OFFERS
        )

    def test_dropped_module_unlocks_aftermath_requires(self) -> None:
        self.session.apply_module_states(
            (ModuleRecord(module_id="zhou_hesitation", status="dropped"),),
            reason="test",
        )
        positions = dict(self.session.state["positions"])
        positions["player"] = "courtyard"
        self.session.state["positions"] = positions
        ids = [candidate.module_id for candidate in self.select()]
        self.assertIn("lin_aftermath", ids)


class ModuleLifecycleTest(unittest.TestCase):
    def test_compaction_transitions_follow_allowed_edges(self) -> None:
        memory = MemoryState(module_states=(
            ModuleRecord(module_id="offered_module", status="offered"),
            ModuleRecord(module_id="resolved_module", status="resolved"),
            ModuleRecord(module_id="dropped_module", status="dropped"),
        ))
        records = compaction_module_updates(memory, (
            ("offered_module", "engaged"),
            ("resolved_module", "engaged"),   # terminal：拒绝
            ("dropped_module", "engaged"),    # 复活：允许
            ("unknown_module", "resolved"),   # 未知：跳过
        ))
        by_id = {record.module_id: record for record in records}
        self.assertEqual(by_id["offered_module"].status, "engaged")
        self.assertEqual(by_id["resolved_module"].status, "resolved")
        self.assertEqual(by_id["dropped_module"].status, "engaged")
        self.assertNotIn("unknown_module", by_id)

    def test_noop_updates_return_none(self) -> None:
        memory = MemoryState(module_states=(
            ModuleRecord(module_id="resolved_module", status="resolved"),
        ))
        self.assertIsNone(
            compaction_module_updates(memory, (("resolved_module", "engaged"),))
        )

    def test_compaction_catalog_hides_unseen_modules(self) -> None:
        story = Story.load(STORY_PATH)
        memory = MemoryState(module_states=(
            ModuleRecord(module_id="canvas_request", status="offered"),
            ModuleRecord(module_id="zhou_hesitation", status="unseen"),
        ))
        catalog = compaction_module_catalog(story, memory)
        self.assertEqual(
            [entry[0] for entry in catalog], ["canvas_request"]
        )


class ModulePromptRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GameSession(Story.load(STORY_PATH), log_dir=None)

    def test_candidates_enter_narrator_prompt_only(self) -> None:
        narrator = CapturingNarrator()
        narrate_player_turn(self.session, narrator, "我看看周师傅在做什么。")
        request = narrator.requests[0]
        ids = [
            module.module_id
            for module in request.author_context.candidate_modules
        ]
        self.assertIn("zhou_hesitation", ids)
        narrative_payload = build_narrative_messages(request)[1]["content"]
        self.assertIn(HOOK_MARK, narrative_payload)

        suggester = CapturingSuggester()
        generate_action_suggestions(
            self.session, suggester, TraceRecorder(None, self.session.session_id)
        )
        suggestion_flat = json.dumps(
            build_suggestion_messages(suggester.requests[0]),
            ensure_ascii=False,
        )
        self.assertNotIn(HOOK_MARK, suggestion_flat)

        perception_flat = json.dumps(
            self.session.perception().to_dict(), ensure_ascii=False
        )
        self.assertNotIn(HOOK_MARK, perception_flat)


def _chain_story(requires_status: str) -> Story:
    return Story({
        "content_profile": "narrative_first",
        "id": "mini_chain",
        "player_role": {"id": "player"},
        "characters": {
            "player": {"name": "旅人", "role": "player"},
            "guide": {"name": "向导", "role": "npc"},
        },
        "scenes": {"room": {"name": "屋子"}},
        "initial_state": {
            "positions": {"player": "room", "guide": "room"},
            "item_locations": {},
        },
        "modules": {
            "gate": {
                "category": "main",
                "title": "前置",
                "purpose": "p",
                "hook": "h",
                "trigger": "t",
            },
            "next_step": {
                "category": "character",
                "title": "后续",
                "purpose": "p",
                "hook": "h",
                "trigger": "t",
                "requires": [
                    {"module": "gate", "status": requires_status}
                ],
            },
        },
    })


def _chain_state() -> dict:
    return {
        "positions": {"player": "room", "guide": "room"},
        "item_locations": {},
    }


def _gate_memory(status: str) -> MemoryState:
    if status == "unseen":
        return MemoryState()
    return MemoryState(module_states=(
        ModuleRecord(
            module_id="gate",
            status=status,
            offers_count=1,
            last_offered_turn=1,
        ),
    ))


class RequiresSatisfactionTest(unittest.TestCase):
    def _selected(self, requires_status: str, gate_status: str) -> list[str]:
        story = _chain_story(requires_status)
        candidates = select_candidate_modules(
            story, _chain_state(), _gate_memory(gate_status), 10
        )
        return [candidate.module_id for candidate in candidates]

    def test_offered_requirement_is_satisfied_by_later_progress(self) -> None:
        self.assertIn("next_step", self._selected("offered", "resolved"))
        self.assertIn("next_step", self._selected("offered", "dropped"))
        self.assertIn("next_step", self._selected("offered", "offered"))

    def test_engaged_requirement_is_satisfied_by_resolved(self) -> None:
        self.assertIn("next_step", self._selected("engaged", "resolved"))

    def test_resolved_requirement_is_not_satisfied_by_engaged(self) -> None:
        self.assertNotIn("next_step", self._selected("resolved", "engaged"))

    def test_unseen_predecessor_satisfies_nothing(self) -> None:
        self.assertNotIn("next_step", self._selected("offered", "unseen"))


class BookkeepingAccelerationTest(unittest.TestCase):
    def _events(self, count: int) -> tuple[MemoryEvent, ...]:
        return tuple(
            MemoryEvent(
                turn_no=index,
                commit_id=f"c{index}",
                player_text="问一句。",
                narrative="发生了一点事。",
                scene_before="room",
                scene_after="room",
            )
            for index in range(1, count + 1)
        )

    def test_blockers_require_a_pending_m2_judgement(self) -> None:
        story = _chain_story("resolved")
        self.assertEqual(
            pending_requires_blockers(story, _gate_memory("offered"), 10),
            ("next_step",),
        )
        self.assertEqual(
            pending_requires_blockers(story, _gate_memory("engaged"), 10),
            ("next_step",),
        )
        # 已满足、从未抛出、或目标为 offered 的依赖都不构成记账需求。
        self.assertEqual(
            pending_requires_blockers(story, _gate_memory("resolved"), 10), ()
        )
        self.assertEqual(
            pending_requires_blockers(story, _gate_memory("unseen"), 10), ()
        )
        self.assertEqual(
            pending_requires_blockers(
                _chain_story("offered"), _gate_memory("unseen"), 10
            ),
            (),
        )

    def test_min_turn_defers_bookkeeping_urgency(self) -> None:
        story = _chain_story("resolved")
        story.data["modules"]["next_step"]["min_turn"] = 20
        self.assertEqual(
            pending_requires_blockers(story, _gate_memory("offered"), 10), ()
        )

    def test_urgent_plan_bypasses_batch_size_but_not_window_or_cooldown(
        self,
    ) -> None:
        memory = MemoryState(events=self._events(5))
        self.assertIsNone(plan_memory_compaction(memory))
        plan = plan_memory_compaction(memory, urgent=True)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.trigger, "module_bookkeeping")
        self.assertEqual([event.turn_no for event in plan.events], [1])

        cooling = MemoryState(
            events=self._events(5), last_compaction_attempt_turn=4
        )
        self.assertIsNone(plan_memory_compaction(cooling, urgent=True))

        protected = MemoryState(events=self._events(4))
        self.assertIsNone(plan_memory_compaction(protected, urgent=True))

    def test_blocked_chain_accelerates_compaction_end_to_end(self) -> None:
        story = _chain_story("resolved")
        session = GameSession(story, log_dir=None)
        for index in range(5):
            session.commit_narrative(f"行动{index}。", f"发生了{index}。")
        session.apply_module_states(
            (ModuleRecord(
                module_id="gate",
                status="offered",
                offers_count=1,
                last_offered_turn=1,
            ),),
            reason="test",
        )
        provider = ScriptedProvider(memory_digests=[MemoryDigest(
            compacted_through_turn=1,
            rolling_summary="旧事已记。",
            module_updates=(("gate", "resolved"),),
        )])
        outcome = maybe_compact_memory(
            session, provider, TraceRecorder(None, session.session_id)
        )
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.trigger, "module_bookkeeping")
        self.assertEqual(
            session.memory.module_record("gate").status, "resolved"
        )
        ids = [
            candidate.module_id
            for candidate in select_candidate_modules(
                story, session.state, session.memory, session.turn_no
            )
        ]
        self.assertIn("next_step", ids)


class OpeningsTest(unittest.TestCase):
    def test_opening_overrides_player_position_only(self) -> None:
        story = Story.load(STORY_PATH)
        default_session = GameSession(story, log_dir=None)
        self.assertEqual(default_session.state["positions"]["player"], "workshop")

        opened = GameSession(story, log_dir=None, opening_id="courtyard_start")
        self.assertEqual(opened.state["positions"]["player"], "courtyard")
        self.assertEqual(opened.state["positions"]["keeper_zhou"], "workshop")

    def test_unknown_opening_is_rejected(self) -> None:
        story = Story.load(STORY_PATH)
        with self.assertRaisesRegex(SessionError, "unknown opening"):
            GameSession(story, log_dir=None, opening_id="rooftop_start")


class ModuleValidationTest(unittest.TestCase):
    def _data(self) -> dict:
        return yaml.safe_load(STORY_PATH.read_text(encoding="utf-8"))

    def test_category_registry_matches_validator(self) -> None:
        self.assertEqual(set(CATEGORY_POLICIES), MODULE_CATEGORIES)

    def test_fixture_modules_and_openings_validate(self) -> None:
        self.assertEqual(validate_content(self._data()).errors, [])

    def test_unknown_category_is_rejected(self) -> None:
        data = self._data()
        data["modules"]["canvas_request"]["category"] = "romance"
        errors = validate_content(data).errors
        self.assertTrue(any("category" in error for error in errors))

    def test_module_effects_are_rejected(self) -> None:
        data = self._data()
        data["modules"]["canvas_request"]["effects"] = [{"set": "flag"}]
        errors = validate_content(data).errors
        self.assertTrue(any("narrative semantics only" in error for error in errors))

    def test_requires_must_reference_known_module(self) -> None:
        data = self._data()
        data["modules"]["lin_aftermath"]["requires"] = [
            {"module": "missing_module", "status": "resolved"}
        ]
        errors = validate_content(data).errors
        self.assertTrue(any("missing_module" in error for error in errors))

    def test_involves_must_reference_known_entities(self) -> None:
        data = self._data()
        data["modules"]["canvas_request"]["involves"] = ["ghost_character"]
        errors = validate_content(data).errors
        self.assertTrue(any("ghost_character" in error for error in errors))

    def test_opening_rejects_unknown_fields_and_scenes(self) -> None:
        data = self._data()
        data["openings"]["courtyard_start"]["entry_effects"] = ["x"]
        data["openings"]["courtyard_start"]["positions"]["player"] = "rooftop"
        errors = validate_content(data).errors
        self.assertTrue(any("entry_effects" in error for error in errors))
        self.assertTrue(any("rooftop" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
