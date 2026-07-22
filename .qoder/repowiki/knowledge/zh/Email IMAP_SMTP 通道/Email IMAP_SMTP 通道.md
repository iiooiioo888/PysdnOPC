---
kind: external_dependency
name: Email IMAP/SMTP 通道
slug: email-channel
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
scope:
    - '**'
---

### Email
- 角色：通过 IMAP 轮询收取邮件、通过 SMTP 发送邮件，无需额外 Python 包依赖。
- 集成点：`channel_config.yaml` 中 `channels.email.imap_host`、`imap_username`、`imap_password`、`smtp_host`、`smtp_username`、`smtp_password` 均为必填，`consent_granted: true` 表示用户同意，`allow_from` 控制发件人白名单。