"""审核、货架与安装计划。"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path
from functools import cmp_to_key

from store.errors import StoreError
from store.plan import PlanError, install_plan, resolve
from store.semver import cmp as _cmp
from store.states import RELEASE, SUBMISSION, advance
from store.validator.registry import PLUGIN_REVIEW_ITEMS
from store.views import (
    _check_add_platform,
    _compute_file_diffs,
    _diff,
    _findings,
    _has_rule,
    _public,
    _read_archive_preview,
    _read_artifact,
    public_view,
)


def _fts_query(text: str) -> str:
    """FTS5 短语查询：整体加引号，内部引号翻倍，避免被当成语法。"""
    return '"' + text.replace('"', '""') + '"'


def _like(text: str) -> str:
    """把用户输入转成 LIKE 模式，转义 % 与 _，避免把查询当成通配符。"""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class CatalogMixin:
    def review_plugin(self, token: str, submission_id: str, checklist: dict) -> dict:
        with self._lock:
            return self._review_plugin(token, submission_id, checklist)
    def _review_plugin(self, token: str, submission_id: str, checklist: dict) -> dict:
        admin = self._require(token, {"admin"})
        row = self._submission(submission_id)
        if row["status"] != "pending":
            raise StoreError("VERSION_UNAVAILABLE", "只能审核待审核的插件")
        manifest = json.loads(row["manifest_json"])
        if manifest.get("kind") != "plugin":
            raise StoreError("PLUGIN_REVIEW_REQUIRED", "只有插件需要审核清单")
        if row["publisher_id"] == admin.id:
            raise StoreError("SELF_REVIEW", "不能审核自己提交的插件")
        if not isinstance(checklist, dict):
            raise StoreError("PLUGIN_REVIEW_REQUIRED", "checklist 必须是对象")
        missing = [item for item in PLUGIN_REVIEW_ITEMS if checklist.get(item) is not True]
        if missing:
            raise StoreError("PLUGIN_REVIEW_REQUIRED", "审核清单还有未确认项：" + "、".join(missing))
        payload = {item: True for item in PLUGIN_REVIEW_ITEMS}
        self._db.execute(
            """
            INSERT INTO plugin_reviews (submission_id, reviewer_id, checklist_json, code_sha256)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(submission_id) DO UPDATE SET
              reviewer_id = excluded.reviewer_id,
              checklist_json = excluded.checklist_json,
              code_sha256 = excluded.code_sha256,
              created_at = CURRENT_TIMESTAMP
            """,
            (submission_id, admin.id, json.dumps(payload, ensure_ascii=False), row["artifact_sha256"] or ""),
        )
        self._db.commit()
        self._audit(admin.id, "review_plugin", row["package_id"], row["version"])
        return {"submission_id": submission_id, "reviewer_id": admin.id, "checklist": payload, "code_sha256": row["artifact_sha256"] or ""}

    def approve(self, token: str, submission_id: str) -> None:
        with self._lock:
            self._approve(token, submission_id)
    def _approve(self, token: str, submission_id: str) -> None:
        admin = self._require(token, {"admin"})
        row = self._submission(submission_id)
        advance(row["status"], "approve", SUBMISSION, "只有待审核的提交可以发布")
        manifest = json.loads(row["manifest_json"])
        if manifest.get("kind") == "plugin" and row["publisher_id"] == admin.id:
            raise StoreError("SELF_REVIEW", "不能批准自己提交的插件")
        if manifest.get("kind") == "plugin":
            review = self._db.execute(
                "SELECT 1 FROM plugin_reviews WHERE submission_id = ?", (submission_id,)
            ).fetchone()
            if review is None:
                raise StoreError("PLUGIN_REVIEW_REQUIRED", "发布插件前必须先记录审核清单")
        # 提交时校验一次，发布时再校验一次：两个人先后提交同一个新 id 时，
        # 先发布的那位成为 owner，后一条不能借审核动作变成第二件商品。
        namespace = row["package_id"].split(".", 1)[0]
        namespace_owner = self._namespace_owner(namespace)
        if namespace_owner is not None and namespace_owner != row["publisher_id"]:
            raise StoreError("ROLE_FORBIDDEN", f"命名空间 {namespace} 已经由别的发布者使用")
        try:
            if row["submit_kind"] == "add_platform":
                self._merge_platform(row)
            elif self._release_exists(row["package_id"], row["version"]):
                raise StoreError("VERSION_IMMUTABLE", "该版本已发布")
            else:
                self._db.execute(
                    """
                    INSERT INTO releases
                      (package_id, version, manifest_json, artifact_path, artifact_sha256)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row["package_id"], row["version"], row["manifest_json"],
                        row["artifact_path"], row["artifact_sha256"],
                    ),
                )
                self._publish_assets(row["id"], row["package_id"], row["version"])
            self._claim_namespace(namespace, row["publisher_id"])
            self._db.execute(
                """
                UPDATE submissions
                SET status = 'published', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (admin.id, submission_id),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        self._refresh_catalog_index(row["package_id"])
        self._db.commit()
        self._audit(admin.id, "approve", row["package_id"], row["version"])
    def reject(self, token: str, submission_id: str, reason: str) -> None:
        with self._lock:
            self._reject(token, submission_id, reason)
    def _reject(self, token: str, submission_id: str, reason: str) -> None:
        admin = self._require(token, {"admin"})
        if not reason.strip():
            raise StoreError("DESCRIPTION", "退回必须写原因")
        row = self._submission(submission_id)
        # 退回只接受待审核的提交。少了这一条，已发布的提交也能被改成 rejected。
        advance(row["status"], "reject", SUBMISSION, "只有待审核的提交可以退回")
        self._db.execute("DELETE FROM plugin_reviews WHERE submission_id = ?", (submission_id,))
        self._db.execute(
            """
            UPDATE submissions
            SET status = 'rejected', review_reason = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (reason.strip(), admin.id, submission_id),
        )
        self._db.commit()
        self._audit(admin.id, "reject", row["package_id"], row["version"])
    def unpublish(self, token: str, package_id: str, version: str, reason: str) -> None:
        with self._lock:
            admin = self._require(token, {"admin"})
            if not reason.strip():
                raise StoreError("DESCRIPTION", "下架必须写原因")
            row = self._db.execute(
                """
                SELECT * FROM releases
                WHERE package_id = ? AND version = ? AND unpublished_at IS NULL
                """,
                (package_id, version),
            ).fetchone()
            if row is None:
                raise StoreError("VERSION_UNAVAILABLE", "没有这个已发布版本")
            advance("published", "unpublish", RELEASE, "这个版本不能下架")
            self._db.execute(
                """
                UPDATE releases
                SET unpublished_at = CURRENT_TIMESTAMP, unpublish_reason = ?
                WHERE package_id = ? AND version = ?
                """,
                (reason.strip(), package_id, version),
            )
            self._db.commit()
            self._refresh_catalog_index(package_id)
            self._db.commit()
            self._audit(admin.id, "unpublish", package_id, version)
    def search(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        client: str | None = None,
        platform: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """兼容旧调用：只返回当前页的列表。要总数与分页信息用 `search_page`。"""
        return self.search_page(
            query, kind=kind, client=client, platform=platform, limit=limit, offset=offset,
        )["items"]

    def search_page(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        client: str | None = None,
        platform: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        sort: str = "updated",
    ) -> dict:
        """分页的货架查询，走 `catalog_index`，不再解析全量清单。"""
        with self._read():
            start = max(0, int(offset))
            result = self._search(
                query, kind=kind, client=client, platform=platform, limit=limit, offset=start, sort=sort,
            )
            return {
                "items": result["items"],
                "total": result["total"],
                "limit": limit,
                "offset": start,
            }

    def _search(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        client: str | None = None,
        platform: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        sort: str = "updated",
    ) -> dict:
        clauses = ["1=1"]
        params: list[object] = []
        if kind is None:
            clauses.append("kind != 'runtime'")
        elif kind:
            clauses.append("kind = ?")
            params.append(kind)
        if client:
            clauses.append("clients LIKE ?")
            params.append(f"%,{client},%")
        if platform:
            clauses.append("(platforms LIKE ? OR platforms LIKE '%,any,%')")
            params.append(f"%,{platform},%")
        needle = query.strip()
        if needle:
            pattern = _like(needle.lower())
            if self._fts_ready and len(needle) >= 3:
                # trigram 最少 3 个字符；id 仍走 LIKE，正文走 FTS。
                clauses.append(
                    "(lower(package_id) LIKE ? ESCAPE '\\'"
                    " OR package_id IN (SELECT package_id FROM catalog_fts WHERE catalog_fts MATCH ?))"
                )
                params.extend([pattern, _fts_query(needle)])
            else:
                clauses.append(
                    "(lower(package_id) LIKE ? ESCAPE '\\' OR lower(title) LIKE ? ESCAPE '\\'"
                    " OR lower(summary) LIKE ? ESCAPE '\\' OR lower(description) LIKE ? ESCAPE '\\')"
                )
                params.extend([pattern] * 4)
        where = " AND ".join(clauses)
        total = self._db.execute(
            "SELECT COUNT(*) FROM catalog_index WHERE " + where, tuple(params)
        ).fetchone()[0]
        size = -1 if limit is None else max(0, int(limit))
        order = {
            "updated": "updated_at DESC, package_id ASC",
            "title": "title COLLATE NOCASE ASC, package_id ASC",
            "id": "package_id ASC",
        }.get(sort, "updated_at DESC, package_id ASC")
        rows = self._db.execute(
            "SELECT package_id, version, manifest_json FROM catalog_index WHERE "
            + where
            + " ORDER BY "
            + order
            + " LIMIT ? OFFSET ?",
            tuple(params) + (size, max(0, int(offset))),
        ).fetchall()
        return {"items": [_public(row) for row in rows], "total": int(total)}

    def _rebuild_catalog_index(self) -> None:
        """启动时按已发布版本重建索引。数据量小，正确性优先。"""
        self._db.execute("DELETE FROM catalog_index")
        if self._fts_ready:
            self._db.execute("DELETE FROM catalog_fts")
        rows = self._db.execute(
            "SELECT DISTINCT package_id FROM releases WHERE unpublished_at IS NULL"
        ).fetchall()
        for row in rows:
            self._refresh_catalog_index(str(row[0]))
        self._db.commit()

    def _refresh_catalog_index(self, package_id: str) -> None:
        """把某个包的最新可见版本写进索引；没有可见版本就删掉。"""
        rows = self._db.execute(
            "SELECT version, manifest_json FROM releases WHERE package_id = ? AND unpublished_at IS NULL",
            (package_id,),
        ).fetchall()
        if not rows:
            self._db.execute("DELETE FROM catalog_index WHERE package_id = ?", (package_id,))
            if self._fts_ready:
                self._db.execute("DELETE FROM catalog_fts WHERE package_id = ?", (package_id,))
            return
        best = max(rows, key=cmp_to_key(lambda left, right: _cmp(left["version"], right["version"])))
        manifest = json.loads(best["manifest_json"])

        def _csv(values: object) -> str:
            items = values if isinstance(values, list) else []
            return "," + ",".join(str(item) for item in items) + "," if items else ","

        self._db.execute(
            """
            INSERT INTO catalog_index
              (package_id, version, kind, title, summary, description, clients, platforms, manifest_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(package_id) DO UPDATE SET
              version = excluded.version,
              kind = excluded.kind,
              title = excluded.title,
              summary = excluded.summary,
              description = excluded.description,
              clients = excluded.clients,
              platforms = excluded.platforms,
              manifest_json = excluded.manifest_json,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                package_id,
                best["version"],
                str(manifest.get("kind") or ""),
                str(manifest.get("title") or ""),
                str(manifest.get("summary") or ""),
                str(manifest.get("description") or ""),
                _csv(manifest.get("clients")),
                _csv(manifest.get("platforms")),
                best["manifest_json"],
            ),
        )
        if self._fts_ready:
            self._db.execute("DELETE FROM catalog_fts WHERE package_id = ?", (package_id,))
            self._db.execute(
                "INSERT INTO catalog_fts (package_id, title, summary, description) VALUES (?, ?, ?, ?)",
                (
                    package_id,
                    str(manifest.get("title") or ""),
                    str(manifest.get("summary") or ""),
                    str(manifest.get("description") or ""),
                ),
            )

    def list_reviews(self, token: str) -> list[dict]:
        with self._read():
            return self._list_reviews(token)
    def _list_reviews(self, token: str) -> list[dict]:
        self._require(token, {"admin"})
        rows = self._db.execute(
            """
            SELECT id, package_id, version, status, submit_kind, manifest_json
            FROM submissions WHERE status = 'pending'
            """
        ).fetchall()
        found = []
        for row in rows:
            manifest = json.loads(row["manifest_json"])
            found.append({
                "id": row["id"],
                "package_id": row["package_id"],
                "version": row["version"],
                "status": row["status"],
                "submit_kind": row["submit_kind"],
                "title": manifest.get("title") or "",
            })
        return found
    def list_audit(
        self,
        token: str,
        *,
        limit: int = 100,
        offset: int = 0,
        package_id: str | None = None,
        action: str | None = None,
    ) -> dict:
        """审计记录。只给 admin；按 id 倒序，支持按包与动作过滤。

        审计一直是只写不读；这里给运维一个出口，不新增写路径。
        """
        with self._read():
            self._require(token, {"admin"})
            limit = max(1, min(int(limit), 500))
            offset = max(0, int(offset))
            clauses: list[str] = []
            params: list[object] = []
            if package_id:
                clauses.append("a.package_id = ?")
                params.append(package_id)
            if action:
                clauses.append("a.action = ?")
                params.append(action)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            total = self._db.execute(
                "SELECT COUNT(*) FROM audit a" + where, tuple(params)
            ).fetchone()[0]
            rows = self._db.execute(
                """
                SELECT a.id, a.actor,
                       COALESCE(acc.nickname, acc.display_name, p.name, a.actor) AS actor_name,
                       a.action, a.package_id, a.version, a.channel, a.at
                FROM audit a
                LEFT JOIN accounts acc ON acc.id = a.actor
                LEFT JOIN principals p ON p.id = a.actor
                """
                + where
                + " ORDER BY a.id DESC LIMIT ? OFFSET ?",
                tuple(params) + (limit, offset),
            ).fetchall()
            return {
                "items": [dict(row) for row in rows],
                "total": int(total),
                "limit": limit,
                "offset": offset,
            }

    def audit_csv(
        self,
        token: str,
        *,
        package_id: str | None = None,
        action: str | None = None,
    ) -> str:
        """审计导出。只给 admin；最多一万条，避免一次拉爆。"""
        with self._read():
            self._require(token, {"admin"})
            clauses: list[str] = []
            params: list[object] = []
            if package_id:
                clauses.append("a.package_id = ?")
                params.append(package_id)
            if action:
                clauses.append("a.action = ?")
                params.append(action)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = self._db.execute(
                """
                SELECT a.id, a.actor,
                       COALESCE(acc.nickname, acc.display_name, p.name, a.actor) AS actor_name,
                       a.action, a.package_id, a.version, a.channel, a.at
                FROM audit a
                LEFT JOIN accounts acc ON acc.id = a.actor
                LEFT JOIN principals p ON p.id = a.actor
                """
                + where
                + " ORDER BY a.id DESC LIMIT 10000",
                tuple(params),
            ).fetchall()
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["id", "actor", "actor_name", "action", "package_id", "version", "channel", "at"])
        for row in rows:
            values = [row[key] for key in ("id", "actor", "actor_name", "action", "package_id", "version", "channel", "at")]
            # CSV 公式注入：以 = + - @ 开头的单元格前面加单引号。
            writer.writerow([
                ("'" + str(value)) if isinstance(value, str) and value[:1] in "=+-@" else value
                for value in values
            ])
        return buffer.getvalue()

    def contributions(
        self,
        token: str,
        *,
        account_id: str | None = None,
        limit: int = 100,
    ) -> dict:
        """发布贡献排行/明细。只给 admin；以已发布版本为准。"""
        with self._read():
            self._require(token, {"admin"})
            limit = max(1, min(int(limit), 500))
            accounts = {
                row["id"]: dict(row)
                for row in self._db.execute(
                    "SELECT id, username, real_name, nickname, department, job_title FROM accounts"
                ).fetchall()
            }
            rows = self._db.execute(
                """
                SELECT package_id, version, manifest_json, published_at
                FROM releases WHERE unpublished_at IS NULL
                ORDER BY published_at DESC, package_id ASC
                """
            ).fetchall()
            grouped: dict[str, dict] = {}
            for row in rows:
                manifest = json.loads(row["manifest_json"])
                author = manifest.get("author") if isinstance(manifest.get("author"), dict) else {}
                publisher_id = str(author.get("publisher_id") or "")
                if not publisher_id:
                    continue
                account = accounts.get(publisher_id, {})
                entry = grouped.setdefault(publisher_id, {
                    "account_id": publisher_id,
                    "username": account.get("username") or author.get("username") or "",
                    "real_name": account.get("real_name") or author.get("real_name") or "",
                    "nickname": account.get("nickname") or author.get("nickname") or "",
                    "department": account.get("department") or author.get("department") or "",
                    "job_title": account.get("job_title") or author.get("job_title") or "",
                    "products": set(),
                    "versions": 0,
                    "kinds": {},
                    "last_at": "",
                    "items": [],
                })
                entry["products"].add(row["package_id"])
                entry["versions"] += 1
                kind = str(manifest.get("kind") or "")
                entry["kinds"][kind] = entry["kinds"].get(kind, 0) + 1
                entry["last_at"] = max(entry["last_at"], str(row["published_at"] or ""))
                entry["items"].append({
                    "id": row["package_id"],
                    "version": row["version"],
                    "kind": kind,
                    "title": manifest.get("title") or row["package_id"],
                    "published_at": row["published_at"],
                })
            ranking = []
            for entry in grouped.values():
                items = sorted(entry.pop("items"), key=lambda item: (item["published_at"], item["id"]), reverse=True)
                entry["product_count"] = len(entry.pop("products"))
                entry["items"] = items
                ranking.append(entry)
            ranking.sort(key=lambda item: (-item["product_count"], -item["versions"], item["account_id"]))
            if account_id:
                selected = next((item for item in ranking if item["account_id"] == account_id), None)
                details = selected["items"] if selected else []
                return {
                    "contributor": {key: value for key, value in (selected or {}).items() if key != "items"},
                    "items": details[:limit],
                    "total": len(details),
                }
            for item in ranking:
                item.pop("items", None)
            return {"items": ranking[:limit], "total": len(ranking)}

    def get_review(self, token: str, submission_id: str) -> dict:
        with self._read():
            self._require(token, {"admin"})
            row = self._submission(submission_id)
            names: list[str] = []
            current_texts: dict[str, str] = {}
            if row["artifact_path"] and Path(row["artifact_path"]).is_file():
                names, current_texts = _read_archive_preview(Path(row["artifact_path"]))
            base_texts: dict[str, str] = {}
            base_row = self._diff_base_row(row["package_id"], row["version"], row["submit_kind"])
            if base_row is not None and base_row["artifact_path"] and Path(base_row["artifact_path"]).is_file():
                _, base_texts = _read_archive_preview(Path(base_row["artifact_path"]))
            sheet = self._sheet(row)
            review = self._db.execute(
                "SELECT checklist_json, reviewer_id, created_at FROM plugin_reviews WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if review is not None:
                sheet["plugin_review"] = {
                    "checklist": json.loads(review["checklist_json"]),
                    "reviewer_id": review["reviewer_id"],
                    "created_at": review["created_at"],
                }
            sheet["files"] = names
            sheet["files_preview"] = current_texts
            sheet["file_diffs"] = _compute_file_diffs(
                base_texts,
                current_texts,
                base_version=base_row["version"] if base_row is not None else None,
                current_version=row["version"],
            )
            return sheet
    def list_versions(self, package_id: str, *, client: str | None = None, platform: str | None = None) -> list[dict]:
        with self._read():
            found = []
            for item in self._plan_releases():
                if item["id"] != package_id:
                    continue
                if client and client not in item["clients"]:
                    continue
                if platform and "any" not in item["platforms"] and platform not in item["platforms"]:
                    continue
                found.append({
                    "version": item["version"],
                    "changes": item["changes"],
                    "platforms": item["platforms"],
                    "clients": item["clients"],
                    "published_at": item["published_at"],
                })
            return sorted(found, key=cmp_to_key(lambda left, right: _cmp(left["version"], right["version"])))
    def describe(self, package_id: str, version: str) -> dict:
        """已发布版本的详情。对外走白名单视图，不返回 publisher_id。"""
        with self._read():
            if not str(version or "").strip():
                raise StoreError("VERSION_REQUIRED", "describe 必须传入精确版本")
            row = self._visible_release(package_id, version)
            if row is None:
                raise StoreError("VERSION_UNAVAILABLE", "没有这个已发布版本")
            return public_view(json.loads(row["manifest_json"]))
    def resolve_package(
        self,
        package_id: str,
        *,
        client: str,
        platform: str,
        version: str | None = None,
        allow_prerelease: bool = False,
    ) -> dict:
        with self._read():
            try:
                return resolve(
                    self._plan_releases(),
                    package_id=package_id,
                    client=client,
                    platform=platform,
                    version=version,
                    allow_prerelease=allow_prerelease,
                )
            except PlanError as exc:
                raise StoreError(exc.rule, exc.message) from exc
    def make_install_plan(
        self, package_id: str, version: str, *, client: str, platform: str, base_url: str,
    ) -> dict:
        with self._read():
            try:
                return install_plan(
                    self._plan_releases(),
                    package_id=package_id,
                    version=version,
                    client=client,
                    platform=platform,
                    base_url=base_url,
                )
            except PlanError as exc:
                raise StoreError(exc.rule, exc.message) from exc
    def artifact(self, package_id: str, version: str, platform: str | None = None) -> tuple[bytes, str]:
        with self._read():
            row = self._visible_release(package_id, version)
            if row is None:
                raise StoreError("VERSION_UNAVAILABLE", "没有这个已发布版本")
            manifest = json.loads(row["manifest_json"])
            if manifest.get("kind") in {"skill", "mcp+skill", "knowledge", "plugin"}:
                return _read_artifact(row["artifact_path"]), row["artifact_sha256"]
            if manifest.get("kind") != "runtime":
                raise StoreError("VERSION_UNAVAILABLE", "这个产品没有可下载的制品")
            if not platform:
                raise StoreError("PLATFORM_MISMATCH", "下载 runtime 必须指定平台")
            asset = self._db.execute(
                """
                SELECT path, sha256 FROM release_assets
                WHERE package_id = ? AND version = ? AND platform = ?
                """,
                (package_id, version, platform),
            ).fetchone()
            if asset is None:
                raise StoreError("PLATFORM_MISMATCH", f"没有平台 {platform} 的制品")
            return _read_artifact(asset["path"]), asset["sha256"]
    def _publish_assets(self, submission_id: str, package_id: str, version: str) -> None:
        rows = self._db.execute(
            "SELECT platform, sha256, path FROM submission_assets WHERE submission_id = ?",
            (submission_id,),
        ).fetchall()
        for row in rows:
            self._db.execute(
                """
                INSERT OR REPLACE INTO release_assets
                  (package_id, version, platform, sha256, path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (package_id, version, row["platform"], row["sha256"], row["path"]),
            )
    def _merge_platform(self, row: sqlite3.Row) -> None:
        current = self._visible_release(row["package_id"], row["version"])
        if current is None:
            raise StoreError("VERSION_UNAVAILABLE", "只能给已发布的版本追加平台")
        published = json.loads(current["manifest_json"])
        incoming = json.loads(row["manifest_json"])
        _check_add_platform(published, incoming)
        platforms = list(published.get("platforms") or [])
        for platform in incoming.get("platforms") or []:
            if platform in platforms:
                raise StoreError("PLATFORM_TAKEN", f"平台 {platform} 已经存在")
            platforms.append(platform)
        published["platforms"] = platforms
        if published.get("kind") == "runtime":
            assets = list((published.get("runtime") or {}).get("assets") or [])
            assets.extend((incoming.get("runtime") or {}).get("assets") or [])
            published.setdefault("runtime", {})["assets"] = assets
        self._db.execute(
            "UPDATE releases SET manifest_json = ? WHERE package_id = ? AND version = ?",
            (json.dumps(published, ensure_ascii=False), row["package_id"], row["version"]),
        )
        self._publish_assets(row["id"], row["package_id"], row["version"])
    def _plan_releases(self) -> list[dict]:
        rows = self._db.execute(
            """
            SELECT package_id, version, manifest_json, artifact_sha256, published_at
            FROM releases WHERE unpublished_at IS NULL
            """
        ).fetchall()
        found = []
        for row in rows:
            manifest = json.loads(row["manifest_json"])
            found.append({
                "id": row["package_id"],
                "version": row["version"],
                "clients": manifest.get("clients") or [],
                "platforms": manifest.get("platforms") or [],
                "changes": manifest.get("changes") or "",
                "published_at": row["published_at"],
                "manifest": manifest,
                "artifact_sha256": row["artifact_sha256"],
            })
        return found
    def _visible_release(self, package_id: str, version: str) -> sqlite3.Row | None:
        return self._db.execute(
            """
            SELECT * FROM releases
            WHERE package_id = ? AND version = ? AND unpublished_at IS NULL
            """,
            (package_id, version),
        ).fetchone()
    def _release_exists(self, package_id: str, version: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM releases WHERE package_id = ? AND version = ?",
            (package_id, version),
        ).fetchone()
        return row is not None
    def _published(self, package_id: str, version: str) -> bool:
        row = self._db.execute(
            """
            SELECT 1 FROM releases
            WHERE package_id = ? AND version = ? AND unpublished_at IS NULL
            """,
            (package_id, version),
        ).fetchone()
        return row is not None
