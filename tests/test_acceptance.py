"""端到端验收：技能包与知识包从目录 → pending → 发布 → 货架 → 安装计划 → 干跑。

这是一条产品链路的冒烟用例，不依赖真实技能仓库，也不在商店服务器上执行安装。
"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

from store.http_api import serve
from store.installer import run as run_installer
from store.mcp_api import handle
from store.service import Store
from store.submit_client import submit_directory
from tests.test_service import SKILL, skill_manifest

PASSWORD = "Passw0rdXyz"


class AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        _profile, _session, self.admin_key = self.store.register_account(
            "root.admin", "管理员", "管理员", "IT", "管理员", PASSWORD,
        )
        _profile, _session, self.publisher_key = self.store.register_account(
            "author.one", "顾木", "木青", "产品", "作者", PASSWORD,
        )
        self.server = serve(self.store)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response)

    def _post(self, path: str, body: dict, *, key: str | None = None) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        if key:
            request.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def _approve(self, submission_id: str) -> None:
        result = self._post(
            f"/v1/submissions/{submission_id}/approve", {}, key=self.admin_key
        )
        self.assertEqual(result["status"], "published")

    def test_skill_pack_end_to_end(self) -> None:
        directory = self.root / "skill"
        directory.mkdir()
        (directory / "manifest.json").write_text(
            json.dumps(skill_manifest(), ensure_ascii=False), encoding="utf-8"
        )
        (directory / "SKILL.md").write_text(SKILL, encoding="utf-8")

        code, payload = submit_directory(directory, base=self.base, key=self.publisher_key)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "pending")
        self.assertNotIn(self.publisher_key, json.dumps(payload))
        self._approve(payload["submission_id"])

        catalog = self._get("/v1/catalog?query=gov")
        self.assertIn("luxi.gov-writer", [item["id"] for item in catalog["items"]])
        described = self._get("/v1/packages/luxi.gov-writer/3.2.0")
        self.assertEqual(described["kind"], "skill")
        self.assertEqual(described["author"]["display_name"], "鹿溪")

        plan = self._post(
            "/v1/install-plan",
            {"id": "luxi.gov-writer", "version": "3.2.0", "client": "reasonix", "platform": "darwin-arm64"},
        )
        ops = [step["op"] for step in plan["steps"]]
        self.assertEqual(ops, ["download", "unpack", "verify"])
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_installer(
                plan,
                dry_run=True,
                work=self.root / "work",
                skills_root=self.root / "skills",
            )
        self.assertEqual(code, 0)
        self.assertIn("download", out.getvalue())

    def test_knowledge_pack_end_to_end(self) -> None:
        example = json.loads(
            (Path(__file__).resolve().parents[1] / "store" / "spec" / "knowledge-example.json").read_text(
                encoding="utf-8"
            )
        )
        directory = self.root / "knowledge"
        directory.mkdir()
        (directory / "manifest.json").write_text(
            json.dumps(example["manifest"], ensure_ascii=False), encoding="utf-8"
        )
        for name, content in example["files"].items():
            (directory / name).write_text(content, encoding="utf-8")

        code, payload = submit_directory(directory, base=self.base, key=self.publisher_key)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["status"], "pending")
        self._approve(payload["submission_id"])

        catalog = self._get("/v1/catalog?kind=knowledge")
        self.assertIn("luxi.knowledge-example", [item["id"] for item in catalog["items"]])
        described = self._get("/v1/packages/luxi.knowledge-example/1.0.0")
        self.assertEqual(described["kind"], "knowledge")
        self.assertEqual(described["knowledge"]["taxonomy"]["business_category"], "knowledge-ops")

        plan = self._post(
            "/v1/install-plan",
            {
                "id": "luxi.knowledge-example",
                "version": "1.0.0",
                "client": "reasonix",
                "platform": "darwin-arm64",
            },
        )
        self.assertEqual([step["op"] for step in plan["steps"]], ["download", "unpack", "verify"])
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_installer(
                plan,
                dry_run=True,
                work=self.root / "work",
                knowledge_root=self.root / "knowledge-root",
            )
        self.assertEqual(code, 0)
        self.assertIn("unpack", out.getvalue())

    def test_store_mcp_serves_the_protocol(self) -> None:
        init = handle(
            self.store, "",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            base_url=self.base,
        )
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        listed = handle(
            self.store, "", {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
            base_url=self.base,
        )
        uris = {item["uri"] for item in listed["result"]["resources"]}
        self.assertIn("qilin://spec/knowledge", uris)
        self.assertIn("qilin://vocab/knowledge", uris)
        read = handle(
            self.store, "",
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read",
             "params": {"uri": "qilin://vocab/knowledge"}},
            base_url=self.base,
        )
        vocab = json.loads(read["result"]["contents"][0]["text"])
        self.assertEqual(vocab["version"], "kv/1")


if __name__ == "__main__":
    unittest.main()
