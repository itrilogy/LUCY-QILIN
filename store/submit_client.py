"""把本机一个目录交给商店。钥匙只从环境变量来，不进参数，也不进输出。"""

from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from store.base import _MAX_BYTES
from store.blobs import write_zip
from store.submissions import _has_payload_files
from store.validator.validate import validate_path


def submit_directory(
    root: Path, *, base: str, key: str, submit_kind: str = "new_version"
) -> tuple[int, dict]:
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
    pure_mcp_no_zip = manifest.get("kind") == "mcp" and not _has_payload_files(root)
    archive = b"" if pure_mcp_no_zip else _zip(root)
    if len(archive) > _MAX_BYTES:
        return 1, {
            "status": "invalid",
            "rules": ["CHECKSUM"],
            "message": "制品大小不在允许范围内",
        }
    started = _tool(base, key, "begin_submit", {
        "manifest": manifest,
        "submit_kind": submit_kind,
        "byte_size": len(archive),
    })
    if started.get("rule") or started.get("status") != "uploading" or (not pure_mcp_no_zip and not started.get("put_url")):
        return 1, {
            "status": "rejected",
            "submission_id": started.get("submission_id"),
            "rules": started.get("rules") or ([started["rule"]] if started.get("rule") else []),
            "message": started.get("message") or "",
        }
    if started.get("put_url") or started.get("upload_url"):
        upload_url = str(started.get("upload_url") or started["put_url"])
        upload_headers = started.get("upload_headers") if isinstance(started.get("upload_headers"), dict) else None
        put = _put(upload_url, key, archive, extra_headers=upload_headers)
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


def _put(
    url: str, key: str, archive: bytes, *, extra_headers: dict[str, str] | None = None,
) -> str | None:
    headers = dict(extra_headers or {})
    if "X-Upload-Ticket" not in headers and key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(
        url,
        data=archive,
        method="PUT",
        headers=headers,
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
