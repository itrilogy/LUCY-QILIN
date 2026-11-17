"""登录/注册限流：滑动窗口，成功登录清空计数。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.http_api import serve
from store.service import Store


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
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

    def test_successful_login_resets_the_window(self) -> None:
        status, issued = self._post("/v1/account", {
            "username": "rateuser",
            "real_name": "限流用户",
            "nickname": "限流",
            "password": "Passw0rdXyz",
        })
        self.assertEqual(status, 200, issued)
        status, _ = self._post("/v1/login", {"username": "rateuser", "password": "WrongPass1"})
        self.assertIn(status, {400, 401})
        status, body = self._post("/v1/login", {"username": "rateuser", "password": "Passw0rdXyz"})
        self.assertEqual(status, 200, body)
        # 成功后窗口清空，下一次错误登录不应立刻 429。
        status, _ = self._post("/v1/login", {"username": "rateuser", "password": "WrongPass1"})
        self.assertIn(status, {400, 401})

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
