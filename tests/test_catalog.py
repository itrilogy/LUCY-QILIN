"""规格对齐的回归用例：对外视图、搜索范围、title 校验。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from store.http_api import serve
from store.service import Store
from store.validator.validate import validate_manifest
from tests.test_service import DESC, SKILL, SUMMARY, skill_manifest


class CatalogViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")
        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _publish(self, body: dict, skill: str = SKILL) -> None:
        result = self.store.submit(self.publisher, body, {"SKILL.md": skill.encode()})
        self.assertEqual(result.status, "pending", result.rules)
        self.store.approve(self.admin, result.submission_id)

    def test_catalog_and_describe_hide_publisher_id(self) -> None:
        self._publish(skill_manifest())
        catalog = json.load(urllib.request.urlopen(self.base + "/v1/catalog"))
        item = catalog["items"][0]
        self.assertEqual(item["author"], {"display_name": "鹿溪"})
        described = json.load(
            urllib.request.urlopen(self.base + "/v1/packages/luxi.gov-writer/3.2.0")
        )
        self.assertEqual(described["author"], {"display_name": "鹿溪"})
        # 商店认识的字段保持原样，安装说明和详情页还要用。
        self.assertIn("invoke", described)
        self.assertIn("skill", described)
        self.assertEqual(described["summary"], SUMMARY)

    def test_unknown_top_level_field_stays_on_review_only(self) -> None:
        body = skill_manifest()
        body["internal_note"] = "仅审核可见"
        result = self.store.submit(self.publisher, body, {"SKILL.md": SKILL.encode()})
        self.assertEqual(result.status, "pending", result.rules)
        sheet = self.store.get_submission(self.publisher, result.submission_id)
        self.assertEqual(sheet["manifest"]["internal_note"], "仅审核可见")
        self.assertIn("publisher_id", sheet["manifest"]["author"])
        self.store.approve(self.admin, result.submission_id)
        described = json.load(
            urllib.request.urlopen(self.base + "/v1/packages/luxi.gov-writer/3.2.0")
        )
        self.assertNotIn("internal_note", described)
        self.assertNotIn("publisher_id", described["author"])
        self.assertIn("invoke", described)

    def test_search_covers_description(self) -> None:
        self._publish(skill_manifest())
        # DESC 里有「成稿」二字，title 与 summary 都不含它。
        self.assertIn("成稿", DESC)
        self.assertNotIn("成稿", SUMMARY)
        if "成稿" in str(skill_manifest().get("title") or ""):
            self.skipTest("夹具的 title 也含该词，无法区分")
        found = self.store.search("成稿")
        self.assertEqual([item["id"] for item in found], ["luxi.gov-writer"])

    def test_search_still_ignores_unrelated_words(self) -> None:
        self._publish(skill_manifest())
        self.assertEqual(self.store.search("完全不相关的词"), [])


class TitleTests(unittest.TestCase):
    def _rules(self, **override: object) -> list[str]:
        return validate_manifest(skill_manifest(**override)).rules()

    def test_valid_title_passes(self) -> None:
        self.assertEqual(self._rules(), [])

    def test_missing_or_blank_title_is_rejected(self) -> None:
        for value in (None, "", "   "):
            with self.subTest(value=value):
                body = skill_manifest()
                body["title"] = value
                self.assertIn("TITLE", validate_manifest(body).rules())

    def test_wrong_type_title_is_rejected(self) -> None:
        for value in (123, [], {}, True):
            with self.subTest(value=value):
                body = skill_manifest()
                body["title"] = value
                self.assertIn("TITLE", validate_manifest(body).rules())

    def test_overlong_title_is_rejected(self) -> None:
        self.assertIn("TITLE", self._rules(title="栖" * 41))
        self.assertEqual(self._rules(title="栖" * 40), [])


if __name__ == "__main__":
    unittest.main()

