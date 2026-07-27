"""Tests for opc.layer5_memory.instinct_engine — Continuous Learning system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opc.layer5_memory.instinct_engine import (
    EVOLVE_CONFIDENCE_THRESHOLD,
    Instinct,
    InstinctEngine,
    ExtractionResult,
)


@pytest.fixture()
def engine(tmp_path: Path) -> InstinctEngine:
    opc_home = tmp_path / "opc-home"
    opc_home.mkdir(parents=True)
    (opc_home / "memory").mkdir()
    (opc_home / "skills").mkdir()
    return InstinctEngine(opc_home)


def _make_messages(error_fix: bool = False, testing: bool = False, tool_heavy: bool = False) -> list[dict]:
    """Build fake session messages for testing extraction."""
    messages: list[dict] = []
    if error_fix:
        messages.extend([
            {"role": "assistant", "content": "Got an error: TypeError exception"},
            {"role": "assistant", "content": "The test failed with traceback"},
            {"role": "assistant", "content": "Fixed the issue, resolved now"},
            {"role": "assistant", "content": "Solution applied, error gone"},
        ])
    if testing:
        messages.extend([
            {"role": "assistant", "content": "Running pytest with coverage"},
            {"role": "assistant", "content": "Writing test assertions"},
            {"role": "assistant", "content": "Test suite passes with 90% coverage"},
        ])
    if tool_heavy:
        for i in range(6):
            messages.append({"role": "tool", "name": "shell", "content": f"command {i}"})
    if not messages:
        messages = [{"role": "user", "content": "hello"}]
    return messages


class TestInstinctEngineExtract:
    @pytest.mark.asyncio
    async def test_extract_empty_messages(self, engine: InstinctEngine):
        result = await engine.extract_from_session("sess-1", [])
        assert result.total_patterns_found == 0
        assert result.new_instincts == []

    @pytest.mark.asyncio
    async def test_extract_error_fix_pattern(self, engine: InstinctEngine):
        messages = _make_messages(error_fix=True)
        result = await engine.extract_from_session("sess-1", messages)
        assert result.total_patterns_found >= 1
        assert len(result.new_instincts) >= 1
        categories = [i.category for i in result.new_instincts]
        assert "debugging" in categories

    @pytest.mark.asyncio
    async def test_extract_testing_pattern(self, engine: InstinctEngine):
        messages = _make_messages(testing=True)
        result = await engine.extract_from_session("sess-2", messages)
        assert result.total_patterns_found >= 1
        categories = [i.category for i in result.new_instincts]
        assert "testing" in categories

    @pytest.mark.asyncio
    async def test_extract_tool_heavy_pattern(self, engine: InstinctEngine):
        messages = _make_messages(tool_heavy=True)
        result = await engine.extract_from_session("sess-3", messages)
        assert result.total_patterns_found >= 1
        categories = [i.category for i in result.new_instincts]
        assert "workflow" in categories

    @pytest.mark.asyncio
    async def test_extract_reinforces_existing(self, engine: InstinctEngine):
        messages = _make_messages(error_fix=True)
        result1 = await engine.extract_from_session("sess-1", messages)
        assert len(result1.new_instincts) >= 1
        # Same patterns again should reinforce, not create new
        result2 = await engine.extract_from_session("sess-2", messages)
        assert len(result2.reinforced_ids) >= 1


class TestInstinctEngineReinforce:
    def test_reinforce_existing(self, engine: InstinctEngine):
        inst = Instinct(id="inst-test1", pattern="test pattern", confidence=0.3)
        engine._instincts["inst-test1"] = inst
        assert engine.reinforce("inst-test1", "new evidence")
        assert inst.confidence > 0.3
        assert inst.reinforcement_count == 2
        assert "new evidence" in inst.evidence

    def test_reinforce_nonexistent(self, engine: InstinctEngine):
        assert not engine.reinforce("inst-nonexistent", "evidence")

    def test_reinforce_caps_at_1(self, engine: InstinctEngine):
        inst = Instinct(id="inst-cap", pattern="cap test", confidence=0.95)
        engine._instincts["inst-cap"] = inst
        engine.reinforce("inst-cap", "ev")
        assert inst.confidence <= 1.0


class TestInstinctEngineEvolve:
    def test_evolve_high_confidence(self, engine: InstinctEngine, tmp_path: Path):
        inst = Instinct(
            id="inst-high", pattern="Always run linter before commit",
            confidence=0.9, category="coding",
            evidence=["s1", "s2", "s3"],
        )
        engine._instincts["inst-high"] = inst
        skill_name = engine.evolve_to_skill(["inst-high"])
        assert skill_name != ""
        assert skill_name.startswith("learned-")
        # Verify skill file created
        skill_dir = engine._skills_dir / skill_name
        assert (skill_dir / "SKILL.md").exists()
        # Verify instinct marked as evolved
        assert inst.metadata.get("status") == "evolved"

    def test_evolve_low_confidence_rejected(self, engine: InstinctEngine):
        inst = Instinct(id="inst-low", pattern="weak pattern", confidence=0.3)
        engine._instincts["inst-low"] = inst
        skill_name = engine.evolve_to_skill(["inst-low"])
        assert skill_name == ""

    def test_evolve_nonexistent(self, engine: InstinctEngine):
        assert engine.evolve_to_skill(["inst-ghost"]) == ""

    def test_evolve_multiple(self, engine: InstinctEngine):
        for i in range(3):
            engine._instincts[f"inst-m{i}"] = Instinct(
                id=f"inst-m{i}", pattern=f"pattern {i}",
                confidence=0.85, category="testing",
            )
        skill_name = engine.evolve_to_skill(["inst-m0", "inst-m1", "inst-m2"])
        assert skill_name != ""
        content = (engine._skills_dir / skill_name / "SKILL.md").read_text(encoding="utf-8")
        assert "3 instincts" in content


class TestInstinctEnginePrune:
    def test_prune_expired(self, engine: InstinctEngine):
        inst = Instinct(
            id="inst-old", pattern="old pattern",
            confidence=0.5, last_reinforced="2020-01-01T00:00:00+00:00",
        )
        engine._instincts["inst-old"] = inst
        pruned = engine.prune_expired(max_age_days=90)
        assert pruned == 1
        assert "inst-old" not in engine._instincts

    def test_prune_keeps_recent(self, engine: InstinctEngine):
        from datetime import datetime, timezone
        inst = Instinct(
            id="inst-recent", pattern="recent",
            confidence=0.5,
            last_reinforced=datetime.now(timezone.utc).isoformat(),
        )
        engine._instincts["inst-recent"] = inst
        pruned = engine.prune_expired(max_age_days=90)
        assert pruned == 0
        assert "inst-recent" in engine._instincts

    def test_prune_skips_evolved(self, engine: InstinctEngine):
        inst = Instinct(
            id="inst-evolved", pattern="evolved",
            confidence=0.9, last_reinforced="2020-01-01T00:00:00+00:00",
            metadata={"status": "evolved"},
        )
        engine._instincts["inst-evolved"] = inst
        pruned = engine.prune_expired(max_age_days=90)
        assert pruned == 0


class TestInstinctEnginePersistence:
    def test_save_and_reload(self, tmp_path: Path):
        opc_home = tmp_path / "opc-home"
        opc_home.mkdir(parents=True)
        (opc_home / "memory").mkdir()
        eng1 = InstinctEngine(opc_home)
        eng1._instincts["inst-x"] = Instinct(
            id="inst-x", pattern="persist test", confidence=0.7,
            created_at="2026-01-01T00:00:00+00:00",
            last_reinforced="2026-01-01T00:00:00+00:00",
        )
        eng1._save()
        # Reload
        eng2 = InstinctEngine(opc_home)
        assert "inst-x" in eng2._instincts
        assert eng2._instincts["inst-x"].pattern == "persist test"
        assert eng2._instincts["inst-x"].confidence == 0.7


class TestInstinctEngineStatus:
    def test_status_sorted_by_confidence(self, engine: InstinctEngine):
        engine._instincts["a"] = Instinct(id="a", pattern="low", confidence=0.2)
        engine._instincts["b"] = Instinct(id="b", pattern="high", confidence=0.9)
        engine._instincts["c"] = Instinct(id="c", pattern="mid", confidence=0.5)
        status = engine.status()
        assert status[0].id == "b"
        assert status[1].id == "c"
        assert status[2].id == "a"


class TestInstinctEngineImportExport:
    def test_export(self, engine: InstinctEngine):
        engine._instincts["e1"] = Instinct(id="e1", pattern="export me", confidence=0.6)
        data = engine.export_instincts()
        assert len(data) == 1
        assert data[0]["id"] == "e1"
        assert data[0]["pattern"] == "export me"

    def test_import(self, engine: InstinctEngine):
        data = [
            {"id": "imp-1", "pattern": "imported pattern", "confidence": 0.5, "category": "coding"},
            {"id": "imp-2", "pattern": "another one", "confidence": 0.4},
        ]
        count = engine.import_instincts(data)
        assert count == 2
        assert "imp-1" in engine._instincts
        assert "imp-2" in engine._instincts

    def test_import_skips_duplicates(self, engine: InstinctEngine):
        engine._instincts["dup"] = Instinct(id="dup", pattern="existing")
        count = engine.import_instincts([{"id": "dup", "pattern": "new"}])
        assert count == 0
