"""状态迁移表：非法迁移必须被拒绝，且不改库。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from store.errors import StoreError
from store.service import Store
from store.states import RELEASE, SUBMISSION, advance
from tests.test_service import SKILL, skill_manifest


class TransitionTests(unittest.TestCase):
    def test_table_edges(self) -> None:
        self.assertEqual(advance("uploading", "finish_ok", SUBMISSION, "x"), "pending")
        self.assertEqual(advance("pending", "approve", SUBMISSION, "x"), "published")
        self.assertEqual(advance("pending", "reject", SUBMISSION, "x"), "rejected")
        self.assertEqual(advance("rejected", "revise", SUBMISSION, "x"), "pending")
        self.assertEqual(advance("published", "unpublish", RELEASE, "x"), "unpublished")

    def test_unknown_edge_raises(self) -> None:
        with self.assertRaises(StoreError) as caught:
            advance("published", "reject", SUBMISSION, "不能退回已发布的提交")
        self.assertEqual(caught.exception.rule, "VERSION_UNAVAILABLE")

    def test_published_submission_cannot_be_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "store.db", root / "blobs")
            self.addCleanup(store.close)
            _, publisher = store.create_principal("作者", "publisher")
            _, admin = store.create_principal("管理员", "admin")
            result = store.submit(publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
            self.assertEqual(result.status, "pending")
            store.approve(admin, result.submission_id)

            with self.assertRaises(StoreError) as caught:
                store.reject(admin, result.submission_id, "不想要了")
            self.assertEqual(caught.exception.rule, "VERSION_UNAVAILABLE")

            row = store._db.execute(
                "SELECT status FROM submissions WHERE id = ?", (result.submission_id,)
            ).fetchone()
            self.assertEqual(row["status"], "published", "被拒绝的迁移不能改库")

    def test_withdraw_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "store.db", root / "blobs")
            self.addCleanup(store.close)
            _, publisher = store.create_principal("作者", "publisher")
            result = store.submit(publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
            self.assertEqual(result.status, "pending")
            store.withdraw(publisher, result.submission_id)
            with self.assertRaises(StoreError):
                store.withdraw(publisher, result.submission_id)


if __name__ == "__main__":
    unittest.main()
