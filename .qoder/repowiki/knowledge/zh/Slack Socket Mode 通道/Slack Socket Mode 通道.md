---
kind: external_dependency
name: Slack Socket Mode 通道
slug: slack
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
scope:
    - '**'
---

### Slack
- 角色：通过 Slack Socket Mode（WebSocket）而非事件回调方式接收消息，降低网络复杂度。