"""內建公司運行時設定檔和輔助工具。"""

from __future__ import annotations

from opc.core.config import (
    ArtifactPolicyConfig,
    CommunicationPolicyConfig,
    GateHarnessPolicyConfig,
    HandoffPolicyConfig,
    MemoryPolicyConfig,
    ReviewPolicyConfig,
    RoleConfig,
    RoleRuntimePolicyConfig,
    RuntimePolicyConfig,
)
from opc.core.models import CompanyProfile
from opc.layer2_organization.data_acquisition_policy import ACQUISITION_SPECIALIST_ROLE_ID

_BROWSER_RESEARCH_TOOLS = [
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_wait_for",
    "browser_scroll",
    "browser_take_screenshot",
]

_BROWSER_EXECUTION_TOOLS = [
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
]

_CORPORATE_COORDINATION_TOOLS = [
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "todo_write",
    "todo_read",
]

_CORPORATE_BOOTSTRAP_TOOLS = [
    *_CORPORATE_COORDINATION_TOOLS,
    "shell_exec",
]

_CORPORATE_WEB_COORDINATION_TOOLS = [
    *_CORPORATE_COORDINATION_TOOLS,
    "web_search",
    "web_fetch",
]

_CORPORATE_EXECUTION_TOOLS = [
    "shell_exec",
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "web_search",
    "web_fetch",
    "todo_write",
    "todo_read",
    *_BROWSER_EXECUTION_TOOLS,
]

_CORPORATE_QA_TOOLS = [
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "shell_exec",
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_wait_for",
    "browser_scroll",
    "browser_select_option",
    "browser_take_screenshot",
]

_CORPORATE_ENV_TOOLS = [
    "shell_exec",
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "web_search",
    "web_fetch",
    "todo_write",
    "todo_read",
]

_CORPORATE_DATA_ACQUISITION_TOOLS = [
    "shell_exec",
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "web_search",
    "web_fetch",
    "todo_write",
    "todo_read",
    *_BROWSER_EXECUTION_TOOLS,
]


def get_company_profile_descriptions() -> dict[str, str]:
    return {
        CompanyProfile.CORPORATE.value: (
            "分層式企業運行時，包含 CEO、C 級高管（CTO/CMO/COO）及專業工作者。執行由工作項目與角色佇列驅動。"
        ),
        CompanyProfile.CUSTOM.value: (
            "使用者自訂的企業運行時。角色與工作項目驅動執行。"
        ),
    }


def get_builtin_runtime_policies() -> dict[str, RuntimePolicyConfig]:
    return {
        CompanyProfile.CORPORATE.value: RuntimePolicyConfig(
            communication=CommunicationPolicyConfig(
                default_mode="dm",
                blocking_default=False,
                meeting_required_for=["architecture", "cross_team_conflict"],
                allow_broadcast=True,
            ),
            memory=MemoryPolicyConfig(
                include_role_memory=True,
                include_project_memory=False,
                include_decision_log=True,
                include_artifact_index=True,
                recent_history_lines=12,
            ),
            handoff=HandoffPolicyConfig(
                require_structured_handoff=True,
                require_ack=False,
                include_risks=True,
                include_open_questions=True,
            ),
            artifact=ArtifactPolicyConfig(
                enforce_contract=False,
                require_artifact_index=True,
                required_kinds=[],
            ),
            review=ReviewPolicyConfig(
                enable_work_item_gates=False,
                strict_gate_inference=False,
                require_reviewer_role=True,
                allow_human_override=True,
            ),
            gate_harness=GateHarnessPolicyConfig(
                decision_mode="agent_first",
                default_degrade_policy="allow",
                allow_pass_with_constraints=True,
            ),
        ),
        CompanyProfile.CUSTOM.value: RuntimePolicyConfig(
            gate_harness=GateHarnessPolicyConfig(
                decision_mode="agent_first",
                default_degrade_policy="allow",
                allow_pass_with_constraints=True,
            ),
        ),
    }


def _apply_configured_role_overrides(
    builtin_roles: list[RoleConfig],
    configured_roles: list[RoleConfig] | None = None,
) -> list[RoleConfig]:
    """Overlay explicit org_config role fields onto builtin role presets."""
    if not configured_roles:
        return builtin_roles

    configured_by_id = {role.id: role for role in configured_roles}
    merged: list[RoleConfig] = []
    for builtin_role in builtin_roles:
        configured_role = configured_by_id.get(builtin_role.id)
        if configured_role is None:
            merged.append(builtin_role)
            continue

        update_fields = {
            field_name: getattr(configured_role, field_name)
            for field_name in configured_role.model_fields_set
            if field_name != "id"
        }
        if not update_fields:
            merged.append(builtin_role)
            continue
        merged.append(builtin_role.model_copy(update=update_fields, deep=True))
    return merged


