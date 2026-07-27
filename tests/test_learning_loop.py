"""Tests for the minimal learning loop in post_edit_validate hook.

Validates:
1. Skill activation: telemetry events carry "skill": "learning-loop"
2. Log recording: .opc/logs/post_edit_validation.jsonl receives entries
3. Pattern reuse: second trigger of same scenario reuses captured pattern
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts/hooks to path for import
_HOOKS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "hooks"
sys.path.insert(0, str(_HOOKS_DIR))

import post_edit_validate as hook  # noqa: E402


@pytest.fixture()
def tmp_root(tmp_path: Path) -> Path:
    """Create a minimal project root with required directories."""
    (tmp_path / ".opc" / "logs").mkdir(parents=True)
    (tmp_path / ".opc" / "memory").mkdir(parents=True)
    return tmp_path


class TestPatternStore:
    """Unit tests for the pattern store load/save/update cycle."""

    def test_load_empty_store(self, tmp_root: Path) -> None:
        store = hook._load_pattern_store(tmp_root)
        assert store == {"version": 1, "patterns": {}}

    def test_save_and_reload(self, tmp_root: Path) -> None:
        store = {"version": 1, "patterns": {"opc/core": {"confidence": 0.9, "runs": 5}}}
        hook._save_pattern_store(tmp_root, store)
        reloaded = hook._load_pattern_store(tmp_root)
        assert reloaded["patterns"]["opc/core"]["confidence"] == 0.9

    def test_file_prefix_extraction(self) -> None:
        assert hook._file_prefix("opc/layer2_organization/phase.py") == "opc/layer2_organization"
        assert hook._file_prefix("tests/test_phase.py") == "tests"
        assert hook._file_prefix("scripts/hooks/post_edit_validate.py") == "scripts/hooks"

    def test_update_pattern_new_entry(self, tmp_root: Path) -> None:
        store = {"version": 1, "patterns": {}}
        entry = hook._update_pattern(store, "opc/core", "tests/ -q --tb=short -x", True, 3000)
        assert entry["runs"] == 1
        assert entry["passes"] == 1
        assert entry["confidence"] == pytest.approx(0.6)  # 0.5 + 0.1
        assert entry["consecutive_failures"] == 0

    def test_update_pattern_confidence_growth(self, tmp_root: Path) -> None:
        store = {"version": 1, "patterns": {}}
        # Simulate 4 consecutive passes: 0.5 -> 0.6 -> 0.7 -> 0.8 -> 0.9
        for _ in range(4):
            entry = hook._update_pattern(store, "opc/core", "tests/ -q", True, 2000)
        assert entry["confidence"] == pytest.approx(0.9)
        assert entry["runs"] == 4

    def test_update_pattern_failure_penalty(self, tmp_root: Path) -> None:
        store = {"version": 1, "patterns": {}}
        hook._update_pattern(store, "opc/core", "tests/ -q", True, 2000)  # 0.6
        entry = hook._update_pattern(store, "opc/core", "tests/ -q", False, 2000)  # 0.3
        assert entry["confidence"] == pytest.approx(0.3)
        assert entry["consecutive_failures"] == 1

    def test_stop_rule_consecutive_failures(self, tmp_root: Path) -> None:
        store = {"version": 1, "patterns": {}}
        for _ in range(3):
            entry = hook._update_pattern(store, "opc/core", "tests/ -q", False, 1000)
        # After 3 consecutive failures: confidence=0, unstable
        assert entry["confidence"] == 0.0
        assert entry["unstable_until"] > 0
        assert entry["consecutive_failures"] == 3


class TestPatternLookup:
    """Tests for pattern lookup with stop rules."""

    def test_lookup_no_entry(self) -> None:
        store = {"version": 1, "patterns": {}}
        assert hook._lookup_pattern(store, "opc/core", 0) is None

    def test_lookup_low_confidence(self) -> None:
        store = {"version": 1, "patterns": {"opc/core": {"confidence": 0.5, "runs": 3}}}
        assert hook._lookup_pattern(store, "opc/core", 3) is None

    def test_lookup_high_confidence(self) -> None:
        store = {
            "version": 1,
            "patterns": {
                "opc/core": {
                    "confidence": 0.9,
                    "runs": 10,
                    "test_target": "tests/test_core.py",
                    "unstable_until": 0,
                }
            },
        }
        result = hook._lookup_pattern(store, "opc/core", 10)
        assert result is not None
        assert result["test_target"] == "tests/test_core.py"

    def test_lookup_unstable_cooldown(self) -> None:
        store = {
            "version": 1,
            "patterns": {
                "opc/core": {
                    "confidence": 0.9,
                    "runs": 5,
                    "test_target": "tests/test_core.py",
                    "unstable_until": 20,
                }
            },
        }
        # global_runs=10 < unstable_until=20 -> blocked
        assert hook._lookup_pattern(store, "opc/core", 10) is None
        # global_runs=25 > unstable_until=20 -> allowed
        assert hook._lookup_pattern(store, "opc/core", 25) is not None

    def test_lookup_staleness_eviction(self) -> None:
        store = {
            "version": 1,
            "patterns": {
                "opc/core": {
                    "confidence": 0.9,
                    "runs": 5,
                    "test_target": "tests/test_core.py",
                    "unstable_until": 0,
                }
            },
        }
        # global_runs - entry.runs = 100 - 5 = 95 > 50 -> stale
        assert hook._lookup_pattern(store, "opc/core", 100) is None


class TestResolveTestTarget:
    """Tests for the learning-loop-aware test target resolution."""

    def test_first_run_no_pattern(self, tmp_root: Path) -> None:
        target, reused = hook._resolve_test_target("opc/core/config.py", tmp_root)
        assert target == "tests/ -q --tb=short -x"
        assert reused is False

    def test_second_run_reuses_pattern(self, tmp_root: Path) -> None:
        # Simulate: build up confidence to >= 0.8
        store = {"version": 1, "patterns": {}}
        for _ in range(4):
            hook._update_pattern(store, "opc/core", "tests/test_config.py", True, 2000)
        hook._save_pattern_store(tmp_root, store)

        target, reused = hook._resolve_test_target("opc/core/config.py", tmp_root)
        assert target == "tests/test_config.py"
        assert reused is True

    def test_test_file_direct_target(self, tmp_root: Path) -> None:
        target, reused = hook._resolve_test_target("tests/test_phase.py", tmp_root)
        assert target == "tests/test_phase.py"
        assert reused is False


class TestTelemetryAndSkillActivation:
    """Validate telemetry logging includes skill activation fields."""

    def test_telemetry_contains_skill_field(self, tmp_root: Path) -> None:
        event = {
            "timestamp": "2025-01-01T00:00:00Z",
            "event": "post_edit_validation",
            "skill": "learning-loop",
            "file": "opc/core/config.py",
            "file_prefix": "opc/core",
            "test_target": "tests/ -q --tb=short -x",
            "pattern_reused": False,
            "passed": True,
            "exit_code": 0,
            "duration_ms": 3000,
            "timed_out": False,
            "summary": "42 passed in 3.0s",
        }
        hook._log_telemetry(tmp_root, event)

        log_path = tmp_root / ".opc" / "logs" / "post_edit_validation.jsonl"
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        recorded = json.loads(lines[0])
        assert recorded["skill"] == "learning-loop"
        assert recorded["pattern_reused"] is False

    def test_second_trigger_shows_pattern_reused(self, tmp_root: Path) -> None:
        # First event: no reuse
        event1 = {
            "timestamp": "2025-01-01T00:00:01Z",
            "event": "post_edit_validation",
            "skill": "learning-loop",
            "file": "opc/core/config.py",
            "file_prefix": "opc/core",
            "test_target": "tests/ -q --tb=short -x",
            "pattern_reused": False,
            "passed": True,
            "exit_code": 0,
            "duration_ms": 3000,
            "timed_out": False,
            "summary": "42 passed",
        }
        hook._log_telemetry(tmp_root, event1)

        # Second event: pattern reused
        event2 = {
            "timestamp": "2025-01-01T00:00:02Z",
            "event": "post_edit_validation",
            "skill": "learning-loop",
            "file": "opc/core/config.py",
            "file_prefix": "opc/core",
            "test_target": "tests/test_config.py",
            "pattern_reused": True,
            "passed": True,
            "exit_code": 0,
            "duration_ms": 1200,
            "timed_out": False,
            "summary": "12 passed",
        }
        hook._log_telemetry(tmp_root, event2)

        log_path = tmp_root / ".opc" / "logs" / "post_edit_validation.jsonl"
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["pattern_reused"] is False
        assert second["pattern_reused"] is True


class TestEndToEndLearningLoop:
    """Integration test: simulate the full learn -> reuse cycle."""

    def test_full_learning_cycle(self, tmp_root: Path) -> None:
        """Simulate 4 passes to build confidence, then verify reuse."""
        prefix = "opc/layer2_organization"
        target = "tests/test_phase.py"

        # Phase 1: Build confidence (4 passes -> 0.5 + 4*0.1 = 0.9)
        store = {"version": 1, "patterns": {}}
        for i in range(4):
            hook._update_pattern(store, prefix, target, True, 2500 - i * 100)
        hook._save_pattern_store(tmp_root, store)

        # Phase 2: Verify lookup returns the learned target
        reloaded = hook._load_pattern_store(tmp_root)
        global_runs = sum(e.get("runs", 0) for e in reloaded["patterns"].values())
        pattern = hook._lookup_pattern(reloaded, prefix, global_runs)
        assert pattern is not None
        assert pattern["test_target"] == target
        assert pattern["confidence"] >= hook.CONFIDENCE_THRESHOLD

        # Phase 3: Verify resolve uses the pattern
        resolved_target, reused = hook._resolve_test_target(
            "opc/layer2_organization/phase.py", tmp_root
        )
        assert resolved_target == target
        assert reused is True

        # Phase 4: Log telemetry showing reuse
        event = {
            "timestamp": "2025-01-01T00:01:00Z",
            "event": "post_edit_validation",
            "skill": "learning-loop",
            "file": "opc/layer2_organization/phase.py",
            "file_prefix": prefix,
            "test_target": resolved_target,
            "pattern_reused": reused,
            "passed": True,
            "exit_code": 0,
            "duration_ms": 1500,
            "timed_out": False,
            "summary": "15 passed",
        }
        hook._log_telemetry(tmp_root, event)

        log_path = tmp_root / ".opc" / "logs" / "post_edit_validation.jsonl"
        recorded = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert recorded["skill"] == "learning-loop"
        assert recorded["pattern_reused"] is True
