#!/bin/bash
# =============================================================================
# OpenOPC Container Startup Script
# =============================================================================
# This script runs on every container start to:
#   1. Auto-fix LLM config (switch to MiMo API)
#   2. Disable external agents (Codex/Claude Code/Cursor/OpenCode)
#   3. Configure Qwen Code authentication
# =============================================================================

set -e

OPC_HOME="${OPC_HOME:-/app/.opc}"
CONFIG_DIR="${OPC_HOME}/config"

echo "[start.sh] Applying runtime configuration patches..."

# ── 1. Fix LLM config → MiMo API ────────────────────────────────────────────
LLM_CONFIG="${CONFIG_DIR}/llm_config.yaml"
if [ -f "$LLM_CONFIG" ]; then
    echo "[start.sh] Patching LLM config → MiMo API (openai/mimo-v2.5-pro)"
    # Replace default_model and api_base for MiMo
    sed -i 's|default_model:.*|default_model: "openai/mimo-v2.5-pro"|' "$LLM_CONFIG"
    sed -i 's|api_base:.*|api_base: "https://token-plan-sgp.xiaomimimo.com/v1"|' "$LLM_CONFIG"
    # Set api_key_env to MIMO_API_KEY
    if grep -q "api_key_env:" "$LLM_CONFIG"; then
        sed -i 's|api_key_env:.*|api_key_env: "MIMO_API_KEY"|' "$LLM_CONFIG"
    else
        sed -i '/api_key:/a\  api_key_env: "MIMO_API_KEY"' "$LLM_CONFIG"
    fi
    # Update tier_routing to use MiMo model
    sed -i 's|critical:.*|critical: "openai/mimo-v2.5-pro"|' "$LLM_CONFIG"
    sed -i 's|reasoning:.*|reasoning: "openai/mimo-v2.5-pro"|' "$LLM_CONFIG"
    sed -i 's|routine:.*|routine: "openai/mimo-v2.5-pro"|' "$LLM_CONFIG"
    sed -i 's|summary:.*|summary: "openai/mimo-v2.5-pro"|' "$LLM_CONFIG"
    # Update degrade_chain
    sed -i '/degrade_chain:/,/^[^ ]/{s|".*"|"openai/mimo-v2.5-pro"|g}' "$LLM_CONFIG"
    echo "[start.sh] LLM config patched."
else
    echo "[start.sh] WARNING: $LLM_CONFIG not found, skipping LLM patch."
fi

# ── 2. External agents are now configured via GUI Settings page ────────────
# (No longer hardcoded here — use Office UI → Settings → External Agents)
echo "[start.sh] External agent config managed via GUI."

# ── 3. Configure Qwen Code authentication ───────────────────────────────────
echo "[start.sh] Configuring Qwen Code auth (--auth-type openai + MiMo API key)..."
# Set QWEN_CODE_AUTH_TYPE env if MIMO_API_KEY is available
if [ -n "${MIMO_API_KEY}" ]; then
    export QWEN_CODE_AUTH_TYPE="openai"
    export DASHSCOPE_API_KEY="${MIMO_API_KEY}"
    echo "[start.sh] Qwen Code auth configured with MIMO_API_KEY."
else
    echo "[start.sh] WARNING: MIMO_API_KEY not set; Qwen Code auth may fail."
fi

# ── 4. Apply MiMo compat patch at runtime (idempotent) ──────────────────────
if [ -f /app/scripts/patch_mimo_compat.py ]; then
    python3 /app/scripts/patch_mimo_compat.py 2>/dev/null || true
fi

# ── 5. Apply no-rebuild patch (idempotent) ──────────────────────────────────
if [ -f /app/scripts/patch_no_rebuild.py ]; then
    python3 /app/scripts/patch_no_rebuild.py 2>/dev/null || true
fi

echo "[start.sh] All patches applied. Starting OpenOPC..."
echo "────────────────────────────────────────────────────────────────"

# Execute the main command (passed as arguments)
exec "$@"
