"""HTTP 请求卫生（评审 P2-10 / P2-14 / P2-17）。

- 超大请求体回 413（以前回 400 "CHECKSUM"，客户端分不清"太大"和"格式错"）；
- 需要登录的写接口在**读正文之前**就回 401（匿名的大 POST 不该被读完再拒）；
- HEAD 有头无体、OPTIONS 回 204 + Allow（监控、CDN、浏览器预检要用）；
- `/api/livez`（存活）与 `/api/readyz`（就绪）分开，`/api/health` 保留兼容。
"""

from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from store.http_api import serve
from store.service import Store


def _raw(host: str, port: int, request: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


class HttpSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        self.server = serve(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = "127.0.0.1"
        self.port = self.server.server_port

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, method: str, path: str) -> tuple[int, dict, bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_oversized_body_is_413(self) -> None:
        # 只发头、不发送正文：服务端必须在读正文之前就拒绝，否则这条会卡住。
        request = (
            b"POST /mcp HTTP/1.0\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 999999999\r\n\r\n"
        )
        raw = _raw(self.host, self.port, request)
        self.assertRegex(raw.split(b"\r\n")[0], rb"^HTTP/1\.[01] 413")
        self.assertIn(b"PAYLOAD_TOO_LARGE", raw)

    def test_anonymous_write_is_rejected_before_the_body_is_read(self) -> None:
        request = (
            b"POST /v1/submissions HTTP/1.0\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 999999999\r\n\r\n"
        )
        raw = _raw(self.host, self.port, request)
        self.assertRegex(raw.split(b"\r\n")[0], rb"^HTTP/1\.[01] 401")
        self.assertIn(b"AUTH_REQUIRED", raw)

    def test_head_returns_headers_without_a_body(self) -> None:
        status, headers, body = self._request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertGreater(int(headers.get("Content-Length", "0")), 0)

    def test_head_on_json_endpoint(self) -> None:
        status, headers, body = self._request("HEAD", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("application/json", headers.get("Content-Type", ""))

    def test_options_lists_allowed_methods(self) -> None:
        status, headers, body = self._request("OPTIONS", "/")
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        allow = headers.get("Allow", "")
        for method in ("GET", "HEAD", "POST", "PUT", "OPTIONS"):
            self.assertIn(method, allow)

    def test_keep_alive_reuses_the_connection(self) -> None:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            for path in ("/api/livez", "/api/health", "/"):
                connection.request("GET", path)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200, path)
                self.assertFalse(response.will_close, f"{path} 不应关掉连接")
        finally:
            connection.close()

    def test_form_content_type_is_415(self) -> None:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(
                "POST", "/mcp", body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(response.status, 415)
            self.assertEqual(json.loads(body)["rule"], "UNSUPPORTED_MEDIA_TYPE")
        finally:
            connection.close()

    def test_json_with_charset_is_accepted(self) -> None:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(
                "POST", "/mcp",
                body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        finally:
            connection.close()

    def test_unread_body_closes_the_connection(self) -> None:
        request = (
            b"POST /v1/submissions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 999999999\r\n\r\n"
        )
        raw = _raw(self.host, self.port, request)
        self.assertRegex(raw.split(b"\r\n")[0], rb"^HTTP/1\.[01] 401")
        self.assertIn(b"Connection: close", raw, "正文没读完就必须关连接，避免下一条请求被污染")

    def test_livez_does_not_touch_the_database(self) -> None:
        status, _headers, body = self._request("GET", "/api/livez")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_readyz_matches_health(self) -> None:
        status, _headers, body = self._request("GET", "/api/readyz")
        self.assertEqual(status, 200)
        ready = json.loads(body)
        self.assertEqual(ready["status"], "ok")
        self.assertEqual(ready["database"], "ok")
        _status, _headers, health_body = self._request("GET", "/api/health")
        self.assertEqual(json.loads(health_body), ready, "/api/health 保留为就绪探针的别名")


if __name__ == "__main__":
    unittest.main()
