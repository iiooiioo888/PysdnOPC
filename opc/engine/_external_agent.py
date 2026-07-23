"""ExternalAgentMixin — 外部代理選擇/構建相關方法。"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from opc.core.config import get_project_workplace
from opc.core.models import (
    ExecutionMode,
    ModeSelection,
    Task,
    TaskResult,
    TaskStatus,
)
import hashlib
import shutil
import uuid
from types import SimpleNamespace

from opc.core.attachment_content import can_extract_text, extract_attachment_text
from opc.core.attachment_store import AttachmentRef, AttachmentStore
from opc.core.company_tools import (
    company_collaboration_enabled_for_task,
    resolve_company_turn_mode,
    resolve_task_collaboration_tools,
)
from opc.core.models import WorkItemExecutionStrategy
from opc.engine._core import AGENT_SELECTION_PROMPT
from opc.layer1_perception.context_assembler import ContextAssembler, ExternalContextLayers
from opc.layer2_organization.prompt_contract import (
    has_prompt_contract,
    is_report_prompt_turn,
    make_prompt_contract,
    prompt_contract_from_work_item,
)
from opc.layer2_organization.recruiter import normalize_recruitment_agent_choice
from opc.layer2_organization.session_scoping import (
    external_resume_allowed_for_scope,
    is_top_level_company_session,
)
from opc.layer2_organization.work_item_identity import (
    projection_id_for_task,
    turn_type_for_task,
    work_item_identity_payload,
    work_item_identity_payload_for_task,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer3_agent.external_session_identity import (
    is_provider_session_token,
    provider_token_from_external_session,
)
from opc.layer3_agent.prompt_harness.builder import (
    _final_decider_role_id,
    _memory_skill_user_facing,
)
from opc.layer4_tools.collaboration import build_external_cli_tool_contract_lines
from opc.layer5_memory.memory_manager import MemoryManager

if TYPE_CHECKING:
    from opc.engine._core import OPCEngine


class ExternalAgentMixin:
    """Mixin providing 外部代理選擇/構建相關方法 for OPCEngine."""

    def _should_force_native_execution(self, task: Task) -> bool:
        return str(task.metadata.get("router_preferred_agent") or "").strip().lower() == "native" or bool(
            task.metadata.get("force_native_execution")
        )

    def _available_external_agents(self) -> list[str]:
        if not self.adapter_registry:
            return []
        return self.adapter_registry.list_available()

    def _requests_explicit_project_knowledge(self, message: str) -> bool:
        normalized = " ".join(str(message or "").strip().lower().split())
        if not normalized:
            return False
        markers = (
            "参考这个 project 的已确认决策",
            "参考项目知识",
            "引用项目知识",
            "导入项目知识",
            "参考已确认决策",
            "导入已确认决策",
            "project knowledge",
            "confirmed project decisions",
            "confirmed project knowledge",
            "import project knowledge",
            "reference project knowledge",
        )
        return any(marker in normalized for marker in markers)

    def _get_role_runtime_value(self, role: Any | None, key: str, default: str = "") -> str:
        if not role:
            return default
        policy = getattr(role, "runtime_policy", {}) or {}
        if isinstance(policy, dict):
            value = policy.get(key, default)
        else:
            value = getattr(policy, key, default)
        return str(value or default)

    def _resolve_task_role(self, task: Task) -> Any:
        assert self.org_engine
        role_id = task.assigned_to or str(task.metadata.get("work_item_role_id", "")).strip()
        role = (
            self.org_engine.get_role_for_work_item(role_id, [])
            if role_id
            else self.org_engine.get_executor()
        )
        task.assigned_to = role.role_id
        return role

    def _estimate_task_complexity(self, task: Task, role: Any | None = None) -> tuple[int, list[str]]:
        text = f"{task.title}\n{task.description}".strip().lower()
        score = 0
        reasons: list[str] = []

        if len(text) > 1400:
            score += 2
            reasons.append("long_task_description")
        elif len(text) > 500:
            score += 1
            reasons.append("medium_task_description")

        if len(task.dependencies) > 1:
            score += 1
            reasons.append("multiple_dependencies")

        tool_heavy_keywords = (
            "implement", "fix", "debug", "refactor", "write code", "edit", "file", "files",
            "shell", "cli", "command", "script", "git", "test", "build", "deploy", "migration",
            "api", "endpoint", "database",
        )
        if any(keyword in text for keyword in tool_heavy_keywords):
            score += 2
            reasons.append("tool_heavy_work")

        multi_step_keywords = (
            "end-to-end", "complex", "multi-step", "runtime", "coordinate", "parallel",
            "integration", "architecture", "deliverable",
        )
        if any(keyword in text for keyword in multi_step_keywords):
            score += 1
            reasons.append("multi_step_work")

        if role:
            role_text = " ".join(
                str(part)
                for part in (
                    getattr(role, "role_id", ""),
                    getattr(role, "name", ""),
                    getattr(role, "responsibility", ""),
                )
                if part
            ).lower()
            if any(keyword in role_text for keyword in ("implement", "engineering", "deployment", "executor", "code", "data processing")):
                score += 1
                reasons.append("execution_oriented_role")
            if any(keyword in role_text for keyword in ("review", "approval", "qa", "plan", "planning", "coordinator")):
                score -= 2
                reasons.append("native_friendly_role")

        return max(score, 0), reasons

    def _build_execution_context_summary(self, task: Task, role: Any | None = None) -> dict[str, Any]:
        runtime_state = dict(task.metadata.get("runtime_v2", {}) or {})
        raw_resume_state = task.context_snapshot.get("runtime_resume", {}) if isinstance(task.context_snapshot, dict) else {}
        resume_state = dict(raw_resume_state) if isinstance(raw_resume_state, dict) else {}
        attachments = list(task.metadata.get("attachment_refs", []) or [])
        work_item_projection_id = projection_id_for_task(task)
        work_item_turn_type = turn_type_for_task(
            task,
            fallback=(self._get_role_runtime_value(role, "default_turn_type", "work").lower() if role else "work"),
        )
        return {
            "execution_mode": str(task.metadata.get("execution_mode", "") or "").strip(),
            **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
            "turn_type": work_item_turn_type,
            "has_runtime_resume": bool(runtime_state or resume_state),
            "active_subagents": len(
                list(
                    (resume_state.get("active_subagents") or runtime_state.get("active_subagents") or [])
                )
            ),
            "pending_permission_requests": len(
                list(
                    (resume_state.get("permission_requests") or runtime_state.get("permission_requests") or [])
                )
            ),
            "attachment_count": len(attachments),
            "force_native_execution": bool(task.metadata.get("force_native_execution")),
            "work_item_orchestration_profile": str(task.metadata.get("work_item_orchestration_profile", "") or "").strip(),
        }

    def _build_execution_capability_matrix(self, task: Task, available: list[str]) -> dict[str, Any]:
        native_caps = self.llm.get_capabilities(task_type="coding") if self.llm else None
        matrix: dict[str, Any] = {
            "native": {
                "model": getattr(native_caps, "model", ""),
                "supports_streaming": bool(getattr(native_caps, "supports_streaming", True)),
                "supports_tool_calling": bool(getattr(native_caps, "supports_tool_calling", True)),
                "supports_streaming_tool_calls": bool(getattr(native_caps, "supports_streaming_tool_calls", True)),
                "supports_multimodal": bool(getattr(native_caps, "supports_multimodal", False)),
                "supports_subagents": bool((self.config.agents.native_subagents or {})),
                "supports_resume": True,
            }
        }
        if not self.adapter_registry:
            return matrix
        for name in available:
            adapter = self.adapter_registry.get(name)
            matrix[name] = {
                "interactive": bool(adapter.supports_interactive()) if adapter else False,
                "supports_resume": True,
                "kind": "external",
            }
        return matrix

    def _prefer_native_for_current_context(
        self,
        task: Task,
        role: Any | None,
        capability_matrix: dict[str, Any],
    ) -> tuple[bool, str]:
        context = self._build_execution_context_summary(task, role)
        native_caps = dict(capability_matrix.get("native", {}) or {})
        if context["has_runtime_resume"]:
            return True, "resume_prefers_native_v2"
        if context["active_subagents"] or context["pending_permission_requests"]:
            return True, "active_runtime_state_prefers_native_v2"
        if context["attachment_count"] and native_caps.get("supports_multimodal"):
            return True, "native_multimodal_context"
        if context.get("work_item_orchestration_profile") == "company_execute_native_first":
            return True, "company_execute_native_first"
        if context["turn_type"] in {"review", "approval", "plan"}:
            return True, "native_friendly_turn_type"
        return False, ""

    def _fallback_select_task_execution_agent(
        self,
        task: Task,
        role: Any,
        available: list[str],
    ) -> tuple[str | None, dict[str, Any]]:
        preferred = (
            str(task.assigned_external_agent or "").strip()
            or str(task.metadata.get("preferred_external_agent") or "").strip()
            or str(getattr(role, "preferred_external_agent", "") or "").strip()
        )
        router_preferred = str(task.metadata.get("router_preferred_agent") or "").strip()
        strategy = str(
            task.metadata.get("work_item_execution_strategy")
            or self._get_role_runtime_value(role, "execution_strategy", "auto")
            or "auto"
        ).lower()
        turn_type = self._get_role_runtime_value(role, "default_turn_type", "work").lower()
        complexity, reasons = self._estimate_task_complexity(task, role)
        capability_matrix = self._build_execution_capability_matrix(task, available)
        current_context = self._build_execution_context_summary(task, role)
        prefer_native, native_reason = self._prefer_native_for_current_context(task, role, capability_matrix)

        selected: str | None = None
        decision_reason = "native_default"
        if not available:
            decision_reason = "no_external_agents_available"
        elif prefer_native:
            decision_reason = native_reason
        elif strategy == "native":
            decision_reason = "role_or_work_item_forces_native"
        elif strategy == "external":
            selected = preferred if preferred in available else available[0]
            decision_reason = "role_or_work_item_forces_external"
        else:
            should_use_external = False
            if preferred and preferred in available and turn_type == "work" and complexity >= 2:
                should_use_external = True
                decision_reason = "preferred_external_for_complex_work_item"
            elif complexity >= 3:
                should_use_external = True
                decision_reason = "high_complexity_task"
            elif strategy == "mixed" and complexity >= 2:
                should_use_external = True
                decision_reason = "mixed_strategy_complex_task"
            elif router_preferred and router_preferred in available and complexity >= 2:
                should_use_external = True
                decision_reason = "router_preferred_external_for_complex_task"

            if should_use_external:
                if preferred and preferred in available:
                    selected = preferred
                elif router_preferred and router_preferred in available:
                    selected = router_preferred
                else:
                    selected = available[0]

        metadata = {
            "selected": selected or "native",
            "strategy": strategy,
            "role_id": task.assigned_to,
            "turn_type": turn_type,
            "complexity_score": complexity,
            "reasons": reasons,
            "decision_reason": decision_reason,
            "available_external_agents": list(available),
            "selection_source": "fallback_rules",
            "capability_matrix": capability_matrix,
            "current_execution_context": current_context,
        }
        return selected, metadata

    async def _select_task_execution_agent_via_llm(
        self,
        task: Task,
        role: Any,
        available: list[str],
    ) -> tuple[str | None, dict[str, Any]] | None:
        if not self.llm or not self.llm.has_credentials():
            # No LLM key configured: the selection calls would fail auth on every
            # retry. Skip straight to rule-based selection so a keyless setup with
            # an external agent still runs without wasted, doomed LLM attempts.
            return None

        preferred = (
            str(task.assigned_external_agent or "").strip()
            or str(task.metadata.get("preferred_external_agent") or "").strip()
            or str(getattr(role, "preferred_external_agent", "") or "").strip()
        ) or None
        router_preferred = str(task.metadata.get("router_preferred_agent") or "").strip() or None
        strategy = str(
            task.metadata.get("work_item_execution_strategy")
            or self._get_role_runtime_value(role, "execution_strategy", "auto")
            or "auto"
        ).lower()
        turn_type = self._get_role_runtime_value(role, "default_turn_type", "work").lower()

        base_payload = {
            "task": {
                "title": task.title,
                "description": task.description,
                "assigned_to": task.assigned_to,
                "tags": list(task.tags),
                "dependencies": list(task.dependencies),
            },
            "role": {
                "role_id": getattr(role, "role_id", ""),
                "name": getattr(role, "name", ""),
                "responsibility": getattr(role, "responsibility", ""),
                "preferred_external_agent": getattr(role, "preferred_external_agent", None),
                "runtime_policy": getattr(role, "runtime_policy", {}),
            },
            "execution_context": {
                "execution_mode": task.metadata.get("execution_mode", ""),
                **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                "turn_type": turn_type,
                "execution_strategy": strategy,
                "router_preferred_agent": router_preferred,
                "task_preferred_external_agent": preferred,
                "original_message": str(task.metadata.get("original_message", "")),
            },
            "available_external_agents": available,
            "capability_matrix": self._build_execution_capability_matrix(task, available),
        }

        retry_feedback: list[dict[str, str]] = []
        max_attempts = 3
        valid_choices = {"native", *available}

        for attempt in range(1, max_attempts + 1):
            payload = dict(base_payload)
            if retry_feedback:
                payload["retry_feedback"] = list(retry_feedback)
            try:
                raw = await self.llm.simple_chat(
                    prompt=json.dumps(payload, ensure_ascii=False),
                    system=AGENT_SELECTION_PROMPT,
                    task_type="quick_tasks",
                )
                text = raw.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                    if text.endswith("```"):
                        text = text[:-3]
                    text = text.strip()
                data = json.loads(text)
                selected_raw = str(data.get("selected_agent", "native")).strip().lower()
                reasoning = str(data.get("reasoning", "")).strip()

                if selected_raw not in valid_choices:
                    issue = (
                        f"Invalid selected_agent `{selected_raw}`. "
                        f"Choose exactly one of: {', '.join(sorted(valid_choices))}."
                    )
                    retry_feedback.append({
                        "attempt": str(attempt),
                        "issue": issue,
                        "previous_response_excerpt": text,
                    })
                    logger.warning(f"Agent selector retry {attempt}/{max_attempts}: {issue}")
                    continue

                selected = None if selected_raw == "native" else selected_raw
                metadata = {
                    "selected": selected or "native",
                    "strategy": strategy,
                    "role_id": task.assigned_to,
                    "turn_type": turn_type,
                    "decision_reason": reasoning or "llm_selected_agent",
                    "available_external_agents": list(available),
                    "selection_source": "llm",
                    "llm_attempts": attempt,
                    "capability_matrix": base_payload["capability_matrix"],
                    "current_execution_context": self._build_execution_context_summary(task, role),
                }
                return selected, metadata
            except json.JSONDecodeError as e:
                issue = f"Response was not valid JSON: {e.msg} at char {e.pos}."
                retry_feedback.append({
                    "attempt": str(attempt),
                    "issue": issue,
                    "previous_response_excerpt": raw if 'raw' in locals() and isinstance(raw, str) else "",
                })
                logger.warning(f"Agent selector retry {attempt}/{max_attempts}: {issue}")
            except Exception as e:
                issue = f"LLM call failed: {e}"
                retry_feedback.append({
                    "attempt": str(attempt),
                    "issue": issue,
                    "previous_response_excerpt": "",
                })
                logger.warning(f"Agent selector retry {attempt}/{max_attempts}: {issue}")

        logger.warning("LLM agent selection exhausted retries; falling back to rules")
        return None

    async def _assign_task_execution_agent(self, task: Task, role: Any | None = None) -> str | None:
        assert self.org_engine
        task.metadata = dict(task.metadata)
        resume_pin = dict(
            task.metadata.get("_company_runtime_resume_execution_agent_pin", {})
            or {}
        )
        if resume_pin:
            available_for_audit: list[str] = []
            selected_name = normalize_recruitment_agent_choice(
                resume_pin.get("selected_execution_agent"),
                default=(
                    str(resume_pin.get("assigned_external_agent", "") or "").strip()
                    or "native"
                ),
            ) or "native"
            assigned_name = str(
                resume_pin.get("assigned_external_agent", "") or ""
            ).strip()
            if selected_name == "native":
                if assigned_name:
                    raise RuntimeError(
                        f"company runtime resume agent pin is inconsistent for task {task.id}"
                    )
                selected: str | None = None
            else:
                if assigned_name and assigned_name != selected_name:
                    raise RuntimeError(
                        f"company runtime resume agent pin is inconsistent for task {task.id}"
                    )
                available_for_audit = self._available_external_agents()
                if selected_name not in available_for_audit:
                    raise RuntimeError(
                        "company runtime resume requires unavailable external agent "
                        f"{selected_name!r} for task {task.id}"
                    )
                selected = selected_name
            task.assigned_external_agent = selected
            task.metadata["selected_execution_agent"] = selected_name
            task.metadata["preferred_external_agent"] = selected
            task.metadata["agent_selection"] = {
                "selected": selected_name,
                "strategy": (
                    WorkItemExecutionStrategy.NATIVE.value
                    if selected_name == "native"
                    else WorkItemExecutionStrategy.EXTERNAL.value
                ),
                "role_id": task.assigned_to
                or task.metadata.get("work_item_role_id", ""),
                "decision_reason": "company_runtime_resume_checkpoint_pin",
                "selection_source": "company_runtime_resume_checkpoint",
                "checkpoint_id": str(resume_pin.get("checkpoint_id", "") or ""),
                "original_selection_source": str(
                    resume_pin.get("selected_execution_agent_source", "") or ""
                ),
                "available_external_agents": (
                    available_for_audit
                ),
            }
            # The pin belongs to this dispatch attempt.  Persisting the
            # resulting choice is useful audit state, but leaving the pin set
            # would silently turn an adaptive role into a permanent lock.
            task.metadata.pop(
                "_company_runtime_resume_execution_agent_pin",
                None,
            )
            return selected
        locked_agent = normalize_recruitment_agent_choice(
            task.metadata.get("selected_execution_agent"),
            default=("native" if not str(task.assigned_external_agent or "").strip() else str(task.assigned_external_agent or "").strip()),
        )
        if task.metadata.get("execution_agent_locked") and locked_agent:
            selected = None if locked_agent == "native" else locked_agent
            task.assigned_external_agent = selected
            task.metadata["preferred_external_agent"] = selected
            task.metadata["agent_selection"] = {
                "selected": locked_agent,
                "strategy": (
                    WorkItemExecutionStrategy.NATIVE.value
                    if locked_agent == "native"
                    else WorkItemExecutionStrategy.EXTERNAL.value
                ),
                "role_id": task.assigned_to or task.metadata.get("work_item_role_id", ""),
                "decision_reason": "explicit_recruitment_agent_override",
                "available_external_agents": self._available_external_agents(),
                "selection_source": "explicit_recruitment_override",
            }
            return selected
        if self._should_force_native_execution(task):
            task.assigned_external_agent = None
            task.metadata["agent_selection"] = {
                "selected": "native",
                "strategy": "native",
                "role_id": task.assigned_to or task.metadata.get("work_item_role_id", ""),
                "decision_reason": "explicit_native_override",
                "available_external_agents": [],
                "selection_source": "forced_native",
            }
            return None

        available = self._available_external_agents()
        role = role or self._resolve_task_role(task)

        llm_choice = await self._select_task_execution_agent_via_llm(task, role, available)
        if llm_choice is not None:
            selected, metadata = llm_choice
        else:
            selected, metadata = self._fallback_select_task_execution_agent(task, role, available)
            metadata["llm_attempts"] = 3 if (self.llm and self.llm.has_credentials()) else 0

        task.assigned_external_agent = selected
        task.metadata["agent_selection"] = metadata
        return selected

    def _should_use_external_pool(self, task: Task) -> bool:
        agent = task.assigned_external_agent
        return bool(agent and agent != "native")

    def _get_external_candidates(self, task: Task) -> list[tuple[str, Any]]:
        if not self.adapter_registry or not self._should_use_external_pool(task):
            return []
        preferred = task.assigned_external_agent
        ordered = self.adapter_registry.get_ordered_available()
        if not preferred:
            return ordered

        preferred_adapter = self.adapter_registry.get(preferred)
        if not preferred_adapter:
            return ordered

        selection = dict(task.metadata.get("agent_selection", {}) or {})
        if (
            str(selection.get("selection_source", "") or "").strip()
            == "company_runtime_resume_checkpoint"
        ):
            # Resume owns one exact execution backend.  Returning alternates
            # here would undermine the checkpoint pin after the selector has
            # consumed its one-shot marker.
            return [(preferred, preferred_adapter)]

        remaining = [(name, adapter) for name, adapter in ordered if name != preferred]
        return [(preferred, preferred_adapter), *remaining]

    @staticmethod
    def _workspace_root_from_output_root(output_root: str | None) -> str | None:
        raw = str(output_root or "").strip()
        if not raw:
            return None
        try:
            output_path = Path(raw).expanduser().resolve()
        except Exception:
            return None
        parent = output_path.parent
        if parent and str(parent) not in {"/", ".", ""}:
            return str(parent)
        return str(output_path)

    async def _resolve_workspace_root(
        self,
        session_id: str | None = None,
        *,
        target_output_dir: str | None = None,
    ) -> str | None:
        session_defaults = await self._load_session_execution_defaults(session_id)
        sticky_workspace = str(session_defaults.get("workspace_root") or "").strip()
        if sticky_workspace:
            return sticky_workspace
        sticky_comms_workspace = str(session_defaults.get("comms_workspace_root") or "").strip()
        if sticky_comms_workspace:
            return sticky_comms_workspace
        sticky_output = str(session_defaults.get("target_output_dir") or "").strip()
        if sticky_output:
            inferred = self._workspace_root_from_output_root(sticky_output)
            if inferred:
                return inferred
        inferred = self._workspace_root_from_output_root(target_output_dir)
        if inferred:
            return inferred
        project_id = str(self.project_id or "default").strip() or "default"
        workplace = get_project_workplace(project_id)
        workplace.mkdir(parents=True, exist_ok=True)
        return str(workplace.resolve())

    async def _resolve_workspace_contract(
        self,
        message: str,
        session_id: str | None = None,
    ) -> dict[str, str]:
        _ = message
        session_defaults = await self._load_session_execution_defaults(session_id)
        sticky_output_root = str(session_defaults.get("target_output_dir") or "").strip()
        output_root = sticky_output_root

        workspace_root = await self._resolve_workspace_root(
            session_id,
            target_output_dir=output_root,
        )
        sticky_comms_workspace = str(session_defaults.get("comms_workspace_root") or "").strip()
        comms_workspace_root = sticky_comms_workspace or workspace_root or self._resolve_comms_workspace_root(output_root)
        comms_root = (
            str(Path(comms_workspace_root).expanduser().resolve() / ".opc-comms")
            if comms_workspace_root else ""
        )
        return {
            "workspace_root": str(workspace_root or "").strip(),
            "output_root": str(output_root or "").strip(),
            "comms_workspace_root": str(comms_workspace_root or "").strip(),
            "comms_root": comms_root,
        }

    async def _load_session_execution_defaults(self, session_id: str | None) -> dict[str, Any]:
        if not self.store or not session_id or not hasattr(self.store, "get_session"):
            return {}
        session = await self.store.get_session(session_id)
        if not session:
            return {}
        defaults = session.metadata.get("execution_defaults", {})
        return dict(defaults) if isinstance(defaults, dict) else {}

    async def _remember_session_execution_defaults(
        self,
        session_id: str | None,
        decision: ModeSelection,
        *,
        target_output_dir: str | None,
        workspace_root: str | None = None,
        comms_workspace_root: str | None = None,
        comms_root: str | None = None,
    ) -> None:
        if (
            not self.store
            or not session_id
            or not hasattr(self.store, "get_session")
            or not hasattr(self.store, "save_session")
        ):
            return
        session = await self.store.get_session(session_id)
        if not session:
            return
        metadata = dict(session.metadata)
        previous = metadata.get("execution_defaults", {})
        previous_defaults = dict(previous) if isinstance(previous, dict) else {}
        metadata["execution_defaults"] = {
            **previous_defaults,
            "mode": decision.mode.value,
            "company_profile": decision.company_profile or previous_defaults.get("company_profile", ""),
            "preferred_agent": decision.preferred_agent or previous_defaults.get("preferred_agent", ""),
            "target_output_dir": target_output_dir or previous_defaults.get("target_output_dir", ""),
            "workspace_root": workspace_root or previous_defaults.get("workspace_root", ""),
            "comms_workspace_root": comms_workspace_root or previous_defaults.get("comms_workspace_root", ""),
            "comms_root": comms_root or previous_defaults.get("comms_root", ""),
            "updated_at": datetime.now().isoformat(),
        }
        session.metadata = metadata
        session.updated_at = datetime.now()
        await self.store.save_session(session)

    async def _sync_origin_task_execution_context(
        self,
        origin_task_id: str | None,
        *,
        session_id: str,
        decision: ModeSelection,
        workspace_contract: dict[str, Any],
        original_message: str = "",
        origin_channel: str = "",
        origin_chat_id: str = "",
        origin_thread_id: str = "",
        attachment_refs: list[dict[str, Any]] | None = None,
    ) -> None:
        """Keep the canonical root/UI task aligned with the resolved execution context.

        Office UI sessions use a user-facing root task as the stable session anchor,
        while company-mode execution fans out into child tasks. The root task must
        still carry the resolved workspace/comms metadata so session-scoped features
        such as the Comms panel can resolve the collaboration tree directly from the
        current session anchor instead of depending on child-task side effects.
        """
        if not self.store or not origin_task_id:
            return
        task = await self.store.get_task(origin_task_id)
        if task is None:
            return

        metadata = dict(task.metadata or {})
        task.project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        task.session_id = str(task.session_id or session_id or "").strip() or task.session_id

        company_profile = (
            decision.company_profile
            or str(metadata.get("company_profile", "") or "").strip()
        )
        profile_key = str(company_profile or "").strip().lower()
        if decision.mode == ExecutionMode.COMPANY_MODE:
            exec_mode = "org" if profile_key == "custom" else "company"
        else:
            exec_mode = "task"
        if exec_mode == "company":
            company_profile_value = "corporate"
        elif exec_mode == "org":
            company_profile_value = "custom"
        else:
            company_profile_value = str(metadata.get("company_profile", "") or "").strip()
        metadata.update({
            "exec_mode": exec_mode,
            "company_profile": company_profile_value,
            "preferred_agent": decision.preferred_agent or str(metadata.get("preferred_agent", "") or "").strip(),
            "execution_mode": decision.mode.value,
            "workspace_root": str(workspace_contract.get("workspace_root", "") or "").strip(),
            "output_root": str(workspace_contract.get("output_root", "") or "").strip(),
            "target_output_dir": str(workspace_contract.get("output_root", "") or "").strip(),
            "comms_workspace_root": str(workspace_contract.get("comms_workspace_root", "") or "").strip(),
            "comms_root": str(workspace_contract.get("comms_root", "") or "").strip(),
            "origin_task_id": str(origin_task_id).strip(),
        })
        if exec_mode != "org":
            metadata.pop("org_id", None)
            metadata.pop("organization_id", None)
        if original_message:
            metadata["original_message"] = original_message
        if origin_channel:
            metadata["origin_channel"] = origin_channel
        if origin_chat_id:
            metadata["origin_chat_id"] = origin_chat_id
        if origin_thread_id:
            metadata["origin_thread_id"] = origin_thread_id
        if attachment_refs:
            metadata["attachment_refs"] = self._normalize_attachment_refs(attachment_refs)
            metadata["attachment_context"] = self._build_attachment_context(attachment_refs)

        task.metadata = metadata
        await self.store.save_task(task)

    async def _configure_external_adapter_for_task(self, task: Task, adapter: Any) -> tuple[Any, dict[str, Any]]:
        run_adapter = self._clone_external_adapter(adapter)
        resume_metadata: dict[str, Any] = {}
        resume_scope_id = str(
            (task.metadata or {}).get("external_resume_session_scope_id", "")
            or ""
        ).strip()
        if is_top_level_company_session(task) and not external_resume_allowed_for_scope(task, resume_scope_id=resume_scope_id):
            task.metadata = dict(task.metadata or {})
            task.metadata.pop("external_resume_session_id", None)
            task.metadata.pop("external_resume_session_scope_id", None)
            task.metadata.pop("external_resume_agent_type", None)
            cloned_config = run_adapter.config.model_copy(deep=True) if hasattr(run_adapter.config, "model_copy") else run_adapter.config
            if hasattr(cloned_config, "session_mode"):
                cloned_config.session_mode = "new"
            if hasattr(cloned_config, "session_id"):
                cloned_config.session_id = ""
            run_adapter = run_adapter.__class__(config=cloned_config)
        if not self._task_requests_external_resume(task):
            return run_adapter, resume_metadata
        supports_resume = bool(
            run_adapter.supports_session_resume()
            if hasattr(run_adapter, "supports_session_resume")
            else str(getattr(run_adapter.config, "resume_session_flag", "") or "").strip()
        )
        if not supports_resume:
            return run_adapter, resume_metadata
        metadata_agent_type = str(task.metadata.get("external_resume_agent_type", "") or "").strip()
        metadata_session_token = str(task.metadata.get("external_resume_session_id", "") or "").strip()
        metadata_token_is_unusable = bool(
            metadata_session_token
            and (
                not metadata_agent_type
                or metadata_agent_type != run_adapter.agent_type
            )
        )
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        session_token = (
            metadata_session_token
            if metadata_session_token
            and metadata_agent_type == run_adapter.agent_type
            and is_provider_session_token(
                metadata_session_token,
                agent_type=run_adapter.agent_type,
                project_id=project_id,
            )
            else ""
        )
        if session_token and await self._checkpoint_external_resume_token_was_terminalized(
            task,
            agent_type=run_adapter.agent_type,
            token=session_token,
            checkpoint_updated_at=str(
                task.metadata.get("external_resume_checkpoint_session_updated_at", "")
                or ""
            ).strip(),
            checkpoint_status=str(
                task.metadata.get("external_resume_checkpoint_session_status", "")
                or ""
            ).strip(),
        ):
            task.metadata = dict(task.metadata or {})
            task.metadata.pop("external_resume_session_id", None)
            task.metadata.pop("external_resume_agent_type", None)
            task.metadata.pop("external_resume_session_scope_id", None)
            task.metadata["external_resume_fallback"] = "context_replay_provider_terminal"
            cloned_config = (
                run_adapter.config.model_copy(deep=True)
                if hasattr(run_adapter.config, "model_copy")
                else run_adapter.config
            )
            if hasattr(cloned_config, "session_mode"):
                cloned_config.session_mode = "new"
            if hasattr(cloned_config, "session_id"):
                cloned_config.session_id = ""
            return run_adapter.__class__(config=cloned_config), resume_metadata
        latest_session = (
            await self._load_best_external_resume_session_for_task(task)
            or await self._load_latest_external_session_for_task(task)
        )
        if latest_session and str(getattr(latest_session, "agent_type", "") or "").strip() != run_adapter.agent_type:
            latest_session = None
        if not session_token:
            session_token = provider_token_from_external_session(
                latest_session,
                agent_type=run_adapter.agent_type,
                project_id=project_id,
            )
        if not session_token and not latest_session and metadata_token_is_unusable:
            return run_adapter, resume_metadata
        if not session_token:
            return run_adapter, resume_metadata
        cloned_config = run_adapter.config.model_copy(deep=True) if hasattr(run_adapter.config, "model_copy") else run_adapter.config
        if hasattr(cloned_config, "session_mode"):
            cloned_config.session_mode = "resume"
        if hasattr(cloned_config, "session_id"):
            cloned_config.session_id = session_token
        run_adapter = run_adapter.__class__(config=cloned_config)
        resume_metadata = {
            "resume_source_session": str(getattr(latest_session, "session_id", "") or "").strip(),
            "resume_session_token": session_token,
            "resume_session_mode": "resume",
            "resume_agent_type": run_adapter.agent_type,
        }
        return run_adapter, resume_metadata

    def _resolve_external_workspace(self, task: Task) -> str:
        workspace_root = (
            str(task.metadata.get("workspace_root", "") or "").strip()
            or str(task.metadata.get("comms_workspace_root", "") or "").strip()
            or str(task.metadata.get("target_output_dir", "") or "").strip()
        )
        if workspace_root:
            target = Path(workspace_root).expanduser()
            try:
                target.mkdir(parents=True, exist_ok=True)
                return str(target.resolve())
            except Exception as e:
                logger.warning(f"Failed to prepare external workspace {workspace_root}: {e}")

        workspace = get_project_workplace(task.project_id or "default")
        workspace.mkdir(parents=True, exist_ok=True)
        return str(workspace.resolve())

    def _resolved_execution_agent_name_for_task(self, task: Task) -> str:
        role_id = str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()
        role = self.org_engine.get_agent(role_id) if self.org_engine and role_id else None
        preferred_external = (
            str(task.assigned_external_agent or "").strip()
            or str(task.metadata.get("preferred_external_agent", "") or "").strip()
            or str(getattr(role, "preferred_external_agent", "") or "").strip()
        )
        selected = normalize_recruitment_agent_choice(
            task.metadata.get("selected_execution_agent"),
            default=("native" if not preferred_external else preferred_external),
        )
        return selected or ("native" if not preferred_external else preferred_external) or "native"

    @staticmethod
    def _role_prompt_user_payload(payload: dict[str, Any]) -> str:
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        return (
            "Assessment payload:\n"
            "```json\n"
            f"{rendered}\n"
            "```\n\n"
            "Return JSON only."
        )

    @staticmethod
    def _role_prompt_external_description(system_prompt: str, payload: dict[str, Any]) -> str:
        return (
            f"{str(system_prompt or '').strip()}\n\n"
            f"{OPCEngine._role_prompt_user_payload(payload)}"
        ).strip()

    def _build_role_prompt_task(
        self,
        source_task: Task,
        *,
        prompt_kind: str,
        description: str,
        execution_agent: str,
        system_prompt: str = "",
        force_new_session: bool = True,
    ) -> Task:
        role_id = str(source_task.assigned_to or source_task.metadata.get("work_item_role_id", "") or "").strip()
        session_id = str(source_task.session_id or source_task.parent_session_id or "").strip() or None
        metadata = {
            "prompt_kind": prompt_kind,
            "source_task_id": str(source_task.id or "").strip(),
            "source_task_title": str(source_task.title or "").strip(),
            "selected_execution_agent": execution_agent,
            "workspace_root": str(source_task.metadata.get("workspace_root", "") or "").strip(),
            "comms_workspace_root": str(source_task.metadata.get("comms_workspace_root", "") or "").strip(),
            "target_output_dir": str(source_task.metadata.get("target_output_dir", "") or "").strip(),
            "_disable_live_inbox_interrupts": True,
            "attachment_refs": [],
        }
        if execution_agent != "native":
            metadata["preferred_external_agent"] = execution_agent
        if system_prompt:
            metadata["_runtime_system_prompt_override"] = str(system_prompt or "").strip()
        if force_new_session:
            metadata.pop("external_resume_session_id", None)
            metadata.pop("external_resume_session_scope_id", None)
            metadata.pop("external_resume_agent_type", None)
        else:
            source_metadata = dict(source_task.metadata or {})
            for key in (
                "work_item_runtime",
                "work_item_runtime_version",
                "execution_mode",
                "company_profile",
                "work_item_role_id",
                "work_item_projection_id",
                "work_item_turn_type",
                "delegation_role_session_id",
                "delegation_seat_id",
                "external_resume_session_id",
                "external_resume_session_scope_id",
                "external_resume_agent_type",
                "employee_assignment",
                "selected_execution_agent",
                "preferred_external_agent",
            ):
                if key in source_metadata and source_metadata.get(key) not in (None, "", [], {}):
                    metadata[key] = copy.deepcopy(source_metadata[key])
            metadata.setdefault("work_item_runtime", bool(source_metadata.get("work_item_runtime", False)))
        return Task(
            id=f"{str(source_task.id or 'task').strip() or 'task'}::{prompt_kind}::{uuid.uuid4().hex}",
            session_id=session_id,
            parent_session_id=str(source_task.parent_session_id or "").strip() or None,
            title=prompt_kind.replace("_", " ").title(),
            description=description,
            assigned_to=role_id,
            status=TaskStatus.PENDING,
            assigned_external_agent=None if execution_agent == "native" else execution_agent,
            project_id=str(source_task.project_id or self.project_id or "default"),
            tags=list(source_task.tags or []),
            context_snapshot={"skip_session_history": True},
            metadata=metadata,
            org_id=source_task.org_id,
        )

    async def _run_role_prompt_via_task_execution_agent(
        self,
        source_task: Task,
        system_prompt: str,
        payload: dict[str, Any],
        prompt_kind: str,
        force_new_session: bool = True,
    ) -> str | None:
        execution_agent = self._resolved_execution_agent_name_for_task(source_task)
        if execution_agent == "native":
            prompt_task = self._build_role_prompt_task(
                source_task,
                prompt_kind=prompt_kind,
                description=self._role_prompt_user_payload(payload),
                execution_agent="native",
                system_prompt=system_prompt,
                force_new_session=force_new_session,
            )
            result = await self._run_native_agent(prompt_task)
            return str(result.content or "").strip() if result.status == TaskStatus.DONE else None

        if not self.adapter_registry or not self.external_broker:
            return None
        adapter = self.adapter_registry.get(execution_agent)
        if adapter is None:
            return None
        prompt_task = self._build_role_prompt_task(
            source_task,
            prompt_kind=prompt_kind,
            description=self._role_prompt_external_description(system_prompt, payload),
            execution_agent=execution_agent,
            force_new_session=force_new_session,
        )
        run_adapter, _ = await self._configure_external_adapter_for_task(prompt_task, adapter)
        if force_new_session:
            adapter_config = getattr(run_adapter, "config", None)
            if adapter_config is not None:
                cloned_config = (
                    adapter_config.model_copy(deep=True)
                    if hasattr(adapter_config, "model_copy")
                    else adapter_config
                )
                if hasattr(cloned_config, "session_mode"):
                    cloned_config.session_mode = "new"
                if hasattr(cloned_config, "session_id"):
                    cloned_config.session_id = ""
                run_adapter = run_adapter.__class__(config=cloned_config)
        workspace = self._resolve_external_workspace(source_task)
        prepared_task = await self._build_external_agent_task(copy.deepcopy(prompt_task))
        result = await self.external_broker.run(
            adapter=run_adapter,
            task=prompt_task,
            workspace_path=workspace,
            prepared_task=prepared_task,
        )
        if result.status != TaskStatus.DONE:
            logger.debug(
                f"Role prompt `{prompt_kind}` failed via `{execution_agent}` for source task `{source_task.id}`: "
                f"{str(result.content or '').strip()}"
            )
            return None
        return str(result.content or "").strip()

    async def _ensure_company_prompt_contract_for_external_task(self, task: Task) -> Task:
        """確保外部代理任務具有完整的 Prompt 契約。

        功能說明：
            對公司模式的外部任務，從關聯的 WorkItem 載入或產生 prompt_contract，
            並處理審查/報告目標的輔助契約。更新寫回 WorkItem metadata 和 Task metadata。

        參數：
            task (Task)：公司模式的外部代理任務。

        返回值：
            Task — 更新了 metadata 的任務（含 prompt_contract）。

        被誰引用：
            - _build_external_agent_task()：公司模式外部任務建構
        """
        if str(task.metadata.get("execution_mode", "") or "").strip() != "company_mode":
            return task
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id or self.store is None or not hasattr(self.store, "get_delegation_work_item"):
            return task
        try:
            work_item = await self.store.get_delegation_work_item(work_item_id)
        except Exception:
            work_item = None
        if work_item is None:
            return task

        metadata_updates: dict[str, Any] = {}
        work_metadata = dict(getattr(work_item, "metadata", {}) or {})
        if not has_prompt_contract(work_metadata.get("prompt_contract")):
            prompt_contract = prompt_contract_from_work_item(
                work_item,
                task_metadata=dict(task.metadata or {}),
                task_description=str(task.description or "").strip(),
            )
            metadata_updates["prompt_contract"] = prompt_contract
            if str(prompt_contract.get("source", {}).get("kind", "") or "") == "prompt_contract_blocker":
                metadata_updates["prompt_contract_blocker"] = True

        target_update_key = ""
        target_id_key = ""
        target_brief = ""
        target_title = ""
        if bool(task.metadata.get("review_execution_work_item") or work_metadata.get("review_execution_work_item")):
            target_update_key = "review_target_prompt_contract"
            target_id_key = "review_target_work_item_id"
            target_brief = str(task.metadata.get("review_target_description", "") or work_metadata.get("review_target_description", "") or "").strip()
            target_title = str(task.metadata.get("review_target_title", "") or work_metadata.get("review_target_title", "") or "").strip()
        elif bool(task.metadata.get("report_execution_work_item") or work_metadata.get("report_execution_work_item")):
            target_update_key = "report_target_prompt_contract"
            target_id_key = "report_target_work_item_id"
            target_brief = str(task.metadata.get("report_target_description", "") or work_metadata.get("report_target_description", "") or "").strip()
            target_title = str(task.metadata.get("report_target_title", "") or work_metadata.get("report_target_title", "") or "").strip()

        if target_update_key:
            target_contract = dict(work_metadata.get(target_update_key, {}) or task.metadata.get(target_update_key, {}) or {})
            if not has_prompt_contract(target_contract):
                target_work_item_id = str(task.metadata.get(target_id_key, "") or work_metadata.get(target_id_key, "") or "").strip()
                target_item = None
                if target_work_item_id:
                    try:
                        target_item = await self.store.get_delegation_work_item(target_work_item_id)
                    except Exception:
                        target_item = None
                if target_item is not None:
                    target_contract = prompt_contract_from_work_item(
                        target_item,
                        task_metadata=dict(task.metadata or {}),
                        task_description=target_brief,
                    )
                    if not has_prompt_contract(dict(getattr(target_item, "metadata", {}) or {}).get("prompt_contract")):
                        try:
                            await self.store.update_delegation_work_item(
                                target_work_item_id,
                                metadata_updates={"prompt_contract": target_contract},
                            )
                        except Exception:
                            logger.opt(exception=True).debug("Best-effort external target prompt_contract update failed")
                else:
                    target_contract = prompt_contract_from_work_item(
                        SimpleNamespace(
                            work_item_id=target_work_item_id,
                            title=target_title,
                            summary=target_brief,
                            kind="execute",
                            metadata=dict(task.metadata or {}),
                        ),
                        task_metadata=dict(task.metadata or {}),
                        task_description=target_brief,
                    )
                metadata_updates[target_update_key] = target_contract
                if target_update_key == "review_target_prompt_contract":
                    metadata_updates["prompt_contract"] = make_prompt_contract(
                        task_brief=(
                            "Review the completed child deliverable and decide whether to "
                            "approve it or request rework."
                        ),
                        target_contract=target_contract,
                        source={"kind": "review_auxiliary_work_item"},
                    )
                else:
                    metadata_updates["prompt_contract"] = make_prompt_contract(
                        task_brief=(
                            "Write a structured handoff report for the deliverable you just "
                            "completed. Do not do new execution work."
                        ),
                        target_contract=target_contract,
                        source={"kind": "report_auxiliary_work_item"},
                    )

        if metadata_updates and hasattr(self.store, "update_delegation_work_item"):
            try:
                updated = await self.store.update_delegation_work_item(
                    work_item_id,
                    metadata_updates=metadata_updates,
                )
                if updated is not None:
                    work_metadata = dict(getattr(updated, "metadata", {}) or {})
            except Exception:
                logger.opt(exception=True).debug("Best-effort external prompt_contract update failed")
                work_metadata = {**work_metadata, **metadata_updates}
        else:
            work_metadata = {**work_metadata, **metadata_updates}

        merged_task_metadata = {**dict(task.metadata or {}), **metadata_updates}
        for key in ("prompt_contract", "review_target_prompt_contract", "report_target_prompt_contract", "prompt_contract_blocker"):
            if key in work_metadata:
                merged_task_metadata[key] = work_metadata[key]
        task.metadata = merged_task_metadata
        return task

    @staticmethod
    def _safe_external_attachment_token(value: Any, *, default: str = "item") -> str:
        """將任意值轉為安全的附件路徑 token（僅保留字母數字和 ._-）。"""
        token = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-")
        return token[:80] if token else default

    @staticmethod
    def _safe_external_attachment_filename(filename: str) -> str:
        """清理附件檔名 — 移除路徑遍歷字元和非法字元。"""
        name = Path(str(filename or "attachment")).name
        name = name.replace("..", "").replace("/", "").replace("\\", "")
        name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
        return name[:160] if name else "attachment"

    def _attachment_store_for_ref(self, task: Task, ref: AttachmentRef) -> AttachmentStore:
        """根據附件參照的磁碟路徑推斷所屬專案並回傳對應的 AttachmentStore。"""
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        try:
            parts = Path(ref.disk_path).parts
        except Exception:
            parts = ()
        if len(parts) >= 3 and parts[0] == "projects" and parts[2] == "attachments":
            project_id = str(parts[1] or project_id).strip() or project_id
        return AttachmentStore(self.opc_home, project_id)

    def _prepare_external_attachment_context(self, task: Task) -> str:
        """將上傳附件暫存到外部代理工作區並渲染路徑上下文。

        功能說明：
            原生代理可直接將附件參照傳給 LLM 提供者，但外部 CLI 僅接收
            文字 prompt 和工作區，因此此方法將附件參照轉為具體的工作區檔案契約。

        參數：
            task (Task)：含 attachment_refs metadata 的任務。

        返回值：
            str — 渲染後的附件上下文文字（Markdown 格式）。

        被誰引用：
            - _build_external_agent_task()：建構外部代理任務時
        """
        metadata = dict(task.metadata or {})
        existing_context = str(metadata.get("attachment_context", "") or "").strip()
        refs = self._normalize_attachment_refs(metadata.get("attachment_refs"))
        if not refs:
            return existing_context

        workspace_hint = str(
            metadata.get("_external_workspace_path")
            or metadata.get("workspace_root")
            or metadata.get("target_output_dir")
            or ""
        ).strip()
        try:
            workspace = Path(workspace_hint).expanduser().resolve() if workspace_hint else Path(self._resolve_external_workspace(task))
            workspace.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"Failed to prepare external attachment workspace: {exc}")
            return existing_context

        turn_token = self._safe_external_attachment_token(
            metadata.get("runtime_v2_current_turn_id")
            or metadata.get("current_turn_id")
            or metadata.get("conversation_turn_id")
            or task.id
            or task.session_id,
            default="turn",
        )
        dest_dir = workspace / ".opc-attachments" / turn_token
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"Failed to create external attachment directory {dest_dir}: {exc}")
            return existing_context

        parts: list[str] = [
            "## Attachments",
            "Uploaded files have been staged inside the external agent workspace. "
            "Use the Agent path below when reading them; the Store path is OpenOPC's canonical copy.",
        ]
        mounted_refs: list[dict[str, Any]] = []
        remaining_budget = 5000
        hidden_count = 0

        for index, ref_dict in enumerate(refs, start=1):
            if index > 6:
                hidden_count += 1
                continue
            try:
                ref = AttachmentRef.from_dict(ref_dict)
            except Exception as exc:
                logger.warning(f"Failed to parse attachment ref for external context: {exc}")
                continue

            store = self._attachment_store_for_ref(task, ref)
            source_path: Path | None = None
            staged_path: Path | None = None
            filename = self._safe_external_attachment_filename(ref.filename)
            name_prefix = self._safe_external_attachment_token(ref.attachment_id, default=f"att-{index}")
            dest = dest_dir / f"{index:02d}-{name_prefix}-{filename}"
            copy_error = ""

            try:
                source_path = store.resolve_abs_path(ref)
                shutil.copy2(source_path, dest)
                staged_path = dest
                mounted_ref = {
                    **ref.to_dict(),
                    "agent_path": str(staged_path),
                    "workspace_relative_path": str(staged_path.relative_to(workspace)),
                    "source_disk_path": ref.disk_path,
                }
                mounted_refs.append(mounted_ref)
            except Exception as exc:
                copy_error = str(exc)
                logger.warning(f"Failed to stage attachment {ref.filename} for external agent: {exc}")

            parts.append(f"### {ref.filename}")
            parts.append(f"- MIME type: {ref.mime_type}")
            parts.append(f"- Size: {ref.size_bytes} bytes")
            if staged_path:
                parts.append(f"- Agent path: {staged_path}")
                parts.append(f"- Workspace relative path: {staged_path.relative_to(workspace)}")
            if source_path:
                parts.append(f"- Store path: {source_path}")
            elif ref.disk_path:
                parts.append(f"- Store path: {ref.disk_path}")
            if copy_error:
                parts.append(f"- Agent path unavailable: {copy_error}")

            if can_extract_text(ref.filename, ref.mime_type) and remaining_budget > 0:
                try:
                    raw = source_path.read_bytes() if source_path and source_path.is_file() else store.read_bytes(ref)
                    preview = extract_attachment_text(
                        ref.filename,
                        ref.mime_type,
                        raw,
                        max_chars=min(remaining_budget, 1800),
                    ).strip()
                except Exception as exc:
                    parts.append(f"- Inline preview unavailable: {exc}")
                    continue
                if not preview:
                    parts.append("- Inline preview: [empty file]")
                    continue
                clipped = preview[: min(remaining_budget, 1800)]
                if len(clipped) < len(preview):
                    clipped = f"{clipped}\n...[truncated]"
                parts.append("```text")
                parts.append(clipped)
                parts.append("```")
                remaining_budget -= len(clipped)
                continue

            if ref.mime_type.startswith("image/"):
                parts.append("- Note: image attachment is available by Agent path for external CLI tools that can inspect images.")
            elif ref.mime_type == "application/pdf":
                parts.append("- Note: PDF attachment is available by Agent path for external CLI tools that can inspect documents.")
            elif ref.mime_type.startswith("video/"):
                parts.append("- Note: video attachment is available by Agent path for external CLI tools that can inspect media.")
            else:
                parts.append("- Note: binary or complex document is available by Agent path.")

        if hidden_count:
            parts.append(f"- Additional attachments omitted from inline context: {hidden_count}")

        rendered = "\n".join(parts)
        task.metadata = {
            **metadata,
            "attachment_refs": refs,
            "attachment_context": rendered,
            "external_attachment_refs": mounted_refs,
            "external_attachment_dir": str(dest_dir),
        }
        return rendered

    @staticmethod
    def _mark_external_prompt_contract(task: Task) -> Task:
        """標記任務的 description 即為完整 prompt（外部代理契約）。"""
        task.metadata = {
            **dict(task.metadata or {}),
            "external_prompt_contract": "description_is_full_prompt",
        }
        return task

    async def _build_external_agent_task(self, task: Task) -> Task:
        """建構外部代理任務 — 組裝完整 prompt（上下文層、契約、工具提示、附件）。

        功能說明：
            將任務的 description 替換為包含所有上下文層的完整 prompt 文字，
            包括：運行時契約、恢復上下文、任務摘要、公司運行時上下文、
            協作上下文、附件狀態、技能摘要、記憶路徑等。

        參數：
            task (Task)：原始任務。

        返回值：
            Task — description 已被完整 prompt 替換的任務副本。

        被誰引用：
            - _run_task_once()：外部代理執行路徑
        """
        if not self.context_assembler and self.memory:
            self.context_assembler = ContextAssembler(
                memory=self.memory,
                store=self.store,
                communication=self.communication,
            )
        company_mode = str(task.metadata.get("execution_mode", "") or "").strip() == "company_mode"
        resume_mode = bool(task.metadata.get("__external_resume_session"))
        external_attachment_context = self._prepare_external_attachment_context(task)
        resume_delta = ""
        resume_metadata: dict[str, Any] = {}
        if resume_mode:
            resume_delta, resume_metadata = await self._build_external_resume_feedback_delta(task)
            if resume_metadata:
                task.metadata = {
                    **dict(task.metadata or {}),
                    **resume_metadata,
                }
            if resume_delta:
                task.metadata = {
                    **dict(task.metadata or {}),
                    "suppress_company_rework_feedback_context": True,
                }
        if company_mode:
            task = await self._ensure_company_prompt_contract_for_external_task(task)
        task_brief = str(task.description or task.title or "").strip()
        if resume_mode and not company_mode:
            if not resume_delta and not external_attachment_context:
                return self._mark_external_prompt_contract(task)
            task_copy = Task(
                id=task.id,
                session_id=task.session_id,
                parent_session_id=task.parent_session_id,
                title=task.title,
                description="\n\n".join(
                    part for part in (
                        f"## Task Brief\n{task_brief}" if task_brief else "",
                        f"## Runtime Context\n{self._demote_prompt_headings(external_attachment_context)}" if external_attachment_context else "",
                        f"## External Resume Delta\n{resume_delta}" if resume_delta else "",
                    )
                    if str(part).strip()
                ),
                assigned_to=task.assigned_to,
                status=task.status,
                priority=task.priority,
                dependencies=list(task.dependencies),
                execution_lock=task.execution_lock,
                context_snapshot=dict(task.context_snapshot),
                assigned_external_agent=task.assigned_external_agent,
                created_at=task.created_at,
                deadline=task.deadline,
                result=task.result,
                parent_id=task.parent_id,
                project_id=task.project_id,
                tags=list(task.tags),
                comments=list(task.comments),
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                metadata=dict(task.metadata),
            )
            return self._mark_external_prompt_contract(task_copy)
        role_id = task.assigned_to or task.metadata.get("work_item_role_id", "")
        layers = ExternalContextLayers()
        if self.context_assembler and hasattr(self.context_assembler, "build_external_context_layers"):
            layers = await self.context_assembler.build_external_context_layers(task, role_id=role_id)
        elif self.context_assembler and hasattr(self.context_assembler, "build_external_context"):
            legacy_ctx = await self.context_assembler.build_external_context(task, role_id=role_id)
            if company_mode:
                layers = ExternalContextLayers(company_runtime_context=legacy_ctx)
            else:
                layers = ExternalContextLayers(openopc_context=legacy_ctx)
        if external_attachment_context and external_attachment_context not in str(layers.attachments_state_context or ""):
            layers.attachments_state_context = "\n\n".join(
                part
                for part in (layers.attachments_state_context, external_attachment_context)
                if str(part or "").strip()
            )
        runtime_tool_hints = "" if (company_mode and is_report_prompt_turn(task.metadata)) else self._build_external_runtime_tool_hints(task, role_id=role_id)
        if self.skills:
            execution_mode = str(task.metadata.get("execution_mode", "") or "").strip() or None
            skills_summary = str(
                self.skills.build_skills_summary(
                    task.project_id,
                    execution_mode=execution_mode,
                    role_id=str(role_id or ""),
                    user_facing=_memory_skill_user_facing(task, str(role_id or "")),
                    final_decider_role_id=_final_decider_role_id(task),
                )
                or ""
            ).strip()
            memory_paths_context = self._build_external_memory_paths_context(
                task,
                role_id=str(role_id or ""),
                execution_mode=execution_mode,
            )
            if skills_summary:
                if company_mode:
                    layers.company_runtime_context = "\n\n".join(
                        part for part in (skills_summary, memory_paths_context, layers.company_runtime_context)
                        if str(part).strip()
                    )
                else:
                    layers.openopc_context = "\n\n".join(
                        part for part in (skills_summary, memory_paths_context, layers.openopc_context)
                        if str(part).strip()
                    )
        from opc.layer3_agent.company_runtime_contract import build_external_company_work_item_contract

        contract_text = ""
        if company_mode:
            contract_text = build_external_company_work_item_contract(task)
        has_layer_context = any(
            str(value or "").strip()
            for value in (
                layers.openopc_context,
                layers.attachments_state_context,
                layers.company_runtime_context,
                layers.prepared_mailbox_context,
                layers.recovery_context,
            )
        )
        if not task_brief and not has_layer_context and not contract_text and not runtime_tool_hints and not resume_delta:
            return self._mark_external_prompt_contract(task)
        rendered_task_brief = task_brief
        if company_mode:
            rendered_task_brief = str(layers.primary_task_brief or "").strip()
        description_parts: list[str] = []
        if contract_text:
            description_parts.append(f"## Runtime Contract (MANDATORY)\n{contract_text}")
        if layers.recovery_context:
            recovery_delta = self._demote_prompt_headings(layers.recovery_context)
            description_parts.append(f"## Recovery Delta (MANDATORY)\n{recovery_delta}")
        if rendered_task_brief:
            description_parts.append(f"## Task Brief\n{rendered_task_brief}")
        if company_mode:
            if layers.company_runtime_context:
                description_parts.append(f"## Company Runtime Context\n{layers.company_runtime_context}")
            collaboration_context = self._build_external_collaboration_context(
                layers.prepared_mailbox_context,
                runtime_tool_hints,
            )
            if collaboration_context:
                description_parts.append(f"## Collaboration Context\n{collaboration_context}")
        else:
            if layers.openopc_context:
                description_parts.append(f"## OpenOPC Context\n{layers.openopc_context}")
        if layers.attachments_state_context:
            runtime_context = self._demote_prompt_headings(layers.attachments_state_context)
            description_parts.append(f"## Runtime Context\n{runtime_context}")
        if resume_delta:
            description_parts.append(f"## External Resume Delta\n{resume_delta}")
        if runtime_tool_hints and not company_mode:
            description_parts.append(runtime_tool_hints)
        task_copy = Task(
            id=task.id,
            session_id=task.session_id,
            parent_session_id=task.parent_session_id,
            title=task.title,
            description="\n\n".join(part for part in description_parts if str(part).strip()),
            assigned_to=task.assigned_to,
            status=task.status,
            priority=task.priority,
            dependencies=list(task.dependencies),
            execution_lock=task.execution_lock,
            context_snapshot=dict(task.context_snapshot),
            assigned_external_agent=task.assigned_external_agent,
            created_at=task.created_at,
            deadline=task.deadline,
            result=task.result,
            parent_id=task.parent_id,
            project_id=task.project_id,
            tags=list(task.tags),
            comments=list(task.comments),
            retry_count=task.retry_count,
            max_retries=task.max_retries,
            metadata=dict(task.metadata),
        )
        return self._mark_external_prompt_contract(task_copy)

    def _build_external_memory_paths_context(
        self,
        task: Task,
        *,
        role_id: str = "",
        execution_mode: str | None = None,
    ) -> str:
        """建構外部代理的記憶路徑上下文（僅最終決策者可見）。"""
        current_mode = str(execution_mode or task.metadata.get("execution_mode", "") or "").strip()
        include = current_mode == "task_mode"
        if current_mode == "company_mode":
            include = _memory_skill_user_facing(task, role_id) and str(role_id or "").strip() == _final_decider_role_id(task)
        if not include:
            return ""

        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        markdown_store = self.memory.markdown_store if self.memory else MemoryManager(self.opc_home, project_id).markdown_store
        global_path = markdown_store.ensure_memory_file(None, heading="# Global Memory")
        project_path = markdown_store.ensure_memory_file(project_id, heading=f"# Project Memory ({project_id})")
        return (
            "## Memory Paths (Canonical)\n"
            f"- OPC_MEMORY_ROOT={markdown_store.global_memory_dir}\n"
            f"- OPC_GLOBAL_MEMORY_PATH={global_path}\n"
            f"- OPC_PROJECT_MEMORY_PATH={project_path}\n"
            "- Use these absolute paths for durable memory. Do not create a separate `.opc/memory` under the workplace."
        )

    @staticmethod
    def _demote_prompt_headings(text: str, *, target_level: int = 3) -> str:
        """將 prompt 片段中的 ## 標題降級為指定層級（避免與外層標題衝突）。"""
        target_level = max(int(target_level or 3), 1)
        prefix = "#" * target_level
        lines: list[str] = []
        for raw_line in str(text or "").strip().splitlines():
            if raw_line.startswith("## ") and not raw_line.startswith(prefix + " "):
                lines.append(f"{prefix} {raw_line[3:].strip()}")
            else:
                lines.append(raw_line)
        return "\n".join(lines).strip()

    def _build_external_collaboration_context(self, *parts: str) -> str:
        """合併信箱和協作工具提示（降級標題避免 H2 噪音）。"""
        nested_parts = [
            self._demote_prompt_headings(part)
            for part in parts
            if str(part or "").strip()
        ]
        return "\n\n".join(part for part in nested_parts if part)

    async def _build_external_resume_feedback_delta(
        self,
        task: Task,
    ) -> tuple[str, dict[str, Any]]:
        """建構外部代理恢復時的審查回饋增量。

        參數：
            task (Task)：正在恢復的外部代理任務。

        返回值：
            tuple[str, dict] — (渲染後的回饋增量文字, 更新後的 metadata)。

        被誰引用：
            - _build_external_agent_task()：resume_mode 時呼叫
        """
        feedback_metadata = dict(task.metadata or {})
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id and self.store and hasattr(self.store, "get_delegation_work_item"):
            try:
                work_item = await self.store.get_delegation_work_item(work_item_id)
            except Exception:
                work_item = None
            if work_item is not None:
                feedback_metadata = {
                    **feedback_metadata,
                    **dict(getattr(work_item, "metadata", {}) or {}),
                }
        feedback = str(feedback_metadata.get("rework_feedback", "") or "").strip()
        if not feedback:
            return "", {}

        current_version = self._review_feedback_version_from_metadata(feedback_metadata)
        last_version = self._review_feedback_version_from_metadata(
            task.metadata or {},
            key="external_resume_review_feedback_version",
        )
        digest = hashlib.sha1(feedback.encode("utf-8")).hexdigest()
        last_digest = str((task.metadata or {}).get("external_resume_review_feedback_digest", "") or "").strip()
        if digest == last_digest and current_version <= last_version:
            return "", {
                "external_resume_review_feedback_version": current_version,
                "external_resume_review_feedback_digest": digest,
            }

        rendered = ""
        if self.context_assembler is not None:
            try:
                rendered = await self.context_assembler.build_rework_feedback_context(task)
            except Exception:
                rendered = ""
        if not rendered:
            rendered = self._fallback_external_resume_feedback_context(feedback_metadata, feedback)

        header = [
            "## Reviewer Delta (MANDATORY NEW CONTEXT)",
            "You are resuming the same external session.",
            "The reviewer feedback below is new and overrides stale assumptions from earlier in the thread.",
        ]
        if current_version > 0:
            header.append(f"review_feedback_version: {current_version}")
        delta = "\n".join(header).strip()
        if rendered.strip():
            delta = f"{delta}\n\n{rendered.strip()}"
        return delta, {
            "external_resume_review_feedback_version": current_version,
            "external_resume_review_feedback_digest": digest,
        }

    @staticmethod
    def _review_feedback_version_from_metadata(
        metadata: dict[str, Any] | None,
        *,
        key: str = "review_feedback_version",
    ) -> int:
        """從 metadata 提取審查回饋版本號（fallback 到 review_rework_count）。"""
        payload = dict(metadata or {})
        try:
            parsed = int(payload.get(key) or 0)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed
        if key != "review_feedback_version":
            return 0
        try:
            fallback = int(payload.get("review_rework_count") or 0)
        except (TypeError, ValueError):
            fallback = 0
        return max(fallback, 0)

    @staticmethod
    def _fallback_external_resume_feedback_context(
        metadata: dict[str, Any],
        feedback: str,
    ) -> str:
        """當 ContextAssembler 不可用時，建構簡化的審查回饋上下文。"""
        reviewer_role = str(metadata.get("review_owner_role_id", "") or "").strip()
        verdict = dict(metadata.get("structured_review_verdict", {}) or {})
        rework_count = OPCEngine._review_feedback_version_from_metadata(metadata)
        lines: list[str] = [
            "## Reviewer Feedback (Rework Required)",
            "",
            "Your previous attempt was rejected. Address the points below before continuing.",
            "",
        ]
        if reviewer_role:
            lines.append(f"Reviewer: {reviewer_role}")
        if rework_count > 0:
            lines.append(f"Rework attempt: #{rework_count}")
        if reviewer_role or rework_count > 0:
            lines.append("")
        lines.append("### Reviewer's Reject Reason")
        lines.append(feedback)
        blocking = [
            str(item).strip()
            for item in list(verdict.get("blocking_issues", []) or [])
            if str(item).strip()
        ]
        followups = [
            str(item).strip()
            for item in list(verdict.get("followups", []) or [])
            if str(item).strip()
        ]
        if blocking:
            lines.append("")
            lines.append("### Blocking Issues")
            lines.extend(f"- {item}" for item in blocking[:12])
        if followups:
            lines.append("")
            lines.append("### Follow-ups")
            lines.extend(f"- {item}" for item in followups[:12])
        return "\n".join(lines).rstrip()

    def _build_external_runtime_tool_hints(self, task: Task, *, role_id: str = "") -> str:
        """建構外部代理的協作工具使用說明。

        功能說明：
            OpenOPC 產生的外部代理透過 opc-collab CLI 與公司其他成員通訊。
            此方法渲染 CLI 使用指南、身份說明和允許的工具列表。

        參數：
            task (Task)：當前任務。
            role_id (str)：角色 ID。

        返回值：
            str — 協作工具說明文字（若未啟用協作則為空字串）。

        被誰引用：
            - _build_external_agent_task()：組裝外部 prompt 時
        """
        if not company_collaboration_enabled_for_task(task):
            return ""

        active_role = str(role_id or task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()
        runtime_state = {
            "manager_board_summary": dict(task.context_snapshot.get("manager_board_summary", {}) or {}),
        }
        role_cfg = None
        if self.org_engine is not None and active_role:
            try:
                role_cfg = self.org_engine.get_agent(active_role)
            except Exception:
                role_cfg = None

        _profile, allowed_tools = resolve_task_collaboration_tools(
            task,
            role=active_role,
            seat=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
            runtime_state=runtime_state,
            role_cfg=role_cfg,
        )
        if not allowed_tools:
            return ""

        current_work_item_id = linked_work_item_id_for_task(task)
        runtime_task_id = str(task.id or "").strip()
        primary_tools = self._primary_external_collaboration_tools(task, allowed_tools)
        lines = [
            "### Collaboration Tools",
            "Use the executable in `OPC_COLLAB_CLI` when it is set; otherwise use `opc-collab` from `PATH`.",
            "Call tools as `opc-collab <tool> --args-stdin` or `opc-collab <tool> --args-json-file <file>` with a JSON object.",
            "Avoid inline single-quoted JSON; `--args-stdin` and `--args-json-file` work consistently on Linux, macOS, and Windows.",
            "On Windows external-agent runs, do not use `--args-json` or pipe JSON into `--args-stdin`; command-line and PowerShell pipeline text can corrupt non-ASCII before it reaches `opc-collab`.",
            "For Windows collaboration calls, write the JSON object to a UTF-8 file and call `opc-collab <tool> --args-json-file <file>`.",
            "PowerShell-safe UTF-8 file write: `$enc = New-Object System.Text.UTF8Encoding $false; [System.IO.File]::WriteAllText($path, $json, $enc)`.",
            "",
            "Identity:",
            "- WorkItem ID is the collaboration identity; `$OPC_WORK_ITEM_ID` is already set for this run.",
            "- Runtime Task IDs are only execution/session carriers; never use `$OPC_TASK_ID` or `$OPC_RUNTIME_TASK_ID` as WorkItem IDs.",
        ]
        if current_work_item_id:
            lines.append("- Omit current-card IDs when a tool can infer them from `$OPC_WORK_ITEM_ID`.")
        if runtime_task_id:
            lines.append("- `$OPC_RUNTIME_TASK_ID` may appear in diagnostics only; do not copy it into collaboration arguments.")
        lines.extend([
            "",
            "Allowed tools this turn:",
        ])
        for logical_name in sorted(allowed_tools):
            lines.append(f"- `{logical_name}`")
        if primary_tools:
            contract_lines = build_external_cli_tool_contract_lines(primary_tools)
            if contract_lines:
                contract_lines[0] = "Primary argument contracts:"
                lines.append("")
                lines.extend(contract_lines)
        return "\n".join(lines)

    @staticmethod
    def _primary_external_collaboration_tools(task: Task, allowed_tools: set[str]) -> set[str]:
        """根據回合模式篩選本回合值得展開參數契約的主要協作工具。"""
        allowed = {str(tool).strip() for tool in allowed_tools if str(tool).strip()}
        turn_mode = resolve_company_turn_mode(task)
        if turn_mode == "dispatch_required":
            preferred = {"delegate_work", "modify_work_item", "delete_work_item", "manager_board_read", "inbox"}
        elif turn_mode == "review_execute":
            preferred = {"manager_board_read"}
        elif turn_mode in {"monitor_children", "synthesize_required", "deliver_required"}:
            preferred = {"manager_board_read", "modify_work_item", "delete_work_item", "inbox", "send_dm", "broadcast_issue"}
        else:
            preferred = {"inbox", "reply_message", "send_dm", "ask_peer_and_wait", "respond_meeting"}
        return allowed.intersection(preferred)
