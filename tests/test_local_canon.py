"""Local Canon admission and lifecycle through the narrative-first commit."""

from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import ValidationError

from server.engine.content import Story
from server.engine.llm import DirectorRequest, ScriptedProvider
from server.engine.llm_deepseek import coerce_director_plan
from server.engine.llm_protocol import LocalCanonKind, LocalCanonProposal, new_protocol_id
from server.engine.local_canon import (
    commit_local_canon,
    generation_context,
    remaining_budget,
    validate_local_canon,
)
from server.engine.llm_protocol import FactBatch
from server.engine.session import GameSession
from server.engine.state import build_initial_state


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


def location_proposal(*, revision: int = 0, **overrides) -> LocalCanonProposal:
    fields = {
        "proposal_id": new_protocol_id("lcp"),
        "state_revision": revision,
        "kind": LocalCanonKind.LOCATION,
        "archetype_id": "storage_nook",
        "entity_id": "gen_canvas_nook",
        "name": "墙根的收纳角",
        "description": "院墙下用木板隔出的小空间，正好放得下几件公用物什。",
        "parent_location_id": "courtyard",
        "reason": "给借来的物品一个可以持续互动的去处。",
    }
    fields.update(overrides)
    return LocalCanonProposal(**fields)


def situation_proposal(*, revision: int = 0, **overrides) -> LocalCanonProposal:
    fields = {
        "proposal_id": new_protocol_id("lcp"),
        "state_revision": revision,
        "kind": LocalCanonKind.SITUATION,
        "archetype_id": "neighbor_gathering",
        "entity_id": "gen_gathering_1",
        "name": "门口的邻里小聚",
        "description": "两三位邻居在修理铺门口围拢起来，等着看长桌怎么摆。",
        "parent_location_id": "workshop",
        "expires_after_turns": 2,
        "reason": "让院子的准备工作有可见的邻里反应。",
    }
    fields.update(overrides)
    return LocalCanonProposal(**fields)


class LocalCanonAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(FIXTURE)
        self.state = build_initial_state(self.story.data)

    def validate(self, proposal, revision: int = 0):
        return validate_local_canon(
            self.story, self.state, proposal, state_revision=revision
        )

    def test_protocol_and_valid_admission(self) -> None:
        with self.assertRaises(ValidationError):
            location_proposal(entity_id="workshop_annex")
        with self.assertRaises(ValidationError):
            location_proposal(expires_after_turns=3)
        self.assertTrue(self.validate(location_proposal()).can_commit)

    def test_stale_archetype_parent_name_and_lifetime_conflicts_are_rejected(self) -> None:
        cases = (
            (location_proposal(revision=3), "local_canon.stale_proposal"),
            (location_proposal(archetype_id="secret_lab"), "local_canon.archetype_unknown"),
            (location_proposal(parent_location_id="workshop"), "local_canon.parent_not_allowed"),
            (location_proposal(name="修理铺"), "local_canon.name_conflicts_canon"),
            (situation_proposal(expires_after_turns=4), "local_canon.lifetime_exceeds_archetype"),
        )
        for proposal, expected in cases:
            with self.subTest(expected=expected):
                codes = {issue.code for issue in self.validate(proposal).issues}
                self.assertIn(expected, codes)

    def test_commit_exhausts_budget_and_generated_children_cannot_nest(self) -> None:
        proposal = location_proposal()
        commit_local_canon(
            self.story, self.state, proposal, self.validate(proposal), turn_no=1
        )
        self.assertEqual(
            remaining_budget(self.story, self.state, LocalCanonKind.LOCATION), 0
        )
        duplicate_codes = {
            issue.code for issue in self.validate(location_proposal()).issues
        }
        self.assertIn("local_canon.budget_exhausted", duplicate_codes)
        self.assertIn("local_canon.entity_id_taken", duplicate_codes)
        nested_codes = {
            issue.code
            for issue in self.validate(
                situation_proposal(parent_location_id="gen_canvas_nook")
            ).issues
        }
        self.assertIn("local_canon.parent_not_authored", nested_codes)

    def test_generation_context_reports_declared_boundary(self) -> None:
        context = generation_context(self.story, self.state)
        self.assertEqual(context["budgets"]["locations"]["remaining"], 1)
        self.assertEqual(
            context["location_archetypes"][0]["archetype_id"], "storage_nook"
        )


class LocalCanonRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(FIXTURE)
        self.session = GameSession(self.story, log_dir=None)

    def commit(self, proposals):
        return self.session.commit_fact_batch(
            FactBatch(
                state_revision=self.session.state_revision,
                player_text="我看看周围。",
                narrative="你留意着眼前的动静。",
            ),
            director_provider=ScriptedProvider(
                [], local_canon_batches=[tuple(proposals)]
            ),
        )

    def test_admitted_location_is_reachable_and_undo_removes_it(self) -> None:
        self.session.state["positions"]["player"] = "courtyard"
        result = self.commit([location_proposal(revision=1)])
        self.assertEqual(result.local_canon[0].entity_id, "gen_canvas_nook")
        self.assertEqual(
            self.story.exit_labels(self.session.state)["gen_canvas_nook"],
            "墙根的收纳角",
        )
        self.session.undo()
        self.assertNotIn(
            "gen_canvas_nook", self.story.generated_locations(self.session.state)
        )

    def test_situation_is_visible_then_expires_with_authored_hint(self) -> None:
        self.commit([situation_proposal(revision=1)])
        visible = {entity.entity_id for entity in self.session.perception().visible_entities}
        self.assertIn("gen_gathering_1", visible)

        self.session.commit_narrative("我继续等。", "你又等了一会儿。")
        result = self.session.commit_narrative("我再看看。", "你抬头看向门口。")

        record = self.session.state["generated"]["situations"]["gen_gathering_1"]
        self.assertFalse(record["active"])
        self.assertIn(
            "围拢的邻居各自散开，院子恢复了平常的样子。",
            result.narrative_hints,
        )
        visible_after = {
            entity.entity_id for entity in self.session.perception().visible_entities
        }
        self.assertNotIn("gen_gathering_1", visible_after)

    def test_per_turn_throttle_admits_only_one_proposal(self) -> None:
        self.session.state["positions"]["player"] = "courtyard"
        result = self.commit([
            location_proposal(revision=1),
            situation_proposal(revision=1, parent_location_id="courtyard"),
        ])
        self.assertEqual(len(result.local_canon), 1)
        self.assertEqual(result.local_canon[0].kind, LocalCanonKind.LOCATION)

    def test_rejected_proposal_leaves_no_authoritative_state(self) -> None:
        result = self.commit([
            situation_proposal(revision=1, archetype_id="secret_lab")
        ])
        self.assertEqual(result.local_canon, [])
        self.assertEqual(self.session.state["generated"]["situations"], {})
        codes = {
            issue["code"]
            for entry in result.director_trace["rejected"]
            for issue in entry["issues"]
        }
        self.assertIn("local_canon.archetype_unknown", codes)


class LocalCanonCoercionTest(unittest.TestCase):
    def request(self, generation: dict) -> DirectorRequest:
        return DirectorRequest(
            story_id="open_neighbor_scene",
            state_revision=1,
            turn_no=1,
            location_id="workshop",
            location_name="修理铺",
            current_goal="",
            player_id="player",
            player_action="看看周围",
            generation=generation,
        )

    def test_proposals_require_generation_context(self) -> None:
        content = '''{"beats": [], "local_canon": [{
            "kind": "situation", "archetype_id": "neighbor_gathering",
            "entity_id": "gathering front door", "name": "门口的邻里小聚",
            "description": "两三位邻居围拢过来。",
            "parent_location_id": "workshop", "expires_after_turns": 2,
            "reason": "回应院子里的动静"
        }]}'''
        plan = coerce_director_plan(
            content, self.request({"budgets": {"situations": {"remaining": 1}}})
        )
        self.assertEqual(len(plan.local_canon), 1)
        self.assertTrue(plan.local_canon[0].entity_id.startswith("gen_"))

        ignored = coerce_director_plan(content, self.request({}))
        self.assertEqual(ignored.local_canon, ())


if __name__ == "__main__":
    unittest.main()
