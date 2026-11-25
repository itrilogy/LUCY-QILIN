"""引导与管理：谁可以成为管理员、注册开到哪里、只能跑一个进程。

背景（评审 P1-2/P1-9）：
- 旧行为是"空库第一个 HTTP 注册者自动成为管理员"。空库一旦暴露到公网，
  第一个请求就能拿下整站。现在管理员只能由 `store init-admin` 建，
  HTTP 注册只产生作者；`QILIN_REGISTRATION=closed` 可整体关闭注册。
- 单进程、单副本一直是文档里的约束，但代码没有断言。现在 serve 会在
  数据库旁拿一把 flock，第二个实例直接拒绝启动。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from store.accounts import password_problems
from store.http_api import serve
from store.service import Store

REPO = Path(__file__).resolve().parent.parent
PASSWORD = "Qilin-Admin-2026"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


class HttpCase(unittest.TestCase):
    """起一个真服务，所有断言走 HTTP。"""

    admin = False

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        if self.admin:
            self.store.create_admin_account("boss", PASSWORD, "管理员", "老板")
        self.server = serve(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _post(self, path: str, body: dict, cookie: str = "") -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"qilin_session={cookie}"
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _get(self, path: str, cookie: str = "") -> dict:
        request = urllib.request.Request(self.base + path)
        if cookie:
            request.add_header("Cookie", f"qilin_session={cookie}")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode())

    def _login(self, username: str, password: str = PASSWORD) -> str:
        request = urllib.request.Request(
            self.base + "/v1/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.headers.get("Set-Cookie", "")
        for part in raw.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == "qilin_session":
                return value
        self.fail("登录没有下发会话 Cookie")


class EmptyStoreTests(HttpCase):
    """空库：HTTP 注册一律拒绝，先 init-admin。"""

    def test_registration_is_refused_until_an_admin_exists(self) -> None:
        identity = self._get("/v1/identity")
        self.assertFalse(identity["has_admin"])
        status, body = self._post("/v1/account", {
            "username": "sneaky.one", "real_name": "抢先", "nickname": "抢先",
            "password": "Sneaky-One-2026", "password_again": "Sneaky-One-2026",
        })
        self.assertEqual(status, 403)
        self.assertEqual(body["rule"], "REGISTRATION_CLOSED")
        self.assertIsNone(
            self.store._db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone(),
            "被拒绝的注册不能留下账号",
        )


class AdminStoreTests(HttpCase):
    admin = True

    def test_identity_reports_open_registration_and_admin(self) -> None:
        identity = self._get("/v1/identity")
        self.assertEqual(identity["registration"], "open")
        self.assertTrue(identity["has_admin"])

    def test_closed_registration_is_refused(self) -> None:
        with mock.patch.dict(os.environ, {"QILIN_REGISTRATION": "closed"}):
            status, body = self._post("/v1/account", {
                "username": "author.one", "real_name": "顾木", "nickname": "木青",
                "password": "Author-One-2026", "password_again": "Author-One-2026",
            })
        self.assertEqual(status, 403)
        self.assertEqual(body["rule"], "REGISTRATION_CLOSED")

    def test_admin_creates_account_with_a_one_time_password(self) -> None:
        cookie = self._login("boss")
        status, issued = self._post("/v1/accounts", {
            "username": "review.one", "real_name": "沈林", "nickname": "林深", "role": "reviewer",
        }, cookie=cookie)
        self.assertEqual(status, 200, issued)
        self.assertEqual(issued["role"], "reviewer")
        self.assertEqual(password_problems(issued["password"], "review.one"), [])
        # 生成的口令能登录；名单接口不会回显它。
        self.assertTrue(self._login("review.one", issued["password"]))
        managed = self._get("/v1/accounts", cookie=cookie)
        self.assertNotIn(issued["password"], json.dumps(managed))

    def test_only_admin_can_create_accounts(self) -> None:
        status, body = self._post("/v1/account", {
            "username": "author.one", "real_name": "顾木", "nickname": "木青",
            "password": "Author-One-2026", "password_again": "Author-One-2026",
        })
        self.assertEqual(status, 200, body)
        cookie = self._login("author.one", "Author-One-2026")
        status, denied = self._post("/v1/accounts", {
            "username": "other.one", "real_name": "别人", "nickname": "别人",
        }, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(denied["rule"], "ROLE_FORBIDDEN")

    def test_role_must_be_known(self) -> None:
        cookie = self._login("boss")
        status, body = self._post("/v1/accounts", {
            "username": "other.one", "real_name": "别人", "nickname": "别人", "role": "root",
        }, cookie=cookie)
        self.assertEqual(status, 403)
        self.assertEqual(body["rule"], "ROLE_FORBIDDEN")


class InitAdminCliTests(unittest.TestCase):
    """`store init-admin`：空库引导 + 拒绝重复。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(REPO)
        return subprocess.run(
            [sys.executable, "-m", "store.cli", "init-admin",
             "--db", str(self.root / "qilin.db"), "--blobs", str(self.root / "blobs"), *args],
            cwd=str(REPO), env=environment, capture_output=True, text=True, timeout=60,
        )

    def test_first_admin_is_created_then_refused(self) -> None:
        first = self._run("--username", "boss", "--password", PASSWORD, "--nickname", "老板")
        self.assertEqual(first.returncode, 0, first.stderr)
        issued = json.loads(first.stdout)
        self.assertEqual(issued["role"], "admin")
        self.assertEqual(issued["password"], PASSWORD)

        again = self._run("--username", "boss2", "--password", "Qilin-Second-2026")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("已经有管理员", again.stderr)

    def test_weak_password_is_refused_without_creating_anything(self) -> None:
        weak = self._run("--username", "boss", "--password", "short")
        self.assertNotEqual(weak.returncode, 0)
        self.assertIn("口令不符合要求", weak.stderr)
        self.assertFalse((self.root / "qilin.db").exists() and
                         self.store_has_accounts())

    def store_has_accounts(self) -> bool:
        store = Store(self.root / "qilin.db", self.root / "blobs")
        try:
            return store._db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None
        finally:
            store.close()


