"""全局路径约定。

知识包是用户级资产，不跟随某个仓库或会话的工作目录。商店安装器与
DSH 的知林插件必须用同一个默认根，否则「装好的包」和「读得到的包」会分成两处。

优先级：
1. 调用方显式传入的路径；
2. QILIN_KNOWLEDGE_ROOT；
3. ~/.qilin/knowledge。
"""

from __future__ import annotations

import os
from pathlib import Path


def default_knowledge_root() -> Path:
    """返回本机全局知识根；不创建目录，只解析路径。"""
    value = os.environ.get("QILIN_KNOWLEDGE_ROOT")
    if value and value.strip():
        return Path(value.strip()).expanduser()
    return Path.home() / ".qilin" / "knowledge"
