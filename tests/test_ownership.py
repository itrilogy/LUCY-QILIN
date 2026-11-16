"""集市身份：同一 id 归首个发布者，admin 也不代发。

登记表退场后，商品身份完全由清单里的 id 与已发布记录决定。这些用例覆盖
提交时拒绝、审核时再校验、并发抢同一个新 id 三种情况。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from store.errors import StoreError
from store.service import Store
from tests.test_service import SUMMARY, skill_manifest


def _skill(version: str) -> str:
    return f"---\nname: gov\ndescription: {SUMMARY}\nversion: {version}\n---\n\n# 公文\n"


class OwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        self.admin_profile, _session, self.admin = self.store.register_account(
            "root.admin", "管理员", "管理员", "IT", "管理员", "Passw0rdXyz",
        )
        self.alice_profile, _session, self.alice = self.store.register_account(
            "author.alice", "作者甲", "作者甲", "", "", "Passw0rdXyz",
        )
        self.bob_profile, _session, self.bob = self.store.register_account(
            "author.bob", "作者乙", "作者乙", "", "", "Passw0rdXyz",
        )

    def _submit(self, token: str, package_id: str, version: str = "1.0.0"):
        return self.store.submit(
            token,
            skill_manifest(id=package_id, version=version),
            {"SKILL.md": _skill(version).encode()},
        )

    def _publish(self, token: str, package_id: str, version: str = "1.0.0") -> None:
        result = self._submit(token, package_id, version)
        self.assertEqual(result.status, "pending", result.rules)
        self.store.approve(self.admin, result.submission_id)

    def test_new_id_is_open_to_everyone(self) -> None:
        result = self._submit(self.alice, "luxi.alice-own")
        self.assertEqual(result.status, "pending")
        self.store.approve(self.admin, result.submission_id)

    def test_other_publisher_cannot_submit_a_new_version(self) -> None:
        self._publish(self.alice, "luxi.owned")
        with self.assertRaises(StoreError) as caught:
            self._submit(self.bob, "luxi.owned", "1.1.0")
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_admin_cannot_submit_for_someone_else(self) -> None:
        self._publish(self.alice, "luxi.owned")
        with self.assertRaises(StoreError) as caught:
            self._submit(self.admin, "luxi.owned", "2.0.0")
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_owner_can_publish_the_next_version(self) -> None:
        self._publish(self.alice, "luxi.owned", "1.0.0")
        self._publish(self.alice, "luxi.owned", "1.1.0")
        versions = self.store.list_versions("luxi.owned")
        self.assertEqual([item["version"] for item in versions], ["1.0.0", "1.1.0"])

    def test_second_approval_of_a_raced_id_is_refused(self) -> None:
        first = self._submit(self.alice, "luxi.race")
        second = self._submit(self.bob, "luxi.race")
        self.assertEqual(first.status, "pending")
        self.assertEqual(second.status, "pending")
        self.store.approve(self.admin, first.submission_id)
        with self.assertRaises(StoreError) as caught:
            self.store.approve(self.admin, second.submission_id)
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_namespace_belongs_to_the_first_publisher(self) -> None:
        self._publish(self.alice, "luxi.alpha")
        with self.assertRaises(StoreError) as caught:
            self._submit(self.bob, "luxi.beta")
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_owner_can_add_another_id_in_the_namespace(self) -> None:
        self._publish(self.alice, "luxi.alpha")
        result = self._submit(self.alice, "luxi.beta")
        self.assertEqual(result.status, "pending")

    def test_other_namespace_stays_open(self) -> None:
        self._publish(self.alice, "luxi.alpha")
        result = self._submit(self.bob, "lab.beta")
        self.assertEqual(result.status, "pending")

    def test_namespace_race_is_settled_by_first_publish(self) -> None:
        first = self._submit(self.alice, "race.alpha")
        second = self._submit(self.bob, "race.beta")
        self.store.approve(self.admin, first.submission_id)
        with self.assertRaises(StoreError):
            self.store.approve(self.admin, second.submission_id)

    def test_admin_cannot_claim_someone_elses_namespace(self) -> None:
        self._publish(self.alice, "luxi.alpha")
        with self.assertRaises(StoreError) as caught:
            self._submit(self.admin, "luxi.gamma")
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_legacy_release_without_submission_is_treated_as_unowned(self) -> None:
        # 旧库可能只有 releases，没有已发布提交与所有者记录。按无主处理，不挡新包。
        self._publish(self.alice, "luxi.owned")
        self.store._db.execute("DELETE FROM package_owners")
        self.store._db.execute("DELETE FROM submissions")
        self.store._db.commit()
        result = self._submit(self.bob, "luxi.owned", "1.1.0")
        self.assertEqual(result.status, "pending")

    def test_admin_can_transfer_the_namespace(self) -> None:
        self._publish(self.alice, "luxi.alpha")
        self.store.transfer_namespace(self.admin, "luxi", self.bob_profile["id"])
        result = self._submit(self.bob, "luxi.beta")
        self.assertEqual(result.status, "pending")
        with self.assertRaises(StoreError):
            self._submit(self.alice, "luxi.gamma")

    def test_non_admin_cannot_transfer(self) -> None:
        self._publish(self.alice, "luxi.alpha")
        with self.assertRaises(StoreError) as caught:
            self.store.transfer_namespace(self.alice, "luxi", self.bob_profile["id"])
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_transfer_to_disabled_account_is_refused(self) -> None:
        self._publish(self.alice, "luxi.alpha")
        self.store.disable_account(self.admin, self.bob_profile["id"])
        with self.assertRaises(StoreError) as caught:
            self.store.transfer_namespace(self.admin, "luxi", self.bob_profile["id"])
        self.assertEqual(caught.exception.rule, "ACCOUNT_DISABLED")

    def test_transfer_unknown_namespace_is_refused(self) -> None:
        with self.assertRaises(StoreError):
            self.store.transfer_namespace(self.admin, "nosuch", self.bob_profile["id"])


if __name__ == "__main__":
    unittest.main()
