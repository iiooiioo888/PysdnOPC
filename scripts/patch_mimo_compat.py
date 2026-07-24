#!/usr/bin/env python3
"""Patch the LLM provider for MiMo API compatibility.

MiMo API (token-plan-sgp.xiaomimimo.com) expects `max_completion_tokens`
instead of the standard `max_tokens` parameter. This script patches
opc/llm/provider.py to rename the parameter when the target endpoint
is a MiMo-compatible API.

Idempotent: safe to run multiple times.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent.parent / "opc" / "llm" / "provider.py"

_PATCH_MARKER = "# MIMO_COMPAT_PATCH"

# The helper function to inject after the imports section
_HELPER_FUNC = '''
# ── MiMo API compatibility ──────────────────────────────────────────────────
# MiMo endpoints expect max_completion_tokens instead of max_tokens.
# MIMO_COMPAT_PATCH
_MIMO_API_HINTS = ("xiaomimimo", "mimo")


def _apply_mimo_compat(call_kwargs: dict) -> dict:
    """Rename max_tokens → max_completion_tokens for MiMo-compatible endpoints."""
    api_base = call_kwargs.get("api_base", "")
    model = call_kwargs.get("model", "")
    is_mimo = any(hint in api_base.lower() for hint in _MIMO_API_HINTS) or \\
              "mimo" in model.lower()
    if is_mimo and "max_tokens" in call_kwargs:
        call_kwargs["max_completion_tokens"] = call_kwargs.pop("max_tokens")
    return call_kwargs
'''


def main() -> int:
    if not _TARGET.exists():
        print(f"[patch_mimo_compat] WARNING: {_TARGET} not found, skipping.")
        return 0

    content = _TARGET.read_text(encoding="utf-8")

    # Already patched?
    if _PATCH_MARKER in content:
        print("[patch_mimo_compat] Already patched, no changes needed.")
        return 0

    # 1. Inject the helper function after the litellm.drop_params line
    anchor = "litellm.drop_params = True"
    if anchor not in content:
        print("[patch_mimo_compat] WARNING: Could not find anchor 'litellm.drop_params = True'.")
        return 1

    content = content.replace(anchor, anchor + "\n" + _HELPER_FUNC, 1)

    # 2. Patch call_kwargs construction sites to apply the compat function
    # Pattern: after building call_kwargs dict, before the litellm call
    # We add _apply_mimo_compat(call_kwargs) right before each litellm.acompletion call
    pattern = r'(call_kwargs: dict\[str, Any\] = \{[^}]+\}[^;]*?\n(?:.*\n)*?)(\s+stream = await litellm\.acompletion|(\s+)stream = litellm\.completion|\s+response = await litellm\.acompletion|\s+response = litellm\.completion)'

    # Simpler approach: find lines with `await litellm.acompletion(**call_kwargs)` or similar
    # and insert the compat call before them
    content = re.sub(
        r'(\n)(\s+)(stream = await litellm\.acompletion\(\*\*call_kwargs\))',
        r'\1\2call_kwargs = _apply_mimo_compat(call_kwargs)\n\2\3',
        content,
    )
    content = re.sub(
        r'(\n)(\s+)(response = await litellm\.acompletion\(\*\*call_kwargs\))',
        r'\1\2call_kwargs = _apply_mimo_compat(call_kwargs)\n\2\3',
        content,
    )
    content = re.sub(
        r'(\n)(\s+)(stream = await litellm\.acompletion\(\*\*retry_kwargs\))',
        r'\1\2retry_kwargs = _apply_mimo_compat(retry_kwargs)\n\2\3',
        content,
    )
    content = re.sub(
        r'(\n)(\s+)(response = await litellm\.acompletion\(\*\*retry_kwargs\))',
        r'\1\2retry_kwargs = _apply_mimo_compat(retry_kwargs)\n\2\3',
        content,
    )

    _TARGET.write_text(content, encoding="utf-8")
    print("[patch_mimo_compat] Patched provider.py for MiMo API compatibility.")
    print("  - Injected _apply_mimo_compat() helper")
    print("  - Patched litellm.acompletion call sites")
    return 0


if __name__ == "__main__":
    sys.exit(main())
