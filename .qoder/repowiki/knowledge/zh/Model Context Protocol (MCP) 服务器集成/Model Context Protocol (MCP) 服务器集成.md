---
kind: external_dependency
name: Model Context Protocol (MCP) 服务器集成
slug: model-context-protocol
category: external_dependency
category_hints:
    - framework_behavior
    - client_constraint
scope:
    - '**'
---

### Model Context Protocol (MCP)
- 角色：OpenOPC 通过 MCP 协议发现并调用外部工具服务器（GitHub、文件系统、Playwright 等），将第三方能力注册为可被 agent 调用的工具。
- 集成点：`config/system_config.yaml` 的 `mcp_servers` 段定义本地 stdio 或远程 HTTP/SSE 服务器；`opc/mcp_client.py` 负责生命周期管理与工具发现。
- 使用模式：本地服务器通过 `npx @playwright/mcp@latest` 等命令启动，远程服务器通过 URL + headers 连接；工具名带 server 前缀避免冲突。