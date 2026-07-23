"""OPC 系統核心資料模型模組。

職責說明：
    定義整個 OPC 系統共享的領域資料模型，包括：
    - 列舉類型（Enum）：任務狀態、執行模式、階段、訊息類型等
    - 資料類別（dataclass）：Task、Message、Agent、Goal 等核心實體
    - 事件模型：OPCEvent 用於事件匯流排

    本模組為純資料定義，不包含業務邏輯（正規化函數除外）。
    所有層（layer0 ~ layer6）和引擎均依賴此模組的型別定義。

關聯關係：
    - 被 opc/engine.py、opc/database/store.py、所有 layer 模組匯入
    - 被 opc/core/events.py 使用 OPCEvent
    - 被 opc/core/config.py 使用部分列舉

使用範例：
    from opc.core.models import Task, TaskStatus, OPCEvent
    task = Task(task_id="t1", title="任務一", status=TaskStatus.PENDING)
"""

from __future__ import annotations  # 啟用延遲型別註解評估，支援前向引用

import uuid  # 標準庫：產生任務/訊息/代理的唯一 ID
from dataclasses import dataclass, field  # 標準庫：定義輕量級資料類別
from datetime import datetime, timezone  # 標準庫：時間戳記（建立/更新時間）
from enum import Enum  # 標準庫：定義列舉類型
from typing import Any, Literal  # 標準庫：型別註解（Any 為通用型別，Literal 為字面量型別）


def _utcnow() -> datetime:
    """Return the current UTC time with timezone info (replaces naive datetime.now)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 列舉類型（Enums）— 定義系統中所有有限狀態集合
# ---------------------------------------------------------------------------

# 角色運行時狀態型別（精簡為三種核心狀態）
RoleRuntimeStatus = Literal["idle", "running", "blocked"]
# 合法的運行時狀態值集合（凍結集合，用於驗證）
ROLE_RUNTIME_STATUSES: frozenset[str] = frozenset({"idle", "running", "blocked"})


def normalize_role_runtime_status(
    status: Any,
    focused_work_item_id: Any = "",
    *,
    default: RoleRuntimeStatus = "idle",
) -> RoleRuntimeStatus:
    """將公司模式的角色/成員/席位運行時狀態收斂為三種核心狀態。

    功能：
        將各種歷史遺留狀態（cold、reserved、booting、draining 等）
        正規化為 idle/running/blocked 三種狀態。正規化在邊界處執行，
        避免運行時內部攜帶過多狀態變體。

    參數：
        status (Any)：原始狀態字串（可能為任何歷史值）。
        focused_work_item_id (Any)：當前聚焦的工作項目 ID。
            有聚焦項目時，某些狀態會被推斷為 running 或 blocked。
        default (RoleRuntimeStatus)：無法判斷時的預設值。預設 "idle"。

    返回值：
        RoleRuntimeStatus — "idle"、"running" 或 "blocked"。

    被誰引用：
        - opc/layer2_organization/company_runtime.py：正規化席位狀態
        - opc/plugins/office_ui/：前端顯示前的狀態正規化
    """
    text = str(status or "").strip().lower()
    focused = bool(str(focused_work_item_id or "").strip())
    if text == "idle":
        return "idle"
    if text == "running":
        return "running" if focused else "idle"
    if text == "blocked":
        return "blocked"
    if text == "":
        return "blocked" if focused else default
    if text == "cold":
        return "idle"
    if text in {"reserved", "booting", "draining"}:
        return "running" if focused else "idle"
    if text in {"dead", "handoff_pending"}:
        return "blocked"
    if focused:
        return "blocked"
    return default

class ExecutionMode(str, Enum):
    """執行模式列舉：決定系統以何種模式運行。

    取值說明：
        TASK_MODE ("task_mode")：任務模式 — 單一代理執行單一任務
        COMPANY_MODE ("company_mode")：公司模式 — 多代理協作執行
        PROJECT_MODE / SINGLE_AGENT：TASK_MODE 的別名（向下相容）
        MULTI_AGENT：COMPANY_MODE 的別名（向下相容）
    """
    TASK_MODE = "task_mode"
    COMPANY_MODE = "company_mode"
    PROJECT_MODE = "task_mode"  # 別名：向下相容舊版配置
    SINGLE_AGENT = "task_mode"  # 別名：向下相容舊版配置
    MULTI_AGENT = "company_mode"  # 別名：向下相容舊版配置

    @classmethod
    def _missing_(cls, value: object) -> "ExecutionMode" | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        if normalized in {"project_mode", "task_mode", "project", "task"}:
            return cls.TASK_MODE
        if normalized in {"company_mode", "company"}:
            return cls.COMPANY_MODE
        return None


class CompanyProfile(str, Enum):
    """公司設定檔類型：決定使用預設企業模板還是自定義配置。"""
    CORPORATE = "corporate"  # 企業模板（預設角色拓撲）
    CUSTOM = "custom"  # 自定義配置（使用者定義的組織 YAML）


class GoalLevel(str, Enum):
    """目標層級：組織目標的層級結構。"""
    COMPANY = "company"  # 公司級目標
    DEPARTMENT = "department"  # 部門級目標
    TEAM = "team"  # 團隊級目標
    TASK = "task"  # 任務級目標


class GoalStatus(str, Enum):
    """目標狀態：目標的生命週期狀態。"""
    ACTIVE = "active"  # 進行中
    COMPLETED = "completed"  # 已完成
    PAUSED = "paused"  # 已暫停
    CANCELLED = "cancelled"  # 已取消


class WorkItemExecutionStrategy(str, Enum):
    """工作項目執行策略：決定工作項目如何被執行。"""
    AUTO = "auto"  # 自動選擇
    NATIVE = "native"  # 原生執行（內部代理）
    EXTERNAL = "external"  # 外部執行（外部代理）
    MIXED = "mixed"  # 混合執行


class TaskStatus(str, Enum):
    """任務狀態：任務的生命週期狀態（任務模式使用）。"""
    PENDING = "pending"  # 待處理
    RUNNING = "running"  # 執行中
    IDLE = "idle"  # 閒置
    BLOCKED = "blocked"  # 被阻塞
    AWAITING_PEER = "awaiting_peer"  # 等待同事
    AWAITING_MANAGER_REVIEW = "awaiting_manager_review"  # 等待經理審查
    AWAITING_HUMAN = "awaiting_human"  # 等待人工
    AWAITING_REVIEW = "awaiting_review"  # 等待審查
    DONE = "done"  # 已完成
    FAILED = "failed"  # 已失敗
    CANCELLED = "cancelled"  # 已取消


class Phase(str, Enum):
    """委派工作項目的單一權威狀態（Phase 狀態機）。

    職責說明：
        取代之前 status + 5 個 metadata 子狀態欄位的混合設計。
        每個工作項目在任何時刻只有一個 Phase 值。
        純函數投影（kanban_column / is_runnable / effective_owner /
        verdict / task_status_for_phase）和轉換表定義於
        opc.layer2_organization.phase 模組。

    看板欄位對應：
        todo：QUEUED、READY、READY_FOR_REWORK、WAITING_DEPENDENCIES
        in_progress：RUNNING、WAITING_FOR_PEER、WAITING_FOR_CHILDREN、PAUSED、NEEDS_ATTENTION
        in_review：AWAITING_MANAGER_REVIEW、AWAITING_HUMAN
        done：APPROVED、FAILED、CANCELLED
    """

    # ─── kanban column: todo (not yet started) ────────────────────────────
    QUEUED = "queued"                               # manager has not released
    READY = "ready"                                 # released, dispatchable
    READY_FOR_REWORK = "ready_for_rework"           # returned by reviewer
    WAITING_DEPENDENCIES = "waiting_dependencies"   # upstream not done

    # ─── kanban column: in_progress (worker holds the card) ───────────────
    RUNNING = "running"
    WAITING_FOR_PEER = "waiting_for_peer"
    WAITING_FOR_CHILDREN = "waiting_for_children"
    PAUSED = "paused"                               # soft-interrupted
    NEEDS_ATTENTION = "needs_attention"             # worker flagged blocker

    # ─── kanban column: in_review (manager holds the card) ────────────────
    AWAITING_MANAGER_REVIEW = "awaiting_manager_review"
    AWAITING_HUMAN = "awaiting_human"

    # ─── kanban column: done (terminal) ───────────────────────────────────
    APPROVED = "approved"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentStatus(str, Enum):
    """代理狀態：代理的運行時狀態。"""
    IDLE = "idle"  # 閒置
    RUNNING = "running"  # 執行中
    BLOCKED = "blocked"  # 被阻塞
    ERROR = "error"  # 錯誤


class MessageUrgency(str, Enum):
    """訊息緊急程度：決定訊息的處理優先級。"""
    BLOCKING = "blocking"  # 阻塞性（必須立即處理）
    HIGH = "high"  # 高優先
    NORMAL = "normal"  # 普通
    LOW = "low"  # 低優先


class MessageStatus(str, Enum):
    """訊息狀態：訊息的投递生命週期。"""
    SENT = "sent"  # 已發送
    DELIVERED = "delivered"  # 已投递
    READ = "read"  # 已讀取
    REPLIED = "replied"  # 已回覆
    TIMED_OUT = "timed_out"  # 已逾時
    CANCELLED = "cancelled"  # 已取消


class AgentEndpointType(str, Enum):
    """代理端點類型：標識端點的性質。"""
    COMPANY_ROLE = "company_role"  # 公司角色
    NATIVE_SUBAGENT = "native_subagent"  # 原生子代理
    EXTERNAL_AGENT = "external_agent"  # 外部代理


class CommsTransportKind(str, Enum):
    """通訊傳輸類型：訊息的傳輸方式。"""
    DM = "dm"  # 私訊（一對一）
    BROADCAST = "broadcast"  # 廣播（一對多）
    MEETING = "meeting"  # 會議（多對多）
    SYSTEM = "system"  # 系統通知


class CommsSemanticType(str, Enum):
    """通訊語義類型：訊息的業務語義分類。"""
    # 以下值在運行時觀察到或保留用於舊版單團隊路徑。
    # 其他 10 個值（ASSIGNMENT、DEPENDENCY_REQUEST 等）已移除：
    # 無程式碼寫入它們且無運行時訊息攜帶它們。
    WORK_UPDATE = "work_update"  # 工作進度更新
    IDLE_NOTIFICATION = "idle_notification"  # 閒置通知
    BLOCKED_ON_DECISION = "blocked_on_decision"  # 被決策阻塞
    HANDOFF_READY = "handoff_ready"  # 交接就緒
    WORK_ITEM_RESULT = "work_item_result"  # 工作項目結果
    BLOCKER = "blocker"  # 阻塞報告
    APPROVAL_REQUEST = "approval_request"  # 審批請求
    COMPLETION = "completion"  # 完成通知
    STATUS_DIGEST = "status_digest"  # 狀態摘要


class CommsState(str, Enum):
    """通訊狀態：訊息的處理狀態。"""
    OPEN = "open"  # 待處理
    ACKNOWLEDGED = "acknowledged"  # 已確認
    RESOLVED = "resolved"  # 已解決
    EXPIRED = "expired"  # 已過期
    SUPERSEDED = "superseded"  # 已被取代


class MeetingStatus(str, Enum):
    """會議狀態：會議的生命週期狀態。"""
    OPEN = "open"  # 已開啟（等待參與者）
    IN_PROGRESS = "in_progress"  # 進行中
    DECIDED = "decided"  # 已達成決策
    CLOSED = "closed"  # 已關閉
    CANCELLED = "cancelled"  # 已取消


class EscalationType(str, Enum):
    """升級類型：代理向人類升級的類型。"""
    INFO_NEEDED = "info_needed"  # 需要資訊
    DECISION_NEEDED = "decision_needed"  # 需要決策
    RISK_WARNING = "risk_warning"  # 風險警告
    RECOMMENDATION = "recommendation"  # 建議


class RiskLevel(str, Enum):
    """風險等級：操作或決策的風險程度。"""
    LOW = "low"  # 低風險
    MEDIUM = "medium"  # 中風險
    HIGH = "high"  # 高風險
    CRITICAL = "critical"  # 極高風險


class ApprovalAction(str, Enum):
    """審批動作：審批系統的裁決結果。"""
    AUTO_APPROVE = "auto_approve"  # 自動核准
    ESCALATE = "escalate"  # 升級（交給人類）
    REJECT = "reject"  # 拒絕
    REQUIRE_INPUT = "require_input"  # 需要額外輸入


class PermissionResolution(str, Enum):
    """權限裁決：對操作權限的判斷結果。"""
    ALLOW = "allow"  # 允許
    ASK = "ask"  # 詢問使用者
    DENY = "deny"  # 拒絕


class PermissionScope(str, Enum):
    """權限範圍：權限裁決的有效期範圍。"""
    ONCE = "once"  # 僅此次
    SESSION = "session"  # 本次工作階段
    PROJECT = "project"  # 本專案
    GLOBAL = "global"  # 全域


class ReorgScope(str, Enum):
    """重組範圍：組織重組的影響範圍。"""
    TASK_ADJUSTMENT = "task_adjustment"  # 僅調整任務（不改變拓撲）
    ORG_MUTATION = "org_mutation"  # 組織拓撲變更


class ReorgRiskLevel(str, Enum):
    """重組風險等級：組織重組的風險程度。"""
    LOW = "low"  # 低風險
    MEDIUM = "medium"  # 中風險
    HIGH = "high"  # 高風險


class ReorgProposalStatus(str, Enum):
    """重組提案狀態：提案的審批生命週期。"""
    PROPOSED = "proposed"  # 已提出
    APPROVED = "approved"  # 已核准
    DENIED = "denied"  # 已拒絕
    APPLIED = "applied"  # 已套用
    FAILED = "failed"  # 套用失敗
    CANCELLED = "cancelled"  # 已取消


class ReorgEventKind(str, Enum):
    """重組事件類型：重組過程中發生的事件種類。"""
    PROPOSED = "proposed"  # 提案建立
    APPROVED = "approved"  # 提案核准
    DENIED = "denied"  # 提案拒絕
    APPLIED = "applied"  # 提案套用
    MIGRATED = "migrated"  # 遷移完成
    AUTO_TASK_ADJUSTED = "auto_task_adjusted"  # 自動任務調整
    FAILED = "failed"  # 失敗


# ---------------------------------------------------------------------------
# Layer 0：訊息模型 — 使用者輸入和系統回覆的資料結構
# ---------------------------------------------------------------------------

@dataclass
class UserMessage:
    """使用者輸入訊息模型。

    職責說明：
        封裝從任何頻道（CLI、Office UI、Telegram 等）收到的使用者訊息。
        作為引擎管道的輸入，流經 layer0 → layer1 → layer2 → layer3。

    關聯關係：
        - 由 opc/channels/ 各頻道適配器建立
        - 被 opc/layer0_interaction/message_bus.py 分發
        - 被 opc/engine.py 處理
    """
    channel: str  # 來源頻道標識（如 "cli"、"office_ui"、"telegram"）
    user_id: str  # 使用者唯一識別碼
    content: str  # 訊息文字內容
    attachments: list[Any] = field(default_factory=list)  # 附件列表（AttachmentRef 或 base64）
    timestamp: datetime = field(default_factory=_utcnow)  # 訊息時間戳記
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 工作階段 ID（自動產生）
    project_context: str | None = None  # 專案上下文標識（可選）
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata（頻道特定資訊）


@dataclass
class SystemMessage:
    """系統回覆訊息模型。

    職責說明：
        封裝系統向使用者發送的訊息（回覆、進度更新、升級通知等）。
        由引擎或各層產生，透過頻道推送給使用者。

    關聯關係：
        - 由 opc/engine.py 和各層產生
        - 被 opc/channels/ 各頻道適配器發送
    """
    channel: str  # 目標頻道標識
    user_id: str  # 目標使用者 ID
    session_id: str  # 所屬工作階段 ID
    content: str  # 訊息文字內容
    message_type: Literal["reply", "escalation", "progress", "suggestion"] = "reply"  # 訊息類型
    actions: list[dict] = field(default_factory=list)  # 可選的操作按鈕列表
    task_ref: str | None = None  # 關聯的任務 ID（可選）
    metadata: dict = field(default_factory=dict)  # 擴展 metadata


# ---------------------------------------------------------------------------
# Layer 1：模式選擇 — 決定訊息以何種模式處理（取代舊版 RouterDecision）
# ---------------------------------------------------------------------------

@dataclass
class ModeSelection:
    """輕量級模式選擇模型。

    職責說明：
        使用者明確選擇任務模式或公司模式；不需要 LLM 路由。
        取代舊版的 RouterDecision（基於 LLM 的自動路由已棄用）。

    關聯關係：
        - 由 opc/layer1_perception/task_router.py 建立
        - 被 opc/engine.py 用於決定執行路徑
    """
    mode: ExecutionMode = ExecutionMode.TASK_MODE  # 執行模式（task_mode 或 company_mode）
    org_id: str | None = None  # 組織 ID（公司模式時指定）
    preferred_agent: str | None = None  # 首選代理 ID（可選）
    domains: list[str] = field(default_factory=list)  # 相關領域標籤
    company_profile: str | None = None  # 公司設定檔（"corporate" 或 "custom"）
    sub_tasks: list[Any] = field(default_factory=list)  # 子任務列表（多任務分發時）
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


# 向下相容別名：舊版程式碼可能引用 RouterDecision
RouterDecision = ModeSelection


# ---------------------------------------------------------------------------
# Layer 2：任務與組織 — 核心業務實體
# ---------------------------------------------------------------------------

# 邊界說明：
# - 在任務模式中，Task 是使用者可見的業務單元。
# - 在公司模式中，Task 僅是代理/工具/工作階段基礎設施使用的運行時執行信封。
#   公司的業務身份是 DelegationWorkItem.work_item_id，其業務狀態是
#   DelegationWorkItem.phase。
@dataclass
class Task:
    """任務資料模型 — 系統中最核心的業務實體。

    職責說明：
        代表一個可執行的工作單元。在任務模式中直接對應使用者請求；
        在公司模式中作為運行時執行容器（關聯到 DelegationWorkItem）。

    關聯關係：
        - 由 opc/engine.py 建立和管理
        - 被 opc/database/store.py 持久化
        - 被 opc/layer2_organization/ 在公司模式中關聯到工作項目
        - 被 opc/layer3_agent/ 用於執行代理工作

    使用範例：
        task = Task(title="分析報告", description="分析 Q3 銷售數據")
        task.status = TaskStatus.RUNNING
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 任務唯一 ID（UUID）
    session_id: str | None = None  # 所屬工作階段 ID
    parent_session_id: str | None = None  # 父工作階段 ID（子任務時）
    title: str = ""  # 任務標題（使用者可見）
    description: str = ""  # 任務描述（詳細說明）
    assigned_to: str = ""  # 指派的代理 ID
    status: TaskStatus = TaskStatus.PENDING  # 任務狀態
    priority: int = 5  # 優先級（1-10，5 為預設）
    dependencies: list[str] = field(default_factory=list)  # 依賴的其他任務 ID 列表
    execution_lock: bool = False  # 執行鎖（防止並發執行）
    context_snapshot: dict = field(default_factory=dict)  # 上下文快照（執行時的環境狀態）
    assigned_external_agent: str | None = None  # 指派的外部代理名稱
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    deadline: datetime | None = None  # 截止時間（可選）
    result: dict | None = None  # 執行結果
    parent_id: str | None = None  # 父任務 ID（子任務時）
    project_id: str = "default"  # 所屬專案 ID
    tags: list[str] = field(default_factory=list)  # 標籤列表
    comments: list[dict] = field(default_factory=list)  # 評論/備註列表
    retry_count: int = 0  # 已重試次數
    max_retries: int = 3  # 最大重試次數
    metadata: dict = field(default_factory=dict)  # 擴展 metadata（execution_mode 等）
    org_id: str | None = None  # 組織 ID（公司模式時）
    goal_id: str | None = None  # 關聯的目標 ID
    checkout_run_id: str | None = None  # 簽出執行 ID（並發控制）
    execution_locked_at: datetime | None = None  # 執行鎖定的時間戳記
    linked_work_item_id: str = field(default="", repr=False, compare=False)  # 關聯的工作項目 ID（公司模式）


