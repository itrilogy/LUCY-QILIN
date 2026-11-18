"""版本选择和安装计划。选择只发生在 resolve，install_plan 只接受精确版本。"""

from __future__ import annotations

from store.semver import SEMVER, cmp as _cmp, key as _version_key
from store.semver import satisfies as _satisfies
from store.validator.knowledge import resolve_knowledge_tree_path
from store.validator.registry import CLIENTS, PLATFORMS

OPS = (
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
)


class PlanError(Exception):
    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message


def resolve(
    releases: list[dict],
    *,
    package_id: str,
    client: str,
    platform: str,
    version: str | None = None,
    allow_prerelease: bool = False,
) -> dict:
    if client not in CLIENTS:
        raise PlanError("CLIENT_UNKNOWN", f"不认识的 AI 工具 {client}")
    if platform not in PLATFORMS or platform == "any":
        raise PlanError("PLATFORM_UNKNOWN", "必须给出具体的操作系统和架构")
    requested = (version or "").strip()
    keep_pre = allow_prerelease or "-" in requested
    exact = bool(requested) and SEMVER.match(requested) is not None and " " not in requested and not requested.startswith((">", "<", "="))

    def usable(item: dict, *, need_platform: bool) -> bool:
        if item.get("id") != package_id or item.get("unpublished"):
            return False
        if client not in item.get("clients", []):
            return False
        if not keep_pre and "-" in str(item.get("version") or ""):
            return False
        if need_platform and not _platform_fits(item.get("platforms") or [], platform):
            return False
        return True

    pool = [item for item in releases if usable(item, need_platform=True)]
    any_platform = [item for item in releases if usable(item, need_platform=False)]
    if exact:
        chosen = next((item for item in pool if item["version"] == requested), None)
        return _result(
            package_id,
            _cards(pool),
            _card(chosen) if chosen else None,
            None if chosen is None else "highest_match",
            None if chosen else "VERSION_UNAVAILABLE",
        )
    expr = requested or "*"
    matched = [item for item in pool if _satisfies(item["version"], expr)]
    wider = [item for item in any_platform if _satisfies(item["version"], expr)]
    if not matched:
        return _result(package_id, [], None, None, "VERSION_UNAVAILABLE")
    selected = _highest(matched)
    reason = "highest_match"
    if wider and _cmp(selected["version"], _highest(wider)["version"]) < 0:
        reason = "highest_match_platform"
    return _result(package_id, _cards(matched), _card(selected), reason, None)


def install_plan(
    releases: list[dict],
    *,
    package_id: str,
    version: str,
    client: str,
    platform: str,
    base_url: str,
    _stack: tuple[str, ...] = (),
    _seen: set[tuple[str, str]] | None = None,
) -> dict:
    if _seen is None:
        _seen = set()
    requested = (version or "").strip()
    if not requested or SEMVER.match(requested) is None or " " in requested or requested.startswith((">", "<", "=")):
        raise PlanError("VERSION_REQUIRED", "install_plan 必须传入精确版本，不能省略或写范围")
    if package_id in _stack:
        raise PlanError("DEP_CYCLE", f"依赖成环：{' → '.join(_stack + (package_id,))}")
    found = next(
        (item for item in releases if item.get("id") == package_id and item.get("version") == requested and not item.get("unpublished")),
        None,
    )
    if found is None:
        raise PlanError("VERSION_UNAVAILABLE", f"没有已发布的 {package_id}@{requested}")
    if client not in found.get("clients", []):
        raise PlanError("CLIENT_MISMATCH", f"{client} 不在该版本的适用工具里")
    if not _platform_fits(found.get("platforms") or [], platform):
        raise PlanError("PLATFORM_MISMATCH", f"{requested} 没有平台 {platform}")
    stack = _stack + (package_id,)
    depends = found["manifest"].get("depends") or {}
    steps: list[dict] = []
    for dep in depends.get("store") or []:
        steps.extend(_runtime_dependency(releases, dep, client, platform, base_url, _seen))
    for dep in depends.get("external") or []:
        steps.append({
            "op": "ensure_bin",
            "name": dep.get("name"),
            "detect": dep.get("detect"),
            "install_guide": dep.get("install_guide"),
            "resources": dep.get("resources") or [],
        })
    for key, kind in (("skills", "skill"), ("mcp", "mcp")):
        for dep in depends.get(key) or []:
            steps.extend(_product_dependency(
                releases, dep, kind, client, platform, base_url, stack, _seen,
            ))
    if found["manifest"].get("kind") in {"mcp", "mcp+skill"}:
        steps.append(_mcp_add(found))
    if found["manifest"].get("kind") in {"skill", "mcp+skill"}:
        steps.extend(_archive_steps(found, base_url, "SKILLS_ROOT"))
    if found["manifest"].get("kind") == "knowledge":
        steps.extend(_archive_steps(found, base_url, "KNOWLEDGE_ROOT"))
    if found["manifest"].get("kind") == "runtime":
        steps.extend(_runtime_steps(found, platform, base_url))
    if found["manifest"].get("kind") == "plugin":
        steps.extend(_plugin_steps(found, base_url))
    for item in found["manifest"].get("secrets") or []:
        steps.append({
            "op": "need_secret",
            "name": item.get("name"),
            "description": item.get("description") or "",
        })
    steps.append(_verify_step(found, package_id, requested))
    return {
        "id": package_id,
        "version": requested,
        "kind": found["manifest"].get("kind"),
        "client": client,
        "platform": platform,
        "steps": steps,
    }


