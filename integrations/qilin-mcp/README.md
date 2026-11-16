# 栖林商店 MCP bundle

配置式 bundle，把一个 streamable-http MCP 连接插进 DSH。不含 Host/Client 代码。

## 装法

1. 启动商店：`PYTHONPATH=. python3 -m store.cli serve --port 8787`
2. 在 DSH 的 Plugins 里添加本地目录 `integrations/qilin-mcp`，或在会话里让 agent 调
   `plugin_manager` 的 `install_bundle`，target 填这个目录的绝对路径。
3. 新开一个会话，工具列表里应出现 `mcp__qilin__*`；让模型读 `qilin://spec/upload`
   或调 `get_spec`。

## 其他 AI 工具

用通用 MCP 配置：

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

要上传再加 `"headers": {"Authorization": "Bearer <商店 KEY>"}`。匿名只能读货架与规范。
