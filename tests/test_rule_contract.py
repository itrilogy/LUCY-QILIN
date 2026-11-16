"""规则 id 与规格正文的契约。

规格第 12 节是唯一的事实源。这里把它读出来，与 `store.errors.SPEC_RULES`
双向比较：规格加了没写进代码、代码加了没写进规格，都会失败。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from store.errors import EXTRA_RULES, KNOWN_RULES, SPEC_RULES, http_status, is_auth_rule

ROOT = Path(__file__).resolve().parents[1]


def _spec_rules() -> set[str]:
    text = (ROOT / "agent-store-design.md").read_text(encoding="utf-8")
    section = text.split("## 12. 规则 id 一览", 1)[1].split("## 13.", 1)[0]
    return set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", section))


class RuleContractTests(unittest.TestCase):
    def test_spec_section_12_matches_the_embedded_table(self) -> None:
        self.assertEqual(_spec_rules(), set(SPEC_RULES))

    def test_known_rules_are_spec_plus_documented_extras(self) -> None:
        self.assertEqual(set(KNOWN_RULES), set(SPEC_RULES) | set(EXTRA_RULES))

    def test_extras_are_the_two_documented_codes(self) -> None:
        # 加新码时先改规格第 12 节，再改这里；不要让「临时码」悄悄变多。
        self.assertEqual(set(EXTRA_RULES), {"INTERNAL", "STATE_UNBOUND"})

    def test_http_status_defaults_to_400_and_role_is_403(self) -> None:
        self.assertEqual(http_status("ROLE_FORBIDDEN"), 403)
        self.assertEqual(http_status("IDENTITY_INVALID"), 401)
        self.assertEqual(http_status("IDENTITY_UNCONFIGURED"), 401)
        self.assertEqual(http_status("VERSION_UNAVAILABLE"), 400)
        self.assertEqual(http_status("没有这个规则"), 400)

    def test_auth_rules_are_logged_at_warning(self) -> None:
        for rule in ("ROLE_FORBIDDEN", "IDENTITY_INVALID", "KEY_INVALID", "ACCOUNT_DISABLED"):
            self.assertTrue(is_auth_rule(rule), rule)
        self.assertFalse(is_auth_rule("VERSION_UNAVAILABLE"))


if __name__ == "__main__":
    unittest.main()
