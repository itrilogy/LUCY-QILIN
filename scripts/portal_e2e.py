#!/usr/bin/env python3
"""门户真实浏览器 E2E（可选，不跑在 CI）。

前置：商店在 127.0.0.1:8787 运行；本机有 Microsoft Edge；装了 Python Playwright。
用法：
  PYTHONPATH=/tmp/pw:. python3 scripts/portal_e2e.py
"""

from __future__ import annotations

import base64
import json
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8787"
REVIEWER = json.loads((ROOT / "var/accounts/reviewer.json").read_text(encoding="utf-8"))
PROFILE = Path.home() / ".dsh/profiles/desktop/cordis.patch.yml"


def admin_key() -> str:
    text = PROFILE.read_text(encoding="utf-8")
    for block in text.split("- id:"):
        if "serverName: QILIN" in block:
            match = re.search(r"Authorization:\s*Bearer\s+([^\s\"']+)", block)
            if match:
                return match.group(1).strip().strip("\"'")
    raise RuntimeError("profile 里没有 QILIN admin key")


def post(path: str, body: dict, key: str) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode())


def pending_plugin(key: str) -> str:
    files = {
        "package.json": json.dumps({
            "name": "@e2e/portal-check",
            "version": "1.0.0",
            "private": True,
            "type": "module",
            "exports": {".": "./index.js", "./client": "./client.js"},
            "dsh": {"bundle": {"patch": "./cordis.patch.yml"}},
        }).encode(),
        "index.js": b"export function apply() {}\n",
        "client.js": b"export function apply() {}\n",
        "cordis.patch.yml": b"- insert: []\n",
    }
    manifest = {
        "spec_version": 1,
        "kind": "plugin",
        "id": "e2e.portal-check",
        "version": "1.0.0",
        "title": "门户 E2E 插件",
        "summary": "用于验证门户插件表单与审核清单。",
        "description": "这个插件只用于浏览器端验收，验证门户的插件上架与审核清单界面，不执行任何业务逻辑。",
        "changes": "首个发布，仅用于门户 E2E。",
        "author": {"display_name": "E2E"},
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
            "files": ["package.json", "index.js", "client.js", "cordis.patch.yml"],
            "dsh": ">=1.5 <2",
            "capabilities": [],
            "external": [],
        },
    }
    encoded = {name: base64.b64encode(data).decode() for name, data in files.items()}
    result = post("/v1/submissions", {"manifest": manifest, "files": encoded, "submit_kind": "new_version"}, key)
    if result.get("status") != "pending":
        raise RuntimeError(f"创建待审插件失败：{result}")
    return result["submission_id"]


def main() -> int:
    key = admin_key()
    submission_id = pending_plugin(key)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(BASE + "/", wait_until="networkidle")
            page.wait_for_selector("#shelf-grid .card")

            # 匿名货架：插件分类与 URL 状态
            page.click('#kind-chips button[data-kind="plugin"]')
            page.wait_for_selector("#shelf-grid .card")
            assert "kind=plugin" in page.url, page.url
            assert page.locator("#kind-chips button[data-kind='plugin']").get_attribute("aria-pressed") == "true"
            page.reload(wait_until="networkidle")
            page.wait_for_selector("#shelf-grid .card")
            assert page.locator("#kind-chips button[data-kind='plugin']").get_attribute("aria-pressed") == "true"

            # 插件详情
            page.locator("#shelf-grid .card .btn.primary").first.click()
            page.wait_for_selector("#detail-sheet .group")
            assert "在 DSH 的 Plugins 里安装" in page.locator("#detail-sheet").inner_text()
            page.click("#back-shelf")

            # 窄屏：侧栏横向、货架单列
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.eval_on_selector(".nav", "el => getComputedStyle(el).flexDirection") == "row"
            columns = page.eval_on_selector(".grid", "el => getComputedStyle(el).gridTemplateColumns")
            assert len(columns.split()) == 1, columns
            page.set_viewport_size({"width": 1280, "height": 900})

            # 登录 reviewer，检查插件表单
            page.click("#login-open")
            page.fill("#login-username", REVIEWER["username"])
            page.fill("#login-password", REVIEWER["password"])
            page.click("#login-submit")
            page.wait_for_function("document.querySelector('#who').textContent.includes('审核')")
            page.click("#new-product")
            page.select_option("#f-kind", "plugin")
            assert page.locator("#plugin-fields").is_visible()
            assert page.locator("#f-plugin-dir").count() == 1
            assert page.input_value("#f-platforms") == "any"
            assert page.input_value("#f-clients") == "deepseek-harness"

            # 审核清单 UI：记录 8 项，再退回
            page.click("#nav-review")
            page.wait_for_selector("#review-queue .list-btn")
            queue = page.locator("#review-queue .list-btn")
            found = False
            for index in range(queue.count()):
                if "e2e.portal-check" in queue.nth(index).inner_text():
                    queue.nth(index).click()
                    found = True
                    break
            assert found, "审核队列里没有 E2E 插件"
            page.wait_for_selector("#review-detail input[type='checkbox']")
            assert page.locator("#review-detail input[type='checkbox']").count() == 8
            for index in range(8):
                page.locator("#review-detail input[type='checkbox']").nth(index).check()
            page.get_by_role("button", name="记录审核").click()
            page.wait_for_function("document.body.innerText.includes('已记录审核清单')")
            page.fill("#review-detail textarea", "E2E 验证完成，退回清理。")
            page.get_by_role("button", name="退回").click()
            page.wait_for_function("document.querySelector('#review-state').textContent.includes('没有待审核')")
            browser.close()
        print("portal e2e: ok")
        return 0
    finally:
        # 失败时尽量把待审插件退回，避免留在队列里。
        try:
            post(f"/v1/submissions/{submission_id}/reject", {"reason": "E2E 清理"}, key)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