# ---------------------------------------------------------------------------
# 自適應協調規格（Adaptive Coordination Specs）— 用於動態推斷角色/工作項目屬性
# ---------------------------------------------------------------------------

@dataclass
class AdaptiveRoleProfile:
    """自適應角色設定檔 — 由協調推斷引擎產生的角色行為特徵。

    職責說明：
        記錄系統對某個角色行為模式的推斷結果，包括執行偏好、
        審查偏好、協作風格等。用於動態調整委派策略。

    關聯關係：
        - 被 CoordinationSpec 引用
        - 由 opc/layer2_organization/ 的協調推斷邏輯產生
    """
    label: str = ""  # 角色標籤（如 "senior_dev"、"reviewer"）
    facets: list[str] = field(default_factory=list)  # 角色面向標籤列表
    authority_scope: list[str] = field(default_factory=list)  # 權限範圍（可操作的資源/領域）
    execution_bias: str = "balanced"  # 執行偏好（"aggressive"/"balanced"/"conservative"）
    review_bias: str = "balanced"  # 審查偏好（"strict"/"balanced"/"lenient"）
    collaboration_style: str = "async"  # 協作風格（"async"/"sync"/"hybrid"）
    confidence: float = 0.0  # 推斷信心值（0.0~1.0）
    evidence: list[str] = field(default_factory=list)  # 推斷依據列表


