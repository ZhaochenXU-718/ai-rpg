from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from server.engine.content import Story
from server.engine.session import GameSession
from tools.validate_content import validate_content


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "open_neighbor_scene.yaml"


class OpenSceneFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.story = Story.load(FIXTURE)
        self.session = GameSession(self.story, log_dir=None)

    def test_minimal_fixture_validates_cleanly(self) -> None:
        data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
        report = validate_content(data)
        self.assertNotIn("storylets", data)
        self.assertNotIn("endings", data)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_prose_request_cannot_silently_transfer_the_canvas(self) -> None:
        before = copy.deepcopy(self.session.state["item_locations"]["rain_canvas"])
        result = self.session.commit_narrative(
            "我问周师傅能不能把旧防雨布借给我。",
            "你向周师傅说明了长桌需要遮盖，也问清了借用的可能。",
        )
        self.assertEqual(self.session.state["item_locations"]["rain_canvas"], before)
        self.assertEqual(result.references, ())

if __name__ == "__main__":
    unittest.main()
