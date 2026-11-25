# 反馈：Antigravity 客户端接入 Qilin Store MCP 的会话稳定性、制品拉取与客户端枚举问题

- 反馈日期：2026-11-24
- 反馈应用（客户端）：**Antigravity**（macOS / darwin-arm64）
- 接入方式：通过 `qilin-publish` MCP Server（HTTP/SSE 会话）对接 Qilin Store（`http://10.37.87.25:8084`）
- 涉及商品：`luxi.paper-arts@4.3.0`、`luxi.gov-unified-writer@3.2.0`、`luxi.qc-tech-project-methodology@1.0.0`、`luxi.professional-writing-methodology@1.0.0`
- 性质：多客户端共用缺陷复现 + MCP 会话断连 + 客户端能力枚举缺口

## 一、在 Antigravity 客户端复现的问题清单

### 1. `qilin-publish` MCP 会话在多轮任务间隔后频繁报 `session not found`

- **现象**：在 Antigravity 进行多步骤技能调研与文件编辑（耗时约数十秒至数分钟）后，再次调用 `qilin-publish` 工具（如 `describe`、`artifact`、`validate`）时，工具层返回连接错误：
  ```
  failed to connect (session ID: <uuid>): session not found
  ```
- **影响**：长链路工作流（如“先检索对比 -> 编辑构建 -> 调用 `validate` -> `begin_submit` -> `submit_package`”）在中间步骤极易因服务端/网关会话过期被清理而中断，Antigravity 侧必须重试或重新触发 MCP 连接，无法保证一次完整发布流程的连续性。

### 2. 已发布技能制品在 Antigravity 侧同样 100% 不可下载（`VERSION_UNAVAILABLE`）

- **复现步骤**：在 Antigravity 中依次调用 `search` → `describe` → `install_plan` → `artifact`：
  - `describe(id="luxi.paper-arts", version="4.3.0")`：成功返回元数据。
  - `describe(id="luxi.gov-unified-writer", version="3.2.0")`：成功返回元数据。
  - `install_plan(id="luxi.paper-arts", version="4.3.0", client="reasonix", platform="darwin-arm64")`：成功返回下载计划及 `sha256`（`ee3eafcb5be98bf3640e8f38447399114c20487322c0f774feefefdc07a9e16a`）。
  - `artifact(id="luxi.paper-arts", version="4.3.0")` / `artifact(id="luxi.gov-unified-writer", version="3.2.0")`：均返回错误：
    ```json
    {"rule": "VERSION_UNAVAILABLE", "message": "制品文件不可读"}
    ```
  - 直接通过 `curl` 请求 `install_plan` 给出的 `http://10.37.87.25:8084/v1/artifacts/luxi.paper-arts/4.3.0` 与 `.../luxi.gov-unified-writer/3.2.0`，均返回 `HTTP/1.1 400` 与相同 JSON 报错。
- **结论确认**：该问题已在 [反馈：制品文件不可读](反馈-制品文件不可读-2026-11-24.md) 中定位为服务端 `releases.artifact_path` 相对路径问题；本记录补充确认 **Antigravity 客户端侧 100% 复现**，最终只能从本机已有下载目录（`qilin-downloads`）手工迁移原包完成后续工作。

### 3. 客户端能力枚举与 `manifest.json` 校验口径不一致

- **现象**：
  1. 在 Antigravity 中调用 `install_plan` 时，`client` 与 `platform` 为必填参数，但商店当前可识别客户端集合中尚无 `antigravity`（只能借用 `reasonix` 或 `deepseek-harness` 查询）。
  2. `get_spec("skill")` 规则 `CLIENT_CAPABILITY` 声明“只有 `reasonix` 有 `skill_dir`，不要给没有这个能力的工具填技能”；但商店已正式上架的 `luxi.paper-arts@4.3.0` 与 `luxi.gov-unified-writer@3.2.0` 的 `manifest.json` 中均包含 `"clients": ["reasonix", "deepseek-harness"]`，且随包附带了 `platform/gemini-cli.md`、`platform/opencode-hermes.md`、`platform/generic-cli.md` 等 5 端适配文件。
- **影响**：技能作者在为 Antigravity、DeepSeek Harness、Gemini CLI 等多端打包时，无法确定 `manifest.json` 的 `clients` 字段应如何填写才既不触发 `CLIENT_CAPABILITY` 报错，又能如实表达多端适配能力。

## 二、对商店侧的建议（Antigravity 客户端视角）

1. **MCP 会话保活或无状态重试**：对 `qilin-publish` 的 HTTP/SSE 会话增加合理的 TTL 或支持在 `session not found` 时服务端自动透明重建会话，避免长任务流中途断链。
2. **优先修复 `releases.artifact_path` 相对路径问题**（同 [反馈：制品文件不可读](反馈-制品文件不可读-2026-11-24.md) 建议），确保 `artifact` 工具与 `/v1/artifacts/...` 在任何客户端均可正常拉取。
3. **补充 `antigravity` 客户端能力注册与多端 `clients` 校验口径对齐**：在商店客户端能力表中明确 `antigravity` 的 `skill_dir` / `knowledge_dir` / `mcp` 能力支持，并与 `get_spec("skill")` 中的 `CLIENT_CAPABILITY` 规则保持一致。
