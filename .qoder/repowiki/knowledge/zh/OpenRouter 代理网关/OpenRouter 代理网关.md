---
kind: external_dependency
name: OpenRouter 代理网关
slug: openrouter
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

### OpenRouter
- 角色：作为默认 LLM API 代理，提供多模型统一入口与密钥管理。
- 集成点：`config/llm_config.yaml` 中 `api_base` 默认指向 `https://openrouter.ai/api/v1`，`api_key` 或 `api_key_env` 注入密钥。
- 使用模式：通过 LiteLLM 的 `openai/*` 兼容协议访问 OpenRouter 上的任意模型；也可替换为其他兼容代理或自建端点。