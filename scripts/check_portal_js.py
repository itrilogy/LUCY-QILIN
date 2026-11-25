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
    portal_dir = Path(__file__).resolve().parents[1] / "store" / "portal"
    scripts = sorted(portal_dir.glob("*.js"))
    node = shutil.which("node")
    if not node:
        print("node 不在 PATH，跳过门户 JS 语法检查")
        return 0
    if not scripts:
        html = (portal_dir / "index.html").read_text(encoding="utf-8")
        start = html.index("<script>") + len("<script>")
        end = html.index("</script>", start)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(html[start:end])
            scripts = [Path(handle.name)]
    status = 0
    for script in scripts:
        result = subprocess.run([node, "--check", str(script)], check=False)
        if result.returncode != 0:
            print(f"语法检查失败：{script}")
            status = result.returncode
    return status


if __name__ == "__main__":
    sys.exit(main())
