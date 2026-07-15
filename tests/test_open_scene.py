from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from server.engine.content import Story
from server.engine.llm import ScriptedProvider
from server.engine.llm_protocol import DirectorBeat, DirectorBeatKind
from server.engine.llm_protocol import FactBatch
from server.engine.session import GameSession
from tools.validate_content import validate_content


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


class OpenSceneFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(FIXTURE)
        self.session = GameSession(self.story, log_dir=None)

    def test_fixture_has_no_ordinary_storylets_and_validates_cleanly(self) -> None:
        data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
        report = validate_content(data)
        self.assertEqual(data["storylets"], [])
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_prose_request_cannot_silently_transfer_the_canvas(self) -> None:
        before = copy.deepcopy(self.session.state["item_locations"]["rain_canvas"])
        result = self.session.commit_narrative(
            "我问周师傅能不能把旧防雨布借给我。",
            "你向周师傅说明了长桌需要遮盖，也问清了借用的可能。",
        )
        self.assertEqual(self.session.state["item_locations"]["rain_canvas"], before)
        self.assertIsNone(result.ending)
        self.assertEqual(result.references, ())

    def test_director_can_move_only_the_authored_adjacent_neighbor(self) -> None:
        beat = DirectorBeat(
            beat_id="beat_neighbor_enters",
            state_revision=1,
            kind=DirectorBeatKind.ENTER_SCENE,
            actor_id="neighbor_lin",
            target_location_id="workshop",
            target_ids=("player",),
            summary="林姐从院子走到门口，先看了看长桌需要遮住的范围。",
            motivation="确认是否确实需要搭手，不替别人做决定。",
        )
        result = self.session.commit_fact_batch(
            FactBatch(
                state_revision=self.session.state_revision,
                player_text="我看看工具墙和半开的门。",
                narrative="你沿着工具墙慢慢看了一遍。",
                references=("tool_wall",),
            ),
            director_provider=ScriptedProvider([], director_batches=[(beat,)]),
        )
        self.assertEqual(self.session.state["positions"]["neighbor_lin"], "workshop")
        self.assertEqual(result.director_beats[0].actor_id, "neighbor_lin")
        self.assertNotIn("invented_neighbor", self.session.state["positions"])


if __name__ == "__main__":
    unittest.main()
