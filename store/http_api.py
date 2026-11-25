"""上架 HTTP、商店 MCP 和栖林门户。只转发到 Store，不另写校验。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from store.diagnostics import auth_failure, internal as log_internal, rejected_request, storage_warning
from store.mcp_api import PROTOCOL_VERSIONS, handle
from store.errors import StoreError, http_status, is_auth_rule
from store.service import Store, _MAX_BYTES

# 请求体上限。`files` 走 base64，膨胀约 1.34 倍；若与制品上限共用同一个
# 数值，门户路径上 32MiB 的制品会必然超限。这里给正文留出 base64 的余量，
# 制品本身仍由 `_MAX_BYTES` 约束，两者不再互相卡住。
_MAX_BODY = (_MAX_BYTES // 3 + 1) * 4 + 1024 * 1024

# 门户与静态资源随包分发，`pip install` 之后也能打开首页。
# 不再引用仓库外的 `LUCY-DESIGN`：换台机器克隆就掉样式的问题由此消除。
class _RateLimiter:
    """单进程滑动窗口限流。多进程/反代后要配 fail2ban 或在网关再做一层。"""

    def __init__(self, limit: int, window: float, max_keys: int = 4096) -> None:
        self.limit = max(1, int(limit))
        self.window = float(window)
        self.max_keys = max(64, int(max_keys))
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            cutoff = moment - self.window
            if len(self._events) >= self.max_keys and key not in self._events:
                # 只清过期桶。表满而仍有新键时**拒绝新键**，不整表清空：
                # 清空等于把所有人（含正在被限流的人）的窗口一起归零，
                # 一次随机键洪水就能给攻击者解封。
                stale = [k for k, vals in self._events.items() if not vals or vals[-1] <= cutoff]
                for k in stale:
                    self._events.pop(k, None)
                if len(self._events) >= self.max_keys:
                    return False
            events = [item for item in self._events.get(key, []) if item > cutoff]
            if len(events) >= self.limit:
                self._events[key] = events
                return False
            events.append(moment)
            self._events[key] = events
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


PORTAL_DIR = Path(__file__).resolve().parent / "portal"
PORTAL = PORTAL_DIR / "index.html"
DESIGN_FILES = {
    "/design/tokens.css": PORTAL_DIR / "assets" / "tokens.css",
    "/design/qilin.svg": PORTAL_DIR / "assets" / "qilin.svg",
    "/design/luxi-lab-gold.svg": PORTAL_DIR / "assets" / "luxi-lab-gold.svg",
    # favicon 与 qilin.svg 同源（同一套色与图形，按 16–32px 光学加粗）。
    # 根上的 /favicon.ico 是浏览器自动请求的保底。
    "/design/favicon.svg": PORTAL_DIR / "assets" / "favicon.svg",
    "/design/apple-touch-icon.png": PORTAL_DIR / "assets" / "apple-touch-icon.png",
    "/favicon.ico": PORTAL_DIR / "assets" / "favicon.ico",
    "/portal/styles.css": PORTAL_DIR / "styles.css",
    "/portal/app.js": PORTAL_DIR / "app.js",
    "/portal/manifest_merge.js": PORTAL_DIR / "manifest_merge.js",
}
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
CSP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; "
    "script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'"
)
# 跳转登录的一次性 state。存 Cookie，不进地址栏以外的任何响应体。
# 名称带 hub 前缀，与会话 Cookie 分开，免得登出时一起被清掉。
_STATE_COOKIE = "qilin_hub_state"
_STATE_TTL = 600

# 上传票据在 URL 里（`?ticket=`），而访问日志会原样记录 requestline。
# 票据是 1 小时有效的能力凭据，不能落进日志（评审 P2-9）。
_TICKET_QUERY = re.compile(r"(ticket=)[^&\s\"]+")


def _redact_ticket(text: str) -> str:
    return _TICKET_QUERY.sub(r"\1<redacted>", text)


def _parse_range(spec: str, size: int) -> tuple[int, int] | None:
    """解析单个 `Range: bytes=...`。不支持多段；不合法返回 None（回 416）。"""
    if not spec.startswith("bytes=") or "," in spec or size <= 0:
        return None
    raw = spec[len("bytes="):].strip()
    start_text, _, end_text = raw.partition("-")
    try:
        if not start_text.strip():
            length = int(end_text)
            if length <= 0:
                return None
            start, end = max(0, size - length), size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text.strip() else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _parse_trusted_proxies(
    raw: str | list[str] | set[str] | tuple[str, ...] | None,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """解析可信反代 IP / CIDR 列表。非法条目跳过，避免环境变量拼写错误阻断启动。"""
    if not raw:
        return ()
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in items:
        entry = str(item).strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _ip_in_networks(
    ip_text: str, networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    try:
        addr = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in networks)


# 这些 POST 不需要登录：登录本身、注册（空库另有闸门）、MCP 的匿名只读、
# 匿名问题反馈、以及枢纽登出回调。其余 POST 在读请求体之前就要确认有身份——否则匿名用户
# 可以用一个 44MB 的 POST 白耗带宽与内存（评审 P2-14）。
_PUBLIC_POST_PATHS = frozenset({
    "/v1/login", "/v1/account", "/mcp", "/auth/logout/backchannel",
    # 安装计划与问题反馈不需要登录：制品地址本来就是匿名的，门户只是在未登录时不展示
    # 这个动作。不要把 API 收紧成比它托管的制品更严。
    "/v1/install-plan",
    "/v1/reports",
})

# 响应附加头。用列表而不是字典，因为 `Set-Cookie` 允许同名头重复出现，
# 一次响应可能同时要下发会话 Cookie 和清除 state 的 Cookie。
Headers = list[tuple[str, str]]


def make_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1：默认保持连接。门户一次加载要取 6–7 个静态资源，以前每个
        # 都是一条新 TCP + 新 SQLite 连接（评审 P2-10 的剩余部分）。
        # 代价是错误响应必须处理"正文没读完"——见 `_close_if_body_unread`。
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(30)
            self._head_only = False
            self._body_read = False

        def _client_ip(self) -> str:
            peer = self.client_address[0]
            if not getattr(self.server, "trust_proxy", False):
                return peer
            trusted = getattr(self.server, "trusted_proxies", ())
            if trusted and not _ip_in_networks(peer, trusted):
                return peer
            xff = (self.headers.get("X-Forwarded-For") or "").strip()
            if xff:
                parts = [item.strip() for item in xff.split(",") if item.strip()]
                for candidate in reversed(parts):
                    if len(candidate) > 64:
                        continue
                    if trusted and _ip_in_networks(candidate, trusted):
                        continue
                    return candidate
            real_ip = (self.headers.get("X-Real-IP") or "").strip()
            if real_ip and len(real_ip) <= 64:
                return real_ip
            return peer

        def _limit(self, bucket: str, subject: str = "") -> None:
            limiter = self.server.limiters[bucket]
            key = f"{self._client_ip()}:{subject}" if subject else self._client_ip()
            if not limiter.allow(key):
                raise StoreError("RATE_LIMITED", "请求过于频繁，请稍后再试。")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            token = _credential(self)
            try:
                # 枢纽令牌验签要取公钥（网络）。放在锁外，慢请求不会挡住只读接口。
                store.prepare_token(token)
                # 刷新 hub 会话要读数据库与网络，一并放进 try，避免异常打断连接。
                if path != "/auth/callback":
                    store.refresh_hub_if_due(token)
                if path == "/auth/callback":
                    code = _one(query, "code")
                    state = _one(query, "state")
                    if not code.strip() or not state.strip() or len(state) > 256:
                        raise StoreError("IDENTITY_INVALID", "跳转缺少交换码。")
                    # state 要能对上「商店此前发给同一个浏览器的那一只 Cookie」，
                    # 才说明这次回调来自本浏览器发起的登录，而不是别人替我们点开的链接。
                    # 比对用 compare_digest，避免按前缀猜值。
                    #
                    # 只有配置了授权页（`--hub-authorize`）时商店才掌控 state 的取值。
                    # 未配置时枢纽自行发起跳转，state 由枢纽决定，商店无从预知；
                    # 这种情况下仍接受回调，但每次记一条 STATE_UNBOUND，便于运维发现
                    # 站点还在「未绑定」状态，而不是悄悄当成已修好。
                    issued = _cookie(self.headers.get("Cookie", ""), _STATE_COOKIE)
                    bound = bool(getattr(self.server, "hub_authorize", ""))
                    if issued:
                        if not hmac.compare_digest(issued, state):
                            raise StoreError("IDENTITY_INVALID", "这次跳转没有对应的登录请求。")
                    elif bound:
                        raise StoreError("IDENTITY_INVALID", "这次跳转没有对应的登录请求。")
                    else:
                        auth_failure(self, "STATE_UNBOUND", "未配置授权页，回调的 state 无法核对")
                    _profile, session = store.login_hub_code(code)
                    self._redirect(
                        "/",
                        _merge_headers(
                            _session_cookie(session, secure=_secure(self)),
                            _state_cookie("", clear=True, secure=_secure(self)),
                        ),
                    )
                    return
                if path == "/auth/start":
                    # 由商店签发 state 并带人去枢纽授权页。配置了授权页才有这条路。
                    template = getattr(self.server, "hub_authorize", "")
                    if not template:
                        raise StoreError("IDENTITY_UNCONFIGURED", "还没配置枢纽授权页，不能用跳转登录。")
                    state = secrets.token_urlsafe(24)
                    status = store.identity_status()
                    self._redirect(
                        _authorize_url(template, state, _base(self), status.get("app_id") or ""),
                        _state_cookie(state, secure=_secure(self)),
                    )
                    return
                if path == "/mcp":
                    # 商店不提供单独的 SSE 流。规范要求这时回 405，并带 Allow。
                    # 头必须是 (name, value) 列表。传字典会在状态行已经写入之后抛错，
                    # 异常处理再补一条 400，客户端会把第二条状态行当成坏掉的头。
                    self._empty(405, [("Allow", "POST")])
                    return
                if path == "/api/livez":
                    # 存活：进程还能回答问题。不碰数据库，编排器据此判断"要不要重启"。
                    self._send(200, {"status": "ok"})
                    return
                if path in {"/api/health", "/api/readyz"}:
                    # 就绪：库可读、制品目录可写。503 表示"先别给我流量"。
                    identity = store.identity_status()
                    health = store.health()
                    self._send(
                        200 if health["status"] == "ok" else 503,
                        {
                            "status": health["status"],
                            "app_id": identity["app_id"],
                            "identity": identity["mode"],
                            "database": health["database"],
                            "blobs": health["blobs"],
                        },
                    )
                    return
                if path == "/v1/identity":
                    info = store.identity_status()
                    # 能否由商店发起跳转，取决于有没有配授权页。门户据此决定
                    # 是否显示「用实验室账号登录」的入口，不去猜。
                    info["hub_start"] = bool(getattr(self.server, "hub_authorize", ""))
                    # 注册开关与"有没有管理员"：门户据此决定是否显示注册表单。
                    # 空库时 HTTP 注册一律拒绝，管理员只能由 `store init-admin` 建。
                    info["registration"] = store.registration_mode()
                    info["has_admin"] = store.has_admin()
                    self._send(200, info)
                    return
                if path in {"/", "/qilin-portal.html"}:
                    self._file(PORTAL)
                    return
                if path in DESIGN_FILES:
                    self._file(DESIGN_FILES[path])
                    return
                if path == "/v1/me":
                    me = store.whoami(token)
                    if me.get("role") not in {None, "anonymous"}:
                        me["mcp_url"] = _base(self).rstrip("/") + "/mcp"
                    self._send(200, me)
                    return
                if path == "/v1/accounts":
                    self._send(200, {"items": store.list_accounts(token)})
                    return
                if path == "/v1/accounts/manage":
                    self._send(200, {"items": store.list_accounts(token)})
                    return
                if path == "/v1/catalog":
                    self._send(200, store.search_page(
                        _one(query, "query"),
                        kind=_one(query, "kind") or None,
                        client=_one(query, "client") or None,
                        platform=_one(query, "platform") or None,
                        limit=_int(query, "limit", None, lo=1, hi=500),
                        sort=_one(query, "sort") or "updated",
                        offset=_int(query, "offset", 0, lo=0, hi=_MAX_OFFSET),
                    ))
                    return
                if path == "/v1/mine":
                    self._send(200, {"items": store.list_my_submissions(token)})
                    return
                if path == "/v1/audit.csv":
                    text = store.audit_csv(
                        token,
                        package_id=_one(query, "package_id") or None,
                        action=_one(query, "action") or None,
                    )
                    self._csv(text, "qilin-audit.csv")
                    return
                if path == "/v1/contributions":
                    self._send(200, store.contributions(
                        token,
                        account_id=_one(query, "account_id") or None,
                        limit=_int(query, "limit", 100, lo=1, hi=500),
                    ))
                    return
                if path == "/v1/audit":
                    self._send(200, store.list_audit(
                        token,
                        limit=_int(query, "limit", 100, lo=1, hi=500),
                        offset=_int(query, "offset", 0, lo=0, hi=_MAX_OFFSET),
                        package_id=_one(query, "package_id") or None,
                        action=_one(query, "action") or None,
                    ))
                    return
                if path == "/v1/reviews":
                    self._send(200, {"items": store.list_reviews(token)})
                    return
                if path == "/v1/reports":
                    limit = _int(query, "limit", 100, lo=1, hi=500) or 100
                    offset = _int(query, "offset", 0, lo=0, hi=_MAX_OFFSET) or 0
                    if _one(query, "mine") == "1":
                        self._send(200, store.list_my_reports(token, limit=limit, offset=offset))
                    elif _one(query, "product") == "1":
                        self._send(200, store.list_product_reports(
                            token,
                            package_id=_one(query, "id") or None,
                            version=_one(query, "version") or None,
                            category=_one(query, "category") or None,
                            status=_one(query, "status") or None,
                            limit=limit,
                            offset=offset,
                        ))
                    else:
                        self._send(200, store.list_reports(
                            token,
                            package_id=_one(query, "id") or None,
                            kind=_one(query, "kind") or None,
                            category=_one(query, "category") or None,
                            status=_one(query, "status") or None,
                            limit=limit,
                            offset=offset,
                        ))
                    return
                if path.startswith("/v1/reports/") and path.count("/") == 3:
                    self._send(200, store.get_report(token, unquote(path.split("/")[3])))
                    return
                if path.startswith("/v1/reviews/"):
                    self._send(200, store.get_review(token, path.split("/")[3]))
                    return
                if path.startswith("/v1/submissions/") and path.count("/") == 3:
                    self._send(200, store.get_submission(token, path.split("/")[3]))
                    return
                if path.startswith("/v1/packages/") and path.endswith("/versions"):
                    package_id = path.split("/")[3]
                    self._send(200, {"versions": store.list_versions(
                        package_id,
                        client=_one(query, "client") or None,
                        platform=_one(query, "platform") or None,
                    )})
                    return
                if path.startswith("/v1/packages/"):
                    parts = [part for part in path.split("/") if part]
                    if len(parts) != 4:
                        raise StoreError("RESOURCE_NOT_FOUND", "没有这个地址")
                    self._send(200, store.describe(parts[2], parts[3]))
                    return
                if path.startswith("/v1/artifacts/"):
                    package_id, version = _artifact_key(path)
                    archive, digest = store.artifact_file(
                        package_id, version, _one(query, "platform") or None,
                    )
                    self._send_artifact(archive, digest)
                    return
                self._send(404, {"rule": "RESOURCE_NOT_FOUND", "message": "没有这个地址"})
            except StoreError as exc:
                self._fail(exc)
            except (KeyError, json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
                # 清单与请求体来自外部：字段缺失、类型不对都在这里收口，
                # 不能让异常冒到 BaseHTTPRequestHandler，那会直接断开连接。
                # 响应只给一句可操作的话：异常类名与原文可能带内部路径/取值，
                # 旧实现把 "ValueError: invalid literal ..." 原样回给了客户端。
                self._send(400, {"rule": "SPEC_VERSION", "message": _bad_request(exc)})
            except Exception as exc:  # noqa: BLE001 - 兜底，避免单个请求打断连接
                trace = log_internal(self, exc)
                # 异常原文只进日志。对外给一个可检索的短标识，不回显路径与 SQL。
                self._send(500, {"rule": "INTERNAL", "message": "服务器内部错误", "trace_id": trace})

        def do_HEAD(self) -> None:
            """HEAD = 与 GET 同样的状态与头部，不发正文（监控与 CDN 探活要用）。"""
            self._head_only = True
            try:
                self.do_GET()
            finally:
                self._head_only = False

        def do_OPTIONS(self) -> None:
            """给浏览器与探针一个明确的 OPTIONS，不发一堆无用的 HTML 错误页。"""
            self.send_response(204)
            self.send_header("Allow", "GET, HEAD, POST, PUT, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            token = _credential(self)
            try:
                store.prepare_token(token)
                store.refresh_hub_if_due(token)
                _require_json(self)
                # 先验身份再读正文：匿名的大请求体不该被读完再拒。
                if path.startswith("/v1/") and path not in _PUBLIC_POST_PATHS:
                    if store.principal_from_token(token) is None:
                        raise StoreError("AUTH_REQUIRED", "请先登录。")
                raw = _read_body(self)
                body = json.loads(raw or b"{}")
                # 顶层必须是对象，否则后面的 body.get 全都不成立。
                if not isinstance(body, dict):
                    if isinstance(body, list) and path == "/mcp":
                        # 批量请求：2025-03-26 之后规范已不支持。明确回 JSON-RPC 错误，
                        # 而不是一个笼统的 400 业务码。
                        self._send(200, {
                            "jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "不支持批量请求，请逐条发送"},
                        })
                        return
                    raise StoreError("SPEC_VERSION", "请求体必须是 JSON 对象")
                if path == "/v1/account":
                    self._limit("register")
                    # 空库不开放自助注册：管理员只能由 `store init-admin` 建。
                    # 旧行为是"第一个注册的人自动是管理员"，空库暴露到公网就等于
                    # 把整站交给第一个请求。
                    if not store.has_admin():
                        raise StoreError(
                            "REGISTRATION_CLOSED",
                            "商店还没有管理员。请在服务器上执行 store init-admin 后再注册。",
                        )
                    if str(body.get("key") or "").strip():
                        raise StoreError("KEY_INVALID", "KEY 由商店生成，不能自己设置。")
                    password = str(body.get("password") or "")
                    again = body.get("password_again")
                    if again is not None and str(again) != password:
                        raise StoreError("PASSWORD_MISMATCH", "两次口令不一致。")
                    profile, session, key = store.register_account(
                        str(body.get("username") or ""),
                        str(body.get("real_name") or ""),
                        str(body.get("nickname") or ""),
                        str(body.get("department") or ""),
                        str(body.get("job_title") or ""),
                        password,
                    )
                    self._send(200, _issued(profile, key, _base(self)), _session_cookie(session, secure=_secure(self)))
                    return
                if path == "/v1/login":
                    username = str(body.get("username") or "").strip().lower()
                    # 同时限制单个 IP 和 (IP, 用户名)，避免换用户名绕过每 IP 次数。
                    self._limit("login")
                    self._limit("login", username)
                    profile, session = store.login_account(
                        str(body.get("username") or ""),
                        str(body.get("password") or ""),
                    )
                    # 登录成功**不清零**限流桶：旧实现一成功就 reset(ip)，
                    # 注册一个自有账号、交替「登自己一次 → 猜别人一次」即可无限爆破。
                    # 窗口按时间自然过期，5 分钟 20 次对正常用户足够。
                    self._send(200, profile, _session_cookie(session, secure=_secure(self)))
                    return
                if path == "/v1/logout":
                    store.logout_account(token)
                    self._send(200, {"status": "logged_out"}, _session_cookie("", clear=True, secure=_secure(self)))
                    return
                if path == "/v1/account/profile":
                    profile = store.update_profile(
                        token,
                        str(body.get("real_name") or ""),
                        str(body.get("nickname") or ""),
                        str(body.get("department") or ""),
                        str(body.get("job_title") or ""),
                    )
                    self._send(200, profile)
                    return
                if path == "/v1/account/password":
                    profile = store.change_password(
                        token,
                        str(body.get("old_password") or ""),
                        str(body.get("new_password") or ""),
                    )
                    self._send(200, profile)
                    return
                if path == "/v1/account/key":
                    supplied = str(body.get("key") or body.get("new_key") or body.get("old_key") or "")
                    if supplied.strip():
                        raise StoreError("KEY_INVALID", "KEY 由商店生成，不能自己设置。")
                    profile, session, key = store.issue_account_key(token)
                    self._send(200, _issued(profile, key, _base(self)), _session_cookie(session, secure=_secure(self)))
                    return
                if path.startswith("/v1/accounts/") and path.endswith("/password"):
                    if str(body.get("password") or body.get("new_password") or "").strip():
                        raise StoreError("PASSWORD_INVALID", "口令由商店生成，不能自己设置。")
                    self._send(200, store.reset_account_password(token, path.split("/")[3]))
                    return
                if path == "/v1/accounts":
                    self._send(200, store.create_account(
                        token,
                        str(body.get("username") or ""),
                        str(body.get("real_name") or ""),
                        str(body.get("nickname") or ""),
                        str(body.get("role") or "publisher"),
                    ))
                    return
                if path.startswith("/v1/namespaces/") and path.endswith("/transfer"):
                    parts = [part for part in path.split("/") if part]
                    if len(parts) != 4:
                        raise StoreError("RESOURCE_NOT_FOUND", "没有这个地址")
                    store.transfer_namespace(token, parts[2], str(body.get("account_id") or ""))
                    self._send(200, {"status": "transferred"})
                    return
                if path.startswith("/v1/accounts/") and path.endswith("/role"):
                    store.set_account_role(token, path.split("/")[3], str(body.get("role") or ""))
                    self._send(200, {"status": "updated"})
                    return
                if path.startswith("/v1/accounts/") and path.endswith("/disable"):
                    store.disable_account(token, path.split("/")[3])
                    self._send(200, {"status": "disabled"})
                    return
                if path == "/mcp":
                    # 无状态 MCP 端点：不签发 Mcp-Session-Id，亦不因客户端携带任意会话头而回 404，
                    # 彻底消除长会话 TTL 过期与进程重启导致的断连。
                    # initialize 自身承担版本协商，不受请求头版本预检拦截；非 initialize 请求若显式声明
                    # 未知协议版本仍回 400。
                    declared = (self.headers.get("MCP-Protocol-Version") or "").strip()
                    if body.get("method") != "initialize" and declared and declared not in PROTOCOL_VERSIONS:
                        raise StoreError(
                            "MCP_TRANSPORT",
                            f"不支持的 MCP 协议版本：{declared[:32]}",
                        )
                    result = handle(
                        store, token, body, base_url=_base(self), source_ip=self._client_ip(),
                    )
                    if result is None:
                        self._empty(202)
                        return
                    self._mcp(200, result, [])
                    return
                if path == "/auth/logout/backchannel":
                    store.accept_logout(str(body.get("logout_token") or ""))
                    self._empty(204)
                    return
                if path == "/v1/validate":
                    manifest = body["manifest"]
                    has_files = body.get("files") is not None
                    has_skill_md = body.get("skill_md") is not None
                    inline_files = None
                    if has_files or has_skill_md:
                        inline_files = _files(body) if has_files else {}
                        if has_skill_md:
                            raw_skill_md = body["skill_md"]
                            if not isinstance(raw_skill_md, str):
                                raise StoreError("CHECKSUM", "skill_md 必须是字符串")
                            inline_files.setdefault("SKILL.md", raw_skill_md.encode("utf-8"))
                    findings = store.explain_manifest(token, manifest, files=inline_files)
                    rules = [item["rule"] for item in findings if item["severity"] == "error"]
                    self._send(200, {
                        "ok": not rules,
                        "scope": "package" if inline_files is not None else "manifest_only",
                        "rules": rules,
                        "findings": findings,
                    })
                    return
                if path == "/v1/reports":
                    created = store.report_problem(
                        token,
                        body.get("id"),
                        body.get("version"),
                        category=body.get("category"),
                        summary=body.get("summary"),
                        steps=body.get("steps") or "",
                        client=body.get("client") or "",
                        platform=body.get("platform") or "",
                        contact=body.get("contact") or "",
                        source_ip=self._client_ip(),
                        channel="http",
                    )
                    self._send(200, created)
                    return
                if path.startswith("/v1/reports/") and path.endswith("/triage"):
                    parts = [part for part in path.split("/") if part]
                    if len(parts) != 4:
                        raise StoreError("RESOURCE_NOT_FOUND", "没有这个地址")
                    updated = store.triage_report(
                        token,
                        unquote(parts[2]),
                        status=str(body.get("status") or ""),
                        resolution=str(body.get("resolution") or ""),
                        channel="http",
                    )
                    self._send(200, updated)
                    return
                if path == "/v1/install-plan":
                    self._send(200, store.make_install_plan(
                        body["id"],
                        str(body.get("version") or ""),
                        client=body["client"],
                        platform=body["platform"],
                        base_url=_base(self),
                    ))
                    return
                if path == "/v1/submissions/begin":
                    started = store.begin_submit(
                        token,
                        body["manifest"],
                        submit_kind=str(body.get("submit_kind") or "new_version"),
                        byte_size=int(body["byte_size"]),
                        channel="portal",
                    )
                    _format_started_urls(started, _base(self))
                    self._send(200, started)
                    return
                if path.startswith("/v1/submissions/") and path.endswith("/finish"):
                    result = store.finish_submit(token, unquote(path.split("/")[3]), channel="portal")
                    self._send(200, _result(result))
                    return
                if path == "/v1/submissions":
                    result = store.submit(
                        token, body["manifest"], _files(body), channel="portal",
                        submit_kind=str(body.get("submit_kind") or "new_version"),
                    )
                    self._send(200, _result(result))
                    return
                if path.startswith("/v1/submissions/") and path.endswith("/revise-begin"):
                    started = store.begin_revise(
                        token,
                        path.split("/")[3],
                        body["manifest"],
                        byte_size=int(body["byte_size"]),
                    )
                    _format_started_urls(started, _base(self))
                    self._send(200, started)
                    return
                if path.startswith("/v1/submissions/") and path.endswith("/revise-finish"):
                    result = store.finish_revise(token, path.split("/")[3])
                    self._send(200, _result(result))
                    return
                if path.startswith("/v1/submissions/") and path.endswith("/revise"):
                    result = store.revise(token, path.split("/")[3], body["manifest"], _files(body))
                    self._send(200, _result(result))
                    return
                if path.startswith("/v1/submissions/") and path.endswith("/plugin-review"):
                    review = store.review_plugin(
                        token, path.split("/")[3], body.get("checklist") or {},
                    )
                    self._send(200, review)
                    return
                if path.startswith("/v1/submissions/") and path.endswith("/approve"):
                    store.approve(token, path.split("/")[3])
                    self._send(200, {"status": "published"})
                    return
                if path.startswith("/v1/submissions/") and path.endswith("/reject"):
                    store.reject(token, unquote(path.split("/")[3]), str(body.get("reason") or ""))
                    self._send(200, {"status": "rejected"})
                    return
                if path.startswith("/v1/submissions/") and path.endswith("/withdraw"):
                    store.withdraw(token, unquote(path.split("/")[3]))
                    self._send(200, {"status": "withdrawn"})
                    return
                if path.startswith("/v1/packages/") and path.endswith("/unpublish"):
                    parts = [part for part in path.split("/") if part]
                    if len(parts) != 5:
                        raise StoreError("RESOURCE_NOT_FOUND", "没有这个地址")
                    store.unpublish(
                        token, unquote(parts[2]), unquote(parts[3]), str(body.get("reason") or "")
                    )
                    self._send(200, {"status": "unpublished"})
                    return
                self._send(404, {"rule": "RESOURCE_NOT_FOUND", "message": "没有这个地址"})
            except StoreError as exc:
                self._fail(exc)
            except (KeyError, json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
                # 清单与请求体来自外部：字段缺失、类型不对都在这里收口，
                # 不能让异常冒到 BaseHTTPRequestHandler，那会直接断开连接。
                # 响应只给一句可操作的话：异常类名与原文可能带内部路径/取值，
                # 旧实现把 "ValueError: invalid literal ..." 原样回给了客户端。
                self._send(400, {"rule": "SPEC_VERSION", "message": _bad_request(exc)})
            except Exception as exc:  # noqa: BLE001 - 兜底，避免单个请求打断连接
                trace = log_internal(self, exc)
                # 异常原文只进日志。对外给一个可检索的短标识，不回显路径与 SQL。
                self._send(500, {"rule": "INTERNAL", "message": "服务器内部错误", "trace_id": trace})

        def do_PUT(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            ticket = (
                (self.headers.get("X-Upload-Ticket") or "").strip()
                or (parse_qs(parsed.query).get("ticket") or [""])[0]
            )
            token = _credential(self)
            if not path.startswith("/v1/uploads/"):
                self._send(404, {"rule": "RESOURCE_NOT_FOUND", "message": "没有这个地址"})
                return
            try:
                # 有 ticket 时不再要求或校验 Authorization：独立终端只凭 URL 上传。
                if token and not ticket:
                    store.prepare_token(token)
                    store.refresh_hub_if_due(token)
                # 同样先验身份再读正文；带 ticket 的上传不需要登录。
                if not ticket and store.principal_from_token(token) is None:
                    raise StoreError("AUTH_REQUIRED", "请先登录。")
                data = _read_body(self)
                parts = [part for part in path.split("/") if part]
                if len(parts) == 4 and parts[2] == "revise":
                    store.save_revise_upload(token, parts[3], data, ticket=ticket)
                else:
                    store.save_upload(token, parts[2], data, ticket=ticket)
                self._send(200, {"status": "uploaded"})
            except StoreError as exc:
                self._fail(exc)
            except Exception as exc:  # noqa: BLE001 - 兜底，避免单个请求打断连接
                trace = log_internal(self, exc)
                # 异常原文只进日志。对外给一个可检索的短标识，不回显路径与 SQL。
                self._send(500, {"rule": "INTERNAL", "message": "服务器内部错误", "trace_id": trace})

        def _close_if_body_unread(self) -> None:
            """错误响应如果没读完正文，就不能复用这条连接。

            HTTP/1.1 keep-alive 下，套接字里残留的正文会被当成下一条请求解析
            （请求走私的经典形态）。这里直接标记关闭。
            """
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            if length > 0 and not self._body_read:
                self.close_connection = True

        def _fail(self, exc: StoreError) -> None:
            """把 `StoreError` 映射成响应，并留下一条记录。

            状态码规则与早先一致：只有 `ROLE_FORBIDDEN` 回 403，其余回 400。
            日志方面，鉴权与登录相关的规则记 warning，业务拒绝记 info——
            业务拒绝量大，不必都当告警。这是本轮补的「事后可查」的最小集合。
            """
            if is_auth_rule(exc.rule):
                auth_failure(self, exc.rule, exc.message)
            else:
                # 业务拒绝此前完全不留痕：作者说"提交被拒"时无法事后归因。
                rejected_request(self, exc.rule, exc.message)
            self._close_if_body_unread()
            self._send(http_status(exc.rule), {"rule": exc.rule, "message": exc.message})

        def log_message(self, fmt: str, *args: object) -> None:
            if os.environ.get("QILIN_ACCESS_LOG") == "1":
                line = _redact_ticket(fmt % args)
                sys.stderr.write(
                    "%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), line)
                )

        def _send(self, status: int, payload: dict, extra: Headers | None = None) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if status == 401:
                # RFC 7235：401 要带挑战头，客户端才知道该拿什么凭据重试。
                self.send_header("WWW-Authenticate", 'Bearer realm="qilin"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            if self.close_connection:
                self.send_header("Connection", "close")
            for name, value in _pairs(extra):
                self.send_header(name, value)
            self.end_headers()
            if not self._head_only:
                self.wfile.write(raw)

        def _mcp(self, status: int, payload: dict, extra: Headers) -> None:
            accept = self.headers.get("Accept", "")
            raw = json.dumps(payload, ensure_ascii=False).encode()
            if "text/event-stream" in accept and "application/json" not in accept:
                data = b"event: message\ndata: " + raw + b"\n\n"
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                for name, value in _pairs(extra):
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)
                return
            self._send(status, payload, extra)

        def _redirect(self, location: str, extra: Headers | None = None) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            for name, value in _pairs(extra):
                self.send_header(name, value)
            self.end_headers()

        def _empty(self, status: int, extra: Headers | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            for name, value in _pairs(extra):
                self.send_header(name, value)
            self.end_headers()

        def _send_artifact(self, archive: Path, digest: str) -> None:
            """流式发制品，支持 Range/206 与 If-None-Match，并在发响应头前完成流式摘要核对。"""
            try:
                size = archive.stat().st_size
            except OSError as exc:
                storage_warning(f"artifact missing on disk: {archive}")
                raise StoreError("ARTIFACT_MISSING", "制品文件不可读") from exc
            etag = '"' + digest + '"'
            if (self.headers.get("If-None-Match") or "").strip() == etag:
                self._empty(304, [("ETag", etag), ("Accept-Ranges", "bytes")])
                return
            if digest and not self._head_only:
                hasher = hashlib.sha256()
                try:
                    with archive.open("rb") as verify_handle:
                        while True:
                            block = verify_handle.read(1024 * 1024)
                            if not block:
                                break
                            hasher.update(block)
                except OSError as exc:
                    storage_warning(f"artifact read failed during checksum: {archive}")
                    raise StoreError("ARTIFACT_MISSING", "制品文件不可读") from exc
                if hasher.hexdigest() != digest:
                    storage_warning(f"artifact checksum mismatch on stream: {archive}")
                    raise StoreError("ARTIFACT_MISSING", "制品校验和异常，暂不可下载")
            start, end, status = 0, size - 1, 200
            spec = (self.headers.get("Range") or "").strip()
            if spec:
                parsed = _parse_range(spec, size)
                if parsed is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = parsed
                status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("X-Checksum-Sha256", digest)
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self._head_only or length == 0:
                return
            try:
                with archive.open("rb") as handle:
                    handle.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = handle.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # 客户端中途断开：头已经发出去了，不能再补一条 500。
                return

        def _csv(self, text: str, filename: str) -> None:
            payload = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            if not self._head_only:
                self.wfile.write(payload)

        def _file(self, path: Path) -> None:
            if not path.is_file():
                self._send(404, {"rule": "RESOURCE_NOT_FOUND", "message": "没有这个文件"})
                return
            payload = path.read_bytes()
            etag = '"' + hashlib.sha256(payload).hexdigest()[:16] + '"'
            if self.headers.get("If-None-Match") == etag:
                self._empty(304, [("ETag", etag), ("Cache-Control", "no-cache")])
                return
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            if path.suffix == ".html":
                self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if not self._head_only:
                self.wfile.write(payload)

    return Handler


class _Server(ThreadingHTTPServer):
    """每个连接一个线程。额外记录在途请求数，供优雅退出时等待收尾。

    这里不做并发配额：真正的限流在网关与 `_RateLimiter`。计数只用于
    `drain()`——容器收到 SIGTERM 时，等正在处理的请求写完再关库，
    免得把「上传到一半」变成一条 500。
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._active = 0
        self._active_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def process_request_thread(self, request, client_address) -> None:  # noqa: ANN001 - 与标准库签名一致
        with self._active_lock:
            self._active += 1
            self._idle.clear()
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active -= 1
                if self._active == 0:
                    self._idle.set()

    def active_requests(self) -> int:
        with self._active_lock:
            return self._active

    def drain(self, timeout: float = 25.0) -> bool:
        """等在途请求收尾，返回 False 表示超时。"""
        return self._idle.wait(timeout)


