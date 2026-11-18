"""参考安装器只执行闭合的 op。"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from store.installer import run
from tests.test_plan import StorePlanTests


class InstallerTests(unittest.TestCase):
    def test_unknown_op_stops_before_later_steps(self) -> None:
        seen = {"count": 0}

        def opener(url: str, timeout: int = 30):
            seen["count"] += 1
            raise AssertionError(url)

        code = run({"steps": [{"op": "format-disk"}, {"op": "download", "url": "http://x"}]}, opener=opener)
        self.assertEqual(code, 2)
        self.assertEqual(seen["count"], 0)

    def test_checksum_mismatch_does_not_unpack(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        archive = root / "package.zip"
        unpacked = root / "skill"

        class Response:
            """极简响应体：只读一次，之后返回空串表示 EOF。"""

            def __init__(self) -> None:
                self.sent = False

            def read(self, size: int = -1) -> bytes:
                if self.sent:
                    return b""
                self.sent = True
                return b"not-the-zip"

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

        code = run({
            "steps": [
                {
                    "op": "download",
                    "url": "http://store.example/package.zip",
                    "sha256": hashlib.sha256(b"the-zip").hexdigest(),
                    "dest": str(archive),
                },
                {"op": "unpack", "src": str(archive), "dest": str(unpacked)},
            ],
        }, opener=lambda url, timeout=30: Response())
        self.assertEqual(code, 1)
        self.assertFalse(archive.exists())
        self.assertFalse(unpacked.exists())

    def test_missing_command_prints_the_guide_and_does_not_download(self) -> None:
        hits = {"count": 0}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                hits["count"] += 1
                self.send_response(200)
                self.end_headers()

            def log_message(self, fmt: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        url = f"http://127.0.0.1:{server.server_port}/pandoc.pkg"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run({"steps": [{
                "op": "ensure_bin",
                "name": "pandoc",
                "detect": ["qilin-missing-pandoc"],
                "install_guide": "从下面的地址安装 pandoc，并保证命令在 PATH 中。",
                "resources": [{"title": "Pandoc", "url": url, "platform": "any"}],
            }]})
        self.assertEqual(code, 1)
        self.assertIn("从下面的地址安装 pandoc", buffer.getvalue())
        self.assertIn(url, buffer.getvalue())
        self.assertEqual(hits["count"], 0)

    def test_dry_run_prints_three_kinds(self) -> None:
        host = StorePlanTests()
        host.setUp()
        self.addCleanup(host.store.close)
        self.addCleanup(host.tmp.cleanup)
        host.test_three_kinds_have_the_spec_op_order()
        expected = {
            "luxi.gov-writer": ["download", "unpack", "verify"],
            "luxi.market-mcp": ["mcp_add", "verify"],
            "luxi.slide-master": ["download", "place_bin", "mcp_add", "download", "unpack", "verify"],
        }
        for package_id, version, client in (
            ("luxi.gov-writer", "3.2.0", "reasonix"),
            ("luxi.market-mcp", "1.0.0", "cursor"),
            ("luxi.slide-master", "1.0.0", "reasonix"),
        ):
            plan = host.store.make_install_plan(
                package_id, version, client=client, platform="darwin-arm64",
                base_url="http://store.example",
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = run(plan, dry_run=True)
            self.assertEqual(code, 0)
            printed = [json.loads(line)["op"] for line in buffer.getvalue().splitlines()]
            self.assertEqual(printed, expected[package_id])

    def test_mcp_add_writes_only_connection_fields(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        code = run({
            "steps": [{
                "op": "mcp_add",
                "id": "luxi.sample-mcp",
                "transport": "stdio",
                "command": "uvx",
                "args": ["sample"],
                "secrets": ["API_KEY"],
                "mcp_dir": str(root / "mcp"),
            }],
        }, bin_dir=root / "bin", skills_root=root / "skills", mcp_dir=root / "mcp")
        self.assertEqual(code, 0)
        payload = json.loads((root / "mcp" / "luxi.sample-mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"id", "transport", "command", "args", "secrets"})
        self.assertNotIn("bin_dir", payload)
        self.assertNotIn("skills_root", payload)

    def test_skill_verify_requires_a_skills_root(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        with redirect_stderr(io.StringIO()):
            code = run({"steps": [{
                "op": "verify",
                "id": "luxi.gov-writer",
                "version": "3.2.0",
                "kind": "skill",
                "entry": "SKILL.md",
            }]})
        self.assertEqual(code, 1)

    def test_skill_verify_checks_the_entry_and_does_not_connect(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))

        def opener(*args: object, **kwargs: object) -> object:
            raise AssertionError("技能校验不应该连接")

        with redirect_stderr(io.StringIO()):
            missing = run({
                "steps": [{
                    "op": "verify",
                    "id": "luxi.gov-writer",
                    "version": "3.2.0",
                    "kind": "skill",
                    "entry": "SKILL.md",
                }],
            }, skills_root=root, opener=opener)
        self.assertEqual(missing, 1)
        entry = root / "luxi.gov-writer" / "3.2.0" / "SKILL.md"
        entry.parent.mkdir(parents=True)
        entry.write_text("# 公文\n", encoding="utf-8")
        found = run({
            "steps": [{
                "op": "verify",
                "id": "luxi.gov-writer",
                "version": "3.2.0",
                "kind": "skill",
                "entry": "SKILL.md",
            }],
        }, skills_root=root, opener=opener)
        self.assertEqual(found, 0)

    def test_http_verify_lists_tools_and_a_refusal_stops(self) -> None:
        seen: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                method = body.get("method")
                seen.append(method)
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method != "initialize" and self.headers.get("Mcp-Session-Id") != "sess":
                    payload = {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32001, "message": "no session"}}
                elif method == "initialize":
                    payload = {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}}
                elif method == "tools/list":
                    payload = {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "search"}]}}
                else:
                    payload = {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "no"}}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "sess")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        url = f"http://127.0.0.1:{server.server_port}/mcp"
        later = {"count": 0}
        code = run({"steps": [_mcp_verify(url)]})
        self.assertEqual(code, 0)
        self.assertEqual(seen, ["initialize", "notifications/initialized", "tools/list"])

        def refuse(request: object, timeout: int = 30) -> object:
            later["count"] += 1
            raise urllib.error.URLError("refused")

        errors = io.StringIO()
        with redirect_stderr(errors):
            stopped = run({"steps": [_mcp_verify("http://127.0.0.1/mcp"), {"op": "download", "url": "http://store.example/next"}]}, opener=refuse)
        self.assertEqual(stopped, 1)
        self.assertEqual(later["count"], 1)
        self.assertIn("连不上 MCP", errors.getvalue())

    def test_stdio_verify_lists_tools_and_a_dead_process_stops(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        script = root / "server.py"
        script.write_text(
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    msg = json.loads(line)\n"
            "    method = msg.get('method')\n"
            "    if method == 'notifications/initialized':\n"
            "        continue\n"
            "    result = {'protocolVersion': '2025-03-26'} if method == 'initialize' else {'tools': [{'name': 'search'}]}\n"
            "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}) + '\\n')\n"
            "    sys.stdout.flush()\n"
            "    if method == 'tools/list':\n"
            "        break\n",
            encoding="utf-8",
        )
        code = run({"steps": [{
            "op": "verify",
            "id": "luxi.market-mcp",
            "version": "1.0.0",
            "kind": "mcp",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
        }]})
        self.assertEqual(code, 0)
        errors = io.StringIO()
        with redirect_stderr(errors):
            dead = run({"steps": [{
                "op": "verify",
                "id": "luxi.market-mcp",
                "version": "1.0.0",
                "kind": "mcp",
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-c", "import sys; sys.exit(1)"],
            }]})
        self.assertEqual(dead, 1)
        self.assertIn("不能列出工具", errors.getvalue())


def _mcp_verify(url: str) -> dict:
    return {
        "op": "verify",
        "id": "luxi.market-mcp",
        "version": "1.0.0",
        "kind": "mcp",
        "transport": "streamable-http",
        "url": url,
    }


if __name__ == "__main__":
    unittest.main()
