"""提交与上传：清单校验、三段上传、修订、撤回与残留清理。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import tempfile
import time
import zipfile
from functools import cmp_to_key
from pathlib import Path
from typing import Callable

from store.base import _MAX_BYTES, _RETRYABLE_URLS, _UPLOAD_TTL_HOURS
from store.blobs import safe_extract, sha256_file, write_zip
from store.errors import Principal, StoreError
from store.roles import ADMIN_ROLES, PUBLISHER_ROLES
from store.models import SubmitResult
from store.safety import PathEscape, safe_join
from store.semver import cmp as _cmp
from store.states import SUBMISSION, advance
from store.validator.validate import validate_manifest, validate_path
from store.views import _check_add_platform, _diff, _findings, _write_tree, review_view

_UPLOAD_TICKET_TTL = 3600


def _namespace_of(package_id: str) -> str:
    """`a.b.c` 的命名空间是第一个点之前的 `a`。"""
    return package_id.split(".", 1)[0]


class SubmissionMixin:
    def submit(
        self,
        token: str,
        manifest: dict,
        files: dict[str, bytes],
        *,
        submit_kind: str = "new_version",
        channel: str = "service",
    ) -> SubmitResult:
        with self._lock:
            publisher = self._require(token, PUBLISHER_ROLES)
            stored = self._prepare(publisher, manifest, submit_kind)
            releases = self._releases()
        package_id = str(stored.get("id") or "")
        version = str(stored.get("version") or "")
        submission_id = secrets.token_hex(8)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tree(root, files, stored)
            report = validate_path(root, releases=releases)
            archive = digest = None
            if report.ok:
                archive = self.blob_dir / "submissions" / f"{submission_id}.zip"
                digest = write_zip(root, archive)
            with self._lock:
                try:
                    # 锁外打包期间若有并发发布，二次核对归属与版本不可变性。
                    stored = self._prepare(publisher, manifest, submit_kind)
                    if report.ok and archive is not None:
                        self._keep_assets(submission_id, root, stored)
                except StoreError:
                    if archive is not None:
                        archive.unlink(missing_ok=True)
                    raise
                self._insert_submission(
                    submission_id, stored, publisher.id, "pending" if report.ok else "rejected",
                    report, submit_kind, archive, digest,
                )
                self._audit(publisher.id, "submit", package_id, version, channel)
        return SubmitResult(submission_id, "pending" if report.ok else "rejected", report.rules())
    def begin_submit(
        self,
        token: str,
        manifest: dict,
        *,
        submit_kind: str = "new_version",
        byte_size: int,
        channel: str = "mcp",
    ) -> dict:
        with self._lock:
            return self._begin_submit(
                token, manifest, submit_kind=submit_kind, byte_size=byte_size, channel=channel,
            )
    def _begin_submit(self, token: str, manifest: dict, *, submit_kind: str, byte_size: int, channel: str) -> dict:
        publisher = self._require(token, PUBLISHER_ROLES)
        if byte_size < 0 or byte_size > _MAX_BYTES:
            raise StoreError("CHECKSUM", "制品大小不在允许范围内")
        # 只开上传不动手的人会把状态与 blob 一直留着。这里顺带清一次，
        # 不引入定时线程；量级小，成本可以忽略。
        self._prune_stale_uploads(max_age_hours=_UPLOAD_TTL_HOURS, now=None)
        stored = self._prepare(publisher, manifest, submit_kind)
        report = validate_manifest(stored, releases=self._releases())
        if not report.ok:
            return {"submission_id": None, "status": "rejected", "rules": report.rules(), "put_path": None}
        submission_id = secrets.token_hex(8)
        self._insert_submission(
            submission_id, stored, publisher.id, "uploading", report, submit_kind, None, None, byte_size,
        )
        self._audit(publisher.id, "begin_submit", stored.get("id"), stored.get("version"), channel)
        ticket = self._new_upload_ticket(submission_id, publisher.id, "submit")
        return {
            "submission_id": submission_id,
            "status": "uploading",
            "rules": [],
            "put_path": f"/v1/uploads/{submission_id}?ticket={ticket}",
        }
    def _new_upload_ticket(self, submission_id: str, account_id: str, scope: str) -> str:
        """生成一次性上传票据；只存哈希，不把票据写进日志。"""
        ticket = secrets.token_urlsafe(32)
        self._db.execute(
            "INSERT INTO upload_tickets (token_hash, submission_id, account_id, scope, expires_at) VALUES (?, ?, ?, ?, ?)",
            (hashlib.sha256(ticket.encode()).hexdigest(), submission_id, account_id, scope,
             int(time.time()) + _UPLOAD_TICKET_TTL),
        )
        self._db.commit()
        return ticket

    def _upload_ticket(self, submission_id: str, scope: str, ticket: str) -> str:
        """校验并消费一次性票据，返回账号 id。"""
        if not ticket:
            raise StoreError("ROLE_FORBIDDEN", "缺少上传票据或登录。")
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        row = self._db.execute(
            "SELECT * FROM upload_tickets WHERE token_hash = ?", (digest,),
        ).fetchone()
        if row is None or row["submission_id"] != submission_id or row["scope"] != scope:
            raise StoreError("ROLE_FORBIDDEN", "上传票据无效或已过期。")
        if row["used_at"] or int(row["expires_at"]) < int(time.time()):
            raise StoreError("ROLE_FORBIDDEN", "上传票据无效或已过期。")
        changed = self._db.execute(
            "UPDATE upload_tickets SET used_at = CURRENT_TIMESTAMP WHERE token_hash = ? AND used_at IS NULL",
            (digest,),
        ).rowcount
        if changed != 1:
            raise StoreError("ROLE_FORBIDDEN", "上传票据已被使用。")
        self._db.commit()
        return str(row["account_id"])

    def _upload_actor(self, token: str, submission_id: str, scope: str, ticket: str) -> tuple[str, str]:
        if ticket:
            return self._upload_ticket(submission_id, scope, ticket), ""
        principal = self._require(token, PUBLISHER_ROLES)
        return principal.id, principal.role

    def save_upload(self, token: str, submission_id: str, data: bytes, *, ticket: str = "") -> None:
        with self._lock:
            self._save_upload(token, submission_id, data, ticket=ticket)
    def _save_upload(self, token: str, submission_id: str, data: bytes, *, ticket: str = "") -> None:
        actor_id, actor_role = self._upload_actor(token, submission_id, "submit", ticket)
        row = self._submission(submission_id)
        if actor_role != "admin" and row["publisher_id"] != actor_id:
            raise StoreError("ROLE_FORBIDDEN", "不能上传别人的制品")
        if row["status"] != "uploading" or row["artifact_path"]:
            raise StoreError("VERSION_UNAVAILABLE", "这个上传地址已经用过")
        if row["byte_size"] is not None and len(data) != row["byte_size"]:
            raise StoreError("CHECKSUM", "制品大小与申报不一致")
        if len(data) > _MAX_BYTES:
            raise StoreError("CHECKSUM", "制品大小不在允许范围内")
        dest = self.blob_dir / "uploads" / f"{submission_id}.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        self._db.execute(
            "UPDATE submissions SET artifact_path = ?, artifact_sha256 = ? WHERE id = ?",
            (str(dest), hashlib.sha256(data).hexdigest(), submission_id),
        )
        self._db.commit()
    def finish_submit(
        self,
        token: str,
        submission_id: str,
        *,
        channel: str = "mcp",
        probe: Callable[[str], int | None] | None = None,
    ) -> SubmitResult:
        with self._lock:
            ready = self._finish_ready(token, submission_id)
        return self._finish_checked(ready, channel=channel, probe=probe)
    def _finish_ready(self, token: str, submission_id: str) -> dict:
        principal = self._require(token, PUBLISHER_ROLES)
        row = self._submission(submission_id)
        if principal.role != "admin" and row["publisher_id"] != principal.id:
            raise StoreError("ROLE_FORBIDDEN", "不能完成别人的提交")
        if row["status"] != "uploading":
            raise StoreError("VERSION_UNAVAILABLE", "这条例提交不在等待制品")
        if not row["artifact_path"]:
            raise StoreError("CHECKSUM", "还没有上传制品")
        self._db.execute(
            "UPDATE submissions SET status = 'validating' WHERE id = ? AND status = 'uploading'",
            (submission_id,),
        )
        self._db.commit()
        return {
            "actor": principal.id,
            "submission_id": submission_id,
            "package_id": row["package_id"],
            "version": row["version"],
            "manifest_json": row["manifest_json"],
            "archive": Path(row["artifact_path"]),
            "releases": self._releases(),
        }
    def _finish_checked(
        self,
        ready: dict,
        *,
        channel: str,
        probe: Callable[[str], int | None] | None,
    ) -> SubmitResult:
        submission_id = ready["submission_id"]
        archive: Path = ready["archive"]
        stored = json.loads(ready["manifest_json"])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                try:
                    safe_extract(archive, root)
                except (zipfile.BadZipFile, ValueError, OSError):
                    with self._lock:
                        self._finish_reject_archive(submission_id, ["CHECKSUM\tartifact\t制品不是合法压缩包"])
                    return SubmitResult(submission_id, "rejected", ["CHECKSUM"])
                (root / "manifest.json").write_text(ready["manifest_json"], encoding="utf-8")
                report = validate_path(
                    root, releases=ready["releases"], check_urls=True, probe=probe,
                )
                if report.ok:
                    with self._lock:
                        self._finish_commit(
                            ready, "pending", report, channel=channel, keep_assets=root, stored=stored,
                        )
                    return SubmitResult(submission_id, "pending", [])
            rules = report.rules()
            status = "uploading" if set(rules) <= _RETRYABLE_URLS else "rejected"
            with self._lock:
                self._finish_commit(ready, status, report, channel=channel, keep_assets=None, stored=None)
            return SubmitResult(submission_id, status, rules)
        except Exception:
            with self._lock:
                self._db.execute(
                    "UPDATE submissions SET status = 'uploading' WHERE id = ? AND status = 'validating'",
                    (submission_id,),
                )
                self._db.commit()
            raise
    def _finish_reject_archive(self, submission_id: str, lines: list[str]) -> None:
        row = self._submission(submission_id)
        if row["status"] not in {"uploading", "validating"}:
            raise StoreError("VERSION_UNAVAILABLE", "这条例提交不在等待制品")
        self._reject_upload(row, lines)
    def _finish_commit(
        self,
        ready: dict,
        status: str,
        report,
        *,
        channel: str,
        keep_assets: Path | None,
        stored: dict | None,
    ) -> None:
        row = self._submission(ready["submission_id"])
        if row["status"] not in {"uploading", "validating"}:
            raise StoreError("VERSION_UNAVAILABLE", "这条例提交不在等待制品")
        archive: Path = ready["archive"]
        if keep_assets is not None and stored is not None:
            self._keep_assets(ready["submission_id"], keep_assets, stored)
        drop = status == "rejected"
        if drop:
            archive.unlink(missing_ok=True)
        self._db.execute(
            """
            UPDATE submissions
            SET status = ?, validation_json = ?, artifact_path = ?
            WHERE id = ?
            """,
            (
                status,
                json.dumps([item.line() for item in report.findings], ensure_ascii=False),
                None if drop else str(archive),
                ready["submission_id"],
            ),
        )
        self._audit(ready["actor"], "finish_submit", ready["package_id"], ready["version"], channel)
    def prune_stale_uploads(self, *, max_age_hours: int | None = None, now: int | None = None) -> int:
        """清掉停在 `uploading` 且已过期的提交，返回清掉的条数。

        走 `begin_submit` 之后没有 `finish_submit` 的提交会一直停在
        `uploading`，其制品也一直占着 blob。这个方法按 `created_at` 淘汰。
        调用点有两个：`Store` 启动时清一次，`_begin_submit` 里顺带清一次
        （不需要定时器，也不需要额外线程）。
        """
        with self._lock:
            return self._prune_stale_uploads(max_age_hours=max_age_hours, now=now)
    def _prune_stale_uploads(self, *, max_age_hours: int | None, now: int | None) -> int:
        moment = int(time.time()) if now is None else int(now)
        ttl = _UPLOAD_TTL_HOURS if max_age_hours is None else int(max_age_hours)
        cutoff = moment - ttl * 3600
        # `created_at` 由 SQLite 的 CURRENT_TIMESTAMP 写成 UTC 文本，
        # 这里用同样的函数在 SQL 里比，避免 Python 侧时区换算出错。
        rows = self._db.execute(
            """
            SELECT id, artifact_path FROM submissions
            WHERE status IN ('uploading', 'validating')
              AND CAST(strftime('%s', created_at) AS INTEGER) < ?
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            if row["artifact_path"]:
                Path(row["artifact_path"]).unlink(missing_ok=True)
            self._db.execute(
                "UPDATE submissions SET status = 'expired', artifact_path = NULL WHERE id = ?",
                (row["id"],),
            )
        # 修订停在 begin_revise / PUT 之后也会占着一份 zip，按同一 TTL 清掉。
        revisions = self._db.execute(
            """
            SELECT submission_id, artifact_path FROM revisions
            WHERE CAST(strftime('%s', created_at) AS INTEGER) < ?
            """,
            (cutoff,),
        ).fetchall()
        for revision in revisions:
            if revision["artifact_path"]:
                Path(revision["artifact_path"]).unlink(missing_ok=True)
            self._db.execute(
                "DELETE FROM revisions WHERE submission_id = ?", (revision["submission_id"],)
            )
        self._db.execute("DELETE FROM sessions WHERE expires_at < ?", (moment,))
        self._db.execute("DELETE FROM hub_logins WHERE expires_at < ?", (moment,))
        self._db.execute("DELETE FROM upload_tickets WHERE expires_at < ? OR used_at IS NOT NULL", (moment,))
        if self._db.in_transaction:
            self._db.commit()
        return len(rows) + len(revisions)
    def list_my_submissions(self, token: str) -> list[dict]:
        with self._read():
            principal = self._require(token, PUBLISHER_ROLES)
            rows = self._db.execute(
                """
                SELECT id, package_id, version, status, review_reason
                FROM submissions WHERE publisher_id = ? ORDER BY created_at
                """,
                (principal.id,),
            ).fetchall()
            return [dict(row) for row in rows]
    def withdraw(self, token: str, submission_id: str) -> None:
        with self._lock:
            principal = self._require(token, PUBLISHER_ROLES)
            row = self._submission(submission_id)
            if principal.role != "admin" and row["publisher_id"] != principal.id:
                raise StoreError("ROLE_FORBIDDEN", "不能撤回别人的提交")
            advance(row["status"], "withdraw", SUBMISSION, "只能撤回待审核的提交")
            self._db.execute(
                "UPDATE submissions SET status = 'withdrawn' WHERE id = ?", (submission_id,)
            )
            self._db.commit()
            self._audit(principal.id, "withdraw", row["package_id"], row["version"])
    def get_submission(self, token: str, submission_id: str) -> dict:
        with self._read():
            return self._get_submission(token, submission_id)
    def _get_submission(self, token: str, submission_id: str) -> dict:
        principal = self._require(token, PUBLISHER_ROLES)
        row = self._submission(submission_id)
        if principal.role != "admin" and row["publisher_id"] != principal.id:
            raise StoreError("ROLE_FORBIDDEN", "不能查看别人的提交")
        return self._sheet(row)
    def revise(
        self,
        token: str,
        submission_id: str,
        manifest: dict,
        files: dict[str, bytes],
        *,
        channel: str = "portal",
    ) -> SubmitResult:
        with self._lock:
            return self._revise(token, submission_id, manifest, files, channel=channel)
    def _revise(
        self,
        token: str,
        submission_id: str,
        manifest: dict,
        files: dict[str, bytes],
        *,
        channel: str,
    ) -> SubmitResult:
        principal = self._require(token, PUBLISHER_ROLES)
        row = self._submission(submission_id)
        if principal.role != "admin" and row["publisher_id"] != principal.id:
            raise StoreError("ROLE_FORBIDDEN", "不能修订别人的提交")
        if row["status"] not in {"pending", "rejected"}:
            raise StoreError("VERSION_UNAVAILABLE", "只能修订待审核或已退回的提交")
        stored = self._prepare(principal, manifest, row["submit_kind"])
        old_artifact = Path(row["artifact_path"]) if row["artifact_path"] else None
        new_archive: Path | None = None
        digest: str | None = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_tree(root, files, stored)
            report = validate_path(root, releases=self._releases())
            if report.ok:
                # 先写临时文件，校验和资产都成功后才替换旧制品。
                # 旧实现先删旧 zip 再打包，打包失败会留下「库指向已删文件」。
                new_archive = self.blob_dir / "submissions" / f"{submission_id}.new.zip"
                digest = write_zip(root, new_archive)
                try:
                    self._keep_assets(submission_id, root, stored)
                except (StoreError, OSError):
                    new_archive.unlink(missing_ok=True)
                    raise
        status = "pending" if report.ok else "rejected"
        try:
            self._db.execute(
                "DELETE FROM submission_assets WHERE submission_id = ?", (submission_id,)
            )
            self._db.execute("DELETE FROM plugin_reviews WHERE submission_id = ?", (submission_id,))
            self._db.execute(
                """
                UPDATE submissions
                SET package_id = ?, version = ?, status = ?, manifest_json = ?,
                    validation_json = ?, artifact_path = ?, artifact_sha256 = ?,
                    review_reason = NULL, reviewed_by = NULL, reviewed_at = NULL
                WHERE id = ?
                """,
                (
                    str(stored.get("id") or ""),
                    str(stored.get("version") or ""),
                    status,
                    json.dumps(stored, ensure_ascii=False),
                    json.dumps([item.line() for item in report.findings], ensure_ascii=False),
                    str(self.blob_dir / "submissions" / f"{submission_id}.zip") if new_archive else None,
                    digest,
                    submission_id,
                ),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            if new_archive is not None:
                new_archive.unlink(missing_ok=True)
            raise
        if new_archive is not None:
            new_archive.replace(self.blob_dir / "submissions" / f"{submission_id}.zip")
        elif old_artifact is not None:
            # 新包不合格：库里已经不指向旧制品，再删文件；删失败只留下孤儿文件。
            old_artifact.unlink(missing_ok=True)
        self._audit(principal.id, "revise", stored.get("id"), stored.get("version"), channel)
        return SubmitResult(submission_id, status, report.rules())

    def begin_revise(
        self,
        token: str,
        submission_id: str,
        manifest: dict,
        *,
        byte_size: int,
    ) -> dict:
        """修订上传的第一段：静态校验清单，登记一条待上传的修订。"""
        with self._lock:
            publisher = self._require(token, PUBLISHER_ROLES)
            if byte_size < 0 or byte_size > _MAX_BYTES:
                raise StoreError("CHECKSUM", "制品大小不在允许范围内")
            row = self._submission(submission_id)
            if publisher.role != "admin" and row["publisher_id"] != publisher.id:
                raise StoreError("ROLE_FORBIDDEN", "不能修订别人的提交")
            if row["status"] not in {"pending", "rejected"}:
                raise StoreError("VERSION_UNAVAILABLE", "只能修订待审核或已退回的提交")
            stored = self._prepare(publisher, manifest, row["submit_kind"])
            report = validate_manifest(stored, releases=self._releases())
            if not report.ok:
                return {
                    "submission_id": submission_id,
                    "status": "rejected",
                    "rules": report.rules(),
                    "put_path": None,
                }
            self._db.execute(
                """
                INSERT INTO revisions (submission_id, manifest_json, byte_size, artifact_path)
                VALUES (?, ?, ?, NULL)
                ON CONFLICT(submission_id) DO UPDATE SET
                  manifest_json = excluded.manifest_json,
                  byte_size = excluded.byte_size,
                  artifact_path = NULL,
                  created_at = CURRENT_TIMESTAMP
                """,
                (submission_id, json.dumps(stored, ensure_ascii=False), byte_size),
            )
            self._db.commit()
            self._audit(publisher.id, "begin_revise", stored.get("id"), stored.get("version"))
            ticket = self._new_upload_ticket(submission_id, publisher.id, "revise")
            return {
                "submission_id": submission_id,
                "status": "uploading",
                "rules": [],
                "put_path": f"/v1/uploads/revise/{submission_id}?ticket={ticket}",
            }

    def save_revise_upload(self, token: str, submission_id: str, data: bytes, *, ticket: str = "") -> None:
        with self._lock:
            actor_id, actor_role = self._upload_actor(token, submission_id, "revise", ticket)
            row = self._submission(submission_id)
            if actor_role != "admin" and row["publisher_id"] != actor_id:
                raise StoreError("ROLE_FORBIDDEN", "不能上传别人的修订")
            if row["status"] not in {"pending", "rejected"}:
                raise StoreError("VERSION_UNAVAILABLE", "只能修订待审核或已退回的提交")
            revision = self._db.execute(
                "SELECT byte_size, artifact_path FROM revisions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if revision is None:
                raise StoreError("VERSION_UNAVAILABLE", "还没有开始这次修订")
            if revision["artifact_path"]:
                raise StoreError("VERSION_UNAVAILABLE", "这个修订地址已经用过")
            if revision["byte_size"] is not None and len(data) != revision["byte_size"]:
                raise StoreError("CHECKSUM", "制品大小与申报不一致")
            if len(data) > _MAX_BYTES:
                raise StoreError("CHECKSUM", "制品大小不在允许范围内")
            dest = self.blob_dir / "uploads" / f"{submission_id}.revise.zip"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            self._db.execute(
                "UPDATE revisions SET artifact_path = ? WHERE submission_id = ?",
                (str(dest), submission_id),
            )
            self._db.commit()

    def finish_revise(self, token: str, submission_id: str) -> "SubmitResult":
        """修订上传的第三段：解包、校验，然后按原单替换。"""
        with self._lock:
            principal = self._require(token, PUBLISHER_ROLES)
            row = self._submission(submission_id)
            if principal.role != "admin" and row["publisher_id"] != principal.id:
                raise StoreError("ROLE_FORBIDDEN", "不能完成别人的修订")
            if row["status"] not in {"pending", "rejected"}:
                raise StoreError("VERSION_UNAVAILABLE", "只能修订待审核或已退回的提交")
            revision = self._db.execute(
                "SELECT manifest_json, artifact_path FROM revisions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if revision is None or not revision["artifact_path"]:
                raise StoreError("CHECKSUM", "还没有上传修订制品")
            archive = Path(revision["artifact_path"])
            manifest = json.loads(revision["manifest_json"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            try:
                safe_extract(archive, root)
            except (zipfile.BadZipFile, ValueError, OSError):
                with self._lock:
                    self._reject_revision(submission_id, ["CHECKSUM\tartifact\t制品不是合法压缩包"])
                return SubmitResult(submission_id, "rejected", ["CHECKSUM"])
            files = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and path.relative_to(root).as_posix() != "manifest.json"
            }
        with self._lock:
            result = self._revise(token, submission_id, manifest, files, channel="mcp")
            self._db.execute("DELETE FROM revisions WHERE submission_id = ?", (submission_id,))
            self._db.commit()
        archive.unlink(missing_ok=True)
        return result

    def _reject_revision(self, submission_id: str, lines: list[str]) -> None:
        row = self._submission(submission_id)
        if row["status"] not in {"pending", "rejected"}:
            raise StoreError("VERSION_UNAVAILABLE", "只能退回待审核或已退回的提交")
        revision = self._db.execute(
            "SELECT artifact_path FROM revisions WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        old_artifact = Path(row["artifact_path"]) if row["artifact_path"] else None
        revision_artifact = Path(revision["artifact_path"]) if revision and revision["artifact_path"] else None
        self._db.execute("DELETE FROM submission_assets WHERE submission_id = ?", (submission_id,))
        self._db.execute("DELETE FROM revisions WHERE submission_id = ?", (submission_id,))
        self._db.execute(
            """
            UPDATE submissions
            SET status = 'rejected', validation_json = ?, artifact_path = NULL,
                artifact_sha256 = NULL, review_reason = NULL, reviewed_by = NULL, reviewed_at = NULL
            WHERE id = ?
            """,
            (json.dumps(lines, ensure_ascii=False), submission_id),
        )
        self._db.commit()
        for path in (old_artifact, revision_artifact):
            if path is not None:
                path.unlink(missing_ok=True)

    def check_manifest(self, token: str, manifest: dict) -> list[str]:
        return [item["rule"] for item in self.explain_manifest(token, manifest) if item["severity"] == "error"]
    def explain_manifest(self, token: str, manifest: dict) -> list[dict]:
        with self._read():
            self._require(token, PUBLISHER_ROLES)
            report = validate_manifest(manifest, releases=self._releases())
            return [
                {"rule": item.rule, "path": item.path, "message": item.message, "severity": item.severity}
                for item in report.findings
            ]
    def _prepare(self, publisher: Principal, manifest: dict, submit_kind: str) -> dict:
        if submit_kind not in {"new_version", "add_platform"}:
            raise StoreError("KIND", "submit_kind 只能是 new_version 或 add_platform")
        # 清单来自外部，先确认是对象，后面的 .get 才安全。
        if not isinstance(manifest, dict):
            raise StoreError("SPEC_VERSION", "清单必须是对象")
        author = manifest.get("author")
        # author 来自外部，可能是字符串或数组；先按形状挡掉，再读 publisher_id。
        if author is not None and not isinstance(author, dict):
            raise StoreError("AUTHOR_MISMATCH", "author 必须是对象")
        claimed = (author or {}).get("publisher_id")
        if claimed and claimed != publisher.id:
            raise StoreError("AUTHOR_MISMATCH", "作者身份与密钥不一致")
        stored = json.loads(json.dumps(manifest))
        if not isinstance(stored.get("author"), dict):
            stored["author"] = {}
        stored["author"]["publisher_id"] = publisher.id
        # 贡献者信息以账号为准，不接受清单里自填的用户名/姓名/部门，
        # 避免把别人的账号挂到自己提交上。公开出口仍只保留 display_name。
        account = self._db.execute(
            "SELECT username, real_name, nickname, department, job_title FROM accounts WHERE id = ?",
            (publisher.id,),
        ).fetchone()
        if account is not None:
            for key in ("username", "real_name", "nickname", "department", "job_title"):
                stored["author"][key] = account[key] or ""
            if not str(stored["author"].get("display_name") or "").strip():
                stored["author"]["display_name"] = account["nickname"] or account["real_name"] or account["username"]
        package_id = str(stored.get("id") or "")
        version = str(stored.get("version") or "")
        # 命名空间（id 第一个点之前）归首个发布者；同一 id 自然也在其中。
        # admin 也不能替别人发。全新命名空间没有 owner，直接放行。
        namespace = _namespace_of(package_id)
        namespace_owner = self._namespace_owner(namespace, persist=True)
        if namespace_owner is not None and namespace_owner != publisher.id:
            raise StoreError("ROLE_FORBIDDEN", f"命名空间 {namespace} 已经由别的发布者使用")
        if stored.get("kind") == "plugin":
            plugin = stored.get("plugin") if isinstance(stored.get("plugin"), dict) else {}
            tier = plugin.get("tier")
            if tier == "C":
                raise StoreError("PLUGIN_TIER", "Tier C（原生模块）当前不接受")
            if tier == "B" and publisher.role != "admin":
                raise StoreError("ROLE_FORBIDDEN", "Tier B 目前只接受 admin 或白名单作者")
        if submit_kind == "new_version" and self._release_exists(package_id, version):
            raise StoreError("VERSION_IMMUTABLE", "该版本已发布，不能覆盖")
        if submit_kind == "add_platform":
            current = self._visible_release(package_id, version)
            if current is None:
                raise StoreError("VERSION_UNAVAILABLE", "只能给已发布的版本追加平台")
            _check_add_platform(json.loads(current["manifest_json"]), stored)
        return stored
    def _insert_submission(
        self,
        submission_id: str,
        stored: dict,
        publisher_id: str,
        status: str,
        report,
        submit_kind: str,
        archive: Path | None,
        digest: str | None,
        byte_size: int | None = None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO submissions (
              id, package_id, version, submit_kind, status, publisher_id,
              manifest_json, validation_json, byte_size, artifact_path, artifact_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                submission_id,
                str(stored.get("id") or ""),
                str(stored.get("version") or ""),
                submit_kind,
                status,
                publisher_id,
                json.dumps(stored, ensure_ascii=False),
                json.dumps([item.line() for item in report.findings], ensure_ascii=False),
                byte_size if byte_size is not None else (archive.stat().st_size if archive else None),
                str(archive) if archive else None,
                digest,
            ),
        )
        self._db.commit()
    def _keep_assets(self, submission_id: str, root: Path, manifest: dict) -> None:
        """把 runtime 各平台的制品从临时目录搬到 blob 里。

        `platform` 会成为目录名、`path` 来自提交方，两者都必须先过路径守卫，
        否则外部输入可以把文件写到 blob 目录之外。校验器已经拦过一轮，这里
        再拦一轮，避免「校验器改了、服务侧忘了」的情况。
        先算完所有落点再写，任一条不合法就整体拒绝，不留半成品。
        """
        if manifest.get("kind") != "runtime":
            return
        plan: list[tuple[str, Path, Path]] = []
        for asset in (manifest.get("runtime") or {}).get("assets") or []:
            platform = asset.get("platform") if isinstance(asset, dict) else None
            relative = asset.get("path") if isinstance(asset, dict) else None
            if not isinstance(platform, str) or not isinstance(relative, str):
                raise StoreError("RUNTIME_ASSET", "资产缺少 platform 或 path")
            try:
                source = safe_join(root, relative)
            except PathEscape as exc:
                raise StoreError("RUNTIME_ASSET", f"资产路径不合法：{exc}") from exc
            if not source.is_file():
                raise StoreError("RUNTIME_ASSET", f"资产文件不存在：{relative}")
            try:
                dest = safe_join(self.blob_dir, "assets", submission_id, platform)
            except PathEscape as exc:
                raise StoreError("RUNTIME_ASSET", f"平台名不合法：{exc}") from exc
            plan.append((platform, source, dest))
        for platform, source, dest in plan:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
            self._db.execute(
                """
                INSERT OR REPLACE INTO submission_assets (submission_id, platform, sha256, path)
                VALUES (?, ?, ?, ?)
                """,
                (submission_id, platform, sha256_file(dest), str(dest)),
            )
    def _reject_upload(self, row: sqlite3.Row, lines: list[str]) -> None:
        path = row["artifact_path"]
        if path:
            Path(path).unlink(missing_ok=True)
        self._db.execute(
            """
            UPDATE submissions
            SET status = 'rejected', validation_json = ?, artifact_path = NULL
            WHERE id = ?
            """,
            (json.dumps(lines, ensure_ascii=False), row["id"]),
        )
        self._db.commit()
    def _backfill_owners(self) -> None:
        """把历史已发布提交的命名空间归属补进所有者表。

        旧库没有 `package_owners`，启动时按最早一次发布回填；同一命名空间只取
        第一位，后续发布者不覆盖。
        """
        rows = self._db.execute(
            "SELECT package_id, publisher_id FROM submissions WHERE status = 'published' ORDER BY reviewed_at"
        ).fetchall()
        seen: set[str] = set()
        for row in rows:
            namespace = _namespace_of(str(row["package_id"]))
            if namespace in seen:
                continue
            seen.add(namespace)
            self._db.execute(
                "INSERT OR IGNORE INTO package_owners (namespace, publisher_id) VALUES (?, ?)",
                (namespace, row["publisher_id"]),
            )
        if rows:
            self._db.commit()

    def _namespace_owner(self, namespace: str, *, persist: bool = False) -> str | None:
        """命名空间的所有者。

        先看 `package_owners`；没有就按已发布提交推导。`persist=True` 时把推导
        结果写进去（只由写路径调用，例如提交时）。
        """
        row = self._db.execute(
            "SELECT publisher_id FROM package_owners WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if row is not None:
            return str(row["publisher_id"])
        derived = self._db.execute(
            """
            SELECT publisher_id FROM submissions
            WHERE status = 'published' AND package_id LIKE ?
            ORDER BY reviewed_at
            LIMIT 1
            """,
            (namespace + ".%",),
        ).fetchone()
        if derived is None:
            return None
        if persist:
            self._db.execute(
                "INSERT OR IGNORE INTO package_owners (namespace, publisher_id) VALUES (?, ?)",
                (namespace, derived["publisher_id"]),
            )
            self._db.commit()
        return str(derived["publisher_id"])

    def _claim_namespace(self, namespace: str, publisher_id: str) -> None:
        """发布成功时登记所有者；已有所有者时什么都不做。"""
        self._db.execute(
            "INSERT OR IGNORE INTO package_owners (namespace, publisher_id) VALUES (?, ?)",
            (namespace, publisher_id),
        )

    def transfer_namespace(self, token: str, namespace: str, account_id: str) -> None:
        """admin 把命名空间转给另一个作者。保留历史提交与审计，不改写。"""
        with self._lock:
            admin = self._require(token, ADMIN_ROLES)
            namespace = str(namespace or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9]*", namespace):
                raise StoreError("ID_FORMAT", "命名空间只能用小写字母和数字，且以字母开头")
            target = self._account_row(account_id)
            if target["disabled_at"]:
                raise StoreError("ACCOUNT_DISABLED", "这个用户已停用。")
            if target["role"] not in PUBLISHER_ROLES:
                raise StoreError("ROLE_FORBIDDEN", "只能转给作者、审核或管理员。")
            owner = self._namespace_owner(namespace, persist=True)
            if owner is None:
                raise StoreError("VERSION_UNAVAILABLE", "没有这个命名空间")
            self._db.execute(
                "UPDATE package_owners SET publisher_id = ?, updated_at = CURRENT_TIMESTAMP WHERE namespace = ?",
                (target["id"], namespace),
            )
            self._db.commit()
            self._audit(admin.id, "transfer_namespace", namespace, None)

    def _diff_base_row(self, package_id: str, version: str, submit_kind: str) -> sqlite3.Row | None:
        if submit_kind == "add_platform":
            return self._visible_release(package_id, version)
        rows = self._db.execute(
            """
            SELECT version, manifest_json, artifact_path FROM releases
            WHERE package_id = ? AND version != ? AND unpublished_at IS NULL
            """,
            (package_id, version),
        ).fetchall()
        if not rows:
            return None
        return max(rows, key=cmp_to_key(lambda left, right: _cmp(left["version"], right["version"])))
    def _diff_base(self, package_id: str, version: str, submit_kind: str) -> dict | None:
        row = self._diff_base_row(package_id, version, submit_kind)
        return json.loads(row["manifest_json"]) if row is not None else None
    def _sheet(self, row: sqlite3.Row) -> dict:
        manifest = review_view(json.loads(row["manifest_json"]))
        return {
            "id": row["id"],
            "status": row["status"],
            "submit_kind": row["submit_kind"],
            "review_reason": row["review_reason"],
            "rules": json.loads(row["validation_json"]),
            "findings": _findings(row["validation_json"]),
            "manifest": manifest,
            "diff": _diff(self._diff_base(row["package_id"], row["version"], row["submit_kind"]), manifest),
        }
