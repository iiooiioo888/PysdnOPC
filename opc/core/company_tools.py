"""公司模式協作工具名稱與能力設定檔解析模組。

職責說明：
    定義公司模式（Company Mode）下所有協作工具的標準名稱，以及根據
    任務上下文動態解析「協作能力設定檔」（collaboration profile）的邏輯。
    不同角色（worker、manager、coordinator）擁有不同的工具集合，
    本模組負責根據任務 metadata、runtime_state 和角色配置決定
    當前執行回合可以使用哪些協作工具。

關聯關係：
    - 被 opc/layer2_organization/company_mode.py 在組裝工具列表時調用
    - 被 opc/layer3_agent/runtime_v2/ 在建立工具棧時調用
    - 被 opc/layer4_tools/collaboration.py 用於工具名稱驗證
    - 被 opc/layer2_organization/collaboration_policy.py 用於策略判斷

使用範例：
    from opc.core.company_tools import resolve_task_collaboration_tools
    profile, tools = resolve_task_collaboration_tools(task, runtime_state=state)
    # profile = "manager_default", tools = {"inbox", "send_dm", "delegate_work", ...}
"""

from __future__ import annotations  # 啟用延遲型別註解評估

from typing import Any  # 標準庫：metadata/runtime_state 的值型別註解


def company_collaboration_enabled(execution_mode: str | None) -> bool:
    """判斷公司協作功能是否啟用。

    功能：
        檢查 execution_mode 是否為 "company_mode"（精確匹配）。
        僅在公司模式下才暴露協作工具。

    參數：
        execution_mode (str | None)：當前執行模式字串。

    返回值：
        bool — True 表示公司協作功能啟用。

    被誰引用：
        - company_collaboration_enabled_for_task()：任務級別判斷
        - opc/layer3_agent/：組裝工具棧前的前置檢查
    """
    return str(execution_mode or "").strip() == "company_mode"


def company_collaboration_enabled_for_task(task: object | None) -> bool:
    """判斷指定任務是否啟用了公司協作功能。

    功能：
        從任務物件的 metadata 中提取 execution_mode 並判斷。

    參數：
        task (object | None)：任務物件（需有 metadata 屬性）。None 返回 False。

    返回值：
        bool — True 表示該任務啟用了公司協作。

    被誰引用：
        - resolve_collaboration_profile()：作為第一步判斷
    """
    if task is None:
        return False
    metadata = dict(getattr(task, "metadata", {}) or {})
    return company_collaboration_enabled(str(metadata.get("execution_mode", "") or ""))


# ── 協作能力設定檔常量 ──────────────────────────────────────────────────
# 每種設定檔對應一組允許使用的協作工具：

COLLAB_PROFILE_DISABLED = "disabled"  # 協作功能停用（非公司模式）
COLLAB_PROFILE_WORKER_DEFAULT = "worker_default"  # 一般工人：基本通訊工具
COLLAB_PROFILE_WORKER_EXECUTE_REVIEW = "worker_execute_review"  # 執行/審查工人：同 worker_default
COLLAB_PROFILE_MANAGER_DEFAULT = "manager_default"  # 經理：增加委派、看板、廣播等管理工具
COLLAB_PROFILE_COORDINATOR_DEFAULT = "coordinator_default"  # 協調者：增加跨團隊路由工具
COLLAB_PROFILE_DEBUG_ADMIN = "debug_admin"  # 除錯管理員：僅唯讀觀察工具

# 工人預設工具集：基本通訊能力
WORKER_DEFAULT_TOOL_NAMES: tuple[str, ...] = (
    "inbox",  # 讀取收件匣
    "send_dm",  # 發送私訊
    "ask_peer_and_wait",  # 向同事提問並等待回覆
    "reply_message",  # 回覆訊息
)

# 執行/審查工人的工具集（目前與 worker_default 相同）
WORKER_EXECUTE_REVIEW_TOOL_NAMES: tuple[str, ...] = WORKER_DEFAULT_TOOL_NAMES

