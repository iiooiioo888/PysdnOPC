# =============================================================================
# OpenOPC Container Image
# =============================================================================
# Stages:
#   frontend — Node.js build of the Office UI React frontend
#   base     — minimal production image with `opc` CLI + pre-built frontend
#   dev      — development image with Office UI server (aiohttp) + Playwright
#
# Build:
#   docker build -t openopc .                (default = dev)
#   docker build --target base -t openopc .  (minimal CLI + frontend assets)
# =============================================================================

# ── Stage: frontend ─────────────────────────────────────────────────────────
FROM node:20-slim AS frontend

WORKDIR /build

# Copy only what npm needs first (layer cache for dependencies)
COPY opc/plugins/office_ui/frontend_src/package.json \
     opc/plugins/office_ui/frontend_src/package-lock.json* \
     ./

RUN npm install --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund --legacy-peer-deps

# Copy the full frontend source and build
COPY opc/plugins/office_ui/frontend_src/ ./

RUN npm run build

# ── Stage: base ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

LABEL org.opencontainers.image.source="https://github.com/${GITHUB_REPOSITORY}"
LABEL org.opencontainers.image.description="OpenOPC — AI-native company runtime"

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install the package (no dev extras, no pip cache)
COPY pyproject.toml README.md ./
COPY opc/ opc/
COPY config/ config/
COPY skills/ skills/
COPY scripts/ scripts/

RUN pip install --no-cache-dir .

# Overlay the freshly-built frontend assets from the frontend stage
# (vite outDir is '../frontend_dist' relative to /build, i.e. /frontend_dist)
COPY --from=frontend /frontend_dist/ /app/opc/plugins/office_ui/frontend_dist/

# Create non-root user
RUN useradd --create-home --uid 1000 opc \
    && chown -R opc:opc /app
USER opc

# Smoke-test: verify the CLI entrypoint resolves
RUN opc --help > /dev/null

ENTRYPOINT ["opc"]

# ── Stage: dev ───────────────────────────────────────────────────────────────
# Adds aiohttp (Office UI server) and Playwright system dependencies.
FROM base AS dev

USER root

# Install aiohttp for Office UI + system libs for Playwright/Chromium
RUN pip install --no-cache-dir "aiohttp>=3.9.0" \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
       libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
       libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
       libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
       fonts-liberation xdg-utils wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright browsers (Chromium only for smaller image)
RUN python -m playwright install chromium --with-deps 2>/dev/null || true

USER opc

# Default: launch Office UI
EXPOSE 8765
CMD ["ui", "--host", "0.0.0.0", "--port", "8765"]
