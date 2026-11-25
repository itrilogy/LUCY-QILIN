"""角色边界：作者 / 审核 / 管理员 / 只读。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from store.errors import StoreError
from store.mcp_api import _visible
from store.service import Store
from tests.test_service import SKILL, bootstrap_admin, skill_manifest


class RoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        self.admin_principal, self.admin = self.store.create_principal("管理员", "admin")
        self.reviewer_principal, self.reviewer = self.store.create_principal("审核", "reviewer")
        self.publisher_principal, self.publisher = self.store.create_principal("作者", "publisher")

    def _submit(self, token, package_id="lab.role"):
        result = self.store.submit(token, skill_manifest(id=package_id), {"SKILL.md": SKILL.encode()})
        self.assertEqual(result.status, "pending", result.rules)
        return result

    def test_reviewer_can_review_but_not_manage_users(self) -> None:
        result = self._submit(self.publisher)
        self.store.approve(self.reviewer, result.submission_id)
        with self.assertRaises(StoreError) as caught:
            self.store.list_accounts(self.reviewer)
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")
        with self.assertRaises(StoreError) as caught:
            self.store.transfer_namespace(self.reviewer, "lab", "u_x")
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")
        self.assertTrue(self.store.list_audit(self.reviewer)["items"])
        self.assertEqual(self.store.contributions(self.reviewer)["total"], 1)

    def test_reviewer_can_unpublish(self) -> None:
        result = self._submit(self.publisher)
        self.store.approve(self.reviewer, result.submission_id)
        self.store.unpublish(self.reviewer, "lab.role", "3.2.0", "审核下架")
        self.assertEqual(self.store.search_page("lab.role")["total"], 0)

    def test_publisher_cannot_review(self) -> None:
        result = self._submit(self.publisher)
        with self.assertRaises(StoreError) as caught:
            self.store.approve(self.publisher, result.submission_id)
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")
        with self.assertRaises(StoreError) as caught:
            self.store.list_reviews(self.publisher)
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_reviewer_can_author_but_not_self_review(self) -> None:
        result = self._submit(self.reviewer, "lab.reviewer-work")
        with self.assertRaises(StoreError) as caught:
            self.store.approve(self.reviewer, result.submission_id)
        self.assertEqual(caught.exception.rule, "SELF_REVIEW")
        self.store.approve(self.admin, result.submission_id)

    def test_mcp_tool_visibility(self) -> None:
        reviewer_tools = _visible("reviewer")
        self.assertIn("approve", reviewer_tools)
        self.assertIn("begin_submit", reviewer_tools)
        self.assertNotIn("transfer_namespace", reviewer_tools)
        admin_tools = _visible("admin")
        self.assertIn("transfer_namespace", admin_tools)
        self.assertNotIn("approve", _visible("publisher"))

    def test_admin_can_assign_reviewer(self) -> None:
        _admin_profile, admin_key = bootstrap_admin(self.store, "account-admin")
        profile, _s, publisher_key = self.store.register_account("account-author", "作者", "作者", "产品", "作者", "Passw0rdXyz")
        self.store.set_account_role(admin_key, profile["id"], "reviewer")
        self.assertEqual(self.store.whoami(publisher_key)["role"], "reviewer")


if __name__ == "__main__":
    unittest.main()
