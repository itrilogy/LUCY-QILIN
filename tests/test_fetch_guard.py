"""外部地址抓取守卫（评审 P1-3）。

商店会在上传完成时替提交者抓一次清单里的地址，这是服务端出的网。
以前只要求 https，不拦内网、不管重定向、不限数量和时长：
- 可以拿它当内网端口探针（结果通过"打不开"回给提交者）；
- 可以对内网服务触发 GET 副作用；
- 一份清单能把一个 handler 线程按住几百秒。

现在：默认只允许公网（可配置），每跳重定向重新校验且不许 https→http，
单份清单最多探 20 个地址、总共 45 秒预算。
"""

from __future__ import annotations

import unittest
from unittest import mock

from store.validator.validate import validate_manifest
from tests.test_service import skill_manifest


def _manifest_with_urls(urls: list[str]) -> dict:
    return skill_manifest(
        depends={
            "store": [],
            "skills": [],
            "mcp": [],
            "external": [
                {
                    "name": "python",
                    "version_range": ">=3.11",
                    "detect": ["python3", "--version"],
                    "install_guide": "安装 Python 3.11 或以上版本，安装后确认 python3 可用。",
                    "resources": [
                        {"platform": "any", "url": url, "sha256": "ab" * 32} for url in urls
                    ],
                }
            ],
        },
    )


class UrlAllowedTests(unittest.TestCase):
    def test_loopback_private_and_metadata_are_blocked_by_default(self) -> None:
        from store.validator.net import url_allowed

        for url in (
            "https://127.0.0.1/x",
            "https://localhost/x",
            "https://10.0.0.5/x",
            "https://192.168.1.10/x",
            "https://172.16.3.4/x",
            "https://169.254.169.254/latest/meta-data/",
            "https://[::1]/x",
            "https://[fd00::1]/x",
            "http://0.0.0.0/x",
        ):
            with self.subTest(url=url):
                self.assertFalse(url_allowed(url), "默认策略不能访问非公网地址")

    def test_public_address_is_allowed(self) -> None:
        from store.validator.net import url_allowed

        with mock.patch.dict("os.environ", {"QILIN_FETCH_POLICY": "public"}, clear=False):
            self.assertTrue(url_allowed("https://93.184.216.34/x"))

    def test_policy_any_restores_old_behaviour(self) -> None:
        from store.validator.net import url_allowed

        with mock.patch.dict("os.environ", {"QILIN_FETCH_POLICY": "any"}):
            self.assertTrue(url_allowed("https://10.0.0.5/x"))

    def test_allowlist_policy_only_allows_listed_hosts(self) -> None:
        from store.validator.net import url_allowed

        with mock.patch.dict(
            "os.environ",
            {"QILIN_FETCH_POLICY": "allowlist", "QILIN_FETCH_ALLOW_HOSTS": "internal.corp"},
        ):
            self.assertTrue(url_allowed("https://internal.corp/pkg"))
            self.assertTrue(url_allowed("https://files.internal.corp/pkg"))
            self.assertFalse(url_allowed("https://example.com/pkg"))

    def test_allow_hosts_opens_a_hole_for_internal_resources(self) -> None:
        from store.validator.net import url_allowed

        with mock.patch.dict(
            "os.environ", {"QILIN_FETCH_POLICY": "public", "QILIN_FETCH_ALLOW_HOSTS": "corp.example"},
        ):
            self.assertTrue(url_allowed("https://dl.corp.example/x"))
            self.assertFalse(url_allowed("https://evilcorp.example/x"), "后缀匹配必须按点边界")

    def test_blocked_target_is_never_requested(self) -> None:
        from store.validator.net import probe_url

        with mock.patch("urllib.request.build_opener", side_effect=AssertionError("不应发起请求")):
            self.assertIsNone(probe_url("https://169.254.169.254/latest/meta-data/"))


class ProbeBudgetTests(unittest.TestCase):
    def test_at_most_twenty_urls_are_probed(self) -> None:
        calls: list[str] = []

        def probe(url: str) -> int:
            calls.append(url)
            return 200

        report = validate_manifest(
            _manifest_with_urls([f"https://host.example/pkg{i}.zip" for i in range(25)]),
            check_urls=True,
            probe=probe,
        )
        self.assertEqual(len(calls), 20, "只应探前 20 个地址")
        self.assertIn("DEP_EXTERNAL_URL", report.rules())

    def test_deadline_stops_further_probes(self) -> None:
        calls: list[str] = []

        def probe(url: str) -> int:
            calls.append(url)
            return 200

        # 第一次取现在（算 deadline），第一次探测仍在预算内，第二次已经超时。
        clock = iter([0.0, 0.0, 10_000.0, 10_000.0, 10_000.0])
        with mock.patch("store.validator.validate.time.monotonic", side_effect=lambda: next(clock)):
            report = validate_manifest(
                _manifest_with_urls(["https://host.example/a.zip", "https://host.example/b.zip"]),
                check_urls=True,
                probe=probe,
            )
        self.assertEqual(len(calls), 1)
        self.assertIn("DEP_EXTERNAL_UNREACHABLE", report.rules())


if __name__ == "__main__":
    unittest.main()
