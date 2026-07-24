"""OPC Native Agent — the primary agent implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.core.company_tools import (
    COMPANY_ALL_COLLABORATION_TOOL_NAMES,
    MULTI_TEAM_COORDINATION_TURN_MODES,
    company_collaboration_enabled_for_task,
    resolve_company_turn_mode,
    resolve_task_collaboration_tools,
)
from opc.core.config import OPCConfig
from opc.core.models import AgentInfo, AgentStatus, ExecutionMode, Task, TaskResult, TaskStatus
from opc.core.events import EventBus
from opc.core.models import OPCEvent
from opc.core.worker_envelope import classify_worker_message
from opc.llm.provider import LLMProvider
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.layer3_agent.company_runtime_contract import build_company_work_item_contract
from opc.layer3_agent.runtime_v2 import NativeRuntimeV2
from opc.layer3_agent.prompt_harness import PromptHarnessBuilder
from opc.layer3_agent.prompt_harness.builder import _final_decider_role_id, _memory_skill_user_facing
from opc.layer4_tools.output_budget import clip_text
from opc.layer4_tools.registry import ToolRegistry
from opc.layer5_memory.memory_manager import MemoryManager
from opc.layer5_memory.preference import PreferenceManager
from opc.layer5_memory.skill_library import SkillLibrary
from opc.layer6_observability.cost_tracker import CostTracker
from opc.layer3_agent.prompt_harness.sections import (
    HONEST_REPORTING_CONTRACT,
    LONG_RUNNING_SESSION_CONTRACT,
    MEMORY_TRUST_CONTRACT,
    SAFE_ACTIONS_CONTRACT,
    SUBAGENT_HARNESS_CONTRACT,
)

# ---------------------------------------------------------------------------
# Role-aware system prompt components
# ---------------------------------------------------------------------------

_CORE_HEADER = (
    "你是 {role_name}，OPC（一人公司）系統中的 AI 代理。\n"
    "角色：{responsibility}\n\n"
    "你透過使用角色可用的工具來完成任務。"
)

_TASK_MODE_CORE_HEADER = (
    "你是 {role_name}，OpenOPC 任務執行代理。\n"
    "角色：{responsibility}\n\n"
    "你透過使用角色可用的工具來完成獨立的使用者任務。"
)

_CORE_OPERATING_PRINCIPLES = """
## 核心運作原則
- 在向使用者詢問缺少的資訊之前，先使用可用的上下文和工具。
- 在明確範圍內承擔使用者的目標，並憑藉最佳可用證據持續推進。
- 對不確定性、失敗的嘗試、不可用的工具和未驗證的結果保持誠實。
- 當操作變得有風險或涉及狀態時，遵循運行時安全、報告、記憶和子代理契約。
- 使用當前的工具策略和工具 schema 作為選擇工具和精確參數的事實來源。
"""


_NATIVE_WORKING_CONTRACT = """
## 原生工作契約
- 使用任務簡報、運行時上下文、可用工具和明確的運行時附錄來選擇本輪的正確工作姿態。
- 將規劃、執行、審查、驗證和綜合作為靈活的工作姿態，而非由元數據選擇的 prompt 設定檔。
- 優先選擇具體的、有證據支持的進展，而非描述假設性的工作。
- 將實作變更限定在請求範圍內，並與專案保持一致。

## 規劃與審查實踐
- 規劃時，產出決策完整的步驟，包含明確的輸入、輸出、交接、風險和驗證目標。
- 審查時，直接檢查當前工作區和證據。不要在未檢查當前狀態的情況下批准、拒絕或重複舊發現。
- 當運行時附錄要求結構化的裁決、派遣、報告或交接格式時，嚴格遵循該附錄。

## 原生自我驗證契約
- 在最終交付前，將使用者的目標與實際變更、產出物和你觸及的路徑進行核對。
- 當你變更程式碼、檔案、UI 行為、指令或產生的產出物時，優先使用可執行證據：
  針對性測試、lint/型別檢查、冒煙指令、瀏覽器檢查或直接產出物檢查。
