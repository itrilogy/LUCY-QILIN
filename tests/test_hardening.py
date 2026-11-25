"""压缩包路径、知识树预留和审核预览。"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from store.blobs import atomic_write, safe_extract, write_zip
from store.installer import _unpack
from store.plan import install_plan
from store.safety import PathEscape, safe_join
from store.service import Store, StoreError
from store.validator.knowledge import resolve_knowledge_tree_path
from store.validator.validate import validate_manifest, validate_path
from tests.test_knowledge import _manifest, _note, _pack
from tests.test_service import SKILL, SUMMARY, skill_manifest


def _skill(version: str, body: str) -> bytes:
    text = (
        "---\n"
        "name: gov\n"
        f"description: {SUMMARY}\n"
        f"version: {version}\n"
        "---\n\n"
        f"{body}\n"
    )
    return text.encode()


class HardeningTests(unittest.TestCase):
    def test_write_zip_skips_dest_and_junk(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        (root / "keep.md").write_text("ok", encoding="utf-8")
        (root / ".DS_Store").write_bytes(b"junk")
        (root / "._foo").write_bytes(b"junk")
        macos = root / "__MACOSX"
        macos.mkdir()
        (macos / "keep.md").write_bytes(b"junk")
        dest = root / "out.zip"
        write_zip(root, dest)
        names = zipfile.ZipFile(dest).namelist()
        self.assertIn("keep.md", names)
        self.assertNotIn("out.zip", names)
        self.assertNotIn(".DS_Store", names)
        self.assertNotIn("._foo", names)
        self.assertFalse(any(name.startswith("__MACOSX") for name in names))

    def test_bin_name_rejects_path(self) -> None:
        for bin_name in ("../x", "a/b"):
            body = skill_manifest(
                id="luxi.officecli",
                kind="runtime",
                version="1.4.0",
                platforms=["darwin-arm64"],
                skill=None,
                runtime={
                    "bin_name": bin_name,
                    "assets": [{
                        "platform": "darwin-arm64",
                        "path": "darwin-arm64/officecli",
                        "sha256": "ab" * 32,
                    }],
                },
                invoke={"form": "exec_bin", "bin_name": bin_name},
            )
            body.pop("skill", None)
            self.assertIn("RUNTIME_ASSET", validate_manifest(body).rules())

    def test_knowledge_tree_path_stays_inside_segments(self) -> None:
        full = _manifest()
        full["knowledge"] = {
            "entry": "00-导航概览.md",
            "taxonomy": {
                "business_category": "制度",
                "business_domain": "公文",
                "knowledge_kind": "操作指引",
            },
        }
        self.assertEqual(
            resolve_knowledge_tree_path(full),
            "制度/公文/操作指引/luxi.sample-notes/1.0.0",
        )
        self.assertEqual(
            resolve_knowledge_tree_path(_manifest()),
            "knowledge-ops/method/guide/luxi.sample-notes/1.0.0",
        )
        for bad_segment in ("..", "制度/公文"):
            bad = _manifest()
            bad["knowledge"] = {
                "entry": "00-导航概览.md",
                "taxonomy": {
                    "business_category": bad_segment,
                    "business_domain": "公文",
                    "knowledge_kind": "操作指引",
                },
            }
            path = resolve_knowledge_tree_path(bad)
            self.assertTrue(path.startswith("_unclassified/"))
            self.assertNotIn("..", Path(path).parts)
            self.assertIn("KNOWLEDGE_TAXONOMY", validate_manifest(bad).rules())

    def test_knowledge_junk_is_not_a_note_file(self) -> None:
        root = _pack({
            "00-导航概览.md": _note("导航", ["01-概念"], type="map_of_content"),
            "01-概念.md": _note("概念", ["00-导航概览"]),
        })
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        (root / ".DS_Store").write_bytes(b"junk")
        (root / "._foo").write_bytes(b"junk")
        macos = root / "__MACOSX"
        macos.mkdir()
        (macos / "00-导航概览.md").write_text("junk", encoding="utf-8")
        self.assertEqual(validate_path(root).rules(), [])

    def test_review_previews_changed_files_and_drops_junk(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = Store(root / "store.db", root / "blobs")
        self.addCleanup(store.close)
        _, publisher = store.create_principal("作者", "publisher")
        _, admin = store.create_principal("管理员", "admin")
        first = store.submit(publisher, skill_manifest(), {"SKILL.md": SKILL.encode()})
        self.assertEqual(first.status, "pending")
        store.approve(admin, first.submission_id)
        second = store.submit(
            publisher,
            skill_manifest(version="3.3.0", changes="这一版修订了技能正文。"),
            {
                "SKILL.md": _skill("3.3.0", "# 公文修订"),
                "references/a.md": "补充说明。\n".encode(),
                ".DS_Store": b"junk",
            },
        )
        self.assertEqual(second.status, "pending", second.rules)
        sheet = store.get_review(admin, second.submission_id)
        self.assertNotIn(".DS_Store", sheet["files"])
        self.assertIn("公文修订", sheet["files_preview"]["SKILL.md"])
        by_path = {item["path"]: item["status"] for item in sheet["file_diffs"]}
        self.assertEqual(by_path["SKILL.md"], "modified")
        self.assertEqual(by_path["references/a.md"], "added")
        self.assertNotIn("manifest.json", by_path)
        with self.assertRaises(StoreError) as caught:
            store.submit(
                publisher,
                skill_manifest(version="3.4.0", changes="路径不合法。"),
                {"SKILL.md": _skill("3.4.0", "# 公文"), "../evil.txt": b"x"},
            )
        self.assertEqual(caught.exception.rule, "CHECKSUM")

    def test_extract_rejects_parent_entry(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        parent = Path(tmp.name)
        archive = parent / "bad.zip"
        dest = parent / "out"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("../evil.txt", "no")
        with self.assertRaises(ValueError):
            safe_extract(archive, dest)
        self.assertFalse((parent / "evil.txt").exists())
        self.assertEqual(_unpack({"src": str(archive), "dest": str(dest)}, None), 1)
        self.assertFalse((parent / "evil.txt").exists())

    def test_tree_dest_does_not_replace_install_dest(self) -> None:
        release = {
            "id": "luxi.sample-notes",
            "version": "1.0.0",
            "clients": ["reasonix"],
            "platforms": ["any"],
            "artifact_sha256": "ab" * 32,
            "manifest": _manifest(),
        }
        plan = install_plan(
            [release],
            package_id="luxi.sample-notes",
            version="1.0.0",
            client="reasonix",
            platform="darwin-arm64",
            base_url="http://store.example",
        )
        unpack = plan["steps"][1]
        self.assertTrue(str(unpack["dest"]).endswith("luxi.sample-notes/1.0.0"))
        self.assertNotIn("_unclassified", unpack["dest"])
        self.assertTrue(unpack["tree_dest"].endswith("knowledge-ops/method/guide/luxi.sample-notes/1.0.0"))


    def test_safe_join_rejects_escapes(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name) / "base"
        base.mkdir()
        for bad in ("/etc/passwd", "../x", "a/../../x", "", "   ", None, 123, "C:/x", " a", "a\x00b"):
            with self.subTest(bad=bad), self.assertRaises(PathEscape):
                safe_join(base, bad)  # type: ignore[arg-type]
        self.assertEqual(safe_join(base, "a/b"), (base / "a" / "b").resolve())
        self.assertEqual(safe_join(base, "a\\b"), (base / "a" / "b").resolve())

    def _runtime_manifest(self, **runtime: object) -> dict:
        body = skill_manifest(
            id="luxi.officecli",
            kind="runtime",
            version="1.4.0",
            platforms=["darwin-arm64"],
            skill=None,
            invoke={"form": "exec_bin", "bin_name": "officecli"},
        )
        body.pop("skill", None)
        body["runtime"] = {"bin_name": "officecli", **(runtime or {})}
        return body

    def test_runtime_asset_path_must_stay_inside(self) -> None:
        digest = "ab" * 32
        for bad_path in ("/etc/passwd", "../outside", "darwin-arm64/../../outside", ""):
            body = self._runtime_manifest(
                assets=[{"platform": "darwin-arm64", "path": bad_path, "sha256": digest}]
            )
            rules = validate_manifest(body).rules()
            self.assertIn("RUNTIME_ASSET", rules, bad_path)

    def test_runtime_asset_platform_must_be_known_and_declared(self) -> None:
        digest = "ab" * 32
        # 未登记、未声明、带路径的形状都必须被拒。
        for bad_platform in ("../escape", "C:/evil", "plan9-mips", "windows-amd64", 123):
            body = self._runtime_manifest(
                assets=[{
                    "platform": bad_platform,
                    "path": "darwin-arm64/officecli",
                    "sha256": digest,
                }]
            )
            rules = validate_manifest(body).rules()
            self.assertIn("RUNTIME_ASSET", rules, repr(bad_platform))

    def test_runtime_asset_platform_must_not_repeat(self) -> None:
        digest = "ab" * 32
        body = self._runtime_manifest(
            assets=[
                {"platform": "darwin-arm64", "path": "a/officecli", "sha256": digest},
                {"platform": "darwin-arm64", "path": "b/officecli", "sha256": digest},
            ]
        )
        self.assertIn("RUNTIME_ASSET", validate_manifest(body).rules())

    def test_keep_assets_refuses_to_write_outside_blobs(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = Store(root / "store.db", root / "blobs")
        self.addCleanup(store.close)
        package = root / "pkg"
        package.mkdir()
        (package / "officecli").write_bytes(b"bin")
        escaped = root / "escaped"
        escaped.mkdir()
        cases = [
            {"platform": "darwin-arm64", "path": str(escaped / "evil.bin")},
            {"platform": "..", "path": "officecli"},
            {"platform": "darwin-arm64", "path": "../pkg/officecli"},
        ]
        for asset in cases:
            manifest = {"kind": "runtime", "runtime": {"assets": [asset]}}
            with self.subTest(asset=asset), self.assertRaises(StoreError) as caught:
                store._keep_assets("sub1", package, manifest)
            self.assertEqual(caught.exception.rule, "RUNTIME_ASSET")
        self.assertEqual(list(escaped.iterdir()), [])
        self.assertFalse((root / "pkg" / "evil.bin").exists())

    def test_atomic_write_leaves_no_part_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "artifact.zip"
            atomic_write(target, b"first")
            atomic_write(target, b"second")
            self.assertEqual(target.read_bytes(), b"second")
            self.assertEqual(list(target.parent.glob("*.part")), [])

    def test_atomic_write_keeps_old_content_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "artifact.zip"
            atomic_write(target, b"good")
            with mock.patch("os.fsync", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    atomic_write(target, b"broken")
            self.assertEqual(target.read_bytes(), b"good", "失败时旧文件必须完好")
            self.assertEqual(list(target.parent.glob("*.part")), [], "失败不能留下 .part")

    def test_write_zip_leaves_no_part_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "keep.md").write_text("ok", encoding="utf-8")
            dest = root / "out.zip"
            digest = write_zip(root, dest)
            self.assertTrue(dest.is_file())
            self.assertEqual(list(root.glob("*.part")), [])
            self.assertEqual(digest, __import__("hashlib").sha256(dest.read_bytes()).hexdigest())

    def test_keep_assets_writes_inside_blobs(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = Store(root / "store.db", root / "blobs")
        self.addCleanup(store.close)
        package = root / "pkg"
        (package / "darwin-arm64").mkdir(parents=True)
        (package / "darwin-arm64" / "officecli").write_bytes(b"bin")
        store._keep_assets(
            "sub2",
            package,
            {
                "kind": "runtime",
                "runtime": {
                    "assets": [{"platform": "darwin-arm64", "path": "darwin-arm64/officecli"}]
                },
            },
        )
        saved = store.blob_dir / "assets" / "sub2" / "darwin-arm64"
        self.assertEqual(saved.read_bytes(), b"bin")
        saved.resolve().relative_to(store.blob_dir.resolve())


if __name__ == "__main__":
    unittest.main()
