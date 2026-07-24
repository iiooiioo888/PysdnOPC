---
version: alpha
name: OpenOPC Office UI
description: AI 公司运行时仪表板 — React + Phaser 多页面单页应用设计契约
colors:
  bg: "#0c111b"
  bg-elevated: "#141b2b"
  text: "#e2e8f0"
  text-secondary: "#8494a7"
  text-dim: "#64748b"
  border: "rgba(148, 163, 184, 0.12)"
  border-hover: "rgba(148, 163, 184, 0.22)"
  surface: "rgba(20, 27, 43, 0.7)"
  surface-hover: "rgba(30, 41, 63, 0.7)"
  accent: "#6366f1"
  accent-soft: "rgba(99, 102, 241, 0.15)"
  accent-glow: "rgba(99, 102, 241, 0.3)"
  green: "#34d399"
  yellow: "#fbbf24"
  red: "#f87171"

typography:
  heading:
    fontFamily: "'Inter', 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: 18px
    fontWeight: 600
    lineHeight: 24px
  body:
    fontFamily: "'Inter', 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 20px
  label:
    fontFamily: "'Inter', 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: 12px
    fontWeight: 600
    lineHeight: 16px
  caption:
    fontFamily: "'Inter', 'SF Pro Display', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: 11px
    fontWeight: 400
    lineHeight: 14px

rounded:
  xs: 6px
  sm: 8px
  md: 12px
  full: 9999px

spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px

components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "#ffffff"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    padding: "{spacing.sm} {spacing.lg}"
  card:
    backgroundColor: "{colors.bg-elevated}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "{spacing.lg}"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.xs}"
    padding: "{spacing.sm} {spacing.md}"
  badge:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent}"
    typography: "{typography.caption}"
    rounded: "{rounded.full}"
    padding: "2px {spacing.sm}"
---

# OpenOPC Office UI — 设计契约

## Overview

Office UI 是 OpenOPC AI 公司运行时的可视化前端，提供任务管理、多角色协作、
组织架构编辑、即时通讯和 2D 办公室动画。技术栈为 React 19 + Vite 7 + Phaser 3，
通过 WebSocket 与 Python 后端实时通信。

本文件是组件架构、模块边界、命名规范、状态管理和视觉设计的唯一权威契约。
所有前端修改必须遵循此契约；偏离需先更新本文件。

构建入口：`opc/plugins/office_ui/frontend_src/` → `npm run build` → `frontend_dist/`

## 目标组件架构

### 当前问题

`App.tsx`（~2860 行）承担了过多职责：WebSocket 连接管理、事件路由、
全局状态、页面切换、侧边栏渲染、开发者工具。需要拆分为职责单一的模块。

### 目标层次

```
main.tsx
└── AppShell (布局骨架 + 主题 + 路由)
    ├── TopBar (导航 + 项目选择 + 模式切换 + 主题)
    ├── pages/
    │   ├── WorkspacePage (聊天 + 看板统一视图)
    │   ├── DashboardPage (交付内容展示 + LLM 对话)
    │   ├── TemplatesPage (组织模板市场)
    │   ├── OrgPage (组织架构编辑 + 人才市场)
    │   ├── SettingsPage (LLM 配置)
    │   └── OfficePage (Phaser 2D 办公室 + 侧边栏)
    ├── overlays/
    │   ├── ExecutionPanel (全局执行详情)
    │   ├── DevToolsOverlay (开发者工具)
    │   └── Toast (全局通知)
    └── game/ (Phaser 引擎，独立于 React 树)
```

### 拆分方向（从 App.tsx 提取）

| 提取目标 | 来源行数范围 | 新文件 |
|---|---|---|
| WebSocket 连接 + 事件路由 | L700–L2300 | `hooks/useWebSocket.ts` |
| 全局 UI 状态（theme, page, sidebar） | L460–L530 | `stores/AppShellStore.ts` |
| 顶栏导航 | L2350–L2423 | `components/TopBar.tsx` |
| 办公室侧边栏 | L2620–L2775 | `office/OfficeSidebar.tsx` |
| 开发者工具面板 | L2778–L2848 | `components/DevToolsOverlay.tsx` |
| Agent 映射工具函数 | L126–L410 | `lib/agentMapping.ts` |

## 模块边界

| 目录 | 职责 | 对外接口 |
|---|---|---|
| `chat/` | 消息列表、输入框、进度卡片、ChatStore | 导出组件 + `useChatStore` |
| `kanban/` | 看板列/卡片、BoardStore、执行面板 | 导出组件 + `useBoardStore` |
| `workspace/` | 统一工作区（聊天+看板+上下文面板） | 导出 `WorkspacePage` |
| `org/` | 组织架构、角色表、人才市场、结构画布 | 导出 `OrgTab` |
| `dashboard/` | 仪表板、LLM 对话面板、模板页 | 导出页面组件 |
| `settings/` | LLM 设置页 | 导出 `LlmSettingsPage` |
| `game/` | Phaser 场景、实体、寻路、GameBridge | 导出 `PhaserGame` + `GameBridge` |
| `stores/` | 全局 store（Session、Project） | 导出 hook |
| `lib/` | 纯函数工具（无 React 依赖优先） | 导出函数 |
| `types/` | TypeScript 类型定义（visual, kanban, chat） | 仅类型导出 |
| `locales/` | i18n 翻译字典 | 导出字典对象 |
| `components/` | 跨模块共享 UI 组件 | 导出组件 |

