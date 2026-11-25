"""数据一致性：读路径不写库、下架可以恢复。

- P2-1：`whoami` 走进过期会话时不能再 DELETE + commit——那会把只读事务升级成
  写事务，并发下撞 SQLITE_BUSY_SNAPSHOT（`busy_timeout` 对它无效）。
- P2-6：下架是"从货架撤下"，不是"这个版本号永久报废"。重新提交同一版本并发布
  后应当回到货架，而不是只能升版本号。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from store.service import Store
from tests.test_service import SKILL, skill_manifest


class ReadPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)

    def test_expired_session_is_not_deleted_on_a_read(self) -> None:
        _profile, session, _key = self.store.register_account(
            "reader.one", "读者", "读者", "", "", "Reader-One-2026",
        )
        self.store._db.execute(
            "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
            (int(time.time()) - 10, __import__("store.base", fromlist=["_hash"])._hash(session)),
        )
        self.store._db.commit()
        self.assertEqual(self.store.whoami(session)["role"], "anonymous")
        rows = self.store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(rows, 1, "读路径不能顺手删行；过期清理交给 prune")

    def test_expired_session_is_cleaned_by_prune(self) -> None:
        self.store._prune_stale_uploads(max_age_hours=1, now=None)
        rows = self.store._db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(rows, 0, "按天清理仍然要把过期会话删掉")


class RepublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.reviewer = self.store.create_principal("审核", "reviewer")

    def _publish(self) -> str:
        result = self.store.submit(
            self.publisher,
            skill_manifest(id="luxi.republish"),
            {"SKILL.md": SKILL.encode()},
        )
        self.assertEqual(result.status, "pending", result.rules)
        self.store.approve(self.reviewer, result.submission_id)
        return result.submission_id

    def test_unpublished_version_can_be_published_again(self) -> None:
        self._publish()
        self.assertIsNotNone(self.store.describe("luxi.republish", "3.2.0"))

        self.store.unpublish(self.reviewer, "luxi.republish", "3.2.0", "误下架")
        with self.assertRaises(Exception):
            self.store.describe("luxi.republish", "3.2.0")
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM catalog_index WHERE package_id = ?", ("luxi.republish",),
            ).fetchone()[0],
            0,
        )

        # 同一版本号重新提交：以前会被 VERSION_IMMUTABLE 挡住，只能升版本。
        again = self.store.submit(
            self.publisher,
            skill_manifest(id="luxi.republish", changes="误下架后恢复上架，内容与上一版一致，只重新发布。"),
            {"SKILL.md": SKILL.encode()},
        )
        self.assertEqual(again.status, "pending", again.rules)
        self.store.approve(self.reviewer, again.submission_id)

        payload = self.store.describe("luxi.republish", "3.2.0")
        self.assertEqual(payload["id"], "luxi.republish")
        rows = self.store._db.execute(
            "SELECT COUNT(*), MAX(unpublished_at) FROM releases WHERE package_id = ?",
            ("luxi.republish",),
        ).fetchone()
        self.assertEqual(rows[0], 1, "重新发布应复用同一行，而不是留两条")
        self.assertIsNone(rows[1])
        self.assertEqual(
            self.store._db.execute(
                "SELECT COUNT(*) FROM catalog_index WHERE package_id = ?", ("luxi.republish",),
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
