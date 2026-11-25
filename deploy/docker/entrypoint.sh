#!/bin/sh
# 栖林商店容器入口。
#
# 职责只有三件，都不改动应用本身：
#   1. 支持 Docker secret：QILIN_xxx_FILE 指向挂载文件时读出内容放进 QILIN_xxx；
#   2. 把环境变量翻译成 `store serve` 的命令行参数（应用只认参数，不认容器约定）；
#   3. 起服前做一次数据目录自检，把「卷属主不对」变成一句能看懂的错误，而不是 SQLite 的
#      "unable to open database file"。
#
# 用法：
#   docker run ... qilin              → serve
#   docker run ... qilin backup /b/x  → store backup /b/x
#   docker run ... qilin sh           → 进容器排查
set -eu

log() {
  printf '%s qilin-entrypoint: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

die() {
  log "error: $*"
  exit 78
}

# 把 <NAME>_FILE 的内容读进 <NAME>。用于 compose secrets / k8s secret 挂载文件。
# 空文件按错误处理：密钥为空时应用会静默降级成「不接枢纽」，那是更难查的故障。
load_secret() {
  name="$1"
  eval "file=\${${name}_FILE:-}"
  [ -n "$file" ] || return 0
  [ -r "$file" ] || die "密钥文件不可读：$file"
  value=$(cat "$file")
  [ -n "$value" ] || die "密钥文件为空：$file"
  export "$name=$value"
  log "已从 $file 载入 $name"
}

load_secret QILIN_HUB_SECRET

QILIN_HOST=${QILIN_HOST:-0.0.0.0}
QILIN_PORT=${QILIN_PORT:-8787}
QILIN_DB=${QILIN_DB:-/var/lib/qilin/qilin.db}
QILIN_BLOBS=${QILIN_BLOBS:-/var/lib/qilin/blobs}
QILIN_TMPDIR=${QILIN_TMPDIR:-/var/lib/qilin/tmp}
export QILIN_DB QILIN_BLOBS QILIN_TMPDIR TMPDIR="$QILIN_TMPDIR"

# 数据卷自检。命名卷的属主由镜像里的目录继承，通常直接可用；
# 绑定挂载要运维先 chown 到 QILIN_UID（默认 10001）。
check_data() {
  data_dir=$(dirname "$QILIN_DB")
  for dir in "$data_dir" "$QILIN_BLOBS" "$QILIN_TMPDIR"; do
    [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || die "无法创建目录 $dir"
    [ -w "$dir" ] || die "目录不可写：${dir}（容器以 uid $(id -u):$(id -g) 运行；绑定挂载请先 chown，或改用命名卷）"
  done
}

# 启动时清掉上次异常退出留下的临时目录（只清 serve 自己的，one-shot 任务不清，
# 免得把正在运行的实例的上传临时文件删掉）。
clean_tmp() {
  find "$QILIN_TMPDIR" -mindepth 1 -maxdepth 1 -mmin +120 -exec rm -rf {} + 2>/dev/null || true
}

if [ "$#" -eq 0 ]; then
  set -- serve
fi

qilin_cmd=$1
shift

case "$qilin_cmd" in
  serve)
    check_data
    clean_tmp
    set -- serve \
      --host "$QILIN_HOST" \
      --port "$QILIN_PORT" \
      --db "$QILIN_DB" \
      --blobs "$QILIN_BLOBS"
    if [ -n "${QILIN_PUBLIC_BASE:-}" ]; then set -- "$@" --public-base "$QILIN_PUBLIC_BASE"; fi
    if [ -n "${QILIN_HUB_ORIGIN:-}" ]; then set -- "$@" --hub-origin "$QILIN_HUB_ORIGIN"; fi
    if [ -n "${QILIN_APP_ID:-}" ]; then set -- "$@" --app-id "$QILIN_APP_ID"; fi
    if [ -n "${QILIN_HUB_AUTHORIZE:-}" ]; then set -- "$@" --hub-authorize "$QILIN_HUB_AUTHORIZE"; fi
    if [ -n "${QILIN_LOG_LEVEL:-}" ]; then set -- "$@" --log-level "$QILIN_LOG_LEVEL"; fi
    if [ "${QILIN_TRUST_PROXY:-0}" = "1" ]; then set -- "$@" --trust-proxy; fi
    if [ -n "${QILIN_TRUSTED_PROXIES:-}" ]; then set -- "$@" --trusted-proxies "$QILIN_TRUSTED_PROXIES"; fi
    log "serve ${QILIN_HOST}:${QILIN_PORT} db=${QILIN_DB} blobs=${QILIN_BLOBS} public_base=${QILIN_PUBLIC_BASE:-<推断>}"
    # exec：让 python 直接接 SIGTERM，docker stop 不会多隔一层 shell。
    exec store "$@"
    ;;
  store)
    exec store "$@"
    ;;
  backup|restore)
    check_data
    # 容器里只有一个数据位置，统一补上 --db/--blobs，免得 one-shot 任务去读工作目录下的 var/。
    exec store "$qilin_cmd" "$@" --db "$QILIN_DB" --blobs "$QILIN_BLOBS"
    ;;
  validate|submit|install|--help|-h)
    exec store "$qilin_cmd" "$@"
    ;;
  sh|bash|ash|python|python3|python3.*|/bin/sh|/bin/bash|/opt/venv/bin/*)
    exec "$qilin_cmd" "$@"
    ;;
  *)
    # 兜底：当作 store 的子命令或外部命令原样执行。
    if command -v "$qilin_cmd" >/dev/null 2>&1; then
      exec "$qilin_cmd" "$@"
    fi
    exec store "$qilin_cmd" "$@"
    ;;
esac
