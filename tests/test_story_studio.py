"""Story studio draft storage, validation, and playtest integration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from server.studio import StudioApplication
from server.studio_service import RevisionConflict, StoryStudioWorkspace


class StoryStudioWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.content_dir = Path(self.temporary.name)
        self.workspace = StoryStudioWorkspace(self.content_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self):
        return self.workspace.create(
            title="雾港最后一班船",
            genre="coastal_mystery",
            language="zh-CN",
        )

    @staticmethod
    def complete_minimal_story(story: dict) -> None:
        story["premise"] = "末班船离港前，玩家需要找出一位没有登船记录的乘客。"
        story["player_role"]["name"] = "值夜员"
        story["player_role"]["public_identity"] = "负责关闭码头的值夜员"
        story["player_role"]["private_goal"] = "确认所有乘客都已安全离港"
        story["characters"]["player"]["name"] = "值夜员"
        story["characters"]["player"]["public_profile"] = "穿着旧雨衣的码头值夜员。"
        opening = story["scenes"]["opening_scene"]
        opening["name"] = "雾港码头"
        opening["purpose"] = "建立末班船即将离港的时间压力。"
        opening["goal"] = "核对最后一批乘客。"
        opening["entry_text"] = "雾从水面漫过木栈桥，汽笛已经响过一次。"

    def test_create_returns_an_editable_draft_without_exposing_a_chosen_id(self) -> None:
        detail = self.create()

        self.assertRegex(detail["id"], r"^story_[a-f0-9]{8}$")
        self.assertEqual(detail["story"]["title"], "雾港最后一班船")
        self.assertIn("player", detail["story"]["characters"])
        self.assertIn("opening_scene", detail["story"]["scenes"])
        self.assertFalse(detail["validation"]["playable"])
        self.assertTrue((self.content_dir / f"{detail['id']}.yaml").exists())

    def test_custom_genre_tags_are_structured_for_future_catalog_filters(self) -> None:
        detail = self.workspace.create(
            title="潮汐钟楼",
            genre_tags=["奇幻", "海港", "时间循环", "奇幻"],
            language="zh-CN",
        )

        self.assertEqual(
            detail["story"]["genre_tags"],
            ["奇幻", "海港", "时间循环"],
        )
        self.assertEqual(detail["story"]["genre"], "奇幻 · 海港 · 时间循环")
        summary = self.workspace.list_stories()[0]
        self.assertEqual(summary["genre_tags"], ["奇幻", "海港", "时间循环"])

    def test_manual_fields_can_be_saved_into_a_runtime_valid_story(self) -> None:
        detail = self.create()
        story = detail["story"]
        self.complete_minimal_story(story)

        saved = self.workspace.save(
            detail["id"],
            story,
            expected_revision=detail["revision"],
        )

        self.assertTrue(saved["validation"]["playable"])
        self.assertEqual(saved["validation"]["errors"], [])
        loaded = yaml.safe_load(
            (self.content_dir / f"{detail['id']}.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(loaded["scenes"]["opening_scene"]["name"], "雾港码头")
        self.assertEqual(loaded["id"], detail["id"])

    def test_stale_revision_cannot_overwrite_newer_manual_edits(self) -> None:
        detail = self.create()
        story = detail["story"]
        story["premise"] = "第一版前提"
        self.workspace.save(
            detail["id"], story, expected_revision=detail["revision"]
        )

        story["premise"] = "来自旧页面的修改"
        with self.assertRaises(RevisionConflict):
            self.workspace.save(
                detail["id"], story, expected_revision=detail["revision"]
            )

    def test_story_identity_and_runtime_profile_are_service_owned(self) -> None:
        detail = self.create()
        story = detail["story"]
        story["id"] = "changed_by_client"
        story["schema_version"] = 99
        story["content_profile"] = "pre_pivot_archive"

        saved = self.workspace.save(
            detail["id"], story, expected_revision=detail["revision"]
        )

        self.assertEqual(saved["story"]["id"], detail["id"])
        self.assertEqual(saved["story"]["schema_version"], 2)
        self.assertEqual(saved["story"]["content_profile"], "narrative_first")

    def test_list_only_includes_active_stories(self) -> None:
        detail = self.create()
        (self.content_dir / "archive.yaml").write_text(
            "id: old_story\ntitle: old\ncontent_profile: pre_pivot_archive\n",
            encoding="utf-8",
        )

        stories = self.workspace.list_stories()

        self.assertEqual([story["id"] for story in stories], [detail["id"]])
        self.assertEqual(stories[0]["stats"]["characters"], 1)

    def test_valid_draft_starts_an_embedded_mock_playtest(self) -> None:
        detail = self.create()
        story = detail["story"]
        self.complete_minimal_story(story)
        saved = self.workspace.save(
            detail["id"], story, expected_revision=detail["revision"]
        )
        app = StudioApplication(self.workspace, provider_name="mock", no_log=True)

        payload = app.start_playtest(detail["id"])

        self.assertEqual(
            payload["url"], f"/play?api=/api/play/{detail['id']}"
        )
        state = app.game(detail["id"]).state()
        self.assertEqual(state["snapshot"]["story_title"], "雾港最后一班船")
        self.assertEqual(state["snapshot"]["scene_name"], "雾港码头")
        self.assertEqual(saved["validation"]["errors"], [])

    def test_llm_field_source_must_be_reviewed_explicitly(self) -> None:
        detail = self.create()
        story = detail["story"]
        story["premise"] = "由助手生成的故事前提。"

        saved = self.workspace.save(
            detail["id"],
            story,
            expected_revision=detail["revision"],
            review_edits=[{
                "path": "premise",
                "source": "llm",
                "status": "unreviewed",
                "operation": "complete",
            }],
        )

        self.assertEqual(saved["review"]["unreviewed_paths"], ["premise"])
        self.assertEqual(
            saved["review"]["fields"]["premise"]["operation"], "complete"
        )

        reviewed = self.workspace.mark_reviewed(
            detail["id"],
            "premise",
            expected_revision=saved["revision"],
        )
        self.assertEqual(reviewed["unreviewed_count"], 0)
        self.assertEqual(reviewed["fields"]["premise"]["status"], "reviewed")

    def test_mock_authoring_api_returns_a_candidate_without_saving_it(self) -> None:
        detail = self.create()
        story = detail["story"]
        story["premise"] = "码头值夜员发现一位乘客没有登船记录。"
        saved = self.workspace.save(
            detail["id"], story, expected_revision=detail["revision"]
        )
        app = StudioApplication(self.workspace, provider_name="mock", no_log=True)

        payload = app.assist(detail["id"], {
            "revision": saved["revision"],
            "operation": "ideas",
            "path": "premise",
            "label": "故事前提",
        })

        self.assertEqual(len(payload["candidates"]), 3)
        self.assertEqual(payload["original_text"], story["premise"])
        reloaded = self.workspace.load(detail["id"])
        self.assertEqual(reloaded["story"]["premise"], story["premise"])


if __name__ == "__main__":
    unittest.main()
