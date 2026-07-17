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
from server.engine.memory import MemoryState, ModuleRecord
from server.engine.modules import (
    CATEGORY_POLICIES,
    MAX_MODULE_OFFERS,
    compaction_module_catalog,
    compaction_module_updates,
    offered_module_states,
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

    def render_narrative(self, request):
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
