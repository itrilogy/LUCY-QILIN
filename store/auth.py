"""认证与账号：本地口令、商店 KEY、会话与枢纽令牌。"""

from __future__ import annotations

import secrets
import sqlite3
import time

from store.base import _hash
from store.errors import Principal, StoreError
from store.roles import ADMIN_ROLES, PUBLISHER_ROLES
from store.identity import APP_ID, IdentityError, _name, _role


def _hub_profile(claims: dict) -> dict:
    return {
        "id": str(claims["sub"]),
        "name": _name(claims),
        "username": "",
        "role": _role(claims.get("app_roles")),
        "source": "hub",
        "key_ready": False,
    }


class AuthMixin:
    def create_principal(self, name: str, role: str) -> tuple[Principal, str]:
        with self._lock:
            return self._create_principal(name, role)
    def _create_principal(self, name: str, role: str) -> tuple[Principal, str]:
        if role not in PUBLISHER_ROLES:
            raise StoreError("ROLE_FORBIDDEN", "角色只能是 publisher、reviewer 或 admin")
        token = secrets.token_urlsafe(32)
        principal = Principal(secrets.token_hex(8), name, role)
        self._db.execute(
            "INSERT INTO principals (id, name, role, key_hash) VALUES (?, ?, ?, ?)",
            (principal.id, name, role, _hash(token)),
        )
        self._db.commit()
        self._audit(principal.id, "create_key", None, None)
        return principal, token
    def principal_from_token(self, token: str) -> Principal | None:
        with self._read():
            return self._principal_from_token(token)
    def prepare_token(self, token: str) -> None:
        """进入全局锁之前，先把需要联网的凭据验掉。

        只有枢纽 JWT 需要取 JWKS；取公钥是网络调用，放在锁外做，
        一个慢请求就不会挡住货架这类只读接口。形状判断与
        `_principal_from_token` 保持一致，本地 KEY 与会话不在这里多算一次。
        """
        if not token or self._identity is None:
            return
        if token.count(".") != 2 or len(token) <= 256:
            return
        try:
            self._identity.principal(token)
        except IdentityError as exc:
            raise StoreError(exc.rule, exc.message) from exc
    def _principal_from_token(self, token: str) -> Principal | None:
        if not token:
            return None
        session = self._session_principal(token)
        if session is not None:
            return session
        hub = self._hub_principal(token)
        if hub is not None:
            return hub
        account = self._account_from_key(token)
        if account is not None:
            return account
        if token.count(".") == 2 and len(token) > 256:
            if self._identity is None:
                raise StoreError("IDENTITY_UNCONFIGURED", "商店还没接上枢纽，不能核对此令牌。")
            try:
                found = self._identity.principal(token)
            except IdentityError as exc:
                raise StoreError(exc.rule, exc.message) from exc
            return Principal(found.id, found.name, found.role)
        return self._local_principal(token)
    def _session_principal(self, token: str) -> Principal | None:
        row = self._db.execute(
            """
            SELECT accounts.id, accounts.display_name, accounts.role, accounts.disabled_at, sessions.expires_at
            FROM sessions JOIN accounts ON accounts.id = sessions.account_id
            WHERE sessions.token_hash = ?
            """,
            (_hash(token),),
        ).fetchone()
        if row is None:
            return None
        if int(row["expires_at"]) < int(time.time()):
            self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
            self._db.commit()
            return None
        if row["disabled_at"]:
            raise StoreError("ACCOUNT_DISABLED", "这个用户已停用。")
        return Principal(row["id"], row["display_name"], row["role"])
    def _hub_row(self, token: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM hub_logins WHERE token_hash = ?", (_hash(token),),
        ).fetchone()
    def _hub_principal(self, token: str) -> Principal | None:
        row = self._hub_row(token)
        if row is None:
            return None
        now = int(time.time())
        if int(row["expires_at"]) < now or int(row["access_exp"]) < now:
            if int(row["expires_at"]) < now or not row["refresh_token"]:
                self._db.execute("DELETE FROM hub_logins WHERE token_hash = ?", (_hash(token),))
                self._db.commit()
            return None
        return Principal(row["sub"], row["name"], row["role"])
    def _account_from_key(self, key: str) -> Principal | None:
        """按 KEY 找账号。

        先用 `key_prefix` 定位候选行，再对候选行做一次 scrypt。旧库里
        `key_prefix` 为空的存量账号会落到下面的兜底分支，仍然全表比对，
        保证升级后不会「原来的 KEY 突然登不上」。
        """
        from store.accounts import key_matches, key_prefix

        if not 4 <= len(key) <= 256:
            return None
        cached = self._cached_key_principal(key)
        if cached is not None:
            return cached
        fingerprint = key_prefix(key)
        row = self._db.execute(
            """
            SELECT id, display_name, role, key_salt, key_hash, disabled_at
            FROM accounts WHERE key_prefix = ?
            """,
            (fingerprint,),
        ).fetchone()
        if row is not None and key_matches(key, row["key_salt"], row["key_hash"]):
            if row["disabled_at"]:
                raise StoreError("ACCOUNT_DISABLED", "这个用户已停用。")
            principal = Principal(row["id"], row["display_name"], row["role"])
            self._cache_key_principal(key, principal)
            return principal
        rows = self._db.execute(
            """
            SELECT id, display_name, role, key_salt, key_hash, disabled_at
            FROM accounts WHERE key_prefix IS NULL OR key_prefix = ''
            """
        ).fetchall()
        for legacy in rows:
            if key_matches(key, legacy["key_salt"], legacy["key_hash"]):
                # 顺手补上指纹：下次这把 KEY 走上面的快路径。
                self._db.execute(
                    "UPDATE accounts SET key_prefix = ? WHERE id = ?",
                    (fingerprint, legacy["id"]),
                )
                self._db.commit()
                if legacy["disabled_at"]:
                    raise StoreError("ACCOUNT_DISABLED", "这个用户已停用。")
                principal = Principal(legacy["id"], legacy["display_name"], legacy["role"])
                self._cache_key_principal(key, principal)
                return principal
        return None
    def _profile_from_row(self, row: sqlite3.Row) -> dict:
        nickname = row["nickname"] or row["display_name"]
        return {
            "id": row["id"],
            "username": row["username"],
            "name": nickname,
            "real_name": row["real_name"] or row["display_name"],
            "nickname": nickname,
            "department": row["department"] or "",
            "job_title": row["job_title"] or "",
            "role": row["role"],
            "key_ready": bool(row["key_hash"]),
            "store_key": row["key_plain"] or "",
        }
    def _open_session(self, account_id: str) -> tuple[dict, str]:
        row = self._db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        token = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO sessions (token_hash, account_id, expires_at) VALUES (?, ?, ?)",
            (_hash(token), account_id, int(time.time()) + 7 * 24 * 3600),
        )
        self._db.commit()
        return self._profile_from_row(row), token
    def _account_row(self, account_id: str) -> sqlite3.Row:
        row = self._db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise StoreError("VERSION_UNAVAILABLE", "没有这个用户。")
        return row
    def _keep_an_admin(self, leaving: str) -> None:
        others = self._db.execute(
            """
            SELECT 1 FROM accounts
            WHERE role = 'admin' AND disabled_at IS NULL AND id != ?
            """,
            (leaving,),
        ).fetchone()
        if others is None:
            raise StoreError("LAST_ADMIN", "至少要留下一位审核。")
    def _local_principal(self, token: str) -> Principal | None:
        row = self._db.execute(
            "SELECT id, name, role FROM principals WHERE key_hash = ? AND revoked_at IS NULL",
            (_hash(token),),
        ).fetchone()
        if row is None:
            return None
        return Principal(row["id"], row["name"], row["role"])
    def whoami(self, token: str) -> dict:
        with self._read():
            if not token:
                return {"role": "anonymous", "source": "none"}
            principal = self._principal_from_token(token)
            if principal is None:
                return {"role": "anonymous", "source": "none"}
            account = self._db.execute(
                "SELECT * FROM accounts WHERE id = ?", (principal.id,),
            ).fetchone()
            if account is not None:
                profile = self._profile_from_row(account)
                profile["source"] = "account"
                return profile
            return {
                "id": principal.id,
                "name": principal.name,
                "username": "",
                "role": principal.role,
                "source": "hub" if self._identity is not None else "local",
                "key_ready": False,
            }
    def register_account(
        self,
        username: str,
        real_name: str,
        nickname: str,
        department: str = "",
        job_title: str = "",
        password: str = "",
    ) -> tuple[dict, str, str]:
        with self._lock:
            return self._register_account(username, real_name, nickname, department, job_title, password)
    def _register_account(
        self,
        username: str,
        real_name: str,
        nickname: str,
        department: str,
        job_title: str,
        password: str,
    ) -> tuple[dict, str, str]:
        from store.accounts import make_key, optional_text, password_of, person_name, username_of

        username = username_of(username)
        if self._db.execute("SELECT 1 FROM accounts WHERE username = ?", (username,)).fetchone():
            raise StoreError("USERNAME_TAKEN", "用户名已存在。")
        real_name = person_name(real_name, "姓名")
        nickname = person_name(nickname, "昵称")
        department = optional_text(department, "部门")
        job_title = optional_text(job_title, "岗位")
        password = password_of(password, username)
        role = "admin" if self._db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is None else "publisher"
        account_id = "u_" + secrets.token_hex(8)
        key, salt, hashed, fingerprint = self._fresh_key()
        password_salt, password_hash = make_key(password)
        self._db.execute(
            """
            INSERT INTO accounts (
              id, username, display_name, real_name, nickname, department, job_title,
              role, password_salt, password_hash, key_salt, key_hash, key_prefix, key_plain
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id, username, nickname, real_name, nickname, department, job_title,
                role, password_salt, password_hash, salt, hashed, fingerprint, key,
            ),
        )
        self._db.commit()
        self._audit(account_id, "register", None, None)
        profile, session = self._open_session(account_id)
        return profile, session, key
    def _fresh_key(self, keep: str | None = None) -> tuple[str, bytes, bytes, str]:
        """生成一把新 KEY，返回 (明文, 盐, 校验值, 指纹)。

        只按 `key_prefix` 查重，不做全表 scrypt。随机 KEY 值重复的概率可以
        忽略；指纹碰撞时重试。旧实现每次注册都把全表逐行 scrypt 一遍，账号
        越多越慢，是 O(N) 的写放大。
        """
        from store.accounts import issue_key, key_prefix, make_key

        for _ in range(3):
            key = issue_key()
            fingerprint = key_prefix(key)
            row = self._db.execute(
                "SELECT id FROM accounts WHERE key_prefix = ? AND id != ?",
                (fingerprint, keep or ""),
            ).fetchone()
            if row is not None:
                continue
            salt, hashed = make_key(key)
            return key, salt, hashed, fingerprint
        raise StoreError("KEY_TAKEN", "没能生成一把不重复的 KEY。")
    def login_account(self, username: str, password: str) -> tuple[dict, str]:
        with self._lock:
            return self._login_account(username, password)
    def _login_account(self, username: str, password: str) -> tuple[dict, str]:
        from store.accounts import key_matches, make_key, username_of

        try:
            username = username_of(username)
        except StoreError:
            make_key("placeholder-key")
            raise StoreError("PASSWORD_MISMATCH", "用户名或口令不正确。") from None
        row = self._db.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
        matched = (
            row is not None
            and row["password_hash"]
            and key_matches(password, row["password_salt"], row["password_hash"])
        )
        if not matched:
            if row is None:
                make_key(password or "placeholder-key")
            raise StoreError("PASSWORD_MISMATCH", "用户名或口令不正确。")
        if row["disabled_at"]:
            raise StoreError("ACCOUNT_DISABLED", "这个用户已停用。")
        return self._open_session(row["id"])
    def change_password(self, token: str, old_password: str, new_password: str) -> dict:
        with self._lock:
            return self._change_password(token, old_password, new_password)
    def _change_password(self, token: str, old_password: str, new_password: str) -> dict:
        from store.accounts import key_matches, make_key, password_of

        principal = self._require(token, PUBLISHER_ROLES)
        row = self._db.execute("SELECT * FROM accounts WHERE id = ?", (principal.id,)).fetchone()
        if row is None:
            raise StoreError("ROLE_FORBIDDEN", "只有本人能修改自己的口令。")
        if not row["password_hash"] or not key_matches(old_password, row["password_salt"], row["password_hash"]):
            raise StoreError("PASSWORD_MISMATCH", "原来的口令不正确。")
        new_password = password_of(new_password, row["username"])
        if key_matches(new_password, row["password_salt"], row["password_hash"]):
            raise StoreError("PASSWORD_INVALID", "新口令要和原来的不一样。")
        salt, hashed = make_key(new_password)
        self._db.execute(
            "UPDATE accounts SET password_salt = ?, password_hash = ? WHERE id = ?",
            (salt, hashed, row["id"]),
        )
        self._db.execute(
            "DELETE FROM sessions WHERE account_id = ? AND token_hash != ?",
            (row["id"], _hash(token)),
        )
        self._db.commit()
        self._audit(row["id"], "change_password", None, None)
        return self._profile_from_row(self._account_row(row["id"]))
    def logout_account(self, token: str) -> None:
        with self._lock:
            if token:
                self._db.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
                self._db.commit()
    def update_profile(
        self,
        token: str,
        real_name: str,
        nickname: str,
        department: str = "",
        job_title: str = "",
    ) -> dict:
        with self._lock:
            return self._update_profile(token, real_name, nickname, department, job_title)
    def _update_profile(
        self, token: str, real_name: str, nickname: str, department: str, job_title: str,
    ) -> dict:
        from store.accounts import optional_text, person_name

        principal = self._require(token, PUBLISHER_ROLES)
        row = self._db.execute("SELECT * FROM accounts WHERE id = ?", (principal.id,)).fetchone()
        if row is None:
            raise StoreError("ROLE_FORBIDDEN", "只有栖林用户能改自己的资料。")
        real_name = person_name(real_name, "姓名")
        nickname = person_name(nickname, "昵称")
        department = optional_text(department, "部门")
        job_title = optional_text(job_title, "岗位")
        self._db.execute(
            """
            UPDATE accounts
            SET display_name = ?, real_name = ?, nickname = ?, department = ?, job_title = ?
            WHERE id = ?
            """,
            (nickname, real_name, nickname, department, job_title, row["id"]),
        )
        self._db.commit()
        self._audit(row["id"], "update_profile", None, None)
        return self._profile_from_row(self._account_row(row["id"]))
    def issue_account_key(self, token: str) -> tuple[dict, str, str]:
        with self._lock:
            return self._issue_account_key(token)
    def _issue_account_key(self, token: str) -> tuple[dict, str, str]:
        principal = self._require(token, PUBLISHER_ROLES)
        row = self._db.execute("SELECT id FROM accounts WHERE id = ?", (principal.id,)).fetchone()
        if row is None:
            raise StoreError("ROLE_FORBIDDEN", "只有本人能重新生成自己的 KEY。")
        key, salt, hashed, fingerprint = self._fresh_key(keep=row["id"])
        self._db.execute(
            "UPDATE accounts SET key_salt = ?, key_hash = ?, key_prefix = ?, key_plain = ? WHERE id = ?",
            (salt, hashed, fingerprint, key, row["id"]),
        )
        self._db.execute("DELETE FROM sessions WHERE account_id = ?", (row["id"],))
        self._db.commit()
        self._bump_auth_epoch()
        self._audit(row["id"], "issue_key", None, None)
        profile, session = self._open_session(row["id"])
        return profile, session, key
    def list_account_names(self) -> list[dict]:
        with self._read():
            rows = self._db.execute(
                """
                SELECT username, display_name, nickname FROM accounts
                WHERE disabled_at IS NULL ORDER BY created_at
                """
            ).fetchall()
            return [
                {"username": row["username"], "display_name": row["nickname"] or row["display_name"]}
                for row in rows
            ]
    def list_accounts(self, token: str) -> list[dict]:
        with self._read():
            self._require(token, ADMIN_ROLES)
            rows = self._db.execute(
                """
                SELECT id, username, display_name, real_name, nickname, department, job_title,
                       role, disabled_at
                FROM accounts ORDER BY created_at
                """
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "display_name": row["nickname"] or row["display_name"],
                    "real_name": row["real_name"] or row["display_name"],
                    "nickname": row["nickname"] or row["display_name"],
                    "department": row["department"] or "",
                    "job_title": row["job_title"] or "",
                    "role": row["role"],
                    "disabled": bool(row["disabled_at"]),
                }
                for row in rows
            ]
    def set_account_role(self, token: str, account_id: str, role: str) -> None:
        with self._lock:
            admin = self._require(token, ADMIN_ROLES)
            if role not in PUBLISHER_ROLES:
                raise StoreError("ROLE_FORBIDDEN", "角色只能是作者、审核或管理员。")
            row = self._account_row(account_id)
            if row["role"] == "admin" and role != "admin":
                self._keep_an_admin(row["id"])
            if admin.id == row["id"] and role != "admin":
                self._keep_an_admin(row["id"])
            self._db.execute("UPDATE accounts SET role = ? WHERE id = ?", (role, row["id"]))
            self._db.commit()
            self._bump_auth_epoch()
            self._audit(admin.id, "set_role", None, None)
    def disable_account(self, token: str, account_id: str) -> None:
        with self._lock:
            admin = self._require(token, ADMIN_ROLES)
            row = self._account_row(account_id)
            if row["id"] == admin.id:
                raise StoreError("ROLE_FORBIDDEN", "不能停用自己。")
            if row["role"] == "admin":
                self._keep_an_admin(row["id"])
            self._db.execute(
                "UPDATE accounts SET disabled_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],),
            )
            self._db.execute("DELETE FROM sessions WHERE account_id = ?", (row["id"],))
            self._db.commit()
            self._bump_auth_epoch()
            self._audit(admin.id, "disable_account", None, None)
    def reset_account_password(self, token: str, account_id: str) -> dict:
        with self._lock:
            return self._reset_account_password(token, account_id)
    def _reset_account_password(self, token: str, account_id: str) -> dict:
        from store.accounts import issue_password, make_key, password_of

        admin = self._require(token, ADMIN_ROLES)
        row = self._account_row(account_id)
        if row["id"] == admin.id:
            raise StoreError("ROLE_FORBIDDEN", "不能重置自己的口令。请在资料页修改。")
        password = password_of(issue_password(row["username"]), row["username"])
        salt, hashed = make_key(password)
        self._db.execute(
            "UPDATE accounts SET password_salt = ?, password_hash = ? WHERE id = ?",
            (salt, hashed, row["id"]),
        )
        self._db.execute("DELETE FROM sessions WHERE account_id = ?", (row["id"],))
        self._db.commit()
        self._audit(admin.id, "reset_password", None, None)
        return {
            "id": row["id"],
            "username": row["username"],
            "nickname": row["nickname"] or row["display_name"],
            "password": password,
        }
    def identity_status(self) -> dict:
        login = bool(self._hub_secret)
        if self._identity is None:
            return {"mode": "unset", "app_id": APP_ID, "origin": "", "login": False}
        return {
            "mode": "hub",
            "app_id": self._identity.app_id,
            "origin": self._identity.origin,
            "login": login,
        }
    def login_hub_code(self, code: str) -> tuple[dict, str]:
        if self._identity is None or not self._hub_secret:
            raise StoreError("IDENTITY_UNCONFIGURED", "商店还没接上枢纽，不能用跳转登录。")
        code = code.strip()
        if not code or len(code) > 512:
            raise StoreError("IDENTITY_INVALID", "交换码无效。")
        try:
            data = self._call_hub("authorization_code", {"code": code})
            claims = self._identity.access_claims(str(data.get("access_token") or ""))
        except IdentityError as exc:
            raise StoreError(exc.rule, exc.message) from exc
        refresh = str(data.get("refresh_token") or "")
        try:
            ttl = int(data.get("refresh_expires_in") or 14 * 24 * 3600)
        except (TypeError, ValueError):
            ttl = 14 * 24 * 3600
        with self._lock:
            return self._save_hub_login(claims, refresh, ttl)
    def refresh_hub_if_due(self, token: str) -> None:
        if not token or self._identity is None:
            return
        # 常见路径是「没有刷新令牌」或「还没到点」。这条路径不拿全局锁，
        # 否则每个请求都会在鉴权前排队一次。
        row = self._hub_row(token)
        if row is None or not row["refresh_token"] or row["refreshing"]:
            return
        if int(row["access_exp"]) > int(time.time()) + 60:
            return
        with self._write():
            current = self._hub_row(token)
            if current is None or not current["refresh_token"] or current["refreshing"]:
                return
            if int(current["access_exp"]) > int(time.time()) + 60:
                return
            self._db.execute(
                "UPDATE hub_logins SET refreshing = 1 WHERE token_hash = ?",
                (_hash(token),),
            )
            self._db.commit()
            refresh = current["refresh_token"]
            sub = current["sub"]
        try:
            data = self._call_hub("refresh_token", {"refresh_token": refresh})
            claims = self._identity.access_claims(str(data.get("access_token") or ""))
            rotated = str(data.get("refresh_token") or "")
            if not rotated or str(claims.get("sub") or "") != sub:
                raise IdentityError("IDENTITY_INVALID", "枢纽没有换出令牌。")
        except IdentityError:
            with self._write():
                self._db.execute(
                    "DELETE FROM hub_logins WHERE token_hash = ? AND refresh_token = ?",
                    (_hash(token), refresh),
                )
                self._db.commit()
            return
        try:
            ttl = int(data.get("refresh_expires_in") or 14 * 24 * 3600)
        except (TypeError, ValueError):
            ttl = 14 * 24 * 3600
        with self._write():
            current = self._hub_row(token)
            if current is None or str(current["refresh_token"] or "") != refresh:
                return
            self._write_hub_login(token, claims, rotated, ttl)
    def _call_hub(self, kind: str, payload: dict) -> dict:
        if self._hub_post is not None:
            return self._hub_post(kind, payload)
        from store.hub_client import post_token

        return post_token(
            origin=self._identity.origin,
            app_id=self._identity.app_id,
            secret=self._hub_secret,
            kind=kind,
            payload=payload,
        )
    def _save_hub_login(self, claims: dict, refresh: str, ttl: int) -> tuple[dict, str]:
        token = secrets.token_urlsafe(32)
        self._write_hub_login(token, claims, refresh, ttl)
        return _hub_profile(claims), token
    def _write_hub_login(self, token: str, claims: dict, refresh: str, ttl: int) -> None:
        now = int(time.time())
        exp = int(claims["exp"])
        expires = exp if not refresh else now + max(60, min(ttl, 14 * 24 * 3600))
        self._db.execute(
            """
            INSERT INTO hub_logins
              (token_hash, sub, name, role, access_exp, refresh_token, refreshing, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(token_hash) DO UPDATE SET
              sub = excluded.sub,
              name = excluded.name,
              role = excluded.role,
              access_exp = excluded.access_exp,
              refresh_token = excluded.refresh_token,
              refreshing = 0,
              expires_at = excluded.expires_at
            """,
            (
                _hash(token),
                str(claims["sub"]),
                _name(claims),
                _role(claims.get("app_roles")),
                exp,
                refresh or None,
                expires,
            ),
        )
        self._db.commit()
    def accept_logout(self, token: str) -> None:
        if self._identity is None:
            raise StoreError("IDENTITY_UNCONFIGURED", "商店还没接上枢纽。")
        try:
            claims = self._identity.assert_logout(token)
        except IdentityError as exc:
            raise StoreError(exc.rule, exc.message) from exc
        with self._lock:
            self._db.execute("DELETE FROM hub_logins WHERE sub = ?", (str(claims.get("sub") or ""),))
            self._db.commit()
    def has_principals(self) -> bool:
        with self._read():
            return self._db.execute("SELECT 1 FROM principals LIMIT 1").fetchone() is not None
