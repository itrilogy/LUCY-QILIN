"""上架单校验。门户、MCP 和 `store validate` 调用同一函数。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from store.blobs import is_ignored_path, load_storeignore
from store.safety import PathEscape, check_relative, safe_join
from store.semver import SEMVER
from store.semver import satisfies as _satisfies
from store.validator.knowledge import check as check_knowledge
from store.validator.net import is_allowlisted_url, probe_url, url_check_mode, url_ok
from store.validator.registry import (
    CLIENTS, KINDS, PLATFORMS, PLUGIN_CAPABILITIES, PLUGIN_TIER_B_ALLOWLIST, PLUGIN_TIERS,
)

ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*)+$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
BIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SECRET_VALUE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|Bearer [A-Za-z0-9._\-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
SKIP_DIRS = {".git", "__pycache__"}

# 外部地址探测的预算：单份清单最多探这么多个地址、总共给这么多秒。
# 没有预算时，一份清单可以把 handler 线程按住几百秒（评审 P1-3）。
_MAX_PROBES = 20
_PROBE_BUDGET_SECONDS = 45.0


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    message: str
    severity: str = "error"  # error | warning

    def line(self) -> str:
        if self.severity and self.severity != "error":
            return f"{self.rule}\t{self.path}\t{self.message}\t{self.severity}"
        return f"{self.rule}\t{self.path}\t{self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)

    def add(self, rule: str, path: str, message: str, severity: str = "error") -> None:
        self.findings.append(Finding(rule, path, message, severity))

    def guard(
        self,
        value: object,
        expected: type | tuple[type, ...],
        rule: str,
        path: str,
        message: str,
        default: object,
    ) -> object:
        """类型不符就记一条 finding，并返回 default。不抛异常。

        只判断类型，不判断空不空。default 若是列表或字典，调用方每次传入新的，
        不要把同一个对象传给多次调用。类型已经对上时，原样返回，不另记一条。
        """
        if isinstance(value, expected):
            return value
        self.add(rule, path, message)
        return default

    def rules(self) -> list[str]:
        return [item.rule for item in self.findings if item.severity == "error"]


@dataclass(frozen=True)
class Release:
    id: str
    version: str
    kind: str


def validate_path(
    target: Path,
    *,
    check_urls: bool = False,
    releases: list[Release] | None = None,
    probe: Callable[[str], int | None] | None = None,
    check_files: bool = True,
) -> Report:
    path = target.expanduser().resolve()
    if path.is_file() and path.name == "manifest.json":
        root = path.parent
    elif path.is_dir():
        root = path
    else:
        report = Report()
        report.add("SKILL_ENTRY_MISSING", str(target), "需要技能目录或 manifest.json")
        return report
    manifest_path = root / "manifest.json"
    report = Report()
    if not manifest_path.is_file():
        report.add("SKILL_ENTRY_MISSING", "manifest.json", "缺少 manifest.json")
        return report
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.add("SPEC_VERSION", "manifest.json", f"清单不是 JSON：{exc.msg}")
        return report
    if not isinstance(manifest, dict):
        report.add("SPEC_VERSION", "manifest.json", "清单必须是对象")
        return report
    _validate_manifest(
        manifest,
        root,
        report,
        check_urls=check_urls,
        releases=releases or [],
        probe=probe or probe_url,
        check_files=check_files,
    )
    return report


def validate_manifest(
    manifest: dict,
    *,
    releases: list[Release] | None = None,
    check_urls: bool = False,
    probe: Callable[[str], int | None] | None = None,
) -> Report:
    """上传开始前的静态校验。不读制品，不把缺文件当成失败。"""
    report = Report()
    if not isinstance(manifest, dict):
        report.add("SPEC_VERSION", "manifest.json", "清单必须是对象")
        return report
    _validate_manifest(
        manifest,
        Path("."),
        report,
        check_urls=check_urls,
        releases=releases or [],
        probe=probe or probe_url,
        check_files=False,
    )
    return report


def _validate_manifest(
    manifest: dict,
    root: Path,
    report: Report,
    *,
    check_urls: bool,
    releases: list[Release],
    probe: Callable[[str], int | None],
    check_files: bool,
) -> None:
    if manifest.get("spec_version") != 1:
        report.add("SPEC_VERSION", "spec_version", "spec_version 必须是 1")
    kind = manifest.get("kind")
    # 清单来自外部。kind 若是数组或对象，直接查集合会抛 TypeError 并打断请求。
    if not isinstance(kind, str) or kind not in KINDS:
        report.add("KIND", "kind", "kind 只能是 mcp、skill、mcp+skill、runtime、knowledge、plugin")
        kind = None
    package_id = manifest.get("id")
    if not isinstance(package_id, str) or not ID_PATTERN.fullmatch(package_id):
        report.add("ID_FORMAT", "id", "id 必须是小写的命名空间.名字")
        package_id = None
    version = manifest.get("version")
    if not isinstance(version, str) or not SEMVER.match(version.strip()):
        report.add("SEMVER", "version", "version 必须是严格 semver")
    # title 是货架上唯一给用户看的短名，必须是非空字符串。
    _text(report, manifest, "title", "TITLE", minimum=1, maximum=40)
    _text(report, manifest, "summary", "SUMMARY", minimum=1, maximum=200)
    _text(report, manifest, "description", "DESCRIPTION", minimum=40)
    changes = manifest.get("changes")
    if not isinstance(changes, str) or (
        len(changes.strip()) < 10 and changes.strip() != "首个发布"
    ):
        report.add("DESCRIPTION", "changes", "changes 至少 10 字，或写「首个发布」")
    author = report.guard(
        manifest.get("author"),
        dict,
        "AUTHOR_MISMATCH",
        "author.display_name",
        "必须填写作者显示名",
        None,
    )
    if author is not None and not str(author.get("display_name") or "").strip():
        report.add("AUTHOR_MISMATCH", "author.display_name", "必须填写作者显示名")
    platforms = _platforms(report, manifest.get("platforms"))
    clients = _clients(report, manifest.get("clients"))
    depends = manifest.get("depends")
    depends = report.guard(
        depends,
        dict,
        "DEP_GUIDE_MISSING",
        "depends",
        "depends 必须包含 store、external、skills、mcp",
        None,
    )
    if depends is None:
        depends = {}
    else:
        for key in ("store", "external", "skills", "mcp"):
            slot = depends[key] if key in depends else None
            rows = report.guard(slot, list, "DEP_GUIDE_MISSING", f"depends.{key}", "必须是数组", None)
            if rows is None:
                depends[key] = []
    _secrets(report, manifest.get("secrets"), manifest)
    if check_files:
        if kind != "knowledge":
            _frontmatter(report, root, manifest)
        _scan_tree(report, root)
    if kind == "runtime" and "any" in platforms:
        report.add("PLATFORM_TOO_BROAD", "platforms", "runtime 不能使用 any")
    if kind in {"mcp", "mcp+skill"}:
        _mcp(report, manifest, root, platforms, clients, check_files=check_files)
    if kind in {"skill", "mcp+skill"}:
        _skill(report, manifest, root, clients, check_files=check_files)
    if kind == "runtime":
        _runtime(report, manifest, root, platforms, clients, check_files=check_files)
    if kind == "knowledge":
        check_knowledge(report, manifest, root, check_files=check_files)
    if kind == "plugin":
        _plugin(report, manifest, root, platforms, clients, check_files=check_files)
    if kind == "mcp" and "any" in platforms:
        local = bool(depends.get("store") or depends.get("external"))
        transport = (manifest.get("mcp") or {}).get("transport") if isinstance(manifest.get("mcp"), dict) else None
        if local or transport != "streamable-http":
            report.add("PLATFORM_TOO_BROAD", "platforms", "只有无本机依赖的远程 MCP 才能用 any")
    _external(report, depends.get("external") or [], platforms, check_urls, probe)
    _catalog_deps(report, depends.get("store") or [], releases, "runtime", "depends.store")
    _catalog_deps(report, depends.get("skills") or [], releases, "skill", "depends.skills")
    _catalog_deps(report, depends.get("mcp") or [], releases, "mcp", "depends.mcp")
    plugin_block = manifest.get("plugin") if isinstance(manifest.get("plugin"), dict) else {}
    needs_bin = (
        kind == "runtime"
        or bool(depends.get("store") or depends.get("external"))
        or bool(plugin_block.get("external"))
    )
    if needs_bin:
        for client in clients:
            if "local_bin" not in CLIENTS.get(client, set()):
                report.add("CLIENT_CAPABILITY", "clients", f"{client} 不能安装本机命令")


def _text(report: Report, manifest: dict, key: str, rule: str, *, minimum: int, maximum: int | None = None) -> None:
    value = manifest.get(key)
    if not isinstance(value, str) or len(value.strip()) < minimum:
        report.add(rule, key, f"{key} 至少 {minimum} 字")
        return
    if maximum is not None and len(value.strip()) > maximum:
        report.add(rule, key, f"{key} 不超过 {maximum} 字")


def _platforms(report: Report, value: object) -> list[str]:
    rows = report.guard(value, list, "PLATFORM_UNKNOWN", "platforms", "至少声明一个适用平台", [])
    if not rows:
        if isinstance(value, list):
            report.add("PLATFORM_UNKNOWN", "platforms", "至少声明一个适用平台")
        return []
    found: list[str] = []
    for item in rows:
        # 非字符串（数组、对象）不能拿去查集合，否则 TypeError 会打断请求。
        name = report.guard(item, str, "PLATFORM_UNKNOWN", "platforms", f"平台名必须是字符串：{item!r}", "")
        if not isinstance(item, str):
            continue
        if name not in PLATFORMS:
            report.add("PLATFORM_UNKNOWN", "platforms", f"不认识的平台 {name}")
        elif name not in found:
            found.append(name)
    if "any" in found and len(found) > 1:
        report.add("PLATFORM_MIXED", "platforms", "any 不能和其他平台写在一起")
    return found


def _clients(report: Report, value: object) -> list[str]:
    rows = report.guard(value, list, "CLIENT_CAPABILITY", "clients", "未声明适用的 AI 工具", [])
    if not rows:
        if isinstance(value, list):
            report.add("CLIENT_CAPABILITY", "clients", "未声明适用的 AI 工具")
        return []
    names: list[str] = []
    for item in rows:
        name = report.guard(item, str, "CLIENT_UNKNOWN", "clients", f"AI 工具名必须是字符串：{item!r}", "")
        if not isinstance(item, str):
            continue
        if name not in CLIENTS:
            report.add("CLIENT_UNKNOWN", "clients", f"不认识的 AI 工具 {name}")
        elif name not in names:
            names.append(name)
    return names


def _secrets(report: Report, value: object, manifest: dict) -> None:
    items = report.guard(value, list, "SECRET_PATTERN", "secrets", "secrets 必须是数组", None)
    if items is None:
        return
    blob = json.dumps(manifest, ensure_ascii=False)
    if SECRET_VALUE.search(blob):
        report.add("SECRET_PATTERN", "manifest.json", "清单里出现密钥样式")
    for index, item in enumerate(items):
        path = f"secrets[{index}]"
        entry = report.guard(item, dict, "SECRET_PATTERN", path, "密钥项必须是对象", None)
        if entry is None:
            continue
        name = str(entry.get("name") or "")
        if not ENV_NAME.match(name) or SECRET_VALUE.search(name):
            report.add("SECRET_PATTERN", path + ".name", "密钥只能写环境变量名，不能写值")
        if not str(entry.get("description") or "").strip():
            report.add("SECRET_PATTERN", path + ".description", "密钥需要说明用途")


def _frontmatter(report: Report, root: Path, manifest: dict) -> None:
    skill = root / "SKILL.md"
    if not skill.is_file():
        return
    text = skill.read_text(encoding="utf-8", errors="replace")
    match = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not match:
        return
    block = match.group(1)
    version = _yaml_scalar(block, "version")
    if version and SEMVER.match(version) and version != str(manifest.get("version") or "").strip():
        report.add("FRONTMATTER_CONFLICT", "version", "frontmatter 的 semver 与清单 version 不一致")
    if version and not SEMVER.match(version):
        changes = str(manifest.get("changes") or "")
        if version not in changes:
            report.add("FRONTMATTER_CONFLICT", "changes", "文字版本号必须写进 changes")
    description = _yaml_scalar(block, "description")
    summary = str(manifest.get("summary") or "").strip()
    if description and summary and summary not in description and description not in summary:
        report.add("FRONTMATTER_CONFLICT", "summary", "summary 建议是 frontmatter description 的缩短", severity="warning")
    if re.search(r"^(dependency|allowed-tools)\s*:", block, re.M):
        depends = manifest.get("depends") if isinstance(manifest.get("depends"), dict) else {}
        listed = any(depends.get(key) for key in ("store", "external", "skills", "mcp"))
        if not listed:
            report.add("FRONTMATTER_CONFLICT", "depends", "frontmatter 写了依赖或工具，清单 depends 却是空的")


def _yaml_scalar(block: str, key: str) -> str:
    match = re.search(rf"^{key}:\s*(.+)$", block, re.M)
    if not match:
        return ""
    return match.group(1).strip().strip("\"'")


def _scan_tree(report: Report, root: Path) -> None:
    ignore_patterns = load_storeignore(root)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        if is_ignored_path(relative, ignore_patterns):
            continue
        if _secret_name(path.name):
            report.add("SECRET_FILE", relative, "文件名命中密钥规则")
            continue
        if path.stat().st_size > 1_000_000:
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        if SECRET_VALUE.search(data.decode("utf-8", errors="ignore")):
            report.add("SECRET_PATTERN", relative, "文件内容命中密钥样式")


def _secret_name(name: str) -> bool:
    lower = name.lower()
    if lower == "skill.md":
        return False
    if "secret" in lower or "credential" in lower:
        return True
    if lower == ".env" or lower.startswith(".env."):
        return True
    return lower.endswith(".md") and "key" in lower


_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".ps1", ".bat")


def _is_bundle_script_arg(arg: str) -> bool:
    """判断 stdio 的参数是否像包内相对脚本路径（例如 sse_bridge.py 或 scripts/run.py）。"""
    text = arg.strip()
    if not text or text.startswith(("-", "/", "${", "~")):
        return False
    if "://" in text or "=" in text:
        return False
    if len(text) > 1 and text[1] == ":" and text[0].isalpha():
        return False
    if "/" in text or "\\" in text:
        return True
    return text.lower().endswith(_SCRIPT_SUFFIXES)


def _mcp(
    report: Report,
    manifest: dict,
    root: Path,
    platforms: list[str],
    clients: list[str],
    *,
    check_files: bool = False,
) -> None:
    block = report.guard(manifest.get("mcp"), dict, "MCP_TRANSPORT", "mcp", "缺少 mcp 连接块", None)
    if block is None:
        return
    transport = block.get("transport")
    if transport not in {"stdio", "streamable-http"}:
        report.add("MCP_TRANSPORT", "mcp.transport", "transport 只能是 stdio 或 streamable-http")
    if transport == "streamable-http":
        url = str(block.get("url") or "")
        if not (url.startswith("https://") or is_allowlisted_url(url)):
            report.add("MCP_CONNECT", "mcp.url", "远程 MCP 必须是 https URL（内网 http 需加入 QILIN_FETCH_ALLOW_HOSTS）")
        if SECRET_VALUE.search(url) or "key=" in url or "token=" in url:
            report.add("SECRET_PATTERN", "mcp.url", "URL 里不能带密钥")
        for client in clients:
            if "mcp_http" not in CLIENTS.get(client, set()):
                report.add("CLIENT_CAPABILITY", "clients", f"{client} 不能连接 streamable-http")
    if transport == "stdio":
        raw_cmd = str(block.get("command") or "").strip()
        raw_args = block.get("args")
        if not raw_cmd or not isinstance(raw_args, list):
            report.add("MCP_CONNECT", "mcp.command", "stdio 必须有 command 和 args")
        elif check_files and manifest.get("kind") == "mcp":
            for item in raw_args:
                if not isinstance(item, str) or not _is_bundle_script_arg(item):
                    continue
                try:
                    target = safe_join(root, item)
                except PathEscape:
                    report.add("MCP_CONNECT", "mcp.args", f"stdio 脚本路径不合法：{item}")
                    continue
                if not target.is_file():
                    report.add("MCP_CONNECT", "mcp.args", f"stdio 参数引用的随包脚本不存在：{item}")
        if "any" in platforms:
            report.add("PLATFORM_TOO_BROAD", "platforms", "stdio MCP 不能用 any")
        for client in clients:
            if "mcp_stdio" not in CLIENTS.get(client, set()):
                report.add("CLIENT_CAPABILITY", "clients", f"{client} 不能连接 stdio")
    if manifest.get("kind") == "mcp":
        _invoke(report, manifest.get("invoke"), {"list_tools", "documented"}, "MCP_INVOKE")
    invoke = manifest.get("invoke") if isinstance(manifest.get("invoke"), dict) else {}
    if invoke.get("form") == "documented":
        tools = invoke.get("tools")
        rows = report.guard(tools, list, "MCP_INVOKE", "invoke.tools", "documented 必须列出工具名和一句话", [])
        if not rows:
            if isinstance(tools, list):
                report.add("MCP_INVOKE", "invoke.tools", "documented 必须列出工具名和一句话")
        else:
            for index, tool in enumerate(rows):
                if not isinstance(tool, dict) or not tool.get("name") or not tool.get("summary"):
                    report.add("MCP_INVOKE", f"invoke.tools[{index}]", "工具需要 name 和 summary")


def _skill(
    report: Report,
    manifest: dict,
    root: Path,
    clients: list[str],
    *,
    check_files: bool,
) -> None:
    block = manifest.get("skill") if isinstance(manifest.get("skill"), dict) else {}
    entry = block.get("entry")
    if entry != "SKILL.md" or (check_files and not (root / "SKILL.md").is_file()):
        report.add("SKILL_ENTRY_MISSING", "skill.entry", "入口必须是存在的 SKILL.md")
    invoke = manifest.get("invoke") if isinstance(manifest.get("invoke"), dict) else {}
    expected = "load_skill_then_mcp" if manifest.get("kind") == "mcp+skill" else "load_skill"
    if invoke.get("form") != expected or invoke.get("entry") != "SKILL.md":
        report.add("SKILL_INVOKE", "invoke.form", f"invoke.form 必须是 {expected}")
    for client in clients:
        if "skill_dir" not in CLIENTS.get(client, set()):
            report.add("CLIENT_CAPABILITY", "clients", f"{client} 不能安装技能包")
    raw_files = block.get("platform_files") or []
    if raw_files:
        declared = report.guard(
            raw_files,
            list,
            "PLATFORM_FILE_MISSING",
            "skill.platform_files",
            "platform_files 必须是数组",
            [],
        )
    else:
        declared = []
    if not check_files:
        return
    for relative in declared:
        # platform_files 也是外部提供的相对路径，拼接前必须过守卫。
        try:
            target = safe_join(root, relative)
        except PathEscape:
            report.add("PLATFORM_FILE_MISSING", str(relative), "声明的适配文件路径不合法")
            continue
        if not target.is_file():
            report.add("PLATFORM_FILE_MISSING", str(relative), "声明的适配文件不存在")
    platform_dir = root / "platform"
    if platform_dir.is_dir():
        ignore_patterns = load_storeignore(root)
        for path in platform_dir.glob("*.md"):
            relative = path.relative_to(root).as_posix()
            if is_ignored_path(relative, ignore_patterns):
                continue
            if relative not in declared:
                report.add("PLATFORM_FILE_MISSING", relative, "适配文件必须写进 skill.platform_files")


def _runtime(
    report: Report,
    manifest: dict,
    root: Path,
    platforms: list[str],
    clients: list[str],
    *,
    check_files: bool,
) -> None:
    block = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    bin_name = str(block.get("bin_name") or "")
    if not bin_name:
        report.add("RUNTIME_ASSET", "runtime.bin_name", "缺少命令名")
    elif not BIN_NAME.fullmatch(bin_name) or ".." in Path(bin_name).parts:
        report.add("RUNTIME_ASSET", "runtime.bin_name", "命令名不能包含路径分隔符或 ..")
    invoke = manifest.get("invoke") if isinstance(manifest.get("invoke"), dict) else {}
    if invoke.get("form") != "exec_bin" or invoke.get("bin_name") != bin_name:
        report.add("RUNTIME_ASSET", "invoke.form", "invoke.form 必须是 exec_bin，且命令名一致")
    assets = block.get("assets") if isinstance(block.get("assets"), list) else []
    # 只认平台清单里的具体平台；runtime 不允许 any，前面已在 _validate_manifest 里挡过。
    known = PLATFORMS - {"any"}
    declared_list = [item for item in platforms if item in known]
    declared = set(declared_list)
    by_platform: dict[str, dict] = {}
    for index, asset in enumerate(assets):
        where = f"runtime.assets[{index}]"
        entry = report.guard(asset, dict, "RUNTIME_ASSET", where, "资产项必须是对象", None)
        if entry is None:
            continue
        name = entry.get("platform")
        # platform 会成为落盘目录名，必须先确认它在平台清单内、且已在 platforms 里声明。
        if not isinstance(name, str) or name not in known:
            report.add("RUNTIME_ASSET", where + ".platform", f"不认识的平台 {name!r}")
            continue
        if name not in declared:
            report.add("RUNTIME_ASSET", where + ".platform", f"{name} 没有写进 platforms")
            continue
        if name in by_platform:
            report.add("RUNTIME_ASSET", where + ".platform", f"{name} 重复声明")
            continue
        try:
            relative = check_relative(asset.get("path"))
        except PathEscape as exc:
            report.add("RUNTIME_ASSET", where + ".path", f"资产路径不合法：{exc}")
            continue
        digest = str(asset.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            report.add("CHECKSUM", relative, "sha256 必须是 64 位小写十六进制")
            continue
        by_platform[name] = asset
        if not check_files:
            continue
        try:
            file_path = safe_join(root, relative)
        except PathEscape:
            report.add("RUNTIME_ASSET", relative, "资产路径越出包目录")
            continue
        if not file_path.is_file():
            report.add("RUNTIME_ASSET", relative, "资产文件不存在")
            continue
        actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual != digest:
            report.add("CHECKSUM", relative, "sha256 与文件不一致")
    for platform in declared_list:
        if platform not in by_platform:
            report.add("RUNTIME_ASSET", "runtime.assets", f"缺少平台 {platform} 的文件")
    for client in clients:
        if "local_bin" not in CLIENTS.get(client, set()):
            report.add("CLIENT_CAPABILITY", "clients", f"{client} 不能放置本机命令")


def _plugin(
    report: Report,
    manifest: dict,
    root: Path,
    platforms: list[str],
    clients: list[str],
    *,
    check_files: bool,
) -> None:
    block = report.guard(
        manifest.get("plugin"), dict, "PLUGIN_MANIFEST", "plugin", "缺少 plugin 块", None,
    )
    if block is None:
        return
    tier = block.get("tier")
    if not isinstance(tier, str) or tier not in PLUGIN_TIERS:
        report.add("PLUGIN_TIER", "plugin.tier", "plugin.tier 只能是 A、B 或 C")
        tier = None
    elif tier == "C":
        report.add("PLUGIN_TIER", "plugin.tier", "Tier C（原生模块）当前不接受")
    form = block.get("form")
    if form not in {"host", "client", "both"}:
        report.add("PLUGIN_ENTRY", "plugin.form", "plugin.form 只能是 host、client 或 both")
        form = None
    entry = block.get("entry")
    client_entry = block.get("client")
    if form in {"host", "both"} and not isinstance(entry, str):
        report.add("PLUGIN_ENTRY", "plugin.entry", "host/both 必须写 plugin.entry")
    if form in {"client", "both"} and not isinstance(client_entry, str):
        report.add("PLUGIN_ENTRY", "plugin.client", "client/both 必须写 plugin.client")
    patch = block.get("patch")
    if not isinstance(patch, str):
        report.add("PLUGIN_ENTRY", "plugin.patch", "必须写 plugin.patch")
    dsh = block.get("dsh")
    if not isinstance(dsh, str) or not dsh.strip():
        report.add("PLUGIN_DSH_VERSION", "plugin.dsh", "必须写 DSH 版本范围，例如 >=1.5 <2")
    raw_files = block.get("files")
    files = report.guard(raw_files, list, "PLUGIN_ENTRY", "plugin.files", "plugin.files 必须是数组", [])
    declared: list[str] = []
    for index, value in enumerate(files):
        if not isinstance(value, str):
            report.add("PLUGIN_ENTRY", f"plugin.files[{index}]", "文件路径必须是字符串")
            continue
        try:
            relative = check_relative(value)
        except PathEscape:
            report.add("PLUGIN_ARTIFACT", f"plugin.files[{index}]", "文件路径越出包目录")
            continue
        declared.append(relative)
    for required in (entry, client_entry, patch):
        if isinstance(required, str) and required not in declared:
            report.add("PLUGIN_ENTRY", "plugin.files", f"运行文件 {required} 没写进 plugin.files")
    raw_caps = block.get("capabilities") or []
    caps = report.guard(raw_caps, list, "PLUGIN_CAPABILITY", "plugin.capabilities", "必须是数组", [])
    for index, cap in enumerate(caps):
        if not isinstance(cap, str) or cap not in PLUGIN_CAPABILITIES:
            report.add("PLUGIN_CAPABILITY", f"plugin.capabilities[{index}]", f"不认识的 capability：{cap!r}")
    raw_external = block.get("external") or []
    external = report.guard(raw_external, list, "PLUGIN_CAPABILITY", "plugin.external", "必须是数组", [])
    for index, name in enumerate(external):
        if not isinstance(name, str) or not name.strip():
            report.add("PLUGIN_CAPABILITY", f"plugin.external[{index}]", "外部命令名必须是非空字符串")
    if any(isinstance(name, str) and name.strip() for name in external) and "process:spawn" not in caps:
        report.add("PLUGIN_CAPABILITY", "plugin.capabilities", "声明了外部命令，就必须声明 process:spawn")
    if "any" not in platforms:
        report.add("PLUGIN_ARTIFACT", "platforms", "插件 v1 只能是纯 JS，platforms 必须含 any")
    for client in clients:
        if "plugin" not in CLIENTS.get(client, set()):
            report.add("CLIENT_CAPABILITY", "clients", f"{client} 不能安装插件")
    _invoke(report, manifest.get("invoke"), {"install_plugin"}, "PLUGIN_ENTRY")
    if not check_files:
        return

    package_path = root / "package.json"
    if not package_path.is_file():
        report.add("PLUGIN_MANIFEST", "package.json", "缺少 package.json")
        return
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        report.add("PLUGIN_MANIFEST", "package.json", "package.json 不是合法 JSON")
        return
    if not isinstance(package, dict):
        report.add("PLUGIN_MANIFEST", "package.json", "package.json 必须是对象")
        return
    if package.get("version") != manifest.get("version"):
        report.add("PLUGIN_MANIFEST", "package.json", "package.json 的 version 必须与清单一致")
    scripts = package.get("scripts") or {}
    if scripts:
        report.add("PLUGIN_SCRIPT", "package.json", "不允许 scripts / install 生命周期脚本")
    deps = package.get("dependencies") or {}
    optional = package.get("optionalDependencies") or {}
    peers = package.get("peerDependencies") or {}
    if not isinstance(deps, dict) or not isinstance(optional, dict) or not isinstance(peers, dict):
        report.add("PLUGIN_MANIFEST", "package.json", "dependencies / optionalDependencies / peerDependencies 必须是对象")
    else:
        if optional:
            report.add("PLUGIN_DEPENDENCY", "package.json", "不允许 optionalDependencies")
        for name in deps:
            if tier == "A":
                report.add("PLUGIN_DEPENDENCY", "package.json", f"Tier A 不允许运行时依赖：{name}")
            elif tier == "B" and name not in PLUGIN_TIER_B_ALLOWLIST:
                report.add("PLUGIN_DEPENDENCY", "package.json", f"依赖 {name} 不在 Tier B 白名单")
        for name in peers:
            if not str(name).startswith("@deepseek-ai/"):
                report.add("PLUGIN_DEPENDENCY", "package.json", f"peerDependencies 只允许 @deepseek-ai/*：{name}")
    total = 0
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        total += path.stat().st_size
        if path.suffix == ".node" or path.name == "binding.gyp":
            report.add("PLUGIN_ARTIFACT", relative, "不允许原生模块或 binding.gyp")
    if total > 16 * 1024 * 1024:
        report.add("PLUGIN_ARTIFACT", "package", "插件包不超过 16 MiB")
    for relative in declared:
        try:
            target = safe_join(root, relative)
        except PathEscape:
            report.add("PLUGIN_ARTIFACT", relative, "文件路径越出包目录")
            continue
        if not target.is_file():
            report.add("PLUGIN_ARTIFACT", relative, "plugin.files 声明的文件不存在")


def _invoke(report: Report, invoke: object, allowed: set[str], rule: str) -> None:
    if not isinstance(invoke, dict) or invoke.get("form") not in allowed:
        report.add(rule, "invoke.form", "缺少调用形式")


def _external(
    report: Report,
    items: list,
    platforms: list[str],
    check_urls: bool,
    probe: Callable[[str], int | None],
) -> None:
    deadline = time.monotonic() + _PROBE_BUDGET_SECONDS
    probed = 0
    mode = url_check_mode() if check_urls else "off"
    for index, item in enumerate(items):
        path = f"depends.external[{index}]"
        entry = report.guard(item, dict, "DEP_GUIDE_MISSING", path, "外部依赖必须是对象", None)
        if entry is None:
            continue
        name = str(entry.get("name") or "")
        guide = str(entry.get("install_guide") or "")
        detect = entry.get("detect")
        raw_resources = entry.get("resources")
        bad_detect = not isinstance(detect, list) or not detect or not all(isinstance(part, str) for part in detect)
        if not name or not entry.get("version_range") or bad_detect or len(guide.strip()) < 20 or name not in guide:
            report.add("DEP_GUIDE_MISSING", path, "需要名称、版本范围、detect 数组，以及包含名称的安装指南")
        resources = report.guard(
            raw_resources,
            list,
            "DEP_GUIDE_MISSING",
            path + ".resources",
            "至少有一个资源地址",
            [],
        )
        if isinstance(raw_resources, list) and not raw_resources:
            report.add("DEP_GUIDE_MISSING", path + ".resources", "至少有一个资源地址")
        covered: set[str] = set()
        for resource in resources:
            one = report.guard(resource, dict, "DEP_EXTERNAL_URL", path + ".resources", "资源必须是对象", None)
            if one is None:
                continue
            url = str(one.get("url") or "")
            if not (url.startswith("https://") or is_allowlisted_url(url)):
                report.add("DEP_EXTERNAL_URL", path + ".url", "资源地址必须是 https（内网 http 需加入 QILIN_FETCH_ALLOW_HOSTS）")
            elif mode != "off":
                unreachable_severity = "warning" if mode == "warn" else "error"
                if probed >= _MAX_PROBES:
                    report.add(
                        "DEP_EXTERNAL_URL", path + ".url",
                        f"单份清单最多检查 {_MAX_PROBES} 个外部地址",
                    )
                elif time.monotonic() > deadline:
                    report.add("DEP_EXTERNAL_UNREACHABLE", url, "地址检查超出时间预算", severity=unreachable_severity)
                else:
                    probed += 1
                    if not url_ok(probe(url)):
                        report.add("DEP_EXTERNAL_UNREACHABLE", url, "地址当时打不开", severity=unreachable_severity)
            platform = one.get("platform")
            if isinstance(platform, str):
                covered.add(platform)
        for platform in platforms:
            if platform not in covered and "any" not in covered:
                report.add("DEP_RESOURCE_GAP", path, f"平台 {platform} 没有安装地址")


def _catalog_deps(report: Report, items: list, releases: list[Release], kind: str, path: str) -> None:
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("id"):
            report.add("DEP_STORE_UNPUBLISHED", f"{path}[{index}]", "依赖缺少 id")
            continue
        dep_id = str(item["id"])
        expr = str(item.get("version") or item.get("version_range") or "*")
        matched = [
            release for release in releases
            if release.id == dep_id and release.kind == kind and _satisfies(release.version, expr)
        ]
        if not matched:
            report.add("DEP_STORE_UNPUBLISHED", f"{path}[{index}]", f"没有已发布的 {kind} {dep_id} 满足 {expr}")
