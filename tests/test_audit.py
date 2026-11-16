"""审计读取：admin 能查，作者不能；过滤与分页可用。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.errors import StoreError
from store.http_api import serve
from store.service import Store
from tests.test_service import SKILL, skill_manifest


class AuditReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        self.publisher_principal, self.publisher = self.store.create_principal("作者", "publisher")
        self.admin_principal, self.admin = self.store.create_principal("管理员", "admin")

    def _submit(self, package_id: str = "luxi.audit-a"):
        return self.store.submit(
            self.publisher,
            skill_manifest(id=package_id),
            {"SKILL.md": SKILL.encode()},
        )

    def test_admin_can_read_the_audit_trail(self) -> None:
        first = self._submit("luxi.audit-a")
        self.store.approve(self.admin, first.submission_id)
        second = self._submit("luxi.audit-b")
        self.store.reject(self.admin, second.submission_id, "再具体一点")
        page = self.store.list_audit(self.admin)
        actions = {item["action"] for item in page["items"]}
        self.assertIn("submit", actions)
        self.assertIn("approve", actions)
        self.assertIn("reject", actions)
        self.assertGreaterEqual(page["total"], 3)
        self.assertTrue(all(item["actor_name"] for item in page["items"]))
        self.assertEqual(page["limit"], 100)
        self.assertEqual(page["offset"], 0)

    def test_publisher_is_forbidden(self) -> None:
        with self.assertRaises(StoreError) as caught:
            self.store.list_audit(self.publisher)
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_filter_by_action_and_package(self) -> None:
        self._submit()
        page = self.store.list_audit(self.admin, action="submit")
        self.assertTrue(page["items"])
        self.assertTrue(all(item["action"] == "submit" for item in page["items"]))
        page = self.store.list_audit(self.admin, package_id="luxi.audit-a")
        self.assertTrue(page["items"])
        self.assertTrue(all(item["package_id"] == "luxi.audit-a" for item in page["items"]))
        self.assertEqual(self.store.list_audit(self.admin, action="nope")["total"], 0)

    def test_pagination_and_cap(self) -> None:
        for index in range(3):
            self._submit(f"luxi.audit-{index}")
        first = self.store.list_audit(self.admin, limit=1, offset=0)
        second = self.store.list_audit(self.admin, limit=1, offset=1)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        capped = self.store.list_audit(self.admin, limit=9999)
        self.assertEqual(capped["limit"], 500)


class AuditHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _p, self.publisher = self.store.create_principal("作者", "publisher")
        _a, self.admin = self.store.create_principal("管理员", "admin")
        self.server = serve(self.store)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _get(self, token: str, query: str = ""):
        request = urllib.request.Request(self.base + "/v1/audit" + query)
        request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def test_admin_reads_and_publisher_is_denied(self) -> None:
        self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        page = self._get(self.admin, "?limit=1&action=submit")
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["action"], "submit")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get(self.publisher)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
