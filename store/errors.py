"""错误码、身份与规则表。

这个模块不依赖任何其他业务模块，`accounts`、`validator`、`service` 都向上依赖它，
避免再出现 `accounts` 反向 import `service` 的循环。

规则 id 以规格第 12 节为准。这里多出两个本实现自己的码：

- `INTERNAL`：未捕获异常的兜底，只出现在 HTTP 500；
- `STATE_UNBOUND`：未配置授权页时回调的 state 无法核对，只记日志。

`tests/test_rule_contract.py` 会把 `SPEC_RULES` 与规格正文逐字对齐；改规格必须
同时改这里，改这里也必须同时改规格，否则测试失败。
"""

from __future__ import annotations

from dataclasses import dataclass

# 规格第 12 节列出的全部规则 id。
SPEC_RULES = frozenset({
    "ACCOUNT_DISABLED", "AUTHOR_MISMATCH", "CHECKSUM",
    "CLIENT_CAPABILITY", "CLIENT_MISMATCH", "CLIENT_UNKNOWN",
    "DEP_CYCLE", "DEP_EXTERNAL_UNREACHABLE", "DEP_EXTERNAL_URL",
    "DEP_GUIDE_MISSING", "DEP_RESOURCE_GAP", "DEP_STORE_UNPUBLISHED",
    "DESCRIPTION", "FRONTMATTER_CONFLICT",
    "IDENTITY_INVALID", "IDENTITY_UNCONFIGURED",
    "ID_FORMAT",
    "KEY_INVALID", "KEY_MISMATCH", "KEY_TAKEN", "KIND",
    "KNOWLEDGE_MAP", "KNOWLEDGE_SCOPE", "KNOWLEDGE_TAXONOMY", "LAST_ADMIN",
    "MCP_CONNECT", "MCP_INVOKE", "MCP_LIST_FAILED", "MCP_TRANSPORT",
    "MCP_URL_UNREACHABLE", "NAME_INVALID",
    "NOTE_ENTRY", "NOTE_FILE", "NOTE_FRONTMATTER", "NOTE_LINK",
    "NOTE_ORPHAN", "NOTE_SECURITY",
    "PASSWORD_INVALID", "PASSWORD_MISMATCH",
    "PLATFORM_FILE_MISSING", "PLATFORM_MISMATCH", "PLATFORM_MIXED",
    "PLATFORM_SCOPE", "PLATFORM_TAKEN", "PLATFORM_TOO_BROAD", "PLATFORM_UNKNOWN",
    "ROLE_FORBIDDEN", "RUNTIME_ASSET",
    "SECRET_FILE", "SECRET_PATTERN", "SEMVER",
    "SKILL_ENTRY_MISSING", "SKILL_INVOKE",
    "SPEC_UNKNOWN", "SPEC_VERSION", "SUMMARY", "TITLE",
    "USERNAME_INVALID", "USERNAME_TAKEN",
    "VERSION_IMMUTABLE", "VERSION_REQUIRED", "VERSION_UNAVAILABLE",
})

EXTRA_RULES = frozenset({"INTERNAL", "STATE_UNBOUND"})
KNOWN_RULES = SPEC_RULES | EXTRA_RULES

# 需要按 warning 记日志的规则：鉴权、登录、账号状态。
AUTH_RULES = frozenset({
    "ROLE_FORBIDDEN", "IDENTITY_INVALID", "IDENTITY_UNCONFIGURED",
    "ACCOUNT_DISABLED", "KEY_INVALID", "PASSWORD_MISMATCH",
})

# HTTP 状态的例外表。其余规则一律 400。
# 这里先保持与旧行为一致；改成 401/404/409 要同时改客户端与用例，单独做。
RULE_HTTP = {
    # 身份不成立是 401，权限不够是 403；其余业务拒绝仍是 400。
    "IDENTITY_INVALID": 401,
    "IDENTITY_UNCONFIGURED": 401,
    "ROLE_FORBIDDEN": 403,
}


def http_status(rule: str) -> int:
    return RULE_HTTP.get(rule, 400)


def is_auth_rule(rule: str) -> bool:
    return rule in AUTH_RULES


class StoreError(Exception):
    """业务拒绝。`rule` 是机器判断用的规则 id，`message` 给人看。"""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    role: str
