"""每人一把由商店生成的 KEY。只存盐和校验值，明文不落库。"""

from __future__ import annotations

import hashlib
import re
import secrets

from store.errors import StoreError

_USERNAME = re.compile(r"^[a-z][a-z0-9._-]{2,31}$")
_SCRYPT_N = 2**14


def username_of(value: str) -> str:
    text = value.strip()
    if not _USERNAME.fullmatch(text):
        raise StoreError("USERNAME_INVALID", "用户名须以小写字母开头，3 到 32 位，只能含数字、点、下划线和连字符。")
    return text


def display_name_of(value: str) -> str:
    text = value.strip()
    if not text or len(text) > 64:
        raise StoreError("NAME_INVALID", "显示名不能为空，且不能超过 64 个字。")
    return text


def person_name(value: str, label: str) -> str:
    text = value.strip()
    if not text or len(text) > 64:
        raise StoreError("NAME_INVALID", f"{label}不能为空，且不能超过 64 个字。")
    return text


def optional_text(value: str, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > 64:
        raise StoreError("NAME_INVALID", f"{label}不能超过 64 个字。")
    return text


def issue_key() -> str:
    return secrets.token_urlsafe(32)


def issue_password(username: str = "") -> str:
    lowers = "abcdefghijkmnopqrstuvwxyz"
    uppers = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "23456789"
    pool = lowers + uppers + digits
    for _ in range(8):
        chars = [
            secrets.choice(lowers),
            secrets.choice(lowers),
            secrets.choice(uppers),
            secrets.choice(uppers),
            secrets.choice(digits),
            secrets.choice(digits),
        ]
        chars.extend(secrets.choice(pool) for _ in range(10))
        for index in range(len(chars) - 1, 0, -1):
            swap = secrets.randbelow(index + 1)
            chars[index], chars[swap] = chars[swap], chars[index]
        password = "".join(chars)
        if not password_problems(password, username):
            return password
    raise StoreError("PASSWORD_INVALID", "没能生成一把符合规则的口令。")


def password_problems(value: str, username: str = "") -> list[str]:
    text = value or ""
    problems: list[str] = []
    if text != text.strip():
        problems.append("首尾不能是空格")
    if not 10 <= len(text) <= 64:
        problems.append("长度须为 10 到 64 位")
    if not re.search(r"[a-z]", text):
        problems.append("须含小写字母")
    if not re.search(r"[A-Z]", text):
        problems.append("须含大写字母")
    if not re.search(r"[0-9]", text):
        problems.append("须含数字")
    if username and username.lower() in text.lower():
        problems.append("不能包含用户名")
    return problems


def password_of(value: str, username: str = "") -> str:
    problems = password_problems(value, username)
    if problems:
        raise StoreError("PASSWORD_INVALID", "口令不符合要求：" + "、".join(problems) + "。")
    return value


def key_of(value: str) -> str:
    text = value.strip()
    if not 4 <= len(text) <= 256:
        raise StoreError("KEY_INVALID", "KEY 长度须在 4 到 256 位之间。")
    return text


def make_key(key: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    hashed = hashlib.scrypt(key.encode(), salt=salt, n=_SCRYPT_N, r=8, p=1, dklen=32)
    return salt, hashed


def key_prefix(key: str) -> str:
    """KEY 的快速指纹，用于把鉴权从「全表 scrypt」缩到「单行 scrypt」。

    取 `sha256(key)` 的前 16 位十六进制。它不参与口令强度判断，只用来
    定位候选行；真正的比对仍由 `key_matches` 做一次 scrypt。
    以「前缀定位 + 慢哈希验证」的组合，安全性与原先一致，复杂度从
    O(账号数) 降到 O(1)。
    """
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def key_matches(key: str, salt: bytes, hashed: bytes) -> bool:
    _, candidate = make_key(key, salt)
    return secrets.compare_digest(candidate, hashed)
