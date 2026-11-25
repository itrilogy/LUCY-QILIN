"""修订 / 新版本时清单保真（评审 P1-8）。

门户表单只能编辑第一条外部依赖和第一条资源地址，也不完整表达 knowledge。
旧实现用表单结果重建清单，于是修订一次就丢：第二条外部依赖、带空格的版本范围
（">=1.5 <2" 被截成 ">=1.5"）、其它平台资源地址、knowledge 的分类等字段。

合并逻辑放在 `store/portal/manifest_merge.js`（独立文件、不依赖 DOM），
这里用 node 直接跑它。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MERGE = ROOT / "store" / "portal" / "manifest_merge.js"
NODE = shutil.which("node")


def _node(script: str) -> dict:
    assert NODE is not None
    result = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def _js(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


@unittest.skipIf(NODE is None, "需要 node")
class PortalMergeTests(unittest.TestCase):
    def _merge(self, built: dict, base: dict | None, ext: dict | None = None) -> dict:
        script = f"""
const M = require({_js(str(MERGE))});
process.stdout.write(JSON.stringify(M.mergeRevised(
  {_js(built)}, {(_js(base) if base is not None else "null")}, {_js(ext or {})}
)));
"""
        return _node(script)

    def _parse(self, text: str) -> list:
        script = f"""
const M = require({_js(str(MERGE))});
process.stdout.write(JSON.stringify(M.parseDepLines({_js(text)})));
"""
        return _node(script)

    def test_version_range_with_spaces_is_not_truncated(self) -> None:
        parsed = self._parse("luxi.pandoc >=1.5 <2\nluxi.rclone 1.2.3")
        self.assertEqual(parsed, [
            {"id": "luxi.pandoc", "version": ">=1.5 <2"},
            {"id": "luxi.rclone", "version": "1.2.3"},
        ])
        script = f"""
const M = require({_js(str(MERGE))});
process.stdout.write(JSON.stringify(M.formatDepLines({_js(parsed)})));
"""
        self.assertEqual(_node(script), "luxi.pandoc >=1.5 <2\nluxi.rclone 1.2.3")

    def _built(self, **external: object) -> dict:
        entry = {
            "name": "python",
            "version_range": ">=3.11",
            "detect": ["python3", "--version"],
            "install_guide": "安装 Python 3.11 或以上版本，装好后确认 python3 可用。",
            "resources": [{"title": "python", "url": "https://www.python.org/downloads/", "platform": "any"}],
        }
        entry.update(external)
        return {
            "spec_version": 1,
            "id": "luxi.tool", "version": "2.0.0", "kind": "skill",
            "title": "工具", "summary": "一句话", "description": "说明",
            "changes": "新版本", "author": {"display_name": "鹿溪"},
            "platforms": ["any"], "clients": ["reasonix"],
            "depends": {"store": [], "skills": [], "mcp": [], "external": [entry]},
            "secrets": [],
            "skill": {"entry": "SKILL.md"}, "invoke": {"form": "load_skill", "entry": "SKILL.md"},
        }

    def _base(self) -> dict:
        base = self._built()
        base["version"] = "1.0.0"
        base["depends"]["external"][0] = {
            "name": "python",
            "version_range": ">=3.11 <4",
            # 参数里带空格：表单会把它拍成 "python3 -c import sys"
            "detect": ["python3", "-c", "import sys"],
            "install_guide": "安装 Python 3.11 或以上版本，装好后确认 python3 可用。",
            "resources": [
                {"title": "python", "url": "https://www.python.org/downloads/", "platform": "any"},
                {"title": "mirror", "url": "https://mirror.internal/python", "platform": "linux-amd64"},
            ],
        }
        base["depends"]["external"].append({
            "name": "pandoc",
            "version_range": ">=3",
            "detect": ["pandoc", "--version"],
            "install_guide": "从下面的地址安装 Pandoc，装好后保证 pandoc 在 PATH 中。",
            "resources": [{"title": "pandoc", "url": "https://pandoc.org/installing.html", "platform": "any"}],
        })
        return base

    def test_second_external_dependency_is_kept(self) -> None:
        merged = self._merge(self._built(), self._base(), {
            "name": "python", "detect": "python3 -c import sys", "url": "https://www.python.org/downloads/",
        })
        external = merged["depends"]["external"]
        self.assertEqual([item["name"] for item in external], ["python", "pandoc"])
        self.assertEqual(external[1], self._base()["depends"]["external"][1], "第二条不能被动过")

    def test_detect_array_and_extra_resources_are_kept(self) -> None:
        merged = self._merge(self._built(), self._base(), {
            "name": "python", "detect": "python3 -c import sys", "url": "https://www.python.org/downloads/",
        })
        first = merged["depends"]["external"][0]
        self.assertEqual(first["detect"], ["python3", "-c", "import sys"], "带空格的参数要保真")
        self.assertEqual(len(first["resources"]), 2, "其它平台资源地址要保留")
        self.assertEqual(first["version_range"], ">=3.11", "表单里的范围以表单为准")

    def test_changed_url_only_replaces_the_first_resource(self) -> None:
        merged = self._merge(self._built(), self._base(), {
            "name": "python", "detect": "python3 -c import sys", "url": "https://new.example/python",
        })
        resources = merged["depends"]["external"][0]["resources"]
        self.assertEqual(resources[0]["url"], "https://new.example/python")
        self.assertEqual(resources[1]["url"], "https://mirror.internal/python")

    def test_knowledge_fields_survive_a_revise(self) -> None:
        base = {
            "id": "luxi.notes", "version": "1.0.0", "kind": "knowledge",
            "depends": {"store": [{"id": "luxi.officecli", "version": "1.4.0"}], "external": [], "skills": [], "mcp": []},
            "secrets": [{"name": "OPENAI_KEY", "description": "调用模型"}],
            "knowledge": {
                "entry": "00-导航.md",
                "taxonomy": {"business_category": "制度", "business_domain": "公文", "knowledge_kind": "操作指引"},
                "classification": "内部",
                "scope": "公文写作",
                "vocabulary": ["行文", "请示"],
            },
            "platforms": ["any"], "clients": ["reasonix"],
        }
        built = {
            "spec_version": 1, "id": "luxi.notes", "version": "1.1.0", "kind": "knowledge",
            "title": "笔记", "summary": "一句话", "description": "说明", "changes": "修订",
            "author": {"display_name": "鹿溪"}, "platforms": ["any"], "clients": ["reasonix"],
            "depends": {"store": [], "skills": [], "mcp": [], "external": []},
            "secrets": [],
            "knowledge": {"entry": "01-新导航.md"},
            "invoke": {"form": "read_notes", "entry": "01-新导航.md"},
        }
        merged = self._merge(built, base)
        self.assertEqual(merged["knowledge"]["entry"], "01-新导航.md")
        self.assertEqual(merged["knowledge"]["taxonomy"]["business_domain"], "公文")
        self.assertEqual(merged["knowledge"]["classification"], "内部")
        self.assertEqual(merged["depends"]["store"][0]["id"], "luxi.officecli")
        self.assertEqual(merged["secrets"][0]["name"], "OPENAI_KEY")

    def test_new_submission_is_untouched(self) -> None:
        built = self._built()
        self.assertEqual(self._merge(built, None), built)


if __name__ == "__main__":
    unittest.main()
