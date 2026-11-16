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
    "claude-desktop": {"mcp_stdio", "mcp_http"},
    "cursor": {"mcp_stdio", "mcp_http"},
    "generic": set(),
}

KINDS = {"mcp", "skill", "mcp+skill", "runtime", "knowledge"}