### 依赖规则

- `lib/` 和 `types/` 不依赖任何 React 组件或 store。
- 页面模块（workspace, org, dashboard, settings）不互相导入。
- `game/` 通过 `GameBridge` 与 React 通信，不直接访问 store。
- WebSocket 逻辑集中在单一 hook，页面通过 props/callback 接收数据。

## 命名规范

### 文件命名

| 类型 | 规则 | 示例 |
|---|---|---|
| React 组件 | PascalCase.tsx | `MessageList.tsx` |
| Store hook | PascalCase.ts（use 前缀导出） | `BoardStore.ts` → `useBoardStore` |
| 纯工具函数 | camelCase.ts | `progressLog.ts` |
| 类型定义 | camelCase.ts（仅 type/interface） | `kanban.ts` |
| 样式 | 模块名.css | `chat.css`, `kanban.css` |
| 测试 | 源文件同名 + `.test.ts(x)` | `ChatStore.test.ts` |

### 导出命名

- 组件：PascalCase（`export function MessageList`）
- Hook：camelCase + use 前缀（`export function useChatStore`）
- 工具函数：camelCase（`export function normalizeProgressLog`）
- 类型：PascalCase interface/type（`export interface Session`）
- 常量：UPPER_SNAKE_CASE（`const MAX_LOG_ITEMS = 80`）

### CSS 类名

- BEM-like 扁平命名：`模块-元素--状态`
- 示例：`.agent-row`, `.agent-row-main`, `.agent-row.selected`
- 主题类：`.theme-{name}` 挂在 `.app-shell` 上

## 状态管理策略

### 当前模式

使用 React 内置 `useReducer` + `useCallback` + `useMemo` 实现自定义 store hook，
无外部状态库。每个 store 是独立 hook，在 App 层调用并通过 props 下发。

### 规则

1. **Store 粒度**：每个业务域一个 store（Session、Board、Chat、Project）。
   不为单个组件创建 store。
2. **数据流**：WebSocket → store dispatch → 组件 re-render。
   组件不直接操作 WebSocket。
3. **跨 store 通信**：通过 App 层（或未来 AppShell）协调，
   store 之间不直接引用。
4. **Ref 镜像**：当 callback 需要最新值但不想重建时，
   使用 `useRef` 镜像 store 状态（如 `sessionStoreRef`）。
5. **临时 UI 状态**：使用组件本地 `useState`，不进入 store。
6. **持久化**：仅 `localStorage` 用于用户偏好（主题、侧边栏折叠）。

### 未来演进

当 App.tsx 拆分完成后，考虑引入 React Context 替代 props drilling，
或采用 Zustand 简化跨模块状态共享。当前阶段不引入新依赖。

## 视觉设计原则

### 主题系统

- 7 个内置主题：midnight（默认）、neon、paper、retro、terminal、cozy、openopc
- 主题通过 CSS 自定义属性（`--bg`, `--text`, `--accent` 等）实现
- 所有颜色必须引用 CSS 变量，禁止硬编码 hex 值（主题变体除外）

### 布局

- 全局网格：`grid-template-rows: 48px minmax(0, 1fr)`（顶栏 + 内容）
- 内容区根据页面切换显示/隐藏（非路由，条件渲染）
- 侧边栏可折叠，宽度固定

### 组件状态

每个交互组件必须考虑以下状态：
- default / hover / active / disabled / loading / error / empty

### 无障碍

- 可操作元素必须有 `aria-label` 或可见文本
- 焦点指示器使用 `--accent` 色 2px outline
- 不仅用颜色传达状态（配合图标或文字）

### Do's and Don'ts

#### Do

- 使用 CSS 变量引用颜色：`var(--accent)`, `var(--text-secondary)`
- 使用 `--radius-sm/md/xs` 统一圆角
- 新组件放入对应模块目录，附带同名 `.css` 或复用模块 CSS
- 组件 props 使用 TypeScript interface 定义，导出供测试使用

#### Don't

- 不在组件内硬编码颜色值（`#6366f1` → `var(--accent)`）
- 不创建超过 300 行的组件文件（拆分）
- 不在 `lib/` 中引入 React 依赖
- 不在页面模块之间直接导入组件
- 不使用 inline style 替代 CSS class（除动态计算值）

## 样式组织

| 文件 | 作用域 |
|---|---|
| `index.css` | 全局 reset + 主题 token + app-shell 布局 |
| `chat/chat.css` | 聊天模块所有样式 |
| `kanban/kanban.css` | 看板模块所有样式 |
| `workspace/workspace.css` | 工作区布局样式 |
| `org/*.css` | 组织模块（org, structure, team, marketplace, config） |
| `dashboard/*.css` | 仪表板 + 模板页样式 |
| `settings/llm-settings.css` | 设置页样式 |

### 规则

- 每个模块目录拥有自己的 CSS 文件，通过 `index.css` 的 `@import` 聚合
- 不使用 CSS-in-JS 或 CSS Modules（当前项目约定）
- 新增模块必须创建对应 CSS 文件并在 `index.css` 中注册

## 验证

修改前端代码后执行：

```bash
cd opc/plugins/office_ui/frontend_src
npm run typecheck   # TypeScript 类型检查
npm run build       # 生产构建
```
