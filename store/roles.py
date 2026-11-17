"""角色及其能力边界。

- member：只读（枢纽侧 viewer / 未授权账号）。
- publisher：作者，默认账号角色。可提交/修订自己的产品。
- reviewer：审核。作者能力 + 审核、发布、退回、下架、审计、贡献。
- admin：管理员。审核能力 + 用户管理、角色分配、命名空间转移。
"""

from __future__ import annotations

from store.errors import StoreError

ROLE_ORDER = {"member": 0, "publisher": 1, "reviewer": 2, "admin": 3}

PUBLISHER_ROLES = frozenset({"publisher", "reviewer", "admin"})
REVIEW_ROLES = frozenset({"reviewer", "admin"})
ADMIN_ROLES = frozenset({"admin"})

ROLE_LABELS = {
    "member": "只读",
    "publisher": "作者",
    "reviewer": "审核",
    "admin": "管理员",
}


def role_allows(role: str, minimum: str) -> bool:
    return ROLE_ORDER.get(role, -1) >= ROLE_ORDER.get(minimum, 99)


def require_role(principal_role: str, minimum: str) -> None:
    if not role_allows(principal_role, minimum):
        raise StoreError("ROLE_FORBIDDEN", "当前身份不能做这件事。")
