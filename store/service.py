"""上架与审核。门户和 MCP 都调用这里，不各写一份。

`Store` 按职责拆成组合：存储与审计在 base，认证与账号在 auth，提交与上传在
submissions，审核、货架与安装计划在 catalog。对外仍是同一个 `Store`，
调用方与测试不需要改。
"""

from __future__ import annotations

from store.auth import AuthMixin
from store.base import StoreBase, _MAX_BYTES, _RETRYABLE_URLS, _UPLOAD_TTL_HOURS
from store.catalog import CatalogMixin
from store.errors import Principal, StoreError
from store.models import SubmitResult
from store.reports import ReportMixin
from store.submissions import SubmissionMixin


class Store(StoreBase, AuthMixin, SubmissionMixin, CatalogMixin, ReportMixin):
    """门店的唯一入口。方法由各 mixin 提供，状态在 StoreBase。"""


__all__ = [
    "Store",
    "StoreError",
    "Principal",
    "SubmitResult",
    "_MAX_BYTES",
    "_RETRYABLE_URLS",
    "_UPLOAD_TTL_HOURS",
]
