---
kind: external_dependency
name: 飞书 (Feishu/Lark) 消息通道
slug: feishu
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
scope:
    - '**'
---

### 飞书 (Feishu/Lark)
- 角色：OpenOPC 的飞书机器人通道，支持 WebSocket 长连接接收和发送消息。
- 认证：基于飞书应用的 `app_id` + `app_secret` 获取临时令牌，`encrypt_key` 和 `verification_token` 仅在租户要求时启用；入站 sender 白名单 `allow_from` 默认为空即拒绝全部。