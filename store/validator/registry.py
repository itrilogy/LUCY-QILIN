"""商店认识的 kind、AI 工具与平台。

这里不放「既有技能目录 → id」的对照表。商品身份由清单里的 `id` 决定，
目录名只是文件夹；同一 id 归首个发布者。历史对照见规格第 14.4 节。
"""

PLATFORMS = {
    "darwin-arm64",
    "darwin-amd64",
    "linux-arm64",
    "linux-amd64",
    "windows-arm64",
    "windows-amd64",
    "any",
}

# v1 客户端能力。generic 没有能力，不能用来绕过闭合检查。
CLIENTS: dict[str, set[str]] = {
    "reasonix": {"skill_dir", "mcp_stdio", "local_bin"},
    "antigravity": {"skill_dir", "mcp_stdio", "mcp_http", "local_bin"},
    "claude-desktop": {"mcp_stdio", "mcp_http"},
    "cursor": {"mcp_stdio", "mcp_http"},
    "deepseek-harness": {"skill_dir", "mcp_stdio", "mcp_http", "local_bin", "plugin"},
    "generic": set(),
}

KINDS = {"mcp", "skill", "mcp+skill", "runtime", "knowledge", "plugin"}

# 插件分级。A：零 npm 运行时依赖，可声明外部 CLI；B：白名单纯 JS 依赖；
# C：原生模块，v1 只识别、不接受。Tier B 白名单从空开始，新增依赖要先评审，
# 再改这里，不允许在清单里自行声明。
PLUGIN_TIERS = {"A", "B", "C"}
PLUGIN_TIER_B_ALLOWLIST: frozenset[str] = frozenset()
PLUGIN_CAPABILITIES = {
    "tools", "client-slots", "commands", "services",
    "network", "credentials:read", "credentials:write",
    "filesystem:read", "filesystem:write", "process:spawn",
    "session:read", "session:write",
}

# 插件审核清单。approve 前必须由 admin 逐项确认；只做人工核对，不做自动扫描。
PLUGIN_REVIEW_ITEMS = (
    "source_readable",
    "no_install_scripts",
    "dependencies_reviewed",
    "capabilities_checked",
    "external_commands_checked",
    "lifecycle_cleanup",
    "no_secrets",
    "dsh_range_checked",
)
