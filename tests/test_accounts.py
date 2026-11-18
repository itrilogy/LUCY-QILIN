"""每人一把商店生成的 KEY。内网策略：明文持久化，登录后可见。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.http_api import serve
from store.service import Store, StoreError

PASSWORD = "LinShen-2026"
NEXT_PASSWORD = "ShenLin-2026"


class AccountTests(unittest.TestCase):
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
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_first_person_gets_one_generated_key(self) -> None:
        admin, admin_cookie, admin_key = self._create("admin.one", "沈林", "林深", "数字化", "审核")
        self.assertEqual(admin["role"], "admin")
        self.assertEqual(admin["real_name"], "沈林")
        self.assertEqual(admin["nickname"], "林深")
        self.assertEqual(admin["department"], "数字化")
        self.assertIn(self.base + "/mcp", json.dumps(admin["mcp"]))
        self.assertIn("Bearer " + admin_key, json.dumps(admin["mcp"]))
        author, _cookie, author_key = self._create("author.one", "顾木", "木青", "产品")
        self.assertEqual(author["role"], "publisher")
        self.assertNotEqual(admin_key, author_key)
        me = self._get("/v1/me", admin_cookie)
        self.assertEqual(me["username"], "admin.one")
        self.assertEqual(me["name"], "林深")
        self.assertEqual(me["source"], "account")
        self.assertTrue(me["key_ready"])
        self.assertEqual(me["store_key"], admin_key)
        hidden = self._open("/v1/accounts", expect=403)
        self.assertEqual(hidden["rule"], "ROLE_FORBIDDEN")
        names = self._get("/v1/accounts", admin_cookie)["items"]
        self.assertEqual([item["display_name"] for item in names], ["林深", "木青"])
        denied = self._open("/v1/accounts/manage", cookie=self._login("author.one"), expect=403)
        self.assertEqual(denied["rule"], "ROLE_FORBIDDEN")
        managed = self._get("/v1/accounts/manage", admin_cookie)["items"]
        author_row = next(item for item in managed if item["username"] == "author.one")
        self.assertEqual(author_row["department"], "产品")
        self.assertNotIn("key_hash", author_row)
        self._post(f"/v1/accounts/{author_row['id']}/role", {"role": "admin"}, cookie=admin_cookie)
        self.assertEqual(self._get("/v1/me", self._login("author.one"))["role"], "admin")

    def test_key_is_the_bearer_and_reissue_replaces_the_only_one(self) -> None:
        _admin, cookie, key = self._create("admin.one", "沈林", "林深")
        me = self._send("/v1/me", bearer=key)
        self.assertEqual(me["role"], "admin")
        self.assertEqual(me["store_key"], key)
        listed = self._rpc("tools/list", bearer=key)
        names = [item["name"] for item in listed["result"]["tools"]]
        self.assertIn("approve", names)
        self.assertNotIn(key, json.dumps(listed))
        status, replaced, new_cookie = self._raw("/v1/account/key", {}, cookie=cookie)
        self.assertEqual(status, 200, replaced)
        self.assertNotEqual(replaced["key"], key)
        self.assertIn(replaced["key"], json.dumps(replaced["mcp"]))
        self.assertEqual(self._send("/v1/me", bearer=key)["role"], "anonymous")
        self.assertEqual(self._send("/v1/me", bearer=replaced["key"])["role"], "admin")
        self.assertEqual(self._get("/v1/me", new_cookie)["username"], "admin.one")
        self.assertEqual(self._get("/v1/me", new_cookie)["store_key"], replaced["key"])

    def test_profile_can_change_without_touching_the_key(self) -> None:
        _admin, cookie, key = self._create("admin.one", "沈林", "林深", "数字化")
        updated = self._post("/v1/account/profile", {
            "real_name": "沈林深",
            "nickname": "深树",
            "department": "实验室",
            "job_title": "产品",
        }, cookie=cookie)
        self.assertEqual(updated["nickname"], "深树")
        self.assertEqual(updated["department"], "实验室")
        self.assertNotIn("key", updated)
        me = self._send("/v1/me", bearer=key)
        self.assertEqual(me["name"], "深树")
        self.assertEqual(me["job_title"], "产品")

    def test_user_cannot_choose_a_key_and_last_admin_stays(self) -> None:
        chosen = self._post("/v1/account", {
            "username": "admin.one", "real_name": "沈林", "nickname": "林深", "key": "my-own-key",
        }, expect=400)
        self.assertEqual(chosen["rule"], "KEY_INVALID")
        _admin, cookie, _key = self._create("admin.one", "沈林", "林深")
        again = self._post("/v1/account", {
            "username": "admin.one", "real_name": "另一个", "nickname": "另一个",
        }, expect=400)
        self.assertEqual(again["rule"], "USERNAME_TAKEN")
        blank = self._post("/v1/account", {
            "username": "author.one", "real_name": "", "nickname": "木青",
        }, expect=400)
        self.assertEqual(blank["rule"], "NAME_INVALID")
        typed = self._post("/v1/account/key", {"new_key": "still-typed"}, cookie=cookie, expect=400)
        self.assertEqual(typed["rule"], "KEY_INVALID")
        mismatch = self._post("/v1/login", {"username": "admin.one", "password": "nope-nope"}, expect=400)
        self.assertEqual(mismatch["rule"], "PASSWORD_MISMATCH")
        with self.assertRaises(StoreError) as last:
            profile, session = self.store.login_account("admin.one", PASSWORD)
            self.store.set_account_role(session, profile["id"], "publisher")
        self.assertEqual(last.exception.rule, "LAST_ADMIN")

    def test_password_rules_and_change_do_not_touch_the_key(self) -> None:
        weak = self._post("/v1/account", {
            "username": "author.two", "real_name": "顾木", "nickname": "木青",
            "password": "short", "password_again": "short",
        }, expect=400)
        self.assertEqual(weak["rule"], "PASSWORD_INVALID")
        copied = self._post("/v1/account", {
            "username": "author.two", "real_name": "顾木", "nickname": "木青",
            "password": "author.two-Aa1", "password_again": "author.two-Aa1",
        }, expect=400)
        self.assertEqual(copied["rule"], "PASSWORD_INVALID")
        mismatch = self._post("/v1/account", {
            "username": "author.two", "real_name": "顾木", "nickname": "木青",
            "password": PASSWORD, "password_again": NEXT_PASSWORD,
        }, expect=400)
        self.assertEqual(mismatch["rule"], "PASSWORD_MISMATCH")
        _admin, cookie, key = self._create("admin.one", "沈林", "林深")
        using_key = self._post("/v1/login", {"username": "admin.one", "password": key}, expect=400)
        self.assertEqual(using_key["rule"], "PASSWORD_MISMATCH")
        changed = self._post("/v1/account/password", {
            "old_password": PASSWORD, "new_password": NEXT_PASSWORD,
        }, cookie=cookie)
        self.assertNotIn("password", changed)
        self.assertNotIn(NEXT_PASSWORD, json.dumps(changed))
        self.assertEqual(self._send("/v1/me", bearer=key)["role"], "admin")
        old = self._post("/v1/login", {"username": "admin.one", "password": PASSWORD}, expect=400)
        self.assertEqual(old["rule"], "PASSWORD_MISMATCH")
        self.assertEqual(self._get("/v1/me", self._login("admin.one", NEXT_PASSWORD))["username"], "admin.one")

    def test_admin_resets_another_password_without_keeping_it(self) -> None:
        from store.accounts import password_problems

        admin, admin_cookie, _admin_key = self._create("admin.one", "沈林", "林深")
        author, author_cookie, author_key = self._create("author.one", "顾木", "木青")
        forbidden = self._post(
            f"/v1/accounts/{author['id']}/password", {}, cookie=author_cookie, expect=403,
        )
        self.assertEqual(forbidden["rule"], "ROLE_FORBIDDEN")
        own = self._post(f"/v1/accounts/{admin['id']}/password", {}, cookie=admin_cookie, expect=403)
        self.assertEqual(own["rule"], "ROLE_FORBIDDEN")
        typed = self._post(
            f"/v1/accounts/{author['id']}/password",
            {"password": NEXT_PASSWORD},
            cookie=admin_cookie,
            expect=400,
        )
        self.assertEqual(typed["rule"], "PASSWORD_INVALID")
        status, reset, _cookie = self._raw(
            f"/v1/accounts/{author['id']}/password", {}, cookie=admin_cookie,
        )
        self.assertEqual(status, 200, reset)
        new_password = reset["password"]
        self.assertEqual(password_problems(new_password, "author.one"), [])
        self.assertNotIn(new_password, json.dumps(self._get("/v1/accounts/manage", admin_cookie)))
        self.assertEqual(self._get("/v1/me", author_cookie)["role"], "anonymous")
        self.assertEqual(self._get("/v1/me", admin_cookie)["role"], "admin")
        self.assertEqual(self._send("/v1/me", bearer=author_key)["role"], "publisher")
        old = self._post("/v1/login", {"username": "author.one", "password": PASSWORD}, expect=400)
        self.assertEqual(old["rule"], "PASSWORD_MISMATCH")
        self.assertEqual(self._get("/v1/me", self._login("author.one", new_password))["username"], "author.one")

    def _create(
        self,
        username: str,
        real_name: str,
        nickname: str,
        department: str = "",
        job_title: str = "",
        password: str = PASSWORD,
    ) -> tuple[dict, str, str]:
        status, body, cookie = self._raw("/v1/account", {
            "username": username,
            "real_name": real_name,
            "nickname": nickname,
            "department": department,
            "job_title": job_title,
            "password": password,
            "password_again": password,
        })
        self.assertEqual(status, 200, body)
        key = body["key"]
        self.assertGreaterEqual(len(key), 20)
        self.assertNotIn(password, json.dumps(body))
        public = {name: value for name, value in body.items() if name not in {"key", "mcp", "store_key"}}
        self.assertNotIn(key, json.dumps(public))
        return body, cookie, key

    def _login(self, username: str, password: str = PASSWORD) -> str:
        status, body, cookie = self._raw("/v1/login", {"username": username, "password": password})
        self.assertEqual(status, 200, body)
        self.assertNotIn("key", body)
        self.assertNotIn(password, json.dumps(body))
        return cookie

    def _get(self, path: str, cookie: str = "") -> dict:
        return self._open(path, cookie)

    def _open(self, path: str, cookie: str = "", expect: int = 200) -> dict:
        request = urllib.request.Request(self.base + path)
        if cookie:
            request.add_header("Cookie", f"qilin_session={cookie}")
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            payload = json.load(exc)
            exc.close()
            if exc.code != expect:
                raise
            return payload

    def _post(self, path: str, body: dict, cookie: str = "", bearer: str = "", expect: int = 200) -> dict:
        status, payload, _cookie = self._raw(path, body, cookie, bearer)
        self.assertEqual(status, expect, payload)
        return payload

    def _send(self, path: str, bearer: str) -> dict:
        request = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {bearer}"})
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def _rpc(self, method: str, bearer: str) -> dict:
        request = urllib.request.Request(
            self.base + "/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {bearer}",
            },
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def _raw(self, path: str, body: dict, cookie: str = "", bearer: str = "") -> tuple[int, dict, str]:
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"qilin_session={cookie}"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(), headers=headers,
        )
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, json.loads(raw or b"{}"), _session(response.headers.get("Set-Cookie", ""))
        except urllib.error.HTTPError as exc:
            payload = json.load(exc)
            exc.close()
            return exc.code, payload, ""


def _session(header: str) -> str:
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == "qilin_session":
            return value
    return ""


if __name__ == "__main__":
    unittest.main()
