"""登录/注册限流：滑动窗口。

两条硬约束（2026-11-20 修）：
- 成功登录**不清零**计数。旧实现一成功就 reset(ip)，注册一个自有账号后
  交替「登自己 → 猜别人」即可无限爆破；
- 键表打满时**拒绝新键**，不整表清空。清空等于一次性给所有人解封。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.http_api import _RateLimiter, serve
from store.service import Store


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        # 空库不再允许 HTTP 注册（第一个管理员只能由 store init-admin 建），
        # 这里直接建一个管理员，让注册用例走"已有管理员"的正常路径。
        self.store.create_admin_account("root", "Init-Pass-2026", "管理员", "管理员")
        self.server = serve(self.store, login_limit=2, login_window=60, register_limit=1, register_window=60)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_login_attempts_are_limited(self) -> None:
        for _ in range(2):
            status, body = self._post("/v1/login", {"username": "nobody", "password": "WrongPass1"})
            self.assertIn(status, {400, 401})
            self.assertNotEqual(body.get("rule"), "RATE_LIMITED")
        status, body = self._post("/v1/login", {"username": "nobody", "password": "WrongPass1"})
        self.assertEqual(status, 429)
        self.assertEqual(body["rule"], "RATE_LIMITED")

    def test_ip_limit_cannot_be_bypassed_by_changing_username(self) -> None:
        for username in ("nobody-a", "nobody-b"):
            status, _ = self._post("/v1/login", {"username": username, "password": "WrongPass1"})
            self.assertIn(status, {400, 401})
        status, body = self._post("/v1/login", {"username": "nobody-c", "password": "WrongPass1"})
        self.assertEqual(status, 429)
        self.assertEqual(body["rule"], "RATE_LIMITED")

    def test_successful_login_does_not_reset_the_window(self) -> None:
        status, issued = self._post("/v1/account", {
            "username": "rateuser",
            "real_name": "限流用户",
            "nickname": "限流",
            "password": "Passw0rdXyz",
        })
        self.assertEqual(status, 200, issued)
        # login_limit=2：第 1 次失败占 1 格，第 2 次成功也占 1 格；
        # 若成功会清零，第 3 次失败应当仍是 400；不清零则必须 429。
        status, _ = self._post("/v1/login", {"username": "rateuser", "password": "WrongPass1"})
        self.assertIn(status, {400, 401})
        status, body = self._post("/v1/login", {"username": "rateuser", "password": "Passw0rdXyz"})
        self.assertEqual(status, 200, body)
        status, body = self._post("/v1/login", {"username": "rateuser", "password": "WrongPass1"})
        self.assertEqual(status, 429, "成功登录不能把限流窗口清零")
        self.assertEqual(body["rule"], "RATE_LIMITED")

    def test_full_table_denies_new_keys_instead_of_clearing(self) -> None:
        # 直接测限流器：容量打满后不能整表清空（那会给被限流的键解封），
        # 新键按 fail-closed 处理。
        # _RateLimiter 把 max_keys 下限夹在 64，所以用 64 并把键灌满。
        limiter = _RateLimiter(2, 60, max_keys=64)
        for _ in range(2):
            self.assertTrue(limiter.allow("victim"))
        self.assertFalse(limiter.allow("victim"))
        for index in range(200):
            limiter.allow(f"flood-{index}")
        self.assertFalse(limiter.allow("victim"), "灌键不能给被限流的键解封")
        self.assertFalse(limiter.allow("brand-new-key"), "表满时新键应被拒绝，而不是清表")

    def test_registration_is_limited(self) -> None:
        first = self._post("/v1/account", {
            "username": "firstuser", "real_name": "第一位", "nickname": "第一位", "password": "Passw0rdXyz",
        })
        self.assertEqual(first[0], 200, first[1])
        second = self._post("/v1/account", {
            "username": "seconduser", "real_name": "第二位", "nickname": "第二位", "password": "Passw0rdXyz",
        })
        self.assertEqual(second[0], 429)
        self.assertEqual(second[1]["rule"], "RATE_LIMITED")

    def test_health_reports_database_and_blobs(self) -> None:
        with urllib.request.urlopen(self.base + "/api/health") as response:
            body = json.load(response)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["database"], "ok")
        self.assertEqual(body["blobs"], "ok")


if __name__ == "__main__":
    unittest.main()
