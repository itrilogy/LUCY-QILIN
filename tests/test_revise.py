"""修订失败时不能把旧制品弄丢。

旧实现先删 `submission_assets` 与旧 zip，再重新打包；打包失败就留下「库指向
已删文件」。这条用例把失败注入到写 zip 的那一步。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from store.service import Store
from tests.test_service import SKILL, skill_manifest


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
        old_path = Path(before["artifact_path"])
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
        self.assertTrue(Path(row["artifact_path"]).is_file())
        self.assertTrue(row["artifact_path"].endswith(f"{first.submission_id}.zip"))


if __name__ == "__main__":
    unittest.main()
