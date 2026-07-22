---
kind: frontend_style
name: OpenOPC Office UI 前端样式体系
category: frontend_style
scope:
    - '**'
source_files:
    - opc/plugins/office_ui/frontend_src/index.css
    - opc/plugins/office_ui/frontend_src/chat/chat.css
    - opc/plugins/office_ui/frontend_src/workspace/workspace.css
    - opc/plugins/office_ui/frontend_src/org/marketplace.css
    - opc/plugins/office_ui/frontend_src/org/structure.css
    - opc/plugins/office_ui/frontend_src/org/team.css
    - opc/plugins/office_ui/frontend_src/vite.config.ts
    - opc/plugins/office_ui/frontend_src/package.json
---

## 系统概览

OpenOPC 的前端界面（Office UI）是一个基于 **React + Vite** 的独立单页应用，位于 `opc/plugins/office_ui/frontend_src/`。它通过 WebSocket 与 Python 后端通信，提供聊天、看板、组织架构图编辑、游戏化办公室等交互界面。

## 技术栈与构建

- **框架**: React 19 + TypeScript 5.9
- **构建工具**: Vite 7，使用 `@vitejs/plugin-react`
- **打包输出**: 构建产物输出到 `frontend_dist/`，由 Python 后端静态托管
- **关键依赖**: Phaser 3（2D 游戏引擎）、@xyflow/react（流程图）、@hello-pangea/dnd（拖拽）、@tanstack/react-table（表格）、react-markdown（Markdown 渲染）

## CSS 架构与方法论

### 设计令牌（Design Tokens）

所有主题色值通过 CSS 自定义属性在 `.app-shell` 上集中声明：

```css
:root { --bg, --text, --accent, --green, --yellow, --red, --radius, ... }
```

支持多套主题变体，通过类名切换：
- `.theme-neon` — 霓虹绿
- `.theme-paper` — 浅色纸张风格  
- `.theme-retro` — 复古终端
- `.theme-terminal` — 纯黑终端
- `.theme-cozy` — 暖棕舒适
- `.theme-openopc` — OpenOPC 品牌主题（赤陶色强调色）

### 模块化 CSS 组织

采用按功能域拆分的 CSS 文件，通过 `index.css` 统一导入：

| 文件 | 职责 |
|------|------|
| `index.css` | 全局布局、主题令牌、Topbar、Sidebar、Canvas |
| `chat/chat.css` | 会话列表、消息流、任务头栏、输入框 |
| `workspace/workspace.css` | 三列工作区布局、上下文面板、卡片网格 |
| `kanban/kanban.css` | 看板视图、卡片拖拽、列布局 |
| `org/config.css` | 配置导入导出面板 |
| `org/marketplace.css` | 人才市场、架构市场、包管理 |
| `org/structure.css` | 组织结构图、React Flow 画布、角色编辑器 |
| `org/team.css` | 团队视图、工作流编辑器、我的组织 |

### 响应式策略

- **容器查询优先**: 大量使用 `@container ctxpanel (max-width: ...)` 而非视口媒体查询，因为右侧面板宽度可拖拽调整
- **CSS Grid + Flexbox**: 主布局使用 Grid（`.main-grid`），组件内部多用 Flexbox
- **渐进降级**: 窄屏下自动折叠次要信息（如 `.task-projection-pill`、`.task-header-time` 在小面板中隐藏）

### 视觉风格约定

- **圆角阶梯**: `--radius-xs`(6px) → `--radius-sm`(8px) → `--radius`(12px)
- **阴影层级**: 使用 `color-mix()` 动态生成半透明阴影，避免硬编码颜色
- **状态色语义**: `var(--green)` 成功/运行、`var(--yellow)` 警告/阻塞、`var(--red)` 错误/失败、`var(--accent)` 强调/选中
- **字体栈**: Inter → SF Pro Display → -apple-system → Segoe UI
- **代码字体**: JetBrains Mono / SF Mono / Menlo / Consolas

### 第三方库样式覆盖

针对 React Flow、Phaser 等库进行主题适配：

```css
.react-flow__background { background: color-mix(...) !important; }
.canvas-wrap canvas { image-rendering: pixelated; }
```

## 开发者规范

1. **所有颜色必须使用 CSS 变量**，禁止硬编码十六进制色值
2. **新增主题只需定义一组 `--*` 变量**，无需修改组件样式
3. **响应式优先使用容器查询**（`@container ctxpanel`），其次才是视口媒体查询
4. **组件样式按功能域拆分到对应 CSS 文件**，保持单一职责
5. **使用 `color-mix()` 替代硬透明度**，确保主题一致性
6. **图标使用 SVG mask 或 emoji**，避免额外图片资源
7. **动画过渡统一使用 120-180ms ease**，避免突兀变化

## 构建与部署

Vite 配置将 `phaser` 单独分包为 `manualChunks`，减少首屏加载体积。构建产物直接复制到 `frontend_dist/`，由 Python HTTP 服务器静态提供服务。
