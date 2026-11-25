"""向枢纽换访问令牌。应用密钥只用于签名，不写进响应。"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.request
from typing import Callable

from store.identity import IdentityError

TOKEN_PATH = "/api/v1/auth/token"


def post_token(
    *,
    origin: str,
    app_id: str,
    secret: str,
    kind: str,
    payload: dict,
    opener: Callable[[urllib.request.Request], bytes] | None = None,
) -> dict:
    body_obj = {"grant_type": kind, "app_id": app_id}
    body_obj.update(payload)
    body = json.dumps(body_obj, ensure_ascii=False).encode()
    request = urllib.request.Request(
        origin.rstrip("/") + TOKEN_PATH,
        data=body,
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if kind == "authorization_code":
        for name, value in _s2s(app_id, secret, body).items():
            request.add_header(name, value)
    fetch = opener or _open
    try:
        raw = fetch(request)
    except Exception as exc:
        raise IdentityError("IDENTITY_INVALID", "枢纽没有换出令牌。") from exc
    return _data(raw)


def _s2s(app_id: str, secret: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    signed = "\n".join([
        "POST",
        TOKEN_PATH,
        timestamp,
        nonce,
        hashlib.sha256(body).hexdigest(),
    ])
    signature = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Lucy-App-Id": app_id,
        "X-Lucy-Timestamp": timestamp,
        "X-Lucy-Nonce": nonce,
        "X-Lucy-Signature": signature,
    }


def _data(raw: bytes) -> dict:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IdentityError("IDENTITY_INVALID", "枢纽没有换出令牌。") from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise IdentityError("IDENTITY_INVALID", "枢纽没有换出令牌。")
    data = payload.get("data")
    if not isinstance(data, dict) or not str(data.get("access_token") or "").strip():
        raise IdentityError("IDENTITY_INVALID", "枢纽没有换出令牌。")
    return data


def _open(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()
