"""贡献分析：账号快照、发布排行/明细、公开视图不泄露账号字段。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.http_api import serve
from store.mcp_api import handle
from store.service import Store
from tests.test_service import bootstrap_admin
from tests.test_service import SKILL, skill_manifest

PASSWORD = "Passw0rdXyz"


class ContributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _admin_profile, self.admin_key = bootstrap_admin(self.store, "root")
        _p, _s, self.publisher_key = self.store.register_account(
            "author", "顾木", "木青", "产品部", "作者", PASSWORD,
        )


    def _publish(self, package_id: str, version: str):
        result = self.store.submit(
            self.publisher_key,
            skill_manifest(id=package_id, version=version),
            {"SKILL.md": SKILL.replace("version: 3.2.0", f"version: {version}").encode()},
        )
        self.assertEqual(result.status, "pending", result.rules)
        self.store.approve(self.admin_key, result.submission_id)
        return result

    def test_snapshot_and_public_author(self) -> None:
        result = self._publish("lab.author", "1.0.0")
        manifest = self.store.get_review(self.admin_key, result.submission_id)["manifest"]
        self.assertEqual(manifest["author"]["username"], "author")
        self.assertEqual(manifest["author"]["real_name"], "顾木")
        self.assertEqual(manifest["author"]["nickname"], "木青")
        self.assertEqual(manifest["author"]["department"], "产品部")
        public = self.store.describe("lab.author", "1.0.0")
        self.assertEqual(public["author"], {"display_name": "鹿溪"})

    def test_ranking_and_detail(self) -> None:
        self._publish("lab.author", "1.0.0")
        self._publish("lab.author", "1.1.0")
        ranking = self.store.contributions(self.admin_key)
        self.assertEqual(ranking["total"], 1)
        row = ranking["items"][0]
        self.assertEqual(row["username"], "author")
        self.assertEqual(row["real_name"], "顾木")
        self.assertEqual(row["department"], "产品部")
        self.assertEqual(row["product_count"], 1)
        self.assertEqual(row["versions"], 2)
        detail = self.store.contributions(self.admin_key, account_id=row["account_id"])
        self.assertEqual(detail["total"], 2)
        self.assertEqual({item["version"] for item in detail["items"]}, {"1.0.0", "1.1.0"})

    def test_publisher_cannot_read_contributions(self) -> None:
        with self.assertRaises(Exception) as caught:
            self.store.contributions(self.publisher_key)
        self.assertEqual(getattr(caught.exception, "rule", ""), "ROLE_FORBIDDEN")

    def test_http_and_mcp(self) -> None:
        self._publish("lab.author", "1.0.0")
        server = serve(self.store)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_port}"
        request = urllib.request.Request(base + "/v1/contributions")
        request.add_header("Authorization", "Bearer " + self.admin_key)
        with urllib.request.urlopen(request) as response:
            data = json.load(response)
        self.assertEqual(data["items"][0]["username"], "author")
        bad = urllib.request.Request(base + "/v1/contributions")
        bad.add_header("Authorization", "Bearer " + self.publisher_key)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(bad)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        response = handle(
            self.store, self.admin_key,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "list_contributions", "arguments": {}}},
            base_url=base,
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["items"][0]["username"], "author")


if __name__ == "__main__":
    unittest.main()
