"""Story studio draft storage, validation, and playtest integration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from server.studio import StudioApplication
from server.studio_service import (
    RevisionConflict,
    StoryStudioWorkspace,
    StudioError,
)


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
        self.assertTrue(
            (self.content_dir / "drafts" / f"{detail['id']}.yaml").exists()
        )

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
            (self.content_dir / "drafts" / f"{detail['id']}.yaml").read_text(
                encoding="utf-8"
            )
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
        (self.content_dir / "drafts" / "archive.yaml").write_text(
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
        app = StudioApplication(self.workspace, no_log=True)

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
        app = StudioApplication(self.workspace, no_log=True)

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


class StoryReleaseTest(unittest.TestCase):
    """Immutable release snapshots with a movable current-version pointer."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.content_dir = Path(self.temporary.name)
        self.workspace = StoryStudioWorkspace(self.content_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def playable_draft(self):
        detail = self.workspace.create(title="雾港最后一班船", genre="悬疑")
        story = detail["story"]
        StoryStudioWorkspaceTest.complete_minimal_story(story)
        return self.workspace.save(
            detail["id"], story, expected_revision=detail["revision"]
        )

    def test_publish_creates_an_immutable_snapshot_and_release_record(self) -> None:
        saved = self.playable_draft()

        published = self.workspace.publish(
            saved["id"], expected_revision=saved["revision"]
        )

        self.assertEqual(published["current"], "1.0.0")
        record = published["releases"][0]
        self.assertEqual(record["source_revision"], saved["revision"])
        snapshot_path = (
            self.content_dir / "releases" / saved["id"] / "1.0.0.yaml"
        )
        snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot["version"], "1.0.0")
        self.assertEqual(snapshot["title"], "雾港最后一班船")

        story = self.workspace.load(saved["id"])["story"]
        story["premise"] = "发布之后修改的前提"
        self.workspace.save(
            saved["id"], story, expected_revision=saved["revision"]
        )
        unchanged = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
        self.assertNotEqual(unchanged["premise"], "发布之后修改的前提")

    def test_publish_requires_a_playable_draft(self) -> None:
        detail = self.workspace.create(title="空白故事", genre="未分类")

        with self.assertRaisesRegex(StudioError, "阻塞问题"):
            self.workspace.publish(
                detail["id"], expected_revision=detail["revision"]
            )
        self.assertEqual(
            self.workspace.list_releases(detail["id"])["releases"], []
        )

    def test_publish_requires_all_llm_fields_reviewed(self) -> None:
        saved = self.playable_draft()
        story = saved["story"]
        story["premise"] = "LLM 起草的前提，等待创作者确认。"
        saved = self.workspace.save(
            saved["id"],
            story,
            expected_revision=saved["revision"],
            review_edits=[{
                "path": "premise",
                "source": "llm",
                "status": "unreviewed",
                "operation": "complete",
            }],
        )

        with self.assertRaisesRegex(StudioError, "未审阅"):
            self.workspace.publish(
                saved["id"], expected_revision=saved["revision"]
            )

        self.workspace.mark_reviewed(
            saved["id"], "premise", expected_revision=saved["revision"]
        )
        published = self.workspace.publish(
            saved["id"], expected_revision=saved["revision"]
        )
        self.assertEqual(published["current"], "1.0.0")

    def test_stale_revision_cannot_publish(self) -> None:
        saved = self.playable_draft()
        story = saved["story"]
        story["premise"] = "发布前的最新修改"
        self.workspace.save(
            saved["id"], story, expected_revision=saved["revision"]
        )

        with self.assertRaises(RevisionConflict):
            self.workspace.publish(
                saved["id"], expected_revision=saved["revision"]
            )

    def test_versions_increment_and_rollback_moves_the_pointer_only(self) -> None:
        saved = self.playable_draft()
        self.workspace.publish(saved["id"], expected_revision=saved["revision"])

        story = self.workspace.load(saved["id"])["story"]
        story["premise"] = "第二版：值夜员开始怀疑船长。"
        second = self.workspace.save(
            saved["id"], story, expected_revision=saved["revision"]
        )
        published = self.workspace.publish(
            saved["id"], expected_revision=second["revision"]
        )
        self.assertEqual(published["current"], "1.1.0")
        self.assertEqual(
            [record["version"] for record in published["releases"]],
            ["1.0.0", "1.1.0"],
        )

        rolled = self.workspace.rollback(saved["id"], "1.0.0")
        self.assertEqual(rolled["current"], "1.0.0")
        self.assertEqual(len(rolled["releases"]), 2)
        for version in ("1.0.0", "1.1.0"):
            self.assertTrue(
                (
                    self.content_dir / "releases" / saved["id"] / f"{version}.yaml"
                ).exists()
            )

        with self.assertRaisesRegex(StudioError, "没有版本"):
            self.workspace.rollback(saved["id"], "9.9.0")

    def test_story_catalog_reports_the_published_version(self) -> None:
        saved = self.playable_draft()
        self.workspace.publish(saved["id"], expected_revision=saved["revision"])

        summary = self.workspace.list_stories()[0]
        self.assertEqual(summary["published_version"], "1.0.0")
        self.assertEqual(summary["release_count"], 1)


if __name__ == "__main__":
    unittest.main()
