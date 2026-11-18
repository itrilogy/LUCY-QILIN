"""修订上传：begin → PUT → finish，保持同一个 submission_id。"""

from __future__ import annotations

import io
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from store.errors import StoreError
from store.service import Store
from tests.test_service import SKILL, skill_manifest


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class ReviseUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.other = self.store.create_principal("另一位作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")

    def _submit(self):
        result = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.assertEqual(result.status, "pending")
        return result

    def _revised_manifest(self):
        return skill_manifest(changes="修订后的说明，这一版只改说明。")

    def test_three_stage_revision_keeps_the_submission_id(self) -> None:
        original = self._submit()
        payload = _zip({"SKILL.md": SKILL.encode()})
        started = self.store.begin_revise(
            self.publisher, original.submission_id, self._revised_manifest(), byte_size=len(payload)
        )
        self.assertEqual(started["status"], "uploading")
        prefix = f"/v1/uploads/revise/{original.submission_id}?ticket="
        self.assertTrue(started["put_path"].startswith(prefix))
        ticket = started["put_path"][len(prefix):]
        # 只凭 ticket 上传，模拟独立终端没有 Authorization。
        self.store.save_revise_upload("", original.submission_id, payload, ticket=ticket)
        result = self.store.finish_revise(self.publisher, original.submission_id)
        self.assertEqual(result.submission_id, original.submission_id)
        self.assertEqual(result.status, "pending")
        seen = self.store.get_submission(self.publisher, original.submission_id)
        self.assertEqual(seen["manifest"]["changes"], "修订后的说明，这一版只改说明。")
        leftover = self.store._db.execute(
            "SELECT 1 FROM revisions WHERE submission_id = ?", (original.submission_id,)
        ).fetchone()
        self.assertIsNone(leftover)

    def test_invalid_package_rejects_the_submission(self) -> None:
        original = self._submit()
        payload = _zip({"NOTES.md": b"no skill entry"})
        self.store.begin_revise(
            self.publisher, original.submission_id, self._revised_manifest(), byte_size=len(payload)
        )
        self.store.save_revise_upload(self.publisher, original.submission_id, payload)
        result = self.store.finish_revise(self.publisher, original.submission_id)
        self.assertEqual(result.status, "rejected")
        self.assertIn("SKILL_ENTRY_MISSING", result.rules)
        row = self.store._db.execute(
            "SELECT status, artifact_path FROM submissions WHERE id = ?", (original.submission_id,)
        ).fetchone()
        self.assertEqual(row["status"], "rejected")
        self.assertIsNone(row["artifact_path"])

    def test_only_owner_can_revise(self) -> None:
        original = self._submit()
        with self.assertRaises(StoreError) as caught:
            self.store.begin_revise(
                self.other, original.submission_id, self._revised_manifest(), byte_size=1
            )
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_published_submission_cannot_be_revised(self) -> None:
        original = self._submit()
        self.store.approve(self.admin, original.submission_id)
        with self.assertRaises(StoreError) as caught:
            self.store.begin_revise(
                self.publisher, original.submission_id, self._revised_manifest(), byte_size=1
            )
        self.assertEqual(caught.exception.rule, "VERSION_UNAVAILABLE")

    def test_finish_without_upload_is_refused(self) -> None:
        original = self._submit()
        self.store.begin_revise(
            self.publisher, original.submission_id, self._revised_manifest(), byte_size=1
        )
        with self.assertRaises(StoreError) as caught:
            self.store.finish_revise(self.publisher, original.submission_id)
        self.assertEqual(caught.exception.rule, "CHECKSUM")


    def test_stale_revision_is_pruned_with_its_archive(self) -> None:
        original = self._submit()
        payload = _zip({"SKILL.md": SKILL.encode()})
        self.store.begin_revise(
            self.publisher, original.submission_id, self._revised_manifest(), byte_size=len(payload)
        )
        self.store.save_revise_upload(self.publisher, original.submission_id, payload)
        revision = self.store._db.execute(
            "SELECT artifact_path FROM revisions WHERE submission_id = ?", (original.submission_id,)
        ).fetchone()
        archive = Path(revision["artifact_path"])
        self.assertTrue(archive.is_file())
        self.store._db.execute(
            "UPDATE revisions SET created_at = datetime('now', '-3 days') WHERE submission_id = ?",
            (original.submission_id,),
        )
        self.store._db.commit()
        removed = self.store.prune_stale_uploads(now=int(time.time()), max_age_hours=1)
        self.assertGreaterEqual(removed, 1)
        self.assertIsNone(self.store._db.execute(
            "SELECT 1 FROM revisions WHERE submission_id = ?", (original.submission_id,)
        ).fetchone())
        self.assertFalse(archive.exists())

    def test_schema_user_version_is_recorded(self) -> None:
        version = int(self.store._db.execute("PRAGMA user_version").fetchone()[0])
        self.assertGreaterEqual(version, 1)


if __name__ == "__main__":
    unittest.main()
