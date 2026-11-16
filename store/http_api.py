"""上架 HTTP、商店 MCP 和栖林门户。只转发到 Store，不另写校验。"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from store.diagnostics import auth_failure, internal as log_internal
from store.mcp_api import handle
from store.errors import StoreError, http_status, is_auth_rule
from store.service import Store, _MAX_BYTES

# 请求体上限。`files` 走 base64，膨胀约 1.34 倍；若与制品上限共用同一个
# 数值，门户路径上 32MiB 的制品会必然超限。这里给正文留出 base64 的余量，
# 制品本身仍由 `_MAX_BYTES` 约束，两者不再互相卡住。
_MAX_BODY = (_MAX_BYTES // 3 + 1) * 4 + 1024 * 1024

# 门户与静态资源随包分发，`pip install` 之后也能打开首页。
# 不再引用仓库外的 `LUCY-DESIGN`：换台机器克隆就掉样式的问题由此消除。
PORTAL_DIR = Path(__file__).resolve().parent / "portal"
PORTAL = PORTAL_DIR / "index.html"
DESIGN_FILES = {
    "/design/tokens.css": PORTAL_DIR / "assets" / "tokens.css",
    "/design/qilin.svg": PORTAL_DIR / "assets" / "qilin.svg",
    "/design/luxi-lab-gold.svg": PORTAL_DIR / "assets" / "luxi-lab-gold.svg",
}
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
# 跳转登录的一次性 state。存 Cookie，不进地址栏以外的任何响应体。
# 名称带 hub 前缀，与会话 Cookie 分开，免得登出时一起被清掉。
_STATE_COOKIE = "qilin_hub_state"
_STATE_TTL = 600

# 响应附加头。用列表而不是字典，因为 `Set-Cookie` 允许同名头重复出现，
# 一次响应可能同时要下发会话 Cookie 和清除 state 的 Cookie。
Headers = list[tuple[str, str]]


def make_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
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
                if path == "/api/health":
                    status = store.identity_status()
                    self._send(200, {"status": "ok", "app_id": status["app_id"], "identity": status["mode"]})
                    return
                if path == "/v1/identity":
                    info = store.identity_status()
                    # 能否由商店发起跳转，取决于有没有配授权页。门户据此决定
                    # 是否显示「用实验室账号登录」的入口，不去猜。
                    info["hub_start"] = bool(getattr(self.server, "hub_authorize", ""))
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
                        limit=int(_one(query, "limit")) if _one(query, "limit") else None,
                        offset=int(_one(query, "offset") or 0),
                    ))
                    return
                if path == "/v1/mine":
                    self._send(200, {"items": store.list_my_submissions(token)})
                    return
                if path == "/v1/audit":
                    self._send(200, store.list_audit(
                        token,
                        limit=int(_one(query, "limit") or 100),
                        offset=int(_one(query, "offset") or 0),
                        package_id=_one(query, "package_id") or None,
                        action=_one(query, "action") or None,
                    ))
                    return
                if path == "/v1/reviews":
                    self._send(200, {"items": store.list_reviews(token)})
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
                        raise StoreError("VERSION_UNAVAILABLE", "没有这个地址")
                    self._send(200, store.describe(parts[2], parts[3]))
                    return
                if path.startswith("/v1/artifacts/"):
                    package_id, version = _artifact_key(path)
                    body, digest = store.artifact(package_id, version, _one(query, "platform") or None)
                    self._bytes(200, body, digest)
                    return
                self._send(404, {"rule": "VERSION_UNAVAILABLE", "message": "没有这个地址"})
            except StoreError as exc:
                self._fail(exc)
            except (KeyError, json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
                # 清单与请求体来自外部：字段缺失、类型不对都在这里收口，
                # 不能让异常冒到 BaseHTTPRequestHandler，那会直接断开连接。
                self._send(400, {"rule": "SPEC_VERSION", "message": f"{type(exc).__name__}: {exc}"})
            except Exception as exc:  # noqa: BLE001 - 兜底，避免单个请求打断连接
                trace = log_internal(self, exc)
                # 异常原文只进日志。对外给一个可检索的短标识，不回显路径与 SQL。
                self._send(500, {"rule": "INTERNAL", "message": "服务器内部错误", "trace_id": trace})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            token = _credential(self)
            try:
                store.prepare_token(token)
                store.refresh_hub_if_due(token)
                raw = _read_body(self)
                body = json.loads(raw or b"{}")
                # 顶层必须是对象，否则后面的 body.get 全都不成立。
                if not isinstance(body, dict):
                    raise StoreError("SPEC_VERSION", "请求体必须是 JSON 对象")
                if path == "/v1/account":
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
                    profile, session = store.login_account(
                        str(body.get("username") or ""),
                        str(body.get("password") or ""),
                    )
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
                if path.startswith("/v1/namespaces/") and path.endswith("/transfer"):
                    parts = [part for part in path.split("/") if part]
                    if len(parts) != 4:
                        raise StoreError("VERSION_UNAVAILABLE", "没有这个地址")
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
                    result = handle(store, token, body, base_url=_base(self))
                    if result is None:
                        self._empty(202)
                        return
                    extra: Headers = []
                    if body.get("method") == "initialize":
                        extra.append(("Mcp-Session-Id", secrets.token_hex(16)))
                    self._mcp(200, result, extra)
                    return
                if path == "/auth/logout/backchannel":
                    store.accept_logout(str(body.get("logout_token") or ""))
                    self._empty(204)
                    return
                if path == "/v1/validate":
                    manifest = body["manifest"]
                    findings = store.explain_manifest(token, manifest)
                    self._send(200, {
                        "rules": [item["rule"] for item in findings if item["severity"] == "error"],
                        "findings": findings,
                    })
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
                    if started.get("put_path"):
                        started["put_url"] = _base(self).rstrip("/") + started["put_path"]
                    started.pop("put_path", None)
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
                if path.startswith("/v1/submissions/") and path.endswith("/approve"):
                    store.approve(token, path.split("/")[3])
                    self._send(200, {"status": "published"})
                    return
                if path.startswith("/v1/submissions/") and path.endswith("/reject"):
                    store.reject(token, path.split("/")[3], str(body.get("reason") or ""))
                    self._send(200, {"status": "rejected"})
                    return
                self._send(404, {"rule": "VERSION_UNAVAILABLE", "message": "没有这个地址"})
            except StoreError as exc:
                self._fail(exc)
            except (KeyError, json.JSONDecodeError, ValueError, TypeError, AttributeError) as exc:
                # 清单与请求体来自外部：字段缺失、类型不对都在这里收口，
                # 不能让异常冒到 BaseHTTPRequestHandler，那会直接断开连接。
                self._send(400, {"rule": "SPEC_VERSION", "message": f"{type(exc).__name__}: {exc}"})
            except Exception as exc:  # noqa: BLE001 - 兜底，避免单个请求打断连接
                trace = log_internal(self, exc)
                # 异常原文只进日志。对外给一个可检索的短标识，不回显路径与 SQL。
                self._send(500, {"rule": "INTERNAL", "message": "服务器内部错误", "trace_id": trace})

        def do_PUT(self) -> None:
            path = urlparse(self.path).path
            token = _credential(self)
            if not path.startswith("/v1/uploads/"):
                self._send(404, {"rule": "VERSION_UNAVAILABLE", "message": "没有这个地址"})
                return
            try:
                store.prepare_token(token)
                store.refresh_hub_if_due(token)
                data = _read_body(self)
                parts = [part for part in path.split("/") if part]
                if len(parts) == 4 and parts[2] == "revise":
                    store.save_revise_upload(token, parts[3], data)
                else:
                    store.save_upload(token, parts[2], data)
                self._send(200, {"status": "uploaded"})
            except StoreError as exc:
                self._fail(exc)
            except Exception as exc:  # noqa: BLE001 - 兜底，避免单个请求打断连接
                trace = log_internal(self, exc)
                # 异常原文只进日志。对外给一个可检索的短标识，不回显路径与 SQL。
                self._send(500, {"rule": "INTERNAL", "message": "服务器内部错误", "trace_id": trace})

        def _fail(self, exc: StoreError) -> None:
            """把 `StoreError` 映射成响应，并留下一条记录。

            状态码规则与早先一致：只有 `ROLE_FORBIDDEN` 回 403，其余回 400。
            日志方面，鉴权与登录相关的规则记 warning，业务拒绝记 info——
            业务拒绝量大，不必都当告警。这是本轮补的「事后可查」的最小集合。
            """
            if is_auth_rule(exc.rule):
                auth_failure(self, exc.rule, exc.message)
            self._send(http_status(exc.rule), {"rule": exc.rule, "message": exc.message})

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(self, status: int, payload: dict, extra: Headers | None = None) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in _pairs(extra):
                self.send_header(name, value)
            self.end_headers()
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

        def _bytes(self, status: int, payload: bytes, digest: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Checksum-Sha256", digest)
            self.end_headers()
            self.wfile.write(payload)

        def _file(self, path: Path) -> None:
            if not path.is_file():
                self._send(404, {"rule": "VERSION_UNAVAILABLE", "message": "没有这个文件"})
                return
            payload = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def serve(
    store: Store,
    host: str = "127.0.0.1",
    port: int = 0,
    public_base: str = "",
    hub_authorize: str = "",
) -> ThreadingHTTPServer:
    """启动服务。`public_base` 用于反代部署时固定对外地址。

    默认空串，表示按请求头推断（见 `_base`）。显式给值时以它为准，
    不再看 `Host`，避免客户端伪造 `Host` 影响下发的安装计划。

    `hub_authorize` 是枢纽授权页地址，可含 `{state}`、`{redirect_uri}`、
    `{app_id}` 三个占位。给了它，`/auth/start` 才可用，回调的 `state`
    也能与商店下发的 Cookie 对上。不给则跳转登录仍只由枢纽发起，
    `state` 无从核对——这条差别在日志里以 `STATE_UNBOUND` 记下。
    """
    server = ThreadingHTTPServer((host, port), make_handler(store))
    server.public_base = public_base.strip()  # type: ignore[attr-defined]
    server.hub_authorize = hub_authorize.strip()  # type: ignore[attr-defined]
    return server



def _read_body(handler: BaseHTTPRequestHandler, max_bytes: int = _MAX_BODY) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise StoreError("CHECKSUM", "Content-Length 不合法") from exc
    if length < 0 or length > max_bytes:
        raise StoreError("CHECKSUM", "请求体过大或大小不在允许范围内")
    return handler.rfile.read(length) if length else b""


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


def _artifact_key(path: str) -> tuple[str, str]:
    parts = [part for part in path.split("/") if part]
    if len(parts) != 4:
        raise StoreError("VERSION_UNAVAILABLE", "没有这个制品")
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

