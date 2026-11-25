"""FTS5 trigram：3 字以上走 MATCH，短词回退 LIKE；发布/下架/重启后仍一致。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from store.service import Store
from tests.test_service import SKILL, skill_manifest


class CatalogFtsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "store.db"
        self.blobs = self.root / "blobs"
        self.store = Store(self.db, self.blobs)
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")
        result = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.store.approve(self.admin, result.submission_id)

    def test_fts_is_enabled_and_holds_the_latest_version(self) -> None:
        self.assertTrue(self.store._fts_ready)
        row = self.store._db.execute("SELECT title FROM catalog_fts").fetchone()
        self.assertEqual(row["title"], "公文撰写")

    def test_three_characters_go_through_match(self) -> None:
        for query in ("公文撰", "流水线", "公文撰写"):
            with self.subTest(query=query):
                self.assertEqual(self.store.search_page(query)["total"], 1)

    def test_short_words_fall_back_to_like(self) -> None:
        self.assertEqual(self.store.search_page("公文")["total"], 1)
        self.assertEqual(self.store.search_page("撰写")["total"], 1)

    def test_package_id_still_matches(self) -> None:
        self.assertEqual(self.store.search_page("luxi.gov")["total"], 1)

    def test_special_characters_do_not_break_the_query(self) -> None:
        for query in ('"', "%", "_", "公文%", '公文"撰'):
            with self.subTest(query=query):
                self.assertEqual(self.store.search_page(query)["total"], 0)

    def test_unpublish_removes_the_fts_row(self) -> None:
        self.store.unpublish(self.admin, "luxi.gov-writer", "3.2.0", "下架")
        self.assertEqual(self.store.search_page("公文撰")["total"], 0)
        self.assertIsNone(self.store._db.execute("SELECT 1 FROM catalog_fts").fetchone())

    def test_reopening_rebuilds_fts(self) -> None:
        self.store.close()
        reopened = Store(self.db, self.blobs)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.search_page("公文撰")["total"], 1)


if __name__ == "__main__":
    unittest.main()
