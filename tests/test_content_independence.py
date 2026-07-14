"""Regression checks that generic runtime code stays story-independent."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.cli import choose_story_path, discover_story_paths, print_help


ROOT = Path(__file__).resolve().parent.parent


class GenericRuntimeContentIndependenceTests(unittest.TestCase):
    def test_runtime_sources_do_not_embed_known_story_vocabulary(self) -> None:
        forbidden = (
            "midnight_archive",
            "rooftop_supper",
            "family_portrait",
            "servant_key",
            "aunt_chen",
            "薇拉小姐",
            "陈阿姨",
            "全家画像",
            "仆役侧门钥匙",
            "离开庄园",
        )
        offenders: list[str] = []
        for source_dir in (ROOT / "server", ROOT / "tools"):
            for path in source_dir.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for token in forbidden:
                    if token in text:
                        offenders.append(f"{path.relative_to(ROOT)}: {token}")
        self.assertEqual(offenders, [])

    def test_help_uses_content_neutral_placeholders(self) -> None:
        with patch("builtins.print") as mocked_print:
            print_help()
        output = "\n".join(str(call.args[0]) for call in mocked_print.call_args_list)
        self.assertIn("<意图编号或 ID>", output)
        self.assertIn("<物品> <目标>", output)

    def test_story_discovery_and_selection_do_not_prefer_a_known_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            content_dir = Path(temp_dir)
            first = content_dir / "a_story.yaml"
            second = content_dir / "b_story.yaml"
            first.write_text('id: a_story\ntitle: "故事甲"\n', encoding="utf-8")
            second.write_text('id: b_story\ntitle: "故事乙"\n', encoding="utf-8")

            self.assertEqual(discover_story_paths(content_dir), [first, second])
            with patch("server.cli.read_line", return_value="2"):
                selected = choose_story_path(None, content_dir)
            self.assertEqual(selected, second)


if __name__ == "__main__":
    unittest.main()
