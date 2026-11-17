"""对外视图、差异与打包辅助。

这一层不碰数据库，只处理清单与文件。service 的各个 mixin 都从这里取用。
"""

from __future__ import annotations

import difflib
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

from store.blobs import is_ignored_path
from store.errors import StoreError
from store.safety import PathEscape, check_relative, safe_join

_PUBLIC_MANIFEST_KEYS = (
    "spec_version", "id", "version", "kind", "title", "summary", "description",
    "changes", "author", "platforms", "clients", "depends", "secrets", "invoke",
    "knowledge", "skill", "mcp", "runtime", "plugin",
)
_PREVIEW_LIMIT = 64 * 1024



def _public(row: sqlite3.Row) -> dict:
    """货架与详情页共用的公开视图。

    规格第 4.1 节规定对外只有 `author.display_name`。清单里另外存了
    `author.publisher_id`（服务端用来做归属核对），不能跟着一起发出去：
    它等同发布者的内部标识，且可用于跨包关联同一个人。
    """
    manifest = json.loads(row["manifest_json"])
    return {
        "id": row["package_id"],
        "version": row["version"],
        "kind": manifest.get("kind"),
        "title": manifest.get("title"),
        "summary": manifest.get("summary"),
        "author": _public_author(manifest.get("author")),
        "platforms": manifest.get("platforms") or [],
        "clients": manifest.get("clients") or [],
    }

def _public_author(author: object) -> dict:
    """只保留对外可见的作者字段，白名单式输出。"""
    if not isinstance(author, dict):
        return {"display_name": ""}
    return {"display_name": str(author.get("display_name") or "")}

def public_view(manifest: object) -> dict:
    """把清单裁成对外可见的样子。

    只复制 `_PUBLIC_MANIFEST_KEYS` 里有的顶层键。作者块再按规格第 4.1 节
    收成 `display_name`。安装计划不走这个函数，它在服务内部读库存清单。
    """
    if not isinstance(manifest, dict):
        return {}
    view = {key: manifest[key] for key in _PUBLIC_MANIFEST_KEYS if key in manifest}
    view["author"] = _public_author(manifest.get("author"))
    return view

def review_view(manifest: object) -> dict:
    """审核视图。审核要看见提交上来的全部字段，包括商店还不认识的键。

    只做浅拷贝，不裁剪。公开出口走 `public_view`。
    """
    if not isinstance(manifest, dict):
        return {}
    return dict(manifest)

def _has_rule(validation_json: str, rule: str) -> bool:
    try:
        lines = json.loads(validation_json)
    except json.JSONDecodeError:
        return False
    return any(str(line).split("\t", 1)[0] == rule for line in lines)

def _findings(validation_json: str) -> list[dict]:
    found = []
    for line in json.loads(validation_json):
        rule, path, message = str(line).split("\t", 2)
        found.append({"rule": rule, "path": path, "message": message})
    return found

def _diff(previous: dict | None, current: dict) -> list[dict]:
    if previous is None:
        return []
    changes: list[dict] = []

    def walk(before: object, after: object, path: str) -> None:
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(set(before) | set(after)):
                walk(before.get(key), after.get(key), f"{path}.{key}" if path else key)
            return
        if before != after:
            changes.append({"path": path, "before": before, "after": after})

    walk(previous, current, "")
    return changes

def _read_artifact(path: str | None) -> bytes:
    """读制品文件。文件缺失时给明确的规则码，不让 `FileNotFoundError` 冒到 HTTP 层。

    `uploading` 状态的提交若被外部清理，`artifact_path` 会指向不存在的文件；
    已发布版本的制品若被误删也一样。两种情况都应是「这个版本暂时取不到」，
    而不是 500 加一段栈。
    """
    if not path:
        raise StoreError("VERSION_UNAVAILABLE", "这个版本没有留存制品")
    target = Path(path)
    if not target.is_file():
        raise StoreError("VERSION_UNAVAILABLE", "制品文件已不在，请联系管理员")
    try:
        return target.read_bytes()
    except OSError as exc:
        raise StoreError("VERSION_UNAVAILABLE", f"制品读取失败：{exc.strerror or exc}") from exc

