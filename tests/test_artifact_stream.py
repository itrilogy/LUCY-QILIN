"""制品下载：流式、Range/206、条件请求（评审 P2-13）。

旧实现把整块制品读进内存再写：32MiB 的包乘以并发数就是内存峰值，而且不支持
断点续传——`Range` 被忽略，永远回 200 全量。现在按 64KiB 块流式发送，
支持单段 Range、`If-None-Match` 与 `Accept-Ranges`。
"""

from __future__ import annotations

import hashlib
import http.client
import tempfile
import threading
import unittest
from pathlib import Path

from store.http_api import serve
from store.service import Store
from tests.test_service import SKILL, skill_manifest


class ArtifactStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.reviewer = self.store.create_principal("审核", "reviewer")
        result = self.store.submit(
            self.publisher, skill_manifest(id="luxi.stream"), {"SKILL.md": SKILL.encode()},
        )
        self.assertEqual(result.status, "pending", result.rules)
        self.store.approve(self.reviewer, result.submission_id)
        self.path, self.digest = self.store.artifact_file("luxi.stream", "3.2.0")
        self.payload = self.path.read_bytes()
        self.server = serve(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _get(self, headers: dict | None = None, method: str = "GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, "/v1/artifacts/luxi.stream/3.2.0", headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_full_download_matches_the_file(self) -> None:
        status, headers, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.payload)
        self.assertEqual(headers.get("X-Checksum-Sha256"), self.digest)
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(headers.get("ETag"), f'"{self.digest}"')

    def test_range_request_returns_partial_content(self) -> None:
        status, headers, body = self._get({"Range": "bytes=0-99"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.payload[:100])
        self.assertEqual(headers.get("Content-Range"), f"bytes 0-99/{len(self.payload)}")
        self.assertEqual(headers.get("Content-Length"), "100")

    def test_suffix_range_returns_the_tail(self) -> None:
        status, headers, body = self._get({"Range": "bytes=-10"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.payload[-10:])
        self.assertEqual(headers.get("Content-Range"), f"bytes {len(self.payload) - 10}-{len(self.payload) - 1}/{len(self.payload)}")

    def test_open_ended_range(self) -> None:
        start = max(0, len(self.payload) // 2)
        status, headers, body = self._get({"Range": f"bytes={start}-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, self.payload[start:])
        self.assertEqual(
            headers.get("Content-Range"), f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}",
        )

    def test_invalid_range_is_416(self) -> None:
        status, headers, body = self._get({"Range": f"bytes={len(self.payload) + 10}-"})
        self.assertEqual(status, 416)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Content-Range"), f"bytes */{len(self.payload)}")

    def test_head_has_range_headers_and_no_body(self) -> None:
        status, headers, body = self._get(method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(headers.get("Content-Length"), str(len(self.payload)))

    def test_if_none_match_returns_304(self) -> None:
        status, headers, body = self._get({"If-None-Match": f'"{self.digest}"'})
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("ETag"), f'"{self.digest}"')
        self.assertEqual(hashlib.sha256(self.payload).hexdigest(), self.digest)


if __name__ == "__main__":
    unittest.main()
