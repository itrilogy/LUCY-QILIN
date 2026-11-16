"""阶段 2：同一份上架单的提交与审核。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from store.http_api import serve
from store.service import Store, StoreError

DESC = "说明阐述。这个技能做什么、不做什么，交付物是任务目录中的成稿，并且不会建立 MCP 连接。"
SUMMARY = "六阶段公文流水线，按阶段挂起。"


def skill_manifest(**overrides: object) -> dict:
    body = {
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
        "invoke": {"form": "load_skill", "entry": "SKILL.md"},
    }
    body.update(overrides)
    return body


SKILL = f"---\nname: gov\ndescription: {SUMMARY}\nversion: 3.2.0\n---\n\n# 公文\n"


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = Store(root / "store.db", root / "blobs")
        self.addCleanup(self.store.close)
        _, self.publisher = self.store.create_principal("作者", "publisher")
        _, self.admin = self.store.create_principal("管理员", "admin")

    def test_publisher_cannot_approve(self) -> None:
        result = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.assertEqual(result.status, "pending")
        with self.assertRaises(StoreError) as caught:
            self.store.approve(self.publisher, result.submission_id)
        self.assertEqual(caught.exception.rule, "ROLE_FORBIDDEN")

    def test_invalid_submission_is_rejected_and_hidden(self) -> None:
        result = self.store.submit(
            self.publisher,
            skill_manifest(version="3.2.0 (Agent-Driven Architecture)"),
            {"SKILL.md": SKILL.encode()},
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn("SEMVER", result.rules)
        self.assertEqual(self.store.list_reviews(self.admin), [])
        self.assertEqual(self.store.search(), [])

    def test_approve_then_version_is_immutable(self) -> None:
        result = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.assertEqual(self.store.search(), [])
        self.store.approve(self.admin, result.submission_id)
        found = self.store.search()
        self.assertEqual([item["version"] for item in found], ["3.2.0"])
        with self.assertRaises(StoreError) as caught:
            self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.assertEqual(caught.exception.rule, "VERSION_IMMUTABLE")

    def test_reject_reason_is_visible_and_resubmit_works(self) -> None:
        first = self.store.submit(self.publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.store.reject(self.admin, first.submission_id, "说明还不够具体")
        seen = self.store.get_submission(self.publisher, first.submission_id)
        self.assertEqual(seen["review_reason"], "说明还不够具体")
        skill_33 = SKILL.replace("version: 3.2.0", "version: 3.3.0")
        second = self.store.submit(
            self.publisher,
            skill_manifest(version="3.3.0", changes="补上交付物说明，供审核重看。"),
            {"SKILL.md": skill_33.encode()},
        )
        self.assertEqual(second.status, "pending")
        self.assertEqual(len(self.store.list_reviews(self.admin)), 1)

    def test_author_cannot_impersonate(self) -> None:
        body = skill_manifest()
        body["author"]["publisher_id"] = "someone-else"
        with self.assertRaises(StoreError) as caught:
            self.store.submit(self.publisher, body, {"SKILL.md": SKILL.encode()})
        self.assertEqual(caught.exception.rule, "AUTHOR_MISMATCH")


class HttpTests(ServiceTests):
    def test_http_publish_uses_the_same_store(self) -> None:
        server = serve(self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        base = f"http://127.0.0.1:{server.server_port}"
        created = _post(base + "/v1/submissions", self.publisher, {
            "manifest": skill_manifest(),
            "files": {},
        })
        # files sent separately: the helper below encodes SKILL.md
        self.assertEqual(created["status"], "pending")
        denied = _post(
            base + f"/v1/submissions/{created['submission_id']}/approve",
            self.publisher,
            {},
            expect=403,
        )
        self.assertEqual(denied["rule"], "ROLE_FORBIDDEN")
        _post(
            base + f"/v1/submissions/{created['submission_id']}/approve",
            self.admin,
            {},
        )
        with urllib.request.urlopen(base + "/v1/catalog") as response:
            catalog = json.load(response)
        self.assertEqual(catalog["items"][0]["id"], "luxi.gov-writer")

    def test_unreachable_url_stays_uploading_until_it_opens(self) -> None:
        submission_id, archive = self._uploading(_external_manifest(), _skill_zip())
        held = threading.Event()
        release = threading.Event()

        def blocking(url: str) -> None:
            held.set()
            release.wait(2)
            return None

        outcome: list = []

        def finish() -> None:
            outcome.append(self.store.finish_submit(
                self.publisher, submission_id, probe=blocking,
            ))

        worker = threading.Thread(target=finish)
        worker.start()
        self.addCleanup(release.set)
        self.assertTrue(held.wait(2))
        searched = threading.Thread(target=self.store.search)
        searched.start()
        searched.join(0.5)
        self.assertFalse(searched.is_alive())
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].status, "uploading")
        self.assertEqual(outcome[0].rules, ["DEP_EXTERNAL_UNREACHABLE"])
        self.assertTrue(archive.is_file())
        self.assertEqual(self.store.list_reviews(self.admin), [])
        opened = self.store.finish_submit(self.publisher, submission_id, probe=lambda url: 200)
        self.assertEqual(opened.status, "pending")
        self.assertEqual(opened.rules, [])
        self.assertEqual(self.store.get_submission(self.publisher, submission_id)["status"], "pending")

    def test_other_failures_still_reject_when_a_url_is_down(self) -> None:
        submission_id, archive = self._uploading(_external_manifest(), _skill_zip(include_skill=False))
        result = self.store.finish_submit(self.publisher, submission_id, probe=lambda url: None)
        self.assertEqual(result.status, "rejected")
        self.assertIn("DEP_EXTERNAL_UNREACHABLE", result.rules)
        self.assertIn("SKILL_ENTRY_MISSING", result.rules)
        self.assertFalse(archive.is_file())
        with self.assertRaises(StoreError) as caught:
            self.store.finish_submit(self.publisher, submission_id, probe=lambda url: 200)
        self.assertEqual(caught.exception.rule, "VERSION_UNAVAILABLE")

    def _uploading(self, manifest: dict, archive: bytes) -> tuple[str, Path]:
        started = self.store.begin_submit(
            self.publisher, manifest, submit_kind="new_version", byte_size=len(archive),
        )
        self.assertEqual(started["status"], "uploading", started)
        self.store.save_upload(self.publisher, started["submission_id"], archive)
        path = self.store.blob_dir / "uploads" / f"{started['submission_id']}.zip"
        self.assertTrue(path.is_file())
        return started["submission_id"], path


def _external_manifest() -> dict:
    body = skill_manifest()
    body["depends"] = {
        "store": [],
        "skills": [],
        "mcp": [],
        "external": [{
            "name": "pandoc",
            "version_range": ">=3",
            "detect": ["pandoc", "--version"],
            "install_guide": "从发布页安装 pandoc，并保证命令 pandoc 在 PATH 中。",
            "resources": [{"title": "Pandoc", "url": "https://example.com/pandoc", "platform": "any"}],
        }],
    }
    return body


def _skill_zip(include_skill: bool = True) -> bytes:
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if include_skill:
            archive.writestr("SKILL.md", SKILL)
        else:
            archive.writestr("README.md", "没有入口")
    return buffer.getvalue()


def _post(url: str, token: str, body: dict, expect: int = 200) -> dict:
    if url.endswith("/submissions") and "files" in body and not body["files"]:
        body = {
            "manifest": body["manifest"],
            "files": {"SKILL.md": __import__("base64").b64encode(SKILL.encode()).decode()},
        }
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
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


if __name__ == "__main__":
    unittest.main()
