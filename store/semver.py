"""semver 的唯一实现。

审计发现仓库里有两套：`store.validator.validate._cmp` 会剥掉预发布标记，
`store.plan._rank` 保留它。两者对 `1.0.0-rc1` 与 `1.0.0` 给出相反的结论，
于是「校验时算通过、选版本时算更低」。这里按 semver 2.0.0 收成一份，
两边都从这里取。

本模块不依赖 store 下的任何模块，避免循环导入。
"""

from __future__ import annotations

import re

SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$"
)


def parse(version: str) -> tuple[int, int, int, tuple | None] | None:
    """把版本号拆成可比较的键；格式不合法返回 None。

    预发布段按 semver 2.0.0 第 11 条拆成若干标识符：纯数字的当整数比，
    其余按 ASCII 字符串比，数字标识符始终低于非数字标识符。
    这里用元组的第一位区分：0 表示数字、1 表示字符串。
    """
    matched = SEMVER.match(version.strip()) if isinstance(version, str) else None
    if not matched:
        return None
    major, minor, patch = (int(matched.group(index)) for index in (1, 2, 3))
    pre = matched.group(4)
    if pre is None:
        return major, minor, patch, None
    identifiers: list[tuple[int, object]] = []
    for part in pre.split("."):
        if not part:
            return None
        if part.isdigit():
            identifiers.append((0, int(part)))
        else:
            identifiers.append((1, part))
    return major, minor, patch, tuple(identifiers)


def is_valid(version: object) -> bool:
    return parse(version) is not None if isinstance(version, str) else False


def is_prerelease(version: str) -> bool:
    parsed = parse(version)
    return parsed is not None and parsed[3] is not None


def key(version: str) -> tuple:
    """排序键。合法版本按其语义排序，非法版本排在最后（不抛异常）。"""
    parsed = parse(version)
    if parsed is None:
        return (1, 0, 0, 0, ())
    major, minor, patch, pre = parsed
    # 正式版高于同号的预发布版：用第 0 位编码这一层。
    if pre is None:
        return (0, major, minor, patch, 1, ())
    return (0, major, minor, patch, 0, pre)


def cmp(left: str, right: str) -> int:
    """比较两个版本。任一不可解析时按字符串兜底比较，保证不抛异常。"""
    a, b = key(left), key(right)
    if a == b:
        return 0
    return 1 if a > b else -1


def satisfies(version: str, expr: str) -> bool:
    """判断版本是否满足表达式。表达式由空格分隔的若干条件组成，全部成立才算满足。

    支持的运算符：`>=`、`<=`、`>`、`<`、`=`，省略时按 `=`。
    空串与 `*` 视为不限。
    """
    text = (expr or "").strip()
    if text in {"", "*"}:
        return True
    if parse(version) is None:
        return False
    for part in text.split():
        matched = re.match(r"^(>=|<=|>|<|=)?\s*(\S+)$", part)
        if not matched:
            return False
        operator, wanted = matched.group(1) or "=", matched.group(2)
        if parse(wanted) is None:
            return False
        order = cmp(version, wanted)
        if operator == "=" and order != 0:
            return False
        if operator == ">" and order <= 0:
            return False
        if operator == ">=" and order < 0:
            return False
        if operator == "<" and order >= 0:
            return False
        if operator == "<=" and order > 0:
            return False
    return True

