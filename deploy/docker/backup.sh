#!/bin/sh
# 一次性备份：由宿主 cron / systemd timer 或 `docker compose run --rm backup` 调用。
#
# 产出：$QILIN_BACKUP_ROOT/<UTC 时间戳>/{qilin.db, blobs/, backup.json}
# 保留：最近 QILIN_BACKUP_KEEP 份（默认 14），更旧的整目录删除。
#
# 为什么不在应用进程里起定时器：备份要能被外部调度、能被单独重试，
# 也要能在进程挂掉时照常跑。
set -eu

ROOT=${QILIN_BACKUP_ROOT:-/backups}
KEEP=${QILIN_BACKUP_KEEP:-14}
DB=${QILIN_DB:-/var/lib/qilin/qilin.db}
BLOBS=${QILIN_BLOBS:-/var/lib/qilin/blobs}
TMP=${QILIN_TMPDIR:-/var/lib/qilin/tmp}

log() {
  printf '%s qilin-backup: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

fail() {
  log "error: $*"
  exit 1
}

case "$KEEP" in
  ''|*[!0-9]*) fail "QILIN_BACKUP_KEEP 必须是整数，当前为：$KEEP" ;;
esac
[ "$KEEP" -ge 1 ] || fail "QILIN_BACKUP_KEEP 至少为 1"

[ -f "$DB" ] || fail "数据库不存在：${DB}（先启动过一次 serve 再备份）"
mkdir -p "$ROOT" 2>/dev/null || fail "备份根目录不可写：$ROOT"
[ -w "$ROOT" ] || fail "备份根目录不可写：$ROOT"

mkdir -p "$TMP" 2>/dev/null || true
TMPDIR=$TMP
export TMPDIR

stamp=$(date -u '+%Y%m%dT%H%M%SZ')
target="$ROOT/$stamp"
[ ! -e "$target" ] || fail "同名备份已存在：$target"

log "备份到 $target"
# store backup 自己会写 backup.json（含每个文件的 sha256），并把目标目录占住。
store backup "$target" --db "$DB" --blobs "$BLOBS" >/dev/null
[ -f "$target/backup.json" ] || fail "备份没有产出 backup.json：$target"
log "完成：$(du -sh "$target" 2>/dev/null | cut -f1 || echo '?')"

# 只清本脚本产出形态的目录：$ROOT 下的一级目录，按修改时间倒序保留前 KEEP 个。
# 不删普通文件、不删软链，避免误伤运维放进去的其它东西。
# 需要按 mtime 倒序；find 的 -printf 不是 POSIX，这里用 ls 并在上面注明。
# shellcheck disable=SC2012
ls -1dt "$ROOT"/*/ 2>/dev/null | tail -n "+$((KEEP + 1))" | while IFS= read -r old; do
  case "$old" in
    "$ROOT"/*/)
      rm -rf -- "$old" && log "清理旧备份：$old"
      ;;
  esac
done

exit 0