def serve(
    store: Store,
    host: str = "127.0.0.1",
    port: int = 0,
    public_base: str = "",
    hub_authorize: str = "",
    trust_proxy: bool = False,
    trusted_proxies: str | list[str] | set[str] | tuple[str, ...] | None = None,
    login_limit: int = 20,
    login_window: float = 300,
    register_limit: int = 10,
    register_window: float = 3600,
) -> ThreadingHTTPServer:
    """启动服务。`public_base` 用于反代部署时固定对外地址。

    默认空串，表示按请求头推断（见 `_base`）。显式给值时以它为准，
    不再看 `Host`，避免客户端伪造 `Host` 影响下发的安装计划。

    `hub_authorize` 是枢纽授权页地址，可含 `{state}`、`{redirect_uri}`、
    `{app_id}` 三个占位。给了它，`/auth/start` 才可用，回调的 `state`
    也能与商店下发的 Cookie 对上。不给则跳转登录仍只由枢纽发起，
    `state` 无从核对——这条差别在日志里以 `STATE_UNBOUND` 记下。
    """
    server = _Server((host, port), make_handler(store))
    server.public_base = public_base.strip()  # type: ignore[attr-defined]
    server.hub_authorize = hub_authorize.strip()  # type: ignore[attr-defined]
    server.trust_proxy = bool(trust_proxy)  # type: ignore[attr-defined]
    raw_proxies = os.environ.get("QILIN_TRUSTED_PROXIES", "") if trusted_proxies is None else trusted_proxies
    server.trusted_proxies = _parse_trusted_proxies(raw_proxies)  # type: ignore[attr-defined]
    server.limiters = {  # type: ignore[attr-defined]
        "login": _RateLimiter(login_limit, login_window),
        "register": _RateLimiter(register_limit, register_window),
    }
    return server