@dataclass
class AdaptiveWorkItemProfile:
    """自適應工作項目設定檔 — 由協調推斷引擎產生的工作項目特徵。

    職責說明：
        記錄系統對某个工作項目的推斷結果，包括回合類型、
        依賴分類、阻塞信號、產出物需求等。

    關聯關係：
        - 被 CoordinationSpec 引用
        - 由 opc/layer2_organization/ 的協調推斷邏輯產生
    """
    turn_kind: str = "execute"  # 回合類型（"execute"/"review"/"dispatch"）
    dependency_class: str = "hard"  # 依賴分類（"hard"=強依賴/"soft"=弱依賴）
    blocked_by_projection_ids: list[str] = field(default_factory=list)  # 被哪些投影 ID 阻塞
    blocked_by_signals: list[str] = field(default_factory=list)  # 被哪些信號阻塞
    required_artifacts: list[str] = field(default_factory=list)  # 必要產出物列表
    reads: list[str] = field(default_factory=list)  # 讀取的資源路徑
    writes: list[str] = field(default_factory=list)  # 寫入的資源路徑
    gate_owner_role_id: str = ""  # 門禁擁有者角色 ID
    soft_release_allowed: bool = False  # 是否允許軟釋放（跳過硬依賴）
    confidence: float = 0.0  # 推斷信心值（0.0~1.0）


@dataclass
class AdaptiveSignalSpec:
    """自適應信號規格 — 描述協調過程中需要等待/發出的信號。

    職責說明：
        信號是工作項目之間的同步機制。一個工作項目可能需要等待
        另一個工作項目發出特定信號才能繼續執行。

    關聯關係：
        - 被 CoordinationSpec.signals 引用
    """
    name: str  # 信號名稱（唯一標識）
    owner_role_id: str = ""  # 信號擁有者角色 ID
    required: bool = True  # 是否為必要信號（必要信號未滿足則阻塞）
    strict: bool = False  # 是否嚴格模式（嚴格模式下不可跳過）
    satisfied: bool = False  # 是否已滿足
    evidence: list[str] = field(default_factory=list)  # 滿足證據列表


@dataclass
class CoordinationSpec:
    """協調規格 — 工作項目的完整協調上下文。

    職責說明：
        整合角色設定檔、工作項目設定檔、信號、依賴關係等所有
        協調相關資訊。由協調推斷引擎產生，供調度器和門禁使用。

    關聯關係：
        - 被 opc/layer2_organization/ 的調度器和門禁系統使用
        - 包含 AdaptiveRoleProfile、AdaptiveWorkItemProfile、AdaptiveSignalSpec
    """
    version: int = 1  # 規格版本號
    inference_mode: str = "heuristic"  # 推斷模式（"heuristic"/"llm"/"manual"）
    fallback_mode: str = "conservative"  # 回退模式（推斷失敗時的策略）
    role_profile: AdaptiveRoleProfile = field(default_factory=AdaptiveRoleProfile)  # 角色設定檔
    work_item_profile: AdaptiveWorkItemProfile = field(default_factory=AdaptiveWorkItemProfile)  # 工作項目設定檔
    signals: list[AdaptiveSignalSpec] = field(default_factory=list)  # 需要等待的信號列表
    emitted_signals: list[str] = field(default_factory=list)  # 已發出的信號名稱列表
    hard_dependency_work_item_ids: list[str] = field(default_factory=list)  # 硬依賴的工作項目 ID
    soft_dependency_work_item_ids: list[str] = field(default_factory=list)  # 軟依賴的工作項目 ID
    normalized_state: str = "planned"  # 正規化狀態（"planned"/"ready"/"running"/"done"）
    notes: list[str] = field(default_factory=list)  # 推斷備註
    confidence: float = 0.0  # 整體推斷信心值（0.0~1.0）
    evidence: list[str] = field(default_factory=list)  # 推斷依據列表


# ---------------------------------------------------------------------------
# 委派執行模型（Delegation）— 公司模式下的多代理協作執行實體
# ---------------------------------------------------------------------------

@dataclass
class DelegationRun:
    """委派執行 — 一次公司模式任務的頂層執行實例。

    職責說明：
        代表一次完整的公司模式執行。每次使用者在公司模式下提交任務，
        系統會建立一個 DelegationRun，包含所有團隊、席位、工作項目。

    關聯關係：
        - 由 opc/layer2_organization/company_runtime.py 建立
        - 包含多個 DelegationCell 和 DelegationWorkItem
        - 被 opc/database/store.py 持久化
    """
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 執行唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    session_id: str = ""  # 關聯的工作階段 ID
    company_profile: str = CompanyProfile.CORPORATE.value  # 公司設定檔類型
    execution_model: str = "actor_runtime"  # 執行模型（"actor_runtime" 為當前唯一實現）
    final_decider_role_id: str = ""  # 最終決策者角色 ID
    top_level_role_ids: list[str] = field(default_factory=list)  # 頂層角色 ID 列表
    status: str = "pending"  # 執行狀態（"pending"/"running"/"done"/"failed"）
    lifecycle_status: str = "active"  # 生命週期狀態（"active"/"archived"/"deleted"）
    current_revision: int = 1  # 當前修訂版本號
    latest_deliverable_summary: str = ""  # 最新交付物摘要
    recovery_pointer: dict[str, Any] = field(default_factory=dict)  # 恢復指標（崩潰恢復用）
    project_dossier: dict[str, Any] = field(default_factory=dict)  # 專案檔案（背景資訊）
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


@dataclass
class DelegationCell:
    """委派單元 — 一個經理及其下屬成員的組織單元。

    職責說明：
        Cell 是組織拓撲中「經理 + 成員」的運行時映射。
        一個 DelegationRun 可包含多個 Cell（對應多個團隊/部門）。

    關聯關係：
        - 屬於某個 DelegationRun
        - 包含多個 DelegationWorkItem
    """
    cell_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 單元唯一 ID
    run_id: str = ""  # 所屬執行 ID
    manager_role_id: str = ""  # 經理角色 ID
    member_role_ids: list[str] = field(default_factory=list)  # 成員角色 ID 列表
    status: str = "idle"  # 單元狀態（"idle"/"active"/"done"）
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


# 公司模式邊界說明：
# DelegationWorkItem 是使用者/業務層級的工作單元。結構化的
# work_item_runtime_links 資料表擁有運行時 Task 投影關係。
@dataclass
class DelegationWorkItem:
    """委派工作項目 — 公司模式中的核心業務實體。

    職責說明：
        代表一個被委派給特定角色/席位的工作單元。與 Task 不同，
        DelegationWorkItem 是公司模式中使用者可見的業務身份，
        其業務狀態由 phase（Phase 列舉）驅動。

    關聯關係：
        - 由 opc/layer2_organization/ 的委派邏輯建立
        - 屬於某個 DelegationRun 和 DelegationCell
        - 透過 work_item_runtime_links 映射到 Task（運行時投影）
        - 被 opc/database/store.py 持久化

    使用範例：
        wi = DelegationWorkItem(title="實作登入功能", kind="execute", phase=Phase.READY)
    """
    work_item_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 工作項目唯一 ID
    run_id: str = ""  # 所屬委派執行 ID
    cell_id: str = ""  # 所屬委派單元 ID
    team_instance_id: str = ""  # 團隊實例 ID
    team_id: str = ""  # 團隊 ID（拓撲定義）
    role_id: str = ""  # 執行角色 ID
    seat_id: str = ""  # 席位 ID
    seat_state_id: str = ""  # 席位狀態 ID
    role_runtime_session_id: str = ""  # 角色運行時工作階段 ID
    parent_work_item_id: str | None = None  # 父工作項目 ID（子任務時）
    source_role_id: str | None = None  # 來源角色 ID（委派者）
    source_seat_id: str | None = None  # 來源席位 ID
    title: str = ""  # 工作項目標題
    summary: str = ""  # 工作項目摘要
    kind: str = "execute"  # 工作類型（"execute"/"review"/"dispatch"/"synthesize"）
    projection_id: str = ""  # 運行時投影 ID（對應的 Task ID）
    phase: Phase = Phase.READY  # 當前階段（Phase 狀態機）
    batch_id: str = ""  # 批次 ID（同批委派的工作項目共享）
    batch_index: int = 0  # 批次內索引
    deliverable_summary: str = ""  # 交付物摘要
    blocked_reason: str = ""  # 阻塞原因
    handoff_status: str = "pending"  # 交接狀態（"pending"/"sent"/"accepted"）
    continuation_source: str = ""  # 接續來源（重新執行時）
    manager_role_id: str = ""  # 經理角色 ID
    manager_seat_id: str = ""  # 經理席位 ID
    claimed_by_role_runtime_session_id: str = ""  # 認領者的角色運行時工作階段 ID
    claimed_by_seat_id: str = ""  # 認領者的席位 ID
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間

    def __post_init__(self) -> None:
        """初始化後處理：確保 phase 為 Phase 列舉實例，metadata 為字典。"""
        if not isinstance(self.phase, Phase):
            try:
                self.phase = Phase(str(self.phase or Phase.READY.value))
            except ValueError:
                self.phase = Phase.READY
        self.metadata = dict(self.metadata or {})


@dataclass
class DelegationEvent:
    """委派事件 — 記錄委派執行過程中發生的事件。

    職責說明：
        用於審計追蹤和事件溯源。記錄工作項目的建立、狀態變更、
        分發、完成等關鍵事件。

    關聯關係：
        - 由 opc/layer2_organization/ 的各種 Hook 產生
        - 被 opc/database/store.py 持久化
    """
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 事件唯一 ID
    run_id: str = ""  # 所屬執行 ID
    work_item_id: str | None = None  # 關聯的工作項目 ID
    cell_id: str | None = None  # 關聯的單元 ID
    role_id: str | None = None  # 關聯的角色 ID
    event_type: str = "created"  # 事件類型（"created"/"dispatched"/"completed"/"failed" 等）
    payload: dict[str, Any] = field(default_factory=dict)  # 事件酬載
    created_at: datetime = field(default_factory=_utcnow)  # 事件時間


# ---------------------------------------------------------------------------
# 運行時工作階段（Runtime Sessions）— 角色/成員的執行狀態容器
# ---------------------------------------------------------------------------

