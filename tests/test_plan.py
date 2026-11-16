"""版本选择、安装计划和追加平台。"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from store.plan import PlanError, install_plan, resolve
from store.service import Store, StoreError
from tests.test_service import DESC, SKILL, SUMMARY, skill_manifest

BASE = "http://store.example"


def _release(package_id: str, version: str, platforms: list[str], *, kind: str = "skill") -> dict:
    return {
        "id": package_id,
        "version": version,
        "clients": ["reasonix"],
        "platforms": platforms,
        "changes": "首个发布" if version == "1.0.0" else "补充了这个平台的脚本。",
        "published_at": "2026-11-11",
        "artifact_sha256": "ab" * 32,
        "manifest": {
            "kind": kind,
            "clients": ["reasonix"],
            "platforms": platforms,
            "depends": {"store": [], "external": [], "skills": [], "mcp": []},
            "secrets": [],
            "skill": {"entry": "SKILL.md"},
            "invoke": {"form": "load_skill", "entry": "SKILL.md"},
        },
    }


class DependencyExpansionTests(unittest.TestCase):
    def test_dependency_steps_are_expanded_without_the_marker(self) -> None:
        base = _release("luxi.base", "1.0.0", ["any"])
        base["manifest"]["depends"]["skills"] = [{"id": "luxi.gov-writer", "version": "*"}]
        dependency = _release("luxi.gov-writer", "3.2.0", ["any"])
        plan = install_plan(
            [base, dependency],
            package_id="luxi.base",
            version="1.0.0",
            client="reasonix",
            platform="darwin-arm64",
            base_url=BASE,
        )
        ops = [step["op"] for step in plan["steps"]]
        self.assertNotIn("ensure_product", ops)
        self.assertEqual(ops.count("unpack"), 2)
        self.assertEqual(ops[-1], "verify")


class ResolveTests(unittest.TestCase):
    def test_latest_without_platform_is_not_a_silent_downgrade(self) -> None:
        releases = [
            _release("luxi.gov-writer", "1.0.0", ["windows-amd64"]),
            _release("luxi.gov-writer", "1.1.0", ["darwin-arm64"]),
        ]
        chosen = resolve(
            releases,
            package_id="luxi.gov-writer",
            client="reasonix",
            platform="windows-amd64",
        )
        self.assertEqual([item["version"] for item in chosen["candidates"]], ["1.0.0"])
        self.assertEqual(chosen["selected"]["version"], "1.0.0")
        self.assertEqual(chosen["reason"], "highest_match_platform")

    def test_highest_match_when_latest_has_the_platform(self) -> None:
        releases = [
            _release("luxi.gov-writer", "1.0.0", ["windows-amd64"]),
            _release("luxi.gov-writer", "1.1.0", ["windows-amd64", "darwin-arm64"]),
        ]
        chosen = resolve(
            releases,
            package_id="luxi.gov-writer",
            client="reasonix",
            platform="windows-amd64",
        )
        self.assertEqual(chosen["selected"]["version"], "1.1.0")
        self.assertEqual(chosen["reason"], "highest_match")

    def test_prerelease_is_dropped_unless_asked_for(self) -> None:
        releases = [
            _release("luxi.gov-writer", "1.0.0", ["darwin-arm64"]),
            _release("luxi.gov-writer", "1.1.0-rc.1", ["darwin-arm64"]),
        ]
        chosen = resolve(
            releases, package_id="luxi.gov-writer", client="reasonix", platform="darwin-arm64",
        )
        self.assertEqual(chosen["selected"]["version"], "1.0.0")
        pinned = resolve(
            releases,
            package_id="luxi.gov-writer",
            client="reasonix",
            platform="darwin-arm64",
            version="1.1.0-rc.1",
        )
        self.assertEqual(pinned["selected"]["version"], "1.1.0-rc.1")

    def test_install_plan_requires_exact_version(self) -> None:
        with self.assertRaises(PlanError) as caught:
            install_plan(
                [_release("luxi.gov-writer", "1.0.0", ["any"])],
                package_id="luxi.gov-writer",
                version=">=1.0.0",
                client="reasonix",
                platform="darwin-arm64",
                base_url=BASE,
            )
        self.assertEqual(caught.exception.rule, "VERSION_REQUIRED")

    def test_dependency_cycle(self) -> None:
        left = _release("luxi.gov-writer", "1.0.0", ["any"])
        right = _release("luxi.qc-cluster", "1.0.0", ["any"])
        left["manifest"]["depends"]["skills"] = [{"id": "luxi.qc-cluster", "version": "*"}]
        right["manifest"]["depends"]["skills"] = [{"id": "luxi.gov-writer", "version": "*"}]
        with self.assertRaises(PlanError) as caught:
            install_plan(
                [left, right],
                package_id="luxi.gov-writer",
                version="1.0.0",
                client="reasonix",
                platform="darwin-arm64",
                base_url=BASE,
            )
        self.assertEqual(caught.exception.rule, "DEP_CYCLE")


class StorePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")

    def test_published_versions_resolve_like_the_spec(self) -> None:
        self._publish("1.0.0", ["windows-amd64"])
        self._publish("1.1.0", ["darwin-arm64"])
        chosen = self.store.resolve_package(
            "luxi.gov-writer", client="reasonix", platform="windows-amd64",
        )
        self.assertEqual([item["version"] for item in chosen["candidates"]], ["1.0.0"])
        self.assertEqual(chosen["selected"]["version"], "1.0.0")
        self.assertEqual(chosen["reason"], "highest_match_platform")
        self.assertEqual(
            [item["version"] for item in self.store.list_versions("luxi.gov-writer")],
            ["1.0.0", "1.1.0"],
        )
        with self.assertRaises(StoreError) as caught:
            self.store.unpublish(self.publisher, "luxi.gov-writer", "1.1.0", "作者不能下架")
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")
        self.store.unpublish(self.admin, "luxi.gov-writer", "1.1.0", "darwin 这一版先撤下")
        again = self.store.resolve_package(
            "luxi.gov-writer", client="reasonix", platform="darwin-arm64",
        )
        self.assertIsNone(again["selected"])
        self.assertEqual(again["rule"], "VERSION_UNAVAILABLE")

    def test_three_kinds_have_the_spec_op_order(self) -> None:
        self._publish("3.2.0", ["any"], skill_manifest())
        skill = self.store.make_install_plan(
            "luxi.gov-writer", "3.2.0", client="reasonix", platform="darwin-arm64", base_url=BASE,
        )
        self.assertEqual([step["op"] for step in skill["steps"]], ["download", "unpack", "verify"])
        self.assertNotIn("transport", skill["steps"][-1])

        remote = skill_manifest(
            id="luxi.market-mcp",
            kind="mcp",
            version="1.0.0",
            clients=["cursor"],
            platforms=["any"],
            skill=None,
            invoke={"form": "list_tools"},
            mcp={"transport": "streamable-http", "url": "https://mcp.example.internal/market"},
        )
        remote.pop("skill")
        self._submit_and_approve(remote, {})
        mcp = self.store.make_install_plan(
            "luxi.market-mcp", "1.0.0", client="cursor", platform="darwin-arm64", base_url=BASE,
        )
        self.assertEqual([step["op"] for step in mcp["steps"]], ["mcp_add", "verify"])
        self.assertEqual(mcp["steps"][-1]["transport"], "streamable-http")
        self.assertEqual(mcp["steps"][-1]["url"], "https://mcp.example.internal/market")
        self.assertNotIn("command", mcp["steps"][-1])

        digest = hashlib.sha256(b"officecli-binary").hexdigest()
        runtime = skill_manifest(
            id="luxi.officecli",
            kind="runtime",
            version="1.4.0",
            platforms=["darwin-arm64"],
            clients=["reasonix"],
            skill=None,
            runtime={
                "bin_name": "officecli",
                "assets": [{
                    "platform": "darwin-arm64",
                    "path": "darwin-arm64/officecli",
                    "sha256": digest,
                }],
            },
            invoke={"form": "exec_bin", "bin_name": "officecli"},
        )
        runtime.pop("skill")
        self._submit_and_approve(runtime, {"darwin-arm64/officecli": b"officecli-binary"})
        hybrid = skill_manifest(
            id="luxi.slide-master",
            kind="mcp+skill",
            version="1.0.0",
            platforms=["darwin-arm64"],
            clients=["reasonix"],
            depends={
                "store": [{"id": "luxi.officecli", "version": ">=1.0.0"}],
                "external": [],
                "skills": [],
                "mcp": [],
            },
            mcp={"transport": "stdio", "command": "officecli", "args": ["serve"]},
            invoke={"form": "load_skill_then_mcp", "entry": "SKILL.md"},
        )
        self._submit_and_approve(hybrid, {"SKILL.md": _skill("1.0.0").encode()})
        plan = self.store.make_install_plan(
            "luxi.slide-master", "1.0.0", client="reasonix", platform="darwin-arm64", base_url=BASE,
        )
        self.assertEqual(
            [step["op"] for step in plan["steps"]],
            ["download", "place_bin", "mcp_add", "download", "unpack", "verify"],
        )
        self.assertEqual(plan["steps"][0]["sha256"], digest)
        self.assertNotIn("latest", [step.get("version") for step in plan["steps"]])
        checked = plan["steps"][-1]
        self.assertEqual(checked["transport"], "stdio")
        self.assertEqual(checked["command"], "officecli")
        self.assertEqual(checked["args"], ["serve"])
        self.assertEqual(checked["entry"], "SKILL.md")

    def test_add_platform_then_resolve_and_reject_a_rewrite(self) -> None:
        digest = hashlib.sha256(b"mac").hexdigest()
        windows = hashlib.sha256(b"win").hexdigest()
        runtime = _runtime_manifest(digest, ["darwin-arm64"])
        self._submit_and_approve(runtime, {"darwin-arm64/officecli": b"mac"})
        added = _runtime_manifest(windows, ["windows-amd64"])
        added["runtime"]["assets"] = [{
            "platform": "windows-amd64",
            "path": "windows-amd64/officecli.exe",
            "sha256": windows,
        }]
        result = self.store.submit(
            self.publisher,
            added,
            {"windows-amd64/officecli.exe": b"win"},
            submit_kind="add_platform",
        )
        self.assertEqual(result.status, "pending")
        self.store.approve(self.admin, result.submission_id)
        chosen = self.store.resolve_package(
            "luxi.officecli", client="reasonix", platform="windows-amd64",
        )
        self.assertEqual(chosen["selected"]["version"], "1.4.0")
        body, file_sha = self.store.artifact("luxi.officecli", "1.4.0", "windows-amd64")
        self.assertEqual(body, b"win")
        self.assertEqual(file_sha, windows)
        with self.assertRaises(StoreError) as taken:
            self.store.submit(
                self.publisher, added, {"windows-amd64/officecli.exe": b"win"},
                submit_kind="add_platform",
            )
        self.assertEqual(taken.exception.rule, "PLATFORM_TAKEN")
        rewritten = _runtime_manifest(windows, ["linux-amd64"])
        rewritten["summary"] = "换成了另一句说明，不该被当成追加平台。"
        rewritten["runtime"]["assets"] = [{
            "platform": "linux-amd64",
            "path": "linux-amd64/officecli",
            "sha256": windows,
        }]
        with self.assertRaises(StoreError) as scoped:
            self.store.submit(
                self.publisher, rewritten, {"linux-amd64/officecli": b"win"},
                submit_kind="add_platform",
            )
        self.assertEqual(scoped.exception.rule, "PLATFORM_SCOPE")

    def _publish(self, version: str, platforms: list[str], manifest: dict | None = None) -> None:
        body = manifest or skill_manifest(version=version, platforms=platforms, changes=_changes(version))
        self._submit_and_approve(body, {"SKILL.md": _skill(version).encode()})

    def _submit_and_approve(self, manifest: dict, files: dict[str, bytes]) -> None:
        result = self.store.submit(self.publisher, manifest, files)
        self.assertEqual(result.status, "pending", result.rules)
        self.store.approve(self.admin, result.submission_id)


def _skill(version: str) -> str:
    if version == "3.2.0":
        return SKILL
    return f"---\nname: gov\ndescription: {SUMMARY}\nversion: {version}\n---\n\n# 公文\n"


def _changes(version: str) -> str:
    if version == "3.2.0":
        return "首个发布"
    return f"发布 {version}，供选择平台。"


def _runtime_manifest(digest: str, platforms: list[str]) -> dict:
    platform = platforms[0]
    body = skill_manifest(
        id="luxi.officecli",
        kind="runtime",
        version="1.4.0",
        platforms=platforms,
        clients=["reasonix"],
        skill=None,
        runtime={
            "bin_name": "officecli",
            "assets": [{
                "platform": platform,
                "path": f"{platform}/officecli",
                "sha256": digest,
            }],
        },
        invoke={"form": "exec_bin", "bin_name": "officecli"},
    )
    body.pop("skill")
    return body


if __name__ == "__main__":
    unittest.main()
