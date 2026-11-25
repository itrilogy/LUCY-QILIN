"""外部依赖地址检查。校验器默认不访问网络，调用方显式打开。

商店会在上传完成时替提交者抓一次清单里的地址（`check_urls`）。这等于让外部输入
决定服务端去访问谁——评审 P1-3 的盲 SSRF。这里按"默认只允许公网"收口：

- `QILIN_FETCH_POLICY`：
  - `public`（默认）：只访问解析结果全部是公网地址的目标。回环、私网、链路本地
    （169.254.169.254 这类元数据地址）、组播、保留地址一律不碰；
  - `allowlist`：只访问 `QILIN_FETCH_ALLOW_HOSTS` 里的主机；
  - `any`：旧行为，全放开（仅限完全可信的内网，显式开启）。
- `QILIN_FETCH_ALLOW_HOSTS`：逗号分隔的主机名或域名后缀，命中即放行（内网资源
  用这个开口子，而不是把策略改成 any）。
- 重定向每一跳都重新过策略，且不允许 https 降级到 http。

被策略拦下的目标返回 None，与"打不开"不可区分——不给提交者当端口探针用。
DNS 在检查时解析一次、连接时再解析一次，理论上存在重绑定窗口；这是已知残余风险，
要彻底消除得把连接钉在已校验的 IP 上（会牺牲 TLS 校验），不在本轮范围。
"""

from __future__ import annotations

import ipaddress
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_POLICIES = {"public", "allowlist", "any"}
_URL_CHECK_MODES = {"strict", "warn", "off"}


def _policy() -> str:
    value = (os.environ.get("QILIN_FETCH_POLICY") or "public").strip().lower()
    return value if value in _POLICIES else "public"


def url_check_mode() -> str:
    """外部依赖说明链接的探测模式：strict（默认，阻断）| warn（记 warning 不阻断）| off（不探测）。"""
    value = (os.environ.get("QILIN_URL_CHECK") or "strict").strip().lower()
    return value if value in _URL_CHECK_MODES else "strict"


def is_allowlisted_url(url: str) -> bool:
    """判断 URL 主机是否显式命中 QILIN_FETCH_ALLOW_HOSTS 白名单（供内网 http/https 服务放行）。"""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    host = parts.hostname or ""
    return bool(host) and _host_allowlisted(host)


def _allow_hosts() -> tuple[str, ...]:
    raw = os.environ.get("QILIN_FETCH_ALLOW_HOSTS") or ""
    return tuple(item.strip().lower().rstrip(".") for item in raw.split(",") if item.strip())


def _host_allowlisted(host: str) -> bool:
    host = host.strip().lower().rstrip(".")
    for item in _allow_hosts():
        if host == item or host.endswith("." + item):
            return True
    return False


def _is_public_ip(text: str) -> bool:
    """地址是否属于"公网可路由"。带 zone id 的 IPv6 先去掉 %en0 这类后缀。"""
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_global
    except ValueError:
        return False


def _resolves_public(hostname: str) -> bool:
    """解析出来的每个地址都必须是公网地址；解析失败按不可达处理（fail closed）。"""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos if info[4]}
    return bool(addresses) and all(_is_public_ip(item) for item in addresses)


def url_allowed(url: str) -> bool:
    """这个地址是否允许服务端去访问。"""
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    host = parts.hostname or ""
    if not host:
        return False
    if _host_allowlisted(host):
        return True
    policy = _policy()
    if policy == "any":
        return True
    if policy == "allowlist":
        return False
    return _resolves_public(host)


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """每一跳重定向都重新过策略，且禁止 https → http。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - 与标准库签名一致
        if urlsplit(req.full_url).scheme == "https" and urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(newurl, code, "不允许降级重定向", headers, fp)
        if not url_allowed(newurl):
            raise urllib.error.HTTPError(newurl, code, "重定向目标不被允许", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def probe_url(url: str, timeout: float = 5.0) -> int | None:
    """HEAD，失败再 GET。返回状态码；被策略拦下或连不上返回 None。"""
    if not url_allowed(url):
        return None
    opener = urllib.request.build_opener(_GuardedRedirect())
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code or 0)
            if 300 <= status < 400:
                # 被重定向守卫拒绝，或重定向次数用尽；不把 3xx 当成"可访问"。
                return None
            if method == "HEAD" and status in {405, 501}:
                continue
            return status or None
        except Exception:
            if method == "HEAD":
                continue
            return None
    return None


def url_ok(status: int | None) -> bool:
    return status is not None and 200 <= status < 400