@dataclass
class RoleRuntimeSession:
    """角色運行時工作階段 — 角色在委派執行中的運行時狀態。

    職責說明：
        每個角色在每次委派執行中擁有一個 RoleRuntimeSession，
        記錄當前聚焦的工作項目、收件匣狀態、記憶切片等運行時資訊。
        是調度器分發工作的依據。

    關聯關係：
        - 由 opc/layer2_organization/company_runtime.py 管理
        - 關聯到 SeatState 和 CompanyMemberSession
        - 被 opc/database/store.py 持久化
    """
    role_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 工作階段唯一 ID
    run_id: str = ""  # 所屬委派執行 ID
    project_id: str = "default"  # 所屬專案 ID
    team_instance_id: str = ""  # 團隊實例 ID
    team_id: str = ""  # 團隊 ID
    role_id: str = ""  # 角色 ID
    seat_id: str = ""  # 席位 ID
    seat_state_id: str = ""  # 席位狀態 ID
    employee_id: str = ""  # 員工 ID
    focused_work_item_id: str = ""  # 當前聚焦的工作項目 ID
    background_work_item_ids: list[str] = field(default_factory=list)  # 背景工作項目 ID 列表
    manager_role_ids: list[str] = field(default_factory=list)  # 上級經理角色 ID 列表
    manager_seat_ids: list[str] = field(default_factory=list)  # 上級經理席位 ID 列表
    seat_ids: list[str] = field(default_factory=list)  # 關聯的席位 ID 列表
    adapter_session_state: dict[str, Any] = field(default_factory=dict)  # 適配器工作階段狀態
    inbox_state: dict[str, Any] = field(default_factory=dict)  # 收件匣狀態
    memory_slices_by_work_item: dict[str, list[str]] = field(default_factory=dict)  # 按工作項目分組的記憶切片
    resume_state: dict[str, Any] = field(default_factory=dict)  # 恢復狀態（中斷續行用）
    current_work_item: dict[str, Any] = field(default_factory=dict)  # 當前工作項目快照
    latest_notification: dict[str, Any] = field(default_factory=dict)  # 最新通知
    manager_digest: dict[str, Any] = field(default_factory=dict)  # 經理摘要
    status: str = "idle"  # 工作階段狀態（"idle"/"running"/"blocked"）
    # Fix 5 PR3：等待此角色工作階段的 FIFO 工作項目 ID 佇列。
    # 角色一次只執行一個工作項目（focused_work_item_id）；
    # 同一角色的新可執行工作由 enqueue_session_work_on_runnable_hook 附加到此。
    # 當聚焦清除時，clear_session_focus_on_terminal_hook 取出佇列頭部並通知調度器。
    # 由 OrgConfig.role_serial_queue_enabled 控制（公司模式預設啟用）。
    pending_work_item_ids: list[str] = field(default_factory=list)  # 待處理工作項目 ID 佇列
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


# 向下相容別名：舊版程式碼可能引用 DelegationRoleSession
DelegationRoleSession = RoleRuntimeSession


@dataclass
class CompanyMemberSession:
    """公司成员工作階段 — 成員（員工）在席位中的詳細運行時狀態。

    職責說明：
        比 RoleRuntimeSession 更細粒度，記錄成員的收件匣佇列、
        工作記憶、協議積壓、通知積壓等。是 Agent 執行回合的上下文來源。

    關聯關係：
        - 由 opc/layer2_organization/company_runtime.py 管理
        - 被 opc/layer3_agent/ 在組裝 Prompt 時讀取
        - 被 opc/database/store.py 持久化
    """
    member_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 成員工作階段唯一 ID
    role_session_id: str = ""  # 關聯的角色工作階段 ID
    team_instance_id: str = ""  # 團隊實例 ID
    team_id: str = ""  # 團隊 ID
    role_id: str = ""  # 角色 ID
    seat_id: str = ""  # 席位 ID
    seat_state_id: str = ""  # 席位狀態 ID
    employee_id: str = ""  # 員工 ID
    status: str = "idle"  # 工作階段狀態
    resident_status: str = "idle"  # 常駐狀態（"idle"/"active"/"suspended"）
    current_task_id: str = ""  # 當前任務 ID
    focused_work_item_id: str = ""  # 當前聚焦的工作項目 ID
    background_work_item_ids: list[str] = field(default_factory=list)  # 背景工作項目 ID 列表
    inbox_cursor: int = 0  # 收件匣游標（已讀位置）
    working_memory: list[str] = field(default_factory=list)  # 工作記憶（短期上下文）
    memory_slices_by_work_item: dict[str, list[str]] = field(default_factory=dict)  # 按工作項目分組的記憶切片
    resume_state: dict[str, Any] = field(default_factory=dict)  # 恢復狀態
    adapter_session_state: dict[str, Any] = field(default_factory=dict)  # 適配器工作階段狀態
    pending_inbox: list[dict[str, Any]] = field(default_factory=list)  # 待處理收件匣訊息
    queued_inbox: list[dict[str, Any]] = field(default_factory=list)  # 已排隊收件匣訊息
    actionable_chat: list[dict[str, Any]] = field(default_factory=list)  # 可操作的聊天訊息
    protocol_backlog: list[dict[str, Any]] = field(default_factory=list)  # 協議積壓（結構化訊息）
    notification_backlog: list[dict[str, Any]] = field(default_factory=list)  # 通知積壓
    actionable_inbox_count: int = 0  # 可操作收件匣數量
    protocol_backlog_count: int = 0  # 協議積壓數量
    notification_backlog_count: int = 0  # 通知積壓數量
    latest_notification: dict[str, Any] = field(default_factory=dict)  # 最新通知
    manager_role_id: str = ""  # 上級經理角色 ID
    manager_role_ids: list[str] = field(default_factory=list)  # 上級經理角色 ID 列表
    inbox_state: dict[str, Any] = field(default_factory=dict)  # 收件匣狀態
    current_turn_mode: str = ""  # 當前回合模式（"execute"/"review"/"dispatch" 等）
    current_assignment: dict[str, Any] = field(default_factory=dict)  # 當前指派資訊
    current_work_item: dict[str, Any] = field(default_factory=dict)  # 當前工作項目快照
    manager_digest: dict[str, Any] = field(default_factory=dict)  # 經理摘要
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


# ---------------------------------------------------------------------------
# 團隊與席位（Team & Seat）— 組織拓撲的運行時實例化
# ---------------------------------------------------------------------------

@dataclass
class TeamInstance:
    """團隊實例 — 組織拓撲中團隊的運行時實例。

    職責說明：
        每次委派執行會將組織拓撲中的團隊實例化為 TeamInstance，
        包含該團隊的所有席位和角色。

    關聯關係：
        - 屬於某個 DelegationRun
        - 包含多個 SeatState
    """
    team_instance_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 團隊實例唯一 ID
    run_id: str = ""  # 所屬委派執行 ID
    project_id: str = "default"  # 所屬專案 ID
    team_id: str = ""  # 團隊 ID（拓撲定義）
    session_id: str = ""  # 關聯的工作階段 ID
    status: str = "pending"  # 團隊狀態（"pending"/"active"/"done"）
    seat_ids: list[str] = field(default_factory=list)  # 席位 ID 列表
    role_ids: list[str] = field(default_factory=list)  # 角色 ID 列表
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


@dataclass
class SeatState:
    """席位狀態 — 組織中席位的運行時狀態。

    職責說明：
        席位（Seat）是角色在團隊中的「位置」。SeatState 記錄了
        該席位當前由誰佔用、正在處理什麼工作、上級是誰等資訊。

    關聯關係：
        - 屬於某個 TeamInstance
        - 關聯到 RoleRuntimeSession 和 CompanyMemberSession
        - 被 opc/database/store.py 持久化
    """
    seat_state_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 席位狀態唯一 ID
    team_instance_id: str = ""  # 所屬團隊實例 ID
    run_id: str = ""  # 所屬委派執行 ID
    project_id: str = "default"  # 所屬專案 ID
    team_id: str = ""  # 團隊 ID
    seat_id: str = ""  # 席位 ID
    role_id: str = ""  # 角色 ID
    employee_id: str = ""  # 佔用此席位的員工 ID
    member_session_id: str = ""  # 關聯的成員工作階段 ID
    role_runtime_session_id: str = ""  # 關聯的角色運行時工作階段 ID
    status: str = "idle"  # 席位狀態（"idle"/"occupied"/"blocked"）
    resident_status: str = "idle"  # 常駐狀態
    current_task_id: str = ""  # 當前任務 ID
    current_work_item_id: str = ""  # 當前工作項目 ID
    manager_role_id: str = ""  # 上級經理角色 ID
    manager_seat_id: str = ""  # 上級經理席位 ID
    manager_role_ids: list[str] = field(default_factory=list)  # 上級經理角色 ID 列表
    manager_seat_ids: list[str] = field(default_factory=list)  # 上級經理席位 ID 列表
    inbox_state: dict[str, Any] = field(default_factory=dict)  # 收件匣狀態
    resume_state: dict[str, Any] = field(default_factory=dict)  # 恢復狀態
    current_work_item: dict[str, Any] = field(default_factory=dict)  # 當前工作項目快照
    latest_notification: dict[str, Any] = field(default_factory=dict)  # 最新通知
    manager_digest: dict[str, Any] = field(default_factory=dict)  # 經理摘要
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


# ---------------------------------------------------------------------------
# 審查與驗證（Review & Verification）— 工作項目的品質把關模型
# ---------------------------------------------------------------------------

@dataclass
class StructuredReviewVerdict:
    """結構化審查裁決 — 經理對工作項目的審查結果。

    職責說明：
        經理審查工作項目後產生的結構化裁決，包含核准/駁回標籤、
        摘要、阻塞問題和後續行動。

    關聯關係：
        - 由 opc/layer2_organization/ 的審查邏輯產生
        - 套用到 DelegationWorkItem 的 phase 轉換
    """
    label: Literal["approve", "reject"] = "reject"  # 裁決標籤（核准/駁回）
    summary: str = ""  # 裁決摘要
    blocking_issues: list[str] = field(default_factory=list)  # 阻塞問題列表（駁回原因）
    followups: list[str] = field(default_factory=list)  # 後續行動建議


@dataclass
class ArtifactContract:
    """產出物合約 — 定義工作項目預期產出的結構化合約。

    職責說明：
        描述一個工作項目應該產生什麼產出物、寫入範圍、
        下游消費者等。用於驗證工作完成度。

    關聯關係：
        - 由委派邏輯在建立工作項目時設定
        - 被驗證邏輯用於檢查產出物完整性
    """
    summary: str = ""  # 合約摘要
    write_scope: str = ""  # 寫入範圍（允許寫入的路徑/資源）
    expected_artifacts: list[str] = field(default_factory=list)  # 預期產出物列表
    downstream_consumer: list[str] = field(default_factory=list)  # 下游消費者（依賴此產出的角色/工作項目）
    allowed_collaboration_targets: list[str] = field(default_factory=list)  # 允許協作的目標
    status: str = "pending"  # 合約狀態（"pending"/"fulfilled"/"violated"）
    issues: list[str] = field(default_factory=list)  # 問題列表


@dataclass
class VerificationEvidence:
    """驗證證據 — 工作項目完成後的驗證結果。

    職責說明：
        記錄對工作項目產出物的驗證結果，包括各項檢查、
        判定結果和原始輸出。

    關聯關係：
        - 由 opc/layer2_organization/ 的驗證邏輯產生
        - 作為審查裁決的依據
    """
    status: str = "missing"  # 驗證狀態（"missing"/"passed"/"failed"/"partial"）
    verdict: str = ""  # 判定結果
    summary: str = ""  # 驗證摘要
    checks: list[dict[str, Any]] = field(default_factory=list)  # 各項檢查結果
    raw_output: str = ""  # 原始輸出（如測試日誌）


