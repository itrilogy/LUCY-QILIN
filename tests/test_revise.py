"""修订失败时不能把旧制品弄丢。

旧实现先删 `submission_assets` 与旧 zip，再重新打包；打包失败就留下「库指向
已删文件」。这条用例把失败注入到写 zip 的那一步。

另有 runtime 的资产回归：`_keep_assets` 用 `INSERT OR REPLACE`，如果修订时
先插新行、后删旧行，会把新行一起删掉，发布后该版本所有平台制品都下载不到。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from store.service import Store
from tests.test_service import SKILL, skill_manifest


def runtime_manifest() -> tuple[dict, dict[str, bytes]]:
    """一份带单个平台资产的 runtime 清单，返回 (清单, 文件)。"""
    payload = b"officecli-bin"
    manifest = skill_manifest(
        id="luxi.officecli",
        version="1.4.0",
        kind="runtime",
        platforms=["darwin-arm64"],
        skill=None,
        invoke={"form": "exec_bin", "bin_name": "officecli"},
    )
    manifest.pop("skill", None)
    manifest["runtime"] = {
        "bin_name": "officecli",
        "assets": [
            {
                "platform": "darwin-arm64",
                "path": "darwin-arm64/officecli",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    return manifest, {"darwin-arm64/officecli": payload}


class ReviseAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")

    def _row(self, submission_id: str):
        return self.store._db.execute(
            "SELECT status, artifact_path FROM submissions WHERE id = ?",
            (submission_id,),
        ).fetchone()

    def test_write_failure_keeps_the_old_artifact_and_status(self) -> None:
        first = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        before = self._row(first.submission_id)
        old_path = self.store.resolve_blob_path(before["artifact_path"])
        assert old_path is not None
        self.assertTrue(old_path.is_file())

        with mock.patch("store.submissions.write_zip", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.revise(
                    self.publisher,
                    first.submission_id,
                    skill_manifest(),
                    {"SKILL.md": SKILL.encode()},
                )

        after = self._row(first.submission_id)
        self.assertEqual(after["artifact_path"], before["artifact_path"])
        self.assertEqual(after["status"], "pending")
        self.assertTrue(old_path.is_file(), "旧制品不能因为修订失败被删掉")
        leftovers = list((self.store.blob_dir / "submissions").glob("*.new.zip"))
        self.assertEqual(leftovers, [])

    def test_successful_revise_replaces_the_artifact(self) -> None:
        first = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        revised = self.store.revise(
            self.publisher,
            first.submission_id,
            skill_manifest(),
            {"SKILL.md": SKILL.encode() + "\n\n## 补充\n".encode()},
        )
        self.assertEqual(revised.status, "pending")
        row = self._row(first.submission_id)
        resolved = self.store.resolve_blob_path(row["artifact_path"])
        assert resolved is not None
        self.assertTrue(resolved.is_file())
        self.assertTrue(row["artifact_path"].endswith(f"{first.submission_id}.zip"))

    def _assets(self, submission_id: str) -> list:
        return self.store._db.execute(
            "SELECT platform, path FROM submission_assets WHERE submission_id = ? ORDER BY platform",
            (submission_id,),
        ).fetchall()

    def test_revise_keeps_runtime_assets(self) -> None:
        manifest, files = runtime_manifest()
        first = self.store.submit(self.publisher, manifest, files)
        self.assertEqual(first.status, "pending", first.rules)
        before = self._assets(first.submission_id)
        self.assertEqual([row["platform"] for row in before], ["darwin-arm64"])

        revised = self.store.revise(self.publisher, first.submission_id, manifest, files)
        self.assertEqual(revised.status, "pending", revised.rules)
        after = self._assets(first.submission_id)
        self.assertEqual([row["platform"] for row in after], ["darwin-arm64"], "修订不能丢掉平台资产")
        asset_file = self.store.resolve_blob_path(after[0]["path"])
        assert asset_file is not None
        self.assertTrue(asset_file.is_file(), "资产文件仍在磁盘上")

    def test_failed_revise_clears_runtime_assets(self) -> None:
        manifest, files = runtime_manifest()
        first = self.store.submit(self.publisher, manifest, files)
        self.assertEqual(len(self._assets(first.submission_id)), 1)
        broken = dict(manifest)
        broken["title"] = ""  # 触发校验失败
        revised = self.store.revise(self.publisher, first.submission_id, broken, files)
        self.assertEqual(revised.status, "rejected")
        self.assertEqual(self._assets(first.submission_id), [], "退回后不应保留旧的平台资产行")


if __name__ == "__main__":
    unittest.main()
