"""发布与提交入库的事务边界（评审 P2-2 / P2-3 / P2-5）。

- 发布：releases、货架索引、submissions 状态、审计必须在同一个事务里。
  任何一步失败都要整体回滚，不能出现"已发布但货架搜不到""发布没有审计"。
- 发布前核对制品：文件在、校验和与提交记录一致，否则拒绝（下载必 404 的版本不能上架）。
- 提交入库：资产行、状态、审计同一个事务；审计失败不能把半成品资产留下。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from store.blobs import write_zip
from store.service import Store, StoreError
from tests.test_revise import runtime_manifest
from tests.test_service import SKILL, skill_manifest


def _zip_bytes(manifest: dict, files: dict[str, bytes]) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pkg"
        root.mkdir()
        (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        for name, data in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        dest = Path(tmp) / "out.zip"
        write_zip(root, dest)
        return dest.read_bytes()


class PublishAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("审核", "reviewer")

    def _pending(self, package_id: str = "luxi.atomic") -> str:
        result = self.store.submit(
            self.publisher, skill_manifest(id=package_id), {"SKILL.md": SKILL.encode()},
        )
        self.assertEqual(result.status, "pending", result.rules)
        return result.submission_id

    def _release_count(self) -> int:
        return self.store._db.execute("SELECT COUNT(*) FROM releases").fetchone()[0]

    def _index_count(self) -> int:
        return self.store._db.execute("SELECT COUNT(*) FROM catalog_index").fetchone()[0]

    def _audit_count(self, action: str) -> int:
        return self.store._db.execute(
            "SELECT COUNT(*) FROM audit WHERE action = ?", (action,),
        ).fetchone()[0]

    def test_missing_artifact_is_refused(self) -> None:
        submission_id = self._pending()
        row = self.store._submission(submission_id)
        archive = self.store.resolve_blob_path(row["artifact_path"])
        assert archive is not None
        archive.unlink()
        with self.assertRaises(StoreError) as caught:
            self.store.approve(self.admin, submission_id)
        self.assertEqual(caught.exception.rule, "CHECKSUM")
        self.assertEqual(self._release_count(), 0)
        self.assertEqual(self.store._submission(submission_id)["status"], "pending")

    def test_tampered_artifact_is_refused(self) -> None:
        submission_id = self._pending()
        row = self.store._submission(submission_id)
        archive = self.store.resolve_blob_path(row["artifact_path"])
        assert archive is not None
        archive.write_bytes(b"not the same bytes")
        with self.assertRaises(StoreError) as caught:
            self.store.approve(self.admin, submission_id)
        self.assertEqual(caught.exception.rule, "CHECKSUM")
        self.assertEqual(self._release_count(), 0)

    def test_index_failure_rolls_back_the_publish(self) -> None:
        submission_id = self._pending()
        with mock.patch.object(
            self.store, "_refresh_catalog_index", side_effect=RuntimeError("index boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.store.approve(self.admin, submission_id)
        self.assertEqual(self._release_count(), 0, "发布不能只落一半")
        self.assertEqual(self.store._submission(submission_id)["status"], "pending")
        self.assertEqual(self._audit_count("approve"), 0)

    def test_audit_failure_rolls_back_the_publish(self) -> None:
        submission_id = self._pending()
        with mock.patch.object(self.store, "_audit", side_effect=RuntimeError("audit boom")):
            with self.assertRaises(RuntimeError):
                self.store.approve(self.admin, submission_id)
        self.assertEqual(self._release_count(), 0)
        self.assertEqual(self._index_count(), 0)
        self.assertEqual(self.store._submission(submission_id)["status"], "pending")

    def test_happy_path_writes_release_index_and_audit_together(self) -> None:
        submission_id = self._pending()
        self.store.approve(self.admin, submission_id)
        self.assertEqual(self._release_count(), 1)
        self.assertEqual(self._index_count(), 1)
        self.assertEqual(self._audit_count("approve"), 1)
        self.assertEqual(self.store._submission(submission_id)["status"], "published")


class FinishCommitAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")

    def test_audit_failure_keeps_the_submission_uploadable(self) -> None:
        manifest, files = runtime_manifest()
        payload = _zip_bytes(manifest, files)
        started = self.store.begin_submit(self.publisher, manifest, byte_size=len(payload))
        submission_id = started["submission_id"]
        self.store.save_upload(self.publisher, submission_id, payload)

        with mock.patch.object(self.store, "_audit", side_effect=RuntimeError("audit boom")):
            with self.assertRaises(RuntimeError):
                self.store.finish_submit(self.publisher, submission_id)

        row = self.store._submission(submission_id)
        self.assertEqual(row["status"], "uploading", "失败后应回到可重试状态")
        assets = self.store._db.execute(
            "SELECT COUNT(*) FROM submission_assets WHERE submission_id = ?", (submission_id,),
        ).fetchone()[0]
        self.assertEqual(assets, 0, "半成品资产行不能留下")

        # 再跑一次能成功，说明清理干净、没有卡在 validating。
        done = self.store.finish_submit(self.publisher, submission_id)
        self.assertEqual(done.status, "pending", done.rules)


if __name__ == "__main__":
    unittest.main()
