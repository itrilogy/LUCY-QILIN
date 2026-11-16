"""知识包 v2 规则：词表、密级、入口、锚点与孤篇。

这些用例证明词表不是文档摆设：不在词表里的类目、领域、主题词都会被拒；
本地主题词可以在 `knowledge.topics` 里声明后使用。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from store.validator.validate import validate_path
from tests.test_service import DESC, SUMMARY

TAXONOMY = {
    "business_category": "knowledge-ops",
    "business_domain": "method",
    "knowledge_kind": "guide",
}


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
            "taxonomy": dict(TAXONOMY),
            "security_level": "internal",
            "scope": "work",
        },
        "invoke": {"form": "read_notes", "entry": "00-导航概览.md"},
    }
    body.update(overrides)
    return body


def _note(
    title: str,
    links: list[str],
    *,
    type: str = "guide_note",
    tags: str = "  - topic/knowledge-management",
    extra: str = "",
) -> str:
    body = "\n".join(f"- [[{name}]]" for name in links)
    return (
        "---\n"
        f"type: {type}\n"
        "status: evergreen\n"
        "created: 2026-11-01\n"
        "updated: 2026-11-02\n"
        "tags:\n"
        f"{tags}\n"
        f"{extra}"
        "prerequisite: 无\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )


def _pack(files: dict[str, str], manifest: dict | None = None) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "manifest.json").write_text(
        json.dumps(manifest or _manifest(), ensure_ascii=False), encoding="utf-8"
    )
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _rules(root: Path) -> list[str]:
    return validate_path(root).rules()


def _findings(root: Path) -> list[tuple[str, str]]:
    return [(item.rule, item.severity) for item in validate_path(root).findings]


class VocabularyEnforcementTests(unittest.TestCase):
    def test_unknown_category_is_rejected(self) -> None:
        manifest = _manifest()
        manifest["knowledge"]["taxonomy"] = {
            "business_category": "not-a-category",
            "business_domain": "method",
            "knowledge_kind": "guide",
        }
        root = _pack({"00-导航概览.md": _note("导航", [], type="map_of_content")}, manifest)
        self.assertIn("KNOWLEDGE_TAXONOMY", _rules(root))

    def test_domain_must_belong_to_the_category(self) -> None:
        manifest = _manifest()
        manifest["knowledge"]["taxonomy"] = {
            "business_category": "knowledge-ops",
            "business_domain": "journal",  # 属于 research
            "knowledge_kind": "guide",
        }
        root = _pack({"00-导航概览.md": _note("导航", [], type="map_of_content")}, manifest)
        self.assertIn("KNOWLEDGE_TAXONOMY", _rules(root))

    def test_taxonomy_is_required(self) -> None:
        manifest = _manifest()
        manifest["knowledge"].pop("taxonomy")
        root = _pack({"00-导航概览.md": _note("导航", [], type="map_of_content")}, manifest)
        self.assertIn("KNOWLEDGE_TAXONOMY", _rules(root))

    def test_unknown_vocabulary_version_is_rejected(self) -> None:
        manifest = _manifest()
        manifest["knowledge"]["vocabulary"] = "kv/2"
        root = _pack({"00-导航概览.md": _note("导航", [], type="map_of_content")}, manifest)
        self.assertIn("KNOWLEDGE_TAXONOMY", _rules(root))

    def test_colon_free_topic_is_rejected(self) -> None:
        root = _pack({"00-导航概览.md": _note("导航", [], type="map_of_content", tags="  - 知识管理")})
        self.assertIn("NOTE_FRONTMATTER", _rules(root))

    def test_unknown_topic_is_rejected(self) -> None:
        root = _pack({"00-导航概览.md": _note("导航", [], type="map_of_content", tags="  - topic/不存在的主题")})
        self.assertIn("NOTE_FRONTMATTER", _rules(root))

    def test_local_topic_can_be_declared_by_the_pack(self) -> None:
        manifest = _manifest()
        manifest["knowledge"]["topics"] = ["本地主题"]
        root = _pack({"00-导航概览.md": _note("导航", [], type="map_of_content", tags="  - topic/本地主题")}, manifest)
        self.assertEqual(_rules(root), [])

    def test_unknown_method_is_rejected(self) -> None:
        root = _pack({"00-导航概览.md": _note(
            "导航", [], type="map_of_content",
            tags="  - topic/knowledge-management\n  - method/没有这个方法",
        )})
        self.assertIn("NOTE_FRONTMATTER", _rules(root))


class PackRuleTests(unittest.TestCase):
    def test_entry_must_be_map_of_content(self) -> None:
        root = _pack({"00-导航概览.md": _note("导航", [], type="guide_note")})
        self.assertIn("NOTE_ENTRY", _rules(root))

    def test_entry_must_use_zero_zero_prefix(self) -> None:
        manifest = _manifest()
        manifest["knowledge"]["entry"] = "01-导航.md"
        manifest["invoke"]["entry"] = "01-导航.md"
        root = _pack({"01-导航.md": _note("导航", [], type="map_of_content")}, manifest)
        self.assertIn("NOTE_ENTRY", _rules(root))

    def test_note_security_cannot_exceed_pack(self) -> None:
        root = _pack({"00-导航概览.md": _note(
            "导航", [], type="map_of_content",
            extra="security_level: confidential\n",
        )})
        self.assertIn("NOTE_SECURITY", _rules(root))

    def test_skill_entry_is_not_a_note(self) -> None:
        root = _pack({
            "00-导航概览.md": _note("导航", ["01-概念"], type="map_of_content"),
            "01-概念.md": _note("概念", ["00-导航概览"]),
            "SKILL.md": "# 技能入口\n",
        })
        self.assertIn("NOTE_FILE", _rules(root))

    def test_note_filename_must_be_numbered(self) -> None:
        root = _pack({
            "00-导航概览.md": _note("导航", ["概念"], type="map_of_content"),
            "概念.md": _note("概念", ["00-导航概览"]),
        })
        self.assertIn("NOTE_FILE", _rules(root))

    def test_broken_anchor_is_a_link_error(self) -> None:
        root = _pack({
            "00-导航概览.md": _note("导航", ["01-概念#不存在的标题"], type="map_of_content"),
            "01-概念.md": _note("概念", ["00-导航概览"]),
        })
        self.assertIn("NOTE_LINK", _rules(root))

    def test_existing_anchor_passes(self) -> None:
        root = _pack({
            "00-导航概览.md": _note("导航", ["01-概念#小节"], type="map_of_content"),
            "01-概念.md": _note("概念", ["00-导航概览"]) + "\n## 小节\n",
        })
        self.assertEqual(_rules(root), [])

    def test_orphan_is_a_warning_not_an_error(self) -> None:
        root = _pack({
            "00-导航概览.md": _note("导航", [], type="map_of_content"),
            "01-概念.md": _note("概念", ["00-导航概览"]),
        })
        self.assertEqual(_rules(root), [])
        self.assertIn(("NOTE_ORPHAN", "warning"), _findings(root))

    def test_knowledge_kind_must_match_type(self) -> None:
        root = _pack({"00-导航概览.md": _note(
            "导航", [], type="map_of_content",
            extra="knowledge_kind: guide\n",
        )})
        self.assertIn("NOTE_FRONTMATTER", _rules(root))


class ExamplePackTests(unittest.TestCase):
    def test_shipped_example_is_a_valid_pack(self) -> None:
        spec = Path(__file__).resolve().parents[1] / "store" / "spec" / "knowledge-example.json"
        example = json.loads(spec.read_text(encoding="utf-8"))
        root = _pack(example["files"], example["manifest"])
        self.assertEqual(_rules(root), [])
        # id 不在旧的技能登记表里会有 ID_UNREGISTERED 警告；那是登记表遗留，见演进规划第 6 节。
        self.assertTrue(all(severity == "warning" for _rule, severity in _findings(root)))


if __name__ == "__main__":
    unittest.main()
