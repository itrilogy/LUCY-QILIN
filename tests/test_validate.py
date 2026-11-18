"""阶段 1 验收用例。夹具写在临时目录，不使用真实技能仓库。"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from store.validator.net import probe_url
from store.validator.validate import Release, validate_manifest, validate_path

DESC = "说明阐述。这个技能做什么、不做什么，交付物是任务目录中的成稿，并且不会建立 MCP 连接。"
SUMMARY = "六阶段公文流水线，按阶段挂起。"


def manifest(**overrides: object) -> dict:
    base = {
        "spec_version": 1,
        "id": "luxi.gov-writer",
        "version": "3.2.0",
        "kind": "skill",
        "title": "公文撰写",
        "summary": SUMMARY,
        "description": DESC,
        "author": {"display_name": "鹿溪"},
        "platforms": ["any"],
        "changes": "首个发布",
        "clients": ["reasonix"],
        "depends": {"store": [], "external": [], "skills": [], "mcp": []},
        "secrets": [],
        "skill": {"entry": "SKILL.md"},
        "invoke": {"form": "load_skill", "entry": "SKILL.md", "note": "由客户端按技能加载"},
    }
    base.update(overrides)
    return base


def write_package(root: Path, body: dict, skill: str = "---\nname: gov\ndescription: 六阶段公文流水线，按阶段挂起。\n---\n\n# 公文\n") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    if body.get("kind") in {"skill", "mcp+skill", None} or "skill" in body:
        (root / "SKILL.md").write_text(skill, encoding="utf-8")


class ValidateTests(unittest.TestCase):
    def test_minimal_skill_passes(self) -> None:
        root = self._tmp("skill")
        write_package(root, manifest())
        report = validate_path(root)
        self.assertEqual(report.rules(), [], [item.line() for item in report.findings])
        self.assertTrue(report.ok)

    def test_minimal_mcp_passes(self) -> None:
        root = self._tmp("mcp")
        body = manifest(
            id="luxi.market-mcp",
            kind="mcp",
            version="1.0.0",
            clients=["cursor"],
            skill=None,
            invoke={"form": "list_tools"},
            mcp={"transport": "streamable-http", "url": "https://mcp.example.internal/market"},
        )
        body.pop("skill")
        write_package(root, body)
        report = validate_path(root)
        self.assertEqual(report.rules(), [], [item.line() for item in report.findings])

    def test_minimal_runtime_passes(self) -> None:
        root = self._tmp("runtime")
        payload = b"officecli-binary"
        digest = hashlib.sha256(payload).hexdigest()
        asset = root / "darwin-arm64" / "officecli"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(payload)
        body = manifest(
            id="luxi.officecli",
            kind="runtime",
            version="1.4.0",
            platforms=["darwin-arm64"],
            skill=None,
            runtime={
                "bin_name": "officecli",
                "assets": [{"platform": "darwin-arm64", "path": "darwin-arm64/officecli", "sha256": digest}],
            },
            invoke={"form": "exec_bin", "bin_name": "officecli"},
        )
        body.pop("skill")
        (root / "manifest.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        report = validate_path(root)
        self.assertEqual(report.rules(), [], [item.line() for item in report.findings])

    def test_semver_rejected(self) -> None:
        root = self._tmp("semver")
        write_package(root, manifest(version="3.2.0 (Agent-Driven Architecture)"))
        self.assertIn("SEMVER", validate_path(root).rules())

    def test_cursor_cannot_install_skill(self) -> None:
        root = self._tmp("cursor")
        write_package(root, manifest(clients=["cursor"]))
        self.assertIn("CLIENT_CAPABILITY", validate_path(root).rules())

    def test_secret_file_and_pattern(self) -> None:
        root = self._tmp("secret")
        write_package(root, manifest())
        (root / "SECRET.md").write_text("placeholder", encoding="utf-8")
        (root / "notes.md").write_text("token sk-" + "a" * 20, encoding="utf-8")
        rules = validate_path(root).rules()
        self.assertIn("SECRET_FILE", rules)
        self.assertIn("SECRET_PATTERN", rules)

    def test_external_url_must_be_https(self) -> None:
        root = self._tmp("http-url")
        write_package(root, manifest(depends=_external("http://example.com/pandoc")))
        self.assertIn("DEP_EXTERNAL_URL", validate_path(root).rules())

    def test_external_guide_missing(self) -> None:
        root = self._tmp("guide")
        depends = _external("https://example.com/pandoc")
        depends["external"][0]["install_guide"] = "太短"
        write_package(root, manifest(depends=depends))
        self.assertIn("DEP_GUIDE_MISSING", validate_path(root).rules())

    def test_unreachable_url(self) -> None:
        root = self._tmp("unreachable")
        write_package(root, manifest(depends=_external("https://127.0.0.1:9/pandoc")))
        self.assertIsNone(probe_url("http://127.0.0.1:9/pandoc", timeout=0.5))
        self.assertIsNone(probe_url("file:///etc/hosts", timeout=0.5))
        report = validate_path(root, check_urls=True, probe=lambda url: None)
        self.assertIn("DEP_EXTERNAL_UNREACHABLE", report.rules())

    def test_probe_local_server(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:
                self.send_response(200)
                self.end_headers()

            def do_GET(self) -> None:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, fmt: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status = probe_url(f"http://127.0.0.1:{server.server_port}/pandoc", timeout=2)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status, 200)

    def test_runtime_missing_asset_and_bad_checksum(self) -> None:
        root = self._tmp("checksum")
        asset = root / "darwin-arm64" / "officecli"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"abc")
        body = manifest(
            id="luxi.officecli",
            kind="runtime",
            platforms=["darwin-arm64", "windows-amd64"],
            runtime={
                "bin_name": "officecli",
                "assets": [{
                    "platform": "darwin-arm64",
                    "path": "darwin-arm64/officecli",
                    "sha256": "0" * 64,
                }],
            },
            invoke={"form": "exec_bin", "bin_name": "officecli"},
        )
        body.pop("skill")
        (root / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
        rules = validate_path(root).rules()
        self.assertIn("RUNTIME_ASSET", rules)
        self.assertIn("CHECKSUM", rules)

    def test_unpublished_runtime_dependency(self) -> None:
        root = self._tmp("dep")
        write_package(root, manifest(depends={
            "store": [{"id": "luxi.officecli", "version": ">=1.0.0"}],
            "external": [],
            "skills": [],
            "mcp": [],
        }))
        report = validate_path(root, releases=[Release("luxi.other", "1.0.0", "runtime")])
        self.assertIn("DEP_STORE_UNPUBLISHED", report.rules())

    def test_frontmatter_version_conflict(self) -> None:
        root = self._tmp("front")
        write_package(
            root,
            manifest(),
            skill="---\nversion: 1.0.0\ndescription: 六阶段公文流水线，按阶段挂起。\n---\n",
        )
        self.assertIn("FRONTMATTER_CONFLICT", validate_path(root).rules())

    def test_directory_name_does_not_rewrite_the_id(self) -> None:
        # 目录名只是文件夹；商品身份由清单里的 id 决定。
        root = self._tmp("GOV-Antigravity-V3")
        write_package(root, manifest(id="luxi.someone-else"))
        self.assertNotIn("ID_FORMAT", validate_path(root).rules())
        self.assertNotIn("ID_UNREGISTERED", validate_path(root).rules())

    def test_id_with_trailing_newline_is_rejected(self) -> None:
        # Python 的 `$` 会匹配结尾换行；id 用 fullmatch 才不会被 "a.b\n" 绕过。
        self.assertIn("ID_FORMAT", validate_manifest(manifest(id="luxi.gov-writer\n")).rules())

    def _tmp(self, name: str) -> Path:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        root = base / name
        root.mkdir()
        return root


def _external(url: str) -> dict:
    return {
        "store": [],
        "external": [{
            "name": "pandoc",
            "version_range": ">=3.0",
            "detect": ["pandoc", "--version"],
            "install_guide": "从下面的地址安装对应系统的安装包，装好后保证命令 pandoc 在 PATH 中。",
            "resources": [{"title": "Pandoc", "url": url, "platform": "any"}],
        }],
        "skills": [],
        "mcp": [],
    }


if __name__ == "__main__":
    unittest.main()
