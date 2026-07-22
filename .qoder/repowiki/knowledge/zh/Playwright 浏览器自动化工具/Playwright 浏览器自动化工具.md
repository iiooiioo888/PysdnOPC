---
kind: external_dependency
name: Playwright 浏览器自动化工具
slug: playwright
category: external_dependency
category_hints:
    - vendor_identity
    - client_constraint
scope:
    - '**'
---

### Playwright
- 角色：原生浏览器工具集（`browser_navigate`、`browser_snapshot`、`browser_click` 等）的底层引擎，支持嵌入式 Chromium 或外部 Chrome。
- 集成点：`config/system_config.yaml` 的 `system.browser.*` 控制启动模式（embedded/chrome/auto）、headless、chrome_channel、user_data_dir 等；`opc/layer4_tools/browser.py` 暴露工具接口。
- 约束：需先执行 `python -m playwright install chromium` 安装浏览器二进制；Windows 下 sandbox 模式为 elevated，Linux/macOS 为 workspace-write。