def _format_started_urls(started: dict, base: str) -> None:
    root = base.rstrip("/")
    if started.get("put_path"):
        started["put_url"] = root + started["put_path"]
    elif "put_path" in started:
        started["put_url"] = None
    if started.get("upload_path") and started.get("upload_ticket"):
        started["upload_url"] = root + started["upload_path"]
        started["upload_headers"] = {"X-Upload-Ticket": started["upload_ticket"]}
    started.pop("put_path", None)
    started.pop("upload_path", None)
    started.pop("upload_ticket", None)



def _read_body(handler: BaseHTTPRequestHandler, max_bytes: int = _MAX_BODY) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise StoreError("CHECKSUM", "Content-Length 不合法") from exc
    if length < 0:
        raise StoreError("CHECKSUM", "Content-Length 不合法")
    if length > max_bytes:
        # 413：让客户端知道是"太大"，不是"格式不对"（评审 P2-10）。
        raise StoreError("PAYLOAD_TOO_LARGE", "请求体超过上限")
    data = handler.rfile.read(length) if length else b""
    handler._body_read = True  # noqa: SLF001 - 同模块内的处理器约定
    return data


def _require_json(handler: BaseHTTPRequestHandler) -> None:
    """POST 正文必须是 JSON。

    允许没带 Content-Type（一些极简客户端），但显式声明成表单/多部分时直接
    415：那类请求是跨站简单请求的形状，也不该被当成 JSON 解析。
    """
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return
    raw = (handler.headers.get("Content-Type") or "").strip().lower()
    media = raw.split(";", 1)[0].strip()
    if not media:
        return
    if media == "application/json" or media.endswith("+json"):
        return
    raise StoreError("UNSUPPORTED_MEDIA_TYPE", "请求体必须是 application/json")


