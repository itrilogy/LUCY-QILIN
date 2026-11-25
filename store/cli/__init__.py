"""`store validate`。退出码 0 表示可以上传。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path

from store.backup import backup as run_backup, restore as run_restore
from store.diagnostics import configure
from store.errors import StoreError
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
    restore.add_argument("--db", type=Path, default=_env_path("QILIN_DB", "var/qilin.db"))
    restore.add_argument("--blobs", type=Path, default=_env_path("QILIN_BLOBS", "var/blobs"))
    restore.add_argument("--force", action="store_true", help="覆盖已存在的库与制品目录")
    backup = sub.add_parser("backup", help="备份数据库与制品目录")
    backup.add_argument("dest", type=Path)
    backup.add_argument("--db", type=Path, default=_env_path("QILIN_DB", "var/qilin.db"))
    backup.add_argument("--blobs", type=Path, default=_env_path("QILIN_BLOBS", "var/blobs"))
    init_admin = sub.add_parser("init-admin", help="空库时创建第一个管理员")
    init_admin.add_argument("--username", default="admin")
    init_admin.add_argument("--real-name", default="管理员")
    init_admin.add_argument("--nickname", default="管理员")
    init_admin.add_argument("--department", default="")
    init_admin.add_argument("--job-title", default="")
    init_admin.add_argument("--password", default="", help="不给就随机生成并只打印一次")
    init_admin.add_argument("--db", type=Path, default=_env_path("QILIN_DB", "var/qilin.db"))
    init_admin.add_argument("--blobs", type=Path, default=_env_path("QILIN_BLOBS", "var/blobs"))
    server = sub.add_parser("serve", help="启动栖林门户和商店接口")
    server.add_argument("--db", type=Path, default=_env_path("QILIN_DB", "var/qilin.db"))
    server.add_argument("--blobs", type=Path, default=_env_path("QILIN_BLOBS", "var/blobs"))
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
    server.add_argument(
        "--trusted-proxies", default="",
        help="可信反代 IP 或 CIDR 列表（逗号分隔），仅来自其中的连接才信任转发头；也可设 QILIN_TRUSTED_PROXIES",
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
    if args.command == "init-admin":
        return _init_admin(args)
    if args.command == "backup":
        manifest = run_backup(args.db, args.blobs, args.dest)
        print(json.dumps(manifest, ensure_ascii=False))
        return 0
    if args.command == "restore":
        # 恢复会整库替换：先确认没有 serve 持有这把锁。容器与宿主进程共用内核，
        # flock 跨容器同样生效，能挡住"服务还在跑就 restore"这种最容易丢数据的操作。
        try:
            restore_lock = _single_instance_lock(args.db)
        except SystemExit:
            print("服务还在运行（锁被占用）。恢复前请先停掉 store serve。", file=sys.stderr)
            return 1
        try:
            manifest = run_restore(args.source, args.db, args.blobs, force=args.force)
        finally:
            if restore_lock is not None:
                restore_lock.close()
        print(json.dumps(manifest, ensure_ascii=False))
        return 0
    if args.command == "serve":
        return _serve(args)
    return 2


def _env_path(name: str, default: str) -> Path:
    """参数默认值：先读环境变量，再退回默认。

    容器镜像里 `QILIN_DB=/var/lib/qilin/qilin.db`，入口脚本本来就把它翻成
    `--db`；CLI 自己也读一遍，是为了让 `docker compose exec qilin store backup`
    这类直接调用的命令不会悄悄落到工作目录下的 `var/`。
    """
    value = (os.environ.get(name) or "").strip()
    return Path(value) if value else Path(default)


def _single_instance_lock(db_path: Path):
    """在数据库旁拿一把排它锁，拿不到就拒绝启动。

    商店的写锁、限流桶、KEY 缓存、启动时的索引重建都是**进程内**状态，
    两个 `store serve` 指向同一个库会互相踩（迁移、validating 恢复、货架抖动）。
    文档里一直写着「单进程、单副本」，但代码此前没有任何断言。
    """
    if str(db_path) == ":memory:":
        return None
    try:
        import fcntl
    except ImportError:  # Windows 没有 flock，退回不加锁
        return None
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{db_path}.lock")
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"无法创建锁文件 {lock_path}：{exc}") from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit(
            f"另一个 store serve 正在使用 {db_path}。这是单进程、单副本服务，"
            f"不要同时起第二个实例（锁文件 {lock_path}）。"
        ) from None
    return handle


def _init_admin(args: argparse.Namespace) -> int:
    """空库引导：建第一个管理员，口令只打印一次。"""
    from store.accounts import issue_password, password_of

    username = args.username.strip().lower()
    try:
        password = password_of(args.password or issue_password(username), username)
    except StoreError as exc:
        print(exc.message, file=sys.stderr)
        return 1
    try:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        store = Store(args.db, args.blobs)
    except (StoreError, OSError) as exc:
        print(f"没能打开数据库：{exc}", file=sys.stderr)
        return 1
    try:
        profile, password, _key = store.create_admin_account(
            username, password,
            args.real_name.strip(), args.nickname.strip(),
            args.department.strip(), args.job_title.strip(),
        )
    except StoreError as exc:
        print(f"没能创建管理员：{exc.message}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps({
        "id": profile["id"],
        "username": profile["username"],
        "nickname": profile["nickname"],
        "role": profile["role"],
        "password": password,
        "db": str(Path(args.db).resolve()),
        "next": "用这个口令在门户登录，然后到「连接」取商店 KEY；建议先改口令。",
    }, ensure_ascii=False))
    return 0


def _serve(args: argparse.Namespace) -> int:
    """前台跑服务，直到进程收到 SIGTERM / SIGINT。

    容器编排（docker stop、k8s pod 删除）发的是 SIGTERM。Python 默认行为是立刻终止，
    正在处理的请求会被截断，上传到一半的制品留在卷里。这里改成「停止接受新连接 →
    等在途请求收尾 → 关库」，与 systemd、compose 的 stop_grace_period 对齐。
    """
    level = configure(args.log_level)
    lock = _single_instance_lock(args.db)
    store = Store(args.db, args.blobs, identity=_identity(args), hub_secret=_hub_secret())
    server = serve(
        store, args.host, args.port,
        public_base=_public_base(args),
        hub_authorize=_hub_authorize(args),
        trust_proxy=bool(args.trust_proxy or os.environ.get("QILIN_TRUST_PROXY") == "1"),
        trusted_proxies=args.trusted_proxies or None,
    )
    print(f"http://{args.host}:{server.server_port}", file=sys.stderr)
    print(f"日志级别 {level}。", file=sys.stderr)
    # 把实际用的库与制品目录打出来：命令行不读 QILIN_DB/QILIN_BLOBS，
    # 只设了环境变量却直接 `store serve` 时会静默落到 var/ 的默认路径上。
    print(f"数据库 {Path(args.db).resolve()}", file=sys.stderr)
    print(f"制品 {Path(args.blobs).resolve()}", file=sys.stderr)

    stopping = threading.Event()
    previous: dict[int, object] = {}

    def _graceful(signum: int, _frame: object) -> None:
        # 第二次信号直接放手：运维不想等的时候，别把进程卡住。
        if stopping.is_set():
            print(f"收到第二次信号 {signum}，立即退出。", file=sys.stderr)
            os._exit(1)
        stopping.set()
        print(f"收到信号 {signum}，停止接收新连接，等待在途请求…", file=sys.stderr)
        # shutdown() 必须在 serve_forever() 之外的线程调用，否则会自锁。
        threading.Thread(target=server.shutdown, name="qilin-shutdown", daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _graceful)
        except (ValueError, OSError):
            # 非主线程或被嵌入时没有信号能力，退回默认行为。
            previous.pop(sig, None)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.drain()
        server.server_close()
        store.close()
        if lock is not None:
            lock.close()
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)  # type: ignore[arg-type]
            except (ValueError, OSError):
                pass
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
