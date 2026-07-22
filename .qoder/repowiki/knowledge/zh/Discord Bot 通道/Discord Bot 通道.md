---
kind: external_dependency
name: Discord Bot 通道
slug: discord
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
scope:
    - '**'
---

### Discord Bot
- 角色：通过 Discord Gateway socket 连接，支持 DM 和群组消息。
- 集成点：`channels-discord` 额外依赖 `discord.py>=2.3`；`channel_config.yaml` 中 `channels.discord.token` 为必填，需启用相应 gateway intents，`group_policy` / `allow_from` 控制权限。