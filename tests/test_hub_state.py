"""跳转登录的 state 绑定（审计 C1）。

三个场景分开验：
1. 配了授权页时，`/auth/start` 签发 state 并写 Cookie；带同一个 Cookie 与
   同一个 state 回来才能换成会话。
2. 配了授权页时，没有 Cookie、或 state 对不上，一律拒绝，且不调用枢纽。
3. 没配授权页时（枢纽自行发起跳转），回调仍可用，但记一条 `STATE_UNBOUND`，
   不在代码里假装已经绑定。

这一组不验证枢纽端的行为——枢纽不在本仓库，不替它假设接口形态。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from store.http_api import PORTAL, _authorize_url, serve
from store.service import Store
from tests.tokens import make_hub

TEMPLATE = "https://hub.example/authorize?app_id={app_id}&state={state}&redirect_uri={redirect_uri}"


class _Hub:
    """最小的假枢纽：换码即签发一张 publisher 令牌，不访问网络。"""

    def __init__(self) -> None:
        self.identity, self.sign = make_hub()
        self.calls: list[str] = []

    def post(self, kind: str, payload: dict) -> dict:
        self.calls.append(kind)
        return {
            "access_token": self.sign(roles=["publisher"], exp=int(time.time()) + 600),
            "expires_in": 600,
            "refresh_token": "rt_one",
            "refresh_expires_in": 3600,
        }


class _ServerCase(unittest.TestCase):
    hub_authorize = ""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.hub = _Hub()
        self.store = Store(
            root / "store.db",
            root / "blobs",
            identity=self.hub.identity,
            hub_secret="hub-secret",
            hub_post=self.hub.post,
        )
        self.addCleanup(self.store.close)
        self.server = serve(self.store, hub_authorize=self.hub_authorize)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"


class StateBoundTests(_ServerCase):
    """配了授权页：state 由商店签发，回调必须对得上。"""

    hub_authorize = TEMPLATE

    def test_start_issues_a_state_cookie_and_targets_the_hub(self) -> None:
        status, headers, _body = _no_redirect(self.base + "/auth/start")
        self.assertEqual(status, 302)
        location = headers["Location"]
        self.assertTrue(location.startswith("https://hub.example/authorize?"), location)
        self.assertNotIn("{state}", location)
        self.assertNotIn("{redirect_uri}", location)
        cookie = "; ".join(headers.get_all("Set-Cookie") or [])
        self.assertIn("qilin_hub_state=", cookie)
        self.assertIn("Path=/auth", cookie)
        self.assertIn("HttpOnly", cookie)
        # state 落在 Cookie 与授权地址里，不进响应体。
        self.assertTrue(_value(location, "state"))

    def test_state_is_fresh_each_time(self) -> None:
        first = _value(_no_redirect(self.base + "/auth/start")[1]["Location"], "state")
        second = _value(_no_redirect(self.base + "/auth/start")[1]["Location"], "state")
        self.assertNotEqual(first, second)

    def test_callback_with_the_matching_cookie_opens_a_session(self) -> None:
        state = "s" * 24
        status, headers, _body = _no_redirect(
            self.base + f"/auth/callback?code=ec_1&state={state}",
            cookie=f"qilin_hub_state={state}",
        )
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")
        cookies = "; ".join(headers.get_all("Set-Cookie") or [])
        self.assertIn("qilin_session=", cookies)
        # 一次性：回调后把 state Cookie 清掉。
        self.assertIn("qilin_hub_state=;", cookies)
        self.assertIn("Max-Age=0", cookies)
        self.assertEqual(self.hub.calls, ["authorization_code"])

    def test_callback_without_the_cookie_is_refused(self) -> None:
        status, _headers, body = _no_redirect(self.base + "/auth/callback?code=ec_1&state=abc")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["rule"], "IDENTITY_INVALID")
        # 拒绝发生在换令牌之前，不给「拿别人的码换会话」留窗口。
        self.assertEqual(self.hub.calls, [])

    def test_callback_with_a_different_state_is_refused(self) -> None:
        status, _headers, body = _no_redirect(
            self.base + "/auth/callback?code=ec_1&state=other",
            cookie="qilin_hub_state=mine",
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["rule"], "IDENTITY_INVALID")
        self.assertEqual(self.hub.calls, [])

    def test_state_of_a_different_length_is_refused(self) -> None:
        # compare_digest 对长度不同也返回 False，这里确认不会抛异常。
        status, _headers, body = _no_redirect(
            self.base + "/auth/callback?code=ec_1&state=x",
            cookie="qilin_hub_state=" + "y" * 40,
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["rule"], "IDENTITY_INVALID")

    def test_identity_reports_that_the_page_may_start_the_flow(self) -> None:
        with urllib.request.urlopen(self.base + "/v1/identity") as response:
            payload = json.load(response)
        self.assertTrue(payload["hub_start"])


class StateUnboundTests(_ServerCase):
    """没配授权页：回调仍可用，但要记一笔「无从核对」。"""

    def test_start_is_unavailable_without_a_template(self) -> None:
        status, _headers, body = _no_redirect(self.base + "/auth/start")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["rule"], "IDENTITY_UNCONFIGURED")

    def test_callback_still_works_and_is_logged(self) -> None:
        with self.assertLogs("qilin", level="WARNING") as logs:
            status, headers, _body = _no_redirect(self.base + "/auth/callback?code=ec_1&state=s")
        self.assertEqual(status, 302)
        self.assertIn("qilin_session=", "; ".join(headers.get_all("Set-Cookie") or []))
        self.assertTrue(
            any("STATE_UNBOUND" in line for line in logs.output), logs.output,
        )

    def test_identity_says_the_page_cannot_start_the_flow(self) -> None:
        with urllib.request.urlopen(self.base + "/v1/identity") as response:
            payload = json.load(response)
        self.assertFalse(payload["hub_start"])


class AuthorizeUrlTests(unittest.TestCase):
    """模板替换只动占位，不重排或增删参数。"""

    def test_placeholders_are_filled_and_encoded(self) -> None:
        url = _authorize_url(TEMPLATE, "a b/c", "https://store.example", "lucy-qilin")
        self.assertIn("app_id=lucy-qilin", url)
        self.assertIn("state=a%20b%2Fc", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fstore.example%2Fauth%2Fcallback", url)
        self.assertNotIn("{", url)

    def test_missing_placeholders_are_left_alone(self) -> None:
        url = _authorize_url("https://hub.example/authorize", "s", "https://store.example")
        self.assertEqual(url, "https://hub.example/authorize")

    def test_redirect_uri_comes_from_the_configured_base(self) -> None:
        url = _authorize_url(TEMPLATE, "s", "https://store.example/")
        self.assertIn("redirect_uri=https%3A%2F%2Fstore.example%2Fauth%2Fcallback", url)


class PortalEntryTests(unittest.TestCase):
    """门户里的入口只在商店真能发起跳转时才显示。

    这条不跑浏览器，只检查静态页面没有把按钮做成「无条件可见」，
    并确认它指向的地址与服务端路由一致。
    """

    def setUp(self) -> None:
        self.page = PORTAL.read_text(encoding="utf-8") + (PORTAL.parent / "app.js").read_text(encoding="utf-8")

    def test_button_starts_hidden(self) -> None:
        self.assertIn('id="hub-login"', self.page)
        self.assertIn('class="btn hidden" id="hub-login"', self.page)

    def test_button_points_at_the_start_route(self) -> None:
        self.assertIn('window.location.assign("/auth/start")', self.page)

    def test_button_is_unhidden_only_when_the_server_can_start(self) -> None:
        self.assertIn("info.hub_start", self.page)


def _no_redirect(url: str, cookie: str = "") -> tuple[int, object, bytes]:
    """取 302 本身，不跟随跳转。"""
    class Keep(urllib.request.HTTPErrorProcessor):
        def http_response(self, request, response):
            return response

        https_response = http_response

    request = urllib.request.Request(url)
    if cookie:
        request.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(Keep)
    with opener.open(request, timeout=5) as response:
        return response.status, response.headers, response.read()


def _value(url: str, key: str) -> str:
    return (parse_qs(urlparse(url).query).get(key) or [""])[0]


if __name__ == "__main__":
    unittest.main()
