"""`store validate`。退出码 0 表示可以上传。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from store.backup import backup as run_backup, restore as run_restore
from store.diagnostics import configure
from store.http_api import serve
from store.identity import APP_ID, HubIdentity
from store.installer import run as run_install
from store.service import Store
from store.submit_client import submit_directory
from store.validator.validate import Release, validate_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="store")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="校验技能目录或 manifest.json")
    validate.add_argument("target", type=Path)
    validate.add_argument("--check-urls", action="store_true")
    validate.add_argument("--released", type=Path, help="已发布产品 JSON 数组")
    submit = sub.add_parser("submit", help="把写好清单的目录交到商店")
    submit.add_argument("directory", type=Path)
    submit.add_argument("--base", default="", help="商店地址。默认用 QILIN_STORE_URL，否则是本机 8787")
    submit.add_argument("--add-platform", action="store_true", help="给已发布的 runtime 版本追加平台")
    install = sub.add_parser("install", help="执行安装计划")
    install.add_argument("plan", type=Path)
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--work", type=Path)
    install.add_argument("--skills-root", type=Path)
    install.add_argument("--bin-dir", type=Path)
    install.add_argument("--mcp-dir", type=Path)
    install.add_argument("--plugin-staging", type=Path)
    restore = sub.add_parser("restore", help="从备份恢复数据库与制品目录")
    restore.add_argument("source", type=Path)
    restore.add_argument("--db", type=Path, default=Path("var/qilin.db"))
    restore.add_argument("--blobs", type=Path, default=Path("var/blobs"))
    restore.add_argument("--force", action="store_true", help="覆盖已存在的库与制品目录")
    backup = sub.add_parser("backup", help="备份数据库与制品目录")
    backup.add_argument("dest", type=Path)
    backup.add_argument("--db", type=Path, default=Path("var/qilin.db"))
    backup.add_argument("--blobs", type=Path, default=Path("var/blobs"))
    server = sub.add_parser("serve", help="启动栖林门户和商店接口")
    server.add_argument("--db", type=Path, default=Path("var/qilin.db"))
    server.add_argument("--blobs", type=Path, default=Path("var/blobs"))
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    server.add_argument("--hub-origin", default="", help="枢纽地址。密钥只从这里的用户管理领取")
    server.add_argument("--app-id", default="", help="枢纽里的应用 id，默认 lucy-qilin")
    server.add_argument(
        "--public-base", default="",
        help="对外地址，如 https://store.example。部署在反向代理之后时用它固定 mcp_url 与安装计划里的地址",
    )
    server.add_argument(
        "--hub-authorize", default="",
        help=(
            "枢纽授权页地址，可含 {state}、{redirect_uri}、{app_id} 占位。"
            "配置后 /auth/start 由商店签发 state；不配置则只能用枢纽自行发起的跳转，回调的 state 无法核对"
        ),
    )
    server.add_argument("--log-level", default="", help="日志级别，默认 WARNING。也可用 QILIN_LOG_LEVEL")
    server.add_argument(
        "--trust-proxy", action="store_true",
        help="反向代理部署时读取 X-Real-IP / X-Forwarded-For 做限流键；也可设 QILIN_TRUST_PROXY=1",
    )
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _validate(args)
    if args.command == "submit":
        return _submit(args)
    if args.command == "install":
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        return run_install(
            plan,
            dry_run=args.dry_run,
            work=args.work,
            skills_root=args.skills_root,
            bin_dir=args.bin_dir,
            mcp_dir=args.mcp_dir,
            plugin_staging=args.plugin_staging,
        )
    if args.command == "backup":
        manifest = run_backup(args.db, args.blobs, args.dest)
        print(json.dumps(manifest, ensure_ascii=False))
        return 0
    if args.command == "restore":
        manifest = run_restore(args.source, args.db, args.blobs, force=args.force)
        print(json.dumps(manifest, ensure_ascii=False))
        return 0
    if args.command == "serve":
        return _serve(args)
    return 2


def _serve(args: argparse.Namespace) -> int:
    level = configure(args.log_level)
    store = Store(args.db, args.blobs, identity=_identity(args), hub_secret=_hub_secret())
    server = serve(
        store, args.host, args.port,
        public_base=_public_base(args),
        hub_authorize=_hub_authorize(args),
        trust_proxy=bool(args.trust_proxy or os.environ.get("QILIN_TRUST_PROXY") == "1"),
    )
    print(f"http://{args.host}:{server.server_port}", file=sys.stderr)
    print(f"日志级别 {level}。", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def _public_base(args: argparse.Namespace) -> str:
    return (args.public_base or os.environ.get("QILIN_PUBLIC_BASE", "")).strip()


def _hub_authorize(args: argparse.Namespace) -> str:
    return (args.hub_authorize or os.environ.get("QILIN_HUB_AUTHORIZE", "")).strip()


def _hub_secret() -> str:
    return os.environ.get("QILIN_HUB_SECRET", "").strip()


def _identity(args: argparse.Namespace) -> HubIdentity | None:
    origin = (args.hub_origin or os.environ.get("QILIN_HUB_ORIGIN", "")).strip()
    app_id = (args.app_id or os.environ.get("QILIN_APP_ID", "")).strip() or APP_ID
    if not origin:
        print("未配置枢纽地址。商店不签发密钥；匿名可看货架。", file=sys.stderr)
        return None
    identity = HubIdentity(origin, app_id=app_id, cache_path=args.db.parent / "jwks-cache.json")
    if identity.warm():
        print(f"身份来自 {identity.origin}，应用 {identity.app_id}。", file=sys.stderr)
    else:
        print(f"枢纽公钥暂时取不到：{identity.origin}。匿名仍可看货架。", file=sys.stderr)
    return identity


def _submit(args: argparse.Namespace) -> int:
    base = (args.base or os.environ.get("QILIN_STORE_URL") or "http://127.0.0.1:8787").strip()
    code, payload = submit_directory(
        args.directory,
        base=base,
        key=os.environ.get("QILIN_STORE_KEY", ""),
        submit_kind="add_platform" if getattr(args, "add_platform", False) else "new_version",
    )
    print(json.dumps(payload, ensure_ascii=False))
    return code


def _validate(args: argparse.Namespace) -> int:
    releases: list[Release] = []
    if args.released:
        raw = json.loads(args.released.read_text(encoding="utf-8"))
        releases = [Release(item["id"], item["version"], item["kind"]) for item in raw]
    report = validate_path(args.target, check_urls=args.check_urls, releases=releases)
    if not report.findings:
        print("ok")
        return 0
    for finding in report.findings:
        print(finding.line())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
