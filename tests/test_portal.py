"""门户和接口读同一份上架单。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.http_api import serve
from store.service import Store
from tests.test_service import SUMMARY, skill_manifest

SKILL = f"---\nname: gov\ndescription: {SUMMARY}\nversion: 3.2.0\n---\n\n# 公文\n"


class PortalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")
        self.server = serve(self.store)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def test_page_is_served_with_the_same_api(self) -> None:
        with urllib.request.urlopen(self.base + "/") as response:
            csp = response.headers.get("Content-Security-Policy", "")
            page = response.read().decode()
        self.assertIn("栖林", page)
        self.assertIn("我的提交", page)
        self.assertIn("货架陈列已经发布的技能、MCP、知识包与插件", page)
        self.assertIn("均需要登录后，才能在页面进行下载", page)
        self.assertIn("用户管理", page)
        self.assertIn("商店 MCP", page)
        self.assertIn("装到这台电脑", page)
        self.assertIn("用户管理", page)
        self.assertIn("口令", page)
        self.assertIn("必填", page)
        self.assertIn("10 到 64 位", page)
        self.assertIn("重置口令", page)
        self.assertIn("这是一份技能，从 SKILL.md 读起", page)
        self.assertIn("远程服务不用交文件", page)
        self.assertIn("先选类型", page)
        self.assertIn("只有运行时可以给同一个版本补一个平台的文件", page)
        self.assertIn("知识包", page)
        self.assertIn('data-kind="plugin"', page)
        self.assertIn('id="plugin-fields"', page)
        self.assertIn('id="f-plugin-dir"', page)
        self.assertIn('id="audit"', page)
        self.assertIn('id="namespace-transfer"', page)
        self.assertIn('id="audit-export"', page)
        self.assertIn('id="sort"', page)
        self.assertIn('id="shelf-more"', page)
        self.assertIn('id="connect-tabs"', page)
        self.assertIn('data-tab="dsh-read"', page)
        self.assertIn('data-tab="dsh-publish"', page)
        self.assertIn('data-tab="generic-read"', page)
        self.assertIn('data-tab="generic-publish"', page)
        self.assertIn('id="connect-keypanel"', page)
        self.assertIn('id="reissue-key"', page)
        self.assertIn('id="author-card"', page)
        self.assertIn('id="contributions"', page)
        self.assertIn('id="nav-contributions"', page)
        self.assertIn('class="form-nav"', page)
        self.assertIn('id="form-identity"', page)
        self.assertIn('id="form-about"', page)
        self.assertNotIn("connect-snippet", page)
        self.assertIn('class="skip-link"', page)
        self.assertIn('id="main"', page)
        self.assertIn("加载更多", page)
        self.assertIn("选择技能目录", page)

        self.assertIn('/portal/styles.css', page)
        self.assertIn('/portal/app.js', page)
        self.assertNotIn("<style>", page)
        self.assertNotIn("<script>", page)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("同一份上架单", page)
        self.assertNotIn("再输入一次 KEY", page)
        self.assertNotIn("枢纽令牌", page)
        self.assertNotIn("未接服务器", page)
        with urllib.request.urlopen(self.base + "/portal/styles.css") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("text/css", response.headers.get("Content-Type", ""))
            styles = response.read().decode()
        with urllib.request.urlopen(self.base + "/portal/app.js") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("javascript", response.headers.get("Content-Type", ""))
            app_js = response.read().decode()
        self.assertIn("/v1/catalog", app_js)
        self.assertIn("请先登录", app_js)
        self.assertIn("从实验室账号进来的", app_js)
        self.assertIn("在 DSH 的 Plugins 里安装", app_js)
        self.assertIn("插件目录根上要有 package.json", app_js)
        self.assertIn("Authorization: Bearer", app_js)
        self.assertIn("qilin-store", app_js)
        self.assertIn("qilin-publish", app_js)
        self.assertIn("loadContributions", app_js)
        self.assertIn("贡献明细", app_js)
        self.assertIn("插件审核清单", app_js)
        self.assertIn("记录审核", app_js)
        self.assertIn("@media (max-width: 900px)", styles)
        with urllib.request.urlopen(self.base + "/design/tokens.css") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("--luxi-green", response.read().decode())
        me = json.load(urllib.request.urlopen(self.base + "/v1/me"))
        self.assertEqual(me["role"], "anonymous")

    def test_review_sees_the_submitted_sheet_and_diff(self) -> None:
        first = self._publish(skill_manifest(), SKILL)
        self.assertEqual(first["summary"], SUMMARY)
        revised_summary = "这一版把挂起写进了说明"
        description = "说明阐述。这一版把挂起写进了说明。交付物是任务目录中的成稿，并且不会建立任何连接。"
        skill = f"---\nname: gov\ndescription: {revised_summary}\nversion: 3.3.0\n---\n\n# 公文\n"
        created = self._submit(skill_manifest(
            version="3.3.0",
            summary=revised_summary,
            description=description,
            changes="把挂起写进了版本说明。",
        ), skill)
        self.assertEqual(created["status"], "pending")
        review = self._get(f"/v1/reviews/{created['submission_id']}", self.admin)
        self.assertEqual(review["manifest"]["summary"], revised_summary)
        self.assertEqual(review["manifest"]["id"], "luxi.gov-writer")
        self.assertTrue(any(item["path"] == "summary" for item in review["diff"]))
        queue = self._get("/v1/reviews", self.admin)["items"]
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["title"], "公文撰写")
        again = self._send(
            f"/v1/submissions/{created['submission_id']}/revise",
            self.publisher,
            {
                "manifest": skill_manifest(
                    version="3.3.0",
                    summary=revised_summary,
                    description=description,
                    changes="修订后仍是这一条提交。",
                ),
                "files": {"SKILL.md": _b64(skill)},
            },
        )
        self.assertEqual(again["submission_id"], created["submission_id"])
        self.assertEqual(len(self._get("/v1/reviews", self.admin)["items"]), 1)
        self._send(f"/v1/submissions/{created['submission_id']}/reject", self.admin, {"reason": "阐述再具体一点"})
        seen = self._get(f"/v1/submissions/{created['submission_id']}", self.publisher)
        self.assertEqual(seen["review_reason"], "阐述再具体一点")
        self.assertEqual(self._get("/v1/reviews", self.admin)["items"], [])

    def test_new_id_publishes_without_confirmation(self) -> None:
        # 商品身份由清单 id 决定；新 id 不需要先登记，也不需要人工确认。
        body = skill_manifest(
            id="luxi.not-registered",
            kind="mcp",
            version="1.0.0",
            clients=["cursor"],
            platforms=["any"],
            skill=None,
            invoke={"form": "list_tools"},
            mcp={"transport": "streamable-http", "url": "https://mcp.example.internal/market"},
        )
        body.pop("skill")
        created = self._submit(body, None)
        self.assertEqual(created["status"], "pending")
        self._send(
            f"/v1/submissions/{created['submission_id']}/approve",
            self.admin,
            {},
        )
        catalog = json.load(urllib.request.urlopen(self.base + "/v1/catalog"))
        self.assertEqual(catalog["items"][0]["id"], "luxi.not-registered")
        self.assertIn("cursor", catalog["items"][0]["clients"])

    def test_fresh_store_does_not_mint_keys(self) -> None:
        root = Path(self.tmp.name) / "fresh"
        root.mkdir()
        store = Store(root / "store.db", root / "blobs")
        self.addCleanup(store.close)
        self.assertFalse(store.has_principals())
        self.assertEqual(store.identity_status()["mode"], "unset")
        me = json.load(urllib.request.urlopen(self.base + "/v1/identity"))
        self.assertEqual(me["mode"], "unset")
        self.assertEqual(me["app_id"], "lucy-qilin")

    def test_http_withdraw_and_unpublish_routes(self) -> None:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("SKILL.md", SKILL)
        archive = buf.getvalue()
        begun = self._send(
            "/v1/submissions/begin",
            self.publisher,
            {"manifest": skill_manifest(), "byte_size": len(archive)},
        )
        self.assertEqual(begun["status"], "uploading")
        put_req = urllib.request.Request(
            begun["put_url"],
            data=archive,
            method="PUT",
            headers={"Authorization": f"Bearer {self.publisher}"},
        )
        with urllib.request.urlopen(put_req) as resp:
            self.assertEqual(resp.status, 200)
        created = self._send(f"/v1/submissions/{begun['submission_id']}/finish", self.publisher, {})
        self.assertEqual(created["status"], "pending")
        withdrawn = self._send(f"/v1/submissions/{created['submission_id']}/withdraw", self.publisher, {})
        self.assertEqual(withdrawn["status"], "withdrawn")
        seen = self._get(f"/v1/submissions/{created['submission_id']}", self.publisher)
        self.assertEqual(seen["status"], "withdrawn")

        second = self._submit(skill_manifest(version="3.3.0"), SKILL.replace("3.2.0", "3.3.0"))
        self._send(f"/v1/submissions/{second['submission_id']}/approve", self.admin, {})
        self.assertEqual(self._get("/v1/catalog")["total"], 1)
        unpublished = self._send(
            "/v1/packages/luxi.gov-writer/3.3.0/unpublish", self.admin, {"reason": "发现问题下架"}
        )
        self.assertEqual(unpublished["status"], "unpublished")
        self.assertEqual(self._get("/v1/catalog")["total"], 0)

    def _publish(self, manifest: dict, skill: str) -> dict:
        created = self._submit(manifest, skill)
        self._send(f"/v1/submissions/{created['submission_id']}/approve", self.admin, {})
        return self._get(f"/v1/packages/{manifest['id']}/{manifest['version']}")

    def _submit(self, manifest: dict, skill: str | None) -> dict:
        files = {} if skill is None else {"SKILL.md": _b64(skill)}
        return self._send("/v1/submissions", self.publisher, {"manifest": manifest, "files": files})

    def _get(self, path: str, token: str | None = None) -> dict:
        request = urllib.request.Request(self.base + path)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def _send(self, path: str, token: str, body: dict, expect: int = 200) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            payload = json.load(exc)
            exc.close()
            if exc.code != expect:
                raise
            return payload


def _b64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()


if __name__ == "__main__":
    unittest.main()
