"""本机目录交给商店。钥匙不出现在输出里。"""

from __future__ import annotations

import io
import json
import os
import threading
import unittest
from contextlib import redirect_stdout

from pathlib import Path

from store.cli import main
from store.http_api import serve
from store.service import Store
from tests.test_knowledge import _note, _pack
from tests.test_service import SKILL, skill_manifest


class SubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = __import__("pathlib").Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _principal, self.key = self.store.create_principal("作者", "publisher")
        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self._old = os.environ.get("QILIN_STORE_KEY")

    def tearDown(self) -> None:
        if self._old is None:
            os.environ.pop("QILIN_STORE_KEY", None)
        else:
            os.environ["QILIN_STORE_KEY"] = self._old

    def test_broken_notes_stop_before_the_store(self) -> None:
        root = _pack({"00-导航概览.md": _note("导航", ["没有这一篇"], type="map_of_content")})
        os.environ["QILIN_STORE_KEY"] = self.key
        code, text = _run(root, "http://127.0.0.1:9")
        self.assertEqual(code, 1)
        body = json.loads(text)
        self.assertEqual(body["status"], "invalid")
        self.assertIn("NOTE_LINK", body["rules"])
        self.assertNotIn(self.key, text)
        self.assertEqual(self.store.list_my_submissions(self.key), [])

    def test_a_closed_pack_waits_for_review(self) -> None:
        root = _pack({
            "00-导航概览.md": _note("导航", ["01-概念"], type="map_of_content"),
            "01-概念.md": _note("概念", ["00-导航概览"]),
        })
        os.environ["QILIN_STORE_KEY"] = self.key
        code, text = _run(root, self.base)
        self.assertEqual(code, 0, text)
        body = json.loads(text)
        self.assertEqual(body["status"], "pending")
        self.assertTrue(body["submission_id"])
        self.assertNotIn(self.key, text)
        mine = self.store.list_my_submissions(self.key)
        self.assertEqual(mine[0]["status"], "pending")

    def test_a_skill_directory_waits_for_review(self) -> None:
        root = Path(self.tmp.name) / "skill"
        root.mkdir()
        (root / "manifest.json").write_text(json.dumps(skill_manifest()), encoding="utf-8")
        (root / "SKILL.md").write_text(SKILL, encoding="utf-8")
        os.environ["QILIN_STORE_KEY"] = self.key
        code, text = _run(root, self.base)
        self.assertEqual(code, 0, text)
        body = json.loads(text)
        self.assertEqual(body["status"], "pending")
        self.assertTrue(body["submission_id"])
        self.assertNotIn(self.key, text)
        mine = self.store.list_my_submissions(self.key)
        self.assertEqual(mine[0]["package_id"], "luxi.gov-writer")
        self.assertEqual(mine[0]["status"], "pending")

    def test_missing_key_does_not_invent_one(self) -> None:
        root = _pack({
            "00-导航概览.md": _note("导航", [], type="map_of_content"),
        })
        os.environ.pop("QILIN_STORE_KEY", None)
        code, text = _run(root, self.base)
        self.assertEqual(code, 2)
        self.assertIn("QILIN_STORE_KEY", text)
        self.assertNotIn("Bearer", text)
        self.assertEqual(self.store.list_my_submissions(self.key), [])


def _run(root, base: str) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["submit", str(root), "--base", base])
    return code, buf.getvalue()


if __name__ == "__main__":
    unittest.main()
