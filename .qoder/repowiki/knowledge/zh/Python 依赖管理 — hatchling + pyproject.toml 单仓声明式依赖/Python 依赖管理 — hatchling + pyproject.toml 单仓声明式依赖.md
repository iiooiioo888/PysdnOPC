---
kind: dependency_management
name: Python 依赖管理 — hatchling + pyproject.toml 单仓声明式依赖
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - .github/workflows/external-agent-smoke.yml
    - opc/layer3_agent/runtime_v2/worktree.py
    - .gitignore
---

## 1. 使用的系统/方法
- **构建后端**：Hatchling（`[build-system] requires = ["hatchling"]`），通过 `pyproject.toml` 声明式管理。
- **依赖声明**：所有 Python 第三方依赖集中在根目录 `pyproject.toml` 的 `[project.dependencies]` 与 `[project.optional-dependencies]` 中，未使用 `requirements.txt`、`Pipfile`、`poetry.lock` 或 `uv.lock` 作为权威来源（`.gitignore` 中仅忽略 `uv.lock`，表明项目不提交 uv lockfile）。
- **可选依赖分组**：按功能域拆分为 `cli-board`、`channels-*`（telegram / whatsapp / discord / feishu / mochat / dingtalk / slack / qq / matrix）、`channels-all`、`all` 等 extras，安装时按需选择。
- **可执行入口**：通过 `[project.scripts] opc = "opc.cli.app:main"` 暴露 `opc` CLI。
- **前端子工程**：Office UI 前端位于 `opc/plugins/office_ui/frontend_src/package.json`，使用独立的 npm 包管理（见 `.gitignore` 忽略 `/package.json` 以及测试中对 `package.json` 的引用）。该前端是独立于 Python 包的子模块，由 Vite 构建产物 `frontend_dist/` 被 force-include 进 wheel。
- **运行时环境探测**：`opc/layer3_agent/runtime_v2/worktree.py` 在智能体工作树中自动识别 `requirements.txt` / `requirements-dev.txt` / `requirements-test.txt` / `requirements-ci.txt`，用于为外部 agent 生成运行环境；但 OpenOPC 自身并不依赖这些文件。
- **CI 安装方式**：GitHub Actions 使用 `python -m pip install -e .` 从源码安装，直接读取 `pyproject.toml`。

## 2. 关键文件
- `pyproject.toml` — 唯一权威依赖清单、构建配置、extras 定义
- `.github/workflows/external-agent-smoke.yml` — CI 中 `pip install -e .` 的安装入口
- `opc/layer3_agent/runtime_v2/worktree.py` — 运行时对 `requirements*.txt` 的探测逻辑
- `opc/plugins/office_ui/frontend_src/package.json` — 前端子工程的 npm 依赖声明
- `.gitignore` — 忽略 `uv.lock`、`/package.json`，确认不提交 lockfile

## 3. 架构与约定
- **单一事实源**：Python 依赖只存在于 `pyproject.toml`，无 vendoring、无私有 PyPI 镜像配置、无 `setup.py`/`setup.cfg` 遗留。
- **可选依赖即插件**：各消息通道（Telegram、WhatsApp、Discord、飞书、Mochat、钉钉、Slack、QQ、Matrix）以 `channels-*` extras 形式提供，默认不安装，降低基础包体积。
- **前端与后端解耦**：Office UI 前端拥有独立 `package.json`，构建产物通过 hatch `force-include` 打包进 wheel，前端依赖不由 Python 层管理。
- **不提交 lockfile**：仓库未包含 `requirements.lock`、`poetry.lock`、`uv.lock` 或 `go.sum`，依赖版本由 `>=` 宽松约束驱动，锁定策略不在本仓库内实现。

## 4. 开发者应遵循的规则
1. **新增 Python 依赖**：一律写入根 `pyproject.toml` 的 `[project.dependencies]` 或对应 `[project.optional-dependencies]` 分组，不要新建 `requirements.txt`。
2. **可选依赖归类**：非核心功能（如某个 channel SDK、CLI board TUI）放入 `optional-dependencies`，并为其创建清晰的 extra 名称（参考现有 `channels-*` 命名模式）。
3. **版本号约束**：保持与现有风格一致——核心依赖用 `>=X.Y` 宽松下限，对需要严格对齐的版本（如 `litellm==1.82.1`）才使用固定版本。
4. **前端依赖**：涉及 Office UI 前端的变更在 `opc/plugins/office_ui/frontend_src/package.json` 中维护，并在构建后重新生成 `frontend_dist/`。
5. **CI 一致性**：任何依赖变更需确保 `pip install -e .` 能在 GitHub Actions 环境中成功安装。