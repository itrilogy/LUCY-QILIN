"""外部依赖地址检查。校验器默认不访问网络，调用方显式打开。"""

from __future__ import annotations

import urllib.error
import urllib.request


def probe_url(url: str, timeout: float = 10) -> int | None:
    """HEAD，失败再 GET。返回状态码；连不上则返回 None。"""
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in {405, 501}:
                continue
            return int(exc.code)
        except Exception:
            if method == "HEAD":
                continue
            return None
    return None


def url_ok(status: int | None) -> bool:
    return status is not None and 200 <= status < 400
