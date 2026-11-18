"""商店 MCP：匿名、用户密钥、管理密钥看到的工具不同。"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.blobs import write_zip
from store.http_api import serve
from store.service import Store
from tests.test_service import SKILL, skill_manifest


class McpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")
        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_anonymous_can_search_but_not_submit(self) -> None:
        listed = self._rpc("tools/list", {}, token="")
        names = [item["name"] for item in listed["result"]["tools"]]
        self.assertIn("search", names)
        self.assertNotIn("begin_submit", names)
        self.assertNotIn("approve", names)
        denied = self._rpc("tools/call", {"name": "begin_submit", "arguments": {}}, token="")
        self.assertTrue(denied["result"]["isError"])
        self.assertEqual(json.loads(denied["result"]["content"][0]["text"])["rule"], "ROLE_FORBIDDEN")
        blob = json.dumps(listed)
        self.assertNotIn("token", blob)
        self.assertNotIn("bearer", blob.lower())
        opened = self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }, token="")
        self.assertEqual(opened["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(opened["result"]["serverInfo"]["name"], "qilin-store")
        self.assertIn("不在这台服务器上执行安装", opened["result"]["instructions"])
        self.assertIn("search", opened["result"]["instructions"])
        self.assertIn("get_spec", opened["result"]["instructions"])
        self.assertNotIn("begin_submit", opened["result"]["instructions"])
        self.assertIn("get_spec", names)
        self.assertIn("resources", opened["result"]["capabilities"])
        search = next(item for item in listed["result"]["tools"] if item["name"] == "search")
        self.assertIn("summary", search["description"])
        self.assertEqual(self._rpc("ping", {}, token="")["result"], {})
        get = urllib.request.Request(self.base + "/mcp", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as denied_get:
            urllib.request.urlopen(get)
        self.assertEqual(denied_get.exception.code, 405)
        denied_get.exception.close()
        self.assertEqual(_raw_get(self.server.server_port).count(b"HTTP/1."), 1)

    def test_publisher_uploads_and_cannot_approve(self) -> None:
        archive = self._zip()
        started = self._tool("begin_submit", {
            "manifest": skill_manifest(),
            "submit_kind": "new_version",
            "byte_size": len(archive),
        }, self.publisher)
        self.assertEqual(started["status"], "uploading")
        self.assertNotIn(self.publisher, json.dumps(started))
        self._put(started["put_url"], archive, self.publisher)
        finished = self._tool("finish_submit", {"submission_id": started["submission_id"]}, self.publisher)
        self.assertEqual(finished["status"], "pending", finished)
        denied = self._tool("approve", {"submission_id": started["submission_id"]}, self.publisher)
        self.assertEqual(denied["rule"], "ROLE_FORBIDDEN")
        names = [item["name"] for item in self._rpc("tools/list", {}, token=self.publisher)["result"]["tools"]]
        self.assertIn("begin_submit", names)
        self.assertNotIn("approve", names)
        guide = self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }, token=self.publisher)["result"]["instructions"]
        self.assertIn("不要在还有错误时 begin_submit", guide)
        self.assertIn("这把 KEY 不能审核", guide)
        broken = self._tool("validate", {"manifest": skill_manifest(summary="")}, self.publisher)
        self.assertFalse(broken["ok"])
        self.assertTrue(broken["rules"])
        self.assertTrue(any(item["message"] and item["severity"] == "error" for item in broken["findings"]))
        clean = self._tool("validate", {"manifest": skill_manifest()}, self.publisher)
        self.assertTrue(clean["ok"])
        self.assertEqual(clean["rules"], [])
        admin_guide = self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        }, token=self.admin)["result"]["instructions"]
        self.assertIn("不改清单字段", admin_guide)

    def test_submit_package_is_fully_mcp_without_cli(self) -> None:
        result = self._tool("submit_package", {
            "manifest": skill_manifest(),
            "files": {"SKILL.md": base64.b64encode(SKILL.encode()).decode()},
        }, self.publisher)
        self.assertEqual(result["status"], "pending", result)
        names = [item["name"] for item in self._rpc("tools/list", {}, token=self.publisher)["result"]["tools"]]
        self.assertIn("submit_package", names)

    def test_spec_comes_from_the_store_not_a_skill_file(self) -> None:
        listed = self._rpc("resources/list", {}, token="")
        uris = [item["uri"] for item in listed["result"]["resources"]]
        self.assertIn("qilin://spec/knowledge", uris)
        self.assertIn("qilin://spec/upload", uris)
        read = self._rpc("resources/read", {"uri": "qilin://spec/knowledge"}, token="")
        body = read["result"]["contents"][0]["text"]
        spec = self._tool("get_spec", {"name": "knowledge"}, "")
        self.assertEqual(spec["text"], body)
        self.assertIn("read_notes", body)
        self.assertIn("篇名", body)
        self.assertNotIn("qilin-upload.md", body)
        upload = self._tool("get_spec", {"name": "upload"}, "")
        self.assertIn("get_spec", upload["text"])
        self.assertIn("QILIN_STORE_KEY", upload["text"])
        missing = self._tool("get_spec", {"name": "nope"}, "")
        self.assertEqual(missing["rule"], "SPEC_UNKNOWN")
        denied = self._rpc("resources/read", {"uri": "qilin://spec/nope"}, token="")
        self.assertEqual(denied["error"]["code"], -32002)

    def test_admin_publishes_and_resolve_reports_platform_gap(self) -> None:
        self._publish("1.0.0", ["windows-amd64"])
        self._publish("1.1.0", ["darwin-arm64"])
        missing = self._tool("install_plan", {
            "id": "luxi.gov-writer",
            "client": "reasonix",
            "platform": "windows-amd64",
        }, self.admin)
        self.assertEqual(missing["rule"], "VERSION_REQUIRED")
        chosen = self._tool("resolve", {
            "id": "luxi.gov-writer",
            "client": "reasonix",
            "platform": "windows-amd64",
        }, self.admin)
        self.assertEqual(chosen["selected"]["version"], "1.0.0")
        self.assertEqual(chosen["reason"], "highest_match_platform")
        self.assertEqual([item["version"] for item in chosen["candidates"]], ["1.0.0"])
        plan = self._tool("install_plan", {
            "id": "luxi.gov-writer",
            "version": "1.0.0",
            "client": "reasonix",
            "platform": "windows-amd64",
        }, self.admin)
        self.assertEqual([step["op"] for step in plan["steps"]], ["download", "unpack", "verify"])
        self.assertNotIn(self.admin, json.dumps(plan))
        with urllib.request.urlopen(self.base + "/v1/artifacts/luxi.gov-writer/1.0.0") as response:
            body = response.read()
            self.assertEqual(response.headers["X-Checksum-Sha256"], hashlib.sha256(body).hexdigest())

    def _publish(self, version: str, platforms: list[str]) -> None:
        manifest = skill_manifest(version=version, platforms=platforms, changes=_changes(version))
        archive = self._zip(version, manifest)
        started = self._tool("begin_submit", {
            "manifest": manifest,
            "byte_size": len(archive),
        }, self.publisher)
        self.assertEqual(started["status"], "uploading", started)
        self._put(started["put_url"], archive, self.publisher)
        finished = self._tool("finish_submit", {"submission_id": started["submission_id"]}, self.publisher)
        self.assertEqual(finished["status"], "pending", finished)
        approved = self._tool("approve", {"submission_id": started["submission_id"]}, self.admin)
        self.assertEqual(approved["status"], "published")

    def _zip(self, version: str = "3.2.0", manifest: dict | None = None) -> bytes:
        root = Path(self.tmp.name) / f"pkg-{version}-{id(manifest)}"
        root.mkdir()
        body = manifest or skill_manifest()
        (root / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
        text = SKILL if version == "3.2.0" else (
            f"---\nname: gov\ndescription: 六阶段公文流水线，按阶段挂起。\nversion: {version}\n---\n\n# 公文\n"
        )
        (root / "SKILL.md").write_text(text, encoding="utf-8")
        dest = root / "package.zip"
        write_zip(root, dest)
        # write_zip includes the zip if dest is inside root. Write beside it.
        return dest.read_bytes()

    def _tool(self, name: str, arguments: dict, token: str) -> dict:
        message = self._rpc("tools/call", {"name": name, "arguments": arguments}, token=token)
        text = json.loads(message["result"]["content"][0]["text"])
        if message["result"]["isError"]:
            return text
        return text

    def _rpc(self, method: str, params: dict, *, token: str) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        request = urllib.request.Request(
            self.base + "/mcp",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def _put(self, url: str, data: bytes, token: str) -> None:
        request = urllib.request.Request(
            url, data=data, method="PUT", headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)


def _raw_get(port: int) -> bytes:
    """读完整响应。Python 的 urllib 会放过没有冒号的头，Go 客户端不会。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(
            b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\nConnection: close\r\n\r\n"
        )
        chunks: list[bytes] = []
        while True:
            piece = sock.recv(4096)
            if not piece:
                break
            chunks.append(piece)
    finally:
        sock.close()
    data = b"".join(chunks)
    if b"Allow: POST" not in data or not data.startswith(b"HTTP/1.0 405"):
        raise AssertionError(data)
    return data


def _changes(version: str) -> str:
    if version == "3.2.0":
        return "首个发布"
    return f"发布 {version}，供选择平台。"


if __name__ == "__main__":
    unittest.main()
