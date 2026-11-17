"""插件品类：Tier A/B/C、包结构、安装计划与 staging。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from store.errors import StoreError
from store.installer import run as run_installer
from store.service import Store
from store.validator.validate import validate_manifest, validate_path


def plugin_manifest(**overrides) -> dict:
    manifest = {
        "spec_version": 1,
        "kind": "plugin",
        "id": "lab.hello-plugin",
        "version": "1.0.0",
        "title": "示例插件",
        "summary": "一个用于验收的最小插件。",
        "description": "这个插件只注册一个工具，用来验证插件品类的清单、包结构和安装计划，不执行任何真实业务逻辑。",
        "changes": "首个发布，用于验收。",
        "author": {"display_name": "鹿溪实验室"},
        "platforms": ["any"],
        "clients": ["deepseek-harness"],
        "depends": {"store": [], "external": [], "skills": [], "mcp": []},
        "secrets": [],
        "invoke": {"form": "install_plugin"},
        "plugin": {
            "tier": "A",
            "form": "both",
            "entry": "index.js",
            "client": "client.js",
            "patch": "cordis.patch.yml",
            "files": ["index.js", "client.js", "cordis.patch.yml"],
            "dsh": ">=1.5 <2",
            "capabilities": ["tools"],
            "external": [],
        },
    }
    manifest.update(overrides)
    return manifest


def plugin_files() -> dict[str, bytes]:
    return {
        "package.json": json.dumps({
            "name": "@lab/hello-plugin",
            "version": "1.0.0",
            "type": "module",
            "exports": {".": "./index.js", "./client": "./client.js"},
            "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
        }, ensure_ascii=False).encode(),
        "index.js": b"export function apply() {}\n",
        "client.js": b"export function apply() {}\n",
        "cordis.patch.yml": b"- insert: []\n",
    }


def write_tree(files: dict[str, bytes]) -> Path:
    root = Path(tempfile.mkdtemp())
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (root / "manifest.json").write_text(json.dumps(plugin_manifest(), ensure_ascii=False), encoding="utf-8")
    return root


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class PluginValidationTests(unittest.TestCase):
    def test_minimal_tier_a_package_is_valid(self) -> None:
        root = write_tree(plugin_files())
        report = validate_path(root)
        self.assertTrue(report.ok, report.rules())

    def test_static_manifest_accepts_plugin_without_files(self) -> None:
        report = validate_manifest(plugin_manifest())
        self.assertTrue(report.ok, report.rules())

    def test_scripts_are_rejected(self) -> None:
        files = plugin_files()
        package = json.loads(files["package.json"])
        package["scripts"] = {"postinstall": "node install.js"}
        files["package.json"] = json.dumps(package).encode()
        report = validate_path(write_tree(files))
        self.assertIn("PLUGIN_SCRIPT", report.rules())

    def test_tier_a_runtime_dependency_is_rejected(self) -> None:
        files = plugin_files()
        package = json.loads(files["package.json"])
        package["dependencies"] = {"yaml": "^2"}
        files["package.json"] = json.dumps(package).encode()
        report = validate_path(write_tree(files))
        self.assertIn("PLUGIN_DEPENDENCY", report.rules())

    def test_tier_b_dependency_not_on_allowlist_is_rejected(self) -> None:
        manifest = plugin_manifest()
        manifest["plugin"]["tier"] = "B"
        files = plugin_files()
        package = json.loads(files["package.json"])
        package["dependencies"] = {"yaml": "^2"}
        files["package.json"] = json.dumps(package).encode()
        root = write_tree(files)
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = validate_path(root)
        self.assertIn("PLUGIN_DEPENDENCY", report.rules())

    def test_native_module_is_rejected(self) -> None:
        files = plugin_files()
        files["native.node"] = b"\x00"
        report = validate_path(write_tree(files))
        self.assertIn("PLUGIN_ARTIFACT", report.rules())

    def test_external_command_requires_process_capability(self) -> None:
        manifest = plugin_manifest()
        manifest["plugin"]["external"] = ["rg"]
        report = validate_manifest(manifest)
        self.assertIn("PLUGIN_CAPABILITY", report.rules())

    def test_tier_c_is_rejected(self) -> None:
        manifest = plugin_manifest()
        manifest["plugin"]["tier"] = "C"
        report = validate_manifest(manifest)
        self.assertIn("PLUGIN_TIER", report.rules())

    def test_wrong_client_is_rejected(self) -> None:
        manifest = plugin_manifest(clients=["reasonix"])
        report = validate_manifest(manifest)
        self.assertIn("CLIENT_CAPABILITY", report.rules())


class PluginServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")
        _, self.other_admin = self.store.create_principal("另一个管理员", "admin")

    def _submit(self, manifest=None):
        result = self.store.submit(self.publisher, manifest or plugin_manifest(), plugin_files())
        self.assertEqual(result.status, "pending", result.rules)
        return result

    def test_tier_a_publisher_can_submit_and_admin_publishes(self) -> None:
        result = self._submit()
        self.store.approve(self.admin, result.submission_id)
        releases = self.store.list_versions("lab.hello-plugin")
        self.assertEqual(releases[0]["version"], "1.0.0")
        page = self.store.search_page("示例插件")
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["kind"], "plugin")

    def test_tier_b_publisher_is_blocked(self) -> None:
        manifest = plugin_manifest()
        manifest["plugin"]["tier"] = "B"
        with self.assertRaises(StoreError) as caught:
            self.store.submit(self.publisher, manifest, plugin_files())
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_admin_cannot_approve_own_plugin(self) -> None:
        result = self.store.submit(self.admin, plugin_manifest(), plugin_files())
        self.assertEqual(result.status, "pending", result.rules)
        with self.assertRaises(StoreError) as caught:
            self.store.approve(self.admin, result.submission_id)
        self.assertEqual(caught.exception.rule, "SELF_REVIEW")
        self.store.approve(self.other_admin, result.submission_id)

    def test_install_plan_uses_plugin_staging(self) -> None:
        result = self._submit()
        self.store.approve(self.admin, result.submission_id)
        plan = self.store.make_install_plan(
            "lab.hello-plugin", "1.0.0",
            client="deepseek-harness", platform="darwin-arm64", base_url="http://127.0.0.1:8787",
        )
        ops = [step["op"] for step in plan["steps"]]
        self.assertEqual(ops, ["download", "plugin_stage", "plugin_confirm", "verify"])
        self.assertIn("${PLUGIN_STAGING}", plan["steps"][1]["dest"])

    def test_installer_stages_without_executing(self) -> None:
        archive = make_zip(plugin_files())
        root = Path(self.tmp.name)
        src = root / "plugin.zip"
        src.write_bytes(archive)
        staging = root / "plugins"
        plan = {
            "steps": [
                {
                    "op": "plugin_stage",
                    "src": str(src),
                    "dest": str(staging / "lab.hello-plugin" / "1.0.0"),
                    "id": "lab.hello-plugin",
                    "version": "1.0.0",
                    "entry": "index.js",
                    "client": "client.js",
                    "patch": "cordis.patch.yml",
                },
                {
                    "op": "plugin_confirm",
                    "id": "lab.hello-plugin",
                    "version": "1.0.0",
                    "path": str(staging / "lab.hello-plugin" / "1.0.0"),
                },
            ],
        }
        self.assertEqual(run_installer(plan), 0)
        self.assertTrue((staging / "lab.hello-plugin" / "1.0.0" / "package.json").is_file())


if __name__ == "__main__":
    unittest.main()
