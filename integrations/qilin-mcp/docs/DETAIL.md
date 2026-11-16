# 栖林商店 MCP

这是一个配置式 bundle，只插入一条 `@deepseek-ai/dsh-mcp-client` 连接，不含任何可执行代码。

- 地址：`http://127.0.0.1:8787/mcp`
- serverName：`qilin`，工具名形如 `mcp__qilin__search`
- 匿名可用：`get_spec`、`search`、`list_versions`、`resolve`、`describe`、`install_plan`、`artifact`
- 需要 KEY 才有：`validate`、`begin_submit`、`begin_revise` 等上传工具
- 规范资源：`qilin://spec/*`、`qilin://schema/knowledge`、`qilin://vocab/knowledge` 等 12 条

安装前先在本仓库根启动：

```sh
PYTHONPATH=. python3 -m store.cli serve --port 8787
```

如果商店不在本机或换了端口，改 `cordis.patch.yml` 里的 `url`。