def _files(body: dict) -> dict[str, bytes]:
    """把 `files` 字段解成字节。

    两个约定：(1) 只接受字符串键与字符串值，不接受数组或对象；
    (2) 用 `validate=True` 严格解码，非法 base64 直接报 `CHECKSUM`，
    不再像早先那样静默丢弃非法字符——那会让提交方以为文件已完整上传，
    实际拿到的是截断内容。
    """
    raw = body.get("files")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise StoreError("CHECKSUM", "files 必须是对象")
    files: dict[str, bytes] = {}
    for name, content in raw.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise StoreError("CHECKSUM", "files 的键与值都必须是字符串")
        try:
            files[name] = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise StoreError("CHECKSUM", f"文件 {name} 不是合法的 base64") from exc
    return files


def _result(result) -> dict:
    return {"submission_id": result.submission_id, "status": result.status, "rules": result.rules}


def _issued(profile: dict, key: str, base: str) -> dict:
    issued = dict(profile)
    issued["key"] = key
    issued["mcp"] = {
        "mcpServers": {
            "qilin": {
                "type": "http",
                "url": base.rstrip("/") + "/mcp",
                "headers": {"Authorization": "Bearer " + key},
            }
        }
    }
    return issued


def _bearer(header: str) -> str:
    return header.removeprefix("Bearer ").strip()