def _read_archive_preview(archive_path: Path) -> tuple[list[str], dict[str, str]]:
    names: list[str] = []
    texts: dict[str, str] = {}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                if is_ignored_path(name):
                    continue
                names.append(name)
                if info.file_size > _PREVIEW_LIMIT * 2:
                    texts[name] = f"[文件较大（{info.file_size} 字节），已省略在线预览]"
                    continue
                raw = archive.read(info)
                if b"\0" in raw:
                    texts[name] = f"[二进制文件（{len(raw)} 字节）]"
                    continue
                text = raw.decode("utf-8", errors="replace")
                if len(text) > _PREVIEW_LIMIT:
                    text = text[:_PREVIEW_LIMIT] + "\n... [内容已截断]"
                texts[name] = text
    except (zipfile.BadZipFile, OSError):
        return [], {}
    return sorted(names), texts

def _compute_file_diffs(
    base_texts: dict[str, str],
    current_texts: dict[str, str],
    *,
    base_version: str | None,
    current_version: str,
) -> list[dict]:
    if not base_texts:
        return []
    diffs: list[dict] = []
    all_paths = sorted(set(base_texts) | set(current_texts))
    from_label = f"v{base_version}" if base_version else "prev"
    to_label = f"v{current_version}"
    for path in all_paths:
        if path == "manifest.json":
            continue
        before = base_texts.get(path)
        after = current_texts.get(path)
        if before == after:
            continue
        if before is None:
            status = "added"
            before_lines: list[str] = []
            after_lines = (after or "").splitlines()
        elif after is None:
            status = "removed"
            before_lines = before.splitlines()
            after_lines = []
        else:
            status = "modified"
            before_lines = before.splitlines()
            after_lines = after.splitlines()
        patch = "\n".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"{from_label}/{path}",
                tofile=f"{to_label}/{path}",
                lineterm="",
                n=3,
            )
        )
        diffs.append({"path": path, "status": status, "diff": patch})
    return diffs

def _write_tree(root: Path, files: dict[str, bytes], manifest: dict) -> None:
    """把提交的文件树写到临时目录。路径不合法抛 CHECKSUM。"""
    if not isinstance(files, dict):
        raise StoreError("CHECKSUM", "files 必须是对象")
    for raw_name, content in files.items():
        if not isinstance(content, bytes):
            raise StoreError("CHECKSUM", f"{raw_name} 的内容必须是字节")
        try:
            name = check_relative(raw_name)
            path = safe_join(root, name)
        except PathEscape as exc:
            raise StoreError("CHECKSUM", f"制品路径不合法：{exc}") from exc
        if is_ignored_path(name) or name == "manifest.json":
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

def _check_add_platform(published: dict, incoming: dict) -> None:
    if published.get("kind") != "runtime":
        raise StoreError("PLATFORM_SCOPE", "只有 runtime 可以按平台追加制品")
    if _scope(published) != _scope(incoming):
        raise StoreError("PLATFORM_SCOPE", "追加平台不能改清单正文")
    already = set(published.get("platforms") or [])
    added = list(incoming.get("platforms") or [])
    if not added or any(platform in already for platform in added):
        raise StoreError("PLATFORM_TAKEN", "平台已经存在，或没有新平台")

def _scope(manifest: dict) -> str:
    copy = json.loads(json.dumps(manifest))
    copy.pop("platforms", None)
    runtime = copy.get("runtime")
    if isinstance(runtime, dict):
        runtime = dict(runtime)
        runtime.pop("assets", None)
        copy["runtime"] = runtime
    return json.dumps(copy, sort_keys=True, ensure_ascii=False)
