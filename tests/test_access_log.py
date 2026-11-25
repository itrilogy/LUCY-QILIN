"""上传票据不能进访问日志（评审 P2-9）。

票据在 URL 的 `?ticket=` 里（独立终端只凭 URL 上传，这个便利性保留），
但它是 1 小时有效的能力凭据。访问日志原样记 requestline，等于抄了一份凭据。
这里把 `ticket=` 的值从应用访问日志里抹掉；反向代理自己的 access log 仍会记
完整 URL，部署文档里要求对它做同样处理或干脆不记 `/v1/uploads`。
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from store.http_api import _redact_ticket, serve
from store.service import Store

SECRET = "ticket-value-that-must-not-leak"


class RedactTests(unittest.TestCase):
    def test_ticket_value_is_replaced(self) -> None:
        line = f'PUT /v1/uploads/abc?ticket={SECRET} HTTP/1.0" 200 -'
        redacted = _redact_ticket(line)
        self.assertNotIn(SECRET, redacted)
        self.assertIn("ticket=<redacted>", redacted)

    def test_other_query_parameters_are_kept(self) -> None:
        line = f"GET /v1/artifacts/a/1.0.0?platform=linux-amd64&ticket={SECRET} HTTP/1.0"
        redacted = _redact_ticket(line)
        self.assertIn("platform=linux-amd64", redacted)
        self.assertNotIn(SECRET, redacted)

    def test_lines_without_ticket_are_untouched(self) -> None:
        line = 'GET /v1/catalog?query=abc HTTP/1.0" 200 1234'
        self.assertEqual(_redact_ticket(line), line)


class AccessLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        self.server = serve(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_access_log_does_not_contain_the_ticket(self) -> None:
        captured = io.StringIO()
        request = urllib.request.Request(
            f"{self.base}/v1/uploads/nope?ticket={SECRET}",
            data=b"x",
            method="PUT",
        )
        with mock.patch.dict(os.environ, {"QILIN_ACCESS_LOG": "1"}), \
                mock.patch("sys.stderr", captured):
            try:
                urllib.request.urlopen(request, timeout=10)
            except urllib.error.HTTPError:
                pass
        log = captured.getvalue()
        self.assertIn("/v1/uploads/nope", log, "请求本身仍要能被看到")
        self.assertNotIn(SECRET, log, "票据不能进访问日志")


if __name__ == "__main__":
    unittest.main()
