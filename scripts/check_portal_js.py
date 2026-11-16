#!/usr/bin/env python3
"""检查门户内联 JS 的语法：抽出 <script> 内容，交给 node --check。

CI 里有 Node；本地没有 Node 时打印跳过并返回 0，避免验收脚本在纯 Python
环境里失败。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    portal = Path(__file__).resolve().parents[1] / "store" / "portal" / "index.html"
    html = portal.read_text(encoding="utf-8")
    start = html.index("<script>") + len("<script>")
    end = html.index("</script>", start)
    node = shutil.which("node")
    if not node:
        print("node 不在 PATH，跳过门户 JS 语法检查")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(html[start:end])
        path = handle.name
    try:
        return subprocess.run([node, "--check", path], check=False).returncode
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
