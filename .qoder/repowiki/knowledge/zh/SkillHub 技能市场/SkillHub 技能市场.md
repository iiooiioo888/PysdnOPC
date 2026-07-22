---
kind: external_dependency
name: SkillHub 技能市场
slug: skillhub
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

### SkillHub
- 角色：OpenOPC 的技能远程市场，支持搜索、缓存和自动推广成功使用的技能。
- 集成点：`config/system_config.yaml` 的 `capabilities.skillhub.*` 控制开关、API 地址、认证、搜索限制、缓存策略等；默认 `enabled: false`。
- 使用模式：通过 `api_base=https://www.skillhub.club/api/v1` 访问远程技能仓库，支持 hybrid 搜索方法，成功后自动 promote 到本地。