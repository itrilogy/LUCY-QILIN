# 栖林商店

[![ci](https://github.com/itrilogy/LUCY-QILIN/actions/workflows/ci.yml/badge.svg)](https://github.com/itrilogy/LUCY-QILIN/actions/workflows/ci.yml)

本仓库是栖林商店：一个 Python 进程，同时提供门户页面、HTTP 接口和商店 MCP。制品与账号存在本机的 SQLite 和目录里。产品种类：`skill`、`mcp`、`mcp+skill`、`runtime`、`knowledge`、`plugin`。规格在 [agent-store-design.md](agent-store-design.md)。

运行时基础安装只用 Python 标准库。枢纽令牌验签需要 `cryptography`，装在 `hub` extra 里：`pip install -e '.[hub]'`。设计目标是 Python 3.11 及以上。自动化测试另需 `pytest`。

下面的步骤都在仓库根目录执行，且按本机 `127.0.0.1` 来。本机试用不必配置对外地址，也不必配置枢纽。

## 生产部署

部署、TLS 反代、systemd/launchd、备份恢复与安全清单见 [生产部署手册](docs/生产部署-2026-11-17.md)。

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

1. 打开门户，注册第一个账号。库里还没有账号时，这个账号是管理员；之后注册的是发布者。
2. 口令 10 到 64 位，须同时含小写字母、大写字母和数字，首尾不能是空格，不能包含用户名。
3. 注册成功后页面会给出一把商店 KEY，只显示这一次。AI 工具连接用这把 KEY，不要把它写进仓库。
4. 用发布者提交，用管理员在审核页发布。匿名可以看已发布的货架，不能提交。

管理员可以在门户里把后来的账号改成管理员。

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
