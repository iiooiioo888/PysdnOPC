"""QwenCodeAdapter 行為測試 — 驗證適配器註冊與基本呼叫路徑。"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from opc.core.config import AgentsConfig, ExternalAgentConfig
from opc.core.models import AgentStatus, Task, TaskResult, TaskStatus
from opc.layer3_agent.adapters.qwen_code_adapter import QwenCodeAdapter
from opc.layer3_agent.adapters.registry import ADAPTER_CLASSES, AdapterRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> ExternalAgentConfig:
    defaults = {
        "command": "qwen-code",
        "run_mode": "interactive",
        "approval_mode": "full-auto",
        "show_thinking": True,
    }
    defaults.update(overrides)
    return ExternalAgentConfig(**defaults)


def _make_task(title: str = "Test task", description: str = "Do something") -> Task:
    return Task(id="t1", title=title, description=description, status=TaskStatus.PENDING)


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class QwenCodeRegistrationTests(unittest.TestCase):
    """Verify qwen_code is properly registered in the adapter system."""

    def test_adapter_class_registered(self) -> None:
        self.assertIn("qwen_code", ADAPTER_CLASSES)
        self.assertIs(ADAPTER_CLASSES["qwen_code"], QwenCodeAdapter)

    def test_preferred_order_includes_qwen_code(self) -> None:
        config = AgentsConfig()
        self.assertIn("qwen_code", config.preferred_order)

    def test_default_agents_config_has_qwen_code(self) -> None:
        config = AgentsConfig()
        self.assertIn("qwen_code", config.agents)
        agent_cfg = config.agents["qwen_code"]
        self.assertEqual(agent_cfg.command, "qwen-code")
        self.assertEqual(agent_cfg.run_mode, "interactive")
        self.assertEqual(agent_cfg.approval_mode, "full-auto")
        self.assertTrue(agent_cfg.show_thinking)

    def test_registry_initializes_qwen_code_adapter(self) -> None:
        config = AgentsConfig()
        registry = AdapterRegistry(config)
        # Manually inject adapter without network discovery
        adapter = QwenCodeAdapter(config=config.agents["qwen_code"])
        registry._adapters["qwen_code"] = adapter
        registry._available["qwen_code"] = True
        self.assertIsNotNone(registry.get("qwen_code"))
        self.assertEqual(registry.get("qwen_code").agent_type, "qwen_code")


# ---------------------------------------------------------------------------
# Adapter property tests
# ---------------------------------------------------------------------------

class QwenCodeAdapterPropertyTests(unittest.TestCase):
    """Verify adapter static properties and capabilities."""

    def setUp(self) -> None:
        self.adapter = QwenCodeAdapter(config=_make_config())

    def test_agent_type(self) -> None:
        self.assertEqual(self.adapter.agent_type, "qwen_code")

    def test_default_command(self) -> None:
        self.assertEqual(self.adapter.default_command, "qwen-code")

    def test_supports_interactive(self) -> None:
        self.assertTrue(self.adapter.supports_interactive())

    def test_supports_session_resume(self) -> None:
        self.assertTrue(self.adapter.supports_session_resume())

    def test_can_resume_without_session_id(self) -> None:
        self.assertTrue(self.adapter.can_resume_without_session_id())

    def test_agent_isolation_home_slug(self) -> None:
        self.assertEqual(self.adapter.agent_isolation_home_slug(), "qwen_code")

    def test_agent_home_env_vars(self) -> None:
        env = self.adapter.agent_home_env_vars("/tmp/qwen_home")
        self.assertEqual(env, {"QWEN_CODE_HOME": "/tmp/qwen_home"})

    def test_stdin_policy_is_devnull(self) -> None:
        policy = self.adapter.stdin_policy_for_process(["qwen-code", "run"])
        self.assertEqual(policy, "devnull")


# ---------------------------------------------------------------------------
# Binary resolution tests
# ---------------------------------------------------------------------------

class QwenCodeBinaryResolutionTests(unittest.TestCase):
    """Verify binary resolution logic."""

    def test_resolve_binary_returns_none_when_disabled(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config(enabled=False))
        self.assertIsNone(adapter.resolve_binary())

    def test_candidate_commands_includes_configured(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config(command="my-qwen"))
        candidates = adapter._candidate_commands()
        self.assertIn("my-qwen", candidates)

    def test_candidate_commands_includes_env_var(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config())
        with patch.dict(os.environ, {"QWEN_CODE_BIN": "/opt/qwen/bin/qwen-code"}):
            candidates = adapter._candidate_commands()
        self.assertIn("/opt/qwen/bin/qwen-code", candidates)

    def test_candidate_commands_includes_default_paths(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config())
        with patch.dict(os.environ, {"QWEN_CODE_BIN": ""}, clear=False):
            candidates = adapter._candidate_commands()
        # Should include bare "qwen-code" as fallback
        self.assertIn("qwen-code", candidates)

    def test_candidate_commands_deduplicates(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config(command="qwen-code"))
        with patch.dict(os.environ, {"QWEN_CODE_BIN": "qwen-code"}):
            candidates = adapter._candidate_commands()
        # "qwen-code" should appear only once despite multiple sources
        self.assertEqual(candidates.count("qwen-code"), 1)


# ---------------------------------------------------------------------------
# Invocation build tests
# ---------------------------------------------------------------------------

class QwenCodeInvocationTests(unittest.TestCase):
    """Verify CLI invocation construction."""

    def setUp(self) -> None:
        self.adapter = QwenCodeAdapter(config=_make_config())

    def test_build_invocation_returns_cmd_and_metadata(self) -> None:
        task = _make_task()
        with patch.object(self.adapter, "resolve_binary", return_value="/usr/bin/qwen-code"):
            cmd, metadata = self.adapter.build_invocation(task)
        self.assertIsInstance(cmd, list)
        self.assertIsInstance(metadata, dict)
        self.assertEqual(cmd[0], "/usr/bin/qwen-code")
        self.assertEqual(metadata["binary"], "/usr/bin/qwen-code")
        self.assertEqual(metadata["agent"], "qwen_code")

    def test_build_invocation_includes_output_format(self) -> None:
        task = _make_task()
        with patch.object(self.adapter, "resolve_binary", return_value="qwen-code"):
            cmd, _ = self.adapter.build_invocation(task)
        self.assertIn("--output-format", cmd)
        fmt_idx = cmd.index("--output-format")
        self.assertEqual(cmd[fmt_idx + 1], "stream-json")

    def test_build_invocation_uses_prompt_flag(self) -> None:
        task = _make_task()
        with patch.object(self.adapter, "resolve_binary", return_value="qwen-code"):
            cmd, _ = self.adapter.build_invocation(task)
        self.assertIn("-p", cmd)

    def test_build_invocation_no_legacy_flags(self) -> None:
        """New qwen CLI removed --auto-approve, --show-thinking, --auth-type."""
        task = _make_task()
        with patch.object(self.adapter, "resolve_binary", return_value="qwen-code"):
            cmd, _ = self.adapter.build_invocation(task)
        self.assertNotIn("--auto-approve", cmd)
        self.assertNotIn("--show-thinking", cmd)
        self.assertNotIn("--auth-type", cmd)
        self.assertNotIn("--format", cmd)

    def test_build_invocation_includes_prompt(self) -> None:
        task = _make_task(title="Fix bug", description="Fix the login bug")
        with patch.object(self.adapter, "resolve_binary", return_value="qwen-code"):
            cmd, _ = self.adapter.build_invocation(task)
        # Last element should be the prompt
        self.assertIn("Fix the login bug", cmd[-1])

    def test_build_interactive_invocation_includes_stream_json_format(self) -> None:
        task = _make_task()
        with patch.object(self.adapter, "resolve_binary", return_value="qwen-code"):
            cmd, metadata = self.adapter.build_interactive_invocation(task)
        self.assertIn("--output-format", cmd)
        fmt_idx = cmd.index("--output-format")
        self.assertEqual(cmd[fmt_idx + 1], "stream-json")
        self.assertIn("-i", cmd)

    def test_build_model_args_with_model(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config(model="qwen-max"))
        task = _make_task()
        with patch.object(adapter, "resolve_binary", return_value="qwen-code"):
            cmd, _ = adapter.build_invocation(task)
        self.assertIn("--model", cmd)
        model_idx = cmd.index("--model")
        self.assertEqual(cmd[model_idx + 1], "qwen-max")


# ---------------------------------------------------------------------------
# Process env tests
# ---------------------------------------------------------------------------

class QwenCodeProcessEnvTests(unittest.TestCase):
    """Verify environment variable construction."""

    def test_full_auto_sets_auto_approve_env(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config(approval_mode="full-auto"))
        env = adapter.build_process_env(extra_env={"FOO": "bar"})
        self.assertIsNotNone(env)
        self.assertEqual(env["QWEN_CODE_AUTO_APPROVE"], "1")
        self.assertEqual(env["FOO"], "bar")

    def test_non_full_auto_does_not_set_auto_approve_env(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config(approval_mode="auto"))
        env = adapter.build_process_env(extra_env={"FOO": "bar"})
        # When not full-auto, env is returned from super() without QWEN_CODE_AUTO_APPROVE
        if env is not None:
            self.assertNotIn("QWEN_CODE_AUTO_APPROVE", env)


# ---------------------------------------------------------------------------
# Output normalization tests
# ---------------------------------------------------------------------------

class QwenCodeOutputNormalizationTests(unittest.TestCase):
    """Verify JSON stream output normalization."""

    def setUp(self) -> None:
        self.adapter = QwenCodeAdapter(config=_make_config())

    def test_plain_text_passthrough(self) -> None:
        output = "Hello world\nThis is plain text"
        self.assertEqual(self.adapter.normalize_result_output(output), output)

    def test_result_event_extracted(self) -> None:
        events = [
            json.dumps({"type": "session.started", "session_id": "abc123"}),
            json.dumps({"type": "result", "text": "Task completed successfully"}),
        ]
        output = "\n".join(events)
        self.assertEqual(self.adapter.normalize_result_output(output), "Task completed successfully")

    def test_assistant_message_extracted(self) -> None:
        events = [
            json.dumps({"type": "assistant_message", "role": "assistant", "text": "Here is the answer"}),
        ]
        output = "\n".join(events)
        self.assertEqual(self.adapter.normalize_result_output(output), "Here is the answer")

    def test_tool_only_fallback(self) -> None:
        events = [
            json.dumps({"type": "tool_call", "tool": "bash", "target": "ls -la"}),
        ]
        output = "\n".join(events)
        result = self.adapter.normalize_result_output(output)
        self.assertIn("bash(ls -la)", result)

    def test_empty_json_events_fallback(self) -> None:
        events = [
            json.dumps({"type": "init"}),
        ]
        output = "\n".join(events)
        result = self.adapter.normalize_result_output(output)
        self.assertIn("no text output", result)


# ---------------------------------------------------------------------------
# Progress update tests
# ---------------------------------------------------------------------------

class QwenCodeProgressUpdateTests(unittest.TestCase):
    """Verify streaming progress update formatting."""

    def setUp(self) -> None:
        self.adapter = QwenCodeAdapter(config=_make_config())

    def test_stderr_progress(self) -> None:
        result = self.adapter.format_progress_update("some error", "stderr")
        self.assertIn("[External:qwen_code:stderr]", result)

    def test_tool_event_progress(self) -> None:
        event = json.dumps({"type": "tool_call", "tool": "edit", "target": "main.py"})
        result = self.adapter.format_progress_update(event, "stdout")
        self.assertIn("[External:qwen_code:tool]", result)
        self.assertIn("edit(main.py)", result)

    def test_thinking_event_progress(self) -> None:
        event = json.dumps({"type": "thinking", "text": "Analyzing code..."})
        result = self.adapter.format_progress_update(event, "stdout")
        self.assertIn("[External:qwen_code:thinking]", result)
        self.assertIn("Analyzing code...", result)

    def test_session_init_event(self) -> None:
        event = json.dumps({"type": "session.started", "session_id": "sess_abc12345"})
        result = self.adapter.format_progress_update(event, "stdout")
        self.assertIn("[External:qwen_code:init]", result)
        self.assertIn("sess_abc", result)


# ---------------------------------------------------------------------------
# Async behavior tests
# ---------------------------------------------------------------------------

class QwenCodeAsyncTests(unittest.IsolatedAsyncioTestCase):
    """Verify async adapter methods."""

    async def test_is_available_false_when_disabled(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config(enabled=False))
        self.assertFalse(await adapter.is_available())

    async def test_get_status_idle_when_no_process(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config())
        status = await adapter.get_status()
        self.assertEqual(status, AgentStatus.IDLE)

    async def test_execute_returns_failed_when_unavailable(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config(enabled=False))
        task = _make_task()
        result = await adapter.execute(task, workspace_path="/tmp")
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("not found", result.content)

    async def test_cancel_returns_false_when_no_process(self) -> None:
        adapter = QwenCodeAdapter(config=_make_config())
        self.assertFalse(await adapter.cancel("task-1"))


if __name__ == "__main__":
    unittest.main()
