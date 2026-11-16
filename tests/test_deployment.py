"""部署与性能相关的回归用例：KEY 指纹、过期上传、对外地址、请求体上限。

这一组用例的共同点是「不改变正常路径的行为」，只验证边界条件。
每个用例都对应 `docs/代码审计与演进报告` 里的一条发现，编号写在注释里。
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.accounts import key_prefix
from store.http_api import _MAX_BODY, _session_cookie, serve
from store.service import Store, StoreError, _UPLOAD_TTL_HOURS
from tests.test_service import skill_manifest

PASSWORD = "LinShen-2026"


class KeyPrefixTests(unittest.TestCase):
    """C4：KEY 鉴权从 O(账号数) 降到 O(1)，且旧库仍可登录。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)

    def test_prefix_column_is_filled_on_register(self) -> None:
        _profile, key = self._register("author.one")
        row = self.store._db.execute("SELECT key_prefix FROM accounts").fetchone()
        self.assertEqual(row["key_prefix"], key_prefix(key))

    def test_legacy_row_without_prefix_still_authenticates(self) -> None:
        _profile, key = self._register("author.one")
        # 模拟旧库：把指纹清空，走兜底分支。
        self.store._db.execute("UPDATE accounts SET key_prefix = NULL")
        self.store._db.commit()
        principal = self.store.principal_from_token(key)
        self.assertIsNotNone(principal)
        self.assertEqual(principal.name, "木青")
        # 兜底分支会顺手补回指纹，第二次就走快路径。
        filled = self.store._db.execute("SELECT key_prefix FROM accounts").fetchone()
        self.assertEqual(filled["key_prefix"], key_prefix(key))

    def test_wrong_key_does_not_match_any_row(self) -> None:
        self._register("author.one")
        self.assertIsNone(self.store.principal_from_token("x" * 43))

    def test_reissued_key_replaces_prefix(self) -> None:
        profile, old_key = self._register("author.one")
        new_profile, _session, new_key = self.store.issue_account_key(old_key)
        self.assertEqual(new_profile["id"], profile["id"])
        self.assertIsNone(self.store.principal_from_token(old_key))
        self.assertIsNotNone(self.store.principal_from_token(new_key))
        row = self.store._db.execute("SELECT key_prefix FROM accounts").fetchone()
        self.assertEqual(row["key_prefix"], key_prefix(new_key))

    def _register(self, username: str) -> tuple[dict, str]:
        profile, _session, key = self.store.register_account(
            username, "顾木", "木青", "产品", "作者", PASSWORD,
        )
        return profile, key