@dataclass
class EnvironmentManifest:
    """環境清單 — 設定工作項目產生的環境狀態結構化記錄。

    職責說明：
        記錄執行環境的詳細資訊：平台、已安裝工具、環境變數、
        運行時類型、GPU 狀態等。供後續工作項目使用。

    關聯關係：
        - 由環境設定工作項目產生
        - 被後續執行工作項目讀取
    """
    platform: str = ""  # 平台標識（如 "windows"、"linux"）
    tools_installed: list[dict[str, Any]] = field(default_factory=list)  # 已安裝工具列表
    env_vars: dict[str, str] = field(default_factory=dict)  # 環境變數
    runtime_type: str = "native"  # 運行時類型（"native"/"venv"/"conda"/"docker"）
    runtime_path: str = ""  # 運行時路徑
    activate_command: str = ""  # 啟動命令（如 "source venv/bin/activate"）
    shell_prefix: str = ""  # Shell 前綴（Linux/macOS）
    shell_prefix_win: str = ""  # Shell 前綴（Windows）
    gpu_available: bool = False  # GPU 是否可用
    gpu_info: str = ""  # GPU 資訊
    verification_checks: list[dict[str, Any]] = field(default_factory=list)  # 驗證檢查（Linux/macOS）
    verification_checks_win: list[dict[str, Any]] = field(default_factory=list)  # 驗證檢查（Windows）
    notes: str = ""  # 備註


@dataclass
class WorkspaceManifest:
    """工作區清單 — 為運行時準備的共享工作區結構化記錄。

    職責說明：
        記錄共享工作區的根路徑、保留路徑、狀態等。

    關聯關係：
        - 由工作區準備邏輯產生
        - 被所有工作項目讀取
    """
    root_path: str = ""  # 工作區根路徑
    manifest_path: str = ""  # 清單檔案路徑
    reserved_paths: dict[str, str] = field(default_factory=dict)  # 保留路徑映射（名稱→路徑）
    status: str = "ready"  # 工作區狀態（"ready"/"preparing"/"error"）
    notes: list[str] = field(default_factory=list)  # 備註列表


@dataclass
class DataAcquisitionReport:
    """資料獲取報告 — 任務關鍵外部輸入的結構化就緒報告。

    職責說明：
        記錄資料獲取工作項目的結果：哪些輸入已就位、哪些缺失、
        嘗試了哪些來源和工具等。

    關聯關係：
        - 由資料獲取工作項目產生
        - 被後續工作項目用於判斷是否可以繼續
    """
    status: str = "missing_critical"  # 狀態（"ready"/"missing_critical"/"partial"）
    designated_input_dir: str = ""  # 指定的輸入目錄
    required_inputs: list[str] = field(default_factory=list)  # 必要輸入列表
    present_inputs: list[str] = field(default_factory=list)  # 已就位的輸入
    missing_inputs: list[str] = field(default_factory=list)  # 缺失的輸入
    attempted_sources: list[str] = field(default_factory=list)  # 已嘗試的來源
    attempted_tools: list[str] = field(default_factory=list)  # 已嘗試的工具
    prepared_assets: list[str] = field(default_factory=list)  # 已準備的資產
    blocked_reasons: list[str] = field(default_factory=list)  # 阻塞原因
    acquisition_attempted: bool = False  # 是否已嘗試獲取
    report_path: str = ""  # 報告檔案路徑
    log_path: str = ""  # 日誌檔案路徑
    source_candidates_path: str = ""  # 來源候選路徑
    download_manifest_path: str = ""  # 下載清單路徑
    provenance_summary: str = ""  # 來源摘要
    notes: list[str] = field(default_factory=list)  # 備註列表


# ---------------------------------------------------------------------------
# 招聘系統（Recruitment）— 公司模式啟動前的員工配置決策
# ---------------------------------------------------------------------------

@dataclass
class RecruitmentNeed:
    """招聘需求 — 描述某個角色的人員配置需求。

    職責說明：
        招聘者在執行進入組織前運行一次，每個需求對應拓撲中的一個角色。
        描述該角色需要什麼樣的員工來填補。

    關聯關係：
        - 由 opc/layer2_organization/ 的招聘邏輯產生
        - 被 RecruitmentProposal 引用
    """
    role_id: str  # 角色 ID
    role_name: str = ""  # 角色名稱
    role_responsibility: str = ""  # 角色職責描述
    request_text: str = ""  # 招聘請求文字
    domains: list[str] = field(default_factory=list)  # 相關領域
    existing_employee_ids: list[str] = field(default_factory=list)  # 現有員工 ID 列表


@dataclass
class RecruitmentCandidateRecommendation:
    """招聘候選人推薦 — 從模板庫推薦的候選員工。

    職責說明：
        當沒有合適的現有員工時，從員工模板庫中推薦的候選人。

    關聯關係：
        - 被 RecruitmentProposal.candidate 引用
    """
    template_id: str  # 員工模板 ID
    template_name: str  # 模板名稱
    category: str = ""  # 模板分類
    domains: list[str] = field(default_factory=list)  # 相關領域
    prompt_ref: str = ""  # Prompt 引用路徑
    preferred_external_agent: str | None = None  # 首選外部代理
    source_path: str = ""  # 模板來源路徑
    rationale: str = ""  # 推薦理由
    proposed_employee_name: str = ""  # 提議的員工名稱
    proposed_employee_id: str = ""  # 提議的員工 ID
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


@dataclass
class RecruitmentEmployeeRecommendation:
    """招聘現有員工推薦 — 從現有員工中推薦的候選人。

    職責說明：
        當有合適的現有員工時，推薦其填補角色。

    關聯關係：
        - 被 RecruitmentProposal.existing_employee 引用
    """
    employee_id: str  # 員工 ID
    employee_name: str  # 員工名稱
    role_id: str  # 目標角色 ID
    category: str = ""  # 員工分類
    domains: list[str] = field(default_factory=list)  # 相關領域
    learned_skill_refs: list[str] = field(default_factory=list)  # 已學技能引用
    experience_score: float = 0.0  # 經驗分數
    rationale: str = ""  # 推薦理由
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


@dataclass
class RecruitmentProposal:
    """招聘提案 — 針對單一角色的人員配置決策。

    職責說明：
        招聘者對某個角色的最終決策：使用現有員工、招聘新員工、
        僅使用角色（無特定員工）、或直接由角色執行。

    關聯關係：
        - 被 RecruitmentPlan.proposals 引用
        - 包含 RecruitmentCandidateRecommendation 或 RecruitmentEmployeeRecommendation
    """
    role_id: str  # 目標角色 ID
    status: Literal["existing_staff", "proposed_hire", "fallback_role_only", "direct_role_execution"] = "fallback_role_only"  # 決策狀態
    rationale: str = ""  # 決策理由
    role_labels: list[str] = field(default_factory=list)  # 角色標籤
    candidate: RecruitmentCandidateRecommendation | None = None  # 新員工候選人（proposed_hire 時）
    existing_employee: RecruitmentEmployeeRecommendation | None = None  # 現有員工推薦（existing_staff 時）
    existing_employee_ids: list[str] = field(default_factory=list)  # 現有員工 ID 列表
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


@dataclass
class RecruitmentPlan:
    """招聘計劃 — 一次公司模式啟動的完整人員配置計劃。

    職責說明：
        包含所有角色的招聘提案、招聘者回饋和摘要。

    關聯關係：
        - 由 opc/layer2_organization/ 的招聘邏輯產生
        - 被 company_runtime 用於初始化員工配置
    """
    company_profile: str = CompanyProfile.CORPORATE.value  # 公司設定檔類型
    proposals: list[RecruitmentProposal] = field(default_factory=list)  # 招聘提案列表
    recruiter_feedback: list[str] = field(default_factory=list)  # 招聘者回饋
    summary: str = ""  # 計劃摘要
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


# ---------------------------------------------------------------------------
# 組織重組（Reorg）— 執行中的組織拓撲變更提案
# ---------------------------------------------------------------------------

@dataclass
class ReorgRoleChange:
    """重組角色變更 — 描述對組織拓撲中角色的異動。

    職責說明：
        描述新增、移除、替換或更新角色的具體操作。

    關聯關係：
        - 被 ReorgChangeSet.role_changes 引用
    """
    action: Literal["add", "remove", "replace", "update"] = "update"  # 變更動作
    role_id: str = ""  # 目標角色 ID
    replacement_role_id: str | None = None  # 替換角色 ID（replace 時）
    role: dict[str, Any] = field(default_factory=dict)  # 角色定義（add/update 時）
    reason: str = ""  # 變更原因


@dataclass
class ReorgTaskAdjustment:
    """重組任務調整 — 描述對現有任務的異動。

    職責說明：
        組織重組時可能需要重新指派任務、調整優先級、更新描述等。

    關聯關係：
        - 被 ReorgChangeSet.task_adjustments 引用
    """
    task_id: str = ""  # 目標任務 ID
    action: Literal["reassign", "reprioritize", "update_description", "append_acceptance_criteria", "request_review"] = "reassign"  # 調整動作
    new_role_id: str | None = None  # 新指派角色 ID（reassign 時）
    priority: int | None = None  # 新優先級（reprioritize 時）
    description_append: str = ""  # 描述附加文字
    acceptance_criteria: list[str] = field(default_factory=list)  # 驗收標準
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


@dataclass
class ReorgChangeSet:
    """重組變更集 — 一次重組提案的所有變更集合。

    職責說明：
        包含角色變更和任務調整兩大部分。

    關聯關係：
        - 被 ReorgProposal.changeset 引用
    """
    role_changes: list[ReorgRoleChange] = field(default_factory=list)  # 角色變更列表
    task_adjustments: list[ReorgTaskAdjustment] = field(default_factory=list)  # 任務調整列表
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


@dataclass
class ReorgMigrationPlan:
    """重組遷移計劃 — 描述如何安全地從舊組織遷移到新組織。

    職責說明：
        包含受影響的任務、檢查點、交接、角色映射、
        需要作廢的等待、相容性警告和回滾快照。

    關聯關係：
        - 被 ReorgProposal.migration_plan 引用
    """
    affected_task_ids: list[str] = field(default_factory=list)  # 受影響的任務 ID
    affected_checkpoint_ids: list[str] = field(default_factory=list)  # 受影響的檢查點 ID
    affected_handoff_ids: list[str] = field(default_factory=list)  # 受影響的交接 ID
    role_mapping: dict[str, str] = field(default_factory=dict)  # 角色映射（舊→新）
    invalidated_waits: list[str] = field(default_factory=list)  # 需要作廢的等待
    migration_notes: list[str] = field(default_factory=list)  # 遷移備註
    compatibility_warnings: list[str] = field(default_factory=list)  # 相容性警告
    rollback_snapshot_id: str | None = None  # 回滾快照 ID
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


