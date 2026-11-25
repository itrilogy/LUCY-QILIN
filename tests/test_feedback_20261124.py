"""2026-11-24 反馈审计与客户端新增需求全量回归测试。

覆盖：
1. 历史相对/容器绝对路径（var/blobs/... 与 /var/lib/qilin/blobs/...）自动归一化、脱离 cwd 下载、审核基线 diff 恢复；
2. 制品缺失与篡改统一返回 ARTIFACT_MISSING（HTTP 503），/api/health 报 degraded，MCP artifact 不把 zip 读入内存；
3. kind=mcp 口径 A：stdio 随包脚本单 zip 下载 + install_plan 解压重写 + installer 端到端通过；纯远程无 zip 报 KIND_NO_ARTIFACT；
4. REQ-01~05：validate 跨文件预检与 summary 降级 warning、QILIN_URL_CHECK=warn/off 与内网白名单 http、withdraw 支持 uploading/rejected、X-Upload-Ticket 头上传、antigravity 客户端与 plugin.form=client Schema；
5. RP-01：trusted_proxies CIDR 白名单与 X-Forwarded-For 自右向左解析防伪造；
6. WP-4：消费者问题反馈闭环（report_problem / get_report / list_my_reports / list_product_reports / list_reports / triage_report）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from store.blobs import write_zip
from store.errors import StoreError
from store.http_api import serve
from store.installer import run as run_install
from store.service import Store
from tests.test_service import SKILL, skill_manifest


STDIO_BRIDGE_SCRIPT = """import json, sys
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    msg = json.loads(raw)
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        out = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "chengju-bridge", "version": "3.6.3"},
            },
        }
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.stdout.flush()
    elif method == "tools/list":
        out = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": [{"name": "render_chart", "description": "渲染图表"}]},
        }
        sys.stdout.write(json.dumps(out) + "\\n")
        sys.stdout.flush()
        break
