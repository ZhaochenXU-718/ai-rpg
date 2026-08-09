"""Brief-to-concept proposals and concept-seeded story creation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from server.engine.llm_deepseek import DeepSeekCallResult, DeepSeekProvider
from server.studio import StudioApplication
from server.studio_llm import (
    ConceptBrief,
    HeuristicConceptAssistant,
    ProviderConceptAssistant,
    build_concept_messages,
    coerce_concept_result,
)
from server.studio_scale import (
    SCALE_TEMPLATES,
    get_scale_template,
    scale_payload,
)
from server.studio_service import StoryStudioWorkspace


class RecordingTransport:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = []

    def __call__(self, messages, options):
        self.calls.append((messages, options))
        return DeepSeekCallResult(
            content=self.reply,
            finish_reason="stop",
            usage={"total_tokens": 88},
        )


class ScaleTemplateTest(unittest.TestCase):
    def test_templates_are_three_ascending_advisory_tiers(self) -> None:
        self.assertEqual(
            [template["key"] for template in SCALE_TEMPLATES],
            ["short", "medium", "long"],
        )
        previous_modules = 0
        for template in SCALE_TEMPLATES:
            for field in ("characters", "scenes", "modules", "duration_minutes"):
                low, high = template[field]
                self.assertLess(low, high, f"{template['key']}.{field}")
            self.assertGreater(template["modules"][1], previous_modules)
            previous_modules = template["modules"][1]

    def test_payload_is_json_friendly_and_detached(self) -> None:
        payload = scale_payload()
        json.dumps(payload, ensure_ascii=False)
        payload[0]["characters"][0] = -99
        self.assertEqual(SCALE_TEMPLATES[0]["characters"][0], 3)


class ConceptAssistantTest(unittest.TestCase):
    def brief(self, scale: str = "medium") -> ConceptBrief:
        return ConceptBrief(
            brief="一座只在涨潮时出现的灯塔，和一位从不点灯的守塔人。",
            scale_key=scale,
            genre_tags=("悬疑",),
        )

    def test_brief_requires_content_and_a_known_scale(self) -> None:
        with self.assertRaises(ValueError):
            ConceptBrief(brief="   ", scale_key="medium")
        with self.assertRaises(ValueError):
            ConceptBrief(brief="一个想法", scale_key="epic")

    def test_mock_returns_three_distinct_in_range_proposals(self) -> None:
        result = HeuristicConceptAssistant().propose(self.brief("short"))

        self.assertEqual(len(result.proposals), 3)
        self.assertEqual(
            len({proposal.title for proposal in result.proposals}), 3
        )
        template = get_scale_template("short")
        for proposal in result.proposals:
            self.assertEqual(proposal.genre_tags, ("悬疑",))
            for field in ("characters", "scenes", "modules"):
                low, high = template[field]
                self.assertTrue(low <= proposal.scale[field] <= high)

    def test_prompt_carries_brief_scale_ranges_and_contract(self) -> None:
        messages = build_concept_messages(self.brief("long"))
        payload = json.loads(messages[1]["content"])

        self.assertIn("守塔人", payload["创意简报"])
        self.assertIn("12–22", payload["篇幅档位"])
        self.assertEqual(payload["创作者已选题材标签"], ["悬疑"])
        self.assertIn("不剧透隐藏真相", messages[0]["content"])
        self.assertIn("分支脚本", messages[0]["content"])
        self.assertIn("可以偏离区间", messages[0]["content"])

    def test_structured_proposals_are_coerced_and_capped(self) -> None:
        raw = {
            "proposals": [
                {
                    "title": "涨潮灯塔",
                    "premise": "灯塔只在涨潮时出现。",
                    "main_goal": "弄清守塔人不点灯的原因。",
                    "scale": {"characters": 7, "scenes": 4, "modules": 9,
                              "rationale": "中篇标准结构"},
                    "genre_tags": ["悬疑", "  ", "海港"],
                },
                {"title": "", "premise": "缺标题，应被丢弃。"},
                {"title": "方向二", "premise": "第二个方向。"},
                {"title": "方向三", "premise": "第三个方向。"},
                {"title": "方向四", "premise": "超出上限，应被丢弃。"},
            ]
        }
        content = "```json\n" + json.dumps(raw, ensure_ascii=False) + "\n```"

        proposals = coerce_concept_result(content)

        self.assertEqual(len(proposals), 3)
        self.assertEqual(proposals[0].title, "涨潮灯塔")
        self.assertEqual(proposals[0].genre_tags, ("悬疑", "海港"))
        self.assertEqual(proposals[0].scale["modules"], 9)
        with self.assertRaises(ValueError):
            coerce_concept_result(json.dumps({"proposals": []}))

    def test_deepseek_adapter_uses_the_concept_policy(self) -> None:
        transport = RecordingTransport(json.dumps({
            "proposals": [{
                "title": "涨潮灯塔",
                "premise": "灯塔只在涨潮时出现，守塔人每晚上塔却从不点灯。",
            }],
        }, ensure_ascii=False))
        assistant = ProviderConceptAssistant(DeepSeekProvider(transport=transport))

        result = assistant.propose(self.brief())

        self.assertEqual(len(result.proposals), 1)
        self.assertEqual(transport.calls[0][1]["capability"], "studio_concept")
        self.assertTrue(transport.calls[0][1]["json_mode"])
        self.assertEqual(result.usage["total_tokens"], 88)


class ConceptStoryCreationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.content_dir = Path(self.temporary.name)
        self.workspace = StoryStudioWorkspace(self.content_dir)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def concept(self) -> dict:
        return {
            "title": "涨潮灯塔",
            "premise": "灯塔只在涨潮时出现。",
            "main_goal": "弄清守塔人不点灯的原因。",
            "opposition": "潮汐的时间窗口和守塔人的沉默。",
            "hidden_truth": "灯一旦点亮，就会有东西看见岸。",
            "emotional_contract": "潮湿而克制的不安。",
            "genre_tags": ["悬疑", "海港"],
            "scale": {"characters": 7, "scenes": 4, "modules": 9,
                      "rationale": "中篇标准结构"},
        }

    def test_adopted_concept_seeds_blueprint_as_unreviewed_llm_text(self) -> None:
        detail = self.workspace.create_from_concept(
            concept=self.concept(),
            scale_key="medium",
            brief="涨潮灯塔的想法",
        )

        story = detail["story"]
        self.assertEqual(story["title"], "涨潮灯塔")
        self.assertEqual(story["premise"], "灯塔只在涨潮时出现。")
        self.assertEqual(
            story["ai_plot"]["hidden_truth"],
            "灯一旦点亮，就会有东西看见岸。",
        )
        self.assertEqual(story["genre_tags"], ["悬疑", "海港"])
        self.assertEqual(story["target_duration_minutes"], "60-150")

        review = detail["review"]
        self.assertEqual(review["unreviewed_count"], 5)
        self.assertIn("ai_plot.hidden_truth", review["unreviewed_paths"])
        self.assertEqual(
            review["fields"]["premise"]["operation"], "concept"
        )
        self.assertEqual(review["authoring"]["scale"], "medium")
        self.assertEqual(review["authoring"]["brief"], "涨潮灯塔的想法")
        self.assertEqual(review["authoring"]["concept_scale"]["modules"], 9)

        saved = yaml.safe_load(
            (self.content_dir / "drafts" / f"{detail['id']}.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(saved["ai_plot"]["main_goal"], "弄清守塔人不点灯的原因。")

    def test_authoring_metadata_survives_a_later_manual_save(self) -> None:
        detail = self.workspace.create_from_concept(
            concept=self.concept(),
            scale_key="short",
            brief="想法",
        )
        story = detail["story"]
        story["premise"] = "创作者改写后的前提。"

        saved = self.workspace.save(
            detail["id"],
            story,
            expected_revision=detail["revision"],
            review_edits=[{
                "path": "premise", "source": "manual", "status": "manual",
            }],
        )

        self.assertEqual(saved["review"]["authoring"]["scale"], "short")
        self.assertNotIn("premise", saved["review"]["unreviewed_paths"])

    def test_proposal_endpoint_is_stateless(self) -> None:
        app = StudioApplication(self.workspace, no_log=True)

        payload = app.propose_concepts({
            "brief": "涨潮灯塔",
            "scale": "medium",
            "genre_tags": ["悬疑"],
        })

        self.assertEqual(len(payload["proposals"]), 3)
        self.assertEqual(payload["scale"], "medium")
        self.assertEqual(payload["brief"], "涨潮灯塔")
        self.assertEqual(self.workspace.list_stories(), [])


if __name__ == "__main__":
    unittest.main()