# 經理預設工具集：工人工具 + 管理能力
MANAGER_DEFAULT_TOOL_NAMES: tuple[str, ...] = (
    *WORKER_DEFAULT_TOOL_NAMES,
    "delegate_work",  # 委派工作項目給下屬
    "modify_work_item",  # 修改工作項目
    "delete_work_item",  # 刪除工作項目
    "manager_board_read",  # 讀取管理看板
    "broadcast_issue",  # 廣播問題給團隊
    "start_meeting",  # 發起會議
)

# 協調者預設工具集：經理工具 + 跨團隊協調
COORDINATOR_DEFAULT_TOOL_NAMES: tuple[str, ...] = (
    *MANAGER_DEFAULT_TOOL_NAMES,
    "propose_task_adjustment",  # 提議任務調整
    "route_work",  # 路由工作到其他團隊
)

# 除錯管理員工具集：僅唯讀觀察
DEBUG_ADMIN_TOOL_NAMES: tuple[str, ...] = (
    "read_inbox",  # 讀取收件匣（唯讀）
    "read_meeting",  # 讀取會議記錄（唯讀）
    "list_colleagues",  # 列出同事（唯讀）
)

# 會議回應工具：當有進行中的會議時額外暴露
MEETING_RESPONSE_TOOL_NAMES: tuple[str, ...] = ("respond_meeting",)

# 人工審查工具：當允許關閉人工審查時額外暴露
HUMAN_REVIEW_TOOL_NAMES: tuple[str, ...] = ("close_human_review",)


# 所有公司協作工具的完整集合（用於審批豁免判斷）
COMPANY_COLLABORATION_TOOL_NAMES: tuple[str, ...] = (
    *COORDINATOR_DEFAULT_TOOL_NAMES,
    *MEETING_RESPONSE_TOOL_NAMES,
    *HUMAN_REVIEW_TOOL_NAMES,
)

# 公司除錯工具集合
COMPANY_DEBUG_TOOL_NAMES: tuple[str, ...] = DEBUG_ADMIN_TOOL_NAMES

# 所有公司工具的完整去重集合（協作 + 除錯）
COMPANY_ALL_COLLABORATION_TOOL_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            *COMPANY_COLLABORATION_TOOL_NAMES,
            *COMPANY_DEBUG_TOOL_NAMES,
        ]
    )
)

# 審批豁免工具集合：這些工具不需要經過審批流程即可執行
COMPANY_APPROVAL_EXEMPT_TOOL_NAMES: tuple[str, ...] = (
    *COMPANY_ALL_COLLABORATION_TOOL_NAMES,
)

# 多團隊協調回合模式集合：在這些模式下移除 ask_peer_and_wait 工具
MULTI_TEAM_COORDINATION_TURN_MODES: frozenset[str] = frozenset(
    {
        "dispatch_required",  # 需要分發工作
        "monitor_children",  # 監控子任務
        "synthesize_required",  # 需要綜合結果
        "deliver_required",  # 需要交付
    }
)

# 看板推送審查回合模式：專門的審查任務在席位中執行。
# 經理發出結構化裁決作為回合輸出；運行時自動套用到子工作項目。
# 受限的工具集使回合專注於判斷 — 無委派/訊息/分發等側通道。
REVIEW_EXECUTE_TURN_MODE: str = "review_execute"

# 審查執行回合的允許工具（極簡：僅看板讀取）
REVIEW_EXECUTE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "manager_board_read",
    }
)

# 多團隊協調者的舊版工具（在 multi_team_org 模型中移除）
MULTI_TEAM_COORDINATOR_LEGACY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "propose_task_adjustment",
        "route_work",
    }
)


def _task_metadata(task: object | None) -> dict[str, Any]:
    """安全提取任務物件的 metadata 字典（內部輔助函數）。"""
    return dict(getattr(task, "metadata", {}) or {}) if task is not None else {}


