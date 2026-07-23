# AGENTS.md — OpenOPC

## Project Positioning

OpenOPC is an AI-native company runtime: it recruits role-specific AI employees,
orchestrates task execution through a work-item state machine, and accumulates
organizational memory. Two execution modes exist — **Task Mode** (single-agent)
and **Company Mode** (multi-role collaboration with delegation, review, and
escalation). Python ≥ 3.10, MIT license, CLI + Office UI (React + Phaser).

## Directory Responsibilities

| Path | Responsibility |
|---|---|
| `opc/core/` | Shared models, config loading, events, employee registry, org config |
| `opc/engine/` | Execution engine: task mode, company mode, checkpoints, staffing, external agent |
| `opc/layer0_interaction/` | CLI entry, Office UI WebSocket handler, message bus |
| `opc/layer1_perception/` | Context loading, routing metadata, context assembly |
| `opc/layer2_organization/` | Work-item planning, company runtime, comms, approval, phase machine, recruitment |
| `opc/layer3_agent/` | Native runtime, external agent broker, preflight checks, skill installer |
| `opc/layer4_tools/` | Shell, file ops, browser (Playwright), web search, Python exec, git, collaboration |
| `opc/layer5_memory/` | Markdown memory, session compaction, preferences, skill library |
| `opc/layer6_observability/` | Events, cost tracking, structured logs, UI/runtime snapshots |
| `opc/database/` | SQLite stores: sessions, tasks, work items, collaboration, delegation |
| `opc/channels/` | External messaging providers (Feishu, Telegram, Slack, Discord, etc.) |
| `opc/cli/` | Typer CLI app, command groups |
| `opc/plugins/office_ui/` | Office UI backend + `frontend_src/` (React) → `frontend_dist/` |
| `opc/market/` | Architecture presets, talent packs, `.opcpkg` import/export |
| `config/` | Template configs copied by `opc init` into `.opc/config/` |
| `skills/core/` | Built-in skill markdown files (coding, deployment, writing, etc.) |
| `tests/` | Pytest suite (unit + integration) |
| `scripts/` | Operational helpers (e.g. `reset_stuck_task.py`) |
| `.github/workflows/` | CI: `external-agent-smoke.yml`, `full-test.yml` |

## High-Risk Areas ⚠️

These modules have broad blast radius — changes here require full test runs:

| Area | Why high-risk | Key files |
|---|---|---|
| Work-item state machine | Phase transitions drive kanban, ownership, runnability | `layer2_organization/phase.py`, `work_item_transition.py`, `work_item_runtime_invariants.py` |
| Company runtime orchestration | Multi-role scheduling, delegation DAG, escalation | `layer2_organization/company_mode.py`, `company_runtime.py`, `engine/_company_mode.py` |
| Approval & shell safety | Security boundary for tool execution | `layer2_organization/approval.py`, `shell_safety.py` |
| Database store & migrations | Schema changes break all persisted state | `database/store.py`, `_store_work_items.py` |
| Engine core & checkpoints | Session recovery, resume, durable execution | `engine/_core.py`, `engine/_checkpoints.py` |
| External agent broker | Subprocess lifecycle, session continuity | `layer3_agent/external_broker.py`, `engine/_external_agent.py` |
| Config loading | All runtime behavior depends on config shape | `core/config.py` |

## Common Task Paths

| Task | Entry point |
|---|---|
| Add/modify a native tool | `opc/layer4_tools/` → register in tool registry |
| Add a channel provider | `opc/channels/` + `config/channel_config.yaml` |
| Modify company-mode phase logic | `opc/layer2_organization/phase.py` + `phase_hooks.py` |
| Change work-item transitions | `opc/layer2_organization/work_item_transition.py` |
| Add CLI command | `opc/cli/app.py` (Typer groups) |
| Modify Office UI | `opc/plugins/office_ui/frontend_src/` → `npm run build` |
| Add/modify skill | `skills/core/*.md` |
| Modify LLM routing | `opc/llm/` + `config/llm_config.yaml` |
| Add org architecture preset | `opc/market/` + `config/company_corporate_config.yaml` |

## Verification Commands

| Scope | Command |
|---|---|
| Full test suite | `python -m pytest` |
| CI smoke (matches GitHub Actions) | `python -m pytest -q tests/test_cli_app.py::CliInitProjectTests::test_external_agent_preflight_accepts_fake_agent_binaries tests/test_external_agent_preflight.py` |
| Single test file | `python -m pytest tests/test_<name>.py -q` |
| Type-check frontend | `cd opc/plugins/office_ui/frontend_src && npm run typecheck` |
| Build frontend | `cd opc/plugins/office_ui/frontend_src && npm run build` |
| Install (editable) | `pip install -e .` |
| Setup from lock file | `make setup` or `uv sync --frozen` |
| Regenerate lock file | `make lock` or `uv lock` |
| Init runtime | `python -m opc.cli.app init` or `opc init` |
| Reset stuck task | `python scripts/reset_stuck_task.py --project <p> --session <id> --apply` |

### Mandatory Execution Rules

When the user requests testing (e.g. "全面测试", "run full tests", "run all
tests", "跑一下测试", "执行测试"), the agent **MUST**:

1. **Actually execute** the corresponding pytest command via the terminal —
   never merely describe or reference the command without running it.
2. Use `python -m pytest tests/ -q` for a full-suite request.
3. **Return a visible result summary** including: total tests run, passed,
   failed, errors, and elapsed time.
4. If failures occur, list the failing test names and brief error messages.

Do NOT substitute a description of what the command would do for actual
execution. The user expects to see real pytest output.

## CI Workflow

Two workflows run on **PR + manual dispatch** (matrix: `ubuntu-latest`, `macos-latest`, `windows-latest` / Python 3.11):

### `external-agent-smoke.yml`
- Steps: `pip install -e .` → targeted pytest (external-agent preflight)
- Scope: `test_cli_app.py::CliInitProjectTests::test_external_agent_preflight_accepts_fake_agent_binaries` + `test_external_agent_preflight.py`

### `full-test.yml`
- **Stage 1 (high-risk):** runs 25 test files covering phase state machine, work-item transitions, company collaboration/reorg/review, approval engine, shell safety, store migrations, engine recovery, external agent, runtime config, and org config.
- **Stage 2 (full-suite):** runs the entire `tests/` directory (depends on Stage 1 passing).

High-risk areas above are covered by the `full-test.yml` high-risk stage; run the
full suite locally before merging changes to those modules.

## Deeper Links

| Topic | Location |
|---|---|
| Full README (EN) | `README.md` |
| Full README (繁體中文) | `README.zh-CN.md` |
| CLI & slash commands | `docs/cli-chat-slash.md` |
| Channel configuration | `docs/channels.md`, `docs/channel-bridges.md` |
| Company metadata ownership | `docs/company-metadata-ownership.md` |
| Build config | `pyproject.toml` |
| Dependency lock file | `uv.lock` (generated by `uv lock`) |
| Config templates | `config/` |
| Skill definitions | `skills/core/` |
