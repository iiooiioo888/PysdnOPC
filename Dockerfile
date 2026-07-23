# =============================================================================
# OpenOPC Container Image
# =============================================================================
# Stages:
#   base — minimal production image with `opc` CLI
#   dev  — development image with Office UI (aiohttp) + Playwright browsers
#
# Build:
#   docker build -t openopc .              (production, default=dev)
#   docker build --target base -t openopc .  (minimal CLI only)
# =============================================================================

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

# Smoke-test: verify the CLI entrypoint resolves
RUN opc --help > /dev/null

ENTRYPOINT ["opc"]

# ── Stage: dev ───────────────────────────────────────────────────────────────
# Adds aiohttp (Office UI server) and Playwright system dependencies.
FROM base AS dev

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

# Default: launch Office UI
EXPOSE 8765
CMD ["ui", "--host", "0.0.0.0", "--port", "8765"]
