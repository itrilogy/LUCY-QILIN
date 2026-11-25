# 栖林商店运行镜像。
#
#   构建：docker build -f deploy/docker/Dockerfile -t qilin:local .
#   上下文必须是仓库根目录（.dockerignore 在根上）。
#
# 两个阶段：
#   builder  装依赖并预编译字节码（cryptography 走 manylinux wheel，不需要编译器）
#   runtime  只带走 venv、store 包和入口脚本，非 root 运行
#
# 只跑一个进程、一份 SQLite、一个本地制品目录。不要扩副本：
# 写路径靠进程内 RLock 串行化，限流与 KEY 缓存都是进程内状态（见 docs/Docker容器化部署设计-2026-11-20.md）。

ARG PYTHON_VERSION=3.13
ARG PYTHON_IMAGE=python:${PYTHON_VERSION}-slim-bookworm
ARG BUILDER_IMAGE=
ARG PIP_INDEX_URL=

# ---------- builder ----------
FROM ${BUILDER_IMAGE:-${PYTHON_IMAGE}} AS builder
USER root

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /src

# 在线构建创建独立 venv 并拉取 cryptography；离线重构建可通过 BUILDER_IMAGE 复用已有 /opt/venv，零外网访问。
RUN if [ -x /opt/venv/bin/python ] && /opt/venv/bin/python -c "import cryptography" >/dev/null 2>&1; then \
      echo "reusing prebuilt /opt/venv"; \
    else \
      python -m venv /opt/venv \
      && /opt/venv/bin/python -m pip install --upgrade "pip>=24" \
      && /opt/venv/bin/python -m pip install "cryptography>=42"; \
    fi

COPY pyproject.toml ./
COPY store ./store

# 在线构建走标准 wheel 安装；离线复用已有 venv（BUILDER_IMAGE）时直接替换 store 包并修正全局可读权限与字节码。
RUN if ls -d /opt/venv/lib/python*/site-packages/qilin_store-*.dist-info >/dev/null 2>&1; then \
      sp=$(dirname "$(ls -d /opt/venv/lib/python*/site-packages/qilin_store-*.dist-info | head -n1)"); \
      rm -rf "$sp/store" && cp -a ./store "$sp/store" && chmod -R a+rX "$sp/store"; \
    else \
      /opt/venv/bin/python -m pip install --no-deps .; \
    fi \
 && /opt/venv/bin/python -m pip check \
 && /opt/venv/bin/python -m compileall -q /opt/venv/lib/python*/site-packages/store \
 && chmod -R a+rX /opt/venv

# ---------- runtime ----------
FROM ${PYTHON_IMAGE} AS runtime

# 构建参数：非 root 的 uid/gid。绑定挂载的数据目录要用同一个属主。
ARG QILIN_UID=10001
ARG QILIN_GID=10001
# 版本信息只进 OCI 标签，供镜像仓库与排障识别。
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="qilin-store" \
      org.opencontainers.image.description="栖林商店：门户 + HTTP 接口 + 商店 MCP（单进程 SQLite）" \
      org.opencontainers.image.source="https://github.com/itrilogy/LUCY-QILIN" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# PYTHONUNBUFFERED：日志实时进 docker logs，不丢最后一行。
# PYTHONDONTWRITEBYTECODE：配合只读根文件系统，不在运行时写 .pyc。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/qilin \
    QILIN_HOST=0.0.0.0 \
    QILIN_PORT=8787 \
    QILIN_DB=/var/lib/qilin/qilin.db \
    QILIN_BLOBS=/var/lib/qilin/blobs \
    QILIN_TMPDIR=/var/lib/qilin/tmp

# 基础镜像已带 ca-certificates / tzdata（枢纽验签与 TZ 都需要），这里不再 apt。
RUN set -eux; \
    groupadd --gid "${QILIN_GID}" qilin; \
    useradd --uid "${QILIN_UID}" --gid qilin \
      --home-dir /home/qilin --create-home --shell /usr/sbin/nologin qilin; \
    install -d -o qilin -g qilin -m 0750 /var/lib/qilin /var/lib/qilin/blobs

COPY --from=builder /opt/venv /opt/venv
COPY deploy/docker/entrypoint.sh /usr/local/bin/qilin-entrypoint
COPY deploy/docker/backup.sh /usr/local/bin/qilin-backup
COPY deploy/docker/restore.sh /usr/local/bin/qilin-restore
COPY deploy/docker/healthcheck.py /usr/local/bin/qilin-healthcheck

# 构建期冒烟：包能导入、门户资源在包里。COPY/打包漏了文件时在构建期就失败。
RUN set -eux; \
    chmod 0755 /usr/local/bin/qilin-entrypoint /usr/local/bin/qilin-backup \
                /usr/local/bin/qilin-restore /usr/local/bin/qilin-healthcheck; \
    python -c "from store.http_api import PORTAL; from store.backup import backup; assert PORTAL.is_file(); print('image smoke ok')"

USER qilin
WORKDIR /home/qilin

EXPOSE 8787

# 就绪探针：/api/health 在库不可读或制品目录不可写时回 503。
# start-period 给启动时的索引重建与迁移留时间。
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["/usr/local/bin/qilin-healthcheck"]

ENTRYPOINT ["/usr/local/bin/qilin-entrypoint"]
CMD ["serve"]
