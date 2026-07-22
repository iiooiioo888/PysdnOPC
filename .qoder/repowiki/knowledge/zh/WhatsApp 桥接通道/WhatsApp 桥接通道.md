---
kind: external_dependency
name: WhatsApp 桥接通道
slug: whatsapp-bridge
category: external_dependency
category_hints:
    - vendor_identity
    - framework_behavior
scope:
    - '**'
---

### WhatsApp Bridge
- 角色：通过独立桥接服务连接 WhatsApp，OpenOPC 仅通过 WebSocket 与桥通信，不直接管理外部进程。
- 集成点：`channels-whatsapp` 额外依赖 `websockets>=12.0`；`channel_config.yaml` 中 `channels.whatsapp.bridge_url` 为必填，`allow_from` 控制入站白名单。
- 行为：需先启动外部 WhatsApp 桥服务并完成 QR 配对，再配置 `bridge_url` 启动 OpenOPC。