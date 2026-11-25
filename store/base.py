"""Store 的存储底座：连接、建表、迁移、审计与几个共用查询。

`Store` 由这个基类与 auth/submissions/catalog 三个 mixin 组合而成。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from store.diagnostics import storage_warning
from store.errors import Principal, StoreError
from store.safety import PathEscape, check_relative, safe_join
from store.states import SUBMISSION, VALIDATING, advance
from store.validator.validate import Release

_SCHEMA_VERSION = 2
_BLOB_SUBDIRS = frozenset({"releases", "uploads", "submissions", "assets"})


def _extract_blob_key(raw: str | None) -> str | None:
    """将数据库中的物理路径（如 var/blobs/... 或 /var/lib/qilin/blobs/...）提取为相对 blob_dir 的逻辑键。"""
    if not raw or not isinstance(raw, str):
        return None
    clean = raw.strip().replace("\\", "/")
    if not clean or clean == ".":
        return None
    parts = [part for part in clean.split("/") if part and part != "."]
    if not parts or ".." in parts:
        return None
    if not clean.startswith("/") and parts[0] in _BLOB_SUBDIRS:
        return "/".join(parts)
    # 倒序寻找最后一个 blobs 目录段或已知顶级子目录段
    for idx in range(len(parts) - 1, -1, -1):
        if parts[idx] == "blobs" and idx + 1 < len(parts) and parts[idx + 1] in _BLOB_SUBDIRS:
            return "/".join(parts[idx + 1 :])
    for idx in range(len(parts) - 1, -1, -1):
        if parts[idx] in _BLOB_SUBDIRS and idx + 1 < len(parts):
            return "/".join(parts[idx:])
    return None
_MAX_BYTES = 32 * 1024 * 1024
_UPLOAD_TTL_HOURS = 72
_RETRYABLE_URLS = frozenset({"DEP_EXTERNAL_UNREACHABLE"})
# KEY 认证结果的进程内缓存。TTL 短，账号一变就整体作废。
_KEY_CACHE_TTL = 10
_KEY_CACHE_MAX = 1024


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class StoreBase:
    def __init__(
        self,
        db_path: Path,
        blob_dir: Path,
        identity: Any | None = None,
        hub_secret: str = "",
        hub_post: Any | None = None,
    ) -> None:
        self.db_path = db_path
        self.blob_dir = blob_dir
        self._identity = identity
        self._hub_secret = hub_secret
        self._hub_post = hub_post
        blob_dir.mkdir(parents=True, exist_ok=True)
        # 写路径仍用一把进程内锁串行化；读路径不加锁，各线程用自己的连接。
        self._lock = threading.RLock()
        self._auth_cache: dict[str, tuple[Principal, int, float]] = {}
        self._auth_cache_lock = threading.Lock()
        self._auth_epoch = 0
        self._closed = False
        self._shared_memory = str(db_path) == ":memory:"
        self._local = threading.local()
        self._local.pending_prefix = None
        self._main_db = self._connect(db_path)
        # 主线程（含迁移与测试）复用主连接，避免多开一条。
        self._local.db = self._main_db
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS principals (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              role TEXT NOT NULL,
              key_hash TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS submissions (
              id TEXT PRIMARY KEY,
              package_id TEXT NOT NULL,
              version TEXT NOT NULL,
              submit_kind TEXT NOT NULL DEFAULT 'new_version',
              status TEXT NOT NULL,
              publisher_id TEXT NOT NULL,
              manifest_json TEXT NOT NULL,
              validation_json TEXT NOT NULL,
              byte_size INTEGER,
              artifact_path TEXT,
              artifact_sha256 TEXT,
              review_reason TEXT,
              reviewed_by TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS submission_assets (
              submission_id TEXT NOT NULL,
              platform TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              path TEXT NOT NULL,
              PRIMARY KEY (submission_id, platform)
            );
            CREATE TABLE IF NOT EXISTS releases (
              package_id TEXT NOT NULL,
              version TEXT NOT NULL,
              manifest_json TEXT NOT NULL,
              artifact_path TEXT,
              artifact_sha256 TEXT,
              published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              unpublished_at TEXT,
              unpublish_reason TEXT,
              PRIMARY KEY (package_id, version)
            );
            CREATE TABLE IF NOT EXISTS plugin_reviews (
              submission_id TEXT PRIMARY KEY,
              reviewer_id TEXT NOT NULL,
              checklist_json TEXT NOT NULL,
              code_sha256 TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS revisions (
              submission_id TEXT PRIMARY KEY,
              manifest_json TEXT NOT NULL,
              byte_size INTEGER,
              artifact_path TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS upload_tickets (
              token_hash TEXT PRIMARY KEY,
              submission_id TEXT NOT NULL,
              account_id TEXT NOT NULL,
              scope TEXT NOT NULL,
              expires_at INTEGER NOT NULL,
              used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS catalog_index (
              package_id TEXT PRIMARY KEY,
              version TEXT NOT NULL,
              kind TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT '',
              summary TEXT NOT NULL DEFAULT '',
              description TEXT NOT NULL DEFAULT '',
              clients TEXT NOT NULL DEFAULT ',',
              platforms TEXT NOT NULL DEFAULT ',',
              manifest_json TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS package_owners (
              namespace TEXT PRIMARY KEY,
              publisher_id TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS release_assets (
              package_id TEXT NOT NULL,
              version TEXT NOT NULL,
              platform TEXT NOT NULL,
              sha256 TEXT NOT NULL,
              path TEXT NOT NULL,
              PRIMARY KEY (package_id, version, platform)
            );
            CREATE TABLE IF NOT EXISTS audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor TEXT NOT NULL,
              action TEXT NOT NULL,
              package_id TEXT,
              version TEXT,
              channel TEXT NOT NULL DEFAULT 'service',
              at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS accounts (
              id TEXT PRIMARY KEY,
              username TEXT NOT NULL UNIQUE,
              display_name TEXT NOT NULL,
              real_name TEXT NOT NULL DEFAULT '',
              nickname TEXT NOT NULL DEFAULT '',
              department TEXT NOT NULL DEFAULT '',
              job_title TEXT NOT NULL DEFAULT '',
              role TEXT NOT NULL,
              password_salt BLOB,
              password_hash BLOB,
              key_salt BLOB NOT NULL,
              key_hash BLOB NOT NULL,
              key_prefix TEXT,
              key_plain TEXT,
              disabled_at TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY,
              account_id TEXT NOT NULL,
              expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hub_logins (
              token_hash TEXT PRIMARY KEY,
              sub TEXT NOT NULL,
              name TEXT NOT NULL,
              role TEXT NOT NULL,
              access_exp INTEGER NOT NULL,
              refresh_token TEXT,
              refreshing INTEGER NOT NULL DEFAULT 0,
              expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
              id TEXT PRIMARY KEY,
              package_id TEXT NOT NULL,
              version TEXT NOT NULL,
              category TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open',
              summary TEXT NOT NULL,
              steps TEXT NOT NULL DEFAULT '',
              client TEXT NOT NULL DEFAULT '',
              platform TEXT NOT NULL DEFAULT '',
              contact TEXT NOT NULL DEFAULT '',
              reporter_id TEXT,
              resolution TEXT NOT NULL DEFAULT '',
              triaged_by TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self._fts_ready = self._enable_fts()
        self._db.executescript(
            """
            CREATE INDEX IF NOT EXISTS releases_visible ON releases (unpublished_at, package_id);
            CREATE INDEX IF NOT EXISTS submissions_status ON submissions (status, package_id);
            CREATE INDEX IF NOT EXISTS submissions_publisher ON submissions (publisher_id);
            CREATE INDEX IF NOT EXISTS upload_tickets_submission ON upload_tickets (submission_id);
            CREATE INDEX IF NOT EXISTS audit_package ON audit (package_id, id);
            CREATE INDEX IF NOT EXISTS catalog_kind ON catalog_index (kind, package_id);
            CREATE INDEX IF NOT EXISTS reports_package ON reports (package_id, version, status);
            CREATE INDEX IF NOT EXISTS reports_reporter ON reports (reporter_id, created_at);
            CREATE INDEX IF NOT EXISTS reports_status ON reports (status, created_at);
            """
        )
        self._migrate_schema()
        self._rebuild_catalog_index()
        # 启动时恢复上次异常退出残留的 validating，并清理过期上传。
        self._db.execute(
            "UPDATE submissions SET status = ? WHERE status = ?",
            (advance(VALIDATING, "recover", SUBMISSION, "恢复残留状态"), VALIDATING),
        )
        self._db.commit()
        self._prune_stale_uploads(max_age_hours=_UPLOAD_TTL_HOURS, now=None)

    def _migrate_schema(self) -> None:
        """按 PRAGMA user_version 执行结构迁移；存量旧库升级到 _SCHEMA_VERSION，并自愈制品逻辑路径。"""
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version < 1:
            self._migrate_accounts()
        self._backfill_owners()
        self._normalize_blob_paths()
        if version < _SCHEMA_VERSION:
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self._db.commit()

    def to_blob_key(self, path: Path | str) -> str:
        """把位于 blob_dir 下的文件路径转为相对逻辑键（如 releases/<id>/<version>.zip）。"""
        if isinstance(path, Path):
            try:
                rel = path.resolve().relative_to(self.blob_dir.resolve()).as_posix()
                return check_relative(rel)
            except (ValueError, PathEscape):
                pass
        extracted = _extract_blob_key(str(path))
        if extracted:
            return check_relative(extracted)
        return check_relative(str(path))

    def resolve_blob_path(self, stored: str | None, *, fallback_rel: str | None = None) -> Path | None:
        """把数据库存储的逻辑键或历史物理路径解析为当前 blob_dir 下的实际路径。"""
        if stored:
            key = _extract_blob_key(stored)
            if key:
                try:
                    candidate = safe_join(self.blob_dir, key)
                    if candidate.exists():
                        return candidate
                    raw_path = Path(stored)
                    if raw_path.is_absolute() and raw_path.exists():
                        return raw_path
                    return candidate
                except PathEscape:
                    pass
            raw_path = Path(stored)
            if raw_path.is_absolute():
                return raw_path
        if fallback_rel:
            try:
                return safe_join(self.blob_dir, fallback_rel)
            except PathEscape:
                return None
        return None

    def _normalize_blob_paths(self) -> int:
        """幂等重写五张表中的历史相对/绝对物理路径为相对 blob_dir 的逻辑键。"""
        specs = (
            ("releases", ("package_id", "version"), "artifact_path"),
            ("release_assets", ("package_id", "version", "platform"), "path"),
            ("submissions", ("id",), "artifact_path"),
            ("submission_assets", ("submission_id", "platform"), "path"),
            ("revisions", ("submission_id",), "artifact_path"),
        )
        updated = 0
        for table, pk_cols, col in specs:
            select_cols = ", ".join((*pk_cols, col))
            rows = self._db.execute(
                f"SELECT {select_cols} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''"
            ).fetchall()
            where = " AND ".join(f"{pk} = ?" for pk in pk_cols)
            for row in rows:
                current = str(row[col])
                key = _extract_blob_key(current)
                if key and key != current:
                    params = (key, *(row[pk] for pk in pk_cols))
                    self._db.execute(f"UPDATE {table} SET {col} = ? WHERE {where}", params)
                    updated += 1
        return updated
    def _migrate_accounts(self) -> None:
        cols = {row["name"] for row in self._db.execute("PRAGMA table_info(accounts)")}
        for name in ("real_name", "nickname", "department", "job_title"):
            if name not in cols:
                self._db.execute(f"ALTER TABLE accounts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        cols = {row["name"] for row in self._db.execute("PRAGMA table_info(accounts)")}
        for name in ("password_salt", "password_hash"):
            if name not in cols:
                self._db.execute(f"ALTER TABLE accounts ADD COLUMN {name} BLOB")
        if "key_prefix" not in {row["name"] for row in self._db.execute("PRAGMA table_info(accounts)")}:
            # 快速指纹列。旧库没有它，存量账号因此暂时留空，
            # 鉴权时对空值行退回全表比对（见 `_account_from_key`）。
            self._db.execute("ALTER TABLE accounts ADD COLUMN key_prefix TEXT")
        if "key_plain" not in {row["name"] for row in self._db.execute("PRAGMA table_info(accounts)")}:
            # 内网部署的明确取舍：保留 KEY 明文，登录后可直接查看/复制发布配置。
            # 旧库的存量账号留空，需本人重新生成一次后才会持久化。
            self._db.execute("ALTER TABLE accounts ADD COLUMN key_plain TEXT")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS accounts_key_prefix ON accounts (key_prefix)"
        )
        self._db.execute(
            "UPDATE accounts SET real_name = display_name WHERE real_name = '' AND display_name != ''"
        )
        self._db.execute(
            "UPDATE accounts SET nickname = display_name WHERE nickname = '' AND display_name != ''"
        )
        self._db.commit()
    def health(self) -> dict:
        """就绪检查：数据库可读、制品目录可写、在架制品可读。不返回路径与错误原文。"""
        missing_count = 0
        try:
            with self._read():
                self._db.execute("SELECT 1").fetchone()
                rows = self._db.execute(
                    """
                    SELECT package_id, version, manifest_json, artifact_path
                    FROM releases WHERE unpublished_at IS NULL
                    """
                ).fetchall()
                assets = self._db.execute(
                    """
                    SELECT ra.package_id, ra.version, ra.platform, ra.path
                    FROM release_assets ra
                    JOIN releases r ON r.package_id = ra.package_id AND r.version = ra.version
                    WHERE r.unpublished_at IS NULL
                    """
                ).fetchall()
            database = "ok"
            for row in rows:
                manifest = json.loads(row["manifest_json"])
                kind = manifest.get("kind")
                if kind in {"skill", "mcp+skill", "knowledge", "plugin"} or (
                    kind == "mcp" and row["artifact_path"]
                ):
                    fallback = f"releases/{row['package_id']}/{row['version']}.zip"
                    target = self.resolve_blob_path(row["artifact_path"], fallback_rel=fallback)
                    if target is None or not target.is_file():
                        missing_count += 1
            for asset in assets:
                target = self.resolve_blob_path(asset["path"])
                if target is None or not target.is_file():
                    missing_count += 1
        except Exception:  # noqa: BLE001 - 健康检查不能把内部异常抛给探针
            database = "unavailable"
        if not (self.blob_dir.is_dir() and os.access(self.blob_dir, os.W_OK)):
            blobs = "unavailable"
        elif missing_count > 0:
            blobs = "missing_artifacts"
            storage_warning(f"health check detected {missing_count} missing release artifact(s)")
        else:
            blobs = "ok"
        return {
            "status": "ok" if database == "ok" and blobs == "ok" else "degraded",
            "database": database,
            "blobs": blobs,
        }

    def _enable_fts(self) -> bool:
        """建 FTS5 trigram 表。SQLite 没编 FTS5 时返回 False，搜索回退 LIKE。"""
        try:
            self._db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5("
                "package_id UNINDEXED, title, summary, description, tokenize='trigram')"
            )
            return True
        except sqlite3.OperationalError:
            return False

    def _cached_key_principal(self, key: str) -> Principal | None:
        cache_key = _hash(key)
        now = time.time()
        with self._auth_cache_lock:
            hit = self._auth_cache.get(cache_key)
            if hit is None:
                return None
            principal, epoch, expires = hit
            if epoch != self._auth_epoch or expires <= now:
                self._auth_cache.pop(cache_key, None)
                return None
            return principal

    def _cache_key_principal(self, key: str, principal: Principal) -> None:
        with self._auth_cache_lock:
            if len(self._auth_cache) >= _KEY_CACHE_MAX:
                self._auth_cache.clear()
            self._auth_cache[_hash(key)] = (
                principal, self._auth_epoch, time.time() + _KEY_CACHE_TTL,
            )

    def _bump_auth_epoch(self) -> None:
        """账号、KEY 或角色一变，缓存整体作废。单进程里这就是即时失效。"""
        with self._auth_cache_lock:
            self._auth_epoch += 1
            self._auth_cache.clear()

    def _connect(self, path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @property
    def _db(self) -> sqlite3.Connection:
        """当前线程的连接。

        `:memory:` 库无法跨连接共享，回落到主连接；文件库每个线程各一条。
        线程退出时 `threading.local` 清空，连接随引用归零关闭，不需要额外回收。
        """
        if self._closed:
            raise RuntimeError("Store 已经关闭")
        if self._shared_memory:
            return self._main_db
        db = getattr(self._local, "db", None)
        if db is None:
            db = self._connect(self.db_path)
            self._local.db = db
        return db

    @contextmanager
    def _read(self):
        """读路径的上下文。

        不加全局锁，但开一个只读事务，让一次请求里的多条查询看到同一个快照。
        已经是事务中（嵌套读或调用方先开了事务）就不再开。
        """
        if self._shared_memory:
            # 内存库只有一条连接，多线程读也走同一把锁，避免事务嵌套冲突。
            with self._lock:
                yield
            return
        db = self._db
        started = not db.in_transaction
        if started:
            db.execute("BEGIN")
        try:
            yield
        finally:
            if started and db.in_transaction:
                db.rollback()

    @contextmanager
    def _write(self):
        with self._lock:
            yield

    def _flush_prefix_backfill(self) -> None:
        """把读路径上发现的旧库 KEY 指纹补上。

        读事务里不能写（会撞 `SQLITE_BUSY_SNAPSHOT`），所以 `_account_from_key`
        只把命中的旧行记在线程本地，事务结束后在这里补一次。
        """
        pending = getattr(self._local, "pending_prefix", None)
        if not pending or self._db.in_transaction:
            return
        self._local.pending_prefix = None
        account_id, fingerprint = pending
        try:
            self._db.execute(
                "UPDATE accounts SET key_prefix = ? WHERE id = ? AND (key_prefix IS NULL OR key_prefix = '')",
                (fingerprint, account_id),
            )
            self._db.commit()
        except sqlite3.OperationalError:
            # 补指纹只是加速；失败了下次还会走兜底分支，不影响正确性。
            pass

    def close(self) -> None:
        with self._lock:
            db = getattr(self._local, "db", None)
            if db is not None and db is not self._main_db:
                db.close()
                self._local.db = None
            if not self._closed:
                self._main_db.close()
                self._closed = True
                self._local.db = None
    def _require(self, token: str, roles: set[str]) -> Principal:
        principal = self._principal_from_token(token)
        if principal is None:
            # 没登录 / 凭据无效是 401，跟"登录了但角色不够"（403）分开：
            # 客户端才知道该重开登录，还是只说一句权限不够。
            raise StoreError("AUTH_REQUIRED", "请先登录。")
        if principal.role not in roles:
            raise StoreError("ROLE_FORBIDDEN", "当前身份不能做这件事。")
        return principal
    def _submission(self, submission_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT * FROM submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if row is None:
            raise StoreError("RESOURCE_NOT_FOUND", "没有这条提交")
        return row
    def _audit(
        self,
        actor: str,
        action: str,
        package_id: str | None,
        version: str | None,
        channel: str = "service",
        *,
        commit: bool = True,
    ) -> None:
        """写一条审计。`commit=False` 交给调用方与业务写入同一个事务提交。

        发布这类"要么全成、要么全不成"的操作，审计必须和 releases/index/状态
        在同一个事务里，否则会出现"发布了但没有审计"（评审 P2-2/P2-3）。
        """
        self._db.execute(
            """
            INSERT INTO audit (actor, action, package_id, version, channel)
            VALUES (?, ?, ?, ?, ?)
            """,
            (actor, action, package_id, version, channel),
        )
        if commit:
            self._db.commit()
    def _releases(self) -> list[Release]:
        rows = self._db.execute(
            """
            SELECT package_id, version, manifest_json FROM releases
            WHERE unpublished_at IS NULL
            """
        ).fetchall()
        found: list[Release] = []
        for row in rows:
            manifest = json.loads(row["manifest_json"])
            found.append(Release(row["package_id"], row["version"], manifest.get("kind", "")))
        return found
