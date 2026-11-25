"""日志。

这一层只为「事后能查」而设：鉴权失败、校验拒绝、未捕获异常各留一条。
不追求结构化字段的完备，只保证三件事可回答——谁、在哪个地址、因为哪条规则。

默认级别是 `WARNING`，输出到 stderr。`QILIN_LOG_LEVEL` 可调；
`store serve --log-level` 会覆盖它。不给正常请求打访问日志，
免得把有用信息淹掉。
"""

from __future__ import annotations

import logging
import os
import secrets
import sys

LOGGER = logging.getLogger("qilin")

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_configured = False


def configure(level: str | None = None) -> str:
    """配置日志，返回最终采用的级别名。重复调用只生效一次。"""
    global _configured
    name = (level or os.environ.get("QILIN_LOG_LEVEL") or "WARNING").strip().upper()
    resolved = getattr(logging, name, None)
    if not isinstance(resolved, int):
        name, resolved = "WARNING", logging.WARNING
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_FORMAT))
        LOGGER.addHandler(handler)
        LOGGER.propagate = False
        _configured = True
    LOGGER.setLevel(resolved)
    return name


def client_of(handler) -> str:
    """取请求来源。反代之后优先用 `X-Forwarded-For` 的第一段。

    这个值只用于日志，不参与任何判断，所以不做严格校验，只截断长度。
    """
    forwarded = (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    try:
        return str(handler.client_address[0])
    except (IndexError, AttributeError):
        return "unknown"


def _path(handler) -> str:
    """只留路径，不留查询串。

    `/auth/callback?code=…&state=…` 的交换码是一次性凭据；整条地址写进日志
    等于把凭据抄了一份。排障只需要知道是哪个端点。
    """
    raw = str(getattr(handler, "path", ""))
    return raw.split("?", 1)[0][:200]


def auth_failure(handler, rule: str, detail: str = "") -> None:
    """鉴权与授权失败。"""
    LOGGER.warning(
        "auth %s %s %s %s",
        rule, client_of(handler), _path(handler), detail,
    )


def rejected_request(handler, rule: str, detail: str = "") -> None:
    """业务拒绝（不是鉴权失败）。

    默认级别是 WARNING，所以这些行在被调成 INFO 之前不可见——这是刻意的：
    业务拒绝量大，不该淹没真正的告警。排障时 `QILIN_LOG_LEVEL=INFO` 打开。
    """
    LOGGER.info("reject %s %s %s %s", rule, client_of(handler), _path(handler), detail)


def storage_warning(detail: str) -> None:
    """存储与制品可读性告警（如数据库有记录但磁盘文件缺失/校验和不符）。"""
    LOGGER.warning("storage %s", detail)


def internal(handler, exc: BaseException) -> str:
    """未捕获异常，带栈。这是唯一需要完整栈的地方。

    返回一个短标识：对外响应只带它，不带异常类名与原文；排障时按它查日志。
    """
    trace = secrets.token_hex(8)
    LOGGER.error(
        "internal %s %s trace=%s",
        client_of(handler), _path(handler), trace,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return trace

