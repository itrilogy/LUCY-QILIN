"""跨层共享的小数据结构。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubmitResult:
    submission_id: str
    status: str
    rules: list[str]