def _runtime_dependency(
    releases: list[dict],
    dep: dict,
    client: str,
    platform: str,
    base_url: str,
    seen: set[tuple[str, str]],
) -> list[dict]:
    dep_id = str(dep.get("id") or "")
    expr = str(dep.get("version") or dep.get("version_range") or "*")
    chosen = resolve(releases, package_id=dep_id, client=client, platform=platform, version=expr)
    if not chosen["selected"]:
        raise PlanError(
            chosen["rule"] or "PLATFORM_MISMATCH",
            f"依赖 {dep_id} 没有适用于 {platform} 的已发布版本",
        )
    selected = chosen["selected"]["version"]
    release = _by_version(releases, dep_id, selected)
    if release["manifest"].get("kind") != "runtime":
        raise PlanError("DEP_STORE_UNPUBLISHED", f"{dep_id} 不是已发布的 runtime")
    key = (dep_id, selected)
    if key in seen:
        return []
    seen.add(key)
    return _runtime_steps(release, platform, base_url)


def _product_dependency(
    releases: list[dict],
    dep: dict,
    kind: str,
    client: str,
    platform: str,
    base_url: str,
    stack: tuple[str, ...],
    seen: set[tuple[str, str]],
) -> list[dict]:
    dep_id = str(dep.get("id") or "")
    expr = str(dep.get("version") or dep.get("version_range") or "*")
    chosen = resolve(releases, package_id=dep_id, client=client, platform=platform, version=expr)
    if not chosen["selected"]:
        raise PlanError(chosen["rule"] or "VERSION_UNAVAILABLE", f"依赖 {dep_id} 没有可安装的已发布版本")
    selected = chosen["selected"]["version"]
    release = _by_version(releases, dep_id, selected)
    if release["manifest"].get("kind") != kind:
        raise PlanError("DEP_STORE_UNPUBLISHED", f"{dep_id}@{selected} 不是 {kind}")
    key = (dep_id, selected)
    if key in seen:
        return []
    seen.add(key)
    nested = install_plan(
        releases,
        package_id=dep_id,
        version=selected,
        client=client,
        platform=platform,
        base_url=base_url,
        _stack=stack,
        _seen=seen,
    )
    # 依赖在计划里直接展开成具体步骤。不再额外发一个 `ensure_product` 标记：
    # 它会和后面的步骤重复，外部安装器按标记再装一遍就是装两次。
    return list(nested["steps"])


def _plugin_steps(release: dict, base_url: str) -> list[dict]:
    manifest = release["manifest"]
    block = manifest.get("plugin") if isinstance(manifest.get("plugin"), dict) else {}
    digest = release.get("artifact_sha256")
    if not digest:
        raise PlanError("CHECKSUM", "已发布的插件没有制品校验和")
    entry = block.get("entry") if isinstance(block.get("entry"), str) else None
    client = block.get("client") if isinstance(block.get("client"), str) else None
    patch = str(block.get("patch") or "")
    archive = f"${{WORK}}/{release['id']}/{release['version']}.zip"
    staging = f"${{PLUGIN_STAGING}}/{release['id']}/{release['version']}"
    steps: list[dict] = [{
        "op": "download",
        "url": _artifact_url(base_url, release["id"], release["version"], None),
        "sha256": digest,
        "dest": archive,
    }]
    for name in block.get("external") or []:
        if isinstance(name, str) and name.strip():
            steps.append({"op": "check_bin", "name": name.strip()})
    steps.append({
        "op": "plugin_stage",
        "src": archive,
        "dest": staging,
        "id": release["id"],
        "version": release["version"],
        "entry": entry,
        "client": client,
        "patch": patch,
    })
    steps.append({
        "op": "plugin_confirm",
        "id": release["id"],
        "version": release["version"],
        "path": staging,
    })
    return steps


