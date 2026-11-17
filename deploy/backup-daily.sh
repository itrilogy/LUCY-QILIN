#!/bin/sh
# 每日备份 + 保留 N 天。由 cron/launchd 调用，不放进商店进程。
set -eu
ROOT=${QILIN_ROOT:-/opt/qilin}
DEST=${QILIN_BACKUP_DIR:-/var/backups/qilin}
KEEP=${QILIN_BACKUP_KEEP:-14}
mkdir -p "$DEST"
today=$(date +%F)
target="$DEST/$today"
if [ -e "$target" ]; then
  echo "backup already exists: $target"
  exit 0
fi
cd "$ROOT"
PYTHONPATH="$ROOT" python3 -m store.cli backup "$target"
find "$DEST" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP" -exec rm -rf {} +
echo "backup: $target"
