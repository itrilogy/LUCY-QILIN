"""枢纽令牌：验签、角色、过期，以及商店 MCP 握手。"""

from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.http_api import serve
from store.hub_client import post_token
from store.identity import IdentityError
from store.service import Store, StoreError
from tests.tokens import ORIGIN, make_hub


class HubTokenTests(unittest.TestCase):
    def test_roles_come_from_the_user_module(self) -> None:
        identity, sign = make_hub()
        publisher = identity.principal(sign(roles=["publisher"]))
        self.assertEqual((publisher.id, publisher.name, publisher.role), ("user-1", "张三", "publisher"))
        admin = identity.principal(sign(roles=["publisher", "admin"], name="李四"))
        self.assertEqual(admin.role, "admin")
        self.assertEqual(admin.name, "李四")
        member = identity.principal(sign(roles=["viewer"]))
        self.assertEqual(member.role, "member")

    def test_rejects_wrong_audience_algorithm_and_expiry(self) -> None:
        identity, sign = make_hub()
        with self.assertRaises(IdentityError) as audience:
            identity.principal(sign(roles=["publisher"], aud="lucy-wenjin"))
        self.assertIn("不是发给栖林", audience.exception.message)
        with self.assertRaises(IdentityError) as algorithm:
            identity.principal(sign(roles=["publisher"], alg="none"))
        self.assertIn("算法", algorithm.exception.message)
        expired, sign_expired = make_hub(now=1_700_000_000)
        with self.assertRaises(IdentityError) as stale:
            expired.principal(sign_expired(roles=["publisher"], exp=1_700_000_000 - 120))
        self.assertIn("过期", stale.exception.message)
        identity.assert_logout(sign(roles=[], typ="logout"))

    def test_uses_the_cached_jwks_when_the_hub_is_down(self) -> None:
        root = Path(tempfile.mkdtemp())
        identity, sign = make_hub()
        identity.cache_path = root / "jwks-cache.json"
        token = sign(roles=["publisher"])
        identity.principal(token)
        identity._opener = lambda _url: (_ for _ in ()).throw(OSError("down"))
        identity._cached = None
        identity._cached_at = 0
        self.assertEqual(identity.principal(token).role, "publisher")

    def test_store_accepts_the_token_and_mcp_handshake(self) -> None:
        identity, sign = make_hub()
        root = Path(tempfile.mkdtemp())
        store = Store(root / "store.db", root / "blobs", identity=identity)
        self.addCleanup(store.close)
        server = serve(store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_port}"
        publisher = sign(roles=["publisher"])
        me = _get(base + "/v1/me", publisher)
        self.assertEqual(me["role"], "publisher")
        self.assertEqual(me["source"], "hub")
        self.assertEqual(me["id"], "user-1")
        self.assertNotIn(publisher, json.dumps(me))
        member = _json(_request(base + "/v1/mine", sign(roles=["viewer"]), expect=403))
        self.assertEqual(member["rule"], "ROLE_FORBIDDEN")
        health = json.load(urllib.request.urlopen(base + "/api/health"))
        self.assertEqual(health["identity"], "hub")
        self.assertEqual(health["app_id"], "lucy-qilin")
        status = _get(base + "/v1/identity")
        self.assertEqual(status["origin"], ORIGIN)
        _request(
            base + "/auth/logout/backchannel",
            body={"logout_token": sign(roles=[], typ="logout")},
            expect=204,
        )
        listed = _rpc(base, "tools/list", {}, publisher)
        names = [item["name"] for item in listed["result"]["tools"]]
        self.assertIn("begin_submit", names)
        self.assertNotIn("approve", names)
        self.assertNotIn(publisher, json.dumps(listed))
        handshake = _rpc(base, "initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }, "")
        self.assertEqual(handshake["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(handshake["result"]["serverInfo"]["name"], "qilin-store")
        ping = _rpc(base, "ping", {}, "")
        self.assertEqual(ping["result"], {})
        searched = _rpc(base, "tools/call", {"name": "search", "arguments": {}}, "")
        self.assertFalse(searched["result"]["isError"])
        self.assertIn("items", json.loads(searched["result"]["content"][0]["text"]))


class HubLoginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity, self.sign = make_hub()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.calls: list[str] = []
        self.short = False
        self.fail_refresh = False
        self.omit_refresh = False
        self.roles = ["publisher"]
        self.store = Store(
            root / "store.db",
            root / "blobs",
            identity=self.identity,
            hub_secret="hub-secret",
            hub_post=self._post,
        )
        self.addCleanup(self.store.close)
        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_callback_opens_a_session_without_a_store_key(self) -> None:
        status, headers, body = _no_redirect(self.base + "/auth/callback?code=ec_1&state=s")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/")
        cookie = _session(headers.get("Set-Cookie", ""))
        self.assertTrue(cookie)
        self.assertNotIn("rt_one", body.decode())
        self.assertNotIn("rt_one", headers.get("Set-Cookie", ""))
        self.assertNotIn("hub-secret", body.decode())
        me = _get(self.base + "/v1/me", cookie=cookie)
        self.assertEqual(me["role"], "publisher")
        self.assertEqual(me["source"], "hub")
        self.assertEqual(me["name"], "张三")
        self.assertFalse(me["key_ready"])
        self.assertNotIn("rt_one", json.dumps(me))
        self.assertEqual(self.calls, ["authorization_code"])
        self.assertTrue(json.load(urllib.request.urlopen(self.base + "/v1/identity"))["login"])

    def test_missing_state_does_not_call_the_hub(self) -> None:
        status, headers, body = _no_redirect(self.base + "/auth/callback?code=ec_1")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["rule"], "IDENTITY_INVALID")
        # 401 要带挑战头，客户端才知道该拿什么凭据重试（RFC 7235）。
        self.assertEqual(headers.get("WWW-Authenticate"), 'Bearer realm="qilin"')
        self.assertEqual(self.calls, [])

    def test_near_expiry_rotates_and_a_failure_drops_the_session(self) -> None:
        self.short = True
        _profile, cookie = self.store.login_hub_code("ec_1")
        self.store.refresh_hub_if_due(cookie)
        me = self.store.whoami(cookie)
        self.assertEqual(me["role"], "admin")
        self.assertNotIn("rt_two", json.dumps(me))
        self.assertEqual(self.calls, ["authorization_code", "refresh_token"])
        again = len(self.calls)
        self.store.refresh_hub_if_due(cookie)
        self.assertEqual(len(self.calls), again)
        self.fail_refresh = True
        self.short = True
        _profile, other = self.store.login_hub_code("ec_2")
        self.store.refresh_hub_if_due(other)
        self.assertEqual(self.store.whoami(other)["role"], "anonymous")
        self.assertEqual(self.store.whoami(cookie)["role"], "admin")

    def test_backchannel_logout_clears_the_hub_session(self) -> None:
        _profile, cookie = self.store.login_hub_code("ec_1")
        self.store.accept_logout(self.sign(roles=[], typ="logout"))
        self.assertEqual(self.store.whoami(cookie)["role"], "anonymous")

    def test_without_a_refresh_token_the_session_stops_at_access_exp(self) -> None:
        self.omit_refresh = True
        _profile, cookie = self.store.login_hub_code("ec_1")
        self.store.refresh_hub_if_due(cookie)
        self.assertEqual(self.calls, ["authorization_code"])
        self.assertEqual(self.store.whoami(cookie)["role"], "publisher")

    def test_member_session_can_look_but_not_upload(self) -> None:
        self.roles = ["viewer"]
        _profile, cookie = self.store.login_hub_code("ec_1")
        self.assertEqual(self.store.whoami(cookie)["role"], "member")
        with self.assertRaises(StoreError) as caught:
            self.store.list_my_submissions(cookie)
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_callback_without_a_secret_is_refused(self) -> None:
        bare = Store(Path(self.tmp.name) / "bare.db", Path(self.tmp.name) / "bare-blobs", identity=self.identity)
        self.addCleanup(bare.close)
        with self.assertRaises(StoreError) as caught:
            bare.login_hub_code("ec_1")
        self.assertEqual(caught.exception.rule, "IDENTITY_UNCONFIGURED")

    def test_authorization_code_is_signed_and_refresh_is_not(self) -> None:
        seen: list[urllib.request.Request] = []

        def opener(request: urllib.request.Request) -> bytes:
            seen.append(request)
            return json.dumps({"code": 0, "data": {"access_token": "a.b.c"}}).encode()

        post_token(
            origin=ORIGIN,
            app_id="lucy-qilin",
            secret="hub-secret",
            kind="authorization_code",
            payload={"code": "ec_1"},
            opener=opener,
        )
        body = seen[0].data or b""
        signed = "\n".join([
            "POST",
            "/api/v1/auth/token",
            seen[0].get_header("X-lucy-timestamp"),
            seen[0].get_header("X-lucy-nonce"),
            hashlib.sha256(body).hexdigest(),
        ])
        expected = hmac.new(b"hub-secret", signed.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(seen[0].get_header("X-lucy-signature"), expected)
        self.assertEqual(seen[0].get_header("X-lucy-app-id"), "lucy-qilin")
        self.assertNotIn(b"hub-secret", body)
        post_token(
            origin=ORIGIN,
            app_id="lucy-qilin",
            secret="hub-secret",
            kind="refresh_token",
            payload={"refresh_token": "rt_one"},
            opener=opener,
        )
        self.assertIsNone(seen[1].get_header("X-lucy-signature"))

    def _post(self, kind: str, payload: dict) -> dict:
        self.calls.append(kind)
        self.store.search()
        if kind == "refresh_token":
            if self.fail_refresh or payload.get("refresh_token") != "rt_one":
                raise IdentityError("IDENTITY_INVALID", "枢纽没有换出令牌。")
            return {
                "access_token": self.sign(roles=["admin"], exp=int(time.time()) + 900),
                "expires_in": 900,
                "refresh_token": "rt_two",
                "refresh_expires_in": 3600,
            }
        return {
            "access_token": self.sign(
                roles=self.roles,
                exp=int(time.time()) + (30 if self.short else 600),
            ),
            "expires_in": 600,
            **({} if self.omit_refresh else {"refresh_token": "rt_one", "refresh_expires_in": 3600}),
        }


def _get(url: str, token: str = "", cookie: str = "") -> dict:
    return _json(_request(url, token, cookie=cookie))


def _no_redirect(url: str) -> tuple[int, object, bytes]:
    class Keep(urllib.request.HTTPErrorProcessor):
        def http_response(self, request, response):
            return response

        https_response = http_response

    opener = urllib.request.build_opener(Keep)
    with opener.open(url, timeout=5) as response:
        return response.status, response.headers, response.read()


def _session(header: str) -> str:
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == "qilin_session":
            return value
    return ""


def _request(url: str, token: str = "", body: dict | None = None, expect: int = 200, cookie: str = ""):
    data = None if body is None else json.dumps(body).encode()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = f"qilin_session={cookie}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            if response.status != expect:
                raise AssertionError(response.status)
            return raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        exc.close()
        if exc.code != expect:
            raise
        return raw


def _json(raw: bytes) -> dict:
    return json.loads(raw)


def _rpc(base: str, method: str, params: dict, token: str) -> dict:
    request = urllib.request.Request(
        base + "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    with urllib.request.urlopen(request) as response:
        if method == "initialize":
            assert response.headers.get("Mcp-Session-Id") is None
        return json.load(response)


if __name__ == "__main__":
    unittest.main()
