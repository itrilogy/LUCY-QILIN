"""路径守卫：清单与压缩包里出现的外部路径统一从这里过一道。

背景：`pathlib` 的 `/` 运算符在右侧是绝对路径时会丢掉左侧的基准目录，
`Path("/srv/blobs") / "/tmp/x"` 得到 `/tmp/x`。只要有一处拼接前没做形状
检查，外部输入就能把落点写到目录之外。本项目原先在四个地方各写了一套
检查，标准不一，其中提交侧的资产落盘（`Store._keep_assets`）完全没有检查。
本模块把检查收成一份，四处共用。

约定：
- 只接受「相对路径」。绝对路径、盘符路径、含 `..` 的路径一律拒绝。
- 形状检查通过后再比一次 `resolve()` 结果，防止符号链接把落点带出根目录。
"""

from __future__ import annotations

from pathlib import Path


class PathEscape(ValueError):
    """外部提供的路径越过了允许的根目录。继承 ValueError，便于调用方统一处理。"""


def check_relative(value: object) -> str:
    """确认 value 是能安全拼接的相对路径，返回规整后的 POSIX 形式。

    只做形状检查，不碰文件系统。合法输入会被归一：把 `\\` 统一成 `/`、
    丢弃空的与 `.` 片段，其余字符原样保留（不改写首尾空白，避免写出一个
    落盘后名字对不上的文件）。
    """
    if not isinstance(value, str):
        raise PathEscape(f"路径必须是字符串：{value!r}")
    if value != value.strip():
        raise PathEscape(f"路径首尾不能有空白：{value!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise PathEscape(f"路径不能含控制字符：{value!r}")
    text = value.replace("\\", "/")
    if not text or text == ".":
        raise PathEscape(f"路径不能为空：{value!r}")
    if text.startswith("/"):
        raise PathEscape(f"路径必须是相对路径：{value!r}")
    # Windows 盘符（C:/）在 POSIX 上不算绝对路径，需要单独挡。
    if len(text) > 1 and text[1] == ":" and text[0].isalpha():
        raise PathEscape(f"路径必须是相对路径：{value!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise PathEscape(f"路径不能为空：{value!r}")
    if ".." in parts:
        raise PathEscape(f"路径不能包含 ..：{value!r}")
    return "/".join(parts)


def safe_join(base: Path, *parts: str) -> Path:
    """把若干相对路径片段接到 base 下，返回 resolve 之后的绝对路径。

    任一片段不合法、或拼接结果落在 base 之外（含符号链接绕路），都抛
    `PathEscape`。调用方负责把它翻译成自己的错误码或提示。
    """
    checked = [check_relative(part) for part in parts]
    base_resolved = base.resolve()
    target = base_resolved.joinpath(*checked).resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError as exc:
        raise PathEscape(f"路径越界：{'/'.join(checked)}") from exc
    return target


def is_safe_relative(value: object) -> bool:
    """`check_relative` 的布尔版本，用于校验器里「收集 finding」的场景。"""
    try:
        check_relative(value)
    except PathEscape:
        return False
    return True
