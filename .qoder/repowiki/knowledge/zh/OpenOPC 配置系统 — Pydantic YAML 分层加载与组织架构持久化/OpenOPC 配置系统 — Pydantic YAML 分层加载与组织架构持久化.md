---
kind: configuration_system
name: OpenOPC 配置系统 — Pydantic YAML 分层加载与组织架构持久化
category: configuration_system
scope:
    - '**'
source_files:
    - opc/core/config.py
    - config/system_config.yaml
    - config/llm_config.yaml
    - config/agent_config.yaml
    - config/channel_config.yaml
    - config/company_corporate_config.yaml
---

## 体系概览

OpenOPC 使用 **Pydantic v2 + YAML** 实现统一的运行时配置系统。所有配置以多个 YAML 文件声明，启动时由 `opc/core/config.py` 中的 `OPCConfig.load()` 按固定顺序合并、校验并转换为强类型模型，再供各层（LLM、通道、公司模式、权限等）消费。

## 配置文件与层级

- 顶层模板位于 `config/`：
  - `system_config.yaml` — 系统级开关（日志、浏览器、原生运行时、MCP 服务器、心跳、任务模式等）
  - `llm_config.yaml` — LLM 默认模型、API Key、上下文窗口、路由/回退策略
  - `agent_config.yaml` — 外部代理（claude/cursor/codex/opencode）及原生子代理 profile
  - `channel_config.yaml` — 各消息通道（Telegram/Discord/Slack/飞书/钉钉/邮件/Mochat/QQ/Matrix）的启用与凭据
  - `company_corporate_config.yaml` — 内置“企业”公司模式的初始角色/员工/升级规则（作为默认 org payload）

- 运行期数据目录 `.opc/`：
  - `config/` — 用户覆盖后的最终配置副本（由 `OPCConfig.save()` 写入）
  - `company_orgs/<org_id>_config.yaml` — 每个组织的公司架构快照
  - `company_state/<org>/employees/*.yaml` — 员工注册表（从 org payload 中分离）

## 加载与合并规则

1. **项目根发现**：通过向上查找 `pyproject.toml` 定位项目根；`.opc` 目录默认位于 `{project_root}/.opc`，可通过环境变量 `OPC_HOME` 覆盖。
2. **多文件合并顺序**：依次读取 `system_config.yaml` → `llm_config.yaml` → `agent_config.yaml` → `channel_config.yaml`，后读入的键覆盖前者。
3. **组织架构载入**：同时调用 `load_company_org_payload(config_dir, DEFAULT_ORGANIZATION_ID)` 把公司模式 org payload 映射为 `OrgConfig`，并与上述扁平字段合并到 `OPCConfig`。
4. **向后兼容迁移**：
   - `agent_config` 在加载时对旧版 `approval_mode` 做一次性重写并原子写回。
   - 自动检测 `config/company_corporate_config.yaml` / `config/org_config.yaml` 等历史位置，将其升迁到新的 `company_orgs/` 布局。
5. **原子写入**：所有持久化均通过 `_atomic_write_yaml`（先写临时文件再 `os.replace`），避免并发损坏。

## 核心模型与约定

- `OPCConfig` 是顶层 Pydantic 模型，包含 `system`、`llm`、`agents`、`org`、`channels`、`autonomy`、`capabilities` 七个子段。
- 每个子段对应一个 Pydantic `BaseModel`（如 `SystemConfig`、`LLMConfig`、`ChannelsConfig`、`AutonomyConfig`、`MCPServerConfig`、`NativeRuntimeConfig` 等），提供默认值、枚举约束、别名（`task_mode`/`project_mode`）和自定义 `field_validator`。
- 公司模式采用“payload 即配置”的思路：`build_company_org_payload_from_config` 把 `OrgConfig` 渲染为标准 JSON/YAML 结构，再由 `write_company_org_payload` 落盘到 `company_orgs/<id>_config.yaml`，并通过 `company_index.yaml` 维护 active org。

## 安全与权限相关配置

- `autonomy.sensitive_keywords`、`safe_command_prefixes`、`dangerous_shell_patterns` 控制命令审查与安全白名单。
- `autonomy.permissions_v2` 统一权限预测器开关、fail-closed 策略、路径/工具黑白名单、沙箱平台策略等。
- `system.require_confirmation` 列出需要二次确认的高危操作关键词。

## 开发者规范

- **新增配置项**：在 `opc/core/config.py` 中扩展对应 Pydantic 模型，并在 `OPCConfig.load/save` 的合并/序列化逻辑中补齐映射；如需新 YAML 文件，遵循现有命名与合并顺序约定。
- **敏感信息**：永远不要硬编码 API Key，使用 `api_key_env` 或让用户在 `llm_config.yaml` 中填写空字符串占位。
- **向后兼容**：对破坏性变更使用 `_migrate_*` 函数在加载时做一次迁移并原子写回，保留 schema_version 检查。
- **组织配置**：修改公司架构应通过 `build_company_org_payload_from_config` 生成标准 payload，不要直接手写 `company_orgs/` 下的文件。
- **并发安全**：所有写操作必须走 `_atomic_write_yaml`，禁止直接 `open().write()`。

## 关键文件

- `opc/core/config.py` — 配置模型定义、加载/保存/迁移主逻辑
- `config/system_config.yaml` — 系统级运行时开关与 MCP 服务器清单
- `config/llm_config.yaml` — LLM 默认模型与上下文窗口
- `config/agent_config.yaml` — 外部代理与原生子代理 profile
- `config/channel_config.yaml` — 各消息通道凭据与策略
- `config/company_corporate_config.yaml` — 内置企业公司模式初始数据
- `.opc/config/` — 运行期生效的配置副本（由 `opc init` 生成）
