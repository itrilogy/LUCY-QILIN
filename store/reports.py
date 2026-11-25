"""消费者问题反馈闭环（WP-4）。

针对已发布（含后续下架）版本的真实安装与使用问题，打通：
匿名/实名提交（限流 + 长度约束 + 密钥扫描拒收） → 作者按命名空间查看 → 审核/管理员分诊收敛。
审计只记动作与包版本，不记反馈正文全文。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time

from store.errors import StoreError
from store.roles import AUTHENTICATED_ROLES, PUBLISHER_ROLES, REVIEW_ROLES
from store.states import REPORT, REPORT_REJECTED, REPORT_RESOLVED, advance
from store.validator.validate import SECRET_VALUE

REPORT_CATEGORIES = frozenset({
    "install_failed",
    "artifact_missing",
    "doc_mismatch",
    "runtime_error",
    "other",
})
REPORT_STATUSES = frozenset({"open", "triaged", "resolved", "rejected"})

_MAX_SUMMARY = 2000
_MAX_STEPS = 4000
_MAX_RESOLUTION = 2000
_MAX_META = 64
_MAX_CONTACT = 128

_STATUS_EVENTS = {
    "triaged": "triage",
    "resolved": "resolve",
    "rejected": "reject",
}


class _WindowLimiter:
    """进程内轻量滑动窗口，用于消费者反馈的匿名/包维度/账号维度防刷。"""

    def __init__(self, limit: int, window: float, max_keys: int = 4096) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            if len(self._events) >= self.max_keys and key not in self._events:
                stale = [k for k, vals in self._events.items() if not vals or vals[-1] <= cutoff]
                for k in stale:
                    self._events.pop(k, None)
                if len(self._events) >= self.max_keys:
                    return False
            events = [t for t in self._events.get(key, []) if t > cutoff]
            if len(events) >= self.limit:
                self._events[key] = events
                return False
            events.append(now)
            self._events[key] = events
            return True


def _clean_text(value: object, field: str, *, required: bool, maximum: int) -> str:
    if not isinstance(value, str):
        if value is None and not required:
            return ""
        raise StoreError("REPORT_INVALID", f"{field} 必须是字符串")
    trimmed = value.strip()
    if required and not trimmed:
        raise StoreError("REPORT_INVALID", f"{field} 不能为空")
    if len(trimmed) > maximum:
        raise StoreError("REPORT_INVALID", f"{field} 超过 {maximum} 字上限")
    if trimmed and SECRET_VALUE.search(trimmed):
        raise StoreError("SECRET_PATTERN", f"{field} 包含疑似密钥内容，已拒收")
    return trimmed


class ReportMixin:
    def _ensure_report_limiters(self) -> None:
        if hasattr(self, "_report_ip_limiter"):
            return
        with self._lock:
            if not hasattr(self, "_report_ip_limiter"):
                self._report_ip_limiter = _WindowLimiter(15, 3600.0)
                self._report_pkg_limiter = _WindowLimiter(15, 600.0)
                self._report_user_limiter = _WindowLimiter(30, 3600.0)

    def report_problem(
        self,
        token: str,
        package_id: str,
        version: str,
        *,
        category: str,
        summary: str,
        steps: str = "",
        client: str = "",
        platform: str = "",
        contact: str = "",
        source_ip: str = "",
        channel: str = "mcp",
    ) -> dict:
        """对曾发布过的 id@version 提交一条问题反馈（匿名与已登录角色均可用）。"""
        self._ensure_report_limiters()
        pkg_id = _clean_text(package_id, "id", required=True, maximum=128)
        ver = _clean_text(version, "version", required=True, maximum=64)
        cat = _clean_text(category, "category", required=True, maximum=32)
        if cat not in REPORT_CATEGORIES:
            allowed = "、".join(sorted(REPORT_CATEGORIES))
            raise StoreError("REPORT_INVALID", f"category 只能取 {allowed}")
        body_summary = _clean_text(summary, "summary", required=True, maximum=_MAX_SUMMARY)
        body_steps = _clean_text(steps, "steps", required=False, maximum=_MAX_STEPS)
        body_client = _clean_text(client, "client", required=False, maximum=_MAX_META)
        body_platform = _clean_text(platform, "platform", required=False, maximum=_MAX_META)
        body_contact = _clean_text(contact, "contact", required=False, maximum=_MAX_CONTACT)

        with self._lock:
            principal = self._principal_from_token(token) if token else None
            if principal is None:
                ip_key = (source_ip or "anonymous").strip()[:64] or "anonymous"
                if not self._report_ip_limiter.allow(ip_key) or not self._report_pkg_limiter.allow(pkg_id):
                    raise StoreError("RATE_LIMITED", "反馈提交过于频繁，请稍后再试。")
            else:
                if not self._report_user_limiter.allow(principal.id):
                    raise StoreError("RATE_LIMITED", "反馈提交过于频繁，请稍后再试。")
            exists = self._db.execute(
                "SELECT 1 FROM releases WHERE package_id = ? AND version = ?",
                (pkg_id, ver),
            ).fetchone()
            if exists is None:
                raise StoreError("VERSION_UNAVAILABLE", f"没有发布过 {pkg_id}@{ver}")
            report_id = secrets.token_hex(12)
            reporter_id = principal.id if principal is not None else None
            self._db.execute(
                """
                INSERT INTO reports (
                  id, package_id, version, category, status, summary, steps,
                  client, platform, contact, reporter_id
                ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id, pkg_id, ver, cat, body_summary, body_steps,
                    body_client, body_platform, body_contact, reporter_id,
                ),
            )
            self._audit(
                reporter_id or "anonymous",
                "report.create",
                pkg_id,
                ver,
                channel,
                commit=False,
            )
            self._db.commit()
        return {
            "report_id": report_id,
            "id": pkg_id,
            "version": ver,
            "category": cat,
            "status": "open",
        }

    def get_report(self, token: str, report_id: str) -> dict:
        """读取单条反馈。按调用方身份裁剪内部字段；实名提交跨无关用户不可见。"""
        target_id = str(report_id or "").strip()
        if not target_id:
            raise StoreError("REPORT_INVALID", "缺少 report_id")
        with self._read():
            principal = self._principal_from_token(token) if token else None
            row = self._db.execute("SELECT * FROM reports WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                raise StoreError("RESOURCE_NOT_FOUND", "没有这条反馈")
            role = principal.role if principal else None
            is_reviewer = role in REVIEW_ROLES
            is_reporter = principal is not None and row["reporter_id"] == principal.id
            is_owner = False
            if principal is not None and role in PUBLISHER_ROLES:
                namespace = str(row["package_id"]).split(".", 1)[0]
                owner = self._namespace_owner(namespace, persist=False)
                is_owner = owner is not None and owner == principal.id
            # 匿名提交单（reporter_id IS NULL）允许凭 96-bit report_id 查询进度；
            # 实名提交单要求本人、该命名空间作者或审核/管理员。
            if row["reporter_id"] is not None and not (is_reviewer or is_reporter or is_owner):
                raise StoreError("ROLE_FORBIDDEN", "无权查看这条反馈")
            return self._format_report(row, full=is_reviewer, include_contact=is_reviewer or is_reporter)

    def list_my_reports(self, token: str, *, limit: int = 100, offset: int = 0) -> dict:
        """有身份者列出自己提交过的反馈与处理结论。"""
        with self._read():
            principal = self._require(token, AUTHENTICATED_ROLES)
            limit = max(1, min(int(limit), 500))
            offset = max(0, int(offset))
            total = self._db.execute(
                "SELECT COUNT(*) FROM reports WHERE reporter_id = ?", (principal.id,)
            ).fetchone()[0]
            rows = self._db.execute(
                """
                SELECT * FROM reports WHERE reporter_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                """,
                (principal.id, limit, offset),
            ).fetchall()
            return {
                "items": [self._format_report(r, full=False, include_contact=True) for r in rows],
                "total": int(total),
                "limit": limit,
                "offset": offset,
            }

    def list_product_reports(
        self,
        token: str,
        *,
        package_id: str | None = None,
        version: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """产品作者查看自己命名空间下产品的反馈列表与按状态/类别的聚合统计。"""
        with self._read():
            principal = self._require(token, PUBLISHER_ROLES)
            limit = max(1, min(int(limit), 500))
            offset = max(0, int(offset))
            is_reviewer = principal.role in REVIEW_ROLES
            clauses: list[str] = []
            params: list[object] = []
            if package_id:
                pkg = str(package_id).strip()
                namespace = pkg.split(".", 1)[0]
                owner = self._namespace_owner(namespace, persist=False)
                if not is_reviewer and owner != principal.id:
                    raise StoreError("ROLE_FORBIDDEN", "只能查看自己命名空间下产品的反馈")
                clauses.append("package_id = ?")
                params.append(pkg)
            elif not is_reviewer:
                namespaces = self._owned_namespaces(principal.id)
                if not namespaces:
                    return {
                        "items": [],
                        "total": 0,
                        "limit": limit,
                        "offset": offset,
                        "by_status": {},
                        "by_category": {},
                    }
                ns_clauses = " OR ".join("package_id LIKE ?" for _ in namespaces)
                clauses.append(f"({ns_clauses})")
                params.extend(f"{ns}.%" for ns in namespaces)
            if version:
                clauses.append("version = ?")
                params.append(str(version).strip())
            if category:
                clauses.append("category = ?")
                params.append(str(category).strip())
            if status:
                clauses.append("status = ?")
                params.append(str(status).strip())
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            total = self._db.execute("SELECT COUNT(*) FROM reports" + where, tuple(params)).fetchone()[0]
            by_status = {
                r["status"]: int(r["cnt"])
                for r in self._db.execute(
                    "SELECT status, COUNT(*) AS cnt FROM reports" + where + " GROUP BY status",
                    tuple(params),
                ).fetchall()
            }
            by_category = {
                r["category"]: int(r["cnt"])
                for r in self._db.execute(
                    "SELECT category, COUNT(*) AS cnt FROM reports" + where + " GROUP BY category",
                    tuple(params),
                ).fetchall()
            }
            rows = self._db.execute(
                "SELECT * FROM reports" + where + " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset),
            ).fetchall()
            return {
                "items": [self._format_report(r, full=is_reviewer, include_contact=is_reviewer) for r in rows],
                "total": int(total),
                "limit": limit,
                "offset": offset,
                "by_status": by_status,
                "by_category": by_category,
            }

    def list_reports(
        self,
        token: str,
        *,
        package_id: str | None = None,
        kind: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """审核/管理员查看全站问题分诊队列。"""
        with self._read():
            self._require(token, REVIEW_ROLES)
            limit = max(1, min(int(limit), 500))
            offset = max(0, int(offset))
            clauses: list[str] = []
            params: list[object] = []
            if package_id:
                clauses.append("rp.package_id = ?")
                params.append(str(package_id).strip())
            if category:
                clauses.append("rp.category = ?")
                params.append(str(category).strip())
            if status:
                clauses.append("rp.status = ?")
                params.append(str(status).strip())
            if kind:
                clauses.append(
                    "EXISTS (SELECT 1 FROM releases rel WHERE rel.package_id = rp.package_id "
                    "AND rel.version = rp.version AND json_extract(rel.manifest_json, '$.kind') = ?)"
                )
                params.append(str(kind).strip())
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            total = self._db.execute("SELECT COUNT(*) FROM reports rp" + where, tuple(params)).fetchone()[0]
            rows = self._db.execute(
                "SELECT rp.* FROM reports rp" + where + " ORDER BY rp.created_at DESC, rp.rowid DESC LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset),
            ).fetchall()
            return {
                "items": [self._format_report(r, full=True, include_contact=True) for r in rows],
                "total": int(total),
                "limit": limit,
                "offset": offset,
            }

    def triage_report(
        self,
        token: str,
        report_id: str,
        *,
        status: str,
        resolution: str = "",
        channel: str = "mcp",
    ) -> dict:
        """审核/管理员推进反馈状态（open → triaged → resolved / rejected）。"""
        target_id = str(report_id or "").strip()
        if not target_id:
            raise StoreError("REPORT_INVALID", "缺少 report_id")
        target_status = str(status or "").strip()
        event = _STATUS_EVENTS.get(target_status)
        if event is None:
            raise StoreError("REPORT_INVALID", "status 只能取 triaged、resolved 或 rejected")
        need_resolution = target_status in {REPORT_RESOLVED, REPORT_REJECTED}
        note = _clean_text(
            resolution,
            "resolution",
            required=need_resolution,
            maximum=_MAX_RESOLUTION,
        )
        with self._lock:
            reviewer = self._require(token, REVIEW_ROLES)
            row = self._db.execute("SELECT * FROM reports WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                raise StoreError("RESOURCE_NOT_FOUND", "没有这条反馈")
            next_status = advance(
                str(row["status"]),
                event,
                REPORT,
                f"不能从 {row['status']} 变更为 {target_status}",
                rule="REPORT_STATE_INVALID",
            )
            final_resolution = note if note else str(row["resolution"] or "")
            self._db.execute(
                """
                UPDATE reports
                SET status = ?, resolution = ?, triaged_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (next_status, final_resolution, reviewer.id, target_id),
            )
            self._audit(
                reviewer.id,
                "report.triage",
                row["package_id"],
                row["version"],
                channel,
                commit=False,
            )
            self._db.commit()
            updated = self._db.execute("SELECT * FROM reports WHERE id = ?", (target_id,)).fetchone()
            return self._format_report(updated, full=True, include_contact=True)

    def _owned_namespaces(self, publisher_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT namespace FROM package_owners WHERE publisher_id = ?",
            (publisher_id,),
        ).fetchall()
        found = {str(r["namespace"]) for r in rows}
        pub_rows = self._db.execute(
            "SELECT DISTINCT package_id FROM releases WHERE unpublished_at IS NULL"
        ).fetchall()
        for r in pub_rows:
            pkg = str(r["package_id"])
            ns = pkg.split(".", 1)[0]
            if ns not in found and self._namespace_owner(ns, persist=False) == publisher_id:
                found.add(ns)
        return sorted(found)

    @staticmethod
    def _format_report(row: sqlite3.Row, *, full: bool, include_contact: bool) -> dict:
        view = {
            "report_id": row["id"],
            "id": row["package_id"],
            "version": row["version"],
            "category": row["category"],
            "status": row["status"],
            "summary": row["summary"],
            "steps": row["steps"] or "",
            "client": row["client"] or "",
            "platform": row["platform"] or "",
            "resolution": row["resolution"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_contact and row["contact"]:
            view["contact"] = row["contact"]
        if full:
            view["reporter_id"] = row["reporter_id"]
            view["triaged_by"] = row["triaged_by"]
        return view
