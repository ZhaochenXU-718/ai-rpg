"""Local Canon: admission checks, atomic commit, lifecycle and branching.

The open fixture declares one location archetype (courtyard only) and one
situation archetype (max 3 turns) with budgets of 1 each. Everything a test
admits must survive replay through checkpoints, and everything rejected must
leave zero trace in authoritative state.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import ValidationError

from server.engine.capabilities import classify_path_authority
from server.engine.content import Story
from server.engine.director import run_director_cycle
from server.engine.llm import DirectorRequest, ScriptedProvider
from server.engine.llm_deepseek import coerce_director_response
from server.engine.llm_protocol import (
    AuthorityLevel,
    LocalCanonKind,
    LocalCanonProposal,
    new_protocol_id,
)
from server.engine.local_canon import (
    commit_local_canon,
    generation_context,
    remaining_budget,
    validate_local_canon,
)
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


class LocalCanonProtocolTest(unittest.TestCase):
    def test_generated_ids_must_use_the_gen_namespace(self) -> None:
        with self.assertRaises(ValidationError):
            location_proposal(entity_id="workshop_annex")

    def test_generated_locations_cannot_declare_expiry(self) -> None:
        with self.assertRaises(ValidationError):
            location_proposal(expires_after_turns=3)

    def test_generated_paths_classify_as_local_canon(self) -> None:
        story = Story.load(FIXTURE)
        self.assertEqual(
            classify_path_authority(story, "generated.locations.gen_x"),
            AuthorityLevel.LOCAL_CANON,
        )


class LocalCanonAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(FIXTURE)
        self.state = build_initial_state(self.story.data)

    def validate(self, proposal, revision: int = 0):
        return validate_local_canon(
            self.story, self.state, proposal, state_revision=revision
        )

    def test_valid_location_proposal_passes(self) -> None:
        validation = self.validate(location_proposal())
        self.assertTrue(validation.can_commit, validation.issues)

    def test_stale_revision_is_rejected(self) -> None:
        validation = self.validate(location_proposal(revision=3))
        codes = [issue.code for issue in validation.issues]
        self.assertIn("local_canon.stale_proposal", codes)

    def test_unknown_archetype_is_rejected(self) -> None:
        validation = self.validate(location_proposal(archetype_id="secret_lab"))
        codes = [issue.code for issue in validation.issues]
        self.assertIn("local_canon.archetype_unknown", codes)

    def test_parent_outside_archetype_allowlist_is_rejected(self) -> None:
        validation = self.validate(location_proposal(parent_location_id="workshop"))
        codes = [issue.code for issue in validation.issues]
        self.assertIn("local_canon.parent_not_allowed", codes)

    def test_name_conflicting_with_authored_canon_is_rejected(self) -> None:
        validation = self.validate(location_proposal(name="修理铺"))
        codes = [issue.code for issue in validation.issues]
        self.assertIn("local_canon.name_conflicts_canon", codes)

    def test_lifetime_beyond_archetype_cap_is_rejected(self) -> None:
        validation = self.validate(situation_proposal(expires_after_turns=4))
        codes = [issue.code for issue in validation.issues]
        self.assertIn("local_canon.lifetime_exceeds_archetype", codes)

    def test_budget_and_id_are_exhausted_by_a_commit(self) -> None:
        first = location_proposal()
        validation = self.validate(first)
        commit_local_canon(
            self.story, self.state, first, validation, turn_no=1
        )
        self.assertEqual(
            remaining_budget(self.story, self.state, LocalCanonKind.LOCATION), 0
        )

        duplicate = location_proposal()
        codes = [issue.code for issue in self.validate(duplicate).issues]
        self.assertIn("local_canon.budget_exhausted", codes)
        self.assertIn("local_canon.entity_id_taken", codes)

    def test_generated_locations_cannot_nest(self) -> None:
        first = location_proposal()
        commit_local_canon(
            self.story, self.state, first, self.validate(first), turn_no=1
        )
        nested = situation_proposal(parent_location_id="gen_canvas_nook")
        codes = [issue.code for issue in self.validate(nested).issues]
        self.assertIn("local_canon.parent_not_authored", codes)

    def test_generation_context_reports_remaining_budgets(self) -> None:
        context = generation_context(self.story, self.state)
        self.assertEqual(context["budgets"]["locations"]["remaining"], 1)
        archetype_ids = [
            spec["archetype_id"] for spec in context["location_archetypes"]
        ]
        self.assertEqual(archetype_ids, ["storage_nook"])

    def test_story_without_generation_block_yields_empty_context(self) -> None:
        story = Story.load(ROOT / "content" / "midnight_archive.yaml")
        state = build_initial_state(story.data)
        self.assertEqual(generation_context(story, state), {})


class LocalCanonRuntimeTest(unittest.TestCase):
    """Director cycle admission, reachability, perception and branching."""

    def setUp(self) -> None:
        self.story = Story.load(FIXTURE)

    def make_session(self) -> GameSession:
        return GameSession(self.story, log_dir=None)

    def commit_via_director(self, session: GameSession, proposals) -> None:
        provider = ScriptedProvider(
            plans=[],
            local_canon_batches=[tuple(proposals)],
        )
        session.resolve(
            intent_id="observe",
            objects=[],
            _allow_quoted=True,
            _director_provider=provider,
        )

    def test_admitted_location_is_reachable_and_undo_removes_it(self) -> None:
        session = self.make_session()
        session.state["positions"]["player"] = "courtyard"
        self.commit_via_director(
            session, [location_proposal(revision=1)]
        )

        result = session.last_result
        self.assertEqual(len(result.local_canon), 1)
        record = result.local_canon[0]
        self.assertEqual(record.entity_id, "gen_canvas_nook")
        self.assertIn(
            "local_canon.gen_canvas_nook",
            result.change_sources,
        )

        exits = self.story.exit_labels(session.state)
        self.assertEqual(exits.get("gen_canvas_nook"), "墙根的收纳角")

        move = session.resolve(
            intent_id="move",
            objects=["gen_canvas_nook"],
            _allow_quoted=True,
        )
        self.assertEqual(move.scene_after, "gen_canvas_nook")
        perception = session.perception()
        self.assertEqual(perception.location_name, "墙根的收纳角")
        self.assertIn("courtyard", self.story.exit_labels(session.state))

        session.undo()
        session.undo()
        self.assertNotIn(
            "gen_canvas_nook",
            self.story.generated_locations(session.state),
        )
        self.assertNotIn("gen_canvas_nook", self.story.exit_labels(session.state))

    def test_admitted_situation_is_perceivable_then_expires(self) -> None:
        session = self.make_session()
        self.commit_via_director(
            session, [situation_proposal(revision=1)]
        )

        perception = session.perception()
        visible = {entity.entity_id for entity in perception.visible_entities}
        self.assertIn("gen_gathering_1", visible)
        self.assertIn("gen_gathering_1", self.story.actionable_objects(session.state))

        # Created on turn 1 with a 2-turn lifetime: alive on turn 2 and
        # deactivated during turn 3.
        session.resolve(intent_id="observe", objects=[], _allow_quoted=True)
        self.assertTrue(
            session.state["generated"]["situations"]["gen_gathering_1"]["active"]
        )
        expiring = session.resolve(
            intent_id="observe", objects=[], _allow_quoted=True
        )
        record = session.state["generated"]["situations"]["gen_gathering_1"]
        self.assertFalse(record["active"])
        self.assertIn(
            "围拢的邻居各自散开，院子恢复了平常的样子。",
            expiring.world_reaction_hints,
        )
        visible_after = {
            entity.entity_id for entity in session.perception().visible_entities
        }
        self.assertNotIn("gen_gathering_1", visible_after)

    def test_per_turn_throttle_admits_only_one_proposal(self) -> None:
        session = self.make_session()
        session.state["positions"]["player"] = "courtyard"
        self.commit_via_director(session, [
            location_proposal(revision=1),
            situation_proposal(
                revision=1,
                parent_location_id="courtyard",
            ),
        ])
        result = session.last_result
        self.assertEqual(len(result.local_canon), 1)
        self.assertEqual(result.local_canon[0].kind, LocalCanonKind.LOCATION)
        self.assertEqual(session.state["generated"]["situations"], {})

    def test_rejected_proposal_leaves_no_state_and_is_traced(self) -> None:
        session = self.make_session()
        self.commit_via_director(session, [
            situation_proposal(revision=1, archetype_id="secret_lab"),
        ])
        result = session.last_result
        self.assertEqual(result.local_canon, [])
        self.assertEqual(session.state["generated"]["situations"], {})
        rejected = result.director_trace.get("rejected") or []
        self.assertTrue(rejected)
        codes = [
            issue.get("code")
            for entry in rejected
            for issue in entry.get("issues") or []
        ]
        self.assertIn("local_canon.archetype_unknown", codes)


class LocalCanonCoercionTest(unittest.TestCase):
    """DeepSeek Director responses may carry local_canon proposals."""

    def request(self, generation: dict) -> DirectorRequest:
        return DirectorRequest(
            story_id="open_neighbor_scene",
            state_revision=1,
            turn_no=1,
            location_id="workshop",
            location_name="修理铺",
            current_goal="",
            player_id="player",
            player_action="observe",
            generation=generation,
        )

    def test_local_canon_array_is_parsed_with_generation_context(self) -> None:
        content = """
        {"beats": [], "local_canon": [{
            "kind": "situation",
            "archetype_id": "neighbor_gathering",
            "entity_id": "gathering front door",
            "name": "门口的邻里小聚",
            "description": "两三位邻居围拢过来。",
            "parent_location_id": "workshop",
            "expires_after_turns": 2,
            "reason": "回应院子里的动静"
        }]}
        """
        beats, proposals = coerce_director_response(
            content, self.request({"budgets": {"situations": {"remaining": 1}}})
        )
        self.assertEqual(beats, ())
        self.assertEqual(len(proposals), 1)
        self.assertTrue(proposals[0].entity_id.startswith("gen_"))
        self.assertEqual(proposals[0].kind, LocalCanonKind.SITUATION)

    def test_local_canon_is_ignored_without_generation_context(self) -> None:
        content = '{"beats": [], "local_canon": [{"kind": "situation"}]}'
        _, proposals = coerce_director_response(content, self.request({}))
        self.assertEqual(proposals, ())


if __name__ == "__main__":
    unittest.main()
