"""知识包：笔记链接只在包内闭合，安装只是解压。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from store.plan import install_plan
from store.validator.validate import validate_manifest, validate_path
from tests.test_service import DESC, SUMMARY


class KnowledgeTests(unittest.TestCase):
    def test_closed_notes_pass_and_a_broken_link_does_not(self) -> None:
        root = _pack({
            "00-导航概览.md": _note("导航", ["01-概念"], type="map_of_content"),
            "01-概念.md": _note("概念", ["00-导航概览"]),
        })
        self.assertEqual(validate_path(root).rules(), [])
        broken = _pack({
            "00-导航概览.md": _note("导航", ["没有这一篇"], type="map_of_content"),
            "01-概念.md": _note("概念", ["00-导航概览"]),
        })
        self.assertIn("NOTE_LINK", validate_path(broken).rules())

    def test_alias_is_not_a_link_target_and_fences_are_skipped(self) -> None:
        text = _note("概念", ["00-导航概览"]).replace(
            "tags:\n  - topic/knowledge-management\n",
            "tags:\n  - 示例\naliases:\n  - 概念甲\n",
            1,
        )
        linked = _note("导航", ["概念甲"], type="map_of_content")
        self.assertIn("NOTE_LINK", validate_path(_pack({
            "00-导航概览.md": linked,
            "01-概念.md": text,
        })).rules())
        fenced = _note("导航", ["01-概念"], type="map_of_content") + "\n```\n[[没有这一篇]]\n```\n"
        self.assertEqual(validate_path(_pack({
            "00-导航概览.md": fenced,
            "01-概念.md": _note("概念", ["00-导航概览"]),
        })).rules(), [])

    def test_frontmatter_file_and_scope(self) -> None:
        bare = _pack({"00-导航概览.md": "# 没有题头\n\n[[01-概念]]\n", "01-概念.md": _note("概念", [])})
        self.assertIn("NOTE_FRONTMATTER", validate_path(bare).rules())
        extra = _pack({
            "00-导航概览.md": _note("导航", [], type="map_of_content"),
            "图.png": "not-a-note",
        })
        self.assertIn("NOTE_FILE", validate_path(extra).rules())
        wide = _manifest(platforms=["darwin-arm64"])
        self.assertIn("KNOWLEDGE_SCOPE", validate_manifest(wide).rules())
        linked = _manifest()
        linked["depends"] = {"store": [], "external": [], "skills": [{"id": "luxi.gov-writer"}], "mcp": []}
        self.assertIn("KNOWLEDGE_SCOPE", validate_manifest(linked).rules())
        self.assertEqual(validate_manifest(_manifest()).rules(), [])

    def test_install_unpacks_notes_and_does_not_connect(self) -> None:
        release = {
            "id": "luxi.sample-notes",
            "version": "1.0.0",
            "clients": ["reasonix"],
            "platforms": ["any"],
            "artifact_sha256": "ab" * 32,
            "manifest": _manifest(),
        }
        plan = install_plan(
            [release],
            package_id="luxi.sample-notes",
            version="1.0.0",
            client="reasonix",
            platform="darwin-arm64",
            base_url="http://store.example",
        )
        self.assertEqual([step["op"] for step in plan["steps"]], ["download", "unpack", "verify"])
        self.assertIn("KNOWLEDGE_ROOT", plan["steps"][1]["dest"])
        self.assertEqual(plan["steps"][2]["entry"], "00-导航概览.md")
        self.assertNotIn("transport", plan["steps"][2])


def _pack(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    manifest = __import__("json").dumps(_manifest())
    (root / "manifest.json").write_text(manifest, encoding="utf-8")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _manifest(**overrides: object) -> dict:
    body = {
        "spec_version": 1,
        "id": "luxi.sample-notes",
        "version": "1.0.0",
        "kind": "knowledge",
        "title": "示例笔记",
        "summary": SUMMARY,
        "description": DESC,
        "author": {"display_name": "鹿溪"},
        "platforms": ["any"],
        "changes": "首个发布",
        "clients": ["reasonix"],
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
    }
    body.update(overrides)
    return body


def _note(title: str, links: list[str], *, type: str = "guide_note") -> str:
    body = "\n".join(f"- [[{name}]]" for name in links)
    return (
        "---\n"
        f"type: {type}\n"
        "status: evergreen\n"
        "created: 2026-11-12\n"
        "updated: 2026-11-12\n"
        "tags:\n"
        "  - topic/knowledge-management\n"
        "prerequisite: 无\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}\n"
    )


if __name__ == "__main__":
    unittest.main()
