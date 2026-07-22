"""OPC Engine — 中央協調器，將所有層級串接在一起。

職責說明：
    作為整個 OPC 系統的核心引擎，負責：
    - 初始化所有子系統（儲存、LLM、工具、代理、頻道等）
    - 接收使用者訊息並路由到任務模式或公司模式
    - 管理任務生命週期（建立、執行、完成、升級）
    - 協調公司模式的委派執行（DelegationRun → Cell → WorkItem）
    - 處理審批、升級、重組等組織行為

關聯關係：
    - 被 opc/cli/app.py 建立和驅動
    - 被 opc/plugins/office_ui/ 透過 API 驅動
    - 依賴所有 layer0~layer6 模組

使用範例：
    engine = OPCEngine(config=OPCConfig.load())
    await engine.initialize()
    result = await engine.process_message(user_message)
"""

from __future__ import annotations  # 啟用延遲型別註解評估

import asyncio  # 標準庫：非同步事件循環和協程
import copy  # 標準庫：深層複製（任務/配置複製）
import hashlib  # 標準庫：雜湊計算（ID 產生、快取鍵）
import inspect  # 標準庫：introspection（函數簽名檢查）
import json  # 標準庫：JSON 序列化/反序列化
import re  # 標準庫：正規表達式
import shutil  # 標準庫：檔案/目錄操作
import time  # 標準庫：時間戳記
import uuid  # 標準庫：UUID 產生
from contextlib import nullcontext  # 標準庫：空上下文管理器
from datetime import datetime  # 標準庫：日期時間
from pathlib import Path  # 標準庫：跨平台路徑操作
from types import SimpleNamespace  # 標準庫：簡單命名空間
from typing import Any, Callable, Coroutine  # 標準庫：型別註解

from loguru import logger  # 第三方庫：結構化日誌

# --- opc.core 核心模組 ---
from opc.core.attachment_store import AttachmentRef, AttachmentStore  # 附件儲存
from opc.core.attachment_content import can_extract_text, extract_attachment_text  # 附件文字提取
from opc.core.active_task_runs import (  # 活動任務執行註冊表
    ActiveTaskRunAdmissionClosed,
    ActiveTaskRunRegistry,
)
from opc.core.company_tools import (  # 公司模式工具函數
    company_collaboration_enabled_for_task,
    resolve_company_turn_mode,
    resolve_task_collaboration_tools,
)
from opc.core.config import (  # 系統配置
    DEFAULT_ORGANIZATION_ID,
    OPCConfig,
    company_org_path,
    get_opc_home,
    get_project_workplace,
)
from opc.core.events import EventBus  # 事件匯流排
from opc.core.models import (  # 領域資料模型
    ApprovalAction,
    ApprovalDecision,
    DelegationCell,
    DelegationEvent,
    RoleRuntimeSession,
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExecutionMode,
    MeetingRoom,
    ModeSelection,
    OPCEvent,
    Phase,
    ReorgChangeSet,
    ReorgProposal,
    ReorgProposalStatus,
    RiskLevel,

    SeatState,
    SessionLinkRecord,
    SystemMessage,
    Task,
    TaskResult,
    TaskStatus, UserMessage,
    TeamInstance,
    WorkItemExecutionStrategy,
    CompanyProfile,
)
# --- 資料庫層 ---
from opc.database.store import OPCStore  # SQLite 持久化儲存
# --- LLM 提供者 ---
from opc.llm.provider import LLMProvider  # LLM API 抽象層
# --- Layer 0：互動層 ---
from opc.layer0_interaction.message_bus import MessageBus  # 訊息匯流排
# --- 頻道管理 ---
from opc.channels import ChannelManager  # 多頻道管理器
# --- Layer 1：感知層 ---
from opc.layer1_perception.context_assembler import ContextAssembler, ExternalContextLayers  # 上下文組裝器
from opc.layer1_perception.context_loader import ContextLoader  # 上下文載入器
from opc.layer1_perception.task_router import TaskRouter  # 任務路由器
# --- Layer 2：組織層 ---
from opc.layer2_organization.org_engine import (  # 組織引擎
    OrgEngine,
    TASK_MODE_COMPANY_ONLY_TOOLS,
)
from opc.layer2_organization.task_graph import TaskGraphScheduler  # 任務圖排程器
from opc.layer2_organization.approval import ApprovalEngine  # 審批引擎
from opc.layer2_organization.escalation import EscalationEngine  # 升級引擎
from opc.layer2_organization.communication import CommunicationManager  # 通訊管理器
from opc.layer2_organization.collaboration_policy import ownership_guard_violation  # 協作策略
from opc.layer2_organization.secretary import SecretaryService  # 秘書服務
from opc.layer2_organization.company_mode import (  # 公司模式核心
    CompanyExecutorDriverOwnership,
    CompanyRuntimeSpec,
    CompanyRuntimeSpecBuilder,
    CompanyWorkItemExecutor,
    deserialize_company_runtime_spec,
    deserialize_company_work_item_runtime_plan,
    serialize_company_runtime_spec,
    serialize_company_work_item_runtime_plan,
    serialized_company_plan_from_metadata,
)
from opc.layer2_organization.company_runtime import canonical_role_session_id  # 角色工作階段 ID
from opc.layer2_organization.company_runtime_identity import (  # 公司運行時身份
    build_company_runtime_identity_index,
    is_company_runtime_task,
    is_pure_company_ui_anchor,
    load_company_runtime_identity_index,
)
from opc.layer2_organization.metadata_ownership import (  # metadata 所有權
    build_work_item_owner_execution_copy,
)
from opc.layer2_organization.phase import (  # Phase 狀態機
    DONE_PHASES,
    IN_PROGRESS_PHASES,
    IN_REVIEW_PHASES,
    InvalidPhaseTransition,
    task_status_for_phase,
)
from opc.layer2_organization.prompt_contract import (  # Prompt 契約
    has_prompt_contract,
    is_report_prompt_turn,
    make_prompt_contract,
    prompt_contract_from_work_item,
)
from opc.layer2_organization.org_work_item_planner import (  # 工作項目規劃器
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)
from opc.layer2_organization.reactivation_sweeper import CommsReactivationSweeper  # 通訊重新激活掃除器
from opc.layer2_organization.session_scoping import (  # 工作階段範圍
    external_resume_allowed_for_scope,
    is_top_level_company_session,
    task_session_scope_id,
)
from opc.layer2_organization.turn_mode import reset_manager_dispatch_turn_metadata  # 回合模式
from opc.layer2_organization.seat_executor import EngineSeatExecutor  # 席位執行器
from opc.layer2_organization.work_item_runtime import (  # 工作項目運行時
    is_work_item_runtime_metadata,
    mark_work_item_runtime,
)
from opc.layer2_organization.work_item_runtime_invariants import (  # 工作項目運行時不變量
    validate_work_item_runtime_projection,
)
from opc.layer2_organization.work_item_identity import (  # 工作項目身份
    canonical_work_item_turn_type_for_kind,
    mark_projected_work_item_task,
    mark_work_item_projection,
    projection_id_for_task,
    projection_id_for_work_item,
    rework_projection_id_for_gate,
    result_delivery_identity_payload_for_task,
    turn_type_for_task,
    turn_type_for_work_item,
    work_item_identity_payload,
    work_item_identity_payload_for_task,
    work_item_identity_payload_from_metadata,
    work_item_projection_id_from_metadata,
)
from opc.layer2_organization.work_item_links import (  # 工作項目連結
    linked_work_item_id_for_task,
    set_linked_work_item_id,
)
from opc.layer2_organization.recruiter import (  # 招聘系統
    apply_recruitment_role_agent_overrides,
    CompanyRecruiter,
    build_fallback_role_ids,
    build_recruitment_feedback,
    build_recruitment_plan_from_payload,
    build_staffing_experience_modes,
    build_staffing_overrides,
    extract_recruitment_role_agent_overrides,
    normalize_recruitment_agent_choice,
    recruitment_plan_requires_confirmation,
    resolve_effective_execution_agent,
    serialize_recruitment_plan,
)
from opc.layer2_organization.reorg_manager import ReorgManager  # 組織重組管理器
from opc.layer2_organization.talent_market import TalentMarket  # 人才市場
# --- Layer 3：代理層 ---
from opc.layer3_agent.native_agent import NativeAgent  # 原生代理
from opc.layer3_agent.prompt_harness.builder import _final_decider_role_id, _memory_skill_user_facing  # Prompt 組裝
from opc.layer3_agent.adapters.registry import AdapterRegistry  # 適配器註冊表
from opc.layer3_agent.external_broker import ExternalAgentBroker  # 外部代理經紀人
from opc.layer3_agent.external_session_identity import (  # 外部工作階段身份
    external_session_matches_provider_token,
    external_session_status_allows_resume,
    is_provider_session_token,
    provider_token_from_external_session,
    select_best_external_resume_session,
)
# --- Layer 4：工具層 ---
from opc.layer4_tools.registry import ToolRegistry, ToolDefinition  # 工具註冊表
from opc.layer4_tools.shell import create_shell_tool, create_shell_tools  # Shell 工具
from opc.layer4_tools.file_ops import create_file_tools  # 檔案操作工具
from opc.layer4_tools.user_input import create_user_input_tool  # 使用者輸入工具
from opc.layer4_tools.web_search import create_web_tools  # 網路搜尋工具
from opc.layer4_tools.browser import browser_snapshot, create_browser_tools  # 瀏覽器工具
from opc.layer4_tools.git_ops import create_git_tools  # Git 操作工具
from opc.layer4_tools.python_exec import create_python_tool  # Python 執行工具
from opc.layer4_tools.collaboration import (  # 協作工具
    build_external_cli_tool_contract_lines,
    create_collaboration_tools,
)
from opc.layer4_tools.todo import create_todo_tools  # 待辦事項工具
from opc.layer4_tools.agent_runtime import create_agent_runtime_tools  # 代理運行時工具
from opc.layer2_organization.heartbeat import HeartbeatScheduler  # 心跳排程器
from opc.mcp_client import MCPManager  # MCP 客戶端管理器
# --- Layer 5：記憶層 ---
from opc.layer5_memory.memory_manager import MemoryManager  # 記憶管理器


# ---------------------------------------------------------------------------
# 模組級常量 — 任務狀態集合和運行時控制鍵
# ---------------------------------------------------------------------------

_REVIEW_WAITING_STATUSES = {  # 等待審查的任務狀態集合
    TaskStatus.AWAITING_MANAGER_REVIEW,
    TaskStatus.AWAITING_HUMAN,
    TaskStatus.AWAITING_REVIEW,
}
_WAITING_TASK_STATUSES = {  # 所有等待中的任務狀態集合（含等待同儕）
    *_REVIEW_WAITING_STATUSES,
    TaskStatus.AWAITING_PEER,
}
_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES = {  # 公司運行時暫停檢查點類型
    "company_runtime_suspended",
    "company_runtime_interrupted",
}
_COMPANY_RUNTIME_CONTROL_METADATA_KEYS = (  # 公司運行時控制 metadata 鍵（用於停止/暫停）
    "dispatch_hold",
    "company_runtime_stop_state",
    "company_runtime_stop_intent_id",
    "company_runtime_stop_marked_at",
    "company_runtime_suspend_checkpoint_type",
    "company_runtime_suspended_at",
)
# --- Layer 5：記憶層（續）---
from opc.layer5_memory.history_compactor import HistoryCompactor  # 歷史壓縮器
from opc.layer5_memory.preference import PreferenceManager  # 偏好管理器
from opc.layer5_memory.secretary_policy import SecretaryPolicyManager  # 秘書策略管理器
from opc.layer5_memory.capability_manager import CapabilityManager  # 能力管理器
from opc.layer5_memory.skill_library import SkillLibrary  # 技能庫
# --- Layer 6：觀測層 ---
from opc.layer6_observability.cost_tracker import CostTracker  # 成本追蹤器
from opc.layer6_observability.opc_logger import setup_logging  # 日誌設定

# ---------------------------------------------------------------------------
# LLM Prompt 模板 — 代理選擇和回饋歸因
# ---------------------------------------------------------------------------

# 代理選擇 Prompt：用於 LLM 決定任務應由哪個執行代理處理
AGENT_SELECTION_PROMPT = """\
You are the task execution-agent selector for an AI orchestration system.

Given a concrete task, its assigned role, execution metadata, and the currently available
external agents, choose the best execution agent for THIS task only.

Return strict JSON:
{
  "selected_agent": "native" | "claude_code" | "cursor" | "codex" | "opencode",
  "reasoning": "short explanation"
}

Rules:
- Respect hard constraints:
  - If execution_strategy is "native", return "native".
  - If execution_strategy is "external", choose one available external agent.
  - If no external agents are available, return "native".
- For "auto" and "mixed", decide based on the task's real needs:
  - role responsibility and work-item turn type
  - subtask objective and expected artifacts
  - whether the work is tool-heavy, coding-heavy, file/system-heavy, or better suited for direct native reasoning
  - any preferred external agent from role or work-item metadata
- Use "native" for lighter planning/review/approval/conversational tasks when external delegation is not clearly beneficial.
- Use an external agent for substantial implementation, CLI-heavy, repo/file-editing, automation, or multi-step execution tasks when that is a better fit.
- Choose only from the provided available agents. If a preferred external agent is unavailable, choose the best available alternative or "native".
- If retry_feedback is present, fix the exact issue it describes and return a corrected answer.
- Return JSON only. No markdown fences, no extra text.
"""

# 公司回饋歸因 Prompt：用於 LLM 評估使用者對公司工作項目的回饋
COMPANY_FEEDBACK_ATTRIBUTION_PROMPT = """\
You evaluate user feedback for a completed company work-item runtime.

Return strict JSON:
{
  "overall_outcome": "success" | "partial_success" | "failure",
  "summary": "short summary grounded in the user's feedback",
  "strengths": ["..."],
  "weaknesses": ["..."],
  "employees": [
    {
      "employee_id": "employee id",
      "outcome": "success" | "partial_success" | "failure",
      "reason": "why this employee received this outcome",
      "strengths": ["..."],
      "weaknesses": ["..."]
    }
  ]
}

Rules:
- User feedback is the source of truth. Do not upgrade a negative user judgment into success.
- Use partial_success when the user gives mixed or qualified feedback.
- Attribute strengths and weaknesses only to employees that actually appear in the runtime data.
- Keep the result concise and actionable.
- Return JSON only.
"""


class ExternalRecruiterLLMAdapter:
    """外部招聘 LLM 適配器 — 透過外部代理執行招聘規劃。

    職責說明：
        將外部任務代理（如 claude_code、cursor 等）包裝為
        CompanyRecruiter 所需的 simple_chat 介面。
        使招聘系統可以透過外部代理進行招聘規劃。

    關聯關係：
        - 被 opc/layer2_organization/recruiter.py 的 CompanyRecruiter 使用
        - 依賴 OPCEngine 的 adapter_registry 和 external_broker
    """

    def __init__(self, engine: "OPCEngine", agent_name: str) -> None:
        """初始化外部招聘適配器。

        參數：
            engine (OPCEngine)：引擎實例（提供基礎設施）。
            agent_name (str)：外部代理名稱。
        """
        self.engine = engine  # 引擎實例引用
        self.agent_name = agent_name  # 外部代理名稱

    async def simple_chat(self, prompt: str, system: str | None = None, task_type: str | None = None) -> str:
        """透過外部代理執行簡單的聊天式招聘規劃。

        參數：
            prompt (str)：招聘規劃的 payload。
            system (str | None)：系統 Prompt。
            task_type (str | None)：任務類型。

        返回值：
            str — 外部代理的回覆內容。

        異常：
            RuntimeError — 基礎設施未初始化或代理不可用。
        """
        engine = self.engine
        if not engine.adapter_registry or not engine.external_broker:
            raise RuntimeError("External recruitment agent infrastructure is not initialized.")
        adapter = engine.adapter_registry.get(self.agent_name)
        if adapter is None:
            raise RuntimeError(f"External recruitment agent `{self.agent_name}` is not available.")

        description = "\n\n".join(
            part
            for part in (
                "You are acting as OPC's recruitment planner.",
                "Return JSON only.",
                "Do not edit files.",
                "Do not run tools unless necessary.",
                "Follow the system contract exactly.",
                f"SYSTEM:\n{system or ''}",
                f"PAYLOAD:\n{prompt}",
            )
            if str(part).strip()
        )
        task = Task(
            title=f"Recruitment planning via {self.agent_name}",
            description=description,
            assigned_to="recruiter",
            status=TaskStatus.PENDING,
            assigned_external_agent=self.agent_name,
            project_id=engine.project_id or "default",
            metadata={
                "mode": "recruitment",
                "task_type": task_type or "quick_tasks",
                "recruitment_planning": True,
                "selected_execution_agent": self.agent_name,
                "selected_execution_agent_source": "recruitment_user_override",
                "execution_agent_locked": True,
                "preferred_external_agent": self.agent_name,
            },
        )
        run_adapter, _ = await engine._configure_external_adapter_for_task(task, adapter)
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

        workspace = engine._resolve_external_workspace(task)
        prepared_task = copy.deepcopy(task)
        result = await engine.external_broker.run(
            adapter=run_adapter,
            task=task,
            workspace_path=workspace,
            prepared_task=prepared_task,
        )
        if result.status != TaskStatus.DONE:
            detail = str(result.content or "").strip()
            raise RuntimeError(detail or f"External recruitment agent `{self.agent_name}` did not complete.")
        return str(result.content or "").strip()



# --- Mixin imports ---
from opc.engine._staffing import StaffingMixin
from opc.engine._task_mode import TaskModeMixin
from opc.engine._company_mode import CompanyModeMixin
from opc.engine._external_agent import ExternalAgentMixin
from opc.engine._checkpoints import CheckpointMixin
from opc.engine._company_runtime import CompanyRuntimeMixin
from opc.engine._reorg import ReorgMixin