class StaleUploadTests(unittest.TestCase):
    """D3：停在 uploading 的提交与 blob 需要过期清理。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        self.body = skill_manifest()

    def test_fresh_upload_is_kept(self) -> None:
        started = self.store.begin_submit(self.publisher, self.body, byte_size=4)
        removed = self.store.prune_stale_uploads()
        self.assertEqual(removed, 0)
        row = self.store._db.execute(
            "SELECT status FROM submissions WHERE id = ?", (started["submission_id"],),
        ).fetchone()
        self.assertEqual(row["status"], "uploading")

    def test_expired_upload_is_dropped_with_its_blob(self) -> None:
        started = self.store.begin_submit(self.publisher, self.body, byte_size=4)
        self.store.save_upload(self.publisher, started["submission_id"], b"data")
        blob = self.store.blob_dir / "uploads" / f"{started['submission_id']}.zip"
        self.assertTrue(blob.is_file())
        # 把创建时间推到 TTL 之外，再清一次。
        later = int(time.time()) + (_UPLOAD_TTL_HOURS + 1) * 3600
        removed = self.store.prune_stale_uploads(now=later)
        self.assertEqual(removed, 1)
        self.assertFalse(blob.is_file())
        row = self.store._db.execute(
            "SELECT status, artifact_path FROM submissions WHERE id = ?",
            (started["submission_id"],),
        ).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertIsNone(row["artifact_path"])

    def test_finished_submission_is_not_touched(self) -> None:
        result = self.store.submit(
            self.publisher, self.body, {"SKILL.md": b"---\nname: gov\n---\n\n# \xe5\x85\xac\xe6\x96\x87\n"},
        )
        self.assertEqual(result.status, "pending", result.rules)
        later = int(time.time()) + (_UPLOAD_TTL_HOURS + 1) * 3600
        self.assertEqual(self.store.prune_stale_uploads(now=later), 0)


class PublicBaseTests(unittest.TestCase):
    """C5：对外地址不再恒为 http，且 Host 头形状受限。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.port = self.server.server_port

    def test_ipv4_host_is_accepted(self) -> None:
        url = f"http://127.0.0.1:{self.port}/v1/identity"
        with urllib.request.urlopen(url) as response:
            self.assertEqual(response.status, 200)

    def test_forwarded_proto_switches_to_https(self) -> None:
        # `/v1/me` 只在已登录时填 mcp_url，这里用账号注册后取。
        _profile, cookie, _key = self._register()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/me")
        request.add_header("Cookie", f"qilin_session={cookie}")
        request.add_header("X-Forwarded-Proto", "https")
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        self.assertTrue(payload["mcp_url"].startswith("https://"), payload["mcp_url"])

    def test_configured_public_base_wins(self) -> None:
        self.server.public_base = "https://store.example"
        _profile, cookie, _key = self._register()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/me")
        request.add_header("Cookie", f"qilin_session={cookie}")
        request.add_header("X-Forwarded-Proto", "http")
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        self.assertEqual(payload["mcp_url"], "https://store.example/mcp")

    def test_host_with_slash_is_not_used(self) -> None:
        _profile, cookie, _key = self._register()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/me")
        request.add_header("Cookie", f"qilin_session={cookie}")
        request.add_header("Host", "evil.example/../../x")
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        # 形状不合法时退回监听地址，而不是把 Host 拼进 URL。
        self.assertNotIn("evil.example", payload["mcp_url"])
        self.assertIn(f"127.0.0.1:{self.port}", payload["mcp_url"])

    def test_session_cookie_carries_secure_when_https(self) -> None:
        self.assertIn("Secure", _header(_session_cookie("tok", secure=True), "Set-Cookie"))
        self.assertNotIn("Secure", _header(_session_cookie("tok", secure=False), "Set-Cookie"))
        cleared = _header(_session_cookie("", clear=True, secure=True), "Set-Cookie")
        self.assertIn("Max-Age=0", cleared)
        self.assertIn("Secure", cleared)

    def _register(self) -> tuple[dict, str, str]:
        body = json.dumps({
            "username": "author.one",
            "real_name": "顾木",
            "nickname": "木青",
            "password": PASSWORD,
        }).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/account",
            data=body, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
            cookie = _cookie(response.headers.get("Set-Cookie", ""))
        return payload, cookie, payload.get("key", "")


class BodyLimitTests(unittest.TestCase):
    """D2：base64 封装后正文上限与制品上限不再互相卡住。"""

    def test_body_limit_covers_base64_inflation(self) -> None:
        from store.service import _MAX_BYTES

        # 32MiB 制品编码后约 42.7MiB，正文上限必须容得下。
        inflated = (_MAX_BYTES // 3 + 1) * 4
        self.assertGreater(_MAX_BODY, inflated)

    def test_invalid_base64_is_rejected(self) -> None:
        from store.http_api import _files

        with self.assertRaises(StoreError) as caught:
            _files({"files": {"a.txt": "not*base64"}})
        self.assertEqual(caught.exception.rule, "CHECKSUM")

    def test_files_must_be_object_of_strings(self) -> None:
        from store.http_api import _files

        for broken in ({"files": []}, {"files": {"a.txt": 1}}, {"files": {1: "YQ=="}}):
            with self.subTest(broken=broken):
                with self.assertRaises(StoreError) as caught:
                    _files(broken)
                self.assertEqual(caught.exception.rule, "CHECKSUM")

    def test_valid_base64_round_trips(self) -> None:
        from store.http_api import _files

        encoded = base64.b64encode(b"\xe5\x85\xac\xe6\x96\x87").decode()
        self.assertEqual(_files({"files": {"a.txt": encoded}}), {"a.txt": "公文".encode()})


class ArtifactMissingTests(unittest.TestCase):
    """D3 的后半段：制品文件不在时给规则码，而不是 500。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")

    def test_deleted_artifact_raises_version_unavailable(self) -> None:
        from tests.test_service import SKILL

        result = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.store.approve(self.admin, result.submission_id)
        row = self.store._db.execute(
            "SELECT artifact_path FROM releases WHERE package_id = ?", ("luxi.gov-writer",),
        ).fetchone()
        Path(row["artifact_path"]).unlink()
        with self.assertRaises(StoreError) as caught:
            self.store.artifact("luxi.gov-writer", "3.2.0")
        self.assertEqual(caught.exception.rule, "VERSION_UNAVAILABLE")


def _cookie(header: str) -> str:
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == "qilin_session":
            return value
    return ""


def _header(pairs: list[tuple[str, str]], name: str) -> str:
    """从附加头列表里取第一段同名头的值。`Set-Cookie` 可能不止一条。"""
    for key, value in pairs:
        if key.lower() == name.lower():
            return value
    return ""


if __name__ == "__main__":
    unittest.main()