def _task_context_snapshot(task: object | None) -> dict[str, Any]:
    """安全提取任務物件的 context_snapshot 字典（內部輔助函數）。"""
    return dict(getattr(task, "context_snapshot", {}) or {}) if task is not None else {}


def _work_item_turn_type(metadata: dict[str, Any]) -> str:
    """從 metadata 中提取工作項目的回合類型（內部輔助函數）。

    功能：
        依序嘗試多個可能的鍵名，返回第一個非空值。
        支援的鍵：work_item_turn_type、work_kind、delegation_turn_kind。

    返回值：
        str — 小寫的回合類型（如 "execute"、"review"、"dispatch"），或空字串。
    """
    for key in ("work_item_turn_type", "work_item_turn_type", "work_kind", "delegation_turn_kind"):
        value = str(metadata.get(key, "") or "").strip().lower()
        if value:
            return value
    return ""


def _runtime_state_dict(runtime_state: Any | None) -> dict[str, Any]:
    """安全地將 runtime_state 轉為字典（內部輔助函數）。"""
    return dict(runtime_state or {}) if isinstance(runtime_state, dict) else {}


def _role_type_hint(role_cfg: Any | None, runtime_state: dict[str, Any]) -> str:
    """從角色配置或 runtime_state 中推斷角色類型（內部輔助函數）。

    功能：
        依序嘗試：role_cfg.runtime_policy.role_type → role_cfg.role_type
        → runtime_state["role_type"]。

    返回值：
        str — 小寫的角色類型（如 "coordinator"、"manager"、"worker"），或空字串。
    """
    if role_cfg is not None:
        runtime_policy = getattr(role_cfg, "runtime_policy", None)
        hinted = str(
            getattr(runtime_policy, "role_type", "")
            or (runtime_policy.get("role_type", "") if isinstance(runtime_policy, dict) else "")
            or getattr(role_cfg, "role_type", "")
            or ""
        ).strip().lower()
        if hinted:
            return hinted
    return str(runtime_state.get("role_type", "") or "").strip().lower()


def _can_spawn_hint(role_cfg: Any | None, runtime_state: dict[str, Any]) -> bool:
    """判斷角色是否有生成子代理的能力（內部輔助函數）。

    功能：
        檢查 role_cfg.can_spawn 或 runtime_state["can_spawn"] 是否有非空項目。
        有 can_spawn 能力通常意味著是協調者角色。

    返回值：
        bool — True 表示有生成能力。
    """
    if role_cfg is not None:
        can_spawn = [str(item).strip() for item in list(getattr(role_cfg, "can_spawn", []) or []) if str(item).strip()]
        if can_spawn:
            return True
    return bool(
        [
            str(item).strip()
            for item in list(runtime_state.get("can_spawn", []) or [])
            if str(item).strip()
        ]
    )


def _is_execute_or_review_work_item(task: object | None) -> bool:
    """判斷任務是否為執行或審查類型的工作項目（內部輔助函數）。"""
    metadata = _task_metadata(task)
    if not company_collaboration_enabled(str(metadata.get("execution_mode", "") or "")):
        return False
    turn_type = _work_item_turn_type(metadata)
    return turn_type in {"execute", "review"}


