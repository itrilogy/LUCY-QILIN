"""W0 止血项的回归用例。

覆盖四件事：
1. 枢纽令牌验签不再占用商店全局锁；
2. 取公钥失败后进入冷却，不重复联网；
3. 未捕获异常不回显异常原文，只给可检索的 trace_id；
4. 压缩包冲突与超大包在服务端与安装器侧都按失败处理，而不是 500 或 OOM。
"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from store import diagnostics
from store.http_api import serve
from store.identity import HubIdentity, IdentityError
from store.installer import run as run_install
from store.service import Store
from tests.test_service import SKILL, SUMMARY, DESC, skill_manifest


def _conflict_zip() -> bytes:
    """先写文件 x，再写 x/y：解压时建父目录会撞上已存在的文件。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("x", b"1")
        archive.writestr("x/y", b"2")
    return buffer.getvalue()


class HubLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)

    def test_prepare_token_verifies_outside_the_store_lock(self) -> None:
        seen: dict[str, bool] = {}

        class FakeIdentity:
            def principal(self, token: str):
                # 验签期间全局锁必须可用。拿得到就说明 prepare_token 在锁外。
                acquired = self_store._lock.acquire(timeout=0.05)
                seen["lock_free"] = acquired
                if acquired:
                    self_store._lock.release()
                raise IdentityError("IDENTITY_INVALID", "测试令牌无效")

        self_store = self.store
        self.store._identity = FakeIdentity()
        with self.assertRaises(Exception):
            self.store.prepare_token("a.b." + "x" * 300)
        self.assertTrue(seen.get("lock_free"), "验签不应占用全局锁")

    def test_prepare_token_ignores_local_keys(self) -> None:
        # 本地 KEY 不是 JWT 形状，prepare_token 不该做任何事，也不该报错。
        self.store.prepare_token("x" * 43)
        self.store.prepare_token("")
        self.assertIsNone(self.store._identity)


class IdentityFetchTests(unittest.TestCase):
    def test_failed_fetch_is_negatively_cached(self) -> None:
        calls = {"count": 0}

        def opener(url: str) -> bytes:
            calls["count"] += 1
            raise urllib.error.URLError("hub down")

        identity = HubIdentity("http://hub.invalid", opener=opener, now=lambda: 1000)
        with self.assertRaises(IdentityError):
            identity._keys(force=False)
        with self.assertRaises(IdentityError):
            identity._keys(force=False)
        self.assertEqual(calls["count"], 1, "冷却期内不应重复联网")

        # 冷却结束后允许再试一次。
        identity._now = lambda: 1000 + 31
        with self.assertRaises(IdentityError):
            identity._keys(force=False)
        self.assertEqual(calls["count"], 2)


class InternalErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        self.server = serve(self.store)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_500_hides_exception_text_and_returns_trace_id(self) -> None:
        def boom(package_id: str, version: str) -> dict:
            raise RuntimeError("内部路径 /srv/secret/qilin.db")

        with mock.patch.object(self.store, "describe", side_effect=boom):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + "/v1/packages/a.b/1.0.0")
        body = caught.exception.read().decode()
        caught.exception.close()
        self.assertEqual(caught.exception.code, 500)
        self.assertNotIn("RuntimeError", body)
        self.assertNotIn("/srv/secret", body)
        payload = json.loads(body)
        self.assertEqual(payload["rule"], "INTERNAL")
        self.assertEqual(len(payload["trace_id"]), 16)

    def test_auth_log_omits_query_string(self) -> None:
        class Handler:
            path = "/auth/callback?code=one-time-code&state=abc"
            headers: dict[str, str] = {}
            client_address = ("127.0.0.1", 1234)

        with self.assertLogs("qilin", level="WARNING") as captured:
            diagnostics.auth_failure(Handler(), "IDENTITY_INVALID", "不通过")
        text = "\n".join(captured.output)
        self.assertNotIn("one-time-code", text)
        self.assertIn("/auth/callback", text)


class ArchiveFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")

    def test_conflicting_archive_is_rejected_not_500(self) -> None:
        manifest = skill_manifest()
        started = self.store.begin_submit(
            self.publisher, manifest, byte_size=len(_conflict_zip()),
        )
        self.assertEqual(started["status"], "uploading")
        self.store.save_upload(self.publisher, started["submission_id"], _conflict_zip())
        result = self.store.finish_submit(self.publisher, started["submission_id"])
        self.assertEqual(result.status, "rejected")
        self.assertEqual(result.rules, ["CHECKSUM"])

    def test_safe_extract_reports_conflict_as_value_error(self) -> None:
        from store.blobs import safe_extract

        archive = self.root / "conflict.zip"
        archive.write_bytes(_conflict_zip())
        with self.assertRaises(ValueError):
            safe_extract(archive, self.root / "out")

    def test_installer_unpack_enforces_unpacked_limit(self) -> None:
        archive = self.root / "big.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("payload.bin", b"0" * 4096)
        with mock.patch("store.blobs.MAX_UNPACKED", 1024):
            code = run_install({"steps": [{"op": "unpack", "src": str(archive), "dest": str(self.root / "out")}]})
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "out" / "payload.bin").exists())

    def test_installer_download_stops_over_limit(self) -> None:
        class Response:
            def __init__(self) -> None:
                self.sent = False

            def read(self, size: int = -1) -> bytes:
                if self.sent:
                    return b""
                self.sent = True
                return b"x" * 100

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        dest = self.root / "asset.bin"
        with mock.patch("store.installer._MAX_DOWNLOAD", 10):
            code = run_install(
                {"steps": [{"op": "download", "url": "http://example/asset", "sha256": "0" * 64, "dest": str(dest)}]},
                opener=lambda url, timeout=30: Response(),
            )
        self.assertEqual(code, 1)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
