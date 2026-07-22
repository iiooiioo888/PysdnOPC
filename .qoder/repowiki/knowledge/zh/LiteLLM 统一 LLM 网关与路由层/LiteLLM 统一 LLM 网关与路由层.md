---
kind: external_dependency
name: LiteLLM 统一 LLM 网关与路由层
slug: litellm
category: external_dependency
category_hints:
    - vendor_identity
    - sdk_real_api
scope:
    - '**'
---

### LiteLLM
- 角色：OpenOPC 的 LLM 抽象层，所有模型调用（含 fallback、routing、context_window 探测）均通过 LiteLLM 完成。
- 集成点：`config/llm_config.yaml` 中的 `default_model`、`api_base`、`api_key`、`routing`、`fallback`、`context_window` 等字段直接映射到 LiteLLM 配置；运行时由 `opc/llm/provider.py` 加载并构造客户端。
- 使用模式：以 `provider/model` 形式选择后端（如 `openai/gpt-5.4`），默认 `api_base` 指向 OpenRouter；对未映射模型回退到 128000 上下文窗口，可通过 `context_window_overrides` 覆盖。