def _has_active_meeting(task: object | None, runtime_state: dict[str, Any]) -> bool:
    """判斷當前是否有進行中的會議（內部輔助函數）。

    功能：
        檢查多個來源以判斷是否有活動會議：
        1. metadata.peer_wait.kind == "meeting"
        2. context_snapshot 中任何訊息帶有 meeting_room_id
        3. runtime_state 中的 meeting_room_id 或 active_meeting 標記

    返回值：
        bool — True 表示有進行中的會議（需要暴露 respond_meeting 工具）。
    """
    metadata = _task_metadata(task)
    # 檢查 peer_wait 是否為會議類型
    peer_wait = dict(metadata.get("peer_wait", {}) or {})
    if str(peer_wait.get("kind", "") or "").strip().lower() == "meeting":
        return True
    # 檢查 context_snapshot 中的訊息佇列
    context_snapshot = _task_context_snapshot(task)
    for bucket in (
        context_snapshot.get("company_member_inbox", []),
        context_snapshot.get("company_member_protocol_backlog", []),
        context_snapshot.get("company_member_notification_backlog", []),
        context_snapshot.get("broker_pending_inbox", []),
    ):
        for item in list(bucket or []):
            if isinstance(item, dict) and (
                str(item.get("meeting_room_id", "") or "").strip()
                or str(dict(item.get("metadata", {}) or {}).get("meeting_room_id", "") or "").strip()
            ):
                return True
    # 檢查最新通知中的會議 ID
    latest_company_notification = dict(context_snapshot.get("latest_company_notification", {}) or {})
    if str(latest_company_notification.get("meeting_room_id", "") or "").strip():
        return True
    # 檢查 runtime_state 中的會議標記
    if str(runtime_state.get("meeting_room_id", "") or "").strip():
        return True
    if bool(runtime_state.get("active_meeting", False)):
        return True
    return False


def _human_review_close_allowed(task: object | None, runtime_state: dict[str, Any]) -> bool:
    """判斷是否允許關閉人工審查（內部輔助函數）。

    功能：
        檢查 metadata、context_snapshot、runtime_state 中是否有
        human_review_close_allowed 標記或 company_delivery_feedback 檢查點類型。

    返回值：
        bool — True 表示允許關閉人工審查（暴露 close_human_review 工具）。
    """
    metadata = _task_metadata(task)
    context_snapshot = _task_context_snapshot(task)
    return any(
        bool(source.get("human_review_close_allowed", False))
        or str(source.get("human_review_checkpoint_type", "") or "").strip() == "company_delivery_feedback"
        for source in (metadata, context_snapshot, runtime_state)
    )


