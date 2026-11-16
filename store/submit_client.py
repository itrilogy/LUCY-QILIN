"""把本机一个目录交给商店。钥匙只从环境变量来，不进参数，也不进输出。"""

from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from store.blobs import write_zip
from store.validator.validate import validate_path


def submit_directory(root: Path, *, base: str, key: str) -> tuple[int, dict]:
    root = root.expanduser().resolve()
    if not (root / "manifest.json").is_file():
        return 2, {"status": "invalid", "message": "目录里还没有 manifest.json"}
    report = validate_path(root)
    if not report.ok:
        return 1, {
            "status": "invalid",
            "rules": report.rules(),
            "findings": [
                {"rule": item.rule, "path": item.path, "message": item.message}
                for item in report.findings
                if item.severity == "error"
            ],
        }
    if not key.strip():
        return 2, {
            "status": "invalid",
            "message": "没有 QILIN_STORE_KEY。把它设成连接商店用的那把钥匙，不要写进命令，也不要写进笔记。",
        }
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    archive = _zip(root)
    started = _tool(base, key, "begin_submit", {
        "manifest": manifest,
        "submit_kind": "new_version",
        "byte_size": len(archive),
    })
    if started.get("rule") or started.get("status") != "uploading" or not started.get("put_url"):
        return 1, {
            "status": "rejected",
            "submission_id": started.get("submission_id"),
            "rules": started.get("rules") or ([started["rule"]] if started.get("rule") else []),
            "message": started.get("message") or "",
        }
    put = _put(str(started["put_url"]), key, archive)
    if put is not None:
        return 1, {
            "status": "uploading",
            "submission_id": started["submission_id"],
            "message": put,
        }
    finished = _tool(base, key, "finish_submit", {"submission_id": started["submission_id"]})
    if finished.get("rule"):
        return 1, {
            "status": "rejected",
            "submission_id": started["submission_id"],
            "rules": [finished["rule"]],
            "message": finished.get("message") or "",
        }
    status = str(finished.get("status") or "")
    payload = {
        "status": status,
        "submission_id": finished.get("submission_id") or started["submission_id"],
        "rules": finished.get("rules") or [],
    }
    return (0 if status == "pending" else 1), payload


def _zip(root: Path) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "package.zip"
        write_zip(root, dest)
        return dest.read_bytes()


def _tool(base: str, key: str, name: str, arguments: dict) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()
    request = urllib.request.Request(
        base.rstrip("/") + "/mcp",
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            message = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        exc.read()
        exc.close()
        return {"rule": "VERSION_UNAVAILABLE", "message": "商店没有接受这次提交。"}
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"rule": "VERSION_UNAVAILABLE", "message": "连不上商店。"}
    result = message.get("result") if isinstance(message, dict) else None
    if not isinstance(result, dict):
        return {"rule": "VERSION_UNAVAILABLE", "message": "商店没有返回提交结果。"}
    content = result.get("content")
    if not content:
        return {"rule": "VERSION_UNAVAILABLE", "message": "商店没有返回提交结果。"}
    try:
        return json.loads(content[0]["text"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return {"rule": "VERSION_UNAVAILABLE", "message": "商店没有返回提交结果。"}


def _put(url: str, key: str, archive: bytes) -> str | None:
    request = urllib.request.Request(
        url,
        data=archive,
        method="PUT",
        headers={"Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                return "制品没有交上去。"
    except urllib.error.HTTPError:
        return "制品没有交上去。"
    except OSError:
        return "连不上商店。"
    return None
