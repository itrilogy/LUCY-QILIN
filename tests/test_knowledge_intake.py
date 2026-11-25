"""知识包下载后的入库契约：规范、全局知识根与安装计划提示。"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from store import protocol
from store.paths import default_knowledge_root
from store.plan import install_plan


def _knowledge_release() -> dict:
    return {
        "id": "luxi.knowledge-intake",
        "version": "1.0.0",
        "clients": ["reasonix"],
        "platforms": ["any"],
        "published_at": "2026-11-23",
        "artifact_sha256": "ab" * 32,
        "manifest": {
            "kind": "knowledge",
            "id": "luxi.knowledge-intake",
            "version": "1.0.0",
            "title": "入库样例",
            "platforms": ["any"],
            "depends": {"store": [], "external": [], "skills": [], "mcp": []},
            "secrets": [],
            "knowledge": {
                "entry": "00-导航概览.md",
                "taxonomy": {
                    "business_category": "knowledge-ops",
                    "business_domain": "method",
                    "knowledge_kind": "guide",
                },
                "security_level": "internal",
                "scope": "work",
            },
            "invoke": {"form": "read_notes", "entry": "00-导航概览.md"},
        },
    }


class KnowledgeIntakeProtocolTests(unittest.TestCase):
    def test_spec_text_names_tool_and_skill(self) -> None:
        body = protocol.text("knowledge-intake")
        self.assertIsNotNone(body)
        self.assertIn("knowledge_intake", body)
        self.assertIn("zhilin-intake", body)
        self.assertIn("~/.qilin/knowledge", body)
        self.assertIn("post_install", body)
        self.assertIn("security_level", body)

    def test_spec_resource_is_addressable(self) -> None:
        self.assertIn("knowledge-intake", protocol.NAMES)
        self.assertEqual(protocol.uri("knowledge-intake"), "qilin://spec/knowledge-intake")
        self.assertEqual(protocol.name_from_uri("qilin://spec/knowledge-intake"), "knowledge-intake")
        self.assertTrue(protocol.blurb("knowledge-intake"))

    def test_knowledge_spec_points_to_intake(self) -> None:
        body = protocol.text("knowledge")
        self.assertIn("qilin://spec/knowledge-intake", body)
        self.assertIn("~/.qilin/knowledge", body)


class KnowledgeRootTests(unittest.TestCase):
    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"QILIN_KNOWLEDGE_ROOT": "/tmp/qilin-knowledge"}, clear=False):
            self.assertEqual(default_knowledge_root(), Path("/tmp/qilin-knowledge"))

    def test_default_is_global_home(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QILIN_KNOWLEDGE_ROOT", None)
            self.assertEqual(default_knowledge_root(), Path.home() / ".qilin" / "knowledge")


class KnowledgePlanTests(unittest.TestCase):
    def test_plan_carries_post_install_hint(self) -> None:
        plan = install_plan(
            [_knowledge_release()],
            package_id="luxi.knowledge-intake",
            version="1.0.0",
            client="reasonix",
            platform="darwin-arm64",
            base_url="http://store.example",
        )
        self.assertEqual([step["op"] for step in plan["steps"]], ["download", "unpack", "verify"])
        hint = plan["post_install"][0]
        self.assertEqual(hint["op"], "knowledge_intake")
        self.assertEqual(hint["id"], "luxi.knowledge-intake")
        self.assertEqual(hint["version"], "1.0.0")
        self.assertEqual(hint["path"], "${KNOWLEDGE_ROOT}/luxi.knowledge-intake/1.0.0")
        self.assertEqual(hint["tool"], "knowledge_intake")
        self.assertEqual(hint["skill"], "zhilin-intake")
        self.assertFalse(hint["required"])

    def test_non_knowledge_plan_has_no_hint(self) -> None:
        release = _knowledge_release()
        release["manifest"]["kind"] = "skill"
        release["manifest"]["skill"] = {"entry": "SKILL.md"}
        release["manifest"]["invoke"] = {"form": "load_skill", "entry": "SKILL.md"}
        plan = install_plan(
            [release],
            package_id="luxi.knowledge-intake",
            version="1.0.0",
            client="reasonix",
            platform="darwin-arm64",
            base_url="http://store.example",
        )
        self.assertNotIn("post_install", plan)


if __name__ == "__main__":
    unittest.main()
