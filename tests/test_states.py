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
        self.assertEqual(advance("uploading", "start_validating", SUBMISSION, "x"), "validating")
        self.assertEqual(advance("uploading", "finish_ok", SUBMISSION, "x"), "pending")
        self.assertEqual(advance("validating", "finish_ok", SUBMISSION, "x"), "pending")
        self.assertEqual(advance("validating", "finish_retryable", SUBMISSION, "x"), "uploading")
        self.assertEqual(advance("validating", "finish_bad", SUBMISSION, "x"), "rejected")
        self.assertEqual(advance("validating", "recover", SUBMISSION, "x"), "uploading")
        self.assertEqual(advance("uploading", "expire", SUBMISSION, "x"), "expired")
        self.assertEqual(advance("validating", "expire", SUBMISSION, "x"), "expired")
        self.assertEqual(advance("pending", "approve", SUBMISSION, "x"), "published")
        self.assertEqual(advance("pending", "reject", SUBMISSION, "x"), "rejected")
        self.assertEqual(advance("rejected", "revise", SUBMISSION, "x"), "pending")
        self.assertEqual(advance("published", "unpublish", RELEASE, "x"), "unpublished")

    def test_every_parked_status_can_expire(self) -> None:
        # 清理器会对停在 uploading/validating 的提交发 expire；表里必须有边，
        # 否则 prune 会在运行时抛异常。
        for status in ("uploading", "validating"):
            with self.subTest(status=status):
                self.assertEqual(advance(status, "expire", SUBMISSION, "x"), "expired")

    def test_validating_cannot_be_approved(self) -> None:
        with self.assertRaises(StoreError):
            advance("validating", "approve", SUBMISSION, "x")

    def test_startup_recovers_validating_to_uploading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "store.db", root / "blobs")
            _, publisher = store.create_principal("作者", "publisher")
            result = store.submit(publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
            store._db.execute(
                "UPDATE submissions SET status = 'validating' WHERE id = ?", (result.submission_id,),
            )
            store._db.commit()
            store.close()
            reopened = Store(root / "store.db", root / "blobs")
            try:
                row = reopened._db.execute(
                    "SELECT status FROM submissions WHERE id = ?", (result.submission_id,),
                ).fetchone()
            finally:
                reopened.close()
            self.assertEqual(row["status"], "uploading", "启动时要把残留的 validating 退回可重试状态")

    def test_unknown_edge_raises(self) -> None:
        with self.assertRaises(StoreError) as caught:
            advance("published", "reject", SUBMISSION, "不能退回已发布的提交")
        self.assertEqual(caught.exception.rule, "SUBMISSION_STATE_INVALID")

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
            self.assertEqual(caught.exception.rule, "SUBMISSION_STATE_INVALID")

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
