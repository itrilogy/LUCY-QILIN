#!/usr/bin/env python3
"""容器健康探针。

`/api/health` 在数据库不可读或制品目录不可写时回 503，urllib 会抛 HTTPError，
这里统一转成退出码 1。不引入 curl：slim 镜像里没有它，装一个只为探针不划算。

端口从 QILIN_PORT 读，与入口脚本用同一个约定。
"""

from __future__ import annotations

import os
import sys
import urllib.request

URL = "http://127.0.0.1:{port}/api/health"


def main() -> int:
    port = os.environ.get("QILIN_PORT", "8787")
    url = URL.format(port=port)
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            if response.status == 200:
                return 0
            print(f"healthcheck: {url} -> HTTP {response.status}", file=sys.stderr)
            return 1
    except Exception as exc:  # noqa: BLE001 - 探针只关心成功与否
        print(f"healthcheck: {url} -> {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
