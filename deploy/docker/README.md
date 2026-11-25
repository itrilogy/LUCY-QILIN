# 栖林商店 · Docker 运行包

这里只有运行镜像需要的四件东西：镜像定义、入口脚本、备份/恢复脚本、健康探针。
调度与拓扑在仓库根的 `compose.yaml`，设计与取舍在
[`docs/Docker容器化部署设计-2026-11-20.md`](../../docs/Docker容器化部署设计-2026-11-20.md)。

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 两阶段构建：builder 装依赖，runtime 非 root 跑 |
| `entrypoint.sh` | 环境变量 → `store serve` 参数；`*_FILE` 读取密钥；数据卷自检 |
| `backup.sh` | 一次性备份 + 保留 N 份 |
| `import-existing-data.sh` | 把现有 `var/` 迁进数据目录，逐表对账（不丢商品） |
| `restore.sh` | 校验和核对后覆盖恢复（要 `QILIN_RESTORE_CONFIRM=yes`） |
| `healthcheck.py` | 探 `/api/health`，503 视为不健康 |
| `Caddyfile` | 容器内 TLS 反代模板 |
| `.env.example` | 全部可配置项与默认值 |

## 最短路径

```sh
# 仓库根
cp deploy/docker/.env.example .env
vi .env                    # 改 QILIN_PUBLIC_BASE，确认 QILIN_DATA_DIR / QILIN_BACKUP_DIR

sudo install -d -o 10001 -g 10001 -m 0750 /srv/qilin/data /srv/qilin/backups

docker compose build
docker compose up -d
docker compose ps                        # qilin (healthy)
curl -fsS http://127.0.0.1:8787/api/health
```

建管理员（空库不会把第一个注册者变成管理员）：

```sh
docker compose exec qilin store init-admin --username admin
```

命令会打印一次性口令。随后浏览器打开 `QILIN_PUBLIC_BASE` 用它登录。

## 已有商品迁进来

从本机/systemd 的 `var/` 切到容器，用迁移脚本；它只读源、写目标，并逐表对账：

```sh
sudo systemctl stop qilin                          # 先停旧服务
./deploy/docker/import-existing-data.sh --source ./var --target /srv/qilin/data
docker compose up -d
```

输出最后一行必须是「对账通过：行数一致、制品齐全、库完整。」。不一致时脚本退出 1，**不要**带着不一致的数据启动服务。

迁进去之后打开货架，确认在架数量与原站一致；`data/` 与 `backups/` 都在 `.gitignore` 里。

## 常用运维命令

```sh
docker compose logs -f --tail=200 qilin           # 看日志
docker compose --profile ops run --rm backup      # 立刻备份一次
docker compose exec qilin store validate /path    # 容器内跑 CLI
docker compose stop qilin && docker compose start qilin   # 重启（不重建）

# 恢复（先停服务）
docker compose stop qilin
docker compose --profile ops run --rm \
  --entrypoint /usr/local/bin/qilin-restore -e QILIN_RESTORE_CONFIRM=yes \
  backup 20261120T030000Z
docker compose start qilin
```

## 边界

- 单副本。SQLite 写锁、限流、KEY 缓存都在进程内，两个副本指向同一份数据会互相踩；`serve` 会在 `<db>.lock` 上取 flock，第二个实例直接拒绝启动，`restore` 在服务运行时也会拒绝。
- 数据目录必须是本地块存储。不要放 NFS/SMB/CIFS，SQLite 的 POSIX 锁在网络文件系统上不可靠。
- 镜像不含 TLS 终结，默认只发布到 `127.0.0.1:8787`，由外部反代或 `--profile edge` 的 Caddy 接 443。
