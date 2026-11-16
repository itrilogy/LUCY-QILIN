"""测试夹具：用一把临时 RSA 密钥签枢纽形状的令牌。不是商店签发的密钥。"""

from __future__ import annotations

import base64
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from store.identity import HubIdentity

ORIGIN = "http://hub.example"
APP_ID = "lucy-qilin"


def make_hub(*, now: int | None = None, app_id: str = APP_ID):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "test-key",
        "n": _b64(public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")),
        "e": _b64(public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")),
    }
    body = json.dumps({"keys": [jwk]}).encode()

    def opener(url: str) -> bytes:
        if not url.startswith(ORIGIN):
            raise OSError(url)
        return body

    clock = now if now is not None else int(time.time())
    identity = HubIdentity(ORIGIN, app_id=app_id, opener=opener, now=lambda: clock)
    return identity, lambda **kwargs: _sign(private, clock=clock, app_id=app_id, **kwargs)


def _sign(private, *, clock: int, app_id: str, roles: list[str], sub: str = "user-1", name: str = "张三", typ: str = "access", aud: str | None = None, exp: int | None = None, alg: str = "RS256") -> str:
    header = {"alg": alg, "typ": "JWT", "kid": "test-key"}
    payload = {
        "iss": ORIGIN,
        "sub": sub,
        "aud": app_id if aud is None else aud,
        "exp": clock + 600 if exp is None else exp,
        "nbf": clock,
        "iat": clock,
        "jti": "jti-1",
        "typ": typ,
        "role_ver": 1,
        "global_role": "MEMBER",
        "app_roles": roles,
        "scope": f"app:{app_id}",
        "user": {"username": "zhangsan", "real_name": name},
    }
    segments = [
        _b64(json.dumps(header, separators=(",", ":")).encode()),
        _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()),
    ]
    signing = ".".join(segments).encode()
    if alg == "RS256":
        signature = private.sign(signing, padding.PKCS1v15(), hashes.SHA256())
    else:
        signature = b"not-a-signature"
    segments.append(_b64(signature))
    return ".".join(segments)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
