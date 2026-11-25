"""semver 的唯一实现：预发布排序与「=」匹配的回归用例。"""

from __future__ import annotations

import unittest

from store.plan import resolve
from store.semver import cmp, is_valid, parse, satisfies
from tests.test_plan import _release


class SemverTests(unittest.TestCase):
    def test_prerelease_is_lower_than_release(self) -> None:
        self.assertLess(cmp("1.0.0-rc1", "1.0.0"), 0)
        self.assertGreater(cmp("1.0.0", "1.0.0-rc1"), 0)
        self.assertEqual(cmp("1.0.0", "1.0.0"), 0)

    def test_numeric_identifiers_compare_as_numbers(self) -> None:
        self.assertLess(cmp("1.0.0-rc.2", "1.0.0-rc.10"), 0)
        # 纯数字标识符低于非数字标识符。
        self.assertLess(cmp("1.0.0-1", "1.0.0-alpha"), 0)

    def test_mixed_identifier_does_not_raise(self) -> None:
        # 旧实现在这里会抛 ValueError。
        self.assertEqual(cmp("1.0.0-1a", "1.0.0-1a"), 0)
        self.assertLess(cmp("1.0.0-1a", "1.0.0"), 0)

    def test_invalid_version_is_sorted_last_not_raised(self) -> None:
        self.assertIsNone(parse("3.2.0 (Agent-Driven)"))
        self.assertFalse(is_valid("3.2.0 (Agent-Driven)"))
        self.assertLess(cmp("1.0.0", "bad"), 0)

    def test_equality_does_not_match_release_for_prerelease(self) -> None:
        # 旧实现 `_cmp` 剥掉预发布，导致 =1.0.0-rc1 命中 1.0.0。
        self.assertFalse(satisfies("1.0.0", "=1.0.0-rc1"))
        self.assertTrue(satisfies("1.0.0-rc1", "=1.0.0-rc1"))
        self.assertFalse(satisfies("1.0.0-rc1", ">=1.0.0"))

    def test_resolve_pins_the_requested_prerelease(self) -> None:
        releases = [
            _release("luxi.gov-writer", "1.0.0", ["darwin-arm64"]),
            _release("luxi.gov-writer", "1.0.0-rc1", ["darwin-arm64"]),
        ]
        pinned = resolve(
            releases,
            package_id="luxi.gov-writer",
            client="reasonix",
            platform="darwin-arm64",
            version="=1.0.0-rc1",
        )
        self.assertIsNotNone(pinned["selected"], pinned)
        self.assertEqual(pinned["selected"]["version"], "1.0.0-rc1")

    def test_prerelease_only_enters_when_asked(self) -> None:
        # 默认不把预发布算进候选；显式 allow_prerelease 才纳入。
        # 按 semver 2.0.0，1.0.0-rc1 < 1.0.0，所以 <1.0.0 在纳入预发布时包含它。
        releases = [
            _release("luxi.gov-writer", "0.9.0", ["darwin-arm64"]),
            _release("luxi.gov-writer", "1.0.0-rc1", ["darwin-arm64"]),
        ]
        quiet = resolve(
            releases,
            package_id="luxi.gov-writer",
            client="reasonix",
            platform="darwin-arm64",
            version="<1.0.0",
        )
        self.assertEqual(quiet["selected"]["version"], "0.9.0")
        loud = resolve(
            releases,
            package_id="luxi.gov-writer",
            client="reasonix",
            platform="darwin-arm64",
            version="<1.0.0",
            allow_prerelease=True,
        )
        self.assertEqual(loud["selected"]["version"], "1.0.0-rc1")


if __name__ == "__main__":
    unittest.main()
