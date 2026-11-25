"""提交与发布的状态迁移。

状态原来散在六处 `if row["status"] != ...` 里，`reject` 漏了一处，已发布的提交
因此可以被改成 `rejected`。这里把「谁能到哪」收成一张表，写状态的地方只查表。

状态只增不改：`withdrawn`、`expired` 是终态，不回到别的状态。
"""

from __future__ import annotations

from store.errors import StoreError

UPLOADING = "uploading"
VALIDATING = "validating"
PENDING = "pending"
PUBLISHED = "published"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
EXPIRED = "expired"

# 提交单的状态迁移。键是 (当前状态, 事件)，值是下一个状态。
#
# `validating` 是 `finish_*` 执行期间的占位状态：进程若在此时退出，启动时用
# `recover` 退回 `uploading`，作者可以重试。`expired` 是清理器对停在
# `uploading`/`validating` 超过 TTL 的提交给出的终态。
SUBMISSION = {
    # 上传完成：通过、可重试（外部地址打不开）、不通过。
    (UPLOADING, "start_validating"): VALIDATING,
    (UPLOADING, "finish_ok"): PENDING,
    (UPLOADING, "finish_retryable"): UPLOADING,
    (UPLOADING, "finish_bad"): REJECTED,
    (VALIDATING, "finish_ok"): PENDING,
    (VALIDATING, "finish_retryable"): UPLOADING,
    (VALIDATING, "finish_bad"): REJECTED,
    # 进程在 validating 中退出后的启动恢复。
    (VALIDATING, "recover"): UPLOADING,
    # 停在半路的提交由清理器判死。
    (UPLOADING, "expire"): EXPIRED,
    (VALIDATING, "expire"): EXPIRED,
    # 审核动作。`reject` 只接受待审核的提交；停在 uploading 的由清理或作者撤回处理。
    (PENDING, "approve"): PUBLISHED,
    (PENDING, "reject"): REJECTED,
    (PENDING, "withdraw"): WITHDRAWN,
    (UPLOADING, "withdraw"): WITHDRAWN,
    (REJECTED, "withdraw"): WITHDRAWN,
    # 退回后作者原单修订。
    (REJECTED, "revise"): PENDING,
}

# 发布行的状态迁移。下架是单向的。
RELEASE = {
    (PUBLISHED, "unpublish"): "unpublished",
}

# 消费者问题反馈的状态迁移。resolved / rejected 为终态。
REPORT_OPEN = "open"
REPORT_TRIAGED = "triaged"
REPORT_RESOLVED = "resolved"
REPORT_REJECTED = "rejected"

REPORT = {
    (REPORT_OPEN, "triage"): REPORT_TRIAGED,
    (REPORT_OPEN, "resolve"): REPORT_RESOLVED,
    (REPORT_OPEN, "reject"): REPORT_REJECTED,
    (REPORT_TRIAGED, "triage"): REPORT_TRIAGED,
    (REPORT_TRIAGED, "resolve"): REPORT_RESOLVED,
    (REPORT_TRIAGED, "reject"): REPORT_REJECTED,
}


def advance(
    current: str,
    event: str,
    table: dict[tuple[str, str], str],
    message: str,
    *,
    rule: str = "SUBMISSION_STATE_INVALID",
) -> str:
    """按表迁移。没有这条边就抛指定规则码（默认 `SUBMISSION_STATE_INVALID`）。

    调用方拿到返回值后写库；不要自己拼下一个状态字符串。
    """
    nxt = table.get((current, event))
    if nxt is None:
        raise StoreError(rule, message)
    return nxt
