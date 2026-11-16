"""KEY 认证缓存：省掉重复 scrypt，但账号一变就必须失效。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from store.errors import StoreError
from store.service import Store

PASSWORD = "Passw0rdXyz"


class AuthCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        # 第一位注册的账号是管理员，第二位才是作者。
        _admin_profile, _session, self.admin = self.store.register_account(
            "root.admin", "管理员", "管理员", "IT", "管理员", PASSWORD,
        )
        self.profile, _session, self.key = self.store.register_account(
            "author.one", "顾木", "木青", "产品", "作者", PASSWORD,
        )

    def test_cache_avoids_a_second_scrypt(self) -> None:
        first = self.store.principal_from_token(self.key)
        self.assertEqual(first.id, self.profile["id"])
        with mock.patch("store.accounts.key_matches", side_effect=AssertionError("不应再做 scrypt")):
            second = self.store.principal_from_token(self.key)
        self.assertEqual(second.id, self.profile["id"])

    def test_cache_expires(self) -> None:
        self.store.principal_from_token(self.key)
        with mock.patch("store.base._KEY_CACHE_TTL", -1):
            self.store.principal_from_token(self.key)
        # 上面那次命中旧缓存（TTL 在写入时已定），清掉后重存一条已过期的。
        self.store._auth_cache.clear()
        with mock.patch("store.base._KEY_CACHE_TTL", -1):
            self.store.principal_from_token(self.key)
        with mock.patch("store.accounts.key_matches", side_effect=AssertionError("过期后应重新比对")):
            with self.assertRaises(AssertionError):
                self.store.principal_from_token(self.key)

    def test_disable_invalidates_the_cache(self) -> None:
        self.store.principal_from_token(self.key)
        self.store.disable_account(self.admin, self.profile["id"])
        with self.assertRaises(StoreError) as caught:
            self.store.principal_from_token(self.key)
        self.assertEqual(caught.exception.rule, "ACCOUNT_DISABLED")

    def test_role_change_invalidates_the_cache(self) -> None:
        self.assertEqual(self.store.principal_from_token(self.key).role, "publisher")
        self.store.set_account_role(self.admin, self.profile["id"], "admin")
        self.assertEqual(self.store.principal_from_token(self.key).role, "admin")

    def test_reissued_key_invalidates_the_old_one(self) -> None:
        self.store.principal_from_token(self.key)
        _profile, _session, new_key = self.store.issue_account_key(self.key)
        self.assertIsNone(self.store.principal_from_token(self.key))
        self.assertIsNotNone(self.store.principal_from_token(new_key))


if __name__ == "__main__":
    unittest.main()