- 如果你無法執行相關的驗證步驟，用一句話明確說明並解釋限制。
- 當你變更了某些內容或運行時要求時，在最終回覆中包含簡短的驗證狀態。
- 如果驗證發現阻斷性問題，在可能的情況下先修復再完成。如果本輪無法修復，
  誠實報告阻斷因素，而非將工作呈現為已完成。
"""

_USER_INPUT_GUIDELINES = """
## 使用者輸入恢復
- 如果最新的使用者回覆解決了阻斷因素，繼續執行而非再次詢問。
- 如果不完整或有歧義，只詢問確切的剩餘缺口。
- 絕不重複相同的寬泛問題，也不詢問使用者已提供的內容。
"""

_TASK_MODE_ORCHESTRATION = """
## 任務模式編排
- 你是本工作階段中使用者的主要任務模式執行代理。
- 作為單一全功能代理執行；不要將任務模式建模為公司組織、招募流程、員工角色或人員分配。
- 將 `task_generalist` 角色 id 僅視為路由和日誌元數據，而非角色來源。
- 優先直接執行，而非敘述你會做什麼。
- 僅在有界並行工作或上下文隔離能改善結果時，才使用 `agent_spawn`、`agent_wait` 和 `agent_send`。
"""

# file_write / file_edit are deliberately NOT blocked: coordination turns
# produce in-context content (briefs, matrices, review notes) that must be
# persistable to the workspace, or it gets trapped in blocking DM hand-offs.
_MULTI_TEAM_COORDINATION_NATIVE_TOOL_BLOCKLIST = {
    "shell_exec",
    "apply_patch",
    "python_exec",
    "web_search",
    "web_fetch",
    "browser_navigate",
    "browser_navigate_back",
    "browser_click",
    "browser_snapshot",
    "browser_type",
    "browser_wait_for",
    "browser_scroll",
    "browser_select_option",
    "browser_take_screenshot",
    "browser_close",
    "git_status",
    "git_commit",
    "git_diff",
    "agent_spawn",
    "agent_wait",
    "agent_send",
    "agent_list",
}

_PROMPT_PROFILE_COMMUNICATION = """
## 溝通契約
- 在第一個有意義的工具操作之前，簡要說明即時計畫。
- 在較長的工作過程中，當發現根本原因、改變方向或完成有意義的里程碑時，給出簡短的進度更新。
- 最終交付必須以結果為先，並在運行時要求時包含明確的驗證狀態。
"""

_PROMPT_PROFILE_HARNESS = """
## 運行時 Harness 提醒
- 運行時可能會壓縮歷史、摘要舊輪次，並重新注入結構化的運行時產出物。
- 將重要狀態保存在任務工具和產出物中，而非僅保存在自由格式的文字中。
- 恢復工作時，信任重新注入的運行時狀態，而非重新解決舊步驟。
"""


@dataclass
class NativePromptBundle:
    """Layered prompt payload for the native runtime."""

    profile_name: str
    stable_system_prompt: str
    runtime_policy_messages: list[dict[str, Any]] = field(default_factory=list)


class PromptProfileManager:
    """Build unified native prompts with stable static sections."""

    UNIFIED_PROFILE = "unified"

    def __init__(self, role: AgentInfo, config: OPCConfig) -> None:
        self.role = role
        self.config = config

    def resolve_profile(self, task: Task) -> str:
        _ = task
        # Compatibility/observability label only. Prompt profiles are no longer
        # selected from YAML; the native prompt is intentionally unified.
        return self.UNIFIED_PROFILE

    def build_stable_system_prompt(self, task: Task) -> tuple[str, str]:
        profile = self.resolve_profile(task)
        header = _TASK_MODE_CORE_HEADER if self._is_task_mode_task(task) else _CORE_HEADER
        parts: list[str] = [
            header.format(
                role_name=self.role.name,
                responsibility=self.role.responsibility,
            ),
            _CORE_OPERATING_PRINCIPLES,
            SAFE_ACTIONS_CONTRACT,
            HONEST_REPORTING_CONTRACT,
            MEMORY_TRUST_CONTRACT,
            SUBAGENT_HARNESS_CONTRACT,
            _NATIVE_WORKING_CONTRACT,
            _USER_INPUT_GUIDELINES,
            _PROMPT_PROFILE_COMMUNICATION,
            _PROMPT_PROFILE_HARNESS,
            LONG_RUNNING_SESSION_CONTRACT,
        ]
        return profile, "\n\n".join(part for part in parts if part)

    def build_runtime_policy_messages(self, task: Task) -> list[dict[str, Any]]:
        parts: list[str] = []

        if self._is_company_mode_task(task):
            parts.append(self._build_company_work_item_contract(task))

        if self._is_task_mode_task(task):
            parts.append(_TASK_MODE_ORCHESTRATION)
        if self.role.prompt_refs and not self._is_task_generalist_role(task):
            parts.append("## 角色操作指令\n" + "\n\n".join(self.role.prompt_refs))
        runtime_prompt_addendum = str(task.metadata.get("_subagent_profile_prompt", "") or "").strip()
        if runtime_prompt_addendum:
            parts.append(f"## 運行時設定檔覆寫\n{runtime_prompt_addendum}")
        return [
            {"role": "system", "content": part}
            for part in parts
            if str(part or "").strip()
        ]

    def build_prompt_bundle(self, task: Task) -> NativePromptBundle:
        profile, stable_prompt = self.build_stable_system_prompt(task)
        return NativePromptBundle(
            profile_name=profile,
            stable_system_prompt=stable_prompt,
            runtime_policy_messages=self.build_runtime_policy_messages(task),
        )

    def build_prompt(self, task: Task) -> tuple[str, str]:
        bundle = self.build_prompt_bundle(task)
        parts = [
            bundle.stable_system_prompt,
            *[
                str(message.get("content", "") or "").strip()
                for message in bundle.runtime_policy_messages
            ],
        ]
        return bundle.profile_name, "\n\n".join(part for part in parts if part)

    @staticmethod
    def _is_task_mode_task(task: Task) -> bool:
        mode = str(task.metadata.get("mode") or "").strip().lower()
        execution_mode = str(task.metadata.get("execution_mode") or "").strip()
        return mode in {"project", "task"} or execution_mode == ExecutionMode.TASK_MODE.value

    @staticmethod
    def _is_company_mode_task(task: Task) -> bool:
        execution_mode = str(task.metadata.get("execution_mode") or "").strip()
        return execution_mode == ExecutionMode.COMPANY_MODE.value

    def _is_task_generalist_role(self, task: Task) -> bool:
        role_id = str(getattr(self.role, "role_id", "") or "").strip()
        return role_id == "task_generalist" and self._is_task_mode_task(task)

    def _build_company_work_item_contract(self, task: Task) -> str:
        return build_company_work_item_contract(task)


class NativeAgent:
    """OPC Native Agent — wraps NativeRuntimeV2 with memory, skills, and preferences."""

    def __init__(
        self,
        role: AgentInfo,
        llm: LLMProvider,
        tool_registry: ToolRegistry,
        context_assembler: ContextAssembler,
        memory: MemoryManager,
        preferences: PreferenceManager,
        skills: SkillLibrary,
        event_bus: EventBus,
        cost_tracker: CostTracker | None = None,
        config: OPCConfig | None = None,
        communication: Any | None = None,
        approval_callback: Any = None,
        permission_policy: Any = None,
    ) -> None:
        self.role = role
        self.llm = llm
        self.tool_registry = tool_registry
        self.context_assembler = context_assembler
        self.memory = memory
        self.preferences = preferences
        self.skills = skills
        self.event_bus = event_bus
        self.cost_tracker = cost_tracker
        self.config = config or OPCConfig()
        self.communication = communication
        self.approval_callback = approval_callback
        self.permission_policy = permission_policy
        self.prompt_profiles = PromptProfileManager(role, self.config)
        max_iter = self.config.system.max_agent_iterations
        comp_threshold = self.config.system.context_compression_threshold
        self.loop = NativeRuntimeV2(
            llm=llm,
            tool_registry=tool_registry,
            event_bus=event_bus,
            cost_tracker=cost_tracker,
            memory_manager=memory,
            history_compactor=getattr(memory, "history_compactor", None),
            max_iterations=max_iter,
            compression_threshold=comp_threshold,
            config=self.config,
            child_agent_factory=self._create_child_agent,
            approval_callback=approval_callback,
            permission_policy=permission_policy,
            prefetch_provider=self._build_runtime_prefetch_payload,
        )

    def _is_task_mode_task(self, task: Task) -> bool:
        mode = str(task.metadata.get("mode") or "").strip().lower()
        execution_mode = str(task.metadata.get("execution_mode") or "").strip()
        if mode in {"project", "task"}:
            return True
        return execution_mode == ExecutionMode.TASK_MODE.value

    async def execute(
        self,
        task: Task,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> TaskResult:
        """Execute a task end-to-end."""
        self.role.status = AgentStatus.RUNNING
        self.role.current_task_id = task.id

        await self.event_bus.publish(OPCEvent(
            event_type="agent_status_changed",
            payload={"role_id": self.role.role_id, "status": "running", "task_id": task.id},
        ))

        is_task_mode = self._is_task_mode_task(task)
        allowed = self._resolve_allowed_tools(task)
        inbox_interrupt_provider = None
        if (
            self.communication
            and task.metadata.get("execution_mode") == ExecutionMode.COMPANY_MODE.value
            and not bool(task.metadata.get("_disable_live_inbox_interrupts", False))
        ):
            inbox_interrupt_provider = self._create_inbox_interrupt_provider()
        runtime_inbox_queue = getattr(task, "_runtime_inbox_queue", None)
        if runtime_inbox_queue is not None:
            inbox_interrupt_provider = self._create_runtime_inbox_provider(runtime_inbox_queue, task)

        try:
            system_prompt = await self._build_system_prompt(task)
            user_message = await self._build_user_message(task)
            context_messages = await self._build_context_messages(task)

            result = await self.loop.run(
                system_prompt=system_prompt,
                user_message=user_message,
                context_messages=context_messages,
                attachment_refs=list(task.metadata.get("attachment_refs", []) or []),
                task=task,
                allowed_tools=allowed,
                on_progress=on_progress,
                inbox_interrupt_provider=inbox_interrupt_provider,
            )

            return result

        except Exception as e:
            logger.error(f"Agent {self.role.role_id} failed on task {task.id}: {e}")
            return TaskResult(status=TaskStatus.FAILED, content=str(e))

        finally:
            self.role.status = AgentStatus.IDLE
            self.role.current_task_id = None
            await self.event_bus.publish(OPCEvent(
                event_type="agent_status_changed",
                payload={"role_id": self.role.role_id, "status": "idle"},
            ))

    async def _build_native_prompt_bundle(self, task: Task) -> NativePromptBundle:
        override = str(task.metadata.get("_runtime_system_prompt_override", "") or "").strip()
        if override:
            task.metadata["runtime_prompt_profile"] = "override"
            return NativePromptBundle(
                profile_name="override",
                stable_system_prompt=override,
                runtime_policy_messages=[],
            )
        bundle = self.prompt_profiles.build_prompt_bundle(task)
        task.metadata["runtime_prompt_profile"] = bundle.profile_name
        return bundle

    async def _build_system_prompt(self, task: Task) -> str:
        bundle = await self._build_native_prompt_bundle(task)
        return bundle.stable_system_prompt

    async def _build_user_message(self, task: Task) -> str:
        return self.context_assembler.build_task_brief(task)

    async def _build_context_messages(self, task: Task) -> list[dict[str, Any]]:
        fork_messages = list(task.metadata.get("_fork_context_messages", []) or [])
        if fork_messages:
            return fork_messages

        harness_output = await self._build_prompt_harness(task)
        dynamic_messages = [
            *harness_output.runtime_policy_messages,
            *harness_output.workspace_context_messages,
            *harness_output.artifact_messages,
        ]
        session_id = getattr(task, "session_id", None)
        if not session_id:
            return dynamic_messages
        context_snapshot = task.context_snapshot if isinstance(task.context_snapshot, dict) else {}
        raw_runtime_resume = context_snapshot.get("runtime_resume")
        has_runtime_resume = isinstance(raw_runtime_resume, dict) and bool(raw_runtime_resume)
        legacy_skip_session_history = raw_runtime_resume is True
        if bool(context_snapshot.get("skip_session_history", False)) or has_runtime_resume or legacy_skip_session_history:
            return dynamic_messages
        return [
            *dynamic_messages,
            *(
                await self.memory.build_session_history_tail_messages(
                    session_id,
                    include_latest_user_turn=False,
                )
            ),
        ]

    async def _build_prompt_harness(self, task: Task) -> Any:
        allowed_tools = self._resolve_allowed_tools(task)
        prompt_bundle = await self._build_native_prompt_bundle(task)
        harness = PromptHarnessBuilder(
            task=task,
            role_id=self.role.role_id,
            config=self.config,
            context_assembler=self.context_assembler,
            preferences=self.preferences,
            skills=self.skills,
        )
        output = await harness.build(
            system_prompt=prompt_bundle.stable_system_prompt,
            allowed_tools=allowed_tools,
            runtime_policy_messages=prompt_bundle.runtime_policy_messages,
        )
        task.metadata["prompt_harness"] = {
            "static_section_ids": list(output.static_section_ids),
            "dynamic_section_ids": list(output.dynamic_section_ids),
            "artifact_manifest": list(output.artifact_manifest),
            "artifact_hashes": dict(output.artifact_hashes),
        }
        task.metadata["_prompt_harness_boot_artifacts"] = list(output.artifact_manifest)
        return output

    def _registered_general_tool_names(self) -> set[str]:
        return {
            str(tool.name or "").strip()
            for tool in self.tool_registry.list_tools()
            if str(tool.name or "").strip()
            and str(tool.name or "").strip() not in COMPANY_ALL_COLLABORATION_TOOL_NAMES
        }

    @staticmethod
    def _configured_general_tool_names(tools: list[str] | tuple[str, ...]) -> set[str]:
        return {
            str(tool or "").strip()
            for tool in list(tools or [])
            if str(tool or "").strip()
            and str(tool or "").strip() not in COMPANY_ALL_COLLABORATION_TOOL_NAMES
        }

    def _resolve_allowed_tools(self, task: Task) -> list[str] | None:
        turn_mode = resolve_company_turn_mode(task, runtime_state={})
        inherited = list(task.metadata.get("_fork_allowed_tools", []) or [])
        if inherited:
            if not company_collaboration_enabled_for_task(task):
                inherited = [
                    tool for tool in inherited
                    if tool not in COMPANY_ALL_COLLABORATION_TOOL_NAMES
                ]
            else:
                _, allowed_collab = resolve_task_collaboration_tools(
                    task,
                    role=self.role.role_id,
                    seat=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
                    runtime_state={},
                    role_cfg=self.role,
                )
                inherited = [
                    tool for tool in inherited
                    if tool not in COMPANY_ALL_COLLABORATION_TOOL_NAMES or tool in allowed_collab
                ]
            if turn_mode in MULTI_TEAM_COORDINATION_TURN_MODES:
                inherited = [
                    tool for tool in inherited
                    if tool not in _MULTI_TEAM_COORDINATION_NATIVE_TOOL_BLOCKLIST
                ]
            return inherited

        configured_general = self._configured_general_tool_names(list(self.role.tools or []))
        company_mode = company_collaboration_enabled_for_task(task)
        if company_mode:
            allowed = set(configured_general) if configured_general else self._registered_general_tool_names()
            _, allowed_collab = resolve_task_collaboration_tools(
                task,
                role=self.role.role_id,
                seat=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
                runtime_state={},
                role_cfg=self.role,
            )
            allowed.update(allowed_collab)
        elif configured_general:
            allowed = set(configured_general)
        else:
            return None

        if turn_mode in MULTI_TEAM_COORDINATION_TURN_MODES:
            allowed.difference_update(_MULTI_TEAM_COORDINATION_NATIVE_TOOL_BLOCKLIST)
        return sorted(allowed)

    async def _build_runtime_prefetch_payload(
        self,
        task: Task,
        query: str,
        _messages: list[dict[str, Any]],
    ) -> dict[str, str]:
        prefetch_cfg = self.config.system.native_runtime.prefetch
        if not prefetch_cfg.enabled:
            return {}
        payload: dict[str, str] = {}
        max_chars = max(400, int(prefetch_cfg.max_chars or 4000))
        session_id = getattr(task, "session_id", None)
        include_project_knowledge = bool(task.metadata.get("include_project_knowledge", False))
        if prefetch_cfg.session_memory and session_id:
            session_memory = (await self.memory.build_session_memory_context(session_id)).strip()
            if session_memory:
                payload["session_memory"] = clip_text(
                    session_memory,
                    limit=max_chars,
                    marker="session memory prefetch truncated",
                ).text
        if prefetch_cfg.focused_memory:
            focused = (
                await self.memory.build_focused_memory_context(
                    query=query,
                    project_id=task.project_id,
                    session_id=session_id,
                    include_project_knowledge=include_project_knowledge,
                    max_chars=max_chars,
                )
            ).strip()
            if focused:
                payload["focused_memory"] = clip_text(
                    focused,
                    limit=max_chars,
                    marker="focused memory prefetch truncated",
                ).text
        if prefetch_cfg.project_memory_candidates:
            project_memory = (
                await self.memory.build_project_memory_context(
                    project_id=task.project_id,
                    include_project_knowledge=include_project_knowledge,
                )
            ).strip()
            if project_memory:
                payload["project_memory_candidates"] = clip_text(
                    project_memory,
                    limit=max_chars,
                    marker="project memory prefetch truncated",
                ).text
        harness_cfg = self.config.system.native_runtime.prompt_harness
        skills_in_prompt_harness = bool(harness_cfg.enabled and harness_cfg.artifact_messages_enabled)
        if prefetch_cfg.skills_summary and not skills_in_prompt_harness:
            execution_mode = str(task.metadata.get("execution_mode", "") or "").strip() or None
            skills_summary = str(
                self.skills.build_skills_summary(
                    task.project_id,
                    execution_mode=execution_mode,
                    role_id=self.role.role_id,
                    user_facing=_memory_skill_user_facing(task, self.role.role_id),
                    final_decider_role_id=_final_decider_role_id(task),
                )
                or ""
            ).strip()
            if skills_summary:
                payload["skills_summary"] = clip_text(
                    skills_summary,
                    limit=max_chars,
                    marker="skills summary prefetch truncated",
                ).text
        return payload

    def _create_inbox_interrupt_provider(self) -> Any:
        communication = self.communication
        agent_role_id = self.role.role_id

        async def _provide(task: Task) -> list[dict[str, Any]]:
            if communication is None:
                return []
            return await communication.consume_live_inbox_messages(task, agent_id=agent_role_id)

        return _provide

    def _create_runtime_inbox_provider(self, inbox_queue: Any, task: Task) -> Any:
        async def _provide(_task: Task) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            while True:
                try:
                    message = inbox_queue.get_nowait()
                except Exception:
                    break
                if not message:
                    continue
                if isinstance(message, dict):
                    normalized = dict(message)
                    normalized.setdefault("from", str(normalized.get("from_agent", "runtime_subagent_parent") or "runtime_subagent_parent"))
                    normalized.setdefault("body", str(normalized.get("body", normalized.get("message", "")) or ""))
                    items.append(normalized)
                    continue
                items.append({"from": "runtime_subagent_parent", "body": str(message)})
            endpoint_id = str(task.metadata.get("_comms_endpoint_id", "") or "").strip()
            if endpoint_id:
                workspace_root = (
                    str(task.metadata.get("comms_workspace_root", "") or "").strip()
                    or str(task.metadata.get("workspace_root", "") or "").strip()
                    or str(task.metadata.get("target_output_dir", "") or "").strip()
                )
                if workspace_root:
                    try:
                        from opc.layer2_organization import comms as _comms

                        layout = _comms.resolve_layout(
                            workspace_root,
                            str(task.project_id or "default").strip() or "default",
                            str(task.parent_session_id or task.session_id or "default").strip() or "default",
                        )
                        unread = _comms.list_unread(layout, endpoint_id, limit=6)
                        injected_ids = {
                            str(item).strip()
                            for item in list(task.context_snapshot.get("runtime_inbox_injected_message_ids", []) or [])
                            if str(item).strip()
                        }
                        for header in unread:
                            msg_id = str(header.message_id or "").strip()
                            if msg_id and msg_id in injected_ids:
                                continue
                            _, body = _comms.read_message(header.path)
                            if body.strip():
                                items.append(classify_worker_message(
                                    {
                                        "from": str(header.from_role or "runtime_subagent_parent").strip() or "runtime_subagent_parent",
                                        "from_agent": str(header.from_role or "runtime_subagent_parent").strip() or "runtime_subagent_parent",
                                        "subject": str(header.subject or "").strip(),
                                        "message_id": str(header.message_id or "").strip(),
                                        "msg_id": str(header.message_id or "").strip(),
                                        "body": body.strip(),
                                        "reply_needed": bool(header.blocking),
                                        "urgency": str(header.priority or "").strip() or "normal",
                                        "transport_kind": str(header.raw_frontmatter.get("transport_kind", "") or "").strip(),
                                        "semantic_type": str(header.raw_frontmatter.get("semantic_type") or header.raw_frontmatter.get("kind") or "").strip(),
                                        "metadata": dict(header.raw_frontmatter or {}),
                                    }
                                ))
                                if msg_id:
                                    injected_ids.add(msg_id)
                        if injected_ids:
                            task.context_snapshot = dict(task.context_snapshot)
                            task.context_snapshot["runtime_inbox_injected_message_ids"] = sorted(injected_ids)[-50:]
                    except Exception:
                        pass
            return items

        return _provide

    def _create_child_agent(
        self,
        profile: str,
        allowed_tools: list[str],
        prompt_addendum: str,
        overrides: dict[str, Any] | None = None,
    ) -> "NativeAgent":
        overrides = dict(overrides or {})
        role_name = str(overrides.get("name") or f"{self.role.name} [{profile}]").strip() or f"{self.role.name} [{profile}]"
        role = AgentInfo(
            role_id=f"{self.role.role_id}:{profile}",
            name=role_name,
            responsibility=self.role.responsibility,
            status=AgentStatus.IDLE,
            current_task_id=None,
            reports_to=self.role.reports_to,
            icon=self.role.icon,
            can_spawn=list(self.role.can_spawn),
            tools=list(allowed_tools),
            preferred_external_agent=self.role.preferred_external_agent,
            prompt_refs=[*self.role.prompt_refs],
            skill_refs=[*self.role.skill_refs],
            handoff_template_ref=self.role.handoff_template_ref,
            memory_policy_ref=self.role.memory_policy_ref,
            artifact_contract_ref=self.role.artifact_contract_ref,
            runtime_policy=dict(self.role.runtime_policy),
            org_id=self.role.org_id,
            budget_monthly_cents=self.role.budget_monthly_cents,
            spent_monthly_cents=self.role.spent_monthly_cents,
            heartbeat_enabled=self.role.heartbeat_enabled,
            heartbeat_interval_sec=self.role.heartbeat_interval_sec,
            last_heartbeat_at=self.role.last_heartbeat_at,
            capabilities=self.role.capabilities,
        )
        if prompt_addendum:
            role.prompt_refs.append(prompt_addendum)
        if overrides.get("description"):
            role.prompt_refs.append(f"Subagent task summary: {str(overrides['description']).strip()}")
        if overrides.get("mode"):
            role.prompt_refs.append(f"Runtime spawn mode: {str(overrides['mode']).strip()}")

        child_llm = self.llm
        model_override = str(overrides.get("model") or "").strip()
        if model_override:
            llm_config = self.llm.config.model_copy(deep=True)
            llm_config.default_model = model_override
            child_llm = LLMProvider(llm_config, opc_home=getattr(self.llm, "opc_home", None))

        child_config = self.config
        max_iterations = overrides.get("max_iterations")
        if self.config is not None and max_iterations:
            child_config = self.config.model_copy(deep=True)
            child_config.system.max_agent_iterations = max(1, int(max_iterations))

        return NativeAgent(
            role=role,
            llm=child_llm,
            tool_registry=self.tool_registry,
            context_assembler=self.context_assembler,
            memory=self.memory,
            preferences=self.preferences,
            skills=self.skills,
            event_bus=self.event_bus,
            cost_tracker=self.cost_tracker,
            config=child_config,
            communication=self.communication,
            approval_callback=self.approval_callback,
            permission_policy=self.permission_policy,
        )