@dataclass
class OrgSnapshot:
    """組織快照 — 組織拓撲在某個時間點的完整快照。

    職責說明：
        用於重組前的備份和回滾。記錄組織版本、拓撲、角色列表等。

    關聯關係：
        - 由重組邏輯在變更前建立
        - 被 opc/database/store.py 持久化
    """
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 快照唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    org_version: int = 1  # 組織版本號
    runtime_topology_version: int = 1  # 運行時拓撲版本號
    company_name: str = ""  # 公司名稱
    topology: str = ""  # 拓撲定義（YAML/JSON 字串）
    roles: list[dict[str, Any]] = field(default_factory=list)  # 角色定義列表
    company_profile: str = CompanyProfile.CORPORATE.value  # 公司設定檔類型
    active_tasks: list[dict[str, Any]] = field(default_factory=list)  # 活動任務列表
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class ReorgProposal:
    """重組提案 — 一次組織重組的完整提案。

    職責說明：
        包含重組的範圍、風險等級、變更集、遷移計劃、審批狀態等。
        需要使用者確認後才能套用。

    關聯關係：
        - 由 opc/layer2_organization/ 的重組邏輯產生
        - 被 opc/database/store.py 持久化
        - 包含 ReorgChangeSet 和 ReorgMigrationPlan
    """
    proposal_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 提案唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    session_id: str | None = None  # 關聯的工作階段 ID
    task_id: str | None = None  # 觸發重組的任務 ID
    initiated_by: str = "owner"  # 發起者（"owner"/"agent"/"system"）
    source_role_id: str = ""  # 發起重組的角色 ID
    scope: ReorgScope = ReorgScope.ORG_MUTATION  # 重組範圍
    risk_level: ReorgRiskLevel = ReorgRiskLevel.MEDIUM  # 風險等級
    status: ReorgProposalStatus = ReorgProposalStatus.PROPOSED  # 提案狀態
    title: str = ""  # 提案標題
    summary: str = ""  # 提案摘要
    rationale: str = ""  # 重組理由
    user_confirmation_required: bool = True  # 是否需要使用者確認
    old_org_version: int = 1  # 舊組織版本號
    new_org_version: int = 1  # 新組織版本號
    old_runtime_topology_version: int = 1  # 舊運行時拓撲版本號
    new_runtime_topology_version: int = 1  # 新運行時拓撲版本號
    changeset: ReorgChangeSet = field(default_factory=ReorgChangeSet)  # 變更集
    migration_plan: ReorgMigrationPlan = field(default_factory=ReorgMigrationPlan)  # 遷移計劃
    impact_summary: dict[str, Any] = field(default_factory=dict)  # 影響摘要
    approval_notes: str = ""  # 審批備註
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


# ---------------------------------------------------------------------------
# 執行結果與決策（Results & Decisions）— 任務執行和審批的結果模型
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    """任務結果 — 任務執行完成後的結果封裝。

    職責說明：
        封裝任務執行的最終狀態、內容、產出物、升級資訊和成本。

    關聯關係：
        - 由 opc/layer3_agent/ 的執行邏輯產生
        - 被 opc/engine.py 處理
    """
    status: TaskStatus  # 最終任務狀態
    content: str = ""  # 結果內容（文字摘要）
    artifacts: dict = field(default_factory=dict)  # 產出物（路徑/內容映射）
    escalation: dict | None = None  # 升級資訊（需要人工介入時）
    cost: float = 0.0  # 執行成本（USD）
    token_usage: dict = field(default_factory=dict)  # Token 使用統計


@dataclass
class ApprovalDecision:
    """審批決策 — 審批系統對工具調用的裁決。

    職責說明：
        當代理嘗試執行需要審批的操作時，審批系統產生的決策結果。

    關聯關係：
        - 由 opc/layer2_organization/approval.py 產生
        - 被 opc/layer4_tools/ 的工具執行邏輯使用
    """
    action: ApprovalAction  # 審批動作（auto_approve/escalate/reject/require_input）
    risk_level: RiskLevel  # 風險等級
    rationale: str  # 決策理由
    confidence: float = 0.0  # 決策信心值（0.0~1.0）
    requires_user_input: bool = False  # 是否需要使用者輸入
    policy_source: str = "heuristic"  # 策略來源（"heuristic"/"llm"/"rule"）
    suggested_response: str = ""  # 建議回覆
    metadata: dict = field(default_factory=dict)  # 擴展 metadata


@dataclass
class RuntimePermissionDecision:
    """運行時權限決策 — 對工具/操作權限的即時裁決。

    職責說明：
        比 ApprovalDecision 更輕量，用於運行時的快速權限判斷。

    關聯關係：
        - 由 opc/layer2_organization/ 的權限邏輯產生
    """
    resolution: PermissionResolution  # 權限裁決（allow/ask/deny）
    scope: PermissionScope = PermissionScope.ONCE  # 權限範圍（once/session/project/global）
    risk_level: RiskLevel = RiskLevel.LOW  # 風險等級
    rationale: str = ""  # 裁決理由
    source: str = "runtime"  # 裁決來源
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


@dataclass
class ModelCapabilitySet:
    """模型能力集 — 描述 LLM 模型支援的功能。

    職責說明：
        記錄特定 LLM 模型的能力（串流、工具調用、思考、多模態等），
        用於運行時選擇合適的模型和適配器。

    關聯關係：
        - 由 opc/layer3_agent/adapters/ 使用
        - 被 opc/engine.py 在模型選擇時參考
    """
    model: str  # 模型名稱/ID
    supports_streaming: bool = True  # 是否支援串流輸出
    supports_tool_calling: bool = True  # 是否支援工具調用
    supports_streaming_tool_calls: bool = True  # 是否支援串流工具調用
    supports_thinking: bool = False  # 是否支援思考模式（如 Claude 的 extended thinking）
    supports_multimodal: bool = False  # 是否支援多模態（圖片等）
    supports_documents: bool = False  # 是否支援文件輸入
    supports_video: bool = False  # 是否支援影片輸入
    provider_family: str = ""  # 提供者家族（"openai"/"anthropic"/"google" 等）
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


@dataclass
class RuntimeLLMEvent:
    """運行時 LLM 事件 — 記錄 LLM 調用過程中的事件。

    職責說明：
        用於追蹤和除錯 LLM 調用過程（開始、完成、錯誤等）。

    關聯關係：
        - 由 opc/layer3_agent/ 的適配器產生
    """
    event_type: str  # 事件類型（"start"/"chunk"/"complete"/"error"）
    payload: dict[str, Any] = field(default_factory=dict)  # 事件酬載
    model: str = ""  # 使用的模型名稱
    timestamp: datetime = field(default_factory=_utcnow)  # 事件時間


@dataclass
class ExternalSession:
    """外部工作階段 — 外部代理（如 Claude Code、Cursor）的工作階段。

    職責說明：
        追蹤外部代理的工作階段狀態，包括工作區路徑、運行模式等。

    關聯關係：
        - 由 opc/layer3_agent/external_broker.py 管理
        - 被 opc/database/store.py 持久化
    """
    agent_type: str  # 外部代理類型（"claude_code"/"cursor"/"custom"）
    project_id: str = "default"  # 所屬專案 ID
    session_id: str = ""  # 外部工作階段 ID
    opc_session_id: str | None = None  # 對應的 OPC 工作階段 ID
    task_id: str | None = None  # 關聯的任務 ID
    workspace_path: str = ""  # 工作區路徑
    run_mode: str = "batch"  # 運行模式（"batch"/"interactive"）
    status: str = "unknown"  # 工作階段狀態
    metadata: dict = field(default_factory=dict)  # 擴展 metadata
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


@dataclass
class ExecutionCheckpoint:
    """執行檢查點 — 任務執行過程中的狀態快照。

    職責說明：
        用於任務的中斷恢復。在關鍵節點儲存執行狀態，
        崩潰或中斷後可從最近的檢查點恢復。

    關聯關係：
        - 由 opc/engine.py 在關鍵節點建立
        - 被 opc/database/store.py 持久化
    """
    checkpoint_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 檢查點唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    session_id: str | None = None  # 關聯的工作階段 ID
    checkpoint_type: str = ""  # 檢查點類型（"task_start"/"tool_result"/"phase_change" 等）
    status: str = "pending"  # 檢查點狀態
    task_id: str | None = None  # 關聯的任務 ID
    payload: dict[str, Any] = field(default_factory=dict)  # 狀態快照酬載
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


# ---------------------------------------------------------------------------
# Layer 2：代理間通訊（Inter-Agent Communication）
# ---------------------------------------------------------------------------


@dataclass
class AgentEndpointRef:
    """代理端點引用 — 標識訊息的發送者或接收者。

    職責說明：
        統一標識公司角色、原生子代理或外部代理的端點。

    關聯關係：
        - 被 CommsEnvelope 的 from_endpoint/to_endpoint 使用
    """
    endpoint_id: str  # 端點唯一 ID
    endpoint_type: AgentEndpointType = AgentEndpointType.COMPANY_ROLE  # 端點類型
    role_id: str = ""  # 角色 ID（company_role 時）
    task_id: str = ""  # 任務 ID
    projection_id: str = ""  # 投影 ID
    session_id: str = ""  # 工作階段 ID


@dataclass
class CommsEnvelope:
    """通訊信封 — 代理間訊息的標準化封裝。

    職責說明：
        所有代理間通訊（私訊、廣播、會議、系統通知）的統一載體。
        包含傳輸類型、語義類型、狀態、發送/接收端點等。

    關聯關係：
        - 由 opc/layer2_organization/comms.py 管理
        - 被 opc/database/store.py 持久化
    """
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 訊息唯一 ID
    session_id: str = ""  # 所屬工作階段 ID
    project_id: str = "default"  # 所屬專案 ID
    task_id: str = ""  # 關聯的任務 ID
    projection_id: str = ""  # 關聯的投影 ID
    transport_kind: CommsTransportKind = CommsTransportKind.DM  # 傳輸類型（dm/broadcast/meeting/system）
    semantic_type: CommsSemanticType = CommsSemanticType.WORK_UPDATE  # 語義類型
    state: CommsState = CommsState.OPEN  # 訊息狀態
    from_endpoint: AgentEndpointRef = field(default_factory=lambda: AgentEndpointRef(endpoint_id=""))  # 發送端點
    to_endpoint: AgentEndpointRef = field(default_factory=lambda: AgentEndpointRef(endpoint_id=""))  # 接收端點
    subject: str = ""  # 訊息主旨
    content: str = ""  # 訊息內容
    artifact_refs: list[str] = field(default_factory=list)  # 產出物引用
    refs: dict[str, Any] = field(default_factory=dict)  # 關聯引用
    transport_metadata: dict[str, Any] = field(default_factory=dict)  # 傳輸 metadata
    payload: dict[str, Any] = field(default_factory=dict)  # 結構化酬載
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


