#!/usr/bin/env python3
"""PostToolUse hook: post-edit validation enforcement for OpenOPC.

Fires after Write/Edit tool calls. When the edited file falls under
opc/, tests/, or scripts/, runs a targeted pytest validation and logs
a structured telemetry event to .opc/logs/post_edit_validation.jsonl.

Exit codes (Qoder hook contract):
  0 - allow continuation (always; this hook is observational, not blocking)
  2 - block (never used here)

Stdin: JSON event context from Qoder Agent lifecycle.
Stdout: optional advisory feedback (visible to agent).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Path patterns that trigger validation (relative to project root)
WATCHED_PREFIXES = ("opc/", "tests/", "scripts/")

# Maximum pytest wall-clock time (seconds)
PYTEST_TIMEOUT = 180

# Telemetry log location (relative to project root)
TELEMETRY_RELPATH = ".opc/logs/post_edit_validation.jsonl"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    """Resolve project root from this script's location (scripts/hooks/)."""
    return Path(__file__).resolve().parent.parent.parent


def _normalize_path(raw: str, root: Path) -> str:
    """Return a forward-slash relative path for matching."""
    p = Path(raw)
    if p.is_absolute():
        try:
            p = p.relative_to(root)
        except ValueError:
            pass
    return p.as_posix()


def _matches_watched(rel_path: str) -> bool:
    """Check if relative path falls under watched directories."""
    return any(rel_path.startswith(prefix) for prefix in WATCHED_PREFIXES)


def _pick_test_target(rel_path: str) -> str:
    """Choose pytest target based on edited file.

    - tests/test_*.py  -> run that specific test file
    - opc/ or scripts/ -> run full suite quick (-q --tb=short -x)
    """
    if rel_path.startswith("tests/") and rel_path.endswith(".py"):
        return rel_path
    return "tests/ -q --tb=short -x"


def _run_pytest(root: Path, target: str) -> dict:
    """Run pytest and capture result metadata."""
    cmd = [sys.executable, "-m", "pytest"] + target.split()
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT,
            encoding="utf-8",
            errors="replace",
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        # Extract summary line (last non-empty line typically)
        output_lines = proc.stdout.strip().splitlines()
        summary = output_lines[-1] if output_lines else ""
        return {
            "exit_code": proc.returncode,
            "passed": proc.returncode == 0,
            "duration_ms": duration_ms,
            "summary": summary[:500],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "exit_code": -1,
            "passed": False,
            "duration_ms": duration_ms,
            "summary": f"TIMEOUT after {PYTEST_TIMEOUT}s",
            "timed_out": True,
        }
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "exit_code": -2,
            "passed": False,
            "duration_ms": duration_ms,
            "summary": f"ERROR: {exc}",
            "timed_out": False,
        }


def _log_telemetry(root: Path, event: dict) -> None:
    """Append structured validation event to JSONL telemetry log."""
    log_path = root / TELEMETRY_RELPATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    root = _project_root()

    # Read hook event from stdin
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        payload = {}

    # Extract file path from tool_input
    tool_input = payload.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, TypeError):
            tool_input = {}

    file_path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("filePath")
        or ""
    )

    if not file_path:
        # No identifiable file path; skip silently
        return 0

    rel_path = _normalize_path(file_path, root)

    if not _matches_watched(rel_path):
        # Edited file outside watched directories; no validation needed
        return 0

    # --- Validation triggered ---
    test_target = _pick_test_target(rel_path)
    result = _run_pytest(root, test_target)

    # Build telemetry event
    telemetry_event = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "post_edit_validation",
        "file": rel_path,
        "test_target": test_target,
        "passed": result["passed"],
        "exit_code": result["exit_code"],
        "duration_ms": result["duration_ms"],
        "timed_out": result["timed_out"],
        "summary": result["summary"],
    }
    _log_telemetry(root, telemetry_event)

    # Advisory output to agent (visible in session)
    if result["passed"]:
        print(
            f"[post-edit-validation] PASS ({result['duration_ms']}ms) "
            f"target={test_target}"
        )
    else:
        print(
            f"[post-edit-validation] FAIL ({result['duration_ms']}ms) "
            f"target={test_target} | {result['summary']}"
        )

    # Always exit 0: observational hook, does not block agent continuation
    return 0


if __name__ == "__main__":
    sys.exit(main())
