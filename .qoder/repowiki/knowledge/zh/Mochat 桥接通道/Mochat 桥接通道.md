---
kind: external_dependency
name: Mochat 桥接通道
slug: mochat-bridge
category: external_dependency
category_hints:
    - vendor_identity
    - framework_behavior
scope:
    - '**'
---

### Mochat 桥接
- 角色：通过 Socket.IO 或 HTTP watch 模式连接 Mochat 平台，支持面板（panel）作为桥/群组目标。
- 行为：优先尝试 socket 模式，失败后自动回退到 HTTP watch/poll。