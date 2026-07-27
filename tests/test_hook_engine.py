"""Tests for opc.layer6_observability.hook_engine — Hook execution engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opc.core.models import OPCEvent
from opc.layer6_observability.hook_engine import (
    ACTION_BUILTIN,
    ACTION_COMMAND,
    HookDefinition,
    HookEngine,
    HookResult,
    PROFILE_MINIMAL,
    PROFILE_STANDARD,
    PROFILE_STRICT,
)
from opc.layer6_observability.builtin_hooks import register_all_builtin_hooks


@pytest.fixture()
def opc_home(tmp_path: Path) -> Path:
    home = tmp_path / "opc-home"
    home.mkdir(parents=True)
    (home / "config").mkdir()
    return home


@pytest.fixture()
def engine(opc_home: Path) -> HookEngine:
    return HookEngine(opc_home)


def _make_event(event_type: str, data: dict | None = None) -> OPCEvent:
    return OPCEvent(event_type=event_type, payload=data or {})


class TestHookEngineLoadHooks:
    def test_load_from_config(self, engine: HookEngine, opc_home: Path):
        config = {
            "hooks": [
                {"id": "h1", "event": "session.started", "action": "builtin"},
                {"id": "h2", "event": "tool.file.edited", "action": "command", "command": "echo hi"},
            ]
        }
        config_path = opc_home / "config" / "hooks.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        count = engine.load_hooks()
        assert count == 2
        assert len(engine.list_hooks()) == 2

    def test_load_missing_config(self, engine: HookEngine):
        count = engine.load_hooks()
        assert count == 0

    def test_load_invalid_json(self, engine: HookEngine, opc_home: Path):
        config_path = opc_home / "config" / "hooks.json"
        config_path.write_text("not json{{{", encoding="utf-8")
        count = engine.load_hooks()
        assert count == 0


class TestHookEngineMatching:
    def test_exact_event_match(self, engine: HookEngine):
        engine.add_hook(HookDefinition(hook_id="h1", event_type="session.started"))
        hooks = engine._match_hooks(_make_event("session.started"))
        assert len(hooks) == 1

    def test_no_match(self, engine: HookEngine):
        engine.add_hook(HookDefinition(hook_id="h1", event_type="session.started"))
        hooks = engine._match_hooks(_make_event("task.completed"))
        assert len(hooks) == 0

    def test_wildcard_match(self, engine: HookEngine):
        engine.add_hook(HookDefinition(hook_id="h1", event_type="session.*"))
        hooks = engine._match_hooks(_make_event("session.started"))
        assert len(hooks) == 1
        hooks = engine._match_hooks(_make_event("session.completed"))
        assert len(hooks) == 1

    def test_disabled_hook_skipped(self, engine: HookEngine):
        engine.add_hook(HookDefinition(hook_id="h1", event_type="session.started", enabled=False))
        hooks = engine._match_hooks(_make_event("session.started"))
        assert len(hooks) == 0

    def test_profile_filter(self, opc_home: Path, monkeypatch):
        monkeypatch.setenv("OPC_HOOK_PROFILE", "minimal")
        eng = HookEngine(opc_home)
        eng.add_hook(HookDefinition(hook_id="h1", event_type="x", profile="strict"))
        eng.add_hook(HookDefinition(hook_id="h2", event_type="x", profile="minimal"))
        hooks = eng._match_hooks(_make_event("x"))
        assert len(hooks) == 1
        assert hooks[0].hook_id == "h2"

    def test_matcher_regex(self, engine: HookEngine):
        engine.add_hook(HookDefinition(
            hook_id="h1", event_type="tool.file.edited",
            matcher="file_path matches '\\.py$'",
        ))
        hooks = engine._match_hooks(_make_event("tool.file.edited", {"file_path": "test.py"}))
        assert len(hooks) == 1
        hooks = engine._match_hooks(_make_event("tool.file.edited", {"file_path": "test.js"}))
        assert len(hooks) == 0


class TestHookEngineExecution:
    @pytest.mark.asyncio
    async def test_builtin_hook_execution(self, engine: HookEngine):
        async def handler(event, data):
            return HookResult(hook_id="test", success=True, output="handled")

        engine.register_builtin("test-hook", handler)
        engine.add_hook(HookDefinition(
            hook_id="test-hook", event_type="test.event", action="builtin"
        ))
        results = await engine.on_event(_make_event("test.event"))
        assert len(results) == 1
        assert results[0].success
        assert results[0].output == "handled"

    @pytest.mark.asyncio
    async def test_builtin_hook_missing_handler(self, engine: HookEngine):
        engine.add_hook(HookDefinition(
            hook_id="no-handler", event_type="test.event", action="builtin"
        ))
        results = await engine.on_event(_make_event("test.event"))
        assert len(results) == 1
        assert not results[0].success
        assert results[0].skipped

    @pytest.mark.asyncio
    async def test_command_hook_execution(self, engine: HookEngine):
        engine.add_hook(HookDefinition(
            hook_id="cmd", event_type="test.event",
            action="command", command="echo hello",
        ))
        results = await engine.on_event(_make_event("test.event"))
        assert len(results) == 1
        assert results[0].success
        assert "hello" in results[0].output


class TestHookEngineEnableDisable:
    def test_disable(self, engine: HookEngine):
        engine.add_hook(HookDefinition(hook_id="h1", event_type="x"))
        assert engine.disable_hook("h1")
        hooks = engine._match_hooks(_make_event("x"))
        assert len(hooks) == 0

    def test_enable(self, engine: HookEngine):
        engine.add_hook(HookDefinition(hook_id="h1", event_type="x", enabled=False))
        assert engine.enable_hook("h1")
        hooks = engine._match_hooks(_make_event("x"))
        assert len(hooks) == 1

    def test_disable_nonexistent(self, engine: HookEngine):
        assert not engine.disable_hook("ghost")


class TestHookEngineEccImport:
    def test_import_ecc_hooks(self, engine: HookEngine, tmp_path: Path):
        ecc_hooks = {
            "hooks": [
                {
                    "matcher": "tool == \"Edit\"",
                    "hooks": [{"type": "command", "command": "echo edited"}],
                },
                {
                    "matcher": "SessionStart",
                    "hooks": [{"type": "command", "command": "echo start"}],
                },
            ]
        }
        ecc_path = tmp_path / "ecc-hooks.json"
        ecc_path.write_text(json.dumps(ecc_hooks), encoding="utf-8")
        count = engine.import_ecc_hooks(ecc_path)
        assert count == 2
        assert len(engine.list_hooks()) == 2

    def test_import_missing_file(self, engine: HookEngine, tmp_path: Path):
        count = engine.import_ecc_hooks(tmp_path / "nonexistent.json")
        assert count == 0


class TestHookEngineSaveConfig:
    def test_save_and_reload(self, engine: HookEngine, opc_home: Path):
        engine.add_hook(HookDefinition(hook_id="saved", event_type="test.event"))
        engine.save_config()
        config_path = opc_home / "config" / "hooks.json"
        assert config_path.exists()
        # Reload in new engine
        eng2 = HookEngine(opc_home)
        count = eng2.load_hooks()
        assert count == 1
        assert eng2.list_hooks()[0].hook_id == "saved"


class TestBuiltinHooksRegistration:
    def test_register_all(self, engine: HookEngine):
        count = register_all_builtin_hooks(engine)
        assert count == 5
        assert len(engine.list_hooks()) == 5

    @pytest.mark.asyncio
    async def test_pre_shell_safety_blocks_dangerous(self, engine: HookEngine):
        register_all_builtin_hooks(engine)
        event = _make_event("tool.shell.requested", {"command": "rm -rf /"})
        results = await engine.on_event(event)
        assert len(results) == 1
        assert not results[0].success
        assert "Security warning" in results[0].error

    @pytest.mark.asyncio
    async def test_pre_shell_safety_allows_safe(self, engine: HookEngine):
        register_all_builtin_hooks(engine)
        event = _make_event("tool.shell.requested", {"command": "python -m pytest"})
        results = await engine.on_event(event)
        assert len(results) == 1
        assert results[0].success

    @pytest.mark.asyncio
    async def test_budget_warning(self, engine: HookEngine):
        register_all_builtin_hooks(engine)
        event = _make_event("budget.threshold", {"usage_percent": 92, "current_cost": "9.2", "budget_limit": "10"})
        results = await engine.on_event(event)
        assert len(results) == 1
        assert results[0].success
        assert "CRITICAL" in results[0].output
