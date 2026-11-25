"""参考安装器。只执行安装计划里的 op，不认识的步骤立刻停止。"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
import re
from typing import Callable

from store.blobs import safe_extract
from store.paths import default_knowledge_root
from store.safety import PathEscape, safe_join

_BIN_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# 与服务端制品上限同量级。下载不设上限时，一个异常制品就能撑爆客户端磁盘。
_MAX_DOWNLOAD = 64 * 1024 * 1024
_MAX_RPC_BYTES = 4 * 1024 * 1024
_CHUNK = 256 * 1024

KNOWN = {
    "download",
    "unpack",
    "place_bin",
    "mcp_add",
    "ensure_bin",
    "ensure_product",
    "need_secret",
    "verify",
    "plugin_stage",
    "plugin_confirm",
    "check_bin",
}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="store-install")
    parser.add_argument("plan", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--skills-root", type=Path)
    parser.add_argument("--bin-dir", type=Path)
    parser.add_argument("--mcp-dir", type=Path)
    parser.add_argument("--plugin-staging", type=Path)
    args = parser.parse_args(argv)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    return run(
        plan,
        dry_run=args.dry_run,
        work=args.work,
        skills_root=args.skills_root,
        bin_dir=args.bin_dir,
        mcp_dir=args.mcp_dir,
        plugin_staging=args.plugin_staging,
    )


def run(
    plan: dict,
    *,
    dry_run: bool = False,
    work: Path | None = None,
    skills_root: Path | None = None,
    knowledge_root: Path | None = None,
    bin_dir: Path | None = None,
    mcp_dir: Path | None = None,
    plugin_staging: Path | None = None,
    opener: Callable[..., object] | None = None,
) -> int:
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list):
        print("安装计划没有 steps", file=sys.stderr)
        return 2
    roots = {
        "WORK": work or _env_path("QILIN_WORK"),
        "SKILLS_ROOT": skills_root or _env_path("QILIN_SKILLS_ROOT"),
        "KNOWLEDGE_ROOT": knowledge_root or default_knowledge_root(),
        "BIN_DIR": bin_dir or _env_path("QILIN_BIN_DIR"),
        "MCP_DIR": mcp_dir or _env_path("QILIN_MCP_DIR"),
        "PLUGIN_STAGING": plugin_staging or _env_path("QILIN_PLUGIN_STAGING"),
    }
    for step in steps:
        if not isinstance(step, dict) or step.get("op") not in KNOWN:
            print(f"未知 op：{step.get('op') if isinstance(step, dict) else step}", file=sys.stderr)
            return 2
        if dry_run:
            print(json.dumps(step, ensure_ascii=False))
            continue
        expanded = _expand(step, roots)
        if expanded is None:
            print("安装计划用了还没设置的目录", file=sys.stderr)
            return 2
        code = _OPS[expanded["op"]](expanded, opener or urllib.request.urlopen)
        if code != 0:
            return code
    return 0


def _download(step: dict, opener: Callable[..., object]) -> int:
    """分块下载，边算校验和边写临时文件。

    下载完成后先核对 sha256，再 `replace` 到目标位置：中途失败不会留下
    半个制品，也不会让后续步骤拿到一个校验不过的文件。
    """
    dest = Path(step["dest"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    total = 0
    try:
        with opener(step["url"], timeout=30) as response, temp.open("wb") as out:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_DOWNLOAD:
                    raise ValueError(f"文件超过 {_MAX_DOWNLOAD} 字节上限")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != step.get("sha256"):
            raise ValueError("sha256 不符")
        temp.replace(dest)
        return 0
    except (OSError, ValueError) as exc:
        temp.unlink(missing_ok=True)
        print(f"下载失败：{exc}，已停止", file=sys.stderr)
        return 1


def _unpack(step: dict, _opener: Callable[..., object]) -> int:
    """解压制品。与服务端共用 `store.blobs.safe_extract`。

    路径守卫、忽略名单、合计体积上限只有一份实现；安装器不再自己写一套，
    否则两边的安全口径迟早分叉。
    """
    archive = Path(step["src"])
    dest = Path(step["dest"])
    try:
        safe_extract(archive, dest)
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        print(f"压缩包不合法：{exc}，已停止", file=sys.stderr)
        return 1
    return 0


def _place_bin(step: dict, _opener: Callable[..., object]) -> int:
    if not step.get("bin_dir"):
        print("没有设置 BIN_DIR", file=sys.stderr)
        return 2
    bin_name = str(step.get("bin_name") or "")
    if not _BIN_NAME.fullmatch(bin_name) or ".." in Path(bin_name).parts:
        print("命令名路径不合法", file=sys.stderr)
        return 1
    destination_dir = Path(step["bin_dir"])
    destination_dir.mkdir(parents=True, exist_ok=True)
    try:
        target = safe_join(destination_dir, bin_name)
    except PathEscape:
        print("命令名路径不合法", file=sys.stderr)
        return 1
    shutil.copyfile(step["src"], target)
    target.chmod(0o755)
    return 0


def _mcp_add(step: dict, _opener: Callable[..., object]) -> int:
    directory = Path(step["mcp_dir"])
    directory.mkdir(parents=True, exist_ok=True)
    # 白名单：`_expand` 会把 bin_dir、skills_root 等注入路径塞进每个步骤，
    # 不能把它们写进客户端的 MCP 配置。
    keys = {"id", "transport", "command", "args", "cwd", "env", "url", "headers", "secrets"}
    payload = {key: step[key] for key in keys if key in step}
    (directory / f"{step['id']}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return 0


def _ensure_bin(step: dict, _opener: Callable[..., object]) -> int:
    try:
        completed = subprocess.run(
            step["detect"], check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is not None and completed.returncode == 0:
        return 0
    print(step.get("install_guide") or "")
    for resource in step.get("resources") or []:
        if isinstance(resource, dict) and resource.get("url"):
            print(resource["url"])
    return 1


def _ensure_product(step: dict, _opener: Callable[..., object]) -> int:
    # 旧计划里的标记。依赖已经展开成后续步骤，这里只记录，不再装一遍。
    print(f"依赖 {step.get('id')}@{step.get('version')} 由后续步骤安装")
    return 0


def _need_secret(step: dict, _opener: Callable[..., object]) -> int:
    print(f"需要环境变量 {step.get('name')}：{step.get('description') or ''}")
    return 0


def _plugin_stage(step: dict, _opener: Callable[..., object]) -> int:
    src = Path(str(step.get("src") or ""))
    dest = Path(str(step.get("dest") or ""))
    if not src.is_file():
        print(f"没有找到插件制品 {src}", file=sys.stderr)
        return 1
    if dest.exists() and any(dest.iterdir()):
        print(f"插件暂存目录不是空的：{dest}", file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    try:
        safe_extract(src, dest)
    except (zipfile.BadZipFile, ValueError, OSError):
        print("插件制品不是合法压缩包", file=sys.stderr)
        return 1
    package_path = dest / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("插件缺少合法的 package.json", file=sys.stderr)
        return 1
    if package.get("version") != step.get("version"):
        print("package.json 的 version 与计划不一致", file=sys.stderr)
        return 1
    for key in ("entry", "client", "patch"):
        relative = step.get(key)
        if relative and not (dest / str(relative)).is_file():
            print(f"插件缺少 {relative}", file=sys.stderr)
            return 1
    print(f"已暂存插件到 {dest}")
    return 0


def _plugin_confirm(step: dict, _opener: Callable[..., object]) -> int:
    path = step.get("path") or ""
    ident = step.get("id") or ""
    version = step.get("version") or ""
    print(f"插件 {ident}@{version} 已暂存到 {path}")
    print("下一步由你在 DSH 的 Plugins 里添加这个目录，或让 agent 调 plugin_manager install_bundle。")
    print("商店和参考安装器都不会自动安装或执行插件代码。")
    return 0


def _check_bin(step: dict, _opener: Callable[..., object]) -> int:
    name = str(step.get("name") or "")
    if not name:
        return 1
    if shutil.which(name):
        print(f"已找到外部命令 {name}")
        return 0
    print(f"缺少外部命令 {name}，请先安装并放进 PATH", file=sys.stderr)
    return 1


def _verify(step: dict, opener: Callable[..., object]) -> int:
    entry = step.get("entry")
    if step.get("kind") in {"skill", "mcp+skill", "knowledge"} and entry:
        root_key = "knowledge_root" if step.get("kind") == "knowledge" else "skills_root"
        if not step.get(root_key):
            print("没有设置知识包目录" if root_key == "knowledge_root" else "没有设置技能目录", file=sys.stderr)
            return 1
        root = Path(step[root_key])
        target = root / step["id"] / step["version"] / entry
        if not target.is_file():
            print(f"没有找到入口 {target}", file=sys.stderr)
            return 1
    if step.get("kind") == "plugin":
        root_value = step.get("plugin_root")
        if not root_value:
            print("没有设置插件暂存目录", file=sys.stderr)
            return 1
        root = Path(str(root_value))
        for key in ("entry", "patch"):
            relative = step.get(key)
            if relative and not (root / str(relative)).is_file():
                print(f"没有找到插件文件 {root / str(relative)}", file=sys.stderr)
                return 1
        return 0
    if step.get("kind") in {"mcp", "mcp+skill"}:
        if step.get("kind") == "mcp" and step.get("cwd"):
            if not Path(str(step["cwd"])).is_dir():
                print(f"没有找到 MCP 解压目录 {step['cwd']}", file=sys.stderr)
                return 1
        return _verify_mcp(step, opener)
    return 0


def _verify_mcp(step: dict, opener: Callable[..., object]) -> int:
    transport = step.get("transport")
    try:
        if transport == "stdio":
            tools = _list_stdio(step)
        elif transport == "streamable-http":
            tools = _list_http(step, opener)
        else:
            print("不认识这种 MCP 连接，不能列出工具。", file=sys.stderr)
            return 1
    except (OSError, TimeoutError):
        print("连不上 MCP，不能列出工具。", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, ValueError, UnicodeError):
        print("MCP 没有返回工具列表。", file=sys.stderr)
        return 1
    if not _tool_names(tools):
        print("MCP 没有返回工具列表。", file=sys.stderr)
        return 1
    return 0


def _tool_names(tools: object) -> bool:
    if not isinstance(tools, list):
        return False
    return all(
        isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()
        for item in tools
    )


def _list_http(step: dict, opener: Callable[..., object]) -> object:
    url = step.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("url")
    session: dict[str, str] = {}
    opened = _post_rpc(opener, url, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "qilin-install", "version": "1"},
        },
    }, session)
    if not isinstance(opened.get("result"), dict):
        raise ValueError("initialize")
    _post_rpc(opener, url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session, allow_empty=True)
    listed = _post_rpc(opener, url, {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }, session)
    result = listed.get("result")
    if not isinstance(result, dict) or "error" in listed:
        raise ValueError("tools")
    return result.get("tools")


def _post_rpc(
    opener: Callable[..., object],
    url: str,
    payload: dict,
    session: dict[str, str],
    allow_empty: bool = False,
) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    if session.get("id"):
        request.add_header("Mcp-Session-Id", session["id"])
    with opener(request, timeout=15) as response:
        status = getattr(response, "status", 200)
        headers = getattr(response, "headers", None)
        if headers is not None and headers.get("Mcp-Session-Id"):
            session["id"] = headers.get("Mcp-Session-Id")
        raw = response.read(_MAX_RPC_BYTES + 1)
    if len(raw) > _MAX_RPC_BYTES:
        raise ValueError("body")
    if status >= 400:
        raise urllib.error.URLError(f"status {status}")
    if not raw and allow_empty:
        return {}
    return _rpc_body(raw)


def _rpc_body(raw: bytes) -> dict:
    text = raw.decode().strip()
    if text.startswith("{"):
        body = json.loads(text)
        if not isinstance(body, dict):
            raise ValueError("body")
        return body
    found = None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        body = json.loads(data)
        if isinstance(body, dict):
            found = body
    if found is None:
        raise ValueError("body")
    return found


def _list_stdio(step: dict) -> object:
    command = step.get("command")
    args = step.get("args") or []
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command")
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ValueError("args")
    workdir = str(step["cwd"]) if step.get("cwd") else None
    process = subprocess.Popen(
        [command, *args],
        cwd=workdir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    lines: queue.Queue[bytes] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=read, daemon=True).start()
    try:
        _stdio_send(process, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "qilin-install", "version": "1"},
            },
        })
        opened = _stdio_wait(lines, 1, process)
        if not isinstance(opened.get("result"), dict):
            raise ValueError("initialize")
        _stdio_send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _stdio_send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = _stdio_wait(lines, 2, process)
        result = listed.get("result")
        if not isinstance(result, dict) or "error" in listed:
            raise ValueError("tools")
        return result.get("tools")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout):
            if pipe is not None:
                pipe.close()


def _stdio_send(process: subprocess.Popen[bytes], payload: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload).encode() + b"\n")
    process.stdin.flush()


def _stdio_wait(lines: queue.Queue[bytes], message_id: int, process: subprocess.Popen[bytes]) -> dict:
    deadline = time.monotonic() + 10
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            line = lines.get(timeout=min(remaining, 0.2))
        except queue.Empty:
            if process.poll() is None:
                continue
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty as exc:
                raise OSError("stdio closed") from exc
        text = line.decode().strip()
        if not text:
            continue
        body = json.loads(text)
        if isinstance(body, dict) and body.get("id") == message_id:
            return body


_OPS = {
    "download": _download,
    "unpack": _unpack,
    "place_bin": _place_bin,
    "mcp_add": _mcp_add,
    "ensure_bin": _ensure_bin,
    "ensure_product": _ensure_product,
    "need_secret": _need_secret,
    "verify": _verify,
    "plugin_stage": _plugin_stage,
    "plugin_confirm": _plugin_confirm,
    "check_bin": _check_bin,
}


_MISSING = object()


def _expand(step: dict, roots: dict[str, Path | None]) -> dict | None:
    def replace(value: object) -> object:
        if not isinstance(value, str):
            return value
        text = value
        for name, path in roots.items():
            token = "${" + name + "}"
            if token in text:
                if path is None:
                    return _MISSING
                text = text.replace(token, str(path))
        return text

    expanded = {}
    for key, value in step.items():
        if isinstance(value, list):
            expanded[key] = [replace(item) if not isinstance(item, dict) else item for item in value]
            if any(item is _MISSING for item in expanded[key]):
                return None
        elif isinstance(value, dict):
            expanded[key] = value
        else:
            replaced = replace(value)
            if replaced is _MISSING:
                return None
            expanded[key] = replaced
    for name in ("BIN_DIR", "MCP_DIR", "SKILLS_ROOT", "KNOWLEDGE_ROOT", "PLUGIN_STAGING"):
        if roots[name] is not None:
            expanded[name.lower()] = str(roots[name])
    return expanded


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


if __name__ == "__main__":
    sys.exit(main())
