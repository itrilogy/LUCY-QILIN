#!/bin/sh
# 把现有部署的数据（qilin.db + blobs/）迁进容器要挂载的数据目录。
#
# 这是「不要让当前商品丢失」的那一步：先备份、再复制、再对账。
# 在宿主上执行，不需要 Docker；复制完成后用 SQLite 只读打开两边库，
# 逐项对比账号/提交/发布的行数并跑 integrity_check，不一致就报错退出。
#
# 用法：
#   sudo ./deploy/docker/import-existing-data.sh --source ./var --target /srv/qilin/data
#   （首次会在目标目录写 10001:10001 的属主）
#
# 前置：先停掉正在跑的 store serve / 容器（WAL 里有未合并页时不能热拷）。
set -eu

SOURCE=${QILIN_SOURCE_DIR:-./var}
TARGET=${QILIN_DATA_DIR:-/srv/qilin/data}
TARGET_UID=${QILIN_UID:-10001}
TARGET_GID=${QILIN_GID:-10001}
FORCE=0
ALLOW_WAL=0

log() {
  printf '%s qilin-import: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

die() {
  log "error: $*"
  exit 1
}

usage() {
  cat >&2 <<'EOF'
用法：import-existing-data.sh [选项]
  --source DIR   现有数据目录（含 qilin.db 与 blobs/），默认 ./var
  --target DIR   容器数据目录（compose 的 QILIN_DATA_DIR），默认 /srv/qilin/data
  --uid N        目标属主，默认 10001
  --gid N        目标属组，默认 10001
  --force        目标已有数据时覆盖
  --allow-wal    源目录还有 qilin.db-wal 时仍继续（确认服务已停）
EOF
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) [ "$#" -ge 2 ] || usage; SOURCE=$2; shift 2 ;;
    --target) [ "$#" -ge 2 ] || usage; TARGET=$2; shift 2 ;;
    --uid) [ "$#" -ge 2 ] || usage; TARGET_UID=$2; shift 2 ;;
    --gid) [ "$#" -ge 2 ] || usage; TARGET_GID=$2; shift 2 ;;
    --force) FORCE=1; shift ;;
    --allow-wal) ALLOW_WAL=1; shift ;;
    -h|--help) usage ;;
    *) log "未知参数：$1"; usage ;;
  esac
done

[ -f "$SOURCE/qilin.db" ] || die "源目录里没有 qilin.db：$SOURCE/qilin.db"
[ -d "$SOURCE/blobs" ] || die "源目录里没有 blobs/：$SOURCE/blobs"

if [ -s "$SOURCE/qilin.db-wal" ] && [ "$ALLOW_WAL" -ne 1 ]; then
  die "源库还有未合并的 qilin.db-wal（服务可能仍在跑）。先停服务；确认已停可加 --allow-wal"
fi

if [ -e "$TARGET/qilin.db" ] || [ -d "$TARGET/blobs" ]; then
  if [ "$FORCE" -ne 1 ]; then
    die "目标目录已有数据：${TARGET}。确认要覆盖再加 --force（建议先备份目标）"
  fi
  log "目标已有数据，按 --force 覆盖"
fi

log "源：$SOURCE"
log "目标：$TARGET"

mkdir -p "$TARGET/blobs"
# 库与两个影子文件一起带过去，SQLite 打开时会自行恢复。
cp -p "$SOURCE/qilin.db" "$TARGET/qilin.db"
if [ -f "$SOURCE/qilin.db-wal" ]; then
  cp -p "$SOURCE/qilin.db-wal" "$TARGET/qilin.db-wal"
fi
if [ -f "$SOURCE/qilin.db-shm" ]; then
  cp -p "$SOURCE/qilin.db-shm" "$TARGET/qilin.db-shm"
fi
if [ -f "$SOURCE/jwks-cache.json" ]; then
  cp -p "$SOURCE/jwks-cache.json" "$TARGET/jwks-cache.json"
fi

# 制品目录：只带 blobs/（tmp/ 是上传校验的临时目录，不需要继承）。
if command -v rsync >/dev/null 2>&1; then
  rsync -a "$SOURCE/blobs"/ "$TARGET/blobs"/
else
  (cd "$SOURCE/blobs" && tar cf - .) | (cd "$TARGET/blobs" && tar xf -)
fi

if [ "$(id -u)" -eq 0 ]; then
  chown -R "$TARGET_UID:$TARGET_GID" "$TARGET"
  chmod 0750 "$TARGET"
  log "属主已设为 $TARGET_UID:$TARGET_GID"