class OPCEngine(
    StaffingMixin,
    TaskModeMixin,
    CompanyModeMixin,
    ExternalAgentMixin,
    CheckpointMixin,
    CompanyRuntimeMixin,
    ReorgMixin,
):
    """OPC 中央協調器 — 初始化並協調所有層級。

    職責說明：
        作為整個 OPC 系統的核心引擎，負責：
        - 初始化所有子系統（儲存、LLM、工具、代理、頻道、記憶等）
        - 接收使用者訊息並路由到任務模式或公司模式
        - 管理任務生命週期（建立、執行、完成、升級）
        - 協調公司模式的委派執行（DelegationRun → Cell → WorkItem）
        - 處理審批、升級、重組等組織行為
        - 管理外部代理和原生子代理的調度

    關聯關係：
        - 被 opc/cli/app.py 建立和驅動（CLI 入口）
        - 被 opc/plugins/office_ui/ 透過 API 驅動（Web UI）
        - 依賴所有 layer0~layer6 模組
        - 持有 OPCConfig、OPCStore、LLMProvider 等核心基礎設施

    使用範例：
        engine = OPCEngine(config=OPCConfig.load())
        await engine.initialize()
        result = await engine.process_message(user_message)
    """

    def __init__(
        self,
        config: OPCConfig | None = None,
        opc_home: Path | None = None,
        project_id: str | None = None,
        store: OPCStore | None = None,
        owns_store: bool = True,
        run_startup_reconcile: bool = True,
        active_task_run_registry: ActiveTaskRunRegistry | None = None,
        owns_active_task_run_registry: bool | None = None,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_runtime_event: Callable[[OPCEvent], Coroutine[Any, Any, None]] | None = None,
        on_escalation: Callable[[str, list[dict]], Coroutine[Any, Any, str | None]] | None = None,
    ) -> None:
        """初始化 OPCEngine。

        參數：
            config (OPCConfig | None)：系統配置。None 時使用預設配置。
            opc_home (Path | None)：OPC 主目錄。None 時自動偵測。
            project_id (str | None)：專案 ID（多專案隔離）。
            store (OPCStore | None)：外部提供的儲存實例（共享時用）。
            owns_store (bool)：是否擁有 store 的生命週期（關閉時負責關閉）。
            run_startup_reconcile (bool)：啟動時是否執行狀態調和。
            active_task_run_registry (ActiveTaskRunRegistry | None)：外部提供的活動任務註冊表。
            owns_active_task_run_registry (bool | None)：是否擁有註冊表的生命週期。
            on_progress：進度回呼（非同步）。
            on_runtime_event：運行時事件回呼（非同步）。
            on_escalation：升級回呼（非同步，返回人類決策）。
        """
        self.config = config or OPCConfig()  # 系統配置
        self.opc_home = opc_home or get_opc_home()  # OPC 主目錄
        self.project_id = project_id  # 專案 ID
        self.on_progress = on_progress  # 進度回呼
        self.on_runtime_event = on_runtime_event  # 運行時事件回呼
        self.on_escalation = on_escalation  # 升級回呼
        # 公司模式建立工作項目時呼叫：(parent_task_id, [child_task_id, ...])
        self.on_company_runtime_children: Callable[[str, list[str]], None] | None = None
        self.on_company_kanban_callback_factory: Callable[[Any], Callable[[], Coroutine[Any, Any, None]]] | None = None

        # 核心基礎設施
        self.event_bus = EventBus()  # 事件匯流排
        self.store: OPCStore | None = store  # 持久化儲存
        self._owns_store = bool(owns_store)  # 是否擁有 store 生命週期
        self._run_startup_reconcile = bool(run_startup_reconcile)  # 啟動時是否調和
        if active_task_run_registry is None:
            self._active_task_run_registry = ActiveTaskRunRegistry()
            default_registry_owner = True
        else:
            self._active_task_run_registry = active_task_run_registry
            default_registry_owner = False
        self._owns_active_task_run_registry = (
            default_registry_owner
            if owns_active_task_run_registry is None
            else bool(owns_active_task_run_registry)
        )
        self.llm: LLMProvider | None = None  # LLM 提供者（initialize 時建立）
        self.attachment_store: AttachmentStore | None = None  # 附件儲存

        # 各層子系統（initialize 時建立）
        self.message_bus = MessageBus()  # Layer 0：訊息匯流排
        self.tool_registry = ToolRegistry()  # Layer 4：工具註冊表
        self.memory: MemoryManager | None = None  # Layer 5：記憶管理器
        self.history_compactor: HistoryCompactor | None = None  # Layer 5：歷史壓縮器
        self.preferences: PreferenceManager | None = None  # Layer 5：偏好管理器
        self.secretary_policies: SecretaryPolicyManager | None = None  # Layer 5：秘書策略
        self.skills: SkillLibrary | None = None  # Layer 5：技能庫
        self.capability_manager: CapabilityManager | None = None  # Layer 5：能力管理器
        self.adapter_registry: AdapterRegistry | None = None  # Layer 3：適配器註冊表
        self.org_engine: OrgEngine | None = None  # Layer 2：組織引擎
        self.task_scheduler: TaskGraphScheduler | None = None  # Layer 2：任務圖排程器
        self.escalation: EscalationEngine | None = None  # Layer 2：升級引擎
        self.communication: CommunicationManager | None = None  # Layer 2：通訊管理器
        self.company_runtime_spec_builder: CompanyRuntimeSpecBuilder | None = None  # Layer 2：公司運行時規格建立器
        self.company_recruiter: CompanyRecruiter | None = None  # Layer 2：公司招聘器
        self.company_executor: CompanyWorkItemExecutor | None = None  # Layer 2：公司工作項目執行器
        self.reorg_manager: ReorgManager | None = None  # Layer 2：組織重組管理器
        self.cost_tracker: CostTracker | None = None  # Layer 6：成本追蹤器
        self.approval_engine: ApprovalEngine | None = None  # Layer 2：審批引擎
        self.external_broker: ExternalAgentBroker | None = None  # Layer 3：外部代理經紀人
        self.secretary: SecretaryService | None = None  # Layer 2：秘書服務
        self.mcp_manager: MCPManager | None = None  # MCP 客戶端管理器
        self.channel_manager: ChannelManager | None = None  # 頻道管理器
        self.talent_market: TalentMarket | None = None  # Layer 2：人才市場
        self.heartbeat_scheduler: HeartbeatScheduler | None = None  # Layer 2：心跳排程器
        self.comms_reactivation_sweeper: CommsReactivationSweeper | None = None  # Layer 2：通訊重新激活掃除器

        # Layer 1：感知層
        self.context_loader: ContextLoader | None = None  # 上下文載入器
        self.context_assembler: ContextAssembler | None = None  # 上下文組裝器
        self.task_router: TaskRouter | None = None  # 任務路由器

        self._initialized = False  # 是否已完成初始化
        self._shutting_down = False  # 是否正在關閉
        self._runtime_config_signature: tuple[tuple[str, float], ...] | None = None  # 運行時配置簽名（熱重載偵測）
        self._project_delegate_lock: asyncio.Lock | None = None  # 專案委派鎖
        self._project_engine_delegates: dict[str, OPCEngine] = {}  # 專案引擎委派映射

    def bind_store(self, store: OPCStore, *, owns_store: bool | None = None) -> None:
        """重新綁定活動 store 到所有快取它的元件。"""
        self.store = store
        if owns_store is not None:
            self._owns_store = bool(owns_store)

        if self.memory:
            self.memory.store = store
        if self.history_compactor:
            self.history_compactor.store = store
        if self.org_engine:
            self.org_engine.store = store
        if self.task_scheduler:
            self.task_scheduler.store = store
        if self.communication:
            self.communication.store = store
        if self.context_assembler:
            self.context_assembler.store = store
        if self.approval_engine:
            self.approval_engine.store = store
        if self.external_broker:
            self.external_broker.store = store
        if self.secretary:
            self.secretary.store = store
        if self.reorg_manager:
            self.reorg_manager.store = store
        if self.cost_tracker:
            self.cost_tracker.store = store
        if self.context_loader:
            self.context_loader.store = store
        if self.heartbeat_scheduler:
            self.heartbeat_scheduler.store = store
        if self.comms_reactivation_sweeper:
            self.comms_reactivation_sweeper.store = store
        if self.company_executor:
            self.company_executor.store = store
            self.company_executor.save_task = store.save_task
            self.company_executor.save_runtime_session = store.save_runtime_session
            if getattr(self.company_executor, "runtime", None):
                self.company_executor.runtime.store = store
                self.company_executor.runtime.save_runtime_session = store.save_runtime_session

    def _runtime_config_signature_for(self, config_dir: Path) -> tuple[tuple[str, float], ...]:
        tracked = (
            "system_config.yaml",
            "agent_config.yaml",
            "company_corporate_config.yaml",
        )
        signature: list[tuple[str, float]] = []
        for name in tracked:
            path = config_dir / name
            mtime = path.stat().st_mtime if path.exists() else -1.0
            signature.append((name, mtime))
        corporate_path = company_org_path(config_dir, DEFAULT_ORGANIZATION_ID)
        corporate_mtime = corporate_path.stat().st_mtime if corporate_path.exists() else -1.0
        signature.append((f"company_orgs/{corporate_path.name}", corporate_mtime))
        return tuple(signature)

    def _task_mode_tool_names(self) -> list[str]:
        names = [tool.name for tool in self.tool_registry.list_tools()]
        filtered: list[str] = []
        seen: set[str] = set()
        for name in names:
            normalized = str(name or "").strip()
            if not normalized or normalized in TASK_MODE_COMPANY_ONLY_TOOLS or normalized in seen:
                continue
            filtered.append(normalized)
            seen.add(normalized)
        return filtered

    async def _refresh_runtime_config_from_disk(self) -> None:
        config_dir = self.opc_home / "config"
        if not config_dir.is_dir():
            return

        signature = self._runtime_config_signature_for(config_dir)
        if signature == self._runtime_config_signature:
            return

        loaded = OPCConfig.load(config_dir)
        self.config.system = loaded.system
        self.config.agents = loaded.agents
        self.config.autonomy = loaded.autonomy
        self._runtime_config_signature = signature

        active_org_id = None
        if str(getattr(self.config.org, "company_profile", "") or "").strip() == "custom":
            try:
                from opc.core.org_config import read_org_index, apply_org_config_payload_to_config, load_org_config_payload
                active_org_id = read_org_index(config_dir)
            except Exception:
                pass
        if active_org_id:
            try:
                payload, path = load_org_config_payload(config_dir, active_org_id)
                refreshed = apply_org_config_payload_to_config(loaded, payload, source_path=path)
                self.config.org = refreshed.org
            except Exception:
                self.config.org = loaded.org
        else:
            self.config.org = loaded.org

        if self.approval_engine:
            self.approval_engine.config = self.config.autonomy
        if self.company_executor:
            self.company_executor.work_item_timeout = self.config.system.task_mode.sub_agent_timeout_sec
        if self.adapter_registry:
            self.adapter_registry.config = self.config.agents
            await self.adapter_registry.initialize()
        if self.org_engine:
            self.org_engine.config = self.config
            self.org_engine.reload_from_config()
            self.org_engine.configure_task_mode_tools(self._task_mode_tool_names())

        logger.info(f"Reloaded runtime config from {config_dir}")

    async def initialize(self) -> None:
        """初始化所有層級和子系統。

        生命週期：
            1. 建立 OPC 主目錄和日誌
            2. 初始化資料庫（OPCStore）
            3. 建立 LLM 提供者
            4. 初始化 Layer 5（記憶、技能、偏好）
            5. 初始化 Layer 2（組織引擎、審批、升級、通訊）
            6. 初始化 Layer 3（適配器、外部代理經紀人）
            7. 註冊工具（Layer 4）
            8. 初始化 Layer 1（上下文組裝、任務路由）
            9. 啟動訊息匯流排和頻道管理器

        被誰引用：
            - process_message()：首次處理訊息時自動呼叫
            - opc/cli/app.py：CLI 啟動時
        """
        if self._initialized:
            return

        self.opc_home.mkdir(parents=True, exist_ok=True)
        setup_logging(self.opc_home / "logs", self.config.system.log_level)
        logger.info("Initializing OPC Engine...")

        # Database
        if self.store is None:
            db_path = self.opc_home / "global.db"
            if self.project_id:
                proj_dir = self.opc_home / "projects" / self.project_id
                proj_dir.mkdir(parents=True, exist_ok=True)
                get_project_workplace(self.project_id).mkdir(parents=True, exist_ok=True)
                db_path = proj_dir / "tasks.db"
            self.store = OPCStore(db_path)
            self._owns_store = True
            # Fix 5 PR3: surface the serial-queue feature flag on the store
            # so phase hooks (which only receive ``store``) can gate their
            # queue-aware branches without reaching back to the engine.
            self.store.role_serial_queue_enabled = bool(
                getattr(self.config.org, "role_serial_queue_enabled", True)
            )
            await self.store.initialize()
        else:
            self.store.role_serial_queue_enabled = bool(
                getattr(self.config.org, "role_serial_queue_enabled", True)
            )
            if self._owns_store:
                await self.store.initialize()
        self._ensure_attachment_store()

        # LLM
        self.llm = LLMProvider(self.config.llm, opc_home=self.opc_home)

        # Layer 4: Tools
        self._register_tools()

        # MCP server connections
        self.mcp_manager = MCPManager()
        await self._register_mcp_tools()

        # Layer 5: Memory
        self.memory = MemoryManager(self.opc_home, self.project_id, store=self.store)
        self.memory.markdown_store.ensure_memory_file(None, "# Global Memory")
        if self.project_id:
            self.memory.markdown_store.ensure_memory_file(self.project_id, f"# Project Memory ({self.project_id})")
        self.history_compactor = HistoryCompactor(
            llm=self.llm,
            store=self.store,
            memory_manager=self.memory,
            compression_threshold=self.config.system.context_compression_threshold,
        )
        self.memory.set_history_compactor(self.history_compactor)
        self.preferences = PreferenceManager(self.opc_home)
        self.secretary_policies = SecretaryPolicyManager(self.opc_home)
        self.skills = SkillLibrary(self.opc_home)
        self.skills.load_all(self.project_id)

        # Layer 3: External Agents
        self.adapter_registry = AdapterRegistry(self.config.agents)
        await self.adapter_registry.initialize()
        self.capability_manager = CapabilityManager(
            config=self.config.capabilities,
            skill_library=self.skills,
            tool_registry=self.tool_registry,
            adapter_registry=self.adapter_registry,
        )

        # Layer 2: Organization
        self.org_engine = OrgEngine(self.config, self.opc_home, store=self.store)
        self.talent_market = TalentMarket(self.opc_home, self.config)
        self.task_scheduler = TaskGraphScheduler(self.store, self.event_bus)
        self.escalation = EscalationEngine(
            self.event_bus,
            timeout_seconds=self.config.system.escalation_timeout_seconds,
            user_reply_callback=self.on_escalation,
        )
        self.communication = CommunicationManager(self.store, self.event_bus, self.llm, self.org_engine)
        await self.communication.rehydrate_queues()
        self.channel_manager = ChannelManager(self.config, self.message_bus)
        self.context_assembler = ContextAssembler(
            memory=self.memory,
            store=self.store,
            communication=self.communication,
        )
        self.approval_engine = ApprovalEngine(
            llm=self.llm,
            store=self.store,
            preferences=self.preferences,
            memory=self.memory,
            escalation=self.escalation,
            config=self.config.autonomy,
            secretary_policies=self.secretary_policies,
        )
        self.external_broker = ExternalAgentBroker(
            self.store,
            self.approval_engine,
            task_preparer=self._build_external_agent_task,
            communication=self.communication,
        )
        self.secretary = SecretaryService(
            llm=self.llm,
            store=self.store,
            memory=self.memory,
            preferences=self.preferences,
            skills=self.skills,
            policies=self.secretary_policies,
        )
        self.company_runtime_spec_builder = CompanyRuntimeSpecBuilder(self.org_engine, self.llm)
        self.company_recruiter = CompanyRecruiter(self.llm, self.org_engine, self.talent_market)
        self.company_executor = CompanyWorkItemExecutor(
            org_engine=self.org_engine,
            communication=self.communication,
            approval_engine=self.approval_engine,
            memory=self.memory,
            llm=self.llm,
            store=self.store,
            execute_task=self._execute_task,
            seat_executor=EngineSeatExecutor(self),
            save_task=self.store.save_task,
            save_runtime_session=self.store.save_runtime_session,
            progress_callback=self.on_progress,
            checkpoint_callback=self._save_execution_checkpoint,
            agent_selector=self._assign_task_execution_agent,
            emit_runtime_event=self._emit_company_runtime_event,
            work_item_timeout=self.config.system.task_mode.sub_agent_timeout_sec,
            role_prompt_runner=self._run_role_prompt_via_task_execution_agent,
            active_task_run_registry=self._active_task_run_registry,
        )
        self.communication.set_meeting_turn_runner(self._run_meeting_turn)
        self.reorg_manager = ReorgManager(
            store=self.store,
            org_engine=self.org_engine,
            approval_engine=self.approval_engine,
            communication=self.communication,
            progress_callback=self.on_progress,
        )
        if self.communication is not None:
            self.communication.task_adjustment_suggester = self.reorg_manager.suggest_task_adjustment
        self.tool_registry.set_approval_callback(self._tool_approval_callback)
        self._register_collaboration_tools()

        # Layer 6: Cost tracking
        self.cost_tracker = CostTracker(self.store, self.event_bus)

        # Layer 1: Perception
        self.context_loader = ContextLoader(
            self.memory,
            self.preferences,
            self.secretary_policies,
            self.skills,
            self.capability_manager,
            self.adapter_registry,
            self.org_engine,
            self.store,
        )
        self.task_router = TaskRouter(self.llm)

        self.org_engine.configure_task_mode_tools(self._task_mode_tool_names())

        # Heartbeat scheduler for company-mode agent autonomy
        heartbeat_cfg = self.config.system.heartbeat if hasattr(self.config.system, "heartbeat") else None
        self.heartbeat_scheduler = HeartbeatScheduler(
            store=self.store,
            org_engine=self.org_engine,
            execute_task_fn=self._execute_task,
            interval_sec=getattr(heartbeat_cfg, "default_interval_sec", 300) if heartbeat_cfg else 300,
            max_concurrent_runs=getattr(heartbeat_cfg, "max_concurrent_runs", 1) if heartbeat_cfg else 1,
            communication=self.communication,
        )

        # Comms reactivation sweeper — periodically re-opens DONE tasks whose
        # role received actionable mail after they finished. This closes the
        # gap between the end-of-turn ``_reactivate_for_unread_mail`` hook
        # (which only fires at task boundaries) and the arrival of a blocking
        # DM from a peer/manager. It replaces the old LLM "impersonation
        # reply" fallback so the recipient role's own agent answers instead.
        self.comms_reactivation_sweeper = CommsReactivationSweeper(
            store=self.store,
            project_id_getter=lambda: self.project_id or "default",
            reactivate_fn=self.company_executor._reactivate_for_unread_mail,
            interval_sec=10.0,
        )

        # Wire message bus
        self.message_bus.set_handler(self._handle_message)
        self.event_bus.subscribe_all(self._persist_event)
        if self.on_runtime_event is not None:
            self.event_bus.subscribe("runtime_event", self._forward_runtime_event)

        reconciled = 0
        if self._run_startup_reconcile:
            try:
                reconciled = await self._reconcile_interrupted_project_tasks()
            except InvalidPhaseTransition:
                logger.opt(exception=True).error(
                    "Startup reconcile hit an invalid work-item phase transition for project {}; aborting initialization",
                    self.project_id or "default",
                )
                raise
            except Exception:
                logger.opt(exception=True).error(
                    "Startup reconcile failed for project {}; aborting initialization",
                    self.project_id or "default",
                )
                raise
        if self.comms_reactivation_sweeper is not None:
            await self.comms_reactivation_sweeper.start()
        self._initialized = True
        logger.info("OPC Engine initialized successfully")
        if reconciled:
            logger.warning(
                "Reconciled {} interrupted task(s) for project {} during startup",
                reconciled,
                self.project_id or "default",
            )
        available_agents = self._available_external_agents()
        if available_agents:
            logger.info(f"External agents available: {', '.join(available_agents)}")
        else:
            logger.info("No external agents detected — using native agent only")

    def _register_tools(self) -> None:
        """註冊所有內建工具到 ToolRegistry（Shell、檔案、網路、瀏覽器、Git、Python、Todo、代理運行時）。"""
        self.tool_registry.register(create_user_input_tool())
        for tool in create_shell_tools():
            self.tool_registry.register(tool)
        for tool in create_file_tools():
            self.tool_registry.register(tool)
        for tool in create_web_tools():
            self.tool_registry.register(tool)
        for tool in create_browser_tools():
            self.tool_registry.register(tool)
        for tool in create_git_tools():
            self.tool_registry.register(tool)
        self.tool_registry.register(create_python_tool())
        for tool in create_todo_tools():
            self.tool_registry.register(tool)
        for tool in create_agent_runtime_tools():
            self.tool_registry.register(tool)
        logger.debug(f"Registered {len(self.tool_registry.list_tools())} tools")

    async def _register_mcp_tools(self) -> None:
        """連線並註冊所有已啟用的 MCP 伺服器工具（本地和遠端）。"""
        assert self.mcp_manager
        for server_cfg in self.config.system.mcp_servers:
            if not server_cfg.enabled:
                continue
            try:
                server_type = getattr(server_cfg, "type", "local") or "local"
                if server_type == "remote":
                    if not server_cfg.url:
                        logger.warning(f"MCP '{server_cfg.name}' is remote but has no url, skipping")
                        continue
                    conn = await self.mcp_manager.connect_remote(
                        name=server_cfg.name,
                        url=server_cfg.url,
                        headers=server_cfg.headers or None,
                        timeout=server_cfg.startup_timeout,
                    )
                else:
                    if not server_cfg.command:
                        logger.warning(f"MCP '{server_cfg.name}' is local but has no command, skipping")
                        continue
                    conn = await self.mcp_manager.connect_local(
                        name=server_cfg.name,
                        command=server_cfg.command,
                        env=server_cfg.env or None,
                        timeout=server_cfg.startup_timeout,
                    )
                tool_filter = set(server_cfg.tools_filter) if server_cfg.tools_filter else None
                tools = await self.mcp_manager.register_tools(conn, tool_filter)
                for tool in tools:
                    self.tool_registry.register(tool)
                logger.info(f"MCP '{server_cfg.name}' ({server_type}): registered {len(tools)} tools")
            except Exception as exc:
                logger.warning(f"MCP '{server_cfg.name}' unavailable, skipping: {exc}")

    def _register_collaboration_tools(self) -> None:
        """註冊公司模式協作工具（訊息、會議、交接等）。"""
        if not self.communication:
            return
        for tool in create_collaboration_tools(
            self.communication,
            reorg_manager=self.reorg_manager,
            capability_manager=self.capability_manager,
        ):
            self.tool_registry.register(tool)

    async def _persist_event(self, event: OPCEvent) -> None:
        if self.store:
            await self.store.save_event(event)

    async def _forward_runtime_event(self, event: OPCEvent) -> None:
        if self.on_runtime_event is None:
            return
        await self.on_runtime_event(event)

    async def _emit_company_runtime_event(self, event_type: str, payload: dict[str, Any]) -> None:
        await self.event_bus.publish(
            OPCEvent(
                event_type="runtime_event",
                payload={"type": event_type, "timestamp_ms": int(time.time() * 1000), **dict(payload or {})},
            )
        )

    async def _ensure_primary_session(self, session_id: str, initial_text: str = "") -> None:
        if not self.memory:
            return
        await self.memory.ensure_session(
            session_id=session_id,
            project_id=self.project_id or "default",
            title=initial_text[:120].strip(),
            mode="primary",
        )

    async def _record_primary_exchange(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        *,
        mode: str | None = None,
        origin_task_id: str | None = None,
        preferred_agent: str | None = None,
    ) -> None:
        if not self.memory:
            return
        _ = (user_text, origin_task_id)
        raw_mode = str(mode or "").strip().lower()
        requested_mode = self._normalize_requested_mode(mode)
        is_task_mode = requested_mode == "task"
        is_company_like_mode = requested_mode == "company" or raw_mode in {"company", "org", "custom"}
        if is_task_mode and await self._task_mode_external_top_level_reply_exists(
            session_id,
            assistant_text,
            task_id=origin_task_id,
        ):
            return
        if is_task_mode and await self._task_mode_reply_uses_native_runtime_transcript(
            session_id,
            assistant_text,
            origin_task_id=origin_task_id,
            preferred_agent=preferred_agent,
        ):
            # Native task-mode final replies are persisted by RuntimeV2 as
            # runtime_v2_assistant. Do not synthesize a top-level reply here.
            return
        if await self._company_reply_is_internal_runtime_result(
            session_id,
            assistant_text,
            allow_marker_fallback=is_company_like_mode,
        ):
            return
        await self.memory.record_assistant_turn(
            session_id=session_id,
            content=assistant_text,
            project_id=self.project_id or "default",
            metadata={"kind": "top_level_reply"},
        )

    async def _tool_approval_callback(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        task: Task | None,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> tuple[bool, Any]:
        assert self.approval_engine
        violation = ownership_guard_violation(
            task=task,
            tool_name=tool.name,
            arguments=arguments,
            org_engine=self.org_engine,
        )
        if violation:
            return False, ApprovalDecision(
                action=ApprovalAction.REJECT,
                risk_level=RiskLevel.HIGH,
                rationale=violation,
                confidence=0.99,
                policy_source="ownership_contract",
                metadata={"tool_name": tool.name},
            )
        bridge = getattr(task, "_runtime_permission_bridge", None) if task is not None else None
        if callable(bridge):
            return await bridge(
                tool=tool,
                arguments=arguments,
                approval_engine=self.approval_engine,
                on_progress=on_progress,
            )
        metadata = {
            "category": tool.category,
            "requires_confirmation": tool.requires_confirmation,
            "description": tool.description,
        }
        return await self.approval_engine.authorize_tool_call(
            task=task,
            tool_name=tool.name,
            arguments=arguments,
            metadata=metadata,
            on_progress=on_progress,
        )

    @staticmethod
    def _conversation_turn_id_for_message(message: UserMessage) -> str:
        metadata = dict(message.metadata or {})
        for key in ("conversation_turn_id", "canonical_turn_id", "turn_id"):
            value = str(metadata.get(key, "") or "").strip()
            if value:
                return value
        ui_message_id = str(metadata.get("ui_message_id", "") or "").strip()
        if ui_message_id:
            return f"ui-turn:{ui_message_id}"
        return f"engine-turn:{message.session_id}:{uuid.uuid4().hex}"

    async def _handle_message(self, message: UserMessage) -> SystemMessage:
        """Core message handler — branches on user-selected mode (project / company)."""
        assert self.context_loader
        attachment_refs = self._normalize_attachment_refs(
            message.attachments or message.metadata.get("attachment_refs", []),
        )
        conversation_turn_id = self._conversation_turn_id_for_message(message)
        response_metadata = {
            "chat_id": message.metadata.get("chat_id", ""),
            "thread_id": message.metadata.get("thread_id", ""),
            "reply_to": message.metadata.get("reply_to", ""),
            "attachments": attachment_refs,
            "attachment_refs": attachment_refs,
            "conversation_turn_id": conversation_turn_id,
        }
        requested_mode = self._normalize_requested_mode(message.metadata.get("mode", "task"))
        origin_task_id = message.metadata.get("origin_task_id")
        preferred_agent = message.metadata.get("preferred_agent")

        async def _record_early_reply(reply: str) -> None:
            await self._record_primary_exchange(
                message.session_id,
                message.content,
                reply,
                mode=requested_mode,
                origin_task_id=origin_task_id,
                preferred_agent=preferred_agent,
            )

        if message.project_context is not None and message.project_context != self.project_id:
            logger.warning(
                "Ignoring cross-project message_context={} on engine project_id={}; "
                "route through process_message(project_id=...) so each project keeps its own store/runtime.",
                message.project_context,
                self.project_id,
            )

        await self._ensure_primary_session(message.session_id, message.content)
        if self.memory:
            user_turn_metadata: dict[str, Any] = {"kind": "top_level_user_turn"}
            for key in ("ui_message_id", "ui_created_at"):
                value = message.metadata.get(key)
                if value not in (None, "", [], {}):
                    user_turn_metadata[key] = value
            user_turn_metadata["conversation_turn_id"] = conversation_turn_id
            user_turn_metadata["canonical_turn_id"] = conversation_turn_id
            user_turn_metadata["turn_id"] = conversation_turn_id
            await self.memory.record_user_turn(
                session_id=message.session_id,
                content=message.content,
                project_id=self.project_id or "default",
                metadata=user_turn_metadata,
            )

        resumed = await self._maybe_resume_checkpoint(
            message.content,
            message.session_id,
            reply_metadata=message.metadata,
        )
        if resumed is not None:
            await _record_early_reply(resumed)
            return SystemMessage(
                channel=message.channel,
                user_id=message.user_id,
                session_id=message.session_id,
                content=resumed,
                message_type="reply",
                metadata=response_metadata,
            )

        reorg_reply = await self._maybe_handle_reorg_message(message.content, message.session_id)
        if reorg_reply is not None:
            await _record_early_reply(reorg_reply)
            return SystemMessage(
                channel=message.channel,
                user_id=message.user_id,
                session_id=message.session_id,
                content=reorg_reply,
                message_type="reply",
                metadata=response_metadata,
            )

        existing_runtime_resume = await self._maybe_resume_existing_company_runtime(
            message.content,
            message.session_id,
            force_resume=bool(message.metadata.get("ui_force_resume", False)),
        )
        if existing_runtime_resume is not None:
            await _record_early_reply(existing_runtime_resume)
            return SystemMessage(
                channel=message.channel,
                user_id=message.user_id,
                session_id=message.session_id,
                content=existing_runtime_resume,
                message_type="reply",
                metadata=response_metadata,
            )

        # Load context
        include_project_knowledge = self._requests_explicit_project_knowledge(message.content)
        context = await self.context_loader.load(
            project_id=self.project_id,
            session_id=message.session_id,
            include_project_knowledge=include_project_knowledge,
        )
        context.default_channel = message.channel
        context.origin_chat_id = str(message.metadata.get("chat_id", "") or "")
        context.origin_thread_id = str(message.metadata.get("thread_id", "") or "")

        # Determine mode from user metadata (no LLM router needed)
        mode = requested_mode
        org_id = message.metadata.get("org_id")
        domains = list(message.metadata.get("domains", []))
        company_profile = message.metadata.get("company_profile")

        selection = ModeSelection(
            mode=ExecutionMode.COMPANY_MODE if mode == "company" else ExecutionMode.TASK_MODE,
            org_id=org_id,
            preferred_agent=str(preferred_agent).strip() if preferred_agent else None,
            domains=domains,
            company_profile=company_profile,
            metadata={
                "company_preflight": str(message.metadata.get("company_preflight", "") or "").strip(),
            },
        )
        logger.info(f"Mode: {selection.mode.value}, org_id={org_id}, agent={preferred_agent}")

        result = await self._execute_decision(
            selection, message.content, context,
            session_id=message.session_id,
            origin_task_id=origin_task_id,
            attachment_refs=attachment_refs,
            conversation_turn_id=conversation_turn_id,
        )
        await self._record_primary_exchange(
            message.session_id,
            message.content,
            result,
            mode=mode,
            origin_task_id=origin_task_id,
            preferred_agent=preferred_agent,
        )

        return SystemMessage(
            channel=message.channel,
            user_id=message.user_id,
            session_id=message.session_id,
            content=result,
            message_type="reply",
            metadata=response_metadata,
        )

    async def _execute_decision(
        self,
        decision: ModeSelection,
        original_message: str,
        context: Any | None = None,
        *,
        session_id: str | None = None,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        conversation_turn_id: str | None = None,
    ) -> str:
        """Build tasks and run them based on mode selection (project / company)."""
        assert self.task_scheduler and self.store

        project_id = self.project_id or "default"
        primary_session_id = session_id or str(uuid.uuid4())
        await self._ensure_primary_session(primary_session_id, original_message)
        workspace_contract = await self._resolve_workspace_contract(original_message, primary_session_id)
        target_output_dir = str(workspace_contract.get("output_root") or "").strip() or None
        await self._remember_session_execution_defaults(
            primary_session_id,
            decision,
            target_output_dir=target_output_dir,
            workspace_root=workspace_contract.get("workspace_root"),
            comms_workspace_root=workspace_contract.get("comms_workspace_root"),
            comms_root=workspace_contract.get("comms_root"),
        )

        if decision.mode == ExecutionMode.COMPANY_MODE:
            assert self.company_runtime_spec_builder
            setup_error = self.org_engine.validate_company_runtime_setup() if self.org_engine else None
            if setup_error:
                return f"Cannot execute company mode: {setup_error}"
            runtime_spec = self.company_runtime_spec_builder.build_spec(
                decision,
                original_message=original_message,
            )

            # Budget check for company mode
            org_id = getattr(decision, "org_id", None)
            if org_id and self.store:
                from opc.layer6_observability.cost_tracker import check_budget
                allowed, reason = await check_budget(self.store, org_id=org_id)
                if not allowed:
                    return f"Cannot execute: {reason}"

            force_manual_preflight = (
                str(dict(getattr(decision, "metadata", {}) or {}).get("company_preflight", "") or "")
                .strip()
                .lower()
                == "manual"
            )
            return await self._begin_company_staffing_loop(
                decision,
                original_message,
                runtime_spec,
                session_id=primary_session_id,
                origin_channel=context.default_channel if context else "cli",
                origin_chat_id=context.origin_chat_id if context else "",
                origin_thread_id=context.origin_thread_id if context else "",
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                force_manual_preflight=force_manual_preflight,
            )
        return await self._continue_task_mode_execution(
            decision,
            original_message,
            None,
            session_id=primary_session_id,
            origin_channel=context.default_channel if context else "cli",
            origin_chat_id=context.origin_chat_id if context else "",
            origin_thread_id=context.origin_thread_id if context else "",
            origin_task_id=origin_task_id,
            attachment_refs=attachment_refs,
            conversation_turn_id=conversation_turn_id,
        )

    async def _emit_external_agent_audit(
        self,
        task: Task,
        metadata: dict[str, Any],
        workspace: str,
        progress_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        agent = metadata.get("agent", task.assigned_external_agent or "external")
        model = metadata.get("model", "(cli default)")
        session_mode = metadata.get("session_mode", "auto")
        new_session = metadata.get("new_session", False)
        command = metadata.get("display_command") or metadata.get("command", "")
        if isinstance(command, str) and ("\n" in command or len(command) > 260):
            command = f"<command:{len(command)}-chars>"
        target_output_dir = metadata.get("target_output_dir", "")
        header = (
            f"[Delegating to {agent}] task={task.title} | model={model} | "
            f"session_mode={session_mode} | new_session={new_session} | "
            f"workspace={workspace}"
        )
        if target_output_dir:
            header += f" | target_output_dir={target_output_dir}"
        header += f" | cmd={command}"
        logger.debug(header)
        if progress_callback:
            await progress_callback(header)

    @staticmethod
    async def _invoke_progress_callback(
        callback: Callable[[str], Coroutine[Any, Any, None]] | None,
        text: str,
        **kw: Any,
    ) -> None:
        if not callback:
            return
        if not kw:
            await callback(text)
            return
        try:
            await callback(text, **kw)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            await callback(text)

    def _resolve_progress_identity(self, task: Task) -> tuple[str, str, str]:
        origin_task_id = str(task.metadata.get("origin_task_id", "") or "").strip()
        is_child_session = bool(
            task.parent_session_id
            and task.session_id
            and task.session_id != task.parent_session_id
            and not self._uses_shared_role_session(task)
        )
        is_distinct_work_item_projection_title = bool(origin_task_id and origin_task_id != task.id)
        progress_task_id = task.id if (is_child_session or is_distinct_work_item_projection_title) else (origin_task_id or task.id)
        agent_role_id = task.assigned_to or str(task.metadata.get("work_item_role_id", "")).strip()
        employee_assignment = dict(task.metadata.get("employee_assignment", {}) or {})
        agent_name = str(employee_assignment.get("name", "")).strip()
        if not agent_name and self.org_engine and agent_role_id:
            role = self.org_engine.get_role_for_work_item(agent_role_id, task.tags)
            agent_name = str(getattr(role, "name", "") or "").strip()
        return progress_task_id, agent_role_id, agent_name

    def _make_task_progress_callback(
        self,
        task: Task,
    ) -> Callable[[str], Coroutine[Any, Any, None]] | None:
        base_cb = self.on_progress
        if not base_cb:
            return None

        progress_task_id, agent_role_id, agent_name = self._resolve_progress_identity(task)

        async def _callback(text: str, **kw: Any) -> None:
            kw["task_id"] = progress_task_id
            if agent_role_id:
                kw.setdefault("agent_role_id", agent_role_id)
            if agent_name:
                kw.setdefault("agent_name", agent_name)
            await self._invoke_progress_callback(base_cb, text, **kw)

        return _callback

    async def _company_runtime_checkpoint_payload(
        self,
        *,
        checkpoint_type: str,
        reason: str,
        parent_session_id: str,
        origin_task_id: str | None,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        stop_intent_id: str | None = None,
    ) -> dict[str, Any]:
        project_id = self.project_id or "default"
        now = datetime.now().isoformat()
        run_id = ""
        company_profile = str(plan.profile or "").strip()
        task_snapshots: list[dict[str, Any]] = []
        active_work_items: list[dict[str, Any]] = []
        role_runtime_session_ids: list[str] = []
        seat_state_ids: list[str] = []
        adapter_session_state_by_role: dict[str, dict[str, Any]] = {}
        native_runtime_resume_by_task: dict[str, dict[str, Any]] = {}
        external_sessions_by_task: dict[str, dict[str, Any]] = {}

        get_work_item = getattr(self.store, "get_delegation_work_item", None) if self.store else None
        get_role_session = getattr(self.store, "get_delegation_role_session", None) if self.store else None

        for task in tasks:
            metadata = dict(task.metadata or {})
            run_id = run_id or str(metadata.get("delegation_run_id", "") or "").strip()
            company_profile = company_profile or str(metadata.get("company_profile", "") or "").strip()
            role_session_id = str(metadata.get("delegation_role_session_id", "") or "").strip()
            seat_state_id = str(metadata.get("delegation_seat_state_id", "") or "").strip()
            if seat_state_id:
                seat_state_ids.append(seat_state_id)
            raw_runtime_resume = task.context_snapshot.get("runtime_resume", {}) if isinstance(task.context_snapshot, dict) else {}
            runtime_resume = dict(raw_runtime_resume) if isinstance(raw_runtime_resume, dict) else {}
            runtime_v2 = dict(metadata.get("runtime_v2", {}) or {})
            if runtime_resume or runtime_v2:
                native_runtime_resume_by_task[task.id] = {**runtime_v2, **runtime_resume}
            external_snapshot = await self._external_resume_snapshot_for_task(task)
            if external_snapshot:
                external_sessions_by_task[task.id] = external_snapshot
            (
                selected_execution_agent,
                assigned_external_agent,
                agent_selection_source,
            ) = self._task_effective_execution_agent_identity(task)
            employee_assignment = dict(metadata.get("employee_assignment", {}) or {})
            execution_identity = {
                "role_id": str(
                    task.assigned_to
                    or metadata.get("work_item_role_id", "")
                    or ""
                ).strip(),
                "seat_id": str(
                    metadata.get("delegation_seat_id", "")
                    or metadata.get("seat_id", "")
                    or ""
                ).strip(),
                "role_runtime_session_id": role_session_id,
                "employee_id": str(employee_assignment.get("employee_id", "") or "").strip(),
                "employee_assignment": copy.deepcopy(employee_assignment),
                "selected_execution_agent": selected_execution_agent,
                "assigned_external_agent": assigned_external_agent,
                "preferred_external_agent": str(
                    metadata.get("preferred_external_agent", "") or ""
                ).strip(),
                "execution_agent_locked": bool(metadata.get("execution_agent_locked", False)),
                "selected_execution_agent_source": str(
                    metadata.get("selected_execution_agent_source", "") or ""
                ).strip(),
                "agent_selection_source": agent_selection_source,
            }

            work_item_id = linked_work_item_id_for_task(task)
            work_item_snapshot: dict[str, Any] = {}
            if work_item_id and callable(get_work_item):
                try:
                    work_item = await get_work_item(work_item_id)
                except Exception:
                    work_item = None
                if work_item is not None:
                    work_item_snapshot = {
                        "work_item_id": work_item_id,
                        "phase": (
                            getattr(work_item, "phase").value
                            if isinstance(getattr(work_item, "phase", None), Phase)
                            else str(getattr(work_item, "phase", "") or "")
                        ),
                        "role_id": str(getattr(work_item, "role_id", "") or ""),
                        "seat_id": str(getattr(work_item, "seat_id", "") or ""),
                        "role_runtime_session_id": str(getattr(work_item, "role_runtime_session_id", "") or ""),
                        "claimed_by_role_runtime_session_id": str(getattr(work_item, "claimed_by_role_runtime_session_id", "") or ""),
                        "claimed_by_seat_id": str(getattr(work_item, "claimed_by_seat_id", "") or ""),
                        "projection_id": str(getattr(work_item, "projection_id", "") or ""),
                        "kind": str(getattr(work_item, "kind", "") or ""),
                        "metadata": dict(getattr(work_item, "metadata", {}) or {}),
                    }
                    execution_identity["seat_id"] = (
                        work_item_snapshot["seat_id"]
                        or execution_identity["seat_id"]
                    )
                    execution_identity["role_id"] = (
                        work_item_snapshot["role_id"]
                        or execution_identity["role_id"]
                    )
                    execution_identity["role_runtime_session_id"] = (
                        work_item_snapshot["role_runtime_session_id"]
                        or execution_identity["role_runtime_session_id"]
                    )
                    work_item_assignment = dict(
                        work_item_snapshot["metadata"].get("employee_assignment", {})
                        or {}
                    )
                    if "employee_assignment" in work_item_snapshot["metadata"]:
                        execution_identity["employee_id"] = str(
                            work_item_assignment.get("employee_id", "") or ""
                        ).strip()
                        execution_identity["employee_assignment"] = copy.deepcopy(
                            work_item_assignment
                        )
                    active_work_items.append(work_item_snapshot)

            role_session_id = str(
                execution_identity["role_runtime_session_id"] or ""
            ).strip()
            if role_session_id:
                role_runtime_session_ids.append(role_session_id)
            if role_session_id and callable(get_role_session):
                try:
                    role_session = await get_role_session(role_session_id)
                except Exception:
                    role_session = None
                if role_session is not None:
                    adapter_session_state_by_role[role_session_id] = dict(
                        getattr(role_session, "adapter_session_state", {}) or {}
                    )

            task_snapshots.append({
                "task_id": task.id,
                "session_id": task.session_id,
                "parent_session_id": task.parent_session_id,
                "status": task.status.value if isinstance(task.status, TaskStatus) else str(task.status),
                "title": task.title,
                "assigned_to": task.assigned_to,
                "assigned_external_agent": assigned_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_identity": execution_identity,
                "work_item_id": work_item_id,
                "projection_id": projection_id_for_task(task),
                "turn_type": turn_type_for_task(task, fallback=""),
                "role_session_id": role_session_id,
                "seat_state_id": seat_state_id,
                "runtime_resume": native_runtime_resume_by_task.get(task.id, {}),
                "external_session": external_sessions_by_task.get(task.id, {}),
                "progress_tail": self._checkpoint_progress_tail(task),
                "work_item": work_item_snapshot,
            })

        payload: dict[str, Any] = {
            "version": 2,
            "stop_intent_id": stop_intent_id or "",
            "stop_state": "suspended",
            "suspend_started_at": now,
            "suspend_finalized_at": now,
            "checkpoint_type": checkpoint_type,
            "reason": reason,
            "project_id": project_id,
            "parent_session_id": parent_session_id,
            "session_id": parent_session_id,
            "origin_task_id": origin_task_id or "",
            "run_id": run_id,
            "company_profile": company_profile or getattr(self.config.org, "company_profile", "corporate"),
            "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
            "plan": serialize_company_work_item_runtime_plan(plan),
            "task_ids": [task.id for task in tasks],
            "active_work_items": active_work_items,
            "task_snapshots": task_snapshots,
            "role_runtime_session_ids": sorted(dict.fromkeys(role_runtime_session_ids)),
            "seat_state_ids": sorted(dict.fromkeys(seat_state_ids)),
            "native_runtime_resume": native_runtime_resume_by_task,
            "adapter_session_state": adapter_session_state_by_role,
            "external_sessions": external_sessions_by_task,
            "progress_tail": {
                task.id: self._checkpoint_progress_tail(task)
                for task in tasks
            },
            "created_at": now,
        }
        payload["basis_hash"] = self._checkpoint_basis_hash(payload)
        return payload

    async def _build_company_runtime_suspend_checkpoint(
        self,
        *,
        checkpoint_type: str,
        reason: str,
        parent_session_id: str,
        origin_task_id: str | None,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        stop_intent_id: str | None = None,
        payload_updates: dict[str, Any] | None = None,
    ) -> ExecutionCheckpoint:
        payload = await self._company_runtime_checkpoint_payload(
            checkpoint_type=checkpoint_type,
            reason=reason,
            parent_session_id=parent_session_id,
            origin_task_id=origin_task_id,
            plan=plan,
            tasks=tasks,
            stop_intent_id=stop_intent_id,
        )
        if payload_updates:
            payload.update(dict(payload_updates))
            payload["basis_hash"] = self._checkpoint_basis_hash(payload)
        return ExecutionCheckpoint(
            project_id=self.project_id or "default",
            session_id=parent_session_id,
            checkpoint_type=checkpoint_type,
            task_id=origin_task_id,
            payload=payload,
        )

    async def _save_company_runtime_suspend_checkpoint(
        self,
        *,
        checkpoint_type: str,
        reason: str,
        parent_session_id: str,
        origin_task_id: str | None,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        stop_intent_id: str | None = None,
        payload_updates: dict[str, Any] | None = None,
    ) -> tuple[ExecutionCheckpoint, bool]:
        assert self.store
        checkpoint = await self._build_company_runtime_suspend_checkpoint(
            checkpoint_type=checkpoint_type,
            reason=reason,
            parent_session_id=parent_session_id,
            origin_task_id=origin_task_id,
            plan=plan,
            tasks=tasks,
            stop_intent_id=stop_intent_id,
            payload_updates=payload_updates,
        )
        return await self.store.get_or_create_active_execution_checkpoint(
            checkpoint,
            checkpoint_types=_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES,
        )

    async def get_pending_company_runtime_suspend_checkpoint(
        self,
        parent_session_id: str | None,
    ) -> ExecutionCheckpoint | None:
        if not self.store:
            return None
        sid = str(parent_session_id or "").strip()
        if not sid:
            return None
        checkpoints = await self.store.get_pending_checkpoints(
            project_id=self.project_id or "default",
            session_id=sid,
            checkpoint_types=list(_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES),
        )
        return checkpoints[0] if checkpoints else None

    async def get_active_company_runtime_suspend_checkpoint(
        self,
        parent_session_id: str | None,
    ) -> ExecutionCheckpoint | None:
        if not self.store:
            return None
        sid = str(parent_session_id or "").strip()
        if not sid:
            return None
        getter = getattr(self.store, "get_execution_checkpoints", None)
        if callable(getter):
            checkpoints = await getter(
                project_id=self.project_id or "default",
                session_id=sid,
                checkpoint_types=list(_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES),
                statuses=["pending", "resuming"],
            )
            return checkpoints[0] if checkpoints else None
        return await self.get_pending_company_runtime_suspend_checkpoint(sid)

    @staticmethod
    def _suspend_target_phase(current: Phase | str | None) -> Phase:
        if isinstance(current, Phase):
            phase = current
        else:
            try:
                phase = Phase(str(current or Phase.READY.value))
            except ValueError:
                return Phase.READY
        if phase in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}:
            return phase
        if phase == Phase.RUNNING:
            return Phase.PAUSED
        if phase == Phase.READY_FOR_REWORK:
            return Phase.READY_FOR_REWORK
        if phase in {Phase.QUEUED, Phase.READY}:
            return phase
        return Phase.READY

    async def _company_runtime_task_is_fully_suspended(
        self,
        task: Task,
        *,
        checkpoint_type: str,
    ) -> bool:
        if not self.store:
            return False
        metadata = dict(task.metadata or {})
        if (
            str(metadata.get("company_runtime_stop_state", "") or "").strip() != "suspended"
            or str(metadata.get("company_runtime_suspend_checkpoint_type", "") or "").strip()
            != checkpoint_type
            or not str(metadata.get("company_runtime_suspended_at", "") or "").strip()
            or task.execution_lock
            or task.execution_locked_at is not None
        ):
            return False

        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id:
            getter = getattr(self.store, "get_delegation_work_item", None)
            if not callable(getter):
                return False
            try:
                work_item = await getter(work_item_id)
            except Exception:
                return False
            if work_item is None:
                return False
            work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
            if getattr(work_item, "phase", None) not in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}:
                if (
                    str(metadata.get("dispatch_hold", "") or "").strip()
                    != "company_runtime_suspended"
                    or str(work_item_metadata.get("dispatch_hold", "") or "").strip()
                    != "company_runtime_suspended"
                    or str(work_item_metadata.get("suspend_checkpoint_type", "") or "").strip()
                    != checkpoint_type
                    or str(getattr(work_item, "claimed_by_role_runtime_session_id", "") or "").strip()
                    or str(getattr(work_item, "claimed_by_seat_id", "") or "").strip()
                    or str(work_item_metadata.get("claimed_by_role_session_id", "") or "").strip()
                    or str(work_item_metadata.get("claimed_task_id", "") or "").strip()
                ):
                    return False

        role_session_id = str(metadata.get("delegation_role_session_id", "") or "").strip()
        if role_session_id:
            getter = getattr(self.store, "get_delegation_role_session", None)
            if callable(getter):
                try:
                    role_session = await getter(role_session_id)
                except Exception:
                    return False
                if role_session is not None and (
                    str(getattr(role_session, "focused_work_item_id", "") or "").strip()
                    or str(getattr(role_session, "status", "") or "").strip().lower() != "idle"
                ):
                    return False

        latest_session = await self._load_latest_external_session_for_task(task)
        if latest_session is not None:
            session_status = str(getattr(latest_session, "status", "") or "").strip().lower()
            if session_status not in {"suspended", "failed", "cancelled", "denied", "rejected"}:
                return False
        return True

    async def _suspend_company_runtime_tasks(
        self,
        tasks: list[Task],
        *,
        reason: str,
        checkpoint_type: str,
        stop_intent_id: str | None = None,
    ) -> list[str]:
        if not self.store:
            return []

        affected: list[str] = []
        get_work_item = getattr(self.store, "get_delegation_work_item", None)
        update_role_session = getattr(self.store, "update_delegation_role_session", None)
        update_work_item = getattr(self.store, "update_delegation_work_item", None)
        for task in tasks:
            work_item_id = linked_work_item_id_for_task(task)
            work_item = None
            if work_item_id:
                if not callable(get_work_item) or not callable(update_work_item):
                    raise RuntimeError(
                        f"company runtime cannot durably suspend work item {work_item_id}"
                    )
                try:
                    work_item = await get_work_item(work_item_id)
                except Exception:
                    logger.opt(exception=True).error(
                        "company runtime suspend: failed to load authoritative work item {}",
                        work_item_id,
                    )
                    raise
                if work_item is None:
                    raise RuntimeError(
                        f"company runtime work item {work_item_id} no longer exists"
                    )
            task_is_terminal = task.status in {
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
            work_item_is_active = bool(
                work_item is not None
                and getattr(work_item, "phase", None)
                not in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}
            )
            if task_is_terminal and not work_item_is_active:
                continue
            if await self._company_runtime_task_is_fully_suspended(
                task,
                checkpoint_type=checkpoint_type,
            ):
                continue
            affected.append(task.id)
            task.metadata = dict(task.metadata or {})
            original_task_status = task.status.value if isinstance(task.status, TaskStatus) else str(task.status or "")
            task.metadata["last_stop_reason"] = reason
            task.metadata["company_runtime_suspend_checkpoint_type"] = checkpoint_type
            task.metadata["company_runtime_suspended_at"] = datetime.now().isoformat()
            task.metadata["company_runtime_stop_state"] = "suspended"
            task.metadata["company_runtime_stop_intent_id"] = stop_intent_id or task.metadata.get("company_runtime_stop_intent_id", "")
            task.metadata["company_runtime_stop_marked_at"] = task.metadata.get("company_runtime_stop_marked_at") or datetime.now().isoformat()
            task.metadata.setdefault("suspended_task_status", original_task_status)
            task.execution_lock = False
            task.execution_locked_at = None
            task.result = task.result if task.result else None

            suspended_phase: Phase | None = None
            if work_item_id:
                if (
                    getattr(work_item, "phase", None)
                    not in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}
                ):
                    current_phase = getattr(work_item, "phase", None)
                    suspended_phase = current_phase if isinstance(current_phase, Phase) else self._suspend_target_phase(current_phase)
                    work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
                    original_claim = {
                        "claimed_by_role_runtime_session_id": str(getattr(work_item, "claimed_by_role_runtime_session_id", "") or ""),
                        "claimed_by_seat_id": str(getattr(work_item, "claimed_by_seat_id", "") or ""),
                        "claimed_by_role_session_id": str(work_item_metadata.get("claimed_by_role_session_id", "") or ""),
                        "claimed_task_id": str(work_item_metadata.get("claimed_task_id", "") or task.id),
                    }
                    try:
                        await update_work_item(
                            work_item_id,
                            metadata_updates={
                                "dispatch_hold": "company_runtime_suspended",
                                "suspended_at": datetime.now().isoformat(),
                                "suspend_reason": reason,
                                "suspend_checkpoint_type": checkpoint_type,
                                "suspend_intent_id": stop_intent_id or "",
                                "suspended_phase": suspended_phase.value,
                                "suspended_task_status": original_task_status,
                                "suspended_claim": original_claim,
                                "claimed_by_role_session_id": "",
                                "claimed_task_id": "",
                            },
                            claimed_by_role_runtime_session_id="",
                            claimed_by_seat_id="",
                        )
                    except Exception:
                        logger.opt(exception=True).error(
                            "company runtime suspend: authoritative hold/release failed for {}",
                            work_item_id,
                        )
                        raise
                    else:
                        task.metadata["dispatch_hold"] = "company_runtime_suspended"
                        task.metadata["suspended_phase"] = suspended_phase.value

            latest_session = await self._load_latest_external_session_for_task(task)
            if latest_session is not None:
                try:
                    session_status = str(getattr(latest_session, "status", "") or "").strip().lower()
                    if session_status not in {"failed", "cancelled", "denied", "rejected"}:
                        latest_session.status = "suspended"
                        latest_session.metadata = {
                            **dict(getattr(latest_session, "metadata", {}) or {}),
                            "company_runtime_suspended_at": datetime.now().isoformat(),
                            "company_runtime_suspend_checkpoint_type": checkpoint_type,
                            "company_runtime_stop_intent_id": stop_intent_id or "",
                        }
                        await self.store.save_external_session(latest_session)
                except Exception:
                    logger.opt(exception=True).debug("company runtime suspend: external session status update failed")

            fresh = await self.store.get_task(task.id)
            target = fresh or task
            target.metadata = {**dict(getattr(target, "metadata", {}) or {}), **dict(task.metadata or {})}
            target.execution_lock = False
            target.execution_locked_at = None
            if task_is_terminal:
                # The WorkItem hold write fires the generic phase projection
                # hook, which can briefly rewrite a stale terminal Task to the
                # authoritative nonterminal phase.  A suspend is not a resume:
                # retain the pre-existing Task projection while attaching the
                # durable hold; resume will project the WorkItem deliberately.
                target.status = task.status
            elif target.status not in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                if suspended_phase is not None:
                    target.status = task_status_for_phase(suspended_phase)
                elif original_task_status:
                    try:
                        target.status = TaskStatus(original_task_status)
                    except ValueError:
                        target.status = TaskStatus.IDLE
            await self.store.save_task(target)

            role_session_id = str((target.metadata or {}).get("delegation_role_session_id", "") or "").strip()
            if role_session_id and callable(update_role_session):
                try:
                    await update_role_session(
                        role_session_id,
                        focused_work_item_id="",
                        status="idle",
                        metadata_updates={
                            "last_suspend_reason": reason,
                            "last_suspend_checkpoint_type": checkpoint_type,
                            "last_suspended_at": datetime.now().isoformat(),
                            "last_suspend_intent_id": stop_intent_id or "",
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("company runtime suspend: role session idle update failed")
        return affected

    async def _checkpoint_and_suspend_company_runtime_scope(
        self,
        *,
        checkpoint_type: str,
        reason: str,
        parent_session_id: str,
        origin_task_id: str | None,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        stop_intent_id: str | None = None,
    ) -> tuple[ExecutionCheckpoint, bool, list[str]]:
        """Install durable holds before publishing a resumable checkpoint.

        A pending checkpoint is an execution capability, so exposing it before
        WorkItem/Task holds are committed lets another controller resume while
        Stop is still suspending the scope.  Build the pre-stop snapshot first,
        commit all holds, and only then atomically create (or normalize) the
        single active checkpoint.
        """

        assert self.store
        candidate = await self._build_company_runtime_suspend_checkpoint(
            checkpoint_type=checkpoint_type,
            reason=reason,
            parent_session_id=parent_session_id,
            origin_task_id=origin_task_id,
            plan=plan,
            tasks=tasks,
            stop_intent_id=stop_intent_id,
        )
        existing = await self.get_active_company_runtime_suspend_checkpoint(
            parent_session_id
        )
        checkpoint = existing or candidate
        payload = dict(checkpoint.payload or {})
        effective_reason = str(payload.get("reason", "") or reason)
        effective_type = str(checkpoint.checkpoint_type or checkpoint_type)
        effective_stop_intent_id = str(
            payload.get("stop_intent_id", "") or stop_intent_id or ""
        )
        affected = await self._suspend_company_runtime_tasks(
            tasks,
            reason=effective_reason,
            checkpoint_type=effective_type,
            stop_intent_id=effective_stop_intent_id,
        )

        created = False
        if existing is None:
            checkpoint, created = (
                await self.store.get_or_create_active_execution_checkpoint(
                    candidate,
                    checkpoint_types=_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES,
                )
            )
            if checkpoint.checkpoint_id != candidate.checkpoint_id:
                payload = dict(checkpoint.payload or {})
                affected.extend(
                    await self._suspend_company_runtime_tasks(
                        tasks,
                        reason=str(payload.get("reason", "") or reason),
                        checkpoint_type=str(
                            checkpoint.checkpoint_type or checkpoint_type
                        ),
                        stop_intent_id=str(
                            payload.get("stop_intent_id", "")
                            or stop_intent_id
                            or ""
                        ),
                    )
                )

        # Stop wins over an in-flight resume only after its holds are durable.
        # If resume completed in the meantime, create a fresh checkpoint over
        # those already-installed holds instead of leaving the scope held with
        # no resumable fact.
        for _attempt in range(4):
            status = str(checkpoint.status or "").strip().lower()
            if status in {"pending", "resuming"}:
                transitioned = await self._mark_company_runtime_checkpoint_status(
                    checkpoint,
                    status="pending",
                    payload_updates=(
                        {
                            "resume_state": "interrupted",
                            "resume_interrupted_at": datetime.now().isoformat(),
                            "resume_interruption_reason": reason,
                        }
                        if status == "resuming"
                        else None
                    ),
                    expected_statuses={status},
                )
                if transitioned:
                    return checkpoint, created, list(dict.fromkeys(affected))
                refreshed = await self._load_execution_checkpoint_by_id(
                    checkpoint.checkpoint_id
                )
                if refreshed is not None:
                    checkpoint = refreshed
                    continue

            replacement = ExecutionCheckpoint(
                project_id=candidate.project_id,
                session_id=candidate.session_id,
                checkpoint_type=candidate.checkpoint_type,
                task_id=candidate.task_id,
                payload=dict(candidate.payload or {}),
            )
            checkpoint, replacement_created = (
                await self.store.get_or_create_active_execution_checkpoint(
                    replacement,
                    checkpoint_types=_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES,
                )
            )
            created = created or replacement_created

        raise RuntimeError(
            "company runtime checkpoint kept changing while suspension was finalized"
        )

    async def suspend_company_runtime(
        self,
        *,
        origin_task_id: str,
        session_id: str | None = None,
        reason: str = "user_stop",
        checkpoint_type: str = "company_runtime_suspended",
        stop_intent_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.store:
            return None
        project_id = str(self.project_id or "default").strip() or "default"
        requested_session_id = str(session_id or "").strip()
        identity_index = await load_company_runtime_identity_index(
            self.store,
            project_id,
        )
        identity = identity_index.resolve(task_id=origin_task_id)
        if identity is None and requested_session_id:
            identity = identity_index.resolve(runtime_session_id=requested_session_id)
        if identity is None:
            return None
        parent_session_id = identity.runtime_session_id
        if requested_session_id and requested_session_id != parent_session_id:
            requested_identity = identity_index.resolve(
                runtime_session_id=requested_session_id,
            )
            if requested_identity is None or requested_identity != identity:
                return None

        async with self._active_task_run_registry.scope_lock(
            project_id,
            parent_session_id,
        ):
            snapshot = await self._load_company_runtime_snapshot(parent_session_id)
            if not snapshot:
                return None
            plan, tasks = snapshot
            if not tasks:
                return None

            checkpoint, created, affected_ids = (
                await self._checkpoint_and_suspend_company_runtime_scope(
                    checkpoint_type=checkpoint_type,
                    reason=reason,
                    parent_session_id=parent_session_id,
                    origin_task_id=origin_task_id,
                    plan=plan,
                    tasks=tasks,
                    stop_intent_id=stop_intent_id,
                )
            )
            idempotent = not created

            payload = dict(checkpoint.payload or {})
            effective_stop_intent_id = str(
                payload.get("stop_intent_id", "") or stop_intent_id or ""
            )
            return {
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "session_id": parent_session_id,
                "task_ids": affected_ids,
                "stop_intent_id": effective_stop_intent_id,
                "idempotent": idempotent,
            }

    @staticmethod
    def _company_runtime_dependencies_satisfied(
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem],
    ) -> bool:
        from opc.layer2_organization.work_item_transition import (
            settled_failure_dependency_ids,
        )

        metadata = dict(getattr(work_item, "metadata", {}) or {})
        dependency_ids = [
            str(item).strip()
            for item in list(metadata.get("dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        if not dependency_ids:
            return True
        dependency_classes = dict(metadata.get("dependency_classes", {}) or {})
        settled_failure_ids = settled_failure_dependency_ids(metadata)
        for dep_id in dependency_ids:
            dependency = work_item_by_id.get(dep_id)
            if dependency is None:
                continue
            dep_phase = getattr(dependency, "phase", None)
            dep_class = str(dependency_classes.get(dep_id, "hard") or "hard").strip().lower()
            if dep_class == "info":
                continue
            if dep_class == "soft":
                if dep_phase not in DONE_PHASES and dep_phase not in IN_PROGRESS_PHASES:
                    if dep_id in settled_failure_ids:
                        continue
                    return False
                continue
            if dep_phase != Phase.APPROVED:
                # Failure-triage release: the frontier pass released this
                # card over the dep (dependency_settlement stamp). Stop/
                # resume must not regress a released triage card back into
                # WAITING_* — with the failed dep already terminal, no
                # later event would ever wake it again.
                if dep_id in settled_failure_ids:
                    continue
                return False
        return True

    @classmethod
    def _company_runtime_resume_target_phase(
        cls,
        work_item: DelegationWorkItem,
        restored_phase: Phase | None,
        work_item_by_id: dict[str, DelegationWorkItem],
    ) -> Phase:
        current_phase = getattr(work_item, "phase", Phase.READY)
        if not isinstance(current_phase, Phase):
            try:
                current_phase = Phase(str(current_phase or Phase.READY.value))
            except ValueError:
                current_phase = Phase.READY
        if current_phase in DONE_PHASES:
            return current_phase
        original_phase = restored_phase or current_phase
        if original_phase in DONE_PHASES:
            return current_phase
        if original_phase in IN_REVIEW_PHASES:
            return original_phase
        if original_phase == Phase.WAITING_DEPENDENCIES:
            return Phase.WAITING_DEPENDENCIES

        deps_satisfied = cls._company_runtime_dependencies_satisfied(work_item, work_item_by_id)
        if not deps_satisfied:
            if original_phase == Phase.RUNNING:
                return Phase.WAITING_FOR_CHILDREN
            if original_phase in {Phase.READY, Phase.READY_FOR_REWORK, Phase.QUEUED}:
                return Phase.WAITING_DEPENDENCIES
            return original_phase
        if original_phase == Phase.QUEUED:
            return Phase.READY
        return original_phase

    @staticmethod
    def _checkpoint_task_execution_identity(
        task_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the immutable execution identity captured at suspension.

        Checkpoints created before the explicit ``execution_identity`` field
        already contain enough role/agent data to derive a safe identity.  The
        derivation is intentionally local to checkpoint consumption; it is not
        a second runtime resolver.
        """

        explicit = dict(task_snapshot.get("execution_identity", {}) or {})
        work_item_snapshot = dict(task_snapshot.get("work_item", {}) or {})
        work_item_metadata = dict(work_item_snapshot.get("metadata", {}) or {})
        employee_assignment = dict(
            explicit.get("employee_assignment", {})
            or work_item_metadata.get("employee_assignment", {})
            or {}
        )
        assigned_external_agent = str(
            explicit.get("assigned_external_agent")
            if "assigned_external_agent" in explicit
            else task_snapshot.get("assigned_external_agent", "")
            or ""
        ).strip()
        selected_execution_agent = normalize_recruitment_agent_choice(
            explicit.get("selected_execution_agent")
            or task_snapshot.get("selected_execution_agent"),
            default=assigned_external_agent or "native",
        ) or "native"
        return {
            "role_id": str(
                explicit.get("role_id")
                or work_item_snapshot.get("role_id")
                or task_snapshot.get("assigned_to")
                or ""
            ).strip(),
            "seat_id": str(
                explicit.get("seat_id")
                or work_item_snapshot.get("seat_id")
                or ""
            ).strip(),
            "role_runtime_session_id": str(
                explicit.get("role_runtime_session_id")
                or work_item_snapshot.get("role_runtime_session_id")
                or task_snapshot.get("role_session_id")
                or ""
            ).strip(),
            "employee_id": str(
                explicit.get("employee_id")
                or employee_assignment.get("employee_id")
                or ""
            ).strip(),
            "employee_assignment": copy.deepcopy(employee_assignment),
            "selected_execution_agent": selected_execution_agent,
            "assigned_external_agent": assigned_external_agent,
            "preferred_external_agent": str(
                explicit.get("preferred_external_agent", "") or ""
            ).strip(),
            "execution_agent_locked": (
                bool(explicit.get("execution_agent_locked", False))
                if "execution_agent_locked" in explicit
                else None
            ),
            "selected_execution_agent_source": str(
                explicit.get("agent_selection_source")
                or explicit.get("selected_execution_agent_source", "")
                or ""
            ).strip(),
            "explicit": bool(explicit),
        }

    @classmethod
    def _restore_and_pin_company_resume_execution_identity(
        cls,
        task: Task,
        work_item: DelegationWorkItem | None,
        task_snapshot: dict[str, Any],
        role_session: RoleRuntimeSession | None,
        *,
        checkpoint_id: str,
    ) -> None:
        """Validate durable role identity and pin this resumed attempt's agent.

        A resume must continue the suspended actor, not re-run recruitment or
        adaptive backend selection.  Non-empty durable values that disagree
        with the checkpoint fail closed.  Missing Task projection fields are
        restored from the checkpoint after the authoritative WorkItem has also
        been checked.
        """

        identity = cls._checkpoint_task_execution_identity(task_snapshot)
        metadata = dict(task.metadata or {})
        task_role_id = str(
            task.assigned_to or metadata.get("work_item_role_id", "") or ""
        ).strip()
        task_seat_id = str(
            metadata.get("delegation_seat_id", "")
            or metadata.get("seat_id", "")
            or ""
        ).strip()
        task_role_session_id = str(
            metadata.get("delegation_role_session_id", "") or ""
        ).strip()
        task_assignment = dict(metadata.get("employee_assignment", {}) or {})
        task_employee_id = str(task_assignment.get("employee_id", "") or "").strip()

        work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
        work_item_assignment = dict(
            work_item_metadata.get("employee_assignment", {}) or {}
        )
        if work_item is not None:
            current_values: dict[str, list[str]] = {
                "role_id": [str(getattr(work_item, "role_id", "") or "").strip()],
                "seat_id": [str(getattr(work_item, "seat_id", "") or "").strip()],
                "role_runtime_session_id": [str(
                    getattr(work_item, "role_runtime_session_id", "") or ""
                ).strip()],
                "employee_id": [str(
                    work_item_assignment.get("employee_id", "") or ""
                ).strip()],
            }
        else:
            current_values = {
                "role_id": [task_role_id],
                "seat_id": [task_seat_id],
                "role_runtime_session_id": [task_role_session_id],
                "employee_id": [task_employee_id],
            }
        for field_name, values in current_values.items():
            expected = str(identity.get(field_name, "") or "").strip()
            if not expected:
                continue
            for current in values:
                if current and current != expected:
                    raise RuntimeError(
                        "company runtime resume identity mismatch for "
                        f"task {task.id}: {field_name}={current!r}, "
                        f"checkpoint={expected!r}"
                    )

        expected_role_session_id = str(
            identity.get("role_runtime_session_id", "") or ""
        ).strip()
        if expected_role_session_id and identity.get("explicit") and role_session is None:
            raise RuntimeError(
                "company runtime resume identity mismatch for "
                f"task {task.id}: role runtime session {expected_role_session_id!r} is missing"
            )
        if role_session is not None:
            # A role session's scalar seat is its home/primary seat, while the
            # checkpoint seat is the authoritative WorkItem's placement.
            role_session_values = {
                "role_id": str(getattr(role_session, "role_id", "") or "").strip(),
                "employee_id": str(getattr(role_session, "employee_id", "") or "").strip(),
            }
            for field_name, current in role_session_values.items():
                expected = str(identity.get(field_name, "") or "").strip()
                if expected and current and current != expected:
                    raise RuntimeError(
                        "company runtime resume identity mismatch for "
                        f"task {task.id}: role session {field_name}={current!r}, "
                        f"checkpoint={expected!r}"
                    )

        expected_agent = str(
            identity.get("selected_execution_agent", "") or "native"
        ).strip()
        expected_assigned_agent = str(
            identity.get("assigned_external_agent", "") or ""
        ).strip()
        if expected_agent == "native":
            if expected_assigned_agent:
                raise RuntimeError(
                    f"company runtime resume checkpoint has inconsistent native agent for task {task.id}"
                )
        elif expected_assigned_agent and expected_assigned_agent != expected_agent:
            raise RuntimeError(
                f"company runtime resume checkpoint has inconsistent external agent for task {task.id}"
            )
        else:
            expected_assigned_agent = expected_agent

        current_agent, current_assigned_agent, _current_source = (
            cls._task_effective_execution_agent_identity(task)
        )
        if (
            current_agent != expected_agent
            or current_assigned_agent != expected_assigned_agent
        ):
            raise RuntimeError(
                "company runtime resume identity mismatch for "
                f"task {task.id}: execution_agent={current_agent!r}, "
                f"checkpoint={expected_agent!r}"
            )

        expected_source = str(
            identity.get("selected_execution_agent_source", "") or ""
        ).strip()

        metadata["work_item_role_id"] = identity["role_id"] or task_role_id
        if identity["seat_id"]:
            metadata.update(delegation_seat_id=identity["seat_id"])
        if identity["role_runtime_session_id"]:
            metadata.update(
                delegation_role_session_id=identity["role_runtime_session_id"]
            )
        if identity.get("explicit") or identity["employee_assignment"]:
            metadata["employee_assignment"] = copy.deepcopy(
                identity["employee_assignment"]
            )
        metadata["selected_execution_agent"] = expected_agent
        metadata["preferred_external_agent"] = (
            str(identity.get("preferred_external_agent", "") or "").strip()
            or (expected_assigned_agent if expected_agent != "native" else None)
        )
        metadata["_company_runtime_resume_execution_agent_pin"] = {
            "checkpoint_id": str(checkpoint_id or "").strip(),
            "selected_execution_agent": expected_agent,
            "assigned_external_agent": expected_assigned_agent,
            "selected_execution_agent_source": expected_source,
        }
        task.assigned_to = identity["role_id"] or task_role_id
        task.assigned_external_agent = expected_assigned_agent or None
        task.metadata = metadata

    async def _prepare_company_runtime_tasks_for_resume(
        self,
        tasks: list[Task],
        payload: dict[str, Any],
        *,
        resume_task_ids: set[str] | None = None,
    ) -> list[Task]:
        assert self.store

        task_snapshot_by_id = {
            str(item.get("task_id", "") or "").strip(): dict(item)
            for item in list(payload.get("task_snapshots", []) or [])
            if isinstance(item, dict) and str(item.get("task_id", "") or "").strip()
        }
        work_item_snapshot_by_id = {
            str(item.get("work_item_id", "") or "").strip(): dict(item)
            for item in list(payload.get("active_work_items", []) or [])
            if isinstance(item, dict) and str(item.get("work_item_id", "") or "").strip()
        }
        refreshed: list[Task] = []
        get_work_item = getattr(self.store, "get_delegation_work_item", None)
        get_role_session = getattr(self.store, "get_delegation_role_session", None)
        list_work_items = getattr(self.store, "list_delegation_work_items", None)
        update_role_session = getattr(self.store, "update_delegation_role_session", None)
        update_work_item = getattr(self.store, "update_delegation_work_item", None)
        work_item_by_id: dict[str, DelegationWorkItem] = {}
        run_ids = {
            str((getattr(task, "metadata", {}) or {}).get("delegation_run_id", "") or "").strip()
            for task in tasks
            if str((getattr(task, "metadata", {}) or {}).get("delegation_run_id", "") or "").strip()
        }
        if callable(list_work_items):
            for run_id in sorted(run_ids):
                try:
                    for item in await list_work_items(run_id):
                        work_item_id = str(getattr(item, "work_item_id", "") or "").strip()
                        if work_item_id:
                            work_item_by_id[work_item_id] = item
                except Exception:
                    logger.opt(exception=True).debug("company runtime resume: failed to load run work items")
        for task in tasks:
            if resume_task_ids is not None and task.id not in resume_task_ids:
                refreshed.append(task)
                continue
            work_item_id = linked_work_item_id_for_task(task)
            work_item = work_item_by_id.get(work_item_id)
            if work_item_id and (
                not callable(get_work_item) or not callable(update_work_item)
            ):
                raise RuntimeError(
                    f"company runtime cannot durably resume work item {work_item_id}"
                )
            if work_item is None and work_item_id:
                try:
                    work_item = await get_work_item(work_item_id)
                except Exception:
                    logger.opt(exception=True).error(
                        "company runtime resume: failed to load authoritative work item {}",
                        work_item_id,
                    )
                    raise
                if work_item is not None:
                    work_item_by_id[work_item_id] = work_item
            if work_item_id and work_item is None:
                raise RuntimeError(
                    f"company runtime work item {work_item_id} no longer exists"
                )
            task_is_terminal = task.status in {
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
            work_item_is_nonterminal = bool(
                work_item is not None
                and getattr(work_item, "phase", None) not in DONE_PHASES
            )
            if task_is_terminal and not work_item_is_nonterminal:
                refreshed.append(task)
                continue
            task_snapshot = task_snapshot_by_id.get(task.id, {})
            if not task_snapshot:
                raise RuntimeError(
                    f"company runtime resume checkpoint has no task identity snapshot for {task.id}"
                )
            identity = self._checkpoint_task_execution_identity(task_snapshot)
            expected_role_session_id = str(
                identity.get("role_runtime_session_id", "") or ""
            ).strip()
            role_session = None
            if expected_role_session_id and callable(get_role_session):
                role_session = await get_role_session(expected_role_session_id)
            self._restore_and_pin_company_resume_execution_identity(
                task,
                work_item,
                task_snapshot,
                role_session,
                checkpoint_id=str(payload.get("checkpoint_id", "") or ""),
            )
            task.metadata = dict(task.metadata or {})
            task.context_snapshot = dict(task.context_snapshot or {})
            runtime_resume = dict(payload.get("native_runtime_resume", {}) or {}).get(task.id)
            if isinstance(runtime_resume, dict) and runtime_resume:
                task.context_snapshot["runtime_resume"] = dict(runtime_resume)
                task.metadata["runtime_v2"] = dict(runtime_resume)
            external_sessions = dict(payload.get("external_sessions", {}) or {})
            external_session = external_sessions.get(task.id)
            task.metadata.pop("external_resume_checkpoint_session_updated_at", None)
            task.metadata.pop("external_resume_checkpoint_session_status", None)
            if isinstance(external_session, dict):
                token_allowed = external_session_status_allows_resume(
                    external_session.get("status")
                )
                agent_type = str(
                    external_session.get("agent_type")
                    or task.assigned_external_agent
                    or ""
                ).strip()
                assigned_agent_type = str(task.assigned_external_agent or "").strip()
                token_candidates = [
                    str(external_session.get("resume_session_id") or "").strip(),
                    str(external_session.get("provider_session_id") or "").strip(),
                    str(external_session.get("session_id") or "").strip(),
                ]
                token = next(
                    (
                        candidate
                        for candidate in token_candidates
                        if is_provider_session_token(
                            candidate,
                            agent_type=agent_type,
                            project_id=str(task.project_id or "default"),
                        )
                    ),
                    "",
                )
                if (
                    token
                    and agent_type
                    and token_allowed
                    and agent_type == assigned_agent_type
                ):
                    task.metadata["external_resume_session_id"] = token
                    task.metadata["external_resume_agent_type"] = agent_type
                    task.metadata["external_resume_session_scope_id"] = task_session_scope_id(task)
                    task.metadata["external_resume_checkpoint_session_updated_at"] = str(
                        external_session.get("updated_at", "") or ""
                    ).strip()
                    task.metadata["external_resume_checkpoint_session_status"] = str(
                        external_session.get("status", "") or ""
                    ).strip()
                    task.metadata.pop("external_resume_fallback", None)
                elif task.assigned_external_agent:
                    task.metadata.pop("external_resume_session_id", None)
                    task.metadata.pop("external_resume_agent_type", None)
                    task.metadata.pop("external_resume_session_scope_id", None)
                    task.metadata["external_resume_fallback"] = "context_replay"
            task.execution_lock = False
            task.execution_locked_at = None
            task.result = None
            work_item_snapshot = work_item_snapshot_by_id.get(work_item_id, {})
            task_work_item_snapshot = task_snapshot.get("work_item", {})
            phase_value = str(work_item_snapshot.get("phase", "") or "").strip()
            if not phase_value and isinstance(task_work_item_snapshot, dict):
                phase_value = str(task_work_item_snapshot.get("phase", "") or "").strip()
            try:
                restored_phase = Phase(phase_value) if phase_value else None
            except ValueError:
                restored_phase = None
            work_item_phase = getattr(work_item, "phase", None) if work_item is not None else None
            if work_item is not None and work_item_phase in DONE_PHASES:
                task.status = task_status_for_phase(work_item_phase)
                task.execution_lock = False
                task.execution_locked_at = None
                for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS:
                    task.metadata.pop(key, None)
                task.metadata["company_runtime_resume_checkpoint_id"] = str(payload.get("checkpoint_id", "") or "")
                task.metadata["company_runtime_resume_requested_at"] = datetime.now().isoformat()
                await self.store.save_task(task)
                fresh = await self.store.get_task(task.id)
                refreshed.append(fresh or task)
                continue
            target_phase: Phase | None = None
            if work_item is not None and getattr(work_item, "phase", None) not in DONE_PHASES:
                target_phase = self._company_runtime_resume_target_phase(
                    work_item,
                    restored_phase,
                    work_item_by_id,
                )
                task.status = task_status_for_phase(target_phase)
            elif restored_phase is not None and restored_phase not in DONE_PHASES:
                target_phase = restored_phase
                task.status = task_status_for_phase(restored_phase)
            else:
                suspended_status = str(
                    task.metadata.get("suspended_task_status")
                    or task_snapshot.get("status")
                    or ""
                ).strip()
                try:
                    task.status = TaskStatus(suspended_status) if suspended_status else TaskStatus.PENDING
                except ValueError:
                    task.status = TaskStatus.PENDING
            for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS:
                task.metadata.pop(key, None)
            task.metadata["company_runtime_resume_checkpoint_id"] = str(payload.get("checkpoint_id", "") or "")
            task.metadata["company_runtime_resume_requested_at"] = datetime.now().isoformat()
            progress = list(task.metadata.get("progress_log", []) or [])
            progress.append("Resumed from company runtime suspend checkpoint.")
            task.metadata["progress_log"] = progress[-20:]

            if work_item_id:
                if work_item is None:
                    try:
                        work_item = await get_work_item(work_item_id)
                    except Exception:
                        logger.opt(exception=True).error(
                            "company runtime resume: failed to reload authoritative work item {}",
                            work_item_id,
                        )
                        raise
                if work_item is not None and getattr(work_item, "phase", None) not in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}:
                    phase_kwargs: dict[str, Any] = {}
                    if target_phase is not None and target_phase not in DONE_PHASES:
                        current_phase = getattr(work_item, "phase", None)
                        if current_phase != target_phase:
                            phase_kwargs["phase"] = target_phase
                    try:
                        await update_work_item(
                            work_item_id,
                            **phase_kwargs,
                            metadata_updates={
                                "dispatch_hold": "",
                                "resume_requested_at": datetime.now().isoformat(),
                                "resume_source_checkpoint_id": str(payload.get("checkpoint_id", "") or ""),
                                "resume_source_checkpoint_type": str(payload.get("checkpoint_type", "") or ""),
                                "claimed_by_role_session_id": "",
                                "claimed_task_id": "",
                            },
                            claimed_by_role_runtime_session_id="",
                            claimed_by_seat_id="",
                        )
                    except Exception:
                        logger.opt(exception=True).debug("company runtime resume: phase restore/hold clear failed")
                        try:
                            await update_work_item(
                                work_item_id,
                                metadata_updates={
                                    "dispatch_hold": "",
                                    "resume_requested_at": datetime.now().isoformat(),
                                    "resume_source_checkpoint_id": str(payload.get("checkpoint_id", "") or ""),
                                },
                                claimed_by_role_runtime_session_id="",
                                claimed_by_seat_id="",
                            )
                        except Exception:
                            logger.opt(exception=True).error(
                                "company runtime resume: authoritative fallback hold clear failed"
                            )
                            raise

            role_session_id = str(task.metadata.get("delegation_role_session_id", "") or "").strip()
            if role_session_id and callable(update_role_session):
                try:
                    await update_role_session(
                        role_session_id,
                        focused_work_item_id="",
                        status="idle",
                        metadata_updates={
                            "last_resume_checkpoint_type": str(payload.get("checkpoint_type", "") or ""),
                            "last_resume_requested_at": datetime.now().isoformat(),
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("company runtime resume: role session update failed")
            await self.store.save_task(task)
            fresh = await self.store.get_task(task.id)
            refreshed.append(fresh or task)
        return refreshed

    async def _clear_company_runtime_parent_stop_state(
        self,
        parent_session_id: str,
        payload: dict[str, Any],
    ) -> None:
        if not self.store:
            return
        parent_session_id = str(parent_session_id or "").strip()
        if not parent_session_id:
            return
        checkpoint_id = str(payload.get("checkpoint_id", "") or "").strip()
        identity_index = await load_company_runtime_identity_index(
            self.store,
            self.project_id or "default",
        )
        identity = identity_index.resolve(
            runtime_session_id=parent_session_id,
            checkpoint_id=checkpoint_id,
        )
        if identity is None or not identity.ui_anchor_task_id:
            return
        task = identity_index.task(identity.ui_anchor_task_id)
        if task is None or not is_pure_company_ui_anchor(
            task,
            identity.runtime_session_id,
        ):
            return
        metadata = dict(getattr(task, "metadata", {}) or {})
        if not any(key in metadata for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS):
            return
        for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS:
            metadata.pop(key, None)
        metadata["company_runtime_resume_checkpoint_id"] = checkpoint_id
        metadata["company_runtime_resume_requested_at"] = datetime.now().isoformat()
        task.metadata = metadata
        task.execution_lock = False
        task.execution_locked_at = None
        await self.store.save_task(task)

    @staticmethod
    def _clear_pending_reorg_marker(task: Task) -> None:
        task.metadata = dict(task.metadata)
        task.metadata.pop("pending_reorg_proposal_id", None)
        task.metadata.pop("pending_reorg_scope", None)

    async def _reconcile_company_work_item_plan_state(
        self,
        parent_session_id: str,
        plan: CompanyWorkItemRuntimePlan,
    ) -> tuple[CompanyWorkItemRuntimePlan, list[Task]] | None:
        if not self.store:
            return None
        snapshot = await self._load_company_runtime_snapshot(parent_session_id)
        if not snapshot:
            return None
        _, tasks = snapshot
        ordered_tasks = list(tasks)
        all_task_ids = [task.id for task in ordered_tasks]
        serialized_plan = serialize_company_work_item_runtime_plan(plan)
        current_org_version = self.org_engine.current_org_version() if self.org_engine else 1
        current_runtime_topology_version = self.org_engine.current_runtime_topology_version() if self.org_engine else 1

        for task in ordered_tasks:
            self._clear_pending_reorg_marker(task)
            task.metadata["company_work_item_plan"] = serialized_plan
            task.metadata["execution_task_ids"] = list(all_task_ids)
            task.metadata["org_version"] = current_org_version
            task.metadata["runtime_topology_version"] = current_runtime_topology_version
            await self.store.save_task(task)
        return plan, ordered_tasks

    @staticmethod
    def _uses_primary_session_external_continuity(task: Task) -> bool:
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not session_id:
            return False
        mode = str(task.metadata.get("mode", "") or "").strip().lower()
        task_mode_contract = str(task.metadata.get("task_mode_contract", "") or "").strip()
        return mode == "task" or task_mode_contract == "single_full_capability_main_agent"

    async def _load_latest_external_session_for_task(self, task: Task) -> Any | None:
        if not self.store or not task.id:
            return None
        project_id = task.project_id or self.project_id or "default"
        agent_type = str(task.assigned_external_agent or "").strip()
        fallback = getattr(self.store, "get_external_session", None)
        if (
            callable(fallback)
            and agent_type
            and self._uses_primary_session_external_continuity(task)
            and str(getattr(task, "session_id", "") or "").strip()
        ):
            session = await fallback(
                agent_type,
                project_id,
                opc_session_id=str(task.session_id or "").strip(),
            )
            if session:
                return session
        getter = getattr(self.store, "get_latest_external_session_for_task", None)
        if callable(getter):
            try:
                session = await getter(project_id, task.id)
                if (
                    session
                    and agent_type
                    and str(getattr(session, "agent_type", "") or "").strip() != agent_type
                ):
                    if callable(fallback):
                        return await fallback(agent_type, project_id, task_id=task.id)
                    return None
                return session
            except TypeError:
                pass
        if not agent_type:
            return None
        if not callable(fallback):
            return None
        return await fallback(agent_type, project_id, task_id=task.id)

    async def _load_best_external_resume_session_for_task(
        self,
        task: Task,
    ) -> Any | None:
        """Prefer a real provider thread over monitoring placeholder rows.

        A live run initially owns a synthetic ``agent:project:task`` row.  The
        provider may publish its real thread id milliseconds later with the
        same (or an older) timestamp.  Recency alone is therefore not a valid
        resume-token selector.
        """

        if not self.store or not task.id:
            return None
        agent_type = str(task.assigned_external_agent or "").strip()
        if not agent_type:
            return None
        list_sessions = getattr(self.store, "list_external_sessions", None)
        if not callable(list_sessions):
            return None
        try:
            sessions = await list_sessions(
                project_id=task.project_id or self.project_id or "default",
                task_id=task.id,
                limit=100,
            )
        except TypeError:
            return None
        except Exception:
            logger.opt(exception=True).debug(
                "failed to list external resume-session candidates"
            )
            return None
        selected, _token = select_best_external_resume_session(
            sessions,
            agent_type=agent_type,
            project_id=str(task.project_id or self.project_id or "default"),
        )
        return selected

    async def _checkpoint_external_resume_token_was_terminalized(
        self,
        task: Task,
        *,
        agent_type: str,
        token: str,
        checkpoint_updated_at: str,
        checkpoint_status: str,
    ) -> bool:
        """Let a newer durable terminal row veto a checkpoint's working token."""

        if not self.store or not task.id or not checkpoint_updated_at:
            return False
        list_sessions = getattr(self.store, "list_external_sessions", None)
        if not callable(list_sessions):
            return False
        try:
            sessions = await list_sessions(
                project_id=task.project_id or self.project_id or "default",
                task_id=task.id,
                limit=100,
            )
        except Exception:
            logger.opt(exception=True).debug(
                "failed to verify checkpoint external resume token status"
            )
            return False
        matching = [
            session
            for session in sessions
            if str(getattr(session, "agent_type", "") or "").strip() == agent_type
            and external_session_matches_provider_token(session, token)
        ]
        if not matching:
            return False

        def _timestamp(value: Any) -> float:
            candidate = value
            if isinstance(candidate, str):
                try:
                    candidate = datetime.fromisoformat(candidate)
                except ValueError:
                    return 0.0
            try:
                return float(candidate.timestamp())
            except Exception:
                return 0.0

        latest = max(
            matching,
            key=lambda session: _timestamp(getattr(session, "updated_at", None)),
        )
        latest_status = str(getattr(latest, "status", "") or "").strip().lower()
        if external_session_status_allows_resume(latest_status):
            return False
        latest_timestamp = _timestamp(getattr(latest, "updated_at", None))
        checkpoint_timestamp = _timestamp(checkpoint_updated_at)
        return latest_timestamp > checkpoint_timestamp or (
            latest_timestamp >= checkpoint_timestamp
            and latest_status != str(checkpoint_status or "").strip().lower()
        )

    @staticmethod
    def _clone_external_adapter(adapter: Any) -> Any:
        config = getattr(adapter, "config", None)
        if config is None:
            return adapter
        if hasattr(config, "model_copy"):
            cloned_config = config.model_copy(deep=True)
        else:
            cloned_config = config
        return adapter.__class__(config=cloned_config)

    @staticmethod
    def _task_requests_external_resume(task: Task) -> bool:
        if is_work_item_runtime_metadata(task.metadata or {}):
            resume_scope_id = str(
                (task.metadata or {}).get("external_resume_session_scope_id", "")
                or ""
            ).strip()
            if not external_resume_allowed_for_scope(task, resume_scope_id=resume_scope_id):
                return False
            return bool(
                str(task.assigned_external_agent or "").strip()
                and (
                    str(task.metadata.get("external_resume_session_id", "") or "").strip()
                    or str(task.metadata.get("delegation_seat_id", "") or "").strip()
                    or str(task.metadata.get("delegation_role_session_id", "") or "").strip()
                )
            )
        if str(task.metadata.get("external_rework_strategy", "") or "").strip() == "resume_if_possible":
            return bool(
                task.metadata.get("gate_rework_request")
                or task.metadata.get("contract_rework_request")
                or task.metadata.get("interrupted_recovery")
                or task.retry_count > 0
            )
        session_id = str(getattr(task, "session_id", "") or "").strip()
        mode = str(task.metadata.get("mode", "") or "").strip().lower()
        task_mode_contract = str(task.metadata.get("task_mode_contract", "") or "").strip()
        return bool(
            str(task.assigned_external_agent or "").strip()
            and session_id
            and (mode == "task" or task_mode_contract == "single_full_capability_main_agent")
        )

    async def _task_runtime_is_live(self, task: Task) -> bool:
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        return self._active_task_run_registry.is_active(project_id, task.id)

    def _describe_interrupted_task_reason(self, task: Task, session: Any | None) -> str:
        projection_id = str(projection_id_for_task(task) or task.title or task.id).strip()
        if not session:
            return (
                f"Execution for work item `{projection_id}` was interrupted while the task was still marked running. "
                "Use `continue` to restart this work item safely."
            )
        status = str(getattr(session, "status", "") or "").strip().lower() or "unknown"
        metadata = dict(getattr(session, "metadata", {}) or {})
        failure_reason = str(metadata.get("failure_reason", "") or "").strip()
        if failure_reason:
            return (
                f"Execution for work item `{projection_id}` was interrupted after the external session ended with "
                f"status `{status}`. Latest note: {failure_reason}"
            )
        if status == "done":
            return (
                f"Execution for work item `{projection_id}` finished in the external agent, but OpenOPC was interrupted "
                "before the work-item result was persisted locally. Use `continue` to rerun it safely."
            )
        if status == "cancelled":
            return (
                f"Execution for work item `{projection_id}` was interrupted because the external agent session was cancelled. "
                "Use `continue` to restart this work item."
            )
        return (
            f"Execution for work item `{projection_id}` was interrupted after the external session moved to `{status}`. "
            "Use `continue` to restart this work item."
        )

    async def _mark_task_interrupted(
        self,
        task: Task,
        *,
        reason: str,
        session: Any | None = None,
    ) -> bool:
        if not self.store:
            return False
        if task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return False
        work_item_id = linked_work_item_id_for_task(task)
        linked_work_item = None
        if work_item_id and hasattr(self.store, "get_delegation_work_item"):
            try:
                linked_work_item = await self.store.get_delegation_work_item(work_item_id)
            except Exception:
                linked_work_item = None
        stable_waiting_phase_values = {
            Phase.AWAITING_MANAGER_REVIEW.value,
            Phase.AWAITING_HUMAN.value,
        }
        linked_phase = getattr(linked_work_item, "phase", None)
        linked_phase_value = str(getattr(linked_phase, "value", linked_phase or "") or "").strip()
        if task.status in _REVIEW_WAITING_STATUSES or linked_phase_value in stable_waiting_phase_values:
            logger.info(
                "Preserving stable waiting task {} during interrupted-task recovery (status={}, work_item_phase={})",
                task.id,
                getattr(task.status, "value", str(task.status)),
                linked_phase_value,
            )
            return False
        previous_status = task.status
        task.status = TaskStatus.FAILED
        task.execution_lock = False
        task.execution_locked_at = None
        existing_result = dict(task.result or {})
        artifacts = dict(existing_result.get("artifacts", {}) or {})
        session_status = str(getattr(session, "status", "") or "").strip()
        session_updated_at = getattr(session, "updated_at", None)
        artifacts.update(
            {
                "interrupted": True,
                "interrupted_detected_at": datetime.now().isoformat(),
                "interrupted_previous_status": getattr(previous_status, "value", str(previous_status)),
                "latest_external_session_status": session_status,
                "latest_external_session_updated_at": session_updated_at.isoformat() if session_updated_at else "",
            }
        )
        existing_result["content"] = str(existing_result.get("content") or reason).strip()
        existing_result["artifacts"] = artifacts
        task.result = existing_result
        task.metadata = dict(task.metadata)
        progress_log = list(task.metadata.get("progress_log", []))
        if not progress_log or progress_log[-1] != reason:
            progress_log.append(reason)
        task.metadata["progress_log"] = progress_log[-20:]
        task.metadata["interrupted_recovery"] = {
            "detected_at": datetime.now().isoformat(),
            "previous_status": getattr(previous_status, "value", str(previous_status)),
            "latest_external_session_status": session_status,
            "latest_external_session_updated_at": session_updated_at.isoformat() if session_updated_at else "",
            "reason": reason,
        }
        await self.store.save_task(task)
        if work_item_id and hasattr(self.store, "update_delegation_work_item"):
            await self.store.update_delegation_work_item(
                work_item_id,
                phase=Phase.PAUSED,
                summary=reason,
                metadata_updates={
                    "interrupted_recovery": dict(task.metadata.get("interrupted_recovery", {}) or {}),
                    "task_status": task.status.value,
                },
            )
        role_session_id = str(task.metadata.get("delegation_role_session_id", "") or "").strip()
        if role_session_id and hasattr(self.store, "update_delegation_role_session"):
            await self.store.update_delegation_role_session(
                role_session_id,
                focused_work_item_id=work_item_id,
                status="blocked",
                metadata_updates={
                    "interrupted_task_id": task.id,
                    "interrupted_detected_at": datetime.now().isoformat(),
                },
            )
        return True

    async def _fail_task_via_phase(
        self,
        task: Task,
        *,
        reason: str,
    ) -> None:
        """Mark a task as FAILED through the phase channel so all projection
        layers (task.status, role_session, in-memory member_session, UI
        column) update atomically. Falls back to direct task.status write
        for tasks that don't have a linked delegation work item (legacy
        plain tasks).

        Callers should set task.metadata / task.result BEFORE calling this;
        this function is the terminal ``save`` step.
        """
        if not self.store:
            return
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id:
            try:
                from opc.layer2_organization.work_item_transition import transition_work_item
                await transition_work_item(
                    self.store, work_item_id,
                    target_phase=Phase.FAILED,
                    reason=reason,
                    release_claim=True,
                )
                # The phase hook chain updated task.status; merge any metadata
                # the caller prepared on the in-memory task object back to the
                # persisted row.
                fresh = await self.store.get_task(task.id)
                if fresh is not None:
                    fresh.metadata = dict(task.metadata or {})
                    fresh.result = dict(task.result or {})
                    await self.store.save_task(fresh)
                return
            except Exception:
                logger.opt(exception=True).warning(
                    f"_fail_task_via_phase: transition failed for {task.id}, falling back to direct status write"
                )
        # Legacy fallback: no work item → direct status write (no cascade).
        task.status = TaskStatus.FAILED
        await self.store.save_task(task)

    @staticmethod
    def _is_company_feedback_waiting_task(task: Task) -> bool:
        if task.status != TaskStatus.AWAITING_HUMAN:
            return False
        metadata = dict(getattr(task, "metadata", {}) or {})
        if OPCEngine._metadata_flag_true(metadata.get("self_evolution_review_completed", False)):
            return False
        turn_kind = str(
            metadata.get("work_kind")
            or metadata.get("delegation_turn_kind")
            or metadata.get("work_item_turn_type")
            or ""
        ).strip().lower()
        if turn_kind in {"deliver", "delivery"}:
            return True
        return bool(str(metadata.get("feedback_scope", "") or "").strip())

    async def _preserve_stable_waiting_task_after_restart(
        self,
        task: Task,
        *,
        reason: str,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> bool:
        """Keep durable review/human waiting states intact during startup.

        Review and human-feedback waits are stable states.  A missing checkpoint
        after process restart means the UI card may need recovery, not that the
        work item should be failed/paused like a dead running process.
        """
        if not self.store:
            return False
        task.metadata = dict(task.metadata or {})
        status_value = getattr(task.status, "value", str(task.status))
        existing_preserved = dict(
            task.metadata.get("startup_reconcile_preserved_waiting_state", {})
            or {}
        )
        marker_changed = (
            str(existing_preserved.get("status", "") or "") != status_value
            or str(existing_preserved.get("reason", "") or "") != reason
        )
        if marker_changed:
            task.metadata["startup_reconcile_preserved_waiting_state"] = {
                "detected_at": datetime.now().isoformat(),
                "status": status_value,
                "reason": reason,
            }
            await self.store.save_task(task)
        restored_checkpoint = False
        if self._is_company_feedback_waiting_task(task):
            try:
                await self._save_company_feedback_followup_checkpoint(task, tasks, plan)
                restored_checkpoint = True
            except Exception:
                logger.opt(exception=True).debug(
                    "Best-effort restore of human feedback checkpoint failed for task {}",
                    task.id,
                )
        if restored_checkpoint:
            task.metadata = dict(task.metadata or {})
            preserved = dict(task.metadata.get("startup_reconcile_preserved_waiting_state", {}) or {})
            if not str(preserved.get("restored_checkpoint_type", "") or "").strip():
                preserved["restored_checkpoint_type"] = "company_delivery_feedback"
                preserved["restored_checkpoint_at"] = datetime.now().isoformat()
                task.metadata["startup_reconcile_preserved_waiting_state"] = preserved
                await self.store.save_task(task)
                marker_changed = True
        return marker_changed or restored_checkpoint

    def _company_runtime_plan_for_tasks(
        self,
        tasks: list[Task],
    ) -> CompanyWorkItemRuntimePlan:
        for task in sorted(tasks, key=lambda item: (item.created_at, item.id), reverse=True):
            candidate = serialized_company_plan_from_metadata(task.metadata)
            if candidate:
                return deserialize_company_work_item_runtime_plan(candidate)
        sample = tasks[0]
        return CompanyWorkItemRuntimePlan(
            profile=str(
                (sample.metadata or {}).get("company_profile", "")
                or getattr(self.config.org, "company_profile", "corporate")
            ).strip()
            or "corporate",
            metadata={
                "execution_model": str(
                    (sample.metadata or {}).get("execution_model", "") or "multi_team_org"
                ).strip()
                or "multi_team_org",
                "work_item_driven": True,
            },
        )

    async def _normalize_legacy_company_interruption(
        self,
        task: Task,
    ) -> dict[str, Any] | None:
        """Convert the old FAILED+marker patch into checkpoint-ready state.

        A work item whose authoritative phase is genuinely terminal is never
        revived.  The old recovery marker, interrupted result artifact, or a
        PAUSED work-item phase is accepted only as evidence that FAILED was a
        crash projection rather than a business outcome.
        """
        if not self.store or task.status != TaskStatus.FAILED:
            return None
        metadata = dict(task.metadata or {})
        marker = metadata.get("interrupted_recovery")
        marker = dict(marker) if isinstance(marker, dict) else {}
        result = dict(task.result or {})
        artifacts = dict(result.get("artifacts", {}) or {})
        work_item_id = linked_work_item_id_for_task(task)
        work_item = None
        if work_item_id:
            getter = getattr(self.store, "get_delegation_work_item", None)
            if callable(getter):
                try:
                    work_item = await getter(work_item_id)
                except Exception:
                    work_item = None
        phase = getattr(work_item, "phase", None)
        if phase in {Phase.APPROVED, Phase.FAILED, Phase.CANCELLED}:
            return None
        previous_status_value = str(
            marker.get("previous_status")
            or artifacts.get("interrupted_previous_status")
            or ""
        ).strip()
        interrupted_artifact = bool(artifacts.get("interrupted", False))
        paused_work_item = phase == Phase.PAUSED
        if not previous_status_value and not interrupted_artifact and not paused_work_item:
            return None

        previous_status = None
        if previous_status_value:
            try:
                candidate = TaskStatus(previous_status_value)
            except ValueError:
                candidate = None
            if candidate not in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                previous_status = candidate
        if paused_work_item:
            target_status = task_status_for_phase(Phase.PAUSED)
        else:
            target_status = previous_status or TaskStatus.PENDING

        provenance = {
            "task_id": task.id,
            "work_item_id": work_item_id or "",
            "legacy_failed_status": TaskStatus.FAILED.value,
            "restored_status": target_status.value,
            "work_item_phase": phase.value if isinstance(phase, Phase) else str(phase or ""),
            "interrupted_recovery": marker,
            "interrupted_artifacts": {
                key: artifacts.get(key)
                for key in (
                    "interrupted",
                    "interrupted_detected_at",
                    "interrupted_previous_status",
                    "latest_external_session_status",
                    "latest_external_session_updated_at",
                )
                if key in artifacts
            },
        }
        task.status = target_status
        task.execution_lock = False
        task.execution_locked_at = None
        return provenance

    async def _clear_migrated_company_interruption_markers(
        self,
        tasks: list[Task],
        migrated_task_ids: set[str],
    ) -> None:
        if not self.store or not migrated_task_ids:
            return
        artifact_keys = {
            "interrupted",
            "interrupted_detected_at",
            "interrupted_previous_status",
            "latest_external_session_status",
            "latest_external_session_updated_at",
        }
        for task in tasks:
            if task.id not in migrated_task_ids:
                continue
            task.metadata = dict(task.metadata or {})
            task.metadata.pop("interrupted_recovery", None)
            if task.result:
                task.result = dict(task.result)
                artifacts = dict(task.result.get("artifacts", {}) or {})
                for key in artifact_keys:
                    artifacts.pop(key, None)
                task.result["artifacts"] = artifacts
            await self.store.save_task(task)

    async def _reconcile_company_runtime_state(
        self,
        parent_session_id: str,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> int:
        if not self.store:
            return 0
        project_id = self.project_id or "default"
        pending_checkpoints = await self.store.get_pending_checkpoints(project_id=project_id)
        legacy_provenance: list[dict[str, Any]] = []
        for task in tasks:
            provenance = await self._normalize_legacy_company_interruption(task)
            if provenance is not None:
                legacy_provenance.append(provenance)
        migrated_task_ids = {
            str(item.get("task_id", "") or "").strip()
            for item in legacy_provenance
            if str(item.get("task_id", "") or "").strip()
        }
        normalize_active = getattr(
            self.store,
            "normalize_active_execution_checkpoints",
            None,
        )
        if callable(normalize_active):
            pending_suspend_checkpoint = await normalize_active(
                project_id=project_id,
                session_id=parent_session_id,
                checkpoint_types=_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES,
            )
        else:
            pending_suspend_checkpoint = await self.get_active_company_runtime_suspend_checkpoint(
                parent_session_id
            )
        if pending_suspend_checkpoint is not None:
            payload = dict(pending_suspend_checkpoint.payload or {})
            checkpoint_status = str(getattr(pending_suspend_checkpoint, "status", "") or "").strip()
            if checkpoint_status == "resuming" or legacy_provenance:
                payload_updates: dict[str, Any] = {}
                if checkpoint_status == "resuming":
                    payload_updates.update({
                        "resume_state": "interrupted",
                        "resume_interrupted_at": datetime.now().isoformat(),
                    })
                if legacy_provenance:
                    payload_updates["legacy_interrupted_recovery"] = legacy_provenance
                transitioned = await self._mark_company_runtime_checkpoint_status(
                    pending_suspend_checkpoint,
                    status="pending",
                    payload_updates=payload_updates,
                    expected_statuses={checkpoint_status},
                )
                if not transitioned:
                    return 0
                payload = dict(pending_suspend_checkpoint.payload or {})
            await self._clear_migrated_company_interruption_markers(tasks, migrated_task_ids)
            affected = await self._suspend_company_runtime_tasks(
                tasks,
                reason=str(payload.get("reason", "") or "startup_recovery"),
                checkpoint_type=str(pending_suspend_checkpoint.checkpoint_type or "company_runtime_interrupted"),
                stop_intent_id=str(payload.get("stop_intent_id", "") or ""),
            )
            return len(affected)
        visible_session_ids = {
            parent_session_id,
            *{
                str(getattr(task, "session_id", "") or "").strip()
                for task in tasks
                if str(getattr(task, "session_id", "") or "").strip()
            },
        }
        group_task_ids = {str(task.id or "").strip() for task in tasks}
        checkpoint_task_ids = {
            str(
                checkpoint.task_id
                or checkpoint.payload.get("waiting_task_id")
                or checkpoint.payload.get("task_id")
                or ""
            ).strip()
            for checkpoint in pending_checkpoints
            if (
                str(checkpoint.session_id or "").strip() in visible_session_ids
                or str(
                    checkpoint.task_id
                    or checkpoint.payload.get("waiting_task_id")
                    or checkpoint.payload.get("task_id")
                    or ""
                ).strip() in group_task_ids
            )
        }
        updated = 0
        interrupted_tasks: list[Task] = []
        get_work_item = getattr(
            self.store,
            "get_delegation_work_item",
            None,
        )
        for task in tasks:
            task_metadata = dict(getattr(task, "metadata", {}) or {})
            if (
                str(task_metadata.get("dispatch_hold", "") or "").strip() == "company_runtime_suspended"
                or str(task_metadata.get("company_runtime_stop_state", "") or "").strip() in {"suspending", "suspended"}
            ):
                interrupted_tasks.append(task)
                continue
            work_item_id = linked_work_item_id_for_task(task)
            work_item = (
                await get_work_item(work_item_id)
                if work_item_id and callable(get_work_item)
                else None
            )
            work_item_phase = getattr(work_item, "phase", None)
            # A linked WorkItem is the company workflow state.  Task.status is
            # only its UI/execution projection and may lag on either side of a
            # crash.  Fall back to Task status only for legacy envelopes that
            # have no durable WorkItem.
            stable_review_wait = (
                work_item_phase in IN_REVIEW_PHASES
                if work_item is not None
                else task.status in _REVIEW_WAITING_STATUSES
            )
            if stable_review_wait:
                if task.id not in checkpoint_task_ids:
                    waiting_label = (
                        work_item_phase.value
                        if isinstance(work_item_phase, Phase)
                        else task.status.value
                    )
                    reason = (
                        f"Work item `{projection_id_for_task(task) or task.title}` was left in "
                        f"`{waiting_label}` but no pending checkpoint could be found after restart. "
                        "Preserving stable waiting state."
                    )
                    if await self._preserve_stable_waiting_task_after_restart(
                        task,
                        reason=reason,
                        plan=plan,
                        tasks=tasks,
                    ):
                        updated += 1
                continue
            if task.id in checkpoint_task_ids and (
                task.status in _WAITING_TASK_STATUSES
                or work_item_phase == Phase.WAITING_FOR_PEER
            ):
                # A durable peer/specialized wait already owns this Task.
                # Startup must not layer a company-runtime interruption over
                # that independent waiting checkpoint.
                continue
            if work_item is not None and work_item_phase not in DONE_PHASES:
                # A controller coroutine is required to dispatch READY work,
                # revisit dependency waits, and finalize RUNNING work.  After
                # process start the registry is empty, so every such WorkItem
                # is interrupted even when its Task projection is still
                # PENDING/BLOCKED or was prematurely terminalized.
                interrupted_tasks.append(task)
                continue
            if task.status == TaskStatus.RUNNING and not await self._task_runtime_is_live(task):
                interrupted_tasks.append(task)
                continue
            if task.status in _WAITING_TASK_STATUSES and task.id not in checkpoint_task_ids:
                interrupted_tasks.append(task)
        if legacy_provenance:
            interrupted_tasks.extend(
                task for task in tasks if task.id in migrated_task_ids
            )
        if interrupted_tasks:
            interrupted_task_ids = list(dict.fromkeys(task.id for task in interrupted_tasks))
            task_by_id = {task.id: task for task in tasks}
            interrupted_task_objects = [task_by_id[task_id] for task_id in interrupted_task_ids]
            origin_task_id = str(getattr(interrupted_task_objects[0], "id", "") or "").strip()
            checkpoint, _created = await self._save_company_runtime_suspend_checkpoint(
                checkpoint_type="company_runtime_interrupted",
                reason="startup_recovery",
                parent_session_id=parent_session_id,
                origin_task_id=origin_task_id or None,
                plan=plan,
                tasks=tasks,
                payload_updates=(
                    {"legacy_interrupted_recovery": legacy_provenance}
                    if legacy_provenance
                    else None
                ),
            )
            if (
                legacy_provenance
                and "legacy_interrupted_recovery"
                not in dict(checkpoint.payload or {})
            ):
                transitioned = await self._mark_company_runtime_checkpoint_status(
                    checkpoint,
                    status="pending",
                    payload_updates={
                        "legacy_interrupted_recovery": legacy_provenance,
                        "resume_state": "interrupted",
                        "resume_interrupted_at": datetime.now().isoformat(),
                    },
                    expected_statuses={"pending", "resuming"},
                )
                if not transitioned:
                    return updated
            await self._clear_migrated_company_interruption_markers(tasks, migrated_task_ids)
            checkpoint_payload = dict(checkpoint.payload or {})
            affected = await self._suspend_company_runtime_tasks(
                tasks,
                reason=str(checkpoint_payload.get("reason", "") or "startup_recovery"),
                checkpoint_type=str(
                    checkpoint.checkpoint_type or "company_runtime_interrupted"
                ),
                stop_intent_id=str(
                    checkpoint_payload.get("stop_intent_id", "") or ""
                ),
            )
            updated += len(affected) or len(interrupted_task_objects)
        return updated

    async def _reconcile_interrupted_project_tasks(self) -> int:
        if not self.store:
            return 0
        project_id = self.project_id or "default"
        tasks = await self.store.get_tasks(project_id=project_id)
        if not tasks:
            return 0

        checkpoint_getter = getattr(self.store, "get_execution_checkpoints", None)
        active_company_checkpoints = (
            await checkpoint_getter(
                project_id=project_id,
                checkpoint_types=list(_COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES),
                statuses=["pending", "resuming"],
            )
            if callable(checkpoint_getter)
            else []
        )
        identity_index = build_company_runtime_identity_index(
            tasks,
            active_company_checkpoints,
        )
        task_by_id = {task.id: task for task in tasks}
        runtime_groups: dict[str, list[Task]] = {}
        runtime_task_ids: set[str] = set()
        ui_anchor_task_ids: set[str] = set()
        for identity in identity_index.identities:
            if identity.ui_anchor_task_id:
                ui_anchor_task_ids.add(identity.ui_anchor_task_id)
            group = [
                task_by_id[task_id]
                for task_id in identity.runtime_task_ids
                if task_id != identity.ui_anchor_task_id
                and task_id in task_by_id
                and is_company_runtime_task(task_by_id[task_id])
            ]
            if group:
                runtime_groups[identity.runtime_session_id] = group
                runtime_task_ids.update(task.id for task in group)

        updated = 0
        for anchor_task_id in ui_anchor_task_ids:
            anchor = task_by_id.get(anchor_task_id)
            if anchor is not None and anchor.status == TaskStatus.RUNNING:
                if await self._clear_stale_company_session_anchor(anchor):
                    updated += 1

        for task in tasks:
            if task.id in runtime_task_ids or task.id in ui_anchor_task_ids:
                continue
            task_metadata = dict(getattr(task, "metadata", {}) or {})
            if (
                str(task_metadata.get("dispatch_hold", "") or "").strip() == "company_runtime_suspended"
                or str(task_metadata.get("company_runtime_stop_state", "") or "").strip() in {"suspending", "suspended"}
            ):
                continue
            if is_company_runtime_task(task):
                continue
            if self._is_company_primary_session_anchor_task(task):
                if task.status == TaskStatus.RUNNING:
                    if await self._clear_stale_company_session_anchor(task):
                        updated += 1
                continue
            if task.status == TaskStatus.RUNNING and not await self._task_runtime_is_live(task):
                session = await self._load_latest_external_session_for_task(task)
                reason = self._describe_interrupted_task_reason(task, session)
                if await self._mark_task_interrupted(task, reason=reason, session=session):
                    updated += 1

        for parent_session_id, group in runtime_groups.items():
            async with self._active_task_run_registry.scope_lock(
                project_id,
                parent_session_id,
            ):
                updated += await self._reconcile_company_runtime_state(
                    parent_session_id,
                    self._company_runtime_plan_for_tasks(group),
                    group,
                )
        return updated

    async def prepare_active_company_runtimes_for_shutdown(
        self,
        *,
        _controller_shutdown: bool = False,
    ) -> list[dict[str, Any]]:
        """Persist checkpoints for company attempts owned by this controller.

        Callers invoke this before cancelling in-flight session coroutines.  It
        is safe to call repeatedly: an existing suspended/interrupted
        checkpoint is reused and never replaced.
        """
        self._shutting_down = True
        if not self._owns_active_task_run_registry and not _controller_shutdown:
            # Project delegates and CustomRuntimeRunner engines borrow the
            # controller registry.  Their local teardown must not close
            # controller-wide admission or checkpoint unrelated attempts.
            return []
        if self._owns_active_task_run_registry:
            self._active_task_run_registry.close_admission()
        project_id = str(self.project_id or "default").strip() or "default"
        active_task_ids = self._active_task_run_registry.active_task_ids(project_id)
        delegate_prepared: list[dict[str, Any]] = []
        for delegate in list(self._project_engine_delegates.values()):
            delegate_prepared.extend(
                await delegate.prepare_active_company_runtimes_for_shutdown(
                    _controller_shutdown=True,
                )
            )
        if not self.store or not bool(getattr(self.store, "is_ready", True)):
            return delegate_prepared
        if not active_task_ids:
            return delegate_prepared
        identity_index = await load_company_runtime_identity_index(
            self.store,
            project_id,
        )
        active_by_scope: dict[str, list[Task]] = {}
        for task_id in active_task_ids:
            identity = identity_index.resolve(task_id=task_id)
            task = identity_index.task(task_id)
            if (
                identity is None
                or task is None
                or task_id == identity.ui_anchor_task_id
                or not is_company_runtime_task(task)
            ):
                continue
            active_by_scope.setdefault(identity.runtime_session_id, []).append(task)

        prepared: list[dict[str, Any]] = delegate_prepared
        for parent_session_id, active_scope_tasks in active_by_scope.items():
            async with self._active_task_run_registry.scope_lock(
                project_id,
                parent_session_id,
            ):
                snapshot = await self._load_company_runtime_snapshot(parent_session_id)
                if snapshot is None:
                    tasks = active_scope_tasks
                    plan = self._company_runtime_plan_for_tasks(tasks)
                else:
                    plan, tasks = snapshot
                origin_task_id = str(active_scope_tasks[0].id or "").strip() or None
                checkpoint, created, affected_task_ids = (
                    await self._checkpoint_and_suspend_company_runtime_scope(
                        checkpoint_type="company_runtime_interrupted",
                        reason="service_shutdown",
                        parent_session_id=parent_session_id,
                        origin_task_id=origin_task_id,
                        plan=plan,
                        tasks=tasks,
                    )
                )
                idempotent = not created
                prepared.append(
                    {
                        "session_id": parent_session_id,
                        "checkpoint_id": checkpoint.checkpoint_id,
                        "checkpoint_type": checkpoint.checkpoint_type,
                        "task_ids": affected_task_ids,
                        "idempotent": idempotent,
                    }
                )
        return prepared

    @staticmethod
    def _is_company_primary_session_anchor_task(task: Task) -> bool:
        """Return True for the user-facing chat task that anchors a company run.

        The anchor is not a business work item. It may be set to RUNNING while
        a user message is being routed, but actual company progress/failure is
        represented by child work-item tasks and DelegationWorkItem phases.
        """
        metadata = dict(getattr(task, "metadata", {}) or {})
        if str(metadata.get("work_item_projection_id", "") or "").strip():
            return False
        if linked_work_item_id_for_task(task):
            return False
        if str(getattr(task, "parent_session_id", "") or "").strip():
            return False
        if str(getattr(task, "parent_id", "") or "").strip():
            return False
        exec_mode = str(metadata.get("exec_mode", "") or metadata.get("mode", "") or "").strip().lower()
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        return exec_mode in {"company", "org", "custom"} or execution_mode == ExecutionMode.COMPANY_MODE.value

    async def _clear_stale_company_session_anchor(self, task: Task) -> bool:
        if not self.store:
            return False
        fresh = await self.store.get_task(task.id)
        target = fresh or task
        if target.status != TaskStatus.RUNNING:
            return False
        target.status = TaskStatus.IDLE
        target.execution_lock = False
        target.execution_locked_at = None
        target.metadata = dict(target.metadata or {})
        progress = list(target.metadata.get("progress_log", []) or [])
        message = "Recovered stale company session routing state after startup; work-item runtime state was left intact."
        if not progress or progress[-1] != message:
            progress.append(message)
        target.metadata["progress_log"] = progress[-20:]
        await self.store.save_task(target)
        logger.info(
            "Recovered stale company session anchor {} for project {} without marking it failed",
            target.id,
            target.project_id or self.project_id or "default",
        )
        return True

    @staticmethod
    def _format_company_runtime_snapshot(
        tasks: list[Task],
        *,
        heading: str = "## Latest Runtime Snapshot",
        annotations: dict[str, str] | None = None,
    ) -> str:
        lines = [heading]
        notes = dict(annotations or {})
        for task in tasks:
            projection_id = str(projection_id_for_task(task) or task.title or task.id).strip()
            title = str(task.title or projection_id).strip() or projection_id
            line = f"- `{projection_id}` ({title}): {task.status.value}"
            note = str(notes.get(projection_id, "") or "").strip()
            if note:
                line += f" [{note}]"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _company_followup_turn_priority(task: Task) -> int:
        turn_type = turn_type_for_task(task, fallback="")
        return {
            "intake": 0,
            "dispatch": 1,
            "plan": 2,
            "deliver": 3,
            "aggregate": 4,
            "review": 5,
            "execute": 6,
        }.get(turn_type, 7)

    @staticmethod
    def _company_followup_status_priority(task: Task) -> int:
        status = task.status
        turn_type = turn_type_for_task(task, fallback="")
        if status == TaskStatus.PENDING:
            return 0
        if status == TaskStatus.DONE and turn_type in {"intake", "dispatch", "plan"}:
            return 1
        if status in _WAITING_TASK_STATUSES:
            return 2
        if status == TaskStatus.BLOCKED:
            return 3
        if status == TaskStatus.DONE:
            return 4
        if status == TaskStatus.FAILED:
            return 5
        if status == TaskStatus.RUNNING:
            return 6
        if status == TaskStatus.CANCELLED:
            return 7
        return 8

    @staticmethod
    def _metadata_flag_true(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    _FINAL_DELIVERY_CURRENT_OUTPUT_KEYS = {
        "delivery_package",
        "final_delivery_package",
        "feedback_followup_message",
        "ceo_pre_delivery_assessment",
        "pre_delivery_assessment_status",
        "pre_delivery_assessment_failure_kind",
        "pre_delivery_rework_cap_reached",
        "pre_delivery_rework_cap",
        "feedback_close_user_message",
    }

    @classmethod
    def _clear_current_final_delivery_outputs(cls, task: Task) -> None:
        """Clear only the current delivery cache before a new owner revision."""
        task.metadata = dict(getattr(task, "metadata", {}) or {})
        task.context_snapshot = dict(getattr(task, "context_snapshot", {}) or {})
        for key in cls._FINAL_DELIVERY_CURRENT_OUTPUT_KEYS:
            task.metadata.pop(key, None)
            task.context_snapshot.pop(key, None)
        owned_outputs = dict(task.context_snapshot.get("work_item_owned_outputs", {}) or {})
        for key in cls._FINAL_DELIVERY_CURRENT_OUTPUT_KEYS:
            owned_outputs.pop(key, None)
        if owned_outputs:
            task.context_snapshot["work_item_owned_outputs"] = owned_outputs
        else:
            task.context_snapshot.pop("work_item_owned_outputs", None)

    @classmethod
    def _is_open_final_delivery_review_task(cls, task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        if cls._metadata_flag_true(metadata.get("feedback_closed", False)):
            return False
        if cls._metadata_flag_true(metadata.get("human_review_closed", False)):
            return False
        if cls._metadata_flag_true(metadata.get("self_evolution_review_completed", False)):
            return False
        if cls._metadata_flag_true(metadata.get("feedback_superseded", False)):
            return False
        if task.status in {TaskStatus.CANCELLED, TaskStatus.FAILED}:
            return False
        if str(metadata.get("execution_mode", "") or "").strip() != ExecutionMode.COMPANY_MODE.value:
            return False
        if str(metadata.get("feedback_scope", "") or "").strip().lower() != "final":
            return False
        if not cls._metadata_flag_true(metadata.get("authoritative_output", False)):
            return False
        if not cls._metadata_flag_true(metadata.get("user_visible", False)):
            return False
        if not cls._metadata_flag_true(metadata.get("requires_user_feedback", False)):
            return False
        return turn_type_for_task(task, fallback="") == "deliver"

    @staticmethod
    def _final_delivery_followup_status_priority(task: Task) -> int:
        return {
            TaskStatus.AWAITING_HUMAN: 0,
            TaskStatus.AWAITING_REVIEW: 1,
            TaskStatus.AWAITING_MANAGER_REVIEW: 2,
            TaskStatus.PENDING: 3,
            TaskStatus.BLOCKED: 4,
            TaskStatus.DONE: 5,
            TaskStatus.RUNNING: 6,
            TaskStatus.CANCELLED: 7,
            TaskStatus.FAILED: 8,
        }.get(task.status, 9)

    def _company_followup_target_task(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> Task | None:
        final_delivery_candidates = [
            task for task in tasks
            if self._is_open_final_delivery_review_task(task)
        ]
        if final_delivery_candidates:
            return sorted(
                final_delivery_candidates,
                key=lambda task: (
                    self._final_delivery_followup_status_priority(task),
                    -float(task.created_at.timestamp()),
                    str(task.id),
                ),
            )[0]

        final_decider_role_id = str(
            getattr(plan, "final_decider_role_id", "")
            or plan.metadata.get("final_decider_role_id", "")
            or ""
        ).strip()
        if not final_decider_role_id and self.org_engine:
            getter = getattr(self.org_engine, "get_final_decider_role_id", None)
            if callable(getter):
                try:
                    final_decider_role_id = str(getter(strict=False) or "").strip()
                except TypeError:
                    final_decider_role_id = str(getter() or "").strip()
        if not final_decider_role_id:
            top_level_role_ids = [
                str(item).strip()
                for item in list(getattr(plan, "top_level_role_ids", []) or plan.metadata.get("top_level_role_ids", []) or [])
                if str(item).strip()
            ]
            if len(top_level_role_ids) == 1:
                final_decider_role_id = top_level_role_ids[0]
        if not final_decider_role_id:
            fallback_candidates = [
                task
                for task in tasks
                if bool(dict(getattr(task, "metadata", {}) or {}).get("authoritative_output", False))
                or str(dict(getattr(task, "metadata", {}) or {}).get("feedback_scope", "") or "").strip() == "final"
                or turn_type_for_task(task, fallback="") == "deliver"
            ]
            if not fallback_candidates:
                return None
            return sorted(
                fallback_candidates,
                key=lambda task: (
                    self._company_followup_status_priority(task),
                    self._company_followup_turn_priority(task),
                    -float(task.created_at.timestamp()),
                    str(task.id),
                ),
            )[0]
        candidates = [
            task
            for task in tasks
            if str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip() == final_decider_role_id
        ]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda task: (
                self._company_followup_status_priority(task),
                self._company_followup_turn_priority(task),
                -float(task.created_at.timestamp()),
                str(task.id),
            ),
        )[0]

    async def _prepare_company_followup_target(
        self,
        task: Task,
        user_reply: str,
        *,
        resume_source: str = "primary_session_followup",
        context_updates: dict[str, Any] | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        assert self.store
        reply = str(user_reply or "").strip()
        task.context_snapshot = dict(task.context_snapshot or {})
        task.context_snapshot["user_supplied_input"] = reply
        if not isinstance(task.context_snapshot.get("runtime_resume"), dict):
            task.context_snapshot.pop("runtime_resume", None)
        task.context_snapshot["skip_session_history"] = True
        task.context_snapshot["current_turn_mode"] = "dispatch_required"
        if context_updates:
            task.context_snapshot.update(dict(context_updates))
        task.status = TaskStatus.PENDING
        task.result = None
        task.execution_lock = False
        task.execution_locked_at = None
        task.metadata = dict(task.metadata or {})
        final_delivery_followup = self._is_open_final_delivery_review_task(task)
        next_delivery_revision: int | None = None
        if final_delivery_followup:
            current_revisions: list[int] = []
            for source in (task.metadata, task.context_snapshot):
                for key in ("delivery_revision", "owner_directive_revision"):
                    try:
                        current_revisions.append(int(dict(source or {}).get(key, 0) or 0))
                    except (TypeError, ValueError):
                        pass
            next_delivery_revision = max(current_revisions or [0]) + 1
            self._clear_current_final_delivery_outputs(task)
            task.context_snapshot["delivery_revision"] = next_delivery_revision
            task.context_snapshot["owner_directive_revision"] = next_delivery_revision
        task.metadata["followup_routed_to_final_decider"] = True
        task.metadata["current_turn_mode"] = "dispatch_required"
        if final_delivery_followup:
            task.metadata.update({
                "work_kind": "delivery",
                "delegation_turn_kind": "delivery",
                "work_item_turn_type": "deliver",
                "review_owner_kind": "human",
                "feedback_scope": "final",
                "requires_user_feedback": True,
                "authoritative_output": True,
                "user_visible": True,
            })
            if next_delivery_revision is not None:
                task.metadata["delivery_revision"] = next_delivery_revision
                task.metadata["owner_directive_revision"] = next_delivery_revision
        else:
            task.metadata["delegation_turn_kind"] = "dispatch"
        if reply:
            task.metadata["latest_user_directive"] = reply
            task.metadata["manager_mutation_user_input"] = reply
            task.metadata["user_supplied_input"] = reply
            if final_delivery_followup:
                task.context_snapshot["latest_user_directive"] = reply
        if metadata_updates:
            task.metadata.update(dict(metadata_updates))
        task.metadata.pop("delegation_pending_work_item_ids", None)
        task.metadata.pop("delegated_children_pending", None)
        task.metadata.pop("delegation_wait_for_work_item_ids", None)
        task.metadata = reset_manager_dispatch_turn_metadata(task.metadata)
        progress = list(task.metadata.get("progress_log", []) or [])
        progress.append(f"Company follow-up routed to final decider ({resume_source}): {reply}")
        task.metadata["progress_log"] = progress[-20:]
        await self.store.save_task(task)

        work_item_id = linked_work_item_id_for_task(task)
        work_item = None
        if not work_item_id:
            get_work_item_for_task = getattr(self.store, "get_work_item_for_runtime_task", None)
            if callable(get_work_item_for_task):
                try:
                    work_item = await get_work_item_for_task(task.id)
                except Exception:
                    work_item = None
                if work_item is not None:
                    work_item_id = str(getattr(work_item, "work_item_id", "") or "").strip()
        if work_item_id and hasattr(self.store, "update_delegation_work_item"):
            target_phase = Phase.READY
            get_work_item = getattr(self.store, "get_delegation_work_item", None)
            reopen_approved = getattr(self.store, "reopen_approved_delegation_work_item_for_rework", None)
            if work_item is None and callable(get_work_item):
                try:
                    work_item = await get_work_item(work_item_id)
                    current_phase = getattr(work_item, "phase", None)
                    if not isinstance(current_phase, Phase):
                        current_phase = Phase(str(current_phase or ""))
                    if current_phase in {Phase.AWAITING_HUMAN, Phase.AWAITING_MANAGER_REVIEW}:
                        target_phase = Phase.READY_FOR_REWORK
                except Exception:
                    target_phase = Phase.READY
            resume_metadata = {
                "resume_requested_at": datetime.now().isoformat(),
                "resume_user_reply": reply,
                "resume_source": resume_source,
                "current_turn_mode": "dispatch_required",
                "delegation_turn_kind": "dispatch",
                "followup_routed_to_final_decider": True,
                "followup_attention": "user_supplied_input",
                "latest_user_directive": reply,
                "manager_mutation_user_input": reply,
                "user_supplied_input": reply,
                "dependency_gate_bypass_reason": "final_decider_followup",
                "delegated_children_pending": False,
                "delegation_wait_for_work_item_ids": [],
                "waiting_on_work_item_ids": [],
            }
            if final_delivery_followup:
                resume_metadata.update({
                    "work_kind": "delivery",
                    "delegation_turn_kind": "delivery",
                    "work_item_turn_type": "deliver",
                    "review_owner_kind": "human",
                    "feedback_scope": "final",
                    "requires_user_feedback": True,
                    "authoritative_output": True,
                    "user_visible": True,
                })
                if next_delivery_revision is not None:
                    resume_metadata["delivery_revision"] = next_delivery_revision
                    resume_metadata["owner_directive_revision"] = next_delivery_revision
            if metadata_updates:
                resume_metadata.update(dict(metadata_updates))
            metadata_unset = (
                sorted(self._FINAL_DELIVERY_CURRENT_OUTPUT_KEYS)
                if final_delivery_followup
                else None
            )
            current_phase = getattr(work_item, "phase", None) if work_item is not None else None
            if current_phase == Phase.APPROVED and callable(reopen_approved):
                await reopen_approved(
                    work_item_id,
                    target_phase=Phase.READY_FOR_REWORK,
                    summary=None,
                    deliverable_summary="",
                    blocked_reason="",
                    metadata_updates=resume_metadata,
                    metadata_unset=metadata_unset,
                    release_claim=True,
                )
            else:
                await self.store.update_delegation_work_item(
                    work_item_id,
                    phase=target_phase,
                    summary=None,
                    metadata_updates=resume_metadata,
                    metadata_unset=metadata_unset,
                    claimed_by_role_runtime_session_id="",
                    claimed_by_seat_id="",
                )
        role_session_id = str(task.metadata.get("delegation_role_session_id", "") or "").strip()
        if role_session_id and hasattr(self.store, "update_delegation_role_session"):
            await self.store.update_delegation_role_session(
                role_session_id,
                focused_work_item_id="",
                current_work_item={},
                status="idle",
                metadata_updates={
                    "resume_requested_at": datetime.now().isoformat(),
                    "resume_user_reply": reply,
                    "resume_source": resume_source,
                },
            )
        seat_state_id = str(task.metadata.get("seat_state_id") or task.metadata.get("delegation_seat_state_id") or "").strip()
        if seat_state_id and hasattr(self.store, "update_seat_state"):
            await self.store.update_seat_state(
                seat_state_id,
                current_task_id="",
                current_work_item_id="",
                current_work_item={},
                status="idle",
                resident_status="idle",
                metadata_updates={
                    "resume_requested_at": datetime.now().isoformat(),
                    "resume_user_reply": reply,
                    "resume_source": resume_source,
                    "current_turn_mode": "dispatch_required",
                },
            )

    async def _resume_company_runtime_via_final_decider(
        self,
        *,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        user_reply: str,
        session_id: str | None,
        resume_source: str = "primary_session_followup",
        context_updates: dict[str, Any] | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> str | None:
        assert self.company_executor
        reply = str(user_reply or "").strip()
        if not reply:
            return None
        target_task = self._company_followup_target_task(plan, tasks)
        if target_task is None:
            return None
        projection_label = projection_id_for_task(target_task) or str(target_task.title or target_task.id).strip()
        projection_title = str(target_task.title or projection_label).strip() or projection_label
        snapshot = self._format_company_runtime_snapshot(
            tasks,
            heading="## Latest Runtime Snapshot (before follow-up)",
            annotations={projection_label: "final decider follow-up"},
        )
        await self._prepare_company_followup_target(
            target_task,
            reply,
            resume_source=resume_source,
            context_updates=context_updates,
            metadata_updates=metadata_updates,
        )
        if self.on_company_runtime_children and session_id and tasks:
            self.on_company_runtime_children(session_id, [t.id for t in tasks])
        result = await self.company_executor.execute(plan, tasks)
        refreshed_target = target_task
        if self.store:
            try:
                refreshed = await self.store.get_task(target_task.id)
                if refreshed is not None:
                    refreshed_target = refreshed
            except Exception:
                refreshed_target = target_task
        target_metadata = dict(getattr(refreshed_target, "metadata", {}) or {})
        close_message = str(target_metadata.get("feedback_close_user_message", "") or "").strip()
        if close_message:
            return close_message
        if bool(target_metadata.get("feedback_closed", False)):
            return "The human review has been closed by the final decider."
        if self.store and session_id:
            refreshed_snapshot = await self._load_company_runtime_snapshot(session_id)
            if refreshed_snapshot is not None:
                refreshed_plan, refreshed_tasks = refreshed_snapshot
                await self._ensure_open_final_delivery_review_checkpoints(
                    refreshed_plan,
                    refreshed_tasks,
                )
        if (
            bool(target_metadata.get("manager_board_mutation_performed", False))
            or [
                str(item).strip()
                for item in list(target_metadata.get("delegation_wait_for_work_item_ids", []) or [])
                if str(item).strip()
            ]
        ):
            return (
                f"Routed the latest user follow-up to `{projection_label}` ({projection_title}) "
                "and resumed the existing company runtime. The updated work item board is continuing through the normal runtime."
                f"\n\n{snapshot}"
            ).strip()
        return (
            str(result or "").strip()
            or f"Routed the latest user follow-up to `{projection_label}` ({projection_title}) and resumed the existing company runtime."
        )

    async def _close_company_delivery_review_task(
        self,
        task: Task,
        *,
        resolution: str,
        closed_at: str | None = None,
        checkpoint_id: str = "",
        metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        if not self.store:
            return
        now = str(closed_at or datetime.now().isoformat())
        task.metadata = dict(task.metadata or {})
        if metadata_updates:
            task.metadata.update(copy.deepcopy(dict(metadata_updates)))
        if checkpoint_id:
            task.metadata["human_review_checkpoint_id"] = checkpoint_id
        progress = list(task.metadata.get("progress_log", []) or [])
        progress.append(f"Delivery human review closed: {resolution}.")
        close_updates = {
            "requires_user_feedback": False,
            "human_review_closed": True,
            "human_review_closed_at": now,
            "human_review_resolution": resolution,
            "feedback_closed": True,
            "feedback_resolved": True,
            "feedback_resolution": resolution,
            "feedback_closed_at": now,
            "progress_log": progress[-50:],
        }
        task.metadata.update(close_updates)
        task.status = TaskStatus.DONE
        task.execution_lock = False
        task.execution_locked_at = None
        await self.store.save_task(task)

        work_item_id = str(linked_work_item_id_for_task(task) or "").strip()
        if not work_item_id or not hasattr(self.store, "update_delegation_work_item"):
            return

        work_item_updates = {
            **close_updates,
            "task_status": TaskStatus.DONE.value,
            "last_transition_reason": resolution,
        }
        try:
            await self.store.update_delegation_work_item(
                work_item_id,
                phase=Phase.APPROVED,
                blocked_reason="",
                metadata_updates=work_item_updates,
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
            )
        except InvalidPhaseTransition:
            try:
                await self.store.update_delegation_work_item(
                    work_item_id,
                    metadata_updates=work_item_updates,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to update closed delivery review work item metadata for {}",
                    work_item_id,
                )
        except TypeError:
            try:
                await self.store.update_delegation_work_item(
                    work_item_id,
                    phase=Phase.APPROVED,
                    blocked_reason="",
                    metadata_updates=work_item_updates,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to approve closed delivery review work item for {}",
                    work_item_id,
                )
        except Exception:
            logger.opt(exception=True).debug(
                "failed to approve closed delivery review work item for {}",
                work_item_id,
            )

    async def _terminalize_company_delivery_feedback_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        status: str,
        resolution: str,
        payload_updates: dict[str, Any] | None = None,
        task_metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        if not self.store:
            return
        payload = {**dict(checkpoint.payload or {}), **dict(payload_updates or {})}
        waiting_task_id = str(
            payload.get("waiting_task_id")
            or payload.get("task_id")
            or getattr(checkpoint, "task_id", "")
            or ""
        ).strip()
        closed_at = str(
            (task_metadata_updates or {}).get("self_evolution_review_completed_at")
            or (task_metadata_updates or {}).get("feedback_superseded_at")
            or datetime.now().isoformat()
        )
        if waiting_task_id:
            try:
                waiting_task = await self.store.get_task(waiting_task_id)
            except Exception:
                waiting_task = None
            if waiting_task is not None:
                await self._close_company_delivery_review_task(
                    waiting_task,
                    resolution=resolution,
                    closed_at=closed_at,
                    checkpoint_id=str(getattr(checkpoint, "checkpoint_id", "") or "").strip(),
                    metadata_updates=task_metadata_updates,
                )
        await self._mark_company_runtime_checkpoint_status(
            checkpoint,
            status=status,
            payload_updates=payload,
        )

    async def ignore_company_delivery_feedback_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        reply_metadata: dict[str, Any] | None = None,
    ) -> str:
        assert self.store
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        status = str(getattr(checkpoint, "status", "") or "").strip().lower()
        if status and status != "pending":
            return "This self-evolution review is no longer active."

        payload = dict(checkpoint.payload or {})
        waiting_task_id = str(
            payload.get("waiting_task_id")
            or payload.get("task_id")
            or getattr(checkpoint, "task_id", "")
            or ""
        ).strip()
        if not waiting_task_id:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not ignore self-evolution because the delivery task reference is missing."
        waiting_task = await self.store.get_task(waiting_task_id)
        if not waiting_task:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not ignore self-evolution because the delivery task no longer exists."

        ignored_at = datetime.now().isoformat()
        await self._terminalize_company_delivery_feedback_checkpoint(
            checkpoint,
            status="ignored",
            resolution="self_evolution_review_ignored",
            payload_updates={
                **payload,
                "feedback_ignored": True,
                "feedback_ignored_at": ignored_at,
                "feedback_resolution": "self_evolution_review_ignored",
                "feedback_reply_metadata": dict(reply_metadata or {}),
            },
            task_metadata_updates={
                "self_evolution_review_ignored": True,
                "self_evolution_review_ignored_at": ignored_at,
                "feedback_ignored": True,
                "feedback_ignored_at": ignored_at,
            },
        )
        return "Self-evolution review ignored."

    async def _ensure_open_final_delivery_review_checkpoints(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> None:
        if not self.store:
            return
        open_delivery_tasks = [
            task
            for task in tasks
            if task.status == TaskStatus.AWAITING_HUMAN
            and self._is_open_final_delivery_review_task(task)
            and not self._metadata_flag_true(dict(getattr(task, "metadata", {}) or {}).get("self_evolution_review_completed", False))
        ]
        if not open_delivery_tasks:
            return
        try:
            pending = await self.store.get_pending_checkpoints(
                project_id=self.project_id or "default",
                checkpoint_types=["company_delivery_feedback"],
            )
        except Exception:
            logger.opt(exception=True).debug("failed to inspect pending delivery feedback checkpoints")
            pending = []
        pending_task_ids = {
            str(
                getattr(checkpoint, "task_id", "")
                or dict(getattr(checkpoint, "payload", {}) or {}).get("waiting_task_id")
                or dict(getattr(checkpoint, "payload", {}) or {}).get("task_id")
                or ""
            ).strip()
            for checkpoint in pending
        }
        for task in open_delivery_tasks:
            if str(task.id or "").strip() in pending_task_ids:
                continue
            try:
                await self._save_company_feedback_followup_checkpoint(task, tasks, plan)
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to restore missing delivery feedback checkpoint for task {}",
                    task.id,
                )

    @staticmethod
    def _reply_metadata_requests_force_resume(reply_metadata: dict[str, Any] | None) -> bool:
        return bool(dict(reply_metadata or {}).get("ui_force_resume", False))

    def _ensure_attachment_store(self) -> None:
        project_id = self.project_id or "default"
        if self.attachment_store and self.attachment_store.project_id == project_id:
            return
        self.attachment_store = AttachmentStore(self.opc_home, project_id)

    def _normalize_attachment_refs(self, attachments: list[Any] | None) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in list(attachments or []):
            try:
                if isinstance(item, AttachmentRef):
                    normalized.append(item.to_dict())
                    continue
                if isinstance(item, dict) and item.get("attachment_id"):
                    normalized.append(AttachmentRef.from_dict(item).to_dict())
            except Exception as exc:
                logger.warning(f"Skipping invalid attachment reference: {exc}")
        return normalized

    def _is_inline_text_attachment(self, ref: AttachmentRef) -> bool:
        return can_extract_text(ref.filename, ref.mime_type)

    def _build_attachment_context(self, attachment_refs: list[dict[str, Any]] | None) -> str:
        refs = self._normalize_attachment_refs(attachment_refs)
        if not refs:
            return ""
        self._ensure_attachment_store()
        if not self.attachment_store:
            return ""

        parts = ["## Attachments"]
        remaining_budget = 5000
        hidden_count = 0

        for index, ref_dict in enumerate(refs, start=1):
            try:
                ref = AttachmentRef.from_dict(ref_dict)
            except Exception as exc:
                logger.warning(f"Failed to parse attachment ref: {exc}")
                continue

            if index > 6:
                hidden_count += 1
                continue

            try:
                abs_path = self.attachment_store.resolve_abs_path(ref)
            except Exception as exc:
                logger.warning(f"Failed to resolve attachment path for {ref.filename}: {exc}")
                abs_path = self.opc_home / ref.disk_path if ref.disk_path else None

            parts.append(f"### {ref.filename}")
            parts.append(f"- MIME type: {ref.mime_type}")
            parts.append(f"- Size: {ref.size_bytes} bytes")
            if abs_path:
                parts.append(f"- Stored path: {abs_path}")

            if self._is_inline_text_attachment(ref) and remaining_budget > 0:
                try:
                    preview = extract_attachment_text(
                        ref.filename,
                        ref.mime_type,
                        self.attachment_store.read_bytes(ref),
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
                parts.append("- Note: image attachment is stored and may be passed directly to the model when the provider path supports image input.")
            elif ref.mime_type == "application/pdf":
                parts.append("- Note: PDF attachment is stored and may be passed directly to the model when the provider path supports PDF input.")
            elif ref.mime_type.startswith("video/"):
                parts.append("- Note: video attachment is stored and may be passed directly to the model when the provider path supports video input.")
            else:
                parts.append("- Note: binary or complex document is available by path only when inline extraction is not available.")

        if hidden_count:
            parts.append(f"- Additional attachments omitted from inline context: {hidden_count}")
        return "\n".join(parts)

    def _secretary_workspace_root(self) -> str | None:
        return None

    async def _resolve_target_output_dir(self, message: str, session_id: str | None = None) -> str | None:
        contract = await self._resolve_workspace_contract(message, session_id)
        output_root = str(contract.get("output_root") or "").strip()
        return output_root or None

    def _resolve_comms_workspace_root(self, target_output_dir: str | None) -> str | None:
        """Pick the directory under which the file-based comms tree lives.

        Comms is OPC's collaboration substrate, NOT a deliverable — it
        belongs at the *workspace* root (sibling to project deliverables),
        not inside the project's specific output folder. Putting it
        inside the deliverable folder pollutes that folder with
        OpenOPC-internal state.

        Resolution order:

        1. Secretary's known workspace root (the user's "main workspace
           directory" — same place subprojects live in).
        2. The parent of `target_output_dir` (under the assumption that
           `target_output_dir` is a project subfolder).
        3. `target_output_dir` itself, as a last resort so something
           still works even if no workspace concept exists.
        4. None — caller should skip comms.
        """
        if target_output_dir:
            try:
                parent = str(Path(target_output_dir).expanduser().resolve().parent)
                if parent and parent not in {"/", "."}:
                    return parent
            except Exception:
                pass
            return target_output_dir
        return None

    def _apply_session_execution_defaults(
        self,
        decision: ModeSelection,
        session_defaults: dict[str, Any] | None,
        user_message: str,
    ) -> ModeSelection:
        defaults = dict(session_defaults or {})
        if not defaults:
            return decision

        explicit_override = self._detect_explicit_mode_override(user_message)
        previous_mode = str(defaults.get("mode") or "").strip()
        previous_profile = str(defaults.get("company_profile") or "").strip() or None
        previous_agent = str(defaults.get("preferred_agent") or "").strip() or None

        if explicit_override == ExecutionMode.SINGLE_AGENT.value:
            return decision

        if decision.mode == ExecutionMode.COMPANY_MODE:
            if not decision.company_profile and previous_profile:
                decision.company_profile = previous_profile
            if previous_agent and decision.preferred_agent is None:
                decision.preferred_agent = previous_agent
            return decision

        if (
            previous_mode == ExecutionMode.COMPANY_MODE.value
            and explicit_override != ExecutionMode.SINGLE_AGENT.value
            and self._looks_like_followup_request(user_message)
        ):
            decision.mode = ExecutionMode.COMPANY_MODE
            decision.company_profile = decision.company_profile or previous_profile
            if previous_agent and decision.preferred_agent is None:
                decision.preferred_agent = previous_agent
            suffix = " Continuing the prior company-mode session by default."
            existing_reasoning = str(getattr(decision, "reasoning", "") or "").strip()
            decision.reasoning = f"{existing_reasoning}{suffix}".strip() if existing_reasoning else suffix.strip()
        return decision

    async def _run_task_once(self, task: Task) -> TaskResult:
        """執行單一任務（嘗試外部代理，失敗時回退到原生代理）。

        參數：
            task (Task)：要執行的任務。

        返回值：
            TaskResult — 任務執行結果。
        """
        attempts: list[dict[str, Any]] = []
        candidates = self._get_external_candidates(task)
        scoped_progress = self._make_task_progress_callback(task)
        if candidates:
            self._apply_task_mode_external_timeout_defaults(task)
            workspace = self._resolve_external_workspace(task)

            for agent_name, adapter in candidates:
                run_adapter, resume_metadata = await self._configure_external_adapter_for_task(task, adapter)
                adapter_config = getattr(run_adapter, "config", None)
                session_mode = str(getattr(adapter_config, "session_mode", "") or "").strip().lower()
                run_mode = str(getattr(adapter_config, "run_mode", "batch") or "batch").strip().lower()
                supports_interactive = bool(
                    run_adapter.supports_interactive() if hasattr(run_adapter, "supports_interactive") else False
                )
                task.metadata = dict(task.metadata)
                task.metadata["__external_resume_session"] = session_mode == "resume"
                external_prompt_task = copy.deepcopy(task)
                external_prompt_task.metadata = dict(external_prompt_task.metadata)
                external_prompt_task.metadata["__external_resume_session"] = session_mode == "resume"
                external_task = await self._build_external_agent_task(external_prompt_task)
                for key in (
                    "external_resume_review_feedback_version",
                    "external_resume_review_feedback_digest",
                ):
                    value = dict(getattr(external_task, "metadata", {}) or {}).get(key)
                    if value in (None, "", [], {}):
                        continue
                    task.metadata = dict(task.metadata or {})
                    task.metadata[key] = copy.deepcopy(value)
                if run_mode == "interactive" and supports_interactive:
                    cmd, metadata = run_adapter.build_interactive_invocation(external_task, workspace_path=workspace)
                else:
                    cmd, metadata = run_adapter.build_invocation(external_task, workspace_path=workspace)
                explicit_user_selected_agent = (
                    str(task.metadata.get("router_preferred_agent", "") or "").strip() == agent_name
                    or (
                        bool(task.metadata.get("execution_agent_locked"))
                        and str(task.metadata.get("selected_execution_agent", "") or "").strip() == agent_name
                    )
                    or (
                        str(
                            dict(task.metadata.get("agent_selection", {}) or {}).get(
                                "selection_source",
                                "",
                            )
                            or ""
                        ).strip()
                        == "company_runtime_resume_checkpoint"
                        and str(task.assigned_external_agent or "").strip()
                        == agent_name
                    )
                )
                metadata = {
                    **metadata,
                    "workspace": workspace,
                    "argv": cmd,
                    "target_output_dir": task.metadata.get("target_output_dir"),
                    "explicit_user_selected_agent": explicit_user_selected_agent,
                    "external_session_continuation": session_mode == "resume",
                    **resume_metadata,
                }
                await self._emit_external_agent_audit(
                    external_task,
                    metadata,
                    workspace,
                    progress_callback=scoped_progress,
                )

                broker_run_kwargs = {
                    "adapter": run_adapter,
                    "task": task,
                    "workspace_path": workspace,
                    "on_progress": scoped_progress,
                }
                if "prepared_task" in inspect.signature(self.external_broker.run).parameters:
                    broker_run_kwargs["prepared_task"] = external_task
                result = await self.external_broker.run(**broker_run_kwargs)
                attempts.append({
                    "agent": agent_name,
                    "status": result.status.value,
                    "command": metadata.get("command", ""),
                    "model": metadata.get("model", "(cli default)"),
                    "session_mode": metadata.get("session_mode", "auto"),
                    "new_session": metadata.get("new_session", False),
                    "failure_reason": result.content if result.status != TaskStatus.DONE else "",
                    "last_activity_at": str((result.artifacts or {}).get("last_activity_at", "")),
                    "activity_count": int((result.artifacts or {}).get("activity_count", 0) or 0),
                })

                if not result.artifacts:
                    result.artifacts = metadata
                else:
                    result.artifacts = {**metadata, **result.artifacts}
                result.artifacts["external_attempts"] = attempts
                session_token = str(
                    result.artifacts.get("resume_session_id", "")
                    or result.artifacts.get("provider_session_id", "")
                    or metadata.get("resume_session_id", "")
                    or ""
                ).strip()
                if session_token and result.status == TaskStatus.DONE:
                    task.metadata = dict(task.metadata)
                    task.metadata["external_resume_session_id"] = session_token
                    task.metadata["external_resume_session_scope_id"] = task_session_scope_id(task)
                    task.metadata["external_resume_agent_type"] = str(
                        getattr(run_adapter, "agent_type", "") or agent_name
                    ).strip()

                if result.status == TaskStatus.DONE:
                    return result
                if self._external_result_requires_user_review(result):
                    logger.info(
                        f"External agent {agent_name} is awaiting user review for task {task.id}, pausing execution"
                    )
                    return result
                if self._external_result_denied_by_user(result):
                    logger.info(
                        f"User denied external agent {agent_name} for task {task.id}, falling back to native agent"
                    )
                    if scoped_progress:
                        await scoped_progress(
                            f"[External agent denied] user denied {agent_name} for task={task.title}; falling back to native agent"
                        )
                    break
                if explicit_user_selected_agent:
                    logger.warning(
                        f"Explicit external agent {agent_name} failed for task {task.id}; "
                        "not trying alternate agents or native fallback"
                    )
                    if scoped_progress:
                        reason_excerpt = (result.content or "").strip().replace("\n", " ")
                        await scoped_progress(
                            f"[External agent failed] explicit {agent_name} failed for task={task.title}; "
                            f"reason={reason_excerpt or 'unknown'}"
                        )
                    return result

                logger.warning(
                    f"External agent {agent_name} failed for task {task.id}, trying next configured agent"
                )
                if scoped_progress:
                    reason_excerpt = (result.content or "").strip().replace("\n", " ")
                    await scoped_progress(
                        f"[External agent failed] {agent_name} failed for task={task.title}; "
                        f"reason={reason_excerpt or 'unknown'}; trying next configured agent"
                    )

            logger.warning("All configured external agents failed, falling back to native agent")
            if scoped_progress:
                await scoped_progress(
                    f"[External agents exhausted] task={task.title}; falling back to native agent"
                )

        native_result = await self._run_native_agent(task)
        if attempts:
            native_result.artifacts = {
                **(native_result.artifacts or {}),
                "external_attempts": attempts,
                "external_fallback_to_native": True,
            }
        return native_result

    async def _execute_task(self, task: Task) -> TaskResult:
        """任務執行入口 — 註冊到 ActiveTaskRunRegistry 後委派實際執行。

        參數：
            task (Task)：待執行的任務物件。

        返回值：
            TaskResult — 任務執行結果（含狀態、內容、artifacts）。

        生命週期：
            1. 向 ActiveTaskRunRegistry 註冊（防止同專案重複執行）
            2. 呼叫 _execute_registered_task_attempt 執行
            3. finally 中解除註冊

        被誰引用：
            - _execute_single_agent()：串列執行每個任務
            - _execute_multi_agent()：並行執行每個任務
        """
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        try:
            attempt_token = self._active_task_run_registry.register(project_id, task.id)
        except ActiveTaskRunAdmissionClosed as exc:
            raise asyncio.CancelledError(str(exc)) from exc
        try:
            return await self._execute_registered_task_attempt(task)
        finally:
            self._active_task_run_registry.unregister(project_id, task.id, attempt_token)

    @staticmethod
    def _company_runtime_task_has_durable_hold(task: Task | None) -> bool:
        """判斷公司運行時任務是否處於持久暫停狀態。

        參數：
            task (Task | None)：待檢查的任務。

        返回值：
            bool — 若任務 metadata 中 dispatch_hold 為 suspended 或
                   stop_state 為 suspending/suspended 則回傳 True。

        被誰引用：
            - _execute_registered_task_attempt()：完成競態檢查
        """
        if task is None or not is_company_runtime_task(task):
            return False
        metadata = dict(task.metadata or {})
        return (
            str(metadata.get("dispatch_hold", "") or "").strip()
            == "company_runtime_suspended"
            or str(metadata.get("company_runtime_stop_state", "") or "").strip()
            in {"suspending", "suspended"}
        )

    @staticmethod
    def _ensure_result_delivery_identity_for_commit(
        task: Task,
        result: TaskResult,
    ) -> dict[str, str]:
        """確保任務結果具有不可變的交付身份標識。

        功能說明：
            運行時產生的 canonical turn 不保證被複製回可變的 Task metadata，
            因此 TaskResult 是運行時持久化與引擎鏡像之間的交接邊界。
            若結果缺少 canonical turn，則在此產生一次性 execution seed。
            產生的身份寫回 TaskResult.artifacts，重播時保持穩定。

        參數：
            task (Task)：產生結果的任務。
            result (TaskResult)：待提交的结果物件。

        返回值：
            dict[str, str] — 交付身份 payload（含 result_delivery_id 等）。

        被誰引用：
            - _execute_registered_task_attempt()：結果提交前呼叫
        """
        artifacts = dict(result.artifacts or {})
        canonical_turn_id = str(
            artifacts.get("canonical_turn_id")
            or artifacts.get("conversation_turn_id")
            or ""
        ).strip()
        persisted_delivery_id = str(artifacts.get("result_delivery_id") or "").strip()
        execution_id = str(artifacts.get("result_execution_id") or "").strip()
        if not persisted_delivery_id and not canonical_turn_id and not execution_id:
            execution_id = uuid.uuid4().hex
        identity = result_delivery_identity_payload_for_task(
            task,
            canonical_turn_id=canonical_turn_id,
            execution_id=execution_id,
            result_delivery_id=persisted_delivery_id,
        )
        result.artifacts = {
            **artifacts,
            **({"result_execution_id": execution_id} if execution_id else {}),
            **identity,
        }
        return identity

    async def _execute_registered_task_attempt(self, task: Task) -> TaskResult:
        """已註冊任務的完整執行嘗試 — 含取消處理、結果提交、重試邏輯。

        生命週期：
            1. 呼叫 _run_task_once 執行任務
            2. 處理 CancelledError（標記 CANCELLED 或重新拋出）
            3. 檢查持久暫停競態（公司運行時）
            4. 套用運行時狀態、確保交付身份、持久化結果
            5. 記錄記憶（共享角色工作階段 / 子任務結果）
            6. 根據結果狀態：DONE → 記錄完成；等待審查 → 儲存檢查點
            7. FAILED 且可重試 → 能力恢復 + 重試

        參數：
            task (Task)：已註冊到 ActiveTaskRunRegistry 的任務。

        返回值：
            TaskResult — 最終執行結果。

        被誰引用：
            - _execute_task()：註冊後委派到此
        """
        try:
            result = await self._run_task_once(task)
        except asyncio.CancelledError:
            if self._shutting_down or is_company_runtime_task(task):
                raise
            store = self.store
            if not store or not bool(getattr(store, "is_ready", True)):
                raise
            try:
                fresh = await store.get_task(task.id)
            except AssertionError:
                logger.debug(
                    "Task cancellation cleanup skipped because store is already closed for task {}",
                    task.id,
                )
                raise
            target = fresh or task
            if is_company_runtime_task(target):
                raise
            if target.status != TaskStatus.CANCELLED:
                target.status = TaskStatus.CANCELLED
                await store.save_task(target)
            raise
        # Re-read task to check if user cancelled during execution
        fresh = await self.store.get_task(task.id)
        if fresh and fresh.status == TaskStatus.CANCELLED:
            logger.info(f"Task {task.id} was cancelled during execution, preserving CANCELLED status")
            return result
        if self._company_runtime_task_has_durable_hold(fresh):
            logger.info(
                "Discarding completed result for task {} because a durable company "
                "runtime hold won the completion race",
                task.id,
            )
            raise asyncio.CancelledError(
                "company runtime was suspended before result commit"
            )
        self._apply_runtime_state_to_task(task, result)
        result_identity = self._ensure_result_delivery_identity_for_commit(task, result)
        task.status = result.status
        task.result = {"content": result.content, "artifacts": result.artifacts}
        await self.store.save_task(task)
        if self.memory and task.session_id and self._uses_shared_role_session(task):
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            await self.memory.record_assistant_turn(
                session_id=task.session_id,
                content=result.content,
                project_id=task.project_id,
                agent_id=task.assigned_to or None,
                task_id=task.id,
                metadata={
                    "kind": "company_role_result",
                    "status": result.status.value,
                    "employee_id": str(assignment.get("employee_id", "")).strip(),
                    "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                    "child_session_id": str(task.session_id),
                    **result_identity,
                    **work_item_identity_payload_for_task(task),
                },
            )
        elif (
            self.memory
            and task.session_id
            and task.parent_session_id
            and task.session_id != task.parent_session_id
        ):
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            child_result_message = await self.memory.record_assistant_turn(
                session_id=task.session_id,
                content=result.content,
                project_id=task.project_id,
                agent_id=task.assigned_to or None,
                task_id=task.id,
                metadata={
                    "kind": "child_task_result",
                    "status": result.status.value,
                    "employee_id": str(assignment.get("employee_id", "")).strip(),
                    "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                    "child_session_id": str(task.session_id),
                    **result_identity,
                    **work_item_identity_payload_for_task(task),
                },
            )
            await self.memory.record_child_session_result(
                parent_session_id=task.parent_session_id,
                child_session_id=task.session_id,
                task=task,
                result_content=result.content,
                artifacts=result.artifacts,
                result_delivery_id=result_identity.get("result_delivery_id", ""),
                source_result_message_id=str(
                    getattr(child_result_message, "message_id", "") or ""
                ),
                canonical_turn_id=result_identity.get("canonical_turn_id", ""),
            )
        if result.status == TaskStatus.DONE:
            await self._record_task_mode_external_result_reply(task, result.content)
            if self.memory and task.metadata.get("execution_mode") != ExecutionMode.COMPANY_MODE.value:
                await self.memory.record_task_completion_async(
                    task=task,
                    result_content=result.content,
                    project=bool(task.project_id and task.project_id != "default"),
                )
        elif result.status in _REVIEW_WAITING_STATUSES:
            await self._save_task_pause_checkpoint(task, result)
        elif result.status == TaskStatus.AWAITING_PEER:
            await self._save_peer_pause_checkpoint(task, result)

        if result.status == TaskStatus.FAILED and task.retry_count < task.max_retries:
            task.retry_count += 1
            await self._attempt_capability_recovery(task, result)
            task.status = TaskStatus.PENDING
            await self.store.save_task(task)
            logger.info(f"Retrying task {task.id} (attempt {task.retry_count})")
            result = await self._run_task_once(task)
            # Re-read task to check if user cancelled during retry
            fresh = await self.store.get_task(task.id)
            if fresh and fresh.status == TaskStatus.CANCELLED:
                logger.info(f"Task {task.id} was cancelled during retry, preserving CANCELLED status")
                return result
            if self._company_runtime_task_has_durable_hold(fresh):
                logger.info(
                    "Discarding retry result for task {} because a durable company "
                    "runtime hold won the completion race",
                    task.id,
                )
                raise asyncio.CancelledError(
                    "company runtime was suspended before retry result commit"
                )
            self._apply_runtime_state_to_task(task, result)
            result_identity = self._ensure_result_delivery_identity_for_commit(task, result)
            task.status = result.status
            task.result = {"content": result.content, "artifacts": result.artifacts}
            await self.store.save_task(task)
            if self.memory and task.session_id and self._uses_shared_role_session(task):
                assignment = dict(task.metadata.get("employee_assignment", {}) or {})
                await self.memory.record_assistant_turn(
                    session_id=task.session_id,
                    content=result.content,
                    project_id=task.project_id,
                    agent_id=task.assigned_to or None,
                    task_id=task.id,
                    metadata={
                        "kind": "company_role_result_retry",
                        "status": result.status.value,
                        "retry_count": task.retry_count,
                        "employee_id": str(assignment.get("employee_id", "")).strip(),
                        "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                        "child_session_id": str(task.session_id),
                        **result_identity,
                        **work_item_identity_payload_for_task(task),
                    },
                )
            elif (
                self.memory
                and task.session_id
                and task.parent_session_id
                and task.session_id != task.parent_session_id
            ):
                assignment = dict(task.metadata.get("employee_assignment", {}) or {})
                child_result_message = await self.memory.record_assistant_turn(
                    session_id=task.session_id,
                    content=result.content,
                    project_id=task.project_id,
                    agent_id=task.assigned_to or None,
                    task_id=task.id,
                    metadata={
                        "kind": "child_task_result_retry",
                        "status": result.status.value,
                        "retry_count": task.retry_count,
                        "employee_id": str(assignment.get("employee_id", "")).strip(),
                        "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                        "child_session_id": str(task.session_id),
                        **result_identity,
                        **work_item_identity_payload_for_task(task),
                    },
                )
                await self.memory.record_child_session_result(
                    parent_session_id=task.parent_session_id,
                    child_session_id=task.session_id,
                    task=task,
                    result_content=result.content,
                    artifacts=result.artifacts,
                    result_delivery_id=result_identity.get("result_delivery_id", ""),
                    source_result_message_id=str(
                        getattr(child_result_message, "message_id", "") or ""
                    ),
                    canonical_turn_id=result_identity.get("canonical_turn_id", ""),
                )
            if result.status == TaskStatus.DONE:
                await self._record_task_mode_external_result_reply(task, result.content)
                if self.memory and task.metadata.get("execution_mode") != ExecutionMode.COMPANY_MODE.value:
                    await self.memory.record_task_completion_async(
                        task=task,
                        result_content=result.content,
                        project=bool(task.project_id and task.project_id != "default"),
                    )
        if task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            await self._supersede_stale_task_wait_checkpoints(
                task.id, reason=f"task settled as {task.status.value}"
            )
        return result

    async def _attempt_capability_recovery(self, task: Task, result: TaskResult) -> None:
        """嘗試能力恢復 — 失敗後從技能庫附加恢復上下文供重試使用。

        參數：
            task (Task)：失敗的任務（將被重試）。
            result (TaskResult)：失敗結果（提供失敗上下文）。

        被誰引用：
            - _execute_registered_task_attempt()：FAILED 且可重試時呼叫
        """
        if not self.capability_manager or not self.config.capabilities.enable_recovery:
            return
        query = task.description or task.title
        if result.content:
            query = f"{query}\n\nFailure context:\n{result.content}"
        recovery_context, candidates = await self.capability_manager.build_recovery_context(query, domains=task.tags)
        if not recovery_context:
            return
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["capability_recovery"] = recovery_context
        if self.on_progress:
            await self.on_progress("[CapabilityRecovery] Attached local skill recovery context and retrying.")

    async def _run_native_agent(self, task: Task) -> TaskResult:
        """建立並執行原生代理 — 根據任務分配的角色實例化 NativeAgent。

        參數：
            task (Task)：待執行的任務（assigned_to 決定角色）。

        返回值：
            TaskResult — 原生代理的執行結果。

        被誰引用：
            - _run_task_once()：原生代理執行路徑
            - _run_meeting_turn()：會議諮詢回合
        """
        assert self.llm and self.memory and self.preferences and self.skills and self.org_engine and self.context_assembler

        if task.assigned_to:
            role = self.org_engine.get_role_for_work_item(task.assigned_to, task.tags)
        else:
            role = self.org_engine.get_role_for_domain(task.tags)

        agent = NativeAgent(
            role=role,
            llm=self.llm,
            tool_registry=self.tool_registry,
            context_assembler=self.context_assembler,
            memory=self.memory,
            preferences=self.preferences,
            skills=self.skills,
            event_bus=self.event_bus,
            cost_tracker=self.cost_tracker,
            config=self.config,
            communication=self.communication,
            approval_callback=self._tool_approval_callback,
            permission_policy=self.approval_engine,
        )

        scoped_progress = self._make_task_progress_callback(task)
        return await agent.execute(task, on_progress=scoped_progress)

    async def _run_meeting_turn(
        self,
        meeting: MeetingRoom,
        participant: str,
        request: dict[str, Any],
    ) -> str:
        """執行會議回合 — 為指定參與者建立臨時任務並執行原生代理。

        參數：
            meeting (MeetingRoom)：會議室物件（含主題、任務 ID）。
            participant (str)：參與者角色 ID。
            request (dict)：回合請求（含 task_brief、mode、round 等）。

        返回值：
            str — 參與者的結構化回應（JSON 字串）。

        被誰引用：
            - CompanyWorkItemExecutor：會議協調時呼叫
        """
        source_task = await self.store.get_task(str(meeting.task_id)) if meeting.task_id else None
        execution_scope_ids = [
            str(item).strip()
            for item in list(request.get("execution_scope_ids", []) or [])
            if str(item).strip()
        ]
        read_only_tools = [
            "file_read",
            "file_search",
            "list_dir",
            "grep",
            "glob",
            "web_search",
            "web_fetch",
            "probe",
        ]
        temp_task = Task(
            title=f"Meeting Consultation: {meeting.topic}",
            description=str(request.get("task_brief", "") or "").strip() or f"Meeting turn for {participant}",
            assigned_to=participant,
            status=TaskStatus.PENDING,
            project_id=str(getattr(source_task, "project_id", "") or self.project_id or "default"),
            tags=list(getattr(source_task, "tags", []) or []),
            metadata=mark_projected_work_item_task({
                "execution_mode": "company_mode",
                "original_message": str(request.get("task_brief", "") or "").strip(),
                "_subagent_profile_prompt": str(request.get("system_addendum", "") or "").strip(),
                "_fork_allowed_tools": list(read_only_tools),
                "_disable_live_inbox_interrupts": True,
                "execution_task_ids": execution_scope_ids,
                "meeting_room_id": meeting.room_id,
                "meeting_consultation": True,
                "meeting_turn_mode": str(request.get("mode", "participant") or "participant"),
            }, projection_id=f"meeting::{meeting.room_id}::{participant}::round{int(request.get('round', 1) or 1)}", turn_type="plan"),
            context_snapshot={
                "meeting_turn_context": dict(request.get("meeting_context", {}) or {}),
            },
        )
        member_sessions = getattr(self.company_executor.runtime, "member_sessions", {}) if self.company_executor else {}
        session = next(
            (session for session in member_sessions.values() if getattr(session, "role_id", "") == participant),
            None,
        )
        if session is not None:
            session_payload = self.company_executor.runtime._serialize_session(session)
            temp_task.metadata["member_session_state"] = session_payload
            temp_task.context_snapshot["member_session"] = session_payload
        result = await self._run_native_agent(temp_task)
        content = str(result.content or "").strip()
        if content:
            return content
        if str(request.get("mode", "") or "") == "decision_owner":
            return '{"decision":"","action_items":[],"reasoning":"No structured owner decision was produced.","requires_human_input":true,"follow_up_questions":[]}'
        return '{"stance":"abstain","proposal":"","support_level":0.5,"vote":"abstain","reasoning":"No structured meeting response was produced.","blocking_issues":["Missing structured participant response."],"assumptions":[],"questions_for_others":[]}'

    # Checkpoint 類型：代表「任務暫停等待使用者輸入」。
    # 不變量：這些類型的 pending 行僅在任務確實處於等待狀態時有效；
    # 其他任何路徑都必須終止它們。
    _TASK_WAIT_CHECKPOINT_TYPES = ("task_user_input", "task_peer_wait")

    # --- Public API ---

    @staticmethod
    def _normalize_requested_mode(value: Any) -> str:
        """標準化請求模式字串（task/company）。"""
        normalized = str(value or "task").strip().lower()
        if normalized == "project":
            return "task"
        if normalized == "company":
            return "company"
        return "task"

    @staticmethod
    def _is_delegate_usable(delegate: "OPCEngine") -> bool:
        """判斷快取的委派引擎是否仍可用（store 連線未關閉）。"""
        store = getattr(delegate, "store", None)
        return bool(store is None or getattr(store, "is_ready", True))

    async def _get_project_delegate(self, project_id: str) -> OPCEngine:
        """取得指定專案的委派引擎實例（跨專案操作時使用獨立引擎）。

        功能說明：
            每個引擎擁有一個 store/memory/runtime 上下文。當呼叫者需要
            操作其他專案時，委派到獨立引擎而非重新綁定當前 store。

        參數：
            project_id (str)：目標專案 ID。

        返回值：
            OPCEngine — 已初始化的專案委派引擎（或自身若同專案）。
        """
        normalized_project_id = str(project_id or "").strip() or "default"
        current_project_id = str(self.project_id or "default").strip() or "default"
        if normalized_project_id == current_project_id:
            return self
        existing = self._project_engine_delegates.get(normalized_project_id)
        if existing is not None and self._is_delegate_usable(existing):
            return existing
        if self._project_delegate_lock is None:
            self._project_delegate_lock = asyncio.Lock()
        async with self._project_delegate_lock:
            existing = self._project_engine_delegates.get(normalized_project_id)
            if existing is not None:
                if self._is_delegate_usable(existing):
                    return existing
                # Store was closed (e.g. project deleted then re-created with
                # the same id) — drop the stale delegate and build a fresh one.
                self._project_engine_delegates.pop(normalized_project_id, None)
                logger.warning(
                    f"Discarding stale project delegate for '{normalized_project_id}' (store closed)"
                )
            try:
                delegate_config = copy.deepcopy(self.config)
            except Exception:
                delegate_config = self.config
            delegate = OPCEngine(
                config=delegate_config,
                opc_home=self.opc_home,
                project_id=normalized_project_id,
                active_task_run_registry=self._active_task_run_registry,
                owns_active_task_run_registry=False,
                on_progress=self.on_progress,
                on_runtime_event=self.on_runtime_event,
                on_escalation=self.on_escalation,
            )
            delegate.on_company_runtime_children = self.on_company_runtime_children
            delegate.on_company_kanban_callback_factory = self.on_company_kanban_callback_factory
            await delegate.initialize()
            self._project_engine_delegates[normalized_project_id] = delegate
            return delegate

    async def process_message(
        self,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
        mode: str = "task",
        org_id: str | None = None,
        preferred_agent: str | None = None,
        domains: list[str] | None = None,
        company_profile: str | None = None,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        message_metadata: dict[str, Any] | None = None,
    ) -> str:
        """處理使用者訊息並返回回覆（引擎的主要公開 API）。

        參數：
            content (str)：使用者訊息內容。
            project_id (str | None)：目標專案 ID（多專案委派）。
            session_id (str | None)：工作階段 ID。
            mode (str)：執行模式 — "task"（預設）、"company" 或 "org"。
                舊版 "project" 對應 task，"custom" 對應 org。
            org_id (str | None)：組織 ID（隔離的 org 模式）。
            preferred_agent (str | None)：首選執行代理
                （"native"、"claude_code"、"cursor"、"codex"、"opencode"）。
            domains (list[str] | None)：領域提示（如 ["coding", "frontend"]）。
            company_profile (str | None)："corporate"（公司模式）或 "custom"（org 模式）。
            origin_task_id (str | None)：來源任務 ID（UI 追蹤用）。
            attachment_refs (list[dict] | None)：附件引用（Office UI）。
            message_metadata (dict | None)：訊息 metadata（UI 身份追蹤）。

        返回值：
            str — 系統回覆內容。

        被誰引用：
            - opc/cli/app.py：CLI 互動迴圈
            - opc/plugins/office_ui/：Web API
        """
        target_project_id = str(project_id or "").strip() or None
        current_project_id = str(self.project_id or "default").strip() or "default"
        if self._initialized and target_project_id and target_project_id != current_project_id:
            delegate = await self._get_project_delegate(target_project_id)
            return await delegate.process_message(
                content,
                project_id=target_project_id,
                session_id=session_id,
                mode=mode,
                org_id=org_id,
                preferred_agent=preferred_agent,
                domains=domains,
                company_profile=company_profile,
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                message_metadata=message_metadata,
            )
        if target_project_id is not None:
            self.project_id = target_project_id
            if self.memory:
                self.memory.set_project(target_project_id)
            self._ensure_attachment_store()
        if not self._initialized:
            await self.initialize()
        await self._refresh_runtime_config_from_disk()

        requested_mode = str(mode or "task").strip().lower()
        company_profile_value = str(company_profile or "").strip().lower()
        if requested_mode in {"org", "custom"} or (
            requested_mode == "company" and company_profile_value == "custom"
        ):
            from opc.layer2_organization.custom_runtime import CustomRuntimeRunner

            return await CustomRuntimeRunner(self).process_message(
                content,
                project_id=self.project_id or target_project_id or "default",
                session_id=session_id,
                org_id=org_id,
                preferred_agent=preferred_agent,
                domains=domains,
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                message_metadata=message_metadata,
            )

        attachment_refs = self._normalize_attachment_refs(attachment_refs)
        merged_message_metadata = {
            "mode": mode,
            "org_id": org_id,
            "preferred_agent": preferred_agent,
            "domains": domains or [],
            "company_profile": company_profile,
            "origin_task_id": origin_task_id,
            "attachment_refs": attachment_refs,
        }
        if message_metadata:
            merged_message_metadata.update(dict(message_metadata))

        message = UserMessage(
            channel="cli",
            user_id="owner",
            content=content,
            attachments=attachment_refs,
            session_id=session_id or str(uuid.uuid4()),
            project_context=self.project_id,
            metadata=merged_message_metadata,
        )

        response = await self.message_bus.process_single(message)
        if response:
            return response.content
        return "No response generated."

    async def process_secretary_message(
        self,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """處理秘書直接訊息並返回結構化回覆。

        參數：
            content (str)：使用者訊息內容。
            project_id (str | None)：專案 ID。
            session_id (str | None)：工作階段 ID。

        返回值：
            dict — 結構化的秘書回覆。
        """
        if project_id is not None:
            self.project_id = project_id
            if self.memory:
                self.memory.set_project(project_id)
        if not self._initialized:
            await self.initialize()
        await self._refresh_runtime_config_from_disk()
        assert self.secretary
        return await self.secretary.handle_message(
            content,
            project_id=self.project_id,
            session_id=session_id,
        )

    async def shutdown(self) -> None:
        """優雅關閉所有子系統。

        生命週期：
            1. 準備活動中的公司運行時進行關閉
            2. 關閉所有專案委派引擎
            3. 停止通訊重新激活掃除器和心跳排程器
            4. 停止訊息匯流排和頻道管理器
            5. 關閉 MCP 管理器
            6. 關閉資料庫連線
            7. 輸出工作階段統計
        """
        self._shutting_down = True
        logger.info("Shutting down OPC Engine...")
        if self._owns_active_task_run_registry:
            await self.prepare_active_company_runtimes_for_shutdown()
        delegates = list(self._project_engine_delegates.values())
        self._project_engine_delegates.clear()
        for delegate in delegates:
            try:
                await delegate.shutdown()
            except Exception:
                logger.opt(exception=True).warning(
                    "Failed to shut down project delegate {}",
                    getattr(delegate, "project_id", None),
                )
        if self.comms_reactivation_sweeper:
            await self.comms_reactivation_sweeper.stop()
        if self.heartbeat_scheduler:
            await self.heartbeat_scheduler.stop()
        self.message_bus.stop()
        if self.channel_manager:
            await self.channel_manager.stop_all()
        if self.mcp_manager:
            await self.mcp_manager.shutdown()
        if self.store and self._owns_store:
            await self.store.close()

        if self.llm:
            stats = self.llm.stats
            logger.info(
                f"Session stats: tokens_in={stats['tokens_in']}, "
                f"tokens_out={stats['tokens_out']}, "
                f"estimated_cost=${stats['estimated_cost']:.4f}"
            )

        logger.info("OPC Engine shut down.")