@dataclass
class ResidentAssignmentEnvelope:
    """常駐指派信封 — 將工作項目指派給常駐成員的完整上下文。

    職責說明：
        當工作項目被分發給席位中的常駐成員時，包含完整的執行上下文：
        工作項目資訊、寫入範圍、依賴快照、收件匣訊息等。

    關聯關係：
        - 由 opc/layer2_organization/ 的調度邏輯建立
        - 被 opc/layer3_agent/ 用於組裝執行上下文
    """
    assignment_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 指派唯一 ID
    member_session_id: str = ""  # 成員工作階段 ID
    team_instance_id: str = ""  # 團隊實例 ID
    team_id: str = ""  # 團隊 ID
    seat_id: str = ""  # 席位 ID
    seat_state_id: str = ""  # 席位狀態 ID
    role_runtime_session_id: str = ""  # 角色運行時工作階段 ID
    work_item_projection_id: str = ""  # 工作項目投影 ID
    work_item_turn_type: str = ""  # 工作項目回合類型
    role_id: str = ""  # 角色 ID
    employee_id: str = ""  # 員工 ID
    manager_role_id: str = ""  # 經理角色 ID
    task_id: str = ""  # 任務 ID
    session_id: str = ""  # 工作階段 ID
    write_scope: str = ""  # 寫入範圍
    ownership_contract: str = ""  # 所有權合約
    dependency_snapshot: list[str] = field(default_factory=list)  # 依賴快照
    pending_inbox: list[dict[str, Any]] = field(default_factory=list)  # 待處理收件匣
    actionable_chat: list[dict[str, Any]] = field(default_factory=list)  # 可操作聊天
    protocol_backlog: list[dict[str, Any]] = field(default_factory=list)  # 協議積壓
    latest_notification: dict[str, Any] = field(default_factory=dict)  # 最新通知
    resident_status: str = "idle"  # 常駐狀態
    team_memory_digest: str = ""  # 團隊記憶摘要
    artifact_refs: list[str] = field(default_factory=list)  # 產出物引用
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間

@dataclass
class AgentMessage:
    """代理訊息 — 代理間的高層級訊息（舊版通訊模型）。

    職責說明：
        比 CommsEnvelope 更高層級的訊息抽象，包含訊息類型、
        緊急程度、是否需要回覆等語義。逐步被 CommsEnvelope 取代。

    關聯關係：
        - 由 opc/layer2_organization/communication.py 管理
        - 被 opc/database/store.py 持久化
    """
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 訊息唯一 ID
    msg_type: Literal[
        "question", "inform", "request_review", "flag_issue",
        "decision_needed", "ack", "answer"
    ] = "inform"  # 訊息類型
    from_agent: str = ""  # 發送者代理 ID
    to_agents: list[str] = field(default_factory=list)  # 接收者代理 ID 列表
    subject: str = ""  # 訊息主旨
    body: str = ""  # 訊息內容
    context_ref: str | None = None  # 上下文引用
    urgency: MessageUrgency = MessageUrgency.NORMAL  # 緊急程度
    reply_needed: bool = False  # 是否需要回覆
    requires_ack: bool = False  # 是否需要確認
    timeout_action: str | None = None  # 逾時動作
    reply_to_msg_id: str | None = None  # 回覆的訊息 ID
    task_id: str | None = None  # 關聯的任務 ID
    status: MessageStatus = MessageStatus.SENT  # 訊息狀態
    timestamp: datetime = field(default_factory=_utcnow)  # 發送時間
    processed_at: datetime | None = None  # 處理時間
    transport_kind: CommsTransportKind = CommsTransportKind.DM  # 傳輸類型
    semantic_type: CommsSemanticType = CommsSemanticType.WORK_UPDATE  # 語義類型
    comms_state: CommsState = CommsState.OPEN  # 通訊狀態
    correlation_id: str = ""  # 關聯 ID（同一對話鏈）
    refs: dict[str, Any] = field(default_factory=dict)  # 關聯引用
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata


@dataclass
class StructuredHandoff:
    """結構化交接 — 工作項目之間的完整交接資訊。

    職責說明：
        當工作從一個代理/工作項目轉移到另一個時，記錄完整的上下文：
        目標、已完成工作、產出物、待解決問題、假設、決策、風險等。

    關聯關係：
        - 由 opc/layer2_organization/ 的交接邏輯建立
        - 被 opc/database/store.py 持久化為 HandoffRecord
    """
    handoff_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 交接唯一 ID
    objective: str = ""  # 交接目標
    completed_work: str = ""  # 已完成工作
    artifacts: list[str] = field(default_factory=list)  # 產出物列表
    open_questions: list[str] = field(default_factory=list)  # 待解決問題
    assumptions: list[str] = field(default_factory=list)  # 假設列表
    decisions: list[str] = field(default_factory=list)  # 已做決策
    risks: list[str] = field(default_factory=list)  # 風險列表
    next_actions: list[str] = field(default_factory=list)  # 後續行動
    acceptance_criteria: list[str] = field(default_factory=list)  # 驗收標準
    summary: str = ""  # 交接摘要
    source_task_id: str | None = None  # 來源任務 ID
    source_projection_id: str | None = None  # 來源投影 ID
    source_projection_title: str | None = None  # 來源投影標題
    source_work_item_id: str | None = None  # 來源工作項目 ID
    target_work_item_id: str | None = None  # 目標工作項目 ID


@dataclass
class MeetingRoom:
    """會議室 — 多代理會議的運行時狀態。

    職責說明：
        當多個代理需要協商決策時，系統建立會議室。
        包含參與者、議程、輪次、共識、結果等。

    關聯關係：
        - 由 opc/layer2_organization/ 的會議邏輯管理
        - 被 opc/database/store.py 持久化
    """
    room_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 會議室唯一 ID
    task_id: str | None = None  # 關聯的任務 ID
    topic: str = ""  # 會議主題
    participants: list[str] = field(default_factory=list)  # 參與者 ID 列表
    shared_context: str = ""  # 共享上下文
    agenda: list[str] = field(default_factory=list)  # 議程列表
    max_rounds: int = 5  # 最大討論輪次
    decision_owner: str = "coordinator"  # 決策擁有者
    status: MeetingStatus = MeetingStatus.OPEN  # 會議狀態
    decision_method: str = ""  # 決策方式
    current_round: int = 0  # 當前輪次
    pending_participants: list[str] = field(default_factory=list)  # 待回覆參與者
    consensus: dict[str, Any] = field(default_factory=dict)  # 共識內容
    outcome: dict | None = None  # 會議結果
    transcript: list[dict] = field(default_factory=list)  # 會議記錄
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間
    last_activity_at: datetime = field(default_factory=_utcnow)  # 最後活動時間
    deadline_at: datetime | None = None  # 截止時間


# ---------------------------------------------------------------------------
# 持久化記錄（Persistence Records）— 對應 store.py 中的資料表
# ---------------------------------------------------------------------------

@dataclass
class WorkItemDecisionRecord:
    """工作項目決策記錄 — 持久化的決策追蹤。"""
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 決策唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    task_id: str | None = None  # 關聯的任務 ID
    role_id: str = ""  # 做出決策的角色 ID
    projection_id: str = ""  # 關聯的投影 ID
    category: str = "general"  # 決策分類
    summary: str = ""  # 決策摘要
    details: dict[str, Any] = field(default_factory=dict)  # 決策詳情
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class ArtifactRecord:
    """產出物記錄 — 持久化的產出物追蹤。"""
    artifact_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 產出物唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    task_id: str | None = None  # 關聯的任務 ID
    projection_id: str = ""  # 關聯的投影 ID
    role_id: str = ""  # 產生者角色 ID
    name: str = ""  # 產出物名稱
    artifact_type: str = "generic"  # 產出物類型（"generic"/"code"/"document"/"report"）
    location: str = ""  # 產出物位置（路徑或 URL）
    status: str = "active"  # 產出物狀態
    details: dict[str, Any] = field(default_factory=dict)  # 產出物詳情
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class RoleMemoryRecord:
    """角色記憶記錄 — 持久化的角色級別記憶。"""
    memory_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 記憶唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    role_id: str = ""  # 角色 ID
    scope: str = "project"  # 記憶範圍（"project"/"global"）
    summary: str = ""  # 記憶摘要
    details: dict[str, Any] = field(default_factory=dict)  # 記憶詳情
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class HandoffRecord:
    """交接記錄 — 持久化的工作交接追蹤。"""
    handoff_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 交接唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    session_id: str | None = None  # 關聯的工作階段 ID
    task_id: str | None = None  # 關聯的任務 ID
    from_role: str = ""  # 來源角色
    to_role: str = ""  # 目標角色
    source_projection_id: str = ""  # 來源投影 ID
    target_projection_id: str = ""  # 目標投影 ID
    source_work_item_id: str = ""  # 來源工作項目 ID
    target_work_item_id: str = ""  # 目標工作項目 ID
    summary: str = ""  # 交接摘要
    payload: dict[str, Any] = field(default_factory=dict)  # 交接酬載
    requires_ack: bool = False  # 是否需要確認
    status: str = "sent"  # 交接狀態（"sent"/"received"/"accepted"/"rejected"）
    received_at: datetime | None = None  # 接收時間
    acked_at: datetime | None = None  # 確認時間
    accepted_at: datetime | None = None  # 接受時間
    rejected_at: datetime | None = None  # 拒絕時間
    response_summary: str = ""  # 回應摘要
    ack_message_id: str | None = None  # 確認訊息 ID
    response_message_id: str | None = None  # 回應訊息 ID
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class ReorgEventRecord:
    """重組事件記錄 — 持久化的重組過程事件。"""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 事件唯一 ID
    proposal_id: str = ""  # 關聯的提案 ID
    project_id: str = "default"  # 所屬專案 ID
    event_kind: ReorgEventKind = ReorgEventKind.PROPOSED  # 事件類型
    summary: str = ""  # 事件摘要
    details: dict[str, Any] = field(default_factory=dict)  # 事件詳情
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


# ---------------------------------------------------------------------------
# 工作階段記錄（Session Records）— 對話和記憶的持久化
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    """工作階段記錄 — 持久化的工作階段（對話）追蹤。"""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 工作階段唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    parent_session_id: str | None = None  # 父工作階段 ID
    title: str = ""  # 工作階段標題
    mode: str = "primary"  # 模式（"primary"/"sub"/"background"）
    status: str = "active"  # 狀態（"active"/"completed"/"archived"）
    summary: str = ""  # 工作階段摘要
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


@dataclass
class SessionMessageRecord:
    """工作階段訊息記錄 — 持久化的對話訊息。"""
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 訊息唯一 ID
    session_id: str = ""  # 所屬工作階段 ID
    role: str = "user"  # 訊息角色（"user"/"assistant"/"system"）
    task_id: str | None = None  # 關聯的任務 ID
    agent_id: str | None = None  # 關聯的代理 ID
    parent_message_id: str | None = None  # 父訊息 ID（回覆時）
    summary_flag: bool = False  # 是否為摘要訊息
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class SessionPartRecord:
    """工作階段訊息部分記錄 — 訊息的組成部分（文字/工具調用/圖片等）。"""
    part_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 部分唯一 ID
    message_id: str = ""  # 所屬訊息 ID
    session_id: str = ""  # 所屬工作階段 ID
    part_type: str = "text"  # 部分類型（"text"/"tool_call"/"tool_result"/"image"）
    payload: dict[str, Any] = field(default_factory=dict)  # 部分酬載
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class SessionCompactionRecord:
    """工作階段壓縮記錄 — 對話歷史壓縮的追蹤。"""
    compaction_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 壓縮唯一 ID
    session_id: str = ""  # 所屬工作階段 ID
    compaction_message_id: str = ""  # 壓縮後訊息 ID
    source_boundary_message_id: str = ""  # 來源邊界訊息 ID
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class SessionMemorySnapshotRecord:
    """工作階段記憶快照記錄 — 對話記憶的持久化快照。"""
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 快照唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    session_id: str = ""  # 所屬工作階段 ID
    summary_message_id: str = ""  # 摘要訊息 ID
    source_boundary_message_id: str = ""  # 來源邊界訊息 ID
    summary_text: str = ""  # 摘要文字
    memory_text: str = ""  # 記憶文字
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


