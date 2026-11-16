"""KEY 生成：查重按指纹，不做全表 scrypt。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from store.errors import StoreError
from store.service import Store

PASSWORD = "Passw0rdXyz"


class KeyGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)

    def test_registration_does_not_compare_every_key(self) -> None:
        self.store.register_account("user.one", "顾木", "木青", "", "", PASSWORD)
        with mock.patch("store.accounts.key_matches", side_effect=AssertionError("不应扫全表")):
            self.store.register_account("user.two", "顾木", "木青", "", "", PASSWORD)

    def test_prefix_collision_retries_then_gives_up(self) -> None:
        with mock.patch("store.accounts.key_prefix", return_value="0123456789abcdef"), \
             mock.patch("store.accounts.issue_key", return_value="x" * 43):
            self.store.register_account("user.one", "顾木", "木青", "", "", PASSWORD)
            with self.assertRaises(StoreError) as caught:
                self.store.register_account("user.two", "顾木", "木青", "", "", PASSWORD)
        self.assertEqual(caught.exception.rule, "KEY_TAKEN")

    def test_reissuing_ignores_the_same_account(self) -> None:
        profile, _session, _old_key = self.store.register_account(
            "user.one", "顾木", "木青", "", "", PASSWORD,
        )
        with mock.patch("store.accounts.key_prefix", return_value="0123456789abcdef"):
            _key, _salt, _hashed, fingerprint = self.store._fresh_key(keep=profile["id"])
        self.assertEqual(fingerprint, "0123456789abcdef")

    def test_keys_are_unique_across_many_accounts(self) -> None:
        keys = set()
        for index in range(8):
            _profile, _session, key = self.store.register_account(
                f"user.{index}", "顾木", "木青", "", "", PASSWORD,
            )
            keys.add(key)
        self.assertEqual(len(keys), 8)


if __name__ == "__main__":
    unittest.main()
