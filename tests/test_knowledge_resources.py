"""商店 MCP 下发的协议资源。

协议不只是仓库里的文件：`resources/list` 要列出它们，`resources/read` 要能读到，
而且读到的 JSON 必须能被解析。这条测试防止「文件在、但 MCP 没发」。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from store.mcp_api import handle
from store.service import Store


class KnowledgeResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        message = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            message["params"] = params
        response = handle(self.store, "", message, base_url="http://127.0.0.1:8787")
        assert response is not None
        return response

    def test_protocol_resources_are_listed(self) -> None:
        response = self._rpc("resources/list")
        uris = {item["uri"] for item in response["result"]["resources"]}
        for expected in (
            "qilin://spec/knowledge",
            "qilin://schema/knowledge",
            "qilin://vocab/knowledge",
            "qilin://tags/knowledge",
            "qilin://metadata/knowledge",
            "qilin://checklist/knowledge",
            "qilin://example/knowledge",
        ):
            self.assertIn(expected, uris)

    def test_vocabulary_is_readable_as_json(self) -> None:
        response = self._rpc("resources/read", {"uri": "qilin://vocab/knowledge"})
        content = response["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "application/json")
        vocab = json.loads(content["text"])
        self.assertEqual(vocab["version"], "kv/1")
        self.assertGreaterEqual(len(vocab["categories"]), 16)

    def test_schema_resource_uses_schema_mime_type(self) -> None:
        response = self._rpc("resources/read", {"uri": "qilin://schema/knowledge"})
        content = response["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "application/schema+json")
        schema = json.loads(content["text"])
        self.assertIn("tags", schema["properties"])

    def test_checklist_is_generated_from_the_workflow(self) -> None:
        response = self._rpc("resources/read", {"uri": "qilin://checklist/knowledge"})
        content = response["result"]["contents"][0]
        self.assertEqual(content["mimeType"], "text/markdown")
        self.assertIn("定题与边界", content["text"])
        self.assertIn("- [ ]", content["text"])

    def test_example_pack_is_complete(self) -> None:
        response = self._rpc("resources/read", {"uri": "qilin://example/knowledge"})
        example = json.loads(response["result"]["contents"][0]["text"])
        self.assertEqual(example["manifest"]["kind"], "knowledge")
        self.assertEqual(example["manifest"]["knowledge"]["entry"], "00-导航概览.md")
        self.assertIn("00-导航概览.md", example["files"])
        self.assertIn("01-知识包是什么.md", example["files"])

    def test_unknown_resource_is_an_error(self) -> None:
        response = self._rpc("resources/read", {"uri": "qilin://vocab/nope"})
        self.assertEqual(response["error"]["code"], -32002)


if __name__ == "__main__":
    unittest.main()
