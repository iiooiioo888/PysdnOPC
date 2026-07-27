#!/usr/bin/env python3
"""PostToolUse hook: post-edit validation enforcement for OpenOPC.

Fires after Write/Edit tool calls. When the edited file falls under
opc/, tests/, or scripts/, runs a targeted pytest validation and logs
a structured telemetry event to .opc/logs/post_edit_validation.jsonl.

Includes a minimal learning loop: captures execution patterns into
.opc/memory/learned_patterns.json and reuses narrowed test targets
when confidence is high enough (>= 0.8).

Mode: BLOCKING — when validation fails, the hook returns exit code 2
to prevent the agent from continuing until tests pass.

Exit codes (Qoder hook contract):
  0 - allow continuation (validation passed or not applicable)
  2 - block (validation failed; agent must fix before proceeding)

Stdin: JSON event context from Qoder Agent lifecycle.
Stdout: validation result + fix guidance (visible to agent).
"""

from __future__ import annotations

import datetime
import json
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

# Learning loop pattern store (relative to project root)
PATTERN_STORE_RELPATH = ".opc/memory/learned_patterns.json"

# Confidence threshold to reuse a narrowed test target
CONFIDENCE_THRESHOLD = 0.8

# Confidence adjustments
CONFIDENCE_GAIN = 0.1
CONFIDENCE_PENALTY = 0.3

# Stop rules
MAX_CONSECUTIVE_FAILURES = 3
UNSTABLE_COOLDOWN_RUNS = 10
STALENESS_EVICTION_RUNS = 50

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    """Resolve project root from this script's location (scripts/hooks/)."""
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Learning Loop: Pattern Store
# ---------------------------------------------------------------------------


def _load_pattern_store(root: Path) -> dict:
    """Load the learned patterns store, or return empty structure."""
    store_path = root / PATTERN_STORE_RELPATH
    try:
        if store_path.exists():
            data = json.loads(store_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "patterns" in data:
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"version": 1, "patterns": {}}


