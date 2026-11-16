"""备份：数据库一致、制品在、清单可读、目标目录不混用。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from store.backup import backup, restore
from store.service import Store
from tests.test_service import SKILL, skill_manifest


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        _, publisher = self.store.create_principal("作者", "publisher")
        _, admin = self.store.create_principal("管理员", "admin")
        result = self.store.submit(publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.store.approve(admin, result.submission_id)

    def test_backup_copies_database_blobs_and_manifest(self) -> None:
        dest = self.root / "backup"
        manifest = backup(self.root / "store.db", self.root / "blobs", dest)
        self.assertTrue((dest / "qilin.db").is_file())
        self.assertTrue((dest / "blobs").is_dir())
        self.assertEqual(manifest["database"], "qilin.db")
        self.assertIn("qilin.db", manifest["checksums"])
        self.assertTrue(any(name.startswith("blobs/") for name in manifest["checksums"]))
        stored = json.loads((dest / "backup.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["database"], "qilin.db")

        copy = sqlite3.connect(f"file:{dest / 'qilin.db'}?mode=ro", uri=True)
        try:
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM principals").fetchone()[0], 2)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM releases").fetchone()[0], 1)
        finally:
            copy.close()
        self.assertTrue(list((dest / "blobs" / "submissions").glob("*.zip")))

    def test_non_empty_destination_is_refused(self) -> None:
        dest = self.root / "backup"
        dest.mkdir()
        (dest / "keep.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            backup(self.root / "store.db", self.root / "blobs", dest)

    def test_missing_database_is_refused(self) -> None:
        with self.assertRaises(FileNotFoundError):
            backup(self.root / "nope.db", self.root / "blobs", self.root / "backup")


class RestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        _, publisher = self.store.create_principal("作者", "publisher")
        _, admin = self.store.create_principal("管理员", "admin")
        result = self.store.submit(publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.store.approve(admin, result.submission_id)
        self.dest = self.root / "backup"
        self.manifest = backup(self.root / "store.db", self.root / "blobs", self.dest)

    def test_restore_recreates_a_working_store(self) -> None:
        target_db = self.root / "restored" / "qilin.db"
        target_blobs = self.root / "restored" / "blobs"
        restore(self.dest, target_db, target_blobs)
        restored = Store(target_db, target_blobs)
        self.addCleanup(restored.close)
        self.assertEqual(restored.search_page("")["total"], 1)
        self.assertTrue(list((target_blobs / "submissions").glob("*.zip")))

    def test_restore_refuses_existing_target_without_force(self) -> None:
        with self.assertRaises(FileExistsError):
            restore(self.dest, self.root / "store.db", self.root / "blobs")
        restore(self.dest, self.root / "store.db", self.root / "blobs", force=True)

    def test_tampered_backup_is_refused(self) -> None:
        (self.dest / "qilin.db").write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            restore(self.dest, self.root / "other.db", self.root / "other-blobs")

    def test_missing_checksums_are_refused(self) -> None:
        payload = json.loads((self.dest / "backup.json").read_text(encoding="utf-8"))
        payload.pop("checksums")
        (self.dest / "backup.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            restore(self.dest, self.root / "other.db", self.root / "other-blobs")


if __name__ == "__main__":
    unittest.main()