def resolve_company_turn_mode(
    task: object | None,
    runtime_state: dict[str, Any] | None = None,
) -> str:
    """解析當前任務的公司回合模式。

    功能：
        從多個來源依序查找 current_turn_mode 值。
        僅在 runtime_model 為 "multi_team_org" 時有效。
        回合模式決定工具集的動態調整（如審查模式限制工具）。

    參數：
        task (object | None)：任務物件。
        runtime_state (dict[str, Any] | None)：運行時狀態字典。

    返回值：
        str — 回合模式字串（如 "review_execute"、"dispatch_required"），
        或空字串（非多團隊模式或無明確模式）。

    被誰引用：
        - resolve_allowed_collaboration_tools()：動態調整工具集
    """
    metadata = _task_metadata(task)
    context_snapshot = _task_context_snapshot(task)
    state = _runtime_state_dict(runtime_state)
    # 僅多團隊組織模型使用回合模式
    runtime_model = str(
        metadata.get("runtime_model", "")
        or context_snapshot.get("runtime_model", "")
        or state.get("runtime_model", "")
        or ""
    ).strip()
    if runtime_model != "multi_team_org":
        return ""
    # 依序從多個來源查找 current_turn_mode（優先順序從高到低）
    for candidate in (
        state.get("current_turn_mode"),
        dict(state.get("manager_digest", {}) or {}).get("current_turn_mode"),
        metadata.get("current_turn_mode"),
        context_snapshot.get("current_turn_mode"),
        dict(metadata.get("member_session_state", {}) or {}).get("current_turn_mode"),
        dict(context_snapshot.get("member_session", {}) or {}).get("current_turn_mode"),
        dict(metadata.get("resident_assignment", {}) or {}).get("metadata", {}).get("current_turn_mode"),
        dict(context_snapshot.get("resident_assignment", {}) or {}).get("metadata", {}).get("current_turn_mode"),
        dict(context_snapshot.get("manager_digest", {}) or {}).get("current_turn_mode"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def resolve_collaboration_profile(
    task: object | None,
    role: str = "",
    seat: str = "",
    runtime_state: dict[str, Any] | None = None,
    *,
    role_cfg: Any | None = None,
    debug_admin: bool = False,
) -> str:
    """解析任務/席位的協作能力設定檔。

    功能：
        根據任務上下文和角色配置，決定使用哪種協作設定檔：
        1. 非公司模式 → disabled
        2. 除錯管理員 → debug_admin
        3. 協調者（role_type 或 can_spawn）→ coordinator_default
        4. 經理（managed_team 或管理回合類型）→ manager_default
        5. 執行/審查工作項目 → worker_execute_review
        6. 其他 → worker_default

    參數：
        task (object | None)：任務物件。
        role (str)：角色 ID（目前未直接使用，保留供未來擴展）。
        seat (str)：席位 ID（目前未直接使用）。
        runtime_state (dict[str, Any] | None)：運行時狀態。
        role_cfg (Any | None)：角色配置物件（可選）。
        debug_admin (bool)：是否為除錯管理員模式。預設 False。

    返回值：
        str — 設定檔名稱（COLLAB_PROFILE_* 常量之一）。

    被誰引用：
        - resolve_task_collaboration_tools()：作為第一步解析
        - opc/layer2_organization/company_mode.py：決定工具集
    """
    metadata = _task_metadata(task)
    state = _runtime_state_dict(runtime_state)
    # 非公司模式 → 停用
    if not company_collaboration_enabled_for_task(task):
        return COLLAB_PROFILE_DISABLED
    # 除錯管理員模式
    if debug_admin or bool(metadata.get("collaboration_debug_admin", False)) or bool(state.get("debug_admin", False)):
        return COLLAB_PROFILE_DEBUG_ADMIN

    # 收集判斷依據
    managed_team_id = str(
        metadata.get("managed_team_id", "")
        or state.get("managed_team_id", "")
        or dict(metadata.get("member_session_state", {}) or {}).get("metadata", {}).get("managed_team_id", "")
        or ""
    ).strip()
    role_type = _role_type_hint(role_cfg, state)
    can_spawn = _can_spawn_hint(role_cfg, state)
    manager_board_summary = (
        dict(state.get("manager_board_summary", {}) or {})
        or dict(_task_context_snapshot(task).get("manager_board_summary", {}) or {})
    )
    work_item_turn_type = _work_item_turn_type(metadata)

    # 協調者：有 coordinator 角色類型或有生成子代理能力
    if role_type == "coordinator" or can_spawn:
        return COLLAB_PROFILE_COORDINATOR_DEFAULT
    # 經理：有管理的團隊、管理看板、或管理類回合類型
    if managed_team_id or manager_board_summary or work_item_turn_type in {"intake", "plan", "dispatch", "monitor", "aggregate", "deliver"}:
        return COLLAB_PROFILE_MANAGER_DEFAULT
    # 執行/審查工作項目
    if _is_execute_or_review_work_item(task):
        return COLLAB_PROFILE_WORKER_EXECUTE_REVIEW
    # 預設工人
    return COLLAB_PROFILE_WORKER_DEFAULT


def resolve_allowed_collaboration_tools(
    profile: str,
    task: object | None = None,
    runtime_state: dict[str, Any] | None = None,
) -> set[str]:
    """根據設定檔和上下文解析允許的協作工具集合。

    功能：
        基於設定檔取得基礎工具集，然後根據回合模式動態調整：
        - review_execute 模式：極簡工具集（僅看板讀取）
        - 多團隊協調模式：移除 ask_peer_and_wait 和舊版工具
        - 有活動會議：加入 respond_meeting
        - 允許人工審查關閉：加入 close_human_review

    參數：
        profile (str)：由 resolve_collaboration_profile 解析的設定檔名稱。
        task (object | None)：任務物件（用於回合模式判斷）。
        runtime_state (dict[str, Any] | None)：運行時狀態。

    返回值：
        set[str] — 允許使用的工具名稱集合。

    被誰引用：
        - resolve_task_collaboration_tools()：組合最終結果
        - opc/layer3_agent/：建立工具棧
    """
    state = _runtime_state_dict(runtime_state)
    # 停用 → 空集合
    if profile == COLLAB_PROFILE_DISABLED:
        return set()
    # 除錯管理員 → 唯讀工具
    if profile == COLLAB_PROFILE_DEBUG_ADMIN:
        return set(DEBUG_ADMIN_TOOL_NAMES)
    # 根據設定檔選擇基礎工具集
    if profile == COLLAB_PROFILE_WORKER_EXECUTE_REVIEW:
        allowed = set(WORKER_EXECUTE_REVIEW_TOOL_NAMES)
    elif profile == COLLAB_PROFILE_MANAGER_DEFAULT:
        allowed = set(MANAGER_DEFAULT_TOOL_NAMES)
    elif profile == COLLAB_PROFILE_COORDINATOR_DEFAULT:
        allowed = set(COORDINATOR_DEFAULT_TOOL_NAMES)
    else:
        allowed = set(WORKER_DEFAULT_TOOL_NAMES)
    # 動態調整：回合模式
    turn_mode = resolve_company_turn_mode(task, runtime_state=state)
    # 審查執行模式：極簡工具集
    if turn_mode == REVIEW_EXECUTE_TURN_MODE and str(_task_metadata(task).get("runtime_model", "") or "").strip() == "multi_team_org":
        return set(REVIEW_EXECUTE_TOOL_NAMES)
    # 多團隊協調模式：移除同步等待工具
    if turn_mode in MULTI_TEAM_COORDINATION_TURN_MODES:
        allowed.discard("ask_peer_and_wait")
    # 多團隊組織模型：移除舊版協調工具
    if turn_mode and str(_task_metadata(task).get("runtime_model", "") or "").strip() == "multi_team_org":
        allowed.difference_update(MULTI_TEAM_COORDINATOR_LEGACY_TOOL_NAMES)
    # 有活動會議：加入會議回應工具
    if _has_active_meeting(task, state):
        allowed.update(MEETING_RESPONSE_TOOL_NAMES)
    # 允許關閉人工審查：加入審查關閉工具
    if _human_review_close_allowed(task, state):
        allowed.update(HUMAN_REVIEW_TOOL_NAMES)
    return allowed


def resolve_task_collaboration_tools(
    task: object | None,
    *,
    role: str = "",
    seat: str = "",
    runtime_state: dict[str, Any] | None = None,
    role_cfg: Any | None = None,
    debug_admin: bool = False,
) -> tuple[str, set[str]]:
    """一站式解析任務的協作設定檔和允許工具集合。

    功能：
        組合 resolve_collaboration_profile 和 resolve_allowed_collaboration_tools
        的結果，返回完整的協作能力描述。

    參數：
        task (object | None)：任務物件。
        role (str)：角色 ID。
        seat (str)：席位 ID。
        runtime_state (dict[str, Any] | None)：運行時狀態。
        role_cfg (Any | None)：角色配置物件。
        debug_admin (bool)：是否為除錯管理員模式。

    返回值：
        tuple[str, set[str]] — (設定檔名稱, 允許的工具名稱集合)。

    被誰引用：
        - opc/layer2_organization/company_mode.py：組裝代理工具棧
        - opc/layer3_agent/runtime_v2/runtime.py：建立運行時工具
    """
    profile = resolve_collaboration_profile(
        task,
        role=role,
        seat=seat,
        runtime_state=runtime_state,
        role_cfg=role_cfg,
        debug_admin=debug_admin,
    )
    return profile, resolve_allowed_collaboration_tools(profile, task=task, runtime_state=runtime_state)