def _save_pattern_store(root: Path, store: dict) -> None:
    """Persist the pattern store to disk."""
    store_path = root / PATTERN_STORE_RELPATH
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(store, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _file_prefix(rel_path: str) -> str:
    """Extract a stable prefix key for pattern grouping.

    Examples:
      opc/layer2_organization/phase.py -> opc/layer2_organization
      tests/test_phase.py -> tests
      scripts/hooks/post_edit_validate.py -> scripts/hooks
    """
    parts = rel_path.rsplit("/", 1)
    return parts[0] if len(parts) > 1 else rel_path


def _lookup_pattern(store: dict, prefix: str, global_runs: int) -> dict | None:
    """Look up a learned pattern for the given prefix.

    Returns the pattern entry if usable (confidence >= threshold, not
    unstable, not stale), otherwise None.
    """
    entry = store.get("patterns", {}).get(prefix)
    if not entry:
        return None
    # Stop rule: staleness eviction
    if global_runs - entry.get("runs", 0) > STALENESS_EVICTION_RUNS:
        return None
    # Stop rule: unstable cooldown
    if entry.get("unstable_until", 0) > global_runs:
        return None
    # Confidence gate
    if entry.get("confidence", 0.0) < CONFIDENCE_THRESHOLD:
        return None
    return entry


def _update_pattern(
    store: dict, prefix: str, test_target: str, passed: bool, duration_ms: int
) -> dict:
    """Update or create a pattern entry after a validation run.

    Returns the updated entry.
    """
    patterns = store.setdefault("patterns", {})
    entry = patterns.get(prefix)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if entry is None:
        entry = {
            "test_target": test_target,
            "confidence": 0.5,
            "runs": 0,
            "passes": 0,
            "failures": 0,
            "last_run": now_iso,
            "last_outcome": "pass" if passed else "fail",
            "avg_duration_ms": duration_ms,
            "consecutive_failures": 0,
            "unstable_until": 0,
        }
        patterns[prefix] = entry

    # Update counters
    entry["runs"] += 1
    entry["last_run"] = now_iso
    entry["last_outcome"] = "pass" if passed else "fail"
    # Running average for duration
    prev_avg = entry.get("avg_duration_ms", duration_ms)
    entry["avg_duration_ms"] = int((prev_avg * (entry["runs"] - 1) + duration_ms) / entry["runs"])

    if passed:
        entry["passes"] += 1
        entry["consecutive_failures"] = 0
        # Confidence gain (only when using narrowed target)
        entry["confidence"] = min(1.0, entry.get("confidence", 0.5) + CONFIDENCE_GAIN)
    else:
        entry["failures"] += 1
        entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
        # Confidence penalty
        entry["confidence"] = max(0.0, entry.get("confidence", 0.5) - CONFIDENCE_PENALTY)
        # Stop rule: consecutive failures -> mark unstable
        if entry["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES:
            entry["unstable_until"] = entry["runs"] + UNSTABLE_COOLDOWN_RUNS
            entry["confidence"] = 0.0

    # Update test target to the most recent one
    entry["test_target"] = test_target
    return entry


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


def _resolve_test_target(rel_path: str, root: Path) -> tuple[str, bool]:
    """Resolve the test target using the learning loop.

    Returns (test_target, pattern_reused).
    If a high-confidence pattern exists for this file prefix, use its
    narrowed test target. Otherwise fall back to the default strategy.
    """
    store = _load_pattern_store(root)
    prefix = _file_prefix(rel_path)
    global_runs = sum(
        e.get("runs", 0) for e in store.get("patterns", {}).values()
    )

    pattern = _lookup_pattern(store, prefix, global_runs)
    if pattern and pattern.get("test_target"):
        return pattern["test_target"], True

    return _pick_test_target(rel_path), False


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

    # --- Learning Loop: resolve test target ---
    test_target, pattern_reused = _resolve_test_target(rel_path, root)
    result = _run_pytest(root, test_target)

    # --- Learning Loop: capture pattern ---
    store = _load_pattern_store(root)
    prefix = _file_prefix(rel_path)
    _update_pattern(store, prefix, test_target, result["passed"], result["duration_ms"])
    _save_pattern_store(root, store)

    # Build telemetry event (with learning-loop metadata)
    telemetry_event = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "post_edit_validation",
        "skill": "learning-loop",
        "file": rel_path,
        "file_prefix": prefix,
        "test_target": test_target,
        "pattern_reused": pattern_reused,
        "passed": result["passed"],
        "exit_code": result["exit_code"],
        "duration_ms": result["duration_ms"],
        "timed_out": result["timed_out"],
        "summary": result["summary"],
    }
    _log_telemetry(root, telemetry_event)

    # Advisory output to agent (visible in session)
    reuse_tag = " [pattern-reused]" if pattern_reused else ""
    if result["passed"]:
        print(
            f"[post-edit-validation] PASS{reuse_tag} ({result['duration_ms']}ms) "
            f"target={test_target}"
        )
        return 0

    # --- BLOCKING: validation failed ---
    print(
        f"[post-edit-validation] FAIL{reuse_tag} ({result['duration_ms']}ms) "
        f"target={test_target} | {result['summary']}"
    )
    print()
    print("=" * 60)
    print("BLOCKED: Post-edit validation failed. You MUST fix this")
    print("before proceeding with any further edits or commits.")
    print("=" * 60)
    print()
    print("Fix guidance:")
    print(f"  1. Review the failing test output above (target: {test_target}).")
    print(f"  2. Identify the root cause in the edited file: {rel_path}")
    print("  3. Fix the code so that all tests pass.")
    print(f"  4. Re-run: python -m pytest {test_target}")
    print("  5. This hook will re-validate automatically on next edit.")
    print()
    if result["timed_out"]:
        print(
            f"NOTE: Test run timed out after {PYTEST_TIMEOUT}s. "
            "Consider narrowing the test target or checking for hangs."
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
