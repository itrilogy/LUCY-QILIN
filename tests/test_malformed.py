"""畸形清单的回归用例：必须得到 finding 或 4xx，不能断开连接。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.http_api import serve
from store.service import Store, StoreError
from store.validator.validate import Report, validate_manifest
from tests.test_service import SKILL, skill_manifest


class MalformedManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _post(self, path: str, payload: object) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.publisher}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            body = json.load(exc)
            exc.close()
            return exc.code, body

    def test_malformed_fields_yield_findings_not_exceptions(self) -> None:
        cases = {
            "kind 是数组": {"kind": []},
            "kind 是对象": {"kind": {}},
            "platforms 项是对象": {"platforms": [{}]},
            "clients 项是对象": {"clients": [{}]},
            "author 是字符串": {"author": "x"},
            "author 是数组": {"author": []},
        }
        for name, override in cases.items():
            with self.subTest(case=name):
                body = skill_manifest(**override)
                # 应当是「得到报告」，不能抛异常。
                report = validate_manifest(body)
                self.assertTrue(report.findings, name)

    def test_non_object_manifest_is_rejected_cleanly(self) -> None:
        for payload in ([], 1, None, "x"):
            with self.subTest(payload=payload):
                status, body = self._post("/v1/submissions", {"manifest": payload, "files": {}})
                self.assertIn(status, {400, 403})
                self.assertIn("rule", body)

    def test_author_string_is_rejected_before_storing(self) -> None:
        body = skill_manifest()
        body["author"] = "鹿溪"
        with self.assertRaises(StoreError) as caught:
            self.store.submit(self.publisher, body, {"SKILL.md": SKILL.encode()})
        self.assertEqual(caught.exception.rule, "AUTHOR_MISMATCH")

    def test_non_object_body_is_rejected(self) -> None:
        for payload in ([], 1, "x"):
            with self.subTest(payload=payload):
                status, body = self._post("/v1/validate", payload)
                self.assertEqual(status, 400)
                self.assertIn("rule", body)

    def test_connection_survives_bad_request(self) -> None:
        # 先打一条畸形请求，再打一条正常请求，确认服务还活着。
        self._post("/v1/submissions", {"manifest": [], "files": {}})
        status, body = self._post("/v1/validate", {"manifest": skill_manifest()})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["rules"], [], body)


class GuardTests(unittest.TestCase):
    def test_matching_type_is_returned_unchanged(self) -> None:
        report = Report()
        value = ["any"]
        self.assertIs(report.guard(value, list, "KIND", "kind", "不应记下", ""), value)
        self.assertEqual(report.findings, [])

    def test_wrong_type_records_one_finding_and_returns_default(self) -> None:
        report = Report()
        self.assertEqual(report.guard({}, str, "KIND", "kind", "必须是字符串", ""), "")
        self.assertEqual([(item.rule, item.path) for item in report.findings], [("KIND", "kind")])
        self.assertFalse(report.ok)

    def test_wrong_shapes_stay_inside_the_report(self) -> None:
        cases = {
            "platforms 不是数组": {"platforms": {}},
            "clients 不是数组": {"clients": "cursor"},
            "depends 不是对象": {"depends": []},
            "depends 的项不是数组": {"depends": {"store": {}, "external": [], "skills": [], "mcp": []}},
            "secrets 不是数组": {"secrets": {}},
            "secrets 项不是对象": {"secrets": ["x"]},
            "mcp 不是对象": {
                "kind": "mcp",
                "mcp": [],
                "clients": ["cursor"],
                "platforms": ["any"],
                "invoke": {"form": "list_tools"},
            },
            "platform_files 不是数组": {"skill": {"entry": "SKILL.md", "platform_files": "x"}},
            "runtime 资产不是对象": {
                "kind": "runtime",
                "platforms": ["darwin-arm64"],
                "clients": ["reasonix"],
                "runtime": {"bin_name": "officecli", "assets": ["x"]},
                "invoke": {"form": "exec_bin", "bin_name": "officecli"},
            },
            "taxonomy 不是对象": {
                "kind": "knowledge",
                "platforms": ["any"],
                "knowledge": {"entry": "note.md", "taxonomy": []},
                "invoke": {"form": "read_notes", "entry": "note.md"},
                "depends": {"store": [], "external": [], "skills": [], "mcp": []},
                "secrets": [],
            },
        }
        for name, override in cases.items():
            with self.subTest(case=name):
                report = validate_manifest(skill_manifest(**override))
                self.assertTrue(report.findings, name)


if __name__ == "__main__":
    unittest.main()
