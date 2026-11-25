"""外部输入收口：分页参数夹取、文件树冲突按 400 退回、错误响应不回显内部原文。

对应评审 P1-7。三条都是"外部输入把 500 打出来"的同一类问题：
- 超大 `offset` 绑不进 SQLite（OverflowError），旧实现落到 500；
- 同一份 files 里 `a` 与 `a/b` 并存，建目录/写文件抛 FileExistsError，旧实现 500；
- 400 的 message 里回显 `ValueError: invalid literal ...` 这类异常原文。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.errors import StoreError
from store.http_api import _MAX_OFFSET, serve
from store.service import Store
from tests.test_service import SKILL, skill_manifest


class QueryLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        self.server = serve(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _catalog(self, query: str) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(f"{self.base}/v1/catalog?{query}", timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_huge_offset_is_clamped_not_500(self) -> None:
        status, body = self._catalog("offset=99999999999999999999")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["offset"], _MAX_OFFSET)

    def test_limit_is_clamped_and_echoed_back(self) -> None:
        status, body = self._catalog("limit=100000")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["limit"], 500, "响应要回实际生效的上限")

    def test_non_integer_limit_is_400_without_internals(self) -> None:
        status, body = self._catalog("limit=abc")
        self.assertEqual(status, 400, body)
        self.assertEqual(body["rule"], "SPEC_VERSION")
        self.assertIn("limit", body["message"])
        self.assertNotIn("ValueError", body["message"], "不能把异常类名回给客户端")
        self.assertNotIn("invalid literal", body["message"])

    def test_non_integer_offset_is_400(self) -> None:
        status, body = self._catalog("offset=1.5")
        self.assertEqual(status, 400, body)
        self.assertEqual(body["rule"], "SPEC_VERSION")


class TreeConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")

    def _submit(self, files: dict[str, bytes]):
        return self.store.submit(self.publisher, skill_manifest(), files)

    def test_file_then_directory_is_a_400_not_500(self) -> None:
        with self.assertRaises(StoreError) as caught:
            self._submit({"a": b"x", "a/b": b"y"})
        self.assertEqual(caught.exception.rule, "CHECKSUM")
        self.assertIn("路径冲突", caught.exception.message)

    def test_directory_then_file_is_a_400_not_500(self) -> None:
        with self.assertRaises(StoreError) as caught:
            self._submit({"a/b": b"y", "a": b"x"})
        self.assertEqual(caught.exception.rule, "CHECKSUM")
        self.assertIn("路径冲突", caught.exception.message)

    def test_manifest_json_cannot_be_turned_into_a_directory(self) -> None:
        with self.assertRaises(StoreError) as caught:
            self._submit({"SKILL.md": SKILL.encode(), "manifest.json/x": b"y"})
        self.assertEqual(caught.exception.rule, "CHECKSUM")

    def test_normal_tree_still_submits(self) -> None:
        result = self._submit({"SKILL.md": SKILL.encode()})
        self.assertEqual(result.status, "pending", result.rules)


if __name__ == "__main__":
    unittest.main()