@dataclass
class AgentCompactionRecord:
    """代理壓縮記錄 — 代理對話歷史壓縮的追蹤。"""
    compaction_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 壓縮唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    session_id: str = ""  # 所屬工作階段 ID
    employee_id: str = ""  # 員工 ID
    role_id: str = ""  # 角色 ID
    compaction_message_id: str = ""  # 壓縮後訊息 ID
    source_boundary_message_id: str = ""  # 來源邊界訊息 ID
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class AgentMemorySnapshotRecord:
    """代理記憶快照記錄 — 代理級別的記憶持久化快照。"""
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 快照唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    session_id: str = ""  # 所屬工作階段 ID
    employee_id: str = ""  # 員工 ID
    role_id: str = ""  # 角色 ID
    memory_scope: str = "session"  # 記憶範圍（"session"/"project"/"global"）
    memory_kind: str = "process"  # 記憶類型（"process"/"semantic"/"episodic"）
    summary_message_id: str = ""  # 摘要訊息 ID
    source_boundary_message_id: str = ""  # 來源邊界訊息 ID
    summary_text: str = ""  # 摘要文字
    memory_text: str = ""  # 記憶文字
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


@dataclass
class SessionLinkRecord:
    """工作階段連結記錄 — 工作階段之間的關聯關係。"""
    link_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 連結唯一 ID
    project_id: str = "default"  # 所屬專案 ID
    session_id: str = ""  # 來源工作階段 ID
    linked_session_id: str | None = None  # 連結的工作階段 ID
    task_id: str | None = None  # 關聯的任務 ID
    link_type: str = "child_session"  # 連結類型（"child_session"/"related"/"forked"）
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


# ---------------------------------------------------------------------------
# Layer 3：代理（Agent）— 代理的靜態配置資訊
# ---------------------------------------------------------------------------

@dataclass
class AgentInfo:
    """代理資訊 — 代理在組織中的完整配置。

    職責說明：
        描述一個代理的靜態配置：角色、職責、工具、技能、
        預算、心跳等。由組織 YAML 定義，運行時載入。

    關聯關係：
        - 由 opc/core/org_config.py 從 YAML 載入
        - 被 opc/layer2_organization/ 和 opc/layer3_agent/ 使用
    """
    role_id: str  # 角色唯一 ID
    name: str  # 代理名稱
    responsibility: str  # 職責描述
    status: AgentStatus = AgentStatus.IDLE  # 代理狀態
    current_task_id: str | None = None  # 當前任務 ID
    reports_to: str = "owner"  # 上級（"owner" 或角色 ID）
    icon: str | None = None  # 圖示
    can_spawn: list[str] = field(default_factory=list)  # 可生成的子代理角色 ID 列表
    tools: list[str] = field(default_factory=list)  # 可用工具列表
    preferred_external_agent: str | None = None  # 首選外部代理
    model: str = ""  # 角色專用 LLM 模型（空字串表示使用全域預設）
    prompt_refs: list[str] = field(default_factory=list)  # Prompt 引用路徑列表
    skill_refs: list[str] = field(default_factory=list)  # 技能引用路徑列表
    handoff_template_ref: str | None = None  # 交接模板引用
    memory_policy_ref: str | None = None  # 記憶策略引用
    artifact_contract_ref: str | None = None  # 產出物合約引用
    runtime_policy: dict[str, Any] = field(default_factory=dict)  # 運行時策略配置
    org_id: str | None = None  # 所屬組織 ID
    budget_monthly_cents: int = 0  # 月度預算（美分）
    spent_monthly_cents: int = 0  # 月度已花費（美分）
    heartbeat_enabled: bool = False  # 是否啟用心跳
    heartbeat_interval_sec: int = 300  # 心跳間隔（秒）
    last_heartbeat_at: datetime | None = None  # 最後心跳時間
    capabilities: str = ""  # 能力描述


# ---------------------------------------------------------------------------
# Layer 4：組織實體（Organization Entities）
# ---------------------------------------------------------------------------

@dataclass
class Organization:
    """組織 — 公司模式的頂層實體。

    職責說明：
        代表一個完整的組織（公司），包含名稱、設定檔、預算等。

    關聯關係：
        - 由 opc/core/org_config.py 管理
        - 被 opc/database/store.py 持久化
    """
    org_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 組織唯一 ID
    name: str = ""  # 組織名稱
    description: str = ""  # 組織描述
    status: str = "active"  # 組織狀態（"active"/"archived"/"deleted"）
    company_profile: str = CompanyProfile.CORPORATE.value  # 公司設定檔類型
    budget_monthly_cents: int = 0  # 月度預算（美分）
    spent_monthly_cents: int = 0  # 月度已花費（美分）
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


@dataclass
class Goal:
    """目標 — 組織的目標層級結構。

    職責說明：
        代表組織中的一個目標，支援層級結構（公司→部門→團隊→任務）。

    關聯關係：
        - 被 opc/database/store.py 持久化
        - 被 opc/layer2_organization/ 用於目標追蹤
    """
    goal_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 目標唯一 ID
    org_id: str = ""  # 所屬組織 ID
    parent_id: str | None = None  # 父目標 ID（層級結構）
    owner_agent_id: str | None = None  # 目標擁有者代理 ID
    level: GoalLevel = GoalLevel.TASK  # 目標層級
    title: str = ""  # 目標標題
    description: str = ""  # 目標描述
    status: GoalStatus = GoalStatus.ACTIVE  # 目標狀態
    priority: int = 5  # 優先級（1-10）
    deadline: datetime | None = None  # 截止時間
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


@dataclass
class CostEvent:
    """成本事件 — LLM 調用的成本記錄。

    職責說明：
        記錄每次 LLM 調用的 Token 使用量和成本，用於預算控制。

    關聯關係：
        - 由 opc/layer3_agent/ 的適配器在每次調用後產生
        - 被 opc/database/store.py 持久化
    """
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 事件唯一 ID
    org_id: str | None = None  # 所屬組織 ID
    agent_id: str | None = None  # 關聯的代理 ID
    task_id: str | None = None  # 關聯的任務 ID
    model: str = ""  # 使用的模型名稱
    tokens_in: int = 0  # 輸入 Token 數
    tokens_out: int = 0  # 輸出 Token 數
    cost_usd: float = 0.0  # 成本（USD）
    timestamp: datetime = field(default_factory=_utcnow)  # 事件時間


@dataclass
class OrgAgent:
    """組織代理 — 代理在組織中的持久化成員關係。

    職責說明：
        記錄代理在組織中的成員關係、預算、心跳等持久化資訊。

    關聯關係：
        - 被 opc/database/store.py 持久化
        - 由 opc/core/org_config.py 管理
    """
    agent_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 代理唯一 ID
    org_id: str = ""  # 所屬組織 ID
    role_id: str = ""  # 角色 ID
    name: str = ""  # 代理名稱
    reports_to: str | None = None  # 上級
    budget_monthly_cents: int = 0  # 月度預算（美分）
    spent_monthly_cents: int = 0  # 月度已花費（美分）
    heartbeat_enabled: bool = False  # 是否啟用心跳
    heartbeat_interval_sec: int = 300  # 心跳間隔（秒）
    last_heartbeat_at: datetime | None = None  # 最後心跳時間
    status: str = "idle"  # 代理狀態
    capabilities: str = ""  # 能力描述
    metadata: dict[str, Any] = field(default_factory=dict)  # 擴展 metadata
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間


# ---------------------------------------------------------------------------
# Layer 6：事件（Events）— 系統事件匯流排的載體
# ---------------------------------------------------------------------------

@dataclass
class OPCEvent:
    """OPC 事件 — 系統事件匯流排的標準事件載體。

    職責說明：
        所有系統內部事件的統一格式。透過 EventBus 分發，
        各模組可訂閱感興趣的事件類型。

    關聯關係：
        - 由 opc/core/events.py 的 EventBus 分發
        - 被所有層訂閱和產生
    """
    event_type: str  # 事件類型（如 "task_created"、"work_item_completed"）
    payload: dict = field(default_factory=dict)  # 事件酬載
    timestamp: datetime = field(default_factory=_utcnow)  # 事件時間
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 事件唯一 ID


# ---------------------------------------------------------------------------
# 數據管理：共用文件庫 + 任務數據整合
# ---------------------------------------------------------------------------

@dataclass
class SharedFileRecord:
    """共用文件庫記錄 — 公司共享檔案的索引元資訊。

    職責說明：
        記錄上傳到公司共用文件庫的每個檔案的元資訊。
        實際檔案內容存放於磁碟（{opc_home}/shared_files/），
        此記錄僅為 SQLite 索引，支援資料夾分類、標籤和搜尋。

    關聯關係：
        - 被 opc/core/shared_file_store.py 建立和管理
        - 被 opc/database/_store_shared_files.py 持久化
        - 被 opc/layer4_tools/shared_files.py 的 Agent 工具查詢
        - 被 opc/plugins/office_ui/services/file_library.py 的 UI 服務使用
    """
    file_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 檔案唯一 ID
    filename: str = ""  # 原始檔名
    folder: str = ""  # 資料夾路徑（如 "reports/2026"）
    mime_type: str = ""  # MIME 類型
    size_bytes: int = 0  # 檔案大小（位元組）
    tags: list[str] = field(default_factory=list)  # 標籤列表
    description: str = ""  # 文件描述
    uploaded_by: str = ""  # 上傳者角色 ID
    created_at: datetime = field(default_factory=_utcnow)  # 建立時間
    updated_at: datetime = field(default_factory=_utcnow)  # 更新時間


@dataclass
class CompanyDataSnapshot:
    """公司數據快照 — 任務、工作項、協作記錄的統一匯出視圖。

    職責說明：
        將分散在不同資料表中的任務、工作項、協作運行記錄
        聚合為一個結構化快照，支援 JSON/CSV 格式匯出和備份。

    關聯關係：
        - 由 opc/core/data_export.py 產生
        - 被 opc/layer4_tools/company_data.py 的 Agent 工具調用
        - 被 opc/plugins/office_ui/services/data_export.py 的 UI 服務使用
    """
    exported_at: datetime = field(default_factory=_utcnow)  # 匯出時間
    project_id: str = ""  # 專案 ID
    tasks: list[dict] = field(default_factory=list)  # 任務列表
    work_items: list[dict] = field(default_factory=list)  # 工作項列表
    delegation_runs: list[dict] = field(default_factory=list)  # 協作運行記錄
    summary: dict = field(default_factory=dict)  # 統計摘要