def _cookie(header: str, name: str) -> str:
    for part in header.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return ""


def _credential(handler: BaseHTTPRequestHandler) -> str:
    bearer = _bearer(handler.headers.get("Authorization", ""))
    if bearer:
        return bearer
    return _cookie(handler.headers.get("Cookie", ""), "qilin_session")


def _session_cookie(token: str, clear: bool = False, secure: bool = False) -> Headers:
    """会话 cookie。HTTPS 下补 `Secure`，避免反代之后仍随明文请求发出。

    `clear` 用于登出：把值清空并把 `Max-Age` 归零。
    """
    flags = "HttpOnly; SameSite=Lax; Path=/" + ("; Secure" if secure else "")
    if clear:
        return [("Set-Cookie", f"qilin_session=; {flags}; Max-Age=0")]
    return [("Set-Cookie", f"qilin_session={token}; {flags}; Max-Age=604800")]


def _state_cookie(value: str, clear: bool = False, secure: bool = False, ttl: int = _STATE_TTL) -> Headers:
    """跳转登录的一次性 state。

    与会话 Cookie 同为 `HttpOnly` 和 `SameSite=Lax`：它只在回调那一次被读，
    不需要脚本访问。`Path` 收窄到 `/auth`，其余请求不会带上它。
    """
    flags = "HttpOnly; SameSite=Lax; Path=/auth" + ("; Secure" if secure else "")
    if clear:
        return [("Set-Cookie", f"{_STATE_COOKIE}=; {flags}; Max-Age=0")]
    return [("Set-Cookie", f"{_STATE_COOKIE}={value}; {flags}; Max-Age={ttl}")]


