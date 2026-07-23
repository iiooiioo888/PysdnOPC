"""Qwen Code CLI 適配器。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

from opc.core.models import AgentStatus, Task, TaskResult, TaskStatus
from opc.layer3_agent.adapters.base import (
    ExternalAgentAdapter,
    ExternalAgentStdinPolicy,
    ExternalApprovalRequest,
)


class QwenCodeAdapter(ExternalAgentAdapter):
    """Invokes the Qwen Code CLI via ``qwen-code``."""

    agent_type = "qwen_code"
    default_command = "qwen-code"

    def __init__(self, config=None) -> None:
        super().__init__(config=config)
        self._process: asyncio.subprocess.Process | None = None

    def resolve_binary(self) -> str | None:
        if not self.config.enabled:
            return None
        for candidate in self._candidate_commands():
            resolved = self._resolve_command_candidate(candidate)
            if resolved:
                return resolved
        return None

    def _runtime_command(self) -> str:
        return self.resolve_binary() or self.configured_command()

    def _candidate_commands(self) -> list[str]:
        configured = str(self.configured_command() or "").strip()
        candidates: list[str] = []
        if configured:
            candidates.append(configured)
        env_binary = str(os.environ.get("QWEN_CODE_BIN") or "").strip()
        if env_binary:
            candidates.append(env_binary)
        candidates.extend([
            str(Path.home() / ".qwen-code" / "bin" / "qwen-code"),
            str(Path.home() / ".local" / "bin" / "qwen-code"),
            "qwen-code",
        ])
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _resolve_command_candidate(candidate: str) -> str | None:
        raw = str(candidate or "").strip()
        if not raw:
            return None
        expanded = Path(raw).expanduser()
        if expanded.is_absolute() or os.sep in raw:
            return str(expanded) if expanded.is_file() and os.access(expanded, os.X_OK) else None
        return shutil.which(raw)

    async def is_available(self) -> bool:
        return self.resolve_binary() is not None

    async def get_status(self) -> AgentStatus:
        if self._process and self._process.returncode is None:
            return AgentStatus.RUNNING
        return AgentStatus.IDLE

    def supports_interactive(self) -> bool:
        return True

    def supports_session_resume(self) -> bool:
        return True

    def can_resume_without_session_id(self) -> bool:
        return True

    def agent_isolation_home_slug(self) -> str:
        return "qwen_code"

    def agent_home_env_vars(self, home: str) -> dict[str, str]:
        return {"QWEN_CODE_HOME": home}

    def build_process_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str] | None:
        env = super().build_process_env(extra_env)
        merged = dict(os.environ if env is None else env)
        if str(self.config.approval_mode or "auto").strip().lower() == "full-auto":
            merged["QWEN_CODE_AUTO_APPROVE"] = "1"
        # Windows: ensure Node.js is discoverable by the qwen-code subprocess
        if os.name == "nt":
            merged = self._ensure_nodejs_in_path(merged)
        return merged

    @staticmethod
    def _ensure_nodejs_in_path(env: dict[str, str]) -> dict[str, str]:
        """Append common Node.js install dirs to PATH on Windows.

        qwen-code is a Node.js CLI; its npm .cmd wrapper calls ``node``
        internally.  If the parent process PATH lacks the Node.js dir
        (common in CI / service contexts), the subprocess fails with
        "node is not recognized".
        """
        path_key = "PATH" if "PATH" in env else ("Path" if "Path" in env else "PATH")
        current_path = env.get(path_key, "")
        path_dirs = [d.lower() for d in current_path.split(os.pathsep) if d]

        candidates: list[str] = []
        # Standard Node.js installer location
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates.append(os.path.join(program_files, "nodejs"))
        # fnm (Fast Node Manager)
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            fnm_dir = os.path.join(local_appdata, "fnm_multishells")
            if os.path.isdir(fnm_dir):
                # Pick the latest subdirectory
                try:
                    subdirs = sorted(Path(fnm_dir).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                    if subdirs:
                        candidates.append(str(subdirs[0]))
                except OSError:
                    pass
        # nvm-windows
        nvm_home = os.environ.get("NVM_HOME", "")
        if nvm_home:
            candidates.append(nvm_home)
            # nvm-windows symlink dir
            nvm_symlink = os.environ.get("NVM_SYMLINK", "")
            if nvm_symlink:
                candidates.append(nvm_symlink)
        # npm global prefix (where qwen-code.cmd itself lives)
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.append(os.path.join(appdata, "npm"))

        additions: list[str] = []
        for d in candidates:
            if d and os.path.isdir(d) and d.lower() not in path_dirs:
                additions.append(d)

        if additions:
            env[path_key] = os.pathsep.join(additions) + (os.pathsep + current_path if current_path else "")
        return env

    def stdin_policy_for_process(
        self,
        cmd: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> ExternalAgentStdinPolicy:
        _ = metadata
        return "devnull"

    def build_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        _ = workspace_path
        prompt = self.build_task_prompt(task)
        command = self._runtime_command()
        cmd = [
            command,
            *self._build_approval_args(),
            *self._build_thinking_args(),
            *self._build_model_args(),
            *self._build_auth_type_args(),
            *list(self.config.extra_args),
            prompt,
        ]
        metadata = self.build_invocation_metadata(cmd)
        metadata["binary"] = command
        return cmd, metadata

    def build_interactive_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        _ = workspace_path
        prompt = self.build_task_prompt(task)
        command = self._runtime_command()
        cmd = [
            command,
            "--format",
            "json",
            *self._build_approval_args(),
            *self._build_thinking_args(),
            *self._build_model_args(),
            *self._build_auth_type_args(),
            *list(self.config.extra_args),
            prompt,
        ]
        metadata = self.build_invocation_metadata(cmd)
        metadata["binary"] = command
        return cmd, metadata

    async def execute(self, task: Task, workspace_path: str) -> TaskResult:
        if not await self.is_available():
            return TaskResult(status=TaskStatus.FAILED, content="Qwen Code CLI not found")
        cmd, metadata = self.build_invocation(task, workspace_path=workspace_path)

        logger.info(f"Qwen Code executing: {task.title}")

        try:
            env = self.build_process_env()
            stdin_policy = self.stdin_policy_for_process(cmd, metadata)
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL if stdin_policy == "devnull" else asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_path,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(self._process.communicate(), timeout=600)

            output = stdout.decode("utf-8", errors="replace")
            errors = stderr.decode("utf-8", errors="replace")

            if self._process.returncode == 0:
                return TaskResult(
                    status=TaskStatus.DONE,
                    content=self.normalize_result_output(output),
                    artifacts={**metadata, "stderr": errors} if errors else metadata,
                )
            return TaskResult(
                status=TaskStatus.FAILED,
                content=f"Qwen Code exited with code {self._process.returncode}\n{errors}\n{output}",
                artifacts=metadata,
            )
        except asyncio.TimeoutError:
            if self._process:
                self._process.kill()
            return TaskResult(
                status=TaskStatus.FAILED,
                content="Qwen Code timed out after 600s",
                artifacts=metadata,
            )
        except Exception as e:
            return TaskResult(
                status=TaskStatus.FAILED,
                content=f"Qwen Code error: {e}",
                artifacts=metadata,
            )
        finally:
            self._process = None

    async def cancel(self, task_id: str) -> bool:
        if self._process and self._process.returncode is None:
            self._process.kill()
            return True
        return False

    def normalize_result_output(self, output: str) -> str:
        last_result = ""
        last_assistant = ""
        tool_summaries: list[str] = []
        saw_json_event = False
        for line in output.splitlines():
            event = self._parse_json_line(line)
            if not isinstance(event, dict):
                continue
            saw_json_event = True
            event_type = str(event.get("type") or event.get("event") or "").strip()
            if event_type in {"result", "session.result", "run.completed"}:
                text = self._event_text(event)
                if text:
                    last_result = text
            elif (
                "tool" in event_type
                or "command" in event_type
                or event_type.startswith("item.")
                or self._event_part_type(event) == "tool"
            ):
                summary = self._tool_summary(event)
                if summary:
                    tool_summaries.append(summary)
            elif (
                self._event_role(event) == "assistant"
                or event_type in {"assistant", "assistant_message", "message", "text"}
            ):
                text = self._event_text(event)
                if text:
                    last_assistant = text
        if last_result or last_assistant:
            return last_result or last_assistant
        if saw_json_event:
            return self._tool_only_result_fallback(tool_summaries)
        return output

    def format_progress_update(self, text: str, stream_name: str) -> str | None:
        if stream_name != "stdout":
            stripped = str(text or "").strip()
            return f"[External:{self.agent_type}:stderr] {stripped[:500]}" if stripped else None

        event = self._parse_json_line(text)
        if not isinstance(event, dict):
            return super().format_progress_update(text, stream_name)

        event_type = str(event.get("type") or event.get("event") or "").strip()
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        part_type = str(part.get("type") or "").strip()
        if event_type in {"session", "session.started", "init", "step_start", "step-start"}:
            session_id = self._session_id_from_event(event)
            return (
                f"[External:{self.agent_type}:init] session={session_id[:8]}"
                if session_id
                else None
            )
        if (
            "tool" in event_type
            or "command" in event_type
            or event_type.startswith("item.")
            or part_type == "tool"
        ):
            summary = self._tool_summary(event)
            return f"[External:{self.agent_type}:tool] {summary}" if summary else None
        if event_type in {"thinking", "reasoning"} or part_type in {"thinking", "reasoning"}:
            message = self._event_text(event)
            return f"[External:{self.agent_type}:thinking] {message[:2400]}" if message else None
        if event_type in {"result", "session.result", "run.completed"}:
            result = self._event_text(event)
            return f"[External:{self.agent_type}:thinking] {result[:2400]}" if result else None
        if (
            self._event_role(event) == "assistant"
            or event_type in {"assistant", "assistant_message", "message", "text"}
        ):
            message = self._event_text(event)
            return f"[External:{self.agent_type}:thinking] {message[:2400]}" if message else None
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_approval_args(self) -> list[str]:
        mode = str(self.config.approval_mode or "auto").strip().lower()
        if mode == "full-auto":
            return ["--auto-approve"]
        return []

    def _build_thinking_args(self) -> list[str]:
        if self.config.show_thinking:
            return ["--show-thinking"]
        return []

    def _build_model_args(self) -> list[str]:
        if self.config.model and self.config.model_flag:
            return [self.config.model_flag, self.config.model]
        if self.config.model:
            return ["--model", self.config.model]
        return []

    def _build_auth_type_args(self) -> list[str]:
        auth_type = str(getattr(self.config, "auth_type", "") or "").strip()
        if auth_type:
            return ["--auth-type", auth_type]
        return []

    @staticmethod
    def _parse_json_line(line: str) -> dict[str, Any] | None:
        stripped = str(line or "").strip()
        if not stripped or not stripped.startswith("{"):
            return None
        try:
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _event_text(event: dict[str, Any]) -> str:
        for key in ("text", "content", "message", "output", "result"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                parts = [
                    str(item.get("text", "") or "").strip()
                    for item in value
                    if isinstance(item, dict) and str(item.get("text", "") or "").strip()
                ]
                if parts:
                    return "\n".join(parts)
        part = event.get("part")
        if isinstance(part, dict):
            text = str(part.get("text", "") or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _event_role(event: dict[str, Any]) -> str:
        return str(event.get("role") or "").strip().lower()

    @staticmethod
    def _event_part_type(event: dict[str, Any]) -> str:
        part = event.get("part")
        if isinstance(part, dict):
            return str(part.get("type") or "").strip()
        return ""

    @staticmethod
    def _session_id_from_event(event: dict[str, Any]) -> str:
        for key in ("session_id", "sessionId", "id"):
            value = str(event.get(key) or "").strip()
            if value:
                return value
        return ""

    def _tool_summary(self, event: dict[str, Any]) -> str:
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        name = (
            str(part.get("name") or "").strip()
            or str(event.get("tool") or "").strip()
            or str(event.get("name") or "").strip()
        )
        if not name:
            return ""
        target = (
            str(part.get("target") or "").strip()
            or str(event.get("target") or "").strip()
        )
        return f"{name}({target})" if target else name

    def _tool_only_result_fallback(self, tool_summaries: list[str]) -> str:
        if not tool_summaries:
            return "(agent completed with no text output)"
        unique = list(dict.fromkeys(tool_summaries))
        return "Agent executed tools:\n" + "\n".join(f"- {s}" for s in unique[:50])
