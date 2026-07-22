---
kind: build_system
name: Hatchling 构建与可选依赖分发
category: build_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - .github/workflows/external-agent-smoke.yml
---

本项目采用 Python 单仓结构，以 hatchling 作为唯一构建后端，通过 `pyproject.toml` 集中声明包元数据、依赖与安装入口。核心要点如下：

1. 构建系统
- 构建后端：`hatchling.build`，要求 Python ≥3.10。
- 包名与入口：包名为 `opc`，暴露 CLI 可执行 `opc`，指向 `opc.cli.app:main`。
- Wheel 打包：`packages = ["opc"]`，并通过 `force-include` 将根级 `config/` 和 `skills/core/` 分别映射为安装包内的 `opc/config_templates` 与 `opc/skills_assets/core`，供运行时 `opc init` 及默认技能加载使用。
- sdist 包含范围：显式 include opc、config、skills/core、docs、scripts、README.md、pyproject.toml，保证源码发行版完整。

2. 依赖与可选特性（extras）
- 基础依赖涵盖 LLM 调用（litellm）、CLI（typer[all]、rich、prompt-toolkit）、配置（pydantic-settings、pyyaml）、异步 IO（anyio、httpx）、存储（aiosqlite、chromadb）、文档处理（python-docx/openpyxl/python-pptx）、浏览器自动化（playwright）以及 MCP 客户端（mcp）等。
- 可选 extras 按功能域切分：`cli-board`（Textual TUI）、`channels-*`（各消息通道 SDK）、`channels-all` / `all`（聚合所有可选依赖），便于按需安装最小化或全量环境。
- 遗留别名 `browser = []` 保留向后兼容，实际浏览器工具已随基础包安装。

3. 测试与 CI
- 测试框架：pytest（从 `.gitignore` 中的 `.pytest_cache/`、`.coverage`、`coverage.xml` 可见项目支持覆盖率输出）。
- CI 仅包含一个 GitHub Actions 工作流 `external-agent-smoke.yml`：在 ubuntu/macos/windows 三平台矩阵上 checkout → setup-python@v5 (3.11) → `pip install -e .` → 运行一组外部代理预检 smoke 用例，用于快速验证跨平台可安装性与关键路径。
- 无 Makefile/Dockerfile/tox/nox 等脚本；本地开发通常直接 `pip install -e ".[all]"` 或 `pip install -e ".[channels-all,cli-board]"` 安装。

4. 约定与建议
- 新增可选能力应遵循现有 extras 命名规范（如 `channels-xxx`、`cli-xxx`），并在 `[project.optional-dependencies]` 中声明，必要时加入 `channels-all` / `all` 聚合集。
- 需要随包分发的静态资源（配置模板、技能提示词等）应在 `[tool.hatch.build.targets.wheel.force-include]` 中注册，确保 wheel 内路径与运行时导入一致。
- 若引入新的代码质量/类型检查工具，建议在 pyproject.toml 的 `[tool.xxx]` 段统一配置，避免散落至独立配置文件。