def _pairs(extra: Headers | dict[str, str] | None) -> Headers:
    if not extra:
        return []
    if isinstance(extra, dict):
        return list(extra.items())
    return list(extra)


def _merge_headers(*groups: Headers) -> Headers:
    merged: Headers = []
    for group in groups:
        merged.extend(group)
    return merged


def _one(query: dict[str, list[str]], key: str) -> str:
    return (query.get(key) or [""])[0]


# 分页 offset 的上限。SQLite 的整数边界远大于此，这里只拦明显异常的输入，
# 免得 int 太大绑不进 SQL（OverflowError → 500）。
_MAX_OFFSET = 10_000_000


def _int(
    query: dict[str, list[str]], key: str, default: int | None, *, lo: int, hi: int,
) -> int | None:
    """读整数查询参数并夹到 [lo, hi]。解析失败按 400 处理。"""
    raw = _one(query, key).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise StoreError("SPEC_VERSION", f"{key} 必须是整数") from exc
    return max(lo, min(hi, value))


def _bad_request(exc: BaseException) -> str:
    """把外部输入引发的异常翻译成一句可操作的话，不回显内部细节。"""
    if isinstance(exc, json.JSONDecodeError):
        return "请求体不是合法 JSON。"
    if isinstance(exc, KeyError) and exc.args:
        return f"缺少字段：{exc.args[0]}"
    return "请求体或参数不符合规格。"


def _artifact_key(path: str) -> tuple[str, str]:
    parts = [part for part in path.split("/") if part]
    if len(parts) != 4:
        raise StoreError("RESOURCE_NOT_FOUND", "没有这个制品")
    return parts[2], parts[3]


def _base(handler: BaseHTTPRequestHandler) -> str:
    """对外地址。部署在反代之后时以显式配置与转发头为准。

    优先级：`--public-base` > `X-Forwarded-Proto` + `Host` > 本地监听地址。

    早先这里恒返回 `http://<Host>`，于是 HTTPS 部署下发给客户端的 MCP 配置
    与安装计划仍是明文地址，制品下载也走明文。`Host` 头来自外部，拼进 URL
    前先做形状检查，形状不对就退回监听地址，不让它以任意内容进入安装计划。
    反向代理部署请配置 `--public-base`，那是唯一不依赖请求头的来源。
    """
    configured = getattr(handler.server, "public_base", "")
    if configured:
        return configured.rstrip("/")
    scheme = _scheme(handler)
    host = _host(handler)
    if host:
        return f"{scheme}://{host}"
    server = handler.server
    return f"{scheme}://{server.server_address[0]}:{server.server_address[1]}"


def _scheme(handler: BaseHTTPRequestHandler) -> str:
    """只认 `http` / `https` 两个值，其余一律按 http 处理。"""
    forwarded = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return "https" if forwarded == "https" else "http"


