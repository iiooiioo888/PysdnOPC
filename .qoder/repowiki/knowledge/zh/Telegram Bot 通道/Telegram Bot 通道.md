---
kind: external_dependency
name: Telegram Bot 通道
slug: telegram
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
scope:
    - '**'
---

### Telegram Bot
- 角色：通过 Telegram Bot API 接收和回复消息的通道，采用 polling 模式。
- 集成点：`channels-telegram` 额外依赖 `python-telegram-bot>=21.0`；`channel_config.yaml` 中 `channels.telegram.token` 为必填，`allow_from` 控制入站白名单。