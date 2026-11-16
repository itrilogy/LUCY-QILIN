"""提交与发布的状态迁移。

状态原来散在六处 `if row["status"] != ...` 里，`reject` 漏了一处，已发布的提交
因此可以被改成 `rejected`。这里把「谁能到哪」收成一张表，写状态的地方只查表。

状态只增不改：`withdrawn`、`expired` 是终态，不回到别的状态。
"""

from __future__ import annotations

from store.errors import StoreError

UPLOADING = "uploading"
PENDING = "pending"
PUBLISHED = "published"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
EXPIRED = "expired"

# 提交单的状态迁移。键是 (当前状态, 事件)，值是下一个状态。
SUBMISSION = {
    # 上传完成：通过、可重试（外部地址打不开）、不通过。
    (UPLOADING, "finish_ok"): PENDING,
    (UPLOADING, "finish_retryable"): UPLOADING,
    (UPLOADING, "finish_bad"): REJECTED,
    # 审核动作。`reject` 只接受待审核的提交；停在 uploading 的由清理逻辑处理。
    (PENDING, "approve"): PUBLISHED,
    (PENDING, "reject"): REJECTED,
    (PENDING, "withdraw"): WITHDRAWN,
    # 退回后作者原单修订。
    (REJECTED, "revise"): PENDING,
}

# 发布行的状态迁移。下架是单向的。
RELEASE = {
    (PUBLISHED, "unpublish"): "unpublished",
}


def advance(current: str, event: str, table: dict[tuple[str, str], str], message: str) -> str:
    """按表迁移。没有这条边就抛 `VERSION_UNAVAILABLE`。

    调用方拿到返回值后写库；不要自己拼下一个状态字符串。
    """
    nxt = table.get((current, event))
    if nxt is None:
        raise StoreError("VERSION_UNAVAILABLE", message)
    return nxt
