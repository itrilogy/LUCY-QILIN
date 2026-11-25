"""知识包题头解析的兼容性用例。

真实笔记来自 Obsidian、Word 导出与不同操作系统，常见写法有 BOM、CRLF、
`tags: [a, b]` 流式数组与 `description: >` 折叠标量。它们不应该被当成
「题头不合法」。
"""

from __future__ import annotations

import unittest

from store.validator.knowledge import _block, _fields, _frontmatter
from store.validator.validate import Report


def note(frontmatter: str, body: str = "正文\n") -> str:
    return "---\n" + frontmatter.strip("\n") + "\n---\n" + body


BASE = """
type: guide_note
status: evergreen
created: 2026-01-02
updated: 2026-02-03
tags:
  - topic/知识管理
  - method/qc
"""


def _errors(text: str) -> list[str]:
    report = Report()
    _frontmatter(report, "note.md", text)
    return [item.rule for item in report.findings if item.severity == "error"]


class ParserCompatTests(unittest.TestCase):
    def test_bom_and_crlf_are_accepted(self) -> None:
        text = ("\ufeff" + note(BASE)).replace("\n", "\r\n")
        self.assertIsNotNone(_block(text))
        self.assertEqual(_errors(text), [])

    def test_flow_array_tags_are_accepted(self) -> None:
        text = note(BASE.replace(
            "tags:\n  - topic/知识管理\n  - method/qc",
            "tags: [topic/知识管理, method/qc]",
        ))
        self.assertEqual(_errors(text), [])

    def test_plain_tag_string_is_rejected(self) -> None:
        text = note(BASE.replace(
            "tags:\n  - topic/知识管理\n  - method/qc",
            "tags: 知识管理, 质量管理",
        ))
        self.assertEqual(_errors(text), ["NOTE_FRONTMATTER"])

    def test_folded_description_does_not_break_the_header(self) -> None:
        text = note(BASE + "\ndescription: >\n  第一行说明\n  第二行说明\n")
        self.assertEqual(_errors(text), [])

    def test_prerequisite_accepts_dash_and_array(self) -> None:
        self.assertEqual(_errors(note(BASE + "\nprerequisite: 无\n")), [])
        self.assertEqual(_errors(note(BASE + "\nprerequisite:\n  - 00-导航概览\n")), [])

    def test_prerequisite_inline_link_is_rejected(self) -> None:
        self.assertIn("NOTE_FRONTMATTER", _errors(note(BASE + "\nprerequisite: [[00-导航概览]]\n")))

    def test_flow_array_parser_shape(self) -> None:
        fields = _fields("tags: [a, b]\nempty: []\n")
        self.assertEqual(fields["tags"], ("", ["a", "b"]))
        self.assertEqual(fields["empty"], ("[]", []))


if __name__ == "__main__":
    unittest.main()
