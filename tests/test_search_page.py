"""货架分页：稳定排序、总数、limit/offset，HTTP 与 MCP 同源。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from store.http_api import serve
from store.mcp_api import handle
from store.service import Store
from tests.test_service import SKILL, skill_manifest


class SearchPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")
        for index in range(5):
            result = self.store.submit(
                self.publisher,
                skill_manifest(id=f"luxi.pkg-{index}"),
                {"SKILL.md": SKILL.encode()},
            )
            self.store.approve(self.admin, result.submission_id)

    def test_page_has_total_and_stable_order(self) -> None:
        first = self.store.search_page(limit=2, sort="id")
        self.assertEqual(first["total"], 5)
        self.assertEqual(first["limit"], 2)
        self.assertEqual(len(first["items"]), 2)
        ids = [item["id"] for item in first["items"]]
        self.assertEqual(ids, sorted(ids))
        second = self.store.search_page(limit=2, offset=2, sort="id")
        self.assertEqual(len(second["items"]), 2)
        self.assertFalse(set(ids) & {item["id"] for item in second["items"]})
        tail = self.store.search_page(limit=2, offset=4, sort="id")
        self.assertEqual(len(tail["items"]), 1)

    def test_sort_by_title(self) -> None:
        page = self.store.search_page(sort="title", limit=5)
        self.assertEqual(page["total"], 5)
        self.assertEqual([item["id"] for item in page["items"]], sorted(item["id"] for item in page["items"]))

    def test_search_list_stays_compatible(self) -> None:
        self.assertEqual(len(self.store.search()), 5)
        self.assertEqual(len(self.store.search(limit=3)), 3)

    def test_http_catalog_carries_the_page(self) -> None:
        server = serve(self.store)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/v1/catalog?limit=2&offset=2&sort=id") as response:
            page = json.load(response)
        self.assertEqual(page["total"], 5)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["offset"], 2)

    def test_mcp_search_returns_the_same_page_shape(self) -> None:
        response = handle(
            self.store, "",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "search", "arguments": {"limit": 1, "offset": 1, "sort": "id"}}},
            base_url="http://127.0.0.1:8787",
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["total"], 5)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["offset"], 1)


if __name__ == "__main__":
    unittest.main()