else
  if [ "$(id -u)" -ne "$TARGET_UID" ]; then
    log "warn: 当前不是 root，未能 chown 到 $TARGET_UID:${TARGET_GID}；容器会以该 uid 运行，启动前请自行 chown"
  fi
fi

# 对账：两边库逐项比行数，目标库再过一次 integrity_check。
if command -v python3 >/dev/null 2>&1; then
  python3 - "$SOURCE/qilin.db" "$TARGET/qilin.db" "$SOURCE/blobs" "$TARGET/blobs" <<'PY'
import pathlib
import sqlite3
import sys

source_db, target_db, source_blobs, target_blobs = sys.argv[1:5]
TABLES = ("accounts", "submissions", "releases", "release_assets", "submission_assets", "package_owners")


def counts(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES}
    finally:
        db.close()


def files(root):
    return sorted(p.relative_to(root).as_posix() for p in pathlib.Path(root).rglob("*") if p.is_file())


before = counts(source_db)
after = counts(target_db)
print(f"{'表':<20}{'源':>8}{'目标':>8}")
bad = 0
for name in TABLES:
    mark = "" if before[name] == after[name] else "  ← 不一致"
    if mark:
        bad += 1
    print(f"{name:<20}{before[name]:>8}{after[name]:>8}{mark}")

missing = set(files(source_blobs)) - set(files(target_blobs))
if missing:
    bad += 1
    print(f"制品缺失：{len(missing)} 个，例如 {sorted(missing)[:3]}")
else:
    print(f"制品文件：{len(files(target_blobs))} 个，与源一致")

db = sqlite3.connect(f"file:{target_db}", uri=True)
try:
    check = db.execute("PRAGMA integrity_check").fetchone()[0]
    # 归一化存量物理路径为相对 blobs 根目录的逻辑键，并核对在架制品逐一可读
    subdirs = {"releases", "uploads", "submissions", "assets"}
    def to_key(raw):
        if not raw:
            return None
        parts = [p for p in str(raw).strip().replace("\\", "/").split("/") if p and p != "."]
        if not parts or ".." in parts:
            return None
        if not str(raw).startswith("/") and parts[0] in subdirs:
            return "/".join(parts)
        for i in range(len(parts) - 1, -1, -1):
            if parts[i] == "blobs" and i + 1 < len(parts) and parts[i + 1] in subdirs:
                return "/".join(parts[i + 1:])
        return None

    for table, pks, col in (
        ("releases", ("package_id", "version"), "artifact_path"),
        ("release_assets", ("package_id", "version", "platform"), "path"),
        ("submissions", ("id",), "artifact_path"),
        ("submission_assets", ("submission_id", "platform"), "path"),
        ("revisions", ("submission_id",), "artifact_path"),
    ):
        rows = db.execute(f"SELECT {', '.join((*pks, col))} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''").fetchall()
        where = " AND ".join(f"{k} = ?" for k in pks)
        for r in rows:
            k = to_key(r[-1])
            if k and k != r[-1]:
                db.execute(f"UPDATE {table} SET {col} = ? WHERE {where}", (k, *r[:-1]))
    db.commit()
    blob_root = pathlib.Path(target_blobs)
    unreadable = []
    for pkg_id, ver, art_path in db.execute(
        "SELECT package_id, version, artifact_path FROM releases WHERE unpublished_at IS NULL AND artifact_path IS NOT NULL AND artifact_path != ''"
    ).fetchall():
        key = to_key(art_path) or f"releases/{pkg_id}/{ver}.zip"
        if not (blob_root / key).is_file():
            unreadable.append(f"{pkg_id}@{ver} -> {key}")
finally:
    db.close()
print(f"目标库 integrity_check：{check}")
if check != "ok":
    bad += 1
if unreadable:
    bad += 1
    print(f"在架制品路径不可读：{len(unreadable)} 个，例如 {unreadable[:3]}")

if bad:
    print("对账失败。目标目录保持原样，可用 --force 重来；不要带着不一致的数据启动服务。")
    sys.exit(1)
print("对账通过：行数一致、制品齐全、库完整。")
PY
else
  log "warn: 没有 python3，跳过行数与 integrity_check 对账；启动后请自行确认货架数量"
fi

log "完成。下一步：docker compose up -d，然后 curl -fsS http://127.0.0.1:8787/api/health"