def get_builtin_roles(
    profile: str,
    configured_roles: list[RoleConfig] | None = None,
) -> list[RoleConfig]:
    # Corporate roles (also fallback for custom and unknown profiles)
    return _apply_configured_role_overrides([
        RoleConfig(
            id="ceo",
            name="CEO",
            icon="leader",
            responsibility="策略性接收、高層級路由、最終彙整並交付給業主。",
            can_spawn=["cto", "cmo", "coo", "hr_manager"],
            tools=list(_CORPORATE_BOOTSTRAP_TOOLS),
            prompt_refs=["將任務路由至適當的 C 級高管。彙整最終結果。"],
        ),
        RoleConfig(
            id="cto",
            name="CTO",
            icon="code",
            responsibility="技術規劃、架構決策、程式碼審查與工程監督。",
            reports_to="ceo",
            can_spawn=["senior_engineer", "devops_engineer"],
            tools=[*_CORPORATE_WEB_COORDINATION_TOOLS, "shell_exec"],
            prompt_refs=["專注於技術可行性、架構品質與工程最佳實踐。"],
        ),
        RoleConfig(
            id="cmo",
            name="CMO",
            icon="marketing",
            responsibility="行銷策略、內容規劃、UX 審查與品牌監督。",
            reports_to="ceo",
            can_spawn=["content_specialist", "designer"],
            tools=[*_CORPORATE_WEB_COORDINATION_TOOLS, *_BROWSER_RESEARCH_TOOLS],
            prompt_refs=["優化受眾契合度、品牌一致性與內容品質。"],
        ),
        RoleConfig(
            id="coo",
            name="COO",
            icon="strategy",
            responsibility="營運協調、流程管理、跨團隊對齊與品質保證。",
            reports_to="ceo",
            can_spawn=[ACQUISITION_SPECIALIST_ROLE_ID, "qa_analyst"],
            tools=[*_CORPORATE_WEB_COORDINATION_TOOLS, *_BROWSER_RESEARCH_TOOLS],
            prompt_refs=["確保營運效率、流程合規與交付品質。"],
        ),
        RoleConfig(
            id="hr_manager",
            name="HR Manager",
            icon="people",
            responsibility=(
                "自主人力資源架構師：獨立設計並建構組織的完整人員結構。負責分析任務需求、"
                "規劃團隊組成、建立與配置角色、驅動招募流程、管理員工生命週期（到職、評估、晉升、離職）、"
                "識別技能缺口、提出組織重組方案，並維護人才庫。主動運作，無需等待指令。"
            ),
            reports_to="ceo",
            can_spawn=["hr_recruiter", "hr_training_specialist"],
            tools=[*_CORPORATE_WEB_COORDINATION_TOOLS, "shell_exec"],
            prompt_refs=[
                "自主分析傳入的任務並決定最佳團隊結構。",
                "主動設計角色定義、匯報關係與人員配置計畫。",
                "驅動完整招募流程：尋找候選人、評估契合度、錄取並進行到職引導。",
                "監控員工績效與技能成長；提出晉升或角色調整建議。",
                "識別組織缺口並主動重組團隊，無需等待指示。",
                "維護動態人才庫及每個關鍵角色的繼任計畫。",
            ],
        ),
        RoleConfig(
            id="hr_recruiter",
            name="HR Recruiter",
            icon="search",
            responsibility=(
                "人才搜尋與招募執行：掃描人才範本、根據角色需求評估候選人契合度、"
                "進行結構化評估，並產出附帶理由的錄取/不錄取建議。"
            ),
            reports_to="hr_manager",
            can_spawn=[],
            tools=[*_CORPORATE_WEB_COORDINATION_TOOLS, *_BROWSER_RESEARCH_TOOLS],
            prompt_refs=[
                "從人才範本與外部人才庫中搜尋候選人。",
                "根據角色專屬能力矩陣評估候選人。",
                "產出附帶評分理由的結構化錄取/不錄取建議。",
            ],
        ),
        RoleConfig(
            id="hr_training_specialist",
            name="HR Training Specialist",
            icon="book",
            responsibility=(
                "員工發展與技能培養：設計到職培訓計畫、建立技能發展方案、追蹤學習進度，"
                "並確保員工勝任其角色。"
            ),
            reports_to="hr_manager",
            can_spawn=[],
            tools=[*_CORPORATE_WEB_COORDINATION_TOOLS],
            prompt_refs=[
                "為新進員工設計結構化的到職培訓計畫。",
                "根據角色需求與技能缺口建立個人化技能發展方案。",
                "追蹤學習進度並據此調整培訓策略。",
            ],
        ),
        RoleConfig(
            id=ACQUISITION_SPECIALIST_ROLE_ID,
            name="Acquisition Specialist",
            icon="target",
            responsibility="在共享工作區中發現、驗證、準備並回報任務關鍵的外部輸入資料。",
            reports_to="coo",
            preferred_external_agent="claude_code",
            tools=list(_CORPORATE_DATA_ACQUISITION_TOOLS),
            prompt_refs=[
                "以四階段執行資料獲取：發現、驗證、準備、回報。",
                "對於媒體任務，HTML 快照與 URL 清單絕不視為已獲取的二進位資產。",
                "透過 shell_exec 使用標準 CLI 下載工具，而非臨時撰寫的網路腳本。",
            ],
        ),
        RoleConfig(
            id="senior_engineer",
            name="Senior Engineer",
            icon="terminal",
            responsibility="程式碼實作、系統開發與技術執行。",
            reports_to="cto",
            preferred_external_agent="codex",
            runtime_policy=RoleRuntimePolicyConfig(execution_strategy="auto"),
            tools=list(_CORPORATE_EXECUTION_TOOLS),
            prompt_refs=["撰寫乾淨、經過測試的程式碼。為審查者留下清晰的文件。"],
        ),
        RoleConfig(
            id="devops_engineer",
            name="DevOps Engineer",
            icon="settings",
            responsibility="基礎設施、部署、CI/CD、監控與營運強化。",
            reports_to="cto",
            preferred_external_agent="cursor",
            tools=list(_CORPORATE_EXECUTION_TOOLS),
            prompt_refs=["優先考慮營運安全性、可觀測性與部署就緒度。"],
        ),
        RoleConfig(
            id="content_specialist",
            name="Content Specialist",
            icon="writing",
            responsibility="文件撰寫、文案創作、簡報製作與面向使用者的寫作。",
            reports_to="cmo",
            tools=list(_CORPORATE_EXECUTION_TOOLS),
            prompt_refs=["為目標受眾清晰撰寫。打磨交付物。"],
        ),
        RoleConfig(
            id="designer",
            name="Designer",
            icon="design",
            responsibility="視覺設計、UX 產出物、線框圖與設計系統工作。",
            reports_to="cmo",
            tools=list(_CORPORATE_EXECUTION_TOOLS),
            prompt_refs=["專注於易用性、視覺一致性與設計品質。"],
        ),
        RoleConfig(
            id="qa_analyst",
            name="QA Analyst",
            icon="bug",
            responsibility="測試、安全審查、合規檢查與驗收確認。",
            reports_to="coo",
            tools=list(_CORPORATE_QA_TOOLS),
            prompt_refs=["嚴格測試。拒絕不清晰或不安全的輸出。"],
        ),
        RoleConfig(
            id="env_engineer",
            name="Environment Engineer",
            icon="database",
            responsibility=(
                "探測主機環境、安裝所需工具與相依套件、"
                "準備指定的 target_output_dir 與基礎工作區目錄、"
                "設定運行時環境（conda/venv/docker/系統套件），"
                "並為下游執行工作項目產出經驗證的環境清單。"
                "支援任何工具鏈：影片編輯（FFmpeg、DaVinci）、3D 引擎（Unity、Unreal、Blender、Godot）、"
                "音訊處理（FMOD、Wwise、SoX）、ML/AI 框架（PyTorch、TensorFlow）、"
                "遊戲開發 SDK、設計工具，以及任務所需的任何其他軟體。"
            ),
            reports_to="cto",
            skill_refs=["env_provisioning"],
            runtime_policy=RoleRuntimePolicyConfig(
                execution_strategy="native",
                default_turn_type="setup",
                shell_timeout_override=1800,
            ),
            tools=list(_CORPORATE_ENV_TOOLS),
            prompt_refs=[
                "在嘗試安裝之前，務必先探測已安裝的內容。",
                "在下游工作項目執行前準備指定的 target_output_dir，包括任何缺失的上層目錄與基礎工作區資料夾。",
                "產出結構化的 environment_manifest JSON 作為最終產出物。",
                "包含下游工作項目可用來驗證環境的驗證指令。",
                "系統工具優先使用系統套件管理器（apt、brew、dnf），Python 套件使用 pip/conda/uv。",
                "需要 GPU 時，檢查 CUDA/ROCm 可用性與驅動程式版本。",
                "對於複雜環境，建立隔離環境（conda/venv）而非污染主機。",
            ],
        ),
    ], configured_roles)

