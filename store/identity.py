"""核枢纽用户管理签发的访问令牌。栖林不签发、不分配密钥。"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ImportError:  # pragma: no cover - 只有未安装 hub extra 时走到
    # 基础安装不依赖第三方包。没有 cryptography 时，本地账号与 KEY 照常，
    # 只有枢纽令牌验签不可用，按 IDENTITY_UNCONFIGURED 明确拒绝。
    _CRYPTO_READY = False
else:
    _CRYPTO_READY = True

APP_ID = "lucy-qilin"
_SKEW = 60
_TTL = 3600
# 取公钥失败后的冷却时间。枢纽打不开时，不必让每个请求都去等一次超时。
_FAIL_TTL = 30


class IdentityError(Exception):
    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message


@dataclass(frozen=True)
class Identity:
    id: str
    name: str
    role: str


class HubIdentity:
    """离线验签。公钥来自枢纽 JWKS，失败时用落盘缓存。"""

    def __init__(
        self,
        origin: str,
        *,
        app_id: str = APP_ID,
        cache_path: Path | None = None,
        opener: Callable[[str], bytes] | None = None,
        now: Callable[[], int] | None = None,
        ttl: int = _TTL,
        skew: int = _SKEW,
    ) -> None:
        self.origin = origin.rstrip("/")
        self.app_id = app_id
        self.cache_path = cache_path
        self._opener = opener or _fetch
        self._now = now or (lambda: int(time.time()))
        self._ttl = ttl
        self._skew = skew
        self._cached: list[dict] | None = None
        self._cached_at = 0
        self._refreshed_for_kid = False
        # 取公钥要联网。多个线程同时验签时只允许一个去取，其余等结果，
        # 避免每个请求各开一次连接。失败后冷却 `_FAIL_TTL` 秒。
        self._fetch_lock = threading.Lock()
        self._fail_until = 0

    def warm(self) -> bool:
        try:
            self._keys(force=True)
            return True
        except IdentityError:
            return self._cached is not None

    def principal(self, token: str) -> Identity:
        payload = self.access_claims(token)
        return Identity(str(payload["sub"]), _name(payload), _role(payload.get("app_roles")))

    def access_claims(self, token: str) -> dict:
        return self._payload(token, expected_type="access")

    def assert_logout(self, token: str) -> dict:
        return self._payload(token, expected_type="logout")

    def _payload(self, token: str, *, expected_type: str) -> dict:
        header_b64, payload_b64, signature = _segments(token)
        try:
            header = json.loads(_b64url_decode(header_b64))
        except (json.JSONDecodeError, ValueError) as exc:
            raise IdentityError("IDENTITY_INVALID", "令牌无效，请从用户管理重新领取。") from exc
        if header.get("alg") != "RS256":
            raise IdentityError("IDENTITY_INVALID", "令牌算法不被接受。")
        key = self._key_for(header.get("kid"))
        signing_input = f"{header_b64}.{payload_b64}".encode()
        if not _verify_rs256(key, signing_input, signature):
            raise IdentityError("IDENTITY_INVALID", "令牌无效，请从用户管理重新领取。")
        try:
            payload = json.loads(_b64url_decode(payload_b64))
        except (json.JSONDecodeError, ValueError) as exc:
            raise IdentityError("IDENTITY_INVALID", "令牌无效，请从用户管理重新领取。") from exc
        if not isinstance(payload, dict):
            raise IdentityError("IDENTITY_INVALID", "令牌无效，请从用户管理重新领取。")
        self._claims(payload, expected_type)
        return payload

    def _claims(self, payload: dict, expected_type: str) -> None:
        if str(payload.get("iss") or "").rstrip("/") != self.origin:
            raise IdentityError("IDENTITY_INVALID", "令牌不是枢纽签发的。")
        if payload.get("aud") != self.app_id:
            raise IdentityError("IDENTITY_INVALID", "这个令牌不是发给栖林的。")
        if payload.get("typ") != expected_type:
            raise IdentityError("IDENTITY_INVALID", "令牌类型不对。")
        if not str(payload.get("sub") or "").strip():
            raise IdentityError("IDENTITY_INVALID", "令牌里没有用户。")
        now = self._now()
        try:
            exp = int(payload["exp"])
            nbf = int(payload.get("nbf") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise IdentityError("IDENTITY_INVALID", "令牌无效，请从用户管理重新领取。") from exc
        if exp < now - self._skew:
            raise IdentityError("IDENTITY_INVALID", "令牌已过期，请从用户管理重新领取。")
        if nbf > now + self._skew:
            raise IdentityError("IDENTITY_INVALID", "令牌尚未生效。")

    def _key_for(self, kid: Any) -> rsa.RSAPublicKey:
        keys = self._usable(self._keys(force=False), kid)
        if keys is None and not self._refreshed_for_kid:
            self._refreshed_for_kid = True
            keys = self._usable(self._keys(force=True), kid)
        if keys is None:
            raise IdentityError("IDENTITY_INVALID", "枢纽公钥对不上这个令牌。")
        return keys

    def _usable(self, keys: list[dict], kid: Any) -> rsa.RSAPublicKey | None:
        chosen = None
        for item in keys:
            if item.get("alg") not in (None, "RS256"):
                continue
            if kid and item.get("kid") != kid:
                continue
            if not kid and chosen is not None:
                return None
            chosen = item
            if kid:
                break
        if chosen is None:
            return None
        return _rsa_key(chosen)

    def _keys(self, *, force: bool) -> list[dict]:
        now = self._now()
        if not force and self._cached is not None and now - self._cached_at < self._ttl:
            return self._cached
        with self._fetch_lock:
            # 等锁期间可能已有别的线程取回来了，先复查一次。
            now = self._now()
            if not force and self._cached is not None and now - self._cached_at < self._ttl:
                return self._cached
            if now < self._fail_until:
                keys = self._cached or self._read_cache()
                if not keys:
                    raise IdentityError("IDENTITY_INVALID", "枢纽公钥还没取到，暂时不能核对此令牌。")
                return keys
            try:
                body = self._opener(f"{self.origin}/.well-known/jwks.json")
                payload = json.loads(body)
                keys = [item for item in payload.get("keys", []) if isinstance(item, dict) and item.get("kty") == "RSA"]
                if not keys:
                    raise IdentityError("IDENTITY_INVALID", "枢纽没有可用的公钥。")
            except IdentityError:
                raise
            except Exception:
                # 失败后进入冷却，并在有旧公钥时继续用旧的。
                self._fail_until = now + _FAIL_TTL
                keys = self._cached or self._read_cache()
                if not keys:
                    raise IdentityError("IDENTITY_INVALID", "枢纽公钥还没取到，暂时不能核对此令牌。") from None
            else:
                self._store(keys, now)
                # 公钥轮换后，下一次 kid 对不上时应再允许一次强制刷新。
                self._refreshed_for_kid = False
            self._cached = keys
            self._cached_at = now
            return keys

    def _store(self, keys: list[dict], now: int) -> None:
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps({"fetched_at": now, "keys": keys}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _read_cache(self) -> list[dict] | None:
        if self.cache_path is None or not self.cache_path.is_file():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        keys = payload.get("keys")
        if not isinstance(keys, list) or not keys:
            return None
        return [item for item in keys if isinstance(item, dict)]


def _role(app_roles: Any) -> str:
    roles = set(app_roles) if isinstance(app_roles, list) else set()
    if "admin" in roles:
        return "admin"
    if "publisher" in roles:
        return "publisher"
    return "member"


def _name(payload: dict) -> str:
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    for key in ("real_name", "username"):
        value = str(user.get(key) or "").strip()
        if value:
            return value
    return str(payload["sub"])


def _segments(token: str) -> tuple[str, str, bytes]:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise IdentityError("IDENTITY_INVALID", "令牌无效，请从用户管理重新领取。")
    try:
        signature = _b64url_decode(parts[2])
    except ValueError as exc:
        raise IdentityError("IDENTITY_INVALID", "令牌无效，请从用户管理重新领取。") from exc
    return parts[0], parts[1], signature


def _rsa_key(jwk: dict) -> rsa.RSAPublicKey:
    if not _CRYPTO_READY:
        raise IdentityError(
            "IDENTITY_UNCONFIGURED",
            "没有安装枢纽支持。需要时执行 pip install 'qilin-store[hub]'。",
        )
    try:
        n = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
        e = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
        return rsa.RSAPublicNumbers(e, n).public_key()
    except (KeyError, ValueError, TypeError) as exc:
        raise IdentityError("IDENTITY_INVALID", "枢纽公钥还没取到，暂时不能核对此令牌。") from exc


def _verify_rs256(key: rsa.RSAPublicKey, signing_input: bytes, signature: bytes) -> bool:
    try:
        key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read()
