"""catalog_index：发布/下架增量维护，启动可重建，搜索不再解析全量清单。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from store.service import Store
from tests.test_service import SUMMARY, skill_manifest


def _skill(version: str) -> str:
    return f"---\nname: gov\ndescription: {SUMMARY}\nversion: {version}\n---\n\n# 公文\n"


class CatalogIndexTests(unittest.TestCase):
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

    def _publish(self, version: str, package_id: str = "luxi.indexed") -> None:
        result = self.store.submit(
            self.publisher,
            skill_manifest(id=package_id, version=version),
            {"SKILL.md": _skill(version).encode()},
        )
        self.store.approve(self.admin, result.submission_id)

    def _row(self, package_id: str = "luxi.indexed"):
        return self.store._db.execute(
            "SELECT version FROM catalog_index WHERE package_id = ?", (package_id,)
        ).fetchone()

    def test_publish_moves_the_index_to_the_newest_version(self) -> None:
        self._publish("1.0.0")
        self.assertEqual(self._row()["version"], "1.0.0")
        self._publish("1.1.0")
        self.assertEqual(self._row()["version"], "1.1.0")
        page = self.store.search_page("indexed")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["version"], "1.1.0")

    def test_unpublish_falls_back_then_removes(self) -> None:
        self._publish("1.0.0")
        self._publish("1.1.0")
        self.store.unpublish(self.admin, "luxi.indexed", "1.1.0", "换一版")
        self.assertEqual(self._row()["version"], "1.0.0")
        self.store.unpublish(self.admin, "luxi.indexed", "1.0.0", "全部下架")
        self.assertIsNone(self._row())
        self.assertEqual(self.store.search_page("indexed")["total"], 0)

    def test_reopening_rebuilds_the_index(self) -> None:
        self._publish("1.0.0")
        self.store.close()
        reopened = Store(self.db, self.blobs)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.search_page("indexed")["total"], 1)
        self.assertEqual(reopened.search_page("indexed")["items"][0]["version"], "1.0.0")

    def test_like_wildcards_in_query_are_escaped(self) -> None:
        self._publish("1.0.0")
        self.assertEqual(self.store.search_page("%")["total"], 0)
        self.assertEqual(self.store.search_page("_")["total"], 0)

    def test_client_and_platform_filters_use_the_index(self) -> None:
        self._publish("1.0.0")
        self.assertEqual(self.store.search_page("", client="reasonix")["total"], 1)
        self.assertEqual(self.store.search_page("", client="cursor")["total"], 0)
        self.assertEqual(self.store.search_page("", platform="any")["total"], 1)
        self.assertEqual(self.store.search_page("", kind="skill")["total"], 1)
        self.assertEqual(self.store.search_page("", kind="knowledge")["total"], 0)

    def test_reopen_preserves_published_at_order_and_archives_release(self) -> None:
        self._publish("1.0.0", "luxi.older")
        self._publish("1.0.0", "luxi.newer")
        self.store._db.execute(
            "UPDATE releases SET published_at = '2026-01-01 00:00:00' WHERE package_id = 'luxi.older'"
        )
        self.store._db.execute(
            "UPDATE releases SET published_at = '2026-02-01 00:00:00' WHERE package_id = 'luxi.newer'"
        )
        rel = self.store._db.execute(
            "SELECT artifact_path FROM releases WHERE package_id = 'luxi.newer'"
        ).fetchone()
        self.assertTrue(str(rel["artifact_path"]).startswith("releases/"))
        resolved = self.store.resolve_blob_path(rel["artifact_path"])
        assert resolved is not None
        self.assertTrue(resolved.is_file())
        self.store._db.commit()
        self.store.close()
        reopened = Store(self.db, self.blobs)
        self.addCleanup(reopened.close)
        items = reopened.search_page("", sort="updated")["items"]
        self.assertEqual([item["id"] for item in items], ["luxi.newer", "luxi.older"])


if __name__ == "__main__":
    unittest.main()
