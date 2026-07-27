#!/usr/bin/env python3
"""Lightweight config consistency checker.

Validates:
1. YAML syntax: every config/*.yaml file parses without errors.
2. Env-var cross-reference: every variable declared in .env.example is
   referenced (in values or comments) by at least one config/*.yaml file.

Exit codes:
  0 — all checks pass
  1 — one or more checks failed (details printed to stderr)

Usage:
  python scripts/check_config_consistency.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit(
        "ERROR: PyYAML is required. Install with: pip install pyyaml"
    )

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_DIR = ROOT / "config"

# Env vars that are purely internal runtime injections (set by the engine,
# not expected to appear in config templates). Listed in .env.example under
# "Internal Runtime" section for debugging reference only.
INTERNAL_RUNTIME_PREFIXES = (
    "OPC_COLLAB_",
    "OPC_PROJECT_",
    "OPC_TASK_",
    "OPC_WORKSPACE_",
    "OPC_COMMS_",
    "OPC_EXTERNAL_",
    "OPC_ALLOWED_",
)

# Env vars that are system-level overrides, not tied to a specific YAML key.
SYSTEM_LEVEL_VARS = {
    "OPC_HOME",
    "XDG_CONFIG_HOME",
    "CODEX_HOME",
    "QWEN_CODE_BIN",
    "OPENCODE_BIN",
    "OPENCODE_CONFIG_DIR",
}


def parse_env_vars(env_path: Path) -> list[str]:
    """Extract variable names from .env.example (active + commented)."""
    pattern = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=")
    variables: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line.strip())
        if m:
            variables.append(m.group(1))
    return variables


def load_config_texts(config_dir: Path) -> dict[str, str]:
    """Return {filename: raw_text} for all YAML files in config_dir."""
    texts: dict[str, str] = {}
    for yaml_file in sorted(config_dir.glob("*.yaml")):
        texts[yaml_file.name] = yaml_file.read_text(encoding="utf-8")
    return texts


def validate_yaml_syntax(config_texts: dict[str, str]) -> list[str]:
    """Check that each YAML file parses without errors."""
    errors: list[str] = []
    for filename, text in config_texts.items():
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            errors.append(f"  YAML syntax error in config/{filename}: {exc}")
    return errors


def check_env_references(
    env_vars: list[str], config_texts: dict[str, str]
) -> list[str]:
    """Check each env var is referenced in at least one config YAML."""
    # Build a combined search corpus (all config file contents)
    combined = "\n".join(config_texts.values())

    errors: list[str] = []
    for var in env_vars:
        # Skip internal runtime vars (documented for reference only)
        if any(var.startswith(p) for p in INTERNAL_RUNTIME_PREFIXES):
            continue
        # Skip system-level overrides
        if var in SYSTEM_LEVEL_VARS:
            continue
        # Check if the variable name appears anywhere in config files
        if var not in combined:
            # Suggest which config file might be the right home
            hint = _suggest_config_file(var)
            errors.append(
                f"  '{var}' declared in .env.example but not referenced "
                f"in any config/*.yaml file.\n"
                f"    Fix: add a reference to '{var}' in the appropriate "
                f"config file (e.g. {hint}), or remove it from .env.example."
            )
    return errors


def _suggest_config_file(var: str) -> str:
    """Heuristic suggestion for which config file should reference the var."""
    lower = var.lower()
    if "api_key" in lower or "model" in lower:
        return "config/llm_config.yaml"
    if any(
        k in lower
        for k in ("telegram", "discord", "slack", "feishu", "dingtalk", "channel")
    ):
        return "config/channel_config.yaml"
    if any(k in lower for k in ("agent", "codex", "qwen", "opencode", "cursor")):
        return "config/agent_config.yaml"
    return "config/system_config.yaml"


def main() -> int:
    if not ENV_EXAMPLE.exists():
        print(f"ERROR: {ENV_EXAMPLE} not found.", file=sys.stderr)
        return 1
    if not CONFIG_DIR.is_dir():
        print(f"ERROR: {CONFIG_DIR} directory not found.", file=sys.stderr)
        return 1

    env_vars = parse_env_vars(ENV_EXAMPLE)
    config_texts = load_config_texts(CONFIG_DIR)

    if not config_texts:
        print("ERROR: no YAML files found in config/.", file=sys.stderr)
        return 1

    all_errors: list[str] = []

    # Check 1: YAML syntax
    yaml_errors = validate_yaml_syntax(config_texts)
    if yaml_errors:
        all_errors.append("[YAML Syntax]")
        all_errors.extend(yaml_errors)

    # Check 2: Env var cross-reference
    ref_errors = check_env_references(env_vars, config_texts)
    if ref_errors:
        all_errors.append("[Env-Config Cross-Reference]")
        all_errors.extend(ref_errors)

    if all_errors:
        print("Config consistency check FAILED:\n", file=sys.stderr)
        for line in all_errors:
            print(line, file=sys.stderr)
        print(
            f"\n{len(env_vars)} env vars checked against "
            f"{len(config_texts)} config files.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Config consistency check PASSED: "
        f"{len(env_vars)} env vars, {len(config_texts)} YAML files OK."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