@unittest.skipIf(os.name == "nt", "Windows 没有 flock")
class SingleInstanceTests(unittest.TestCase):
    """第二个 serve 指向同一个库时必须拒绝启动。"""

    def test_second_serve_and_restore_are_refused_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "qilin.db"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(REPO)
            first_port = _free_port()
            first = subprocess.Popen(
                [sys.executable, "-m", "store.cli", "serve", "--host", "127.0.0.1",
                 "--port", str(first_port), "--db", str(db), "--blobs", str(root / "blobs")],
                cwd=str(REPO), env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            try:
                self.assertTrue(_wait_for_port(first_port, timeout=20), "第一个实例没起来")
                second = subprocess.run(
                    [sys.executable, "-m", "store.cli", "serve", "--host", "127.0.0.1",
                     "--port", str(_free_port()), "--db", str(db), "--blobs", str(root / "blobs")],
                    cwd=str(REPO), env=environment, capture_output=True, text=True, timeout=60,
                )
                self.assertNotEqual(second.returncode, 0)
                self.assertIn("另一个 store serve", second.stderr)
                # 服务在跑时不允许 restore：恢复整库替换，最容易丢数据。
                restore = subprocess.run(
                    [sys.executable, "-m", "store.cli", "restore", str(root / "no-such-backup"),
                     "--db", str(db), "--blobs", str(root / "blobs"), "--force"],
                    cwd=str(REPO), env=environment, capture_output=True, text=True, timeout=60,
                )
                self.assertNotEqual(restore.returncode, 0)
                self.assertIn("恢复前请先停掉", restore.stderr)
            finally:
                first.terminate()
                try:
                    first.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    first.kill()
                    first.wait()


if __name__ == "__main__":
    unittest.main()