def _runtime_steps(release: dict, platform: str, base_url: str) -> list[dict]:
    manifest = release["manifest"]
    block = manifest.get("runtime") or {}
    bin_name = str(block.get("bin_name") or "")
    asset = next((item for item in block.get("assets") or [] if item.get("platform") == platform), None)
    if not asset or not bin_name:
        raise PlanError("PLATFORM_MISMATCH", f"{release['id']}@{release['version']} 没有平台 {platform} 的制品")
    dest = f"${{WORK}}/{release['id']}/{release['version']}/{bin_name}"
    return [
        {
            "op": "download",
            "url": _artifact_url(base_url, release["id"], release["version"], platform),
            "sha256": asset.get("sha256"),
            "dest": dest,
        },
        {"op": "place_bin", "src": dest, "bin_name": bin_name},
    ]


def _archive_steps(release: dict, base_url: str, root_name: str) -> list[dict]:
    digest = release.get("artifact_sha256")
    if not digest:
        raise PlanError("CHECKSUM", "已发布的压缩包没有制品校验和")
    dest = f"${{WORK}}/{release['id']}/{release['version']}.zip"
    unpack_step: dict = {
        "op": "unpack",
        "src": dest,
        "dest": f"${{{root_name}}}/{release['id']}/{release['version']}",
    }
    manifest = release.get("manifest")
    if root_name == "KNOWLEDGE_ROOT" and isinstance(manifest, dict):
        unpack_step["tree_dest"] = f"${{{root_name}}}/{resolve_knowledge_tree_path(manifest)}"
    return [
        {
            "op": "download",
            "url": _artifact_url(base_url, release["id"], release["version"], None),
            "sha256": digest,
            "dest": dest,
        },
        unpack_step,
    ]


def _verify_step(release: dict, package_id: str, version: str) -> dict:
    manifest = release["manifest"]
    kind = manifest.get("kind")
    entry = (manifest.get("skill") or {}).get("entry")
    if kind == "knowledge":
        entry = (manifest.get("knowledge") or {}).get("entry")
    if kind == "plugin":
        block = manifest.get("plugin") or {}
        entry = block.get("entry") or block.get("client")
    step: dict = {
        "op": "verify",
        "id": package_id,
        "version": version,
        "kind": kind,
        "entry": entry,
    }
    if kind == "plugin":
        step["plugin_root"] = f"${{PLUGIN_STAGING}}/{package_id}/{version}"
        step["patch"] = (manifest.get("plugin") or {}).get("patch")
        return step
    if kind not in {"mcp", "mcp+skill"}:
        return step
    block = manifest.get("mcp") or {}
    step["transport"] = block.get("transport")
    if block.get("transport") == "stdio":
        step["command"] = block.get("command")
        step["args"] = list(block.get("args") or [])
    else:
        step["url"] = block.get("url")
    return step


def _mcp_add(release: dict) -> dict:
    block = release["manifest"].get("mcp") or {}
    names = [item.get("name") for item in release["manifest"].get("secrets") or [] if isinstance(item, dict)]
    step = {
        "op": "mcp_add",
        "id": release["id"],
        "transport": block.get("transport"),
        "secrets": names,
    }
    if block.get("transport") == "stdio":
        step["command"] = block.get("command")
        step["args"] = block.get("args") or []
    else:
        step["url"] = block.get("url")
    return step


def _artifact_url(base_url: str, package_id: str, version: str, platform: str | None) -> str:
    url = f"{base_url.rstrip('/')}/v1/artifacts/{package_id}/{version}"
    if platform:
        return f"{url}?platform={platform}"
    return url


def _platform_fits(platforms: list[str], platform: str) -> bool:
    return "any" in platforms or platform in platforms


def _by_version(releases: list[dict], package_id: str, version: str) -> dict:
    found = next((item for item in releases if item.get("id") == package_id and item.get("version") == version), None)
    if found is None:
        raise PlanError("VERSION_UNAVAILABLE", f"没有已发布的 {package_id}@{version}")
    return found


def _highest(items: list[dict]) -> dict:
    return max(items, key=lambda item: _rank(item["version"]))


def _cards(items: list[dict]) -> list[dict]:
    return [_card(item) for item in sorted(items, key=lambda item: _rank(item["version"]), reverse=True)]


def _card(item: dict | None) -> dict | None:
    if item is None:
        return None
    return {
        "version": item["version"],
        "changes": item.get("changes") or "",
        "platforms": list(item.get("platforms") or []),
        "clients": list(item.get("clients") or []),
        "published_at": item.get("published_at") or "",
    }


def _result(package_id: str, candidates: list[dict], selected: dict | None, reason: str | None, rule: str | None) -> dict:
    return {
        "id": package_id,
        "candidates": candidates,
        "selected": selected,
        "reason": reason,
        "rule": rule,
    }


def _rank(version: str) -> tuple:
    """排序键委托给 `store.semver.key`，保留这个名字是为了不改既有调用点。"""
    return _version_key(version)