def _host(handler: BaseHTTPRequestHandler) -> str:
    return _host_header(handler.headers.get("Host", ""))


def _host_header(raw: str | None) -> str:
    """校验主机名的形状，形状不合就返回空串。

    只允许字母、数字与 `.-_:[]`（覆盖主机名、IPv4、方括号 IPv6、端口），
    不允许空白、斜杠、`@` 与控制字符，避免把 URL 结构拆坏。
    """
    host = (raw or "").split(",")[0].strip()
    if not host or len(host) > 255:
        return ""
    for char in host:
        if not (char.isalnum() or char in ".-_:[]"):
            return ""
    return host


def _secure(handler: BaseHTTPRequestHandler) -> bool:
    return _base(handler).startswith("https://")


def _authorize_url(template: str, state: str, base: str, app_id: str = "") -> str:
    """把授权页模板补上 `state`、`redirect_uri`、`app_id` 三个占位。

    只做字符串替换，不解析模板里的查询串：授权页可能不是标准的 OAuth，
    参数名由枢纽决定，商店不替它假设。模板里没出现的占位就跳过。
    回调地址由商店自己的对外地址推出，不接受从请求里读的 `redirect_uri`，
    否则就成了让外部指定令牌落到哪。
    """
    redirect_uri = base.rstrip("/") + "/auth/callback"
    url = template
    for name, value in (("state", state), ("redirect_uri", redirect_uri), ("app_id", app_id)):
        url = url.replace("{" + name + "}", quote(value, safe=""))
    return url

