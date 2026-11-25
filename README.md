# 栖林商店

[![ci](https://github.com/itrilogy/LUCY-QILIN/actions/workflows/ci.yml/badge.svg)](https://github.com/itrilogy/LUCY-QILIN/actions/workflows/ci.yml)

本仓库是栖林商店：一个 Python 进程，同时提供门户页面、HTTP 接口和商店 MCP。制品与账号存在本机的 SQLite 和目录里。产品种类：`skill`、`mcp`、`mcp+skill`、`runtime`、`knowledge`、`plugin`。规格在 [agent-store-design.md](agent-store-design.md)。

运行时基础安装只用 Python 标准库。枢纽令牌验签需要 `cryptography`，装在 `hub` extra 里：`pip install -e '.[hub]'`。设计目标是 Python 3.11 及以上。自动化测试另需 `pytest`。

下面的步骤都在仓库根目录执行，且按本机 `127.0.0.1` 来。本机试用不必配置对外地址，也不必配置枢纽。

## 仓库结构

```text
store/          服务端：门户、HTTP 接口、MCP、校验器、安装器
  portal/       门户静态资源（无构建步骤；CSP 允许的只有同源脚本）
tests/          自动化测试（起临时端口，不读 var/）
deploy/         systemd / launchd / 反代示例；docker/ 是容器运行包
integrations/   DSH 配置式 bundle（qilin-mcp）
scripts/        验收脚本与门户 JS 语法检查
docs/           设计、评审、部署与交付文档（索引见 docs/README.md）
compose.yaml    容器编排（单副本 + 可选容器内 Caddy + 一次性备份）
agent-store-design.md   规格（唯一事实源，测试会拿它与代码核对）
var/            运行期数据（数据库、制品、备份；不进版本库）
```

常用命令收在 `Makefile`：`make test`、`make check`、`make serve`、`make init-admin`、`make backup`、`make clean`。

## 文档

全部文档的索引在 [docs/README.md](docs/README.md)。判定"能不能交付"先看
[交付验收清单](docs/交付验收-2026-11-22.md)。

## 产品接入验证

MCP 查询能力、ZIP 下载/解压、DSH 安装边界见 [产品查询与 ZIP 安装验证](docs/产品查询与ZIP安装验证-2026-11-17.md)。

## DSH 接入

只读/管理两个 MCP 配置、插件索引源是否值得做的评估见 [DSH 接入与插件索引评估](docs/DSH接入与插件索引评估-2026-11-17.md)。

## 知识包阅读与入库

知识包在商店里只负责上架、下载、解压；本地阅读、检索与入库由独立项目 **知林 · ZhiLin** 承担：

- 项目：[LUCY-ZHILIN](../LUCY-ZHILIN/README.md)
- 集成说明（已迁入该项目）：[STORE-INTEGRATION](../LUCY-ZHILIN/docs/STORE-INTEGRATION.md)
- 知识根默认 `~/.qilin/knowledge`，可用 `QILIN_KNOWLEDGE_ROOT` 覆盖；商店安装器与插件共用这个约定。
- 协议：`get_spec(name="knowledge-intake")` 或 `qilin://spec/knowledge-intake`；`kind=knowledge` 的安装计划带 `post_install`。

## 代码审计

最近一轮变更后的代码/文档审计见 [代码与文档变更审计](docs/代码审计-2026-11-18.md)。

2026-11-20 的整体深度评审（P1/P2 分级、功能完善建议、路线图）见
[代码深度评审与功能完善建议](docs/代码深度评审-2026-11-20.md)。

交付前的验收清单（命令、期望结果、已知限制、运维交接）见
[交付验收清单](docs/交付验收-2026-11-22.md)。

## 角色与权限

作者/审核/管理员/只读的边界与 KEY 激活方式见 [角色、权限与激活设计](docs/角色与权限设计-2026-11-17.md)。

## 生产部署

两种方式，二选一：

- **Docker**（推荐）：[Docker 容器化部署设计](docs/Docker容器化部署设计-2026-11-20.md)。仓库根有 `compose.yaml`：

  ```sh
  cp deploy/docker/.env.example .env    # 改 QILIN_PUBLIC_BASE
  sudo install -d -o 10001 -g 10001 -m 0750 /srv/qilin/backups
  docker compose up -d                  # 只有 127.0.0.1:8787，先建管理员
  docker compose exec qilin store init-admin --username admin
  docker compose --profile edge up -d   # 再接 443（容器内 Caddy）
  ```

- **systemd / launchd**：部署、TLS 反代、备份恢复与安全清单见
  [生产部署手册](docs/生产部署-2026-11-17.md)。

置顶约束：单进程、单副本、SQLite 与制品必须在本地块存储上；数据卷不要放 NFS/SMB。

## 启动

```sh
PYTHONPATH=. python3 -m store.cli serve
```

默认监听 `127.0.0.1:8787`。标准错误里会打印实际地址。浏览器打开：

```text
http://127.0.0.1:8787/
```

`Ctrl-C` 停止。数据库默认是 `var/qilin.db`，制品目录默认是 `var/blobs`。这两个路径可以用 `--db`、`--blobs` 改。`var/` 不进版本库。

换端口：

```sh
PYTHONPATH=. python3 -m store.cli serve --port 8788
```

## MCP 接入

商店起来后，MCP 地址是 `http://127.0.0.1:8787/mcp`。匿名可以看货架、读全部规范；上传与审核要在门户里登录并持有 KEY。通用配置：

```json
{
  "mcpServers": {
    "qilin": {
      "type": "http",
      "url": "http://127.0.0.1:8787/mcp"
    }
  }
}
```

DSH 可以直接装 [`integrations/qilin-mcp`](integrations/qilin-mcp) 这个配置式 bundle，装完新开会话会出现 `mcp__qilin__*` 工具。

## 本机试用

1. **先建管理员**（空库时 HTTP 注册一律拒绝，避免空库暴露后第一个请求抢走管理员）：

   ```sh
   PYTHONPATH=. python3 -m store.cli init-admin --username admin
   ```

   命令会生成一个随机口令并只打印一次。口令也可以用 `--password` 指定，规则见下。
2. 打开门户，用这个口令登录。**自助注册只产生作者**；审核与管理员由管理员在「用户管理」里授予或直接开号（初始口令只显示一次）。
3. 口令 10 到 64 位，须同时含小写字母、大写字母和数字，首尾不能是空格，不能包含用户名。
4. 登录后在「连接」页取商店 KEY，AI 工具连接用这把 KEY，不要写进仓库。
5. 用作者提交，用审核在审核页发布。匿名可以看已发布的货架、下载制品，不能提交。

要整体关闭自助注册，启动前设 `QILIN_REGISTRATION=closed`（默认 `open`）。

## 自动化测试

```sh
python3 -m pip install pytest
PYTHONPATH=. python3 -m pytest tests -q
```

测试自己起临时端口上的服务，不占用 8787，也不读你的 `var/qilin.db`。

## 本机不需要、部署时才用的配置

| 参数或环境变量 | 什么时候要 |
|---|---|
| `--public-base` / `QILIN_PUBLIC_BASE` | 前面有反向代理，安装计划里的地址必须是用户实际打开的那个地址。本机直接访问 `127.0.0.1` 时留空 |
| `--hub-origin` / `QILIN_HUB_ORIGIN` | 要验实验室枢纽令牌时。不配则只有门户账号和商店 KEY |
| `--hub-authorize` / `QILIN_HUB_AUTHORIZE` | 要由商店发起「实验室账号登录」时。值是授权页地址，可含 `{state}`、`{redirect_uri}`、`{app_id}`。不配则没有这个按钮；枢纽自己跳回来的回调仍可用，但日志记 `STATE_UNBOUND` |
| `QILIN_HUB_SECRET` | 用枢纽换令牌时的应用密钥。只放在进程环境里 |
| `QILIN_REGISTRATION` | `open`（默认）或 `closed`。只影响自助注册；空库无论哪种都必须先 `store init-admin` |
| `QILIN_FETCH_POLICY` | 上传校验时服务端抓外部地址的策略：`public`（默认，只允许公网）、`allowlist`、`any`。内网依赖用 `QILIN_FETCH_ALLOW_HOSTS` 加白名单 |
| `QILIN_FETCH_ALLOW_HOSTS` | 允许抓取的内网主机/域名后缀，逗号分隔，例如 `internal.corp,10.1.2.3` |
| `--log-level` / `QILIN_LOG_LEVEL` | 日志级别，默认 `WARNING`，写到标准错误 |

这些值在启动时读入。门户里没有修改它们的页面。

## 备份

默认读 `var/qilin.db` 与 `var/blobs`。在低峰或停写时执行：

```sh
PYTHONPATH=. python3 -m store.cli backup var/backup-2026-11-16
```

备份目录必须不存在或为空；里面会有 `qilin.db`、`blobs/` 和一份 `backup.json`。
`backup.json` 记录每个文件的 sha256；恢复前会逐个核对。

恢复：

```sh
PYTHONPATH=. python3 -m store.cli restore var/backup-2026-11-16
```

默认拒绝覆盖已存在的库与制品目录；确认要覆盖时加 `--force`。恢复前先停掉 `store serve`。

定时备份交给系统调度，不在进程里起线程。例如每天的 crontab：

```cron
0 3 * * * cd /path/to/LUCY-QILIN && PYTHONPATH=. python3 -m store.cli backup "var/backup-$(date +\%F)" >> var/backup.log 2>&1
```

备份不会自动清理旧目录；保留几份、放在哪块盘，由部署者决定。

## 许可证

MIT，见 [LICENSE](LICENSE)。
