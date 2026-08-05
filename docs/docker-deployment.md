# Docker Deployment

OpenOPC ships a multi-stage `Dockerfile` and a `docker-compose.yml` for running
the Office UI stack with persistent state. This guide covers image stages,
quick start, CLI usage, persistence, configuration, CI image builds, and
troubleshooting.

## Image Stages

| Stage | Contents | Use case |
|---|---|---|
| `frontend` | Node.js 20 build of the Office UI React frontend (Vite). | Build-time only — assets are copied into `base`. |
| `base` | Python 3.11-slim image with the `opc` CLI, pre-built frontend assets, and the `qwen-code` wrapper. Runs as non-root user `opc` (uid 1000). | Headless / CLI-only deployments, smaller footprint. |
| `dev` (default) | Everything in `base` plus the Office UI server (aiohttp), Playwright + Chromium, and required system libraries. Exposes port `8765`. | Full Office UI deployment. |

Build either stage manually:

```bash
# Full dev image (default target)
docker build -t openopc .

# Minimal CLI + frontend assets (no Playwright/aiohttp)
docker build --target base -t openopc .
```

## Quick Start (Docker Compose)

```bash
# 1. Copy the env template and fill in your LLM API key(s)
cp .env.example .env

# 2. Start the stack (builds the dev image on first run)
docker compose up -d

# 3. Open the Office UI
#    http://localhost:8765
```

Compose behavior highlights:

- Builds the `dev` target of the local `Dockerfile`.
- Maps host port `${OPC_UI_PORT:-8765}` → container port `8765`.
- Mounts `./.opc` → `/app/.opc` and sets `OPC_HOME=/app/.opc`, so configs,
  databases, memory, and logs persist on the host.
- Creates a named volume `chromadb_data` for the vector store.
- Loads variables from `.env` if present (optional, not required to start).
- Entrypoint is `start.sh`, which applies runtime config patches before
  launching `opc ui`.
- Includes a healthcheck against `http://localhost:8765/`.

Common operations:

```bash
docker compose logs -f openopc        # follow logs
docker compose exec openopc opc <cmd> # run CLI commands inside the container
docker compose down                   # stop (state stays in ./.opc)
docker compose up -d --build          # rebuild after code changes
```

## Running CLI Commands

The image `ENTRYPOINT` is `opc`, so any CLI command maps directly:

```bash
# One-shot task-mode chat
docker run --rm -v ./.opc:/app/.opc --env-file .env \
  openopc chat -p demo --mode task --agent native "Hello"

# Initialize a fresh runtime home inside the mounted volume
docker run --rm -v ./.opc:/app/.opc openopc init
```

To run the Office UI without Compose:

```bash
docker run -p 8765:8765 -v ./.opc:/app/.opc --env-file .env openopc
```

## Persistence & Data Layout

| Path (in container) | Contents |
|---|---|
| `/app/.opc/config/` | Runtime configs copied by `opc init` (LLM, system, agent, channel, company) |
| `/app/.opc/global.db`, `ui_state.db` | Global SQLite stores |
| `/app/.opc/projects/` | Per-project databases, workspaces, sessions |
| `/app/.opc/memory/`, `.opc/logs/` | Organizational memory and structured logs |
| `/app/.opc/chroma_data` | ChromaDB vector store (named volume in Compose) |

Always mount or volume `/app/.opc` — without it, all state is lost when the
container is removed.

## Environment Variables

Compose passes through `.env` and explicitly forwards `MIMO_API_KEY`. For other
providers, either:

1. add a `ENV_VAR=${ENV_VAR:-}` line under `environment:` in
   `docker-compose.yml`, or
2. run with `--env-file .env` (the whole file is injected, but only variables
   listed in `environment:` are exported unless you rely on `env_file` alone).

Key variables (full list in `.env.example`):

| Variable | Purpose |
|---|---|
| `DEEPSEEK_API_KEY` | Default LLM provider key |
| `MIMO_API_KEY` | MiMo API key; also reused for Qwen Code auth by `start.sh` |
| `DASHSCOPE_API_KEY` | Qwen Token Plan endpoint key |
| `OPC_HOME` | Data directory override (Compose sets it to `/app/.opc`) |
| `OPC_UI_PORT` | Host port for the Office UI (Compose only) |

## Runtime Config Patches (`start.sh`)

When started via Compose, `start.sh` runs before the `opc` command and:

1. Rewrites `.opc/config/llm_config.yaml` to `openai/mimo-v2.5-pro` with the
   MiMo API base URL and `MIMO_API_KEY`.
2. Exports `QWEN_CODE_AUTH_TYPE=openai` and reuses `MIMO_API_KEY` as
   `DASHSCOPE_API_KEY` for Qwen Code.
3. Re-applies the MiMo compatibility and no-rebuild patches (idempotent).

If you use a different provider, edit `.opc/config/llm_config.yaml` directly
and adjust or bypass `start.sh` accordingly (e.g. override `entrypoint:` in a
Compose override file).

## CI Image Build (Smoke Job)

`.github/workflows/full-test.yml` includes a `docker-build` job that runs after
the full test suite passes:

1. Builds the image with Buildx (GHA layer cache).
2. Smoke-tests `docker run openopc:ci --help`.
3. Fails if the image exceeds 500 MB.
4. On push to `main`, pushes `ghcr.io/<owner>/<repo>/openopc:latest`.

To pull the latest published image:

```bash
docker pull ghcr.io/<owner>/<repo>/openopc:latest
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Healthcheck fails / UI unreachable | Check `docker compose logs openopc`; confirm port 8765 is not in use (`OPC_UI_PORT` to change host port). |
| LLM calls fail with auth errors | Ensure the matching API key is set in `.env` and `llm_config.yaml` points at the right `api_key_env`. |
| State lost after `docker compose down` | State lives in `./.opc` on the host — make sure the bind mount exists and was not deleted. |
| First build is slow | The frontend stage runs `npm install` + `npm run build`; subsequent builds hit the layer cache. |
| Playwright browser missing | The `dev` stage installs Chromium with `--with-deps`; rebuild with `--no-cache` if interrupted. |
