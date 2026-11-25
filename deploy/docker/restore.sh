#!/bin/sh
# 恢复：把一份备份写回数据卷。
#
# 必须先停掉 qilin 服务再运行；恢复会覆盖数据库与制品目录（内部用 --force）。
# 破坏性操作，所以要显式给 QILIN_RESTORE_CONFIRM=yes。
set -eu

ROOT=${QILIN_BACKUP_ROOT:-/backups}
DB=${QILIN_DB:-/var/lib/qilin/qilin.db}
BLOBS=${QILIN_BLOBS:-/var/lib/qilin/blobs}
TMP=${QILIN_TMPDIR:-/var/lib/qilin/tmp}

log() {
  printf '%s qilin-restore: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

fail() {
  log "error: $*"
  exit 1
}

if [ "$#" -ne 1 ]; then
  fail "用法：qilin-restore <备份目录名或绝对路径>（例如 20261120T030000Z）"
fi

case "$1" in
  /*) source_dir=$1 ;;
  *)  source_dir="$ROOT/$1" ;;
esac

[ -f "$source_dir/backup.json" ] || fail "不是备份目录（缺 backup.json）：$source_dir"

if [ "${QILIN_RESTORE_CONFIRM:-}" != "yes" ]; then
  fail "恢复会覆盖 $DB 与 ${BLOBS}。先停掉服务，再带 QILIN_RESTORE_CONFIRM=yes 重跑。"
fi

mkdir -p "$TMP" 2>/dev/null || true
TMPDIR=$TMP
export TMPDIR

log "从 $source_dir 恢复到 db=$DB blobs=$BLOBS"
# restore 会先逐个核对 sha256，任何一项不符就整体拒绝。
store restore "$source_dir" --db "$DB" --blobs "$BLOBS" --force
log "完成。启动服务后确认 GET /api/health 返回 200。"
