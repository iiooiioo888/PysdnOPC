#!/usr/bin/env python3
"""Patch the Office UI plugin to skip runtime frontend rebuild.

This script modifies opc/plugins/office_ui/__init__.py so that
_frontend_needs_rebuild() always returns False, preventing the
mtime-based check from triggering an npm rebuild at runtime.

Idempotent: safe to run multiple times.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent.parent / "opc" / "plugins" / "office_ui" / "__init__.py"

_PATCH_MARKER = "# PATCHED: skip runtime rebuild"


def main() -> int:
    if not _TARGET.exists():
        print(f"[patch_no_rebuild] WARNING: {_TARGET} not found, skipping.")
        return 0

    content = _TARGET.read_text(encoding="utf-8")

    # Already patched?
    if _PATCH_MARKER in content:
        print("[patch_no_rebuild] Already patched, no changes needed.")
        return 0

    # Replace the _frontend_needs_rebuild function body to always return False
    pattern = r'(def _frontend_needs_rebuild\(\) -> bool:)\s*\n(.*?)(?=\n\ndef |\nclass |\Z)'
    replacement = (
        r"\1\n"
        f"    {_PATCH_MARKER}\n"
        r"    return False\n"
    )

    new_content, count = re.subn(pattern, replacement, content, flags=re.DOTALL)

    if count == 0:
        print("[patch_no_rebuild] WARNING: Could not find _frontend_needs_rebuild function.")
        return 1

    _TARGET.write_text(new_content, encoding="utf-8")
    print("[patch_no_rebuild] Patched _frontend_needs_rebuild() → always returns False.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