"""


def _stdio_mcp_manifest(version: str = "3.6.3") -> dict:
    return {
        "spec_version": 1,
        "id": "luxi.chengju-chart-mcp",
        "version": version,
        "kind": "mcp",
        "title": "成矩图表 MCP",
        "summary": "提供 53 种工程与统计图表渲染工具。",
        "description": "通过本地 Python stdio 桥接脚本连接内网图表渲染引擎，支持流式返回渲染产物。",
        "author": {"display_name": "鹿溪"},
        "platforms": ["darwin-arm64", "linux-amd64"],
        "changes": "首个发布，随包附带 sse_bridge.py。",
        "clients": ["antigravity", "deepseek-harness"],
        "depends": {"store": [], "external": [], "skills": [], "mcp": []},
        "secrets": [],
        "mcp": {
            "transport": "stdio",
            "command": sys.executable,
            "args": ["sse_bridge.py"],
        },
        "invoke": {
            "form": "list_tools",
            "tools": [{"name": "render_chart", "summary": "渲染工程图表"}],
        },
    }


def _remote_mcp_manifest() -> dict:
    return {
        "spec_version": 1,
        "id": "luxi.remote-search-mcp",
        "version": "1.0.0",
        "kind": "mcp",
        "title": "远端检索 MCP",
        "summary": "纯远程 HTTP MCP 服务。",
        "description": "无需任何本地脚本或制品压缩包，客户端直接通过 streamable-http 连接远端服务。",
        "author": {"display_name": "鹿溪"},
        "platforms": ["any"],
        "changes": "首个发布",
        "clients": ["antigravity", "cursor"],
        "depends": {"store": [], "external": [], "skills": [], "mcp": []},
        "secrets": [],
        "mcp": {
            "transport": "streamable-http",
            "url": "https://mcp.example.com/mcp",
        },
        "invoke": {
            "form": "documented",
            "tools": [{"name": "remote_search", "summary": "执行远端检索"}],
        },
    }


class LegacyBlobPathAndHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "data" / "qilin.db"
        self.blob_dir = self.root / "data" / "blobs"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.db_path, self.blob_dir)
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")

    def test_legacy_paths_heal_across_cwd_and_preserve_review_diff(self) -> None:
        first = self.store.submit(
            self.publisher,
            skill_manifest(version="1.0.0", changes="首个发布版本说明超过十个字"),
            {"SKILL.md": SKILL.replace("3.2.0", "1.0.0").encode()},
        )
        self.assertEqual(first.status, "pending")
        self.store.approve(self.admin, first.submission_id)

        # 模拟宿主机 var/blobs/... 与旧容器 /var/lib/qilin/blobs/... 混写残留，并退回 user_version=1 触发迁移。
        self.store._db.execute(
            "UPDATE releases SET artifact_path = 'var/blobs/releases/luxi.gov-writer/1.0.0.zip' "
            "WHERE package_id = 'luxi.gov-writer' AND version = '1.0.0'"
        )
        self.store._db.execute("PRAGMA user_version = 1")
        self.store._db.commit()
        self.store.close()

        unrelated_cwd = self.root / "elsewhere"
        unrelated_cwd.mkdir()
        prev_cwd = Path.cwd()
        os.chdir(unrelated_cwd)
        try:
            reopened = Store(self.db_path, self.blob_dir)
            self.addCleanup(reopened.close)
            row = reopened._db.execute(
                "SELECT artifact_path FROM releases WHERE package_id = 'luxi.gov-writer' AND version = '1.0.0'"
            ).fetchone()
            self.assertEqual(row["artifact_path"], "releases/luxi.gov-writer/1.0.0.zip")
            body, digest = reopened.artifact("luxi.gov-writer", "1.0.0")
            self.assertEqual(hashlib.sha256(body).hexdigest(), digest)
            self.assertEqual(reopened.health()["status"], "ok")

            # 提交 1.1.0，验证审核页能正确读取 1.0.0 的基线 zip 并计算 file_diffs。
            second = reopened.submit(
                self.publisher,
                skill_manifest(version="1.1.0", changes="新增参考文档并修订入口"),
                {
                    "SKILL.md": SKILL.replace("3.2.0", "1.1.0").encode(),
                    "references/guide.md": "# 指南\n".encode(),
                },
            )
            self.assertEqual(second.status, "pending", second.rules)
            review = reopened.get_review(self.admin, second.submission_id)
            self.assertTrue(
                any(
                    item["path"] == "references/guide.md" and item["status"] == "added"
                    for item in review["file_diffs"]
                ),
                review["file_diffs"],
            )
        finally:
            os.chdir(prev_cwd)

    def test_missing_or_corrupted_artifact_returns_503_and_mcp_avoids_read_bytes(self) -> None:
        sub = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.store.approve(self.admin, sub.submission_id)
        server = serve(self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_port}"

        # 1. MCP artifact 工具不得调用 Path.read_bytes 把整个制品读入内存。
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("禁止整包读入内存")):
            req = urllib.request.Request(
                base + "/mcp",
                data=json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "artifact", "arguments": {"id": "luxi.gov-writer", "version": "3.2.0"}},
                }).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as resp:
                mcp_body = json.load(resp)
            self.assertFalse(mcp_body["result"]["isError"])
            art_info = json.loads(mcp_body["result"]["content"][0]["text"])
            self.assertIn("/v1/artifacts/luxi.gov-writer/3.2.0", art_info["url"])

        # 2. 篡改磁盘 zip 单字节：流式下载必须在发 200 头之前拦截并回 503 ARTIFACT_MISSING。
        archive_path, _ = self.store.artifact_file("luxi.gov-writer", "3.2.0")
        original_bytes = archive_path.read_bytes()
        archive_path.write_bytes(original_bytes[:-1] + bytes([original_bytes[-1] ^ 0xFF]))
        with self.assertRaises(urllib.error.HTTPError) as corrupt_err:
            urllib.request.urlopen(base + "/v1/artifacts/luxi.gov-writer/3.2.0")
        self.assertEqual(corrupt_err.exception.code, 503)
        corrupt_payload = json.loads(corrupt_err.exception.read().decode())
        corrupt_err.exception.close()
        self.assertEqual(corrupt_payload["rule"], "ARTIFACT_MISSING")

        # 3. 删除磁盘 zip：下载回 503 ARTIFACT_MISSING，且 /api/health 报 503 degraded。
        archive_path.unlink()
        with self.assertRaises(urllib.error.HTTPError) as missing_err:
            urllib.request.urlopen(base + "/v1/artifacts/luxi.gov-writer/3.2.0")
        self.assertEqual(missing_err.exception.code, 503)
        missing_payload = json.loads(missing_err.exception.read().decode())
        missing_err.exception.close()
        self.assertEqual(missing_payload["rule"], "ARTIFACT_MISSING")

        with self.assertRaises(urllib.error.HTTPError) as health_err:
            urllib.request.urlopen(base + "/api/health")
        self.assertEqual(health_err.exception.code, 503)
        health_payload = json.loads(health_err.exception.read().decode())
        health_err.exception.close()
        self.assertEqual(health_payload["status"], "degraded")
        self.assertEqual(health_payload["blobs"], "missing_artifacts")

        # 4. 不存在的版本则返回 404 VERSION_UNAVAILABLE，与 503 清晰区分。
        with self.assertRaises(urllib.error.HTTPError) as not_found_err:
            urllib.request.urlopen(base + "/v1/artifacts/luxi.gov-writer/9.9.9")
        self.assertEqual(not_found_err.exception.code, 404)
        nf_payload = json.loads(not_found_err.exception.read().decode())
        not_found_err.exception.close()
        self.assertEqual(nf_payload["rule"], "VERSION_UNAVAILABLE")


class McpArtifactOptionATests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")
        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_stdio_mcp_missing_bundled_script_is_rejected(self) -> None:
        res = self.store.submit(self.publisher, _stdio_mcp_manifest(), {})
        self.assertEqual(res.status, "rejected")
        self.assertIn("MCP_CONNECT", res.rules)

    def test_stdio_mcp_with_script_downloads_installs_and_verifies_end_to_end(self) -> None:
        res = self.store.submit(
            self.publisher,
            _stdio_mcp_manifest(),
            {"sse_bridge.py": STDIO_BRIDGE_SCRIPT.encode("utf-8")},
        )
        self.assertEqual(res.status, "pending", res.rules)
        self.store.approve(self.admin, res.submission_id)

        # artifact 与 HTTP 下载均返回 200 zip
        body, digest = self.store.artifact("luxi.chengju-chart-mcp", "3.6.3")
        self.assertEqual(hashlib.sha256(body).hexdigest(), digest)

        plan = self.store.make_install_plan(
            "luxi.chengju-chart-mcp",
            "3.6.3",
            client="antigravity",
            platform="darwin-arm64",
            base_url=self.base,
        )
        self.assertEqual([s["op"] for s in plan["steps"]], ["download", "unpack", "mcp_add", "verify"])
        mcp_add_step = next(s for s in plan["steps"] if s["op"] == "mcp_add")
        self.assertEqual(
            mcp_add_step["args"],
            ["${MCP_DIR}/luxi.chengju-chart-mcp/3.6.3/sse_bridge.py"],
        )
        self.assertEqual(mcp_add_step["cwd"], "${MCP_DIR}/luxi.chengju-chart-mcp/3.6.3")

        work_dir = self.root / "client-work"
        mcp_dir = self.root / "client-mcp"
        exit_code = run_install(
            plan,
            work=work_dir,
            skills_root=self.root / "client-skills",
            bin_dir=self.root / "client-bin",
            mcp_dir=mcp_dir,
        )
        self.assertEqual(exit_code, 0)
        installed_script = mcp_dir / "luxi.chengju-chart-mcp" / "3.6.3" / "sse_bridge.py"
        self.assertTrue(installed_script.is_file())
        mcp_cfg = json.loads((mcp_dir / "luxi.chengju-chart-mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(mcp_cfg["args"], [str(installed_script)])
        self.assertEqual(mcp_cfg["cwd"], str(installed_script.parent))

    def test_pure_remote_mcp_publishes_without_zip_and_returns_kind_no_artifact(self) -> None:
        started = self.store.begin_submit(
            self.publisher, _remote_mcp_manifest(), byte_size=0,
        )
        self.assertEqual(started["status"], "uploading")
        self.assertIsNone(started["put_path"])
        finished = self.store.finish_submit(self.publisher, started["submission_id"])
        self.assertEqual(finished.status, "pending", finished.rules)
        self.store.approve(self.admin, started["submission_id"])

        with self.assertRaises(StoreError) as caught:
            self.store.artifact("luxi.remote-search-mcp", "1.0.0")
        self.assertEqual(caught.exception.rule, "KIND_NO_ARTIFACT")


class SubmissionAndValidationReqTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")
        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _mcp_call(self, name: str, arguments: dict, token: str) -> dict:
        req = urllib.request.Request(
            self.base + "/mcp",
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }).encode(),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        with urllib.request.urlopen(req) as resp:
            payload = json.load(resp)
        return json.loads(payload["result"]["content"][0]["text"])

    def test_req01_validate_prechecks_skill_md_and_downgrades_summary_mismatch(self) -> None:
        # 1. frontmatter version 与 manifest.version 冲突：上传前 validate(skill_md=...) 直接拦截。
        bad_skill = "---\nname: gov\ndescription: 六阶段公文流水线。\nversion: 9.9.9\n---\n# 公文\n"
        checked = self._mcp_call(
            "validate",
            {"manifest": skill_manifest(clients=["antigravity"]), "skill_md": bad_skill},
            self.publisher,
        )
        self.assertEqual(checked["scope"], "package")
        self.assertFalse(checked["ok"])
        self.assertIn("FRONTMATTER_CONFLICT", checked["rules"])

        # 2. summary 不是 frontmatter description 的子串：降级为 warning，ok 为 True，且可正常提交。
        paraphrased_skill = "---\nname: gov\ndescription: 面向政企场景的多智能体协同公文写作系统。\nversion: 3.2.0\n---\n# 公文\n"
        warn_checked = self._mcp_call(
            "validate",
            {
                "manifest": skill_manifest(
                    clients=["antigravity"],
                    summary="独立概括的一句话摘要，并非题头子串。",
                ),
                "skill_md": paraphrased_skill,
            },
            self.publisher,
        )
        self.assertTrue(warn_checked["ok"], warn_checked)
        self.assertEqual(warn_checked["rules"], [])
        self.assertTrue(
            any(
                f["rule"] == "FRONTMATTER_CONFLICT" and f["severity"] == "warning"
                for f in warn_checked["findings"]
            )
        )
        submitted = self._mcp_call(
            "submit_package",
            {
                "manifest": skill_manifest(
                    clients=["antigravity"],
                    summary="独立概括的一句话摘要，并非题头子串。",
                ),
                "files": {"SKILL.md": base64.b64encode(paraphrased_skill.encode()).decode()},
            },
            self.publisher,
        )
        self.assertEqual(submitted["status"], "pending", submitted)

    def test_req02_offline_url_check_warn_mode_and_allowlisted_http(self) -> None:
        manifest = skill_manifest(clients=["antigravity"])
        manifest["depends"] = {
            "store": [],
            "skills": [],
            "mcp": [],
            "external": [{
                "name": "python3",
                "version_range": ">=3.11",
                "detect": ["python3", "--version"],
                "install_guide": "请在系统中安装 python3 3.11 及以上版本并加入 PATH。",
                "resources": [{"title": "内网镜像", "url": "http://10.37.87.25:8080/python", "platform": "any"}],
            }],
        }
        pkg_dir = self.root / "offline-skill"
        pkg_dir.mkdir()
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (pkg_dir / "SKILL.md").write_text(SKILL, encoding="utf-8")
        zip_path = self.root / "offline-skill.zip"
        write_zip(pkg_dir, zip_path)
        archive = zip_path.read_bytes()

        with mock.patch.dict(
            os.environ,
            {"QILIN_FETCH_ALLOW_HOSTS": "10.37.87.25", "QILIN_URL_CHECK": "warn"},
        ):
            started = self.store.begin_submit(self.publisher, manifest, byte_size=len(archive))
            self.assertEqual(started["status"], "uploading", started)
            self.store.save_upload(self.publisher, started["submission_id"], archive)
            finished = self.store.finish_submit(
                self.publisher, started["submission_id"], probe=lambda _url: None,
            )
            self.assertEqual(finished.status, "pending", finished.rules)
            sub_view = self.store.get_submission(self.publisher, started["submission_id"])
            self.assertTrue(
                any(
                    f["rule"] == "DEP_EXTERNAL_UNREACHABLE" and f["severity"] == "warning"
                    for f in sub_view["findings"]
                )
            )

    def test_req03_and_req04_header_ticket_upload_and_withdraw_uploading_or_rejected(self) -> None:
        pkg_dir = self.root / "upload-skill"
        pkg_dir.mkdir()
        manifest = skill_manifest(version="3.3.0", changes="测试请求头票据上传与取消提交")
        (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (pkg_dir / "SKILL.md").write_text(SKILL.replace("3.2.0", "3.3.0"), encoding="utf-8")
        zip_path = self.root / "upload-skill.zip"
        write_zip(pkg_dir, zip_path)
        archive = zip_path.read_bytes()

        # 1. begin_submit 返回免查询串的 upload_url + X-Upload-Ticket 头；用完之前撤回 uploading 会清理临时包与票据。
        begun = self._mcp_call(
            "begin_submit",
            {"manifest": manifest, "byte_size": len(archive)},
            self.publisher,
        )
        self.assertIn("upload_url", begun)
        self.assertNotIn("ticket=", begun["upload_url"])
        put_req = urllib.request.Request(
            begun["upload_url"],
            data=archive,
            method="PUT",
            headers=begun["upload_headers"],
        )
        with urllib.request.urlopen(put_req) as resp:
            self.assertEqual(resp.status, 200)
        staged_file = self.store.blob_dir / "uploads" / f"{begun['submission_id']}.zip"
        self.assertTrue(staged_file.is_file())

        withdrawn = self._mcp_call(
            "withdraw", {"submission_id": begun["submission_id"]}, self.publisher,
        )
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertFalse(staged_file.exists())

        # 2. rejected 状态的提交同样支持 withdraw 清理。
        rejected_sub = self.store.submit(self.publisher, manifest, {})
        self.assertEqual(rejected_sub.status, "rejected")
        self.store.withdraw(self.publisher, rejected_sub.submission_id)
        self.assertEqual(
            self.store.get_submission(self.publisher, rejected_sub.submission_id)["status"],
            "withdrawn",
        )

    def test_plugin_schema_allows_client_only_form_without_entry(self) -> None:
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "store" / "spec" / "store.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("entry", schema["properties"]["plugin"]["required"])


class TrustedProxyHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        self.store.create_admin_account("admin", "StrongPass123", "管理员", "管理员")

    def test_rightmost_untrusted_ip_in_xff_prevents_leftmost_spoofing(self) -> None:
        # 127.0.0.1 在可信反代列表里：客户端伪造最左端 IP 1.1.1.1 / 2.2.2.2，真实反代追加 198.51.100.9。
        server = serve(
            self.store,
            trust_proxy=True,
            trusted_proxies="127.0.0.1,10.37.87.25",
            login_limit=2,
            login_window=300,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_port}"

        for idx in range(3):
            req = urllib.request.Request(
                base + "/v1/login",
                data=json.dumps({"username": "admin", "password": "WrongPassword1"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Forwarded-For": f"203.0.113.{idx + 1}, 198.51.100.9, 10.37.87.25",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req)
            code = err.exception.code
            err.exception.close()
            if idx < 2:
                self.assertEqual(code, 400)
            else:
                self.assertEqual(code, 429, "伪造最左端 XFF 不得绕过真实客户端 IP 的限流桶")

    def test_untrusted_peer_cannot_spoof_forwarded_headers(self) -> None:
        # 只信任 10.37.87.25，不信任 127.0.0.1 直连：即使发不同的 X-Real-IP 也按 127.0.0.1 限流。
        server = serve(
            self.store,
            trust_proxy=True,
            trusted_proxies="10.37.87.25/32",
            login_limit=2,
            login_window=300,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_port}"

        for idx in range(3):
            req = urllib.request.Request(
                base + "/v1/login",
                data=json.dumps({"username": "admin", "password": "WrongPassword1"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Real-IP": f"192.0.2.{idx + 1}",
                    "X-Forwarded-For": f"192.0.2.{idx + 1}",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req)
            code = err.exception.code
            err.exception.close()
            if idx < 2:
                self.assertEqual(code, 400)
            else:
                self.assertEqual(code, 429, "非可信反代来源直连不得伪造 X-Real-IP 绕过限流")


class ConsumerFeedbackReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "store.db", self.root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("鹿溪作者", "publisher")
        _, self.other_pub = self.store.create_principal("其他作者", "publisher")
        _, self.reviewer = self.store.create_principal("审核员", "reviewer")
        _, self.admin = self.store.create_principal("管理员", "admin")

        sub = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.store.approve(self.admin, sub.submission_id)

        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _mcp(self, name: str, arguments: dict, token: str = "") -> tuple[bool, dict]:
        req = urllib.request.Request(
            self.base + "/mcp",
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }).encode(),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        with urllib.request.urlopen(req) as resp:
            body = json.load(resp)
        return bool(body["result"]["isError"]), json.loads(body["result"]["content"][0]["text"])

    def test_full_feedback_lifecycle_permissions_secret_scan_and_audit(self) -> None:
        # 1. 匿名提交反馈（MCP）与查询进度
        is_err, created = self._mcp("report_problem", {
            "id": "luxi.gov-writer",
            "version": "3.2.0",
            "category": "artifact_missing",
            "summary": "下载制品时曾报不可用错误",
            "steps": "调用 GET /v1/artifacts/luxi.gov-writer/3.2.0",
            "client": "antigravity",
            "platform": "darwin-arm64",
            "contact": "ops@example.internal",
        })
        self.assertFalse(is_err, created)
        report_id = created["report_id"]
        self.assertEqual(created["status"], "open")

        is_err, anon_view = self._mcp("get_report", {"report_id": report_id})
        self.assertFalse(is_err)
        self.assertEqual(anon_view["status"], "open")
        self.assertNotIn("contact", anon_view)
        self.assertNotIn("reporter_id", anon_view)

        # 2. 疑似密钥写入反馈正文必须被 SECRET_PATTERN 拒收，不落库
        is_err, leaked = self._mcp("report_problem", {
            "id": "luxi.gov-writer",
            "version": "3.2.0",
            "category": "other",
            "summary": "我的私钥 sk-abcdefghijklmnopqrstuvwxyz123456 报错了",
        })
        self.assertTrue(is_err)
        self.assertEqual(leaked["rule"], "SECRET_PATTERN")

        # 3. 命名空间权限隔离：other_pub 不能看 luxi.* 产品反馈；归属作者可见统计与列表（不含联系方式）
        is_err, denied = self._mcp(
            "list_product_reports", {"id": "luxi.gov-writer"}, self.other_pub,
        )
        self.assertTrue(is_err)
        self.assertEqual(denied["rule"], "ROLE_FORBIDDEN")

        is_err, owner_list = self._mcp("list_product_reports", {}, self.publisher)
        self.assertFalse(is_err, owner_list)
        self.assertEqual(owner_list["total"], 1)
        self.assertEqual(owner_list["by_status"], {"open": 1})
        self.assertEqual(owner_list["by_category"], {"artifact_missing": 1})
        self.assertNotIn("contact", owner_list["items"][0])

        # 4. 实名用户提交后可在 list_my_reports 查看，无关第三方不可按 id 偷看实名反馈
        is_err, auth_created = self._mcp("report_problem", {
            "id": "luxi.gov-writer",
            "version": "3.2.0",
            "category": "doc_mismatch",
            "summary": "get_spec 中缺少 summary 降级说明",
        }, self.other_pub)
        self.assertFalse(is_err, auth_created)
        is_err, mine = self._mcp("list_my_reports", {}, self.other_pub)
        self.assertFalse(is_err)
        self.assertEqual(mine["total"], 1)

        _, third_party = self.store.create_principal("路人", "publisher")
        is_err, snooped = self._mcp(
            "get_report", {"report_id": auth_created["report_id"]}, third_party,
        )
        self.assertTrue(is_err)
        self.assertEqual(snooped["rule"], "ROLE_FORBIDDEN")

        # 5. 审核员分诊：resolved 缺结论拒收；open -> triaged -> resolved 闭环；终态不可逆
        is_err, missing_res = self._mcp("triage_report", {
            "report_id": report_id,
            "status": "resolved",
        }, self.reviewer)
        self.assertTrue(is_err)
        self.assertEqual(missing_res["rule"], "REPORT_INVALID")

        is_err, triaged = self._mcp("triage_report", {
            "report_id": report_id,
            "status": "triaged",
            "resolution": "已复现容器工作目录差异",
        }, self.reviewer)
        self.assertFalse(is_err, triaged)
        self.assertEqual(triaged["status"], "triaged")
        self.assertEqual(triaged["contact"], "ops@example.internal")

        # 通过 HTTP POST /v1/reports/<id>/triage 完成结单
        triage_req = urllib.request.Request(
            f"{self.base}/v1/reports/{report_id}/triage",
            data=json.dumps({
                "status": "resolved",
                "resolution": "已完成逻辑键迁移与 ARTIFACT_MISSING 503 修复",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.reviewer}",
            },
        )
        with urllib.request.urlopen(triage_req) as resp:
            resolved_view = json.load(resp)
        self.assertEqual(resolved_view["status"], "resolved")

        is_err, reopen_err = self._mcp("triage_report", {
            "report_id": report_id,
            "status": "triaged",
        }, self.reviewer)
        self.assertTrue(is_err)
        self.assertEqual(reopen_err["rule"], "REPORT_STATE_INVALID")

        # 6. 审计记录包含 report.create 与 report.triage，且不含正文内容
        audits = self.store.list_audit(self.admin, package_id="luxi.gov-writer")["items"]
        actions = [a["action"] for a in audits]
        self.assertIn("report.create", actions)
        self.assertIn("report.triage", actions)
        self.assertNotIn("ops@example.internal", json.dumps(audits))


if __name__ == "__main__":
    unittest.main()
