"""OPC 系統配置管理模組。

職責說明：
    定義整個 OPC 系統的配置模型（Pydantic BaseModel）和配置讀寫邏輯。
    包括：
    - 路徑工具函數（OPC 主目錄、專案根目錄、工作區）
    - 公司組織配置的讀寫、遷移、驗證
    - 所有子系統的配置模型（LLM、代理、頻道、自主性、能力等）
    - OPCConfig 頂層配置的 load/save 生命週期

關聯關係：
    - 被 opc/engine.py 在啟動時載入配置
    - 被 opc/cli/app.py 在 CLI 命令中讀寫配置
    - 被 opc/core/org_config.py 和 opc/core/employee_registry.py 使用
    - 被所有 layer 模組讀取各自相關的配置段

使用範例：
    from opc.core.config import OPCConfig, get_opc_home
    config = OPCConfig.load()
    print(config.llm.default_model)
"""

from __future__ import annotations  # 啟用延遲型別註解評估，支援前向引用

import os  # 標準庫：環境變數讀取（OPC_HOME）、檔案同步（fsync）
import re  # 標準庫：正規表達式（組織 ID 驗證、slug 化）
import tempfile  # 標準庫：原子寫入用的臨時檔案
import unicodedata  # 標準庫：Unicode 正規化（組織名稱 slug 化）
from pathlib import Path  # 標準庫：跨平台路徑操作
from typing import Any, Literal  # 標準庫：型別註解

import yaml  # 第三方庫 PyYAML：YAML 配置檔案的序列化/反序列化
from pydantic import AliasChoices, BaseModel, Field, field_validator  # Pydantic：配置模型驗證

from opc.core.company_tools import COMPANY_APPROVAL_EXEMPT_TOOL_NAMES  # 審批豁免工具名稱集合


# ---------------------------------------------------------------------------
# 路徑工具函數 — 定位專案根目錄、OPC 主目錄、工作區
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    """從當前工作目錄向上尋找 pyproject.toml 以定位專案根目錄（內部輔助函數）。

    返回值：
        Path — 包含 pyproject.toml 的目錄，找不到時返回 cwd。
    """
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return cwd


def get_opc_home() -> Path:
    """取得 OPC 資料目錄路徑。

    解析優先順序：
      1. $OPC_HOME 環境變數（明確覆蓋）
      2. {project_root}/.opc（專案本地，預設值）

    返回值：
        Path — OPC 主目錄路徑。

    被誰引用：
        - OPCConfig.load()：定位配置目錄
        - opc/cli/app.py：各種 CLI 命令
    """
    env = os.environ.get("OPC_HOME")
    if env:
        return Path(env)
    return _find_project_root() / ".opc"


def get_default_workplace_root() -> Path:
    """取得預設共享工作區根目錄（OpenOPC 倉庫旁）。

    返回值：
        Path — {project_root}_workplace 路徑。
    """
    project_root = _find_project_root()
    return project_root.parent / f"{project_root.name}_workplace"


def get_project_workplace(project_id: str) -> Path:
    """取得指定專案的工作區路徑。

    參數：
        project_id (str)：專案 ID。

    返回值：
        Path — 專案工作區路徑。
    """
    project = str(project_id or "default").strip() or "default"
    return get_default_workplace_root() / project


def get_project_config_dir(project_path: Path | None = None) -> Path:
    """取得專案配置目錄路徑。

    參數：
        project_path (Path | None)：專案路徑。None 時返回 OPC 主目錄。

    返回值：
        Path — 配置目錄路徑。
    """
    if project_path:
        return project_path / ".opc"
    return get_opc_home()


def _atomic_write_text(path: Path, content: str) -> None:
    """透過 fsync 和同目錄原子替換寫入文字檔案（內部輔助函數）。

    功能：
        先寫入臨時檔案並 fsync，再原子替換目標檔案。
        確保寫入過程中不會產生損壞的檔案。

    參數：
        path (Path)：目標檔案路徑。
        content (str)：要寫入的文字內容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = Path(f.name)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def _atomic_write_yaml(path: Path, data: Any) -> None:
    """原子寫入 YAML 檔案（內部輔助函數）。"""
    _atomic_write_text(path, yaml.dump(data, default_flow_style=False))


# ---------------------------------------------------------------------------
# 公司組織配置佈局 — 常量和路徑/驗證工具函數
# ---------------------------------------------------------------------------

COMPANY_INDEX_FILENAME = "company_index.yaml"  # 公司索引檔案名稱（記錄活動組織 ID）
COMPANY_ORGS_DIRNAME = "company_orgs"  # 組織配置目錄名稱
COMPANY_ORG_KIND = "opc_org_architecture"  # 組織配置檔案的 kind 標記
COMPANY_INDEX_SCHEMA_VERSION = 1  # 公司索引的 schema 版本號
COMPANY_ORG_SCHEMA_VERSION = 2  # 組織配置的 schema 版本號
DEFAULT_ORGANIZATION_ID = "corporate"  # 預設組織 ID（內建企業模板）
_COMPANY_ORG_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")  # 組織 ID 合法性正規表達式
_COMPANY_ORG_FILE_RE = re.compile(r"^(?:company|org)_([a-z0-9_-]{1,64})_config\.yaml$")  # 組織配置檔案名稱匹配
_ORG_STRUCTURE_KEYS = ("roles", "employees", "escalation_rules")  # 組織結構相關鍵
_ORG_RUNTIME_KEYS = (  # 組織運行時相關鍵
    "runtime_policies",
    "talent_templates",
    "teams",
    "team_runtime",
    "installed_packages",
    "role_serial_queue_enabled",
)
_ORG_BEARING_KEYS = (  # 所有組織配置承載鍵（結構 + 運行時）
    "company",
    *_ORG_STRUCTURE_KEYS,
    *_ORG_RUNTIME_KEYS,
)


def company_index_path(config_dir: Path) -> Path:
    """取得公司索引檔案的完整路徑。"""
    return Path(config_dir) / COMPANY_INDEX_FILENAME


def company_orgs_dir(config_dir: Path) -> Path:
    """取得公司組織配置目錄的完整路徑。"""
    return Path(config_dir) / COMPANY_ORGS_DIRNAME


def slugify_organization_name(name: Any, *, fallback: str = "org") -> str:
    """將使用者可見的組織名稱轉換為安全的檔案 ID（slug 化）。

    參數：
        name：組織名稱（任意型別，會被轉為字串）。
        fallback (str)：名稱無效時的回退值。

    返回值：
        str — 符合 [a-z0-9_-]{1,64} 的安全 ID。
    """
    raw = str(name or "").strip()
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    lowered = re.sub(r"\s+", "_", lowered)
    lowered = re.sub(r"[^a-z0-9_-]+", "_", lowered)
    lowered = re.sub(r"_+", "_", lowered).strip("_-")
    if not lowered:
        lowered = str(fallback or "org").strip().lower()
        lowered = re.sub(r"[^a-z0-9_-]+", "_", lowered)
        lowered = re.sub(r"_+", "_", lowered).strip("_-") or "org"
    return lowered[:64].strip("_-") or "org"


def validate_organization_id(value: Any) -> str:
    """驗證組織 ID 的合法性（必須匹配 [a-z0-9_-]{1,64}）。

    異常：
        ValueError — ID 不合法。
    """
    org_id = str(value or "").strip()
    if not _COMPANY_ORG_ID_RE.match(org_id):
        raise ValueError(f"Invalid organization_id: {org_id!r}")
    return org_id


def company_org_filename(organization_id: Any) -> str:
    """根據組織 ID 產生配置檔案名稱（org_{id}_config.yaml）。"""
    return f"org_{validate_organization_id(organization_id)}_config.yaml"


def organization_id_from_company_org_filename(path: Path) -> str | None:
    """從配置檔案名稱反向解析組織 ID。找不到匹配時返回 None。"""
    match = _COMPANY_ORG_FILE_RE.match(Path(path).name)
    return match.group(1) if match else None


def company_org_path(config_dir: Path, organization_id: Any) -> Path:
    """取得指定組織的配置檔案完整路徑。"""
    return company_orgs_dir(config_dir) / company_org_filename(organization_id)


def company_org_relative_path(organization_id: Any) -> str:
    """取得指定組織的配置檔案相對路徑（相對於 config_dir）。"""
    return f"{COMPANY_ORGS_DIRNAME}/{company_org_filename(organization_id)}"


def _read_yaml_file(path: Path) -> dict[str, Any]:
    """讀取 YAML 檔案並確保頂層為 mapping（內部輔助函數）。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    return data


def _write_yaml_preserving_unicode(path: Path, data: Any) -> None:
    """原子寫入 YAML 檔案，保留 Unicode 字元（內部輔助函數）。"""
    _atomic_write_text(
        path,
        yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
    )


def read_company_index(config_dir: Path) -> str | None:
    """讀取公司索引檔案，返回活動組織 ID。

    返回值：
        str | None — 活動組織 ID，索引不存在時返回 None。
    """
    path = company_index_path(config_dir)
    if not path.exists():
        return None
    data = _read_yaml_file(path)
    schema_version = int(data.get("schema_version", COMPANY_INDEX_SCHEMA_VERSION) or COMPANY_INDEX_SCHEMA_VERSION)
    if schema_version > COMPANY_INDEX_SCHEMA_VERSION:
        raise ValueError(
            f"{COMPANY_INDEX_FILENAME} schema_version {schema_version} is not supported by this version of OpenOPC"
        )
    active_id = data.get("active_organization_id") or DEFAULT_ORGANIZATION_ID
    return validate_organization_id(active_id)


def write_company_index(config_dir: Path, organization_id: Any) -> None:
    """寫入公司索引檔案（設定活動組織 ID）。"""
    org_id = validate_organization_id(organization_id)
    _write_yaml_preserving_unicode(
        company_index_path(config_dir),
        {
            "schema_version": COMPANY_INDEX_SCHEMA_VERSION,
            "active_organization_id": org_id,
        },
    )


def write_company_org_payload(config_dir: Path, organization_id: Any, payload: dict[str, Any]) -> Path:
    """寫入公司組織配置 payload 到 YAML 檔案。

    功能：
        1. 若 payload 包含員工，先外部化到 employee_registry
        2. 清空 payload 中的 employees 和 talent_templates
        3. 附加 metadata 並原子寫入

    參數：
        config_dir (Path)：配置目錄。
        organization_id：組織 ID。
        payload (dict)：組織配置 payload。

    返回值：
        Path — 寫入的檔案路徑。
    """
    org_id = validate_organization_id(organization_id)
    path = company_org_path(config_dir, org_id)
    payload = dict(payload or {})
    raw_employees = list(payload.get("employees", []) or [])
    if raw_employees:
        from opc.core.employee_registry import load_employee_registry, write_employee_registry

        opc_home = Path(config_dir).parent
        existing = load_employee_registry(opc_home, org_id)
        write_employee_registry(opc_home, org_id, [*existing, *raw_employees])
    payload["organization_id"] = org_id
    payload.setdefault("schema_version", COMPANY_ORG_SCHEMA_VERSION)
    payload.setdefault("kind", COMPANY_ORG_KIND)
    payload["employees"] = []
    payload["talent_templates"] = []
    payload["metadata"] = {
        **dict(payload.get("metadata", {}) or {}),
        "organization_config_file": company_org_relative_path(org_id),
    }
    _write_yaml_preserving_unicode(path, payload)
    return path


def list_company_org_config_paths(config_dir: Path) -> list[Path]:
    """列出所有公司組織配置檔案路徑（按名稱排序）。"""
    orgs_dir = company_orgs_dir(config_dir)
    if not orgs_dir.is_dir():
        return []
    return sorted(path for path in orgs_dir.glob("org_*_config.yaml") if organization_id_from_company_org_filename(path))


def allocate_organization_id(config_dir: Path, organization_name: Any, *, preferred_id: Any = "") -> str:
    """分配一個不重複的組織 ID（內部輔助函數）。

    邏輯：優先使用 preferred_id，否則 slug 化組織名稱。
    若已存在則附加數字後綴（_2、_3...）直到不重複。
    """
    base = str(preferred_id or "").strip()
    if base and _COMPANY_ORG_ID_RE.match(base):
        candidate = base
    else:
        candidate = slugify_organization_name(organization_name)
    existing = {
        org_id
        for path in list_company_org_config_paths(config_dir)
        for org_id in [organization_id_from_company_org_filename(path)]
        if org_id
    }
    if candidate not in existing:
        return candidate
    suffix = 2
    while True:
        tail = f"_{suffix}"
        stem = candidate[: max(1, 64 - len(tail))].rstrip("_-") or "org"
        next_id = f"{stem}{tail}"
        if next_id not in existing:
            return next_id
        suffix += 1


# ---------------------------------------------------------------------------
# 配置模型（Config Models）— Pydantic BaseModel 定義
# 每個模型對應配置檔案中的一個區段或子系統
# ---------------------------------------------------------------------------

class LLMConfig(BaseModel):
    """LLM 配置 — 大型語言模型的連接和參數設定。"""
    default_model: str = "deepseek/deepseek-chat"  # 預設模型（litellm 格式，國內優先）
    api_base: str = ""  # API 基礎 URL（自建代理時）
    api_key: str = ""  # API 金鑰（直接指定）
    api_key_env: str = ""  # API 金鑰環境變數名稱
    routing: dict[str, str] = Field(default_factory=dict)  # 模型路由映射（用途→模型）
    fallback: dict[str, Any] = Field(default_factory=dict)  # 回退配置
    temperature: float = 0.3  # 生成溫度（0.0~1.0）
    max_tokens: int = 32768  # 最大輸出 Token 數
    # 活動模型的總輸入上下文視窗（Token 數）。當模型未在 litellm 中映射時
    # （如代理/自託管模型 doubao/minimax/glm）設定此值，使上下文使用環和
    # 壓縮閾值有真實的分母。0 = 透過 litellm 自動偵測；未映射模型回退到 128000。
    # 可選的按模型名稱覆蓋優先於純量值。
    context_window: int = 0  # 上下文視窗大小（0=自動偵測）
    context_window_overrides: dict[str, int] = Field(default_factory=dict)  # 按模型的上下文視窗覆蓋
    # 預算感知調度：模型分層路由和降級鏈
    tier_routing: dict[str, str] = Field(default_factory=dict)  # 模型層級路由（tier→model）
    degrade_chain: dict[str, str] = Field(default_factory=dict)  # 降級鏈（tier→fallback_model）

    def get_model_for_tier(self, tier: str, degraded: bool = False) -> str | None:
        """取得指定層級的模型。

        Args:
            tier: 模型層級（critical, reasoning, routine, summary）
            degraded: 是否使用降級模型

        Returns:
            模型名稱，如果未配置則返回 None。
        """
        if degraded and tier in self.degrade_chain:
            return self.degrade_chain[tier]
        if tier in self.tier_routing:
            return self.tier_routing[tier]
        return None


ExternalAgentApprovalMode = Literal["user-settings", "auto", "full-auto"]  # 外部代理審批模式型別
_EXTERNAL_AGENT_APPROVAL_MODES = {"user-settings", "auto", "full-auto"}  # 合法的審批模式集合
_LEGACY_EXTERNAL_AGENT_APPROVAL_MODE_MIGRATIONS = {  # 舊版審批模式遷移映射
    "delegate": "auto",
    "bypass": "auto",
}
_LEGACY_OPENCODE_DEFAULT_MODEL = "opencode/minimax-m2.5-free"  # 舊版 opencode 預設模型


def _migrate_agent_config_approval_modes(path: Path, data: Any) -> Any:
    """Rewrite pre-three-mode external-agent approval config once on load."""
    if not isinstance(data, dict):
        return data
    external_agents = data.get("external_agents")
    if not isinstance(external_agents, dict):
        return data

    changed = False
    for agent_name, agent_data in external_agents.items():
        if agent_name == "preferred_order" or not isinstance(agent_data, dict):
            continue
        raw_mode = agent_data.get("approval_mode")
        mode = str(raw_mode or "").strip().lower()
        if mode in _EXTERNAL_AGENT_APPROVAL_MODES:
            continue
        migrated = _LEGACY_EXTERNAL_AGENT_APPROVAL_MODE_MIGRATIONS.get(mode)
        if not migrated:
            continue
        agent_data["approval_mode"] = migrated
        changed = True

    if changed:
        _atomic_write_yaml(path, data)
    return data


DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS = 300


def _migrate_agent_config_external_agent_defaults(path: Path, data: Any) -> Any:
    """Repair legacy external-agent defaults that break local CLI config."""
    if not isinstance(data, dict):
        return data
    external_agents = data.get("external_agents")
    if not isinstance(external_agents, dict):
        return data

    changed = False
    opencode = external_agents.get("opencode")
    if isinstance(opencode, dict) and str(opencode.get("model") or "").strip() == _LEGACY_OPENCODE_DEFAULT_MODEL:
        opencode["model"] = ""
        changed = True

    if changed:
        _atomic_write_yaml(path, data)
    return data


class ExternalAgentConfig(BaseModel):
    """外部代理配置 — 單一外部代理（如 Claude Code、Cursor）的連接設定。"""
    enabled: bool = True  # 是否啟用
    command: str = ""
    workspace_base: str = ""
    extra_args: list[str] = Field(default_factory=list)
    model: str = ""
    model_flag: str = ""
    session_mode: str = "auto"
    session_id: str = ""
    new_session_flag: str = ""
    resume_session_flag: str = ""
    run_mode: str = "batch"
    interactive_timeout_seconds: int = 900
    idle_timeout_seconds: int = 900
    startup_timeout_seconds: int = DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS
    status_heartbeat_seconds: int = 30
    approval_mode: ExternalAgentApprovalMode = "auto"
    show_thinking: bool = False
    auth_type: str = ""  # 認證類型（如 api_key, oauth），留空表示未配置


class AgentsConfig(BaseModel):
    """代理配置 — 外部代理和原生子代理的整體設定。"""
    preferred_order: list[str] = Field(default_factory=lambda: ["qwen_code", "opencode", "claude_code", "cursor", "codex"])
    agents: dict[str, ExternalAgentConfig] = Field(default_factory=lambda: {
        "claude_code": ExternalAgentConfig(command="claude", run_mode="interactive", approval_mode="full-auto"),
        "cursor": ExternalAgentConfig(command="cursor-agent", run_mode="interactive", approval_mode="full-auto"),
        "codex": ExternalAgentConfig(command="codex", run_mode="interactive"),
        "opencode": ExternalAgentConfig(
            command="opencode",
            model_flag="--model",
            run_mode="interactive",
            approval_mode="full-auto",
            show_thinking=True,
        ),
        "qwen_code": ExternalAgentConfig(
            command="qwen-code",
            run_mode="interactive",
            approval_mode="full-auto",
            show_thinking=True,
            auth_type="openai",
        ),
    })
    native_subagents: dict[str, "NativeSubagentProfileConfig"] = Field(default_factory=dict)


class RoleRuntimePolicyConfig(BaseModel):
    """角色運行時策略配置 — 定義角色的執行策略和協調偏好。"""
    execution_strategy: str = "auto"
    allowed_downstream_roles: list[str] = Field(default_factory=list)
    review_role: str | None = None
    default_turn_type: str = "work"
    shell_timeout_override: int | None = None
    setup_env_type: str | None = None
    coordination_hints: dict[str, Any] = Field(default_factory=dict)
    signal_capabilities: list[str] = Field(default_factory=list)
    parallelism_constraints: list[str] = Field(default_factory=list)
    gate_preferences: dict[str, Any] = Field(default_factory=dict)


class CommunicationPolicyConfig(BaseModel):
    """通訊策略配置 — 代理間通訊的預設行為。"""
    default_mode: str = "dm"
    blocking_default: bool = False
    meeting_required_for: list[str] = Field(default_factory=list)
    allow_broadcast: bool = True


class MemoryPolicyConfig(BaseModel):
    """記憶策略配置 — 控制哪些記憶內容注入到代理上下文。"""
    include_role_memory: bool = True
    include_project_memory: bool = True
    include_decision_log: bool = True
    include_artifact_index: bool = True
    recent_history_lines: int = 12


class HandoffPolicyConfig(BaseModel):
    """交接策略配置 — 控制工作交接的行為。"""
    require_structured_handoff: bool = True
    require_ack: bool = False
    include_risks: bool = True
    include_open_questions: bool = True


class ArtifactPolicyConfig(BaseModel):
    """產出物策略配置 — 控制產出物合約的執行。"""
    enforce_contract: bool = False
    require_artifact_index: bool = True
    required_kinds: list[str] = Field(default_factory=list)


class ReviewPolicyConfig(BaseModel):
    """審查策略配置 — 控制工作項目審查門禁。"""
    enable_work_item_gates: bool = False
    strict_gate_inference: bool = False
    require_reviewer_role: bool = True
    allow_human_override: bool = True


def _default_gate_harness_blocker_map() -> dict[str, str]:
    return {
        "workspace_mismatch": "rework_same_work_item",
        "missing_required_artifact": "rework_same_work_item",
        "verification_missing": "rework_same_work_item",
        "dependency_not_ready": "rework_same_work_item",
        "cross_work_item_conflict": "replan",
        "external_secret_missing": "await_user_decision",
        "permission_missing": "escalate",
        "environment_capability_gap": "pass_with_constraints",
        "quality_gap": "rework_same_work_item",
        "goal_env_mismatch": "replan",
        "unresolved_user_visible_risk": "pass_with_constraints",
    }


class GateHarnessPolicyConfig(BaseModel):
    """門禁策略配置 — 控制工作項目品質門禁的推斷和執行。"""
    enabled: bool = True
    decision_mode: Literal["rule_first", "agent_first", "hybrid"] = "agent_first"
    default_degrade_policy: Literal["allow", "strict", "replan_first"] = "allow"
    auto_infer_turn_kind: bool = True
    auto_infer_gate_profile: bool = True
    max_rework_rounds_per_issue: int = 2
    stagnation_threshold: int = 2
    allow_pass_with_constraints: bool = True
    enable_delivery_constraints_propagation: bool = True
    agent_model: str = ""
    agent_confidence_threshold: float = 0.55
    fallback_to_rules_on_parse_error: bool = True
    builtin_blocker_map: dict[str, str] = Field(default_factory=_default_gate_harness_blocker_map)
    turn_kind_overrides: dict[str, str] = Field(default_factory=dict)
    gate_profile_overrides: dict[str, str] = Field(default_factory=dict)


class ParallelPolicyConfig(BaseModel):
    """並行策略配置 — 控制工作項目的並行分發。"""
    auto_dispatch: bool = True
    review_gate_enabled: bool = True
    max_workers: int = 10


class CoordinationPolicyConfig(BaseModel):
    """協調策略配置 — 控制工作項目間的協調推斷。"""
    inference_mode: Literal["llm_primary", "rules_first"] = "llm_primary"
    fallback_mode: Literal["conservative", "balanced"] = "conservative"
    strict_gate_turn_kinds: list[str] = Field(default_factory=lambda: ["verify", "deliver"])
    mixed_gate_turn_kinds: list[str] = Field(default_factory=lambda: ["synthesize", "review", "integration"])
    allow_manager_release_for_mixed_only: bool = True
    allow_custom_signals: bool = True


class HeartbeatConfig(BaseModel):
    """心跳配置 — 代理定期心跳的設定。"""
    enabled: bool = False
    default_interval_sec: int = 300
    max_concurrent_runs: int = 1


class BrowserConfig(BaseModel):
    """瀏覽器配置 — 瀏覽器工具的設定。"""
    mode: Literal["embedded", "chrome", "auto"] = "embedded"
    headless: bool = True
    chrome_channel: str = "chrome"
    chrome_executable_path: str = ""
    user_data_dir: str = ""
    args: list[str] = Field(default_factory=list)


class TaskModeConfig(BaseModel):
    """任務模式配置 — 任務模式下的子代理設定。"""
    max_sub_agents: int = 8
    sub_agent_timeout_sec: int = 86400
    allow_parallel_dispatch: bool = True


ProjectModeConfig = TaskModeConfig  # 向下相容別名


class PromptPrefixStabilityConfig(BaseModel):
    """Prompt 前綴穩定性配置 — 優化 LLM 快取命中率。"""
    enabled: bool = True
    separate_dynamic_context: bool = True
    emit_cache_fingerprint_events: bool = True


class PromptHarnessConfig(BaseModel):
    """Prompt 組裝配置 — 控制 Prompt 的靜態/動態分離和增量更新。"""
    enabled: bool = True
    split_static_dynamic: bool = True
    emit_delta_messages: bool = True
    cache_static_prefix_only: bool = True
    artifact_messages_enabled: bool = True
    section_dedup_enabled: bool = True
    reinject_after_compaction: bool = True


class ReactiveCompactionConfig(BaseModel):
    """反應式壓縮配置 — 上下文溢出時的自動壓縮。"""
    enabled: bool = True
    max_overflow_retries: int = 2
    circuit_breaker_failures: int = 2


class ContextUsageReportingConfig(BaseModel):
    """上下文使用報告配置 — 追蹤上下文視窗使用率。"""
    enabled: bool = True
    emit_runtime_events: bool = True


class VerificationPolicyConfig(BaseModel):
    """驗證策略配置 — 控制任務完成後的自動驗證。"""
    enabled: bool = True
    min_todos_for_verification: int = 3
    require_on_code_edits: bool = True
    require_on_risky_tools: bool = True
    verifier_profile: str = "verify"
    skip_metadata_key: str = "skip_verification"


class BackgroundSessionMemoryConfig(BaseModel):
    """背景工作階段記憶配置 — 背景更新的長期記憶。"""
    enabled: bool = True
    update_interval_messages: int = 4
    max_input_chars: int = 6_000


class PrefetchConfig(BaseModel):
    """預取配置 — 控制回合開始前預取的上下文內容。"""
    enabled: bool = True
    session_memory: bool = True
    focused_memory: bool = True
    skills_summary: bool = True
    project_memory_candidates: bool = True
    max_chars: int = 4_000


class ToolAwareMicrocompactConfig(BaseModel):
    """工具感知微壓縮配置 — 壓縮歷史時保留關鍵工具輸出。"""
    enabled: bool = True
    preserve_recent_messages: int = 8
    tool_result_char_budget: int = 4_000
    assistant_char_budget: int = 3_000
    preserve_failure_outputs: bool = True


class TaskLedgerConfig(BaseModel):
    """任務帳本配置 — 追蹤任務的待辦事項和進度。"""
    enabled: bool = True
    max_items: int = 24
    persist_to_runtime_session: bool = True
    persist_to_task_metadata: bool = True
    emit_runtime_events: bool = True


class StreamingToolStartConfig(BaseModel):
    """串流工具啟動配置 — 控制 LLM 串流輸出時提前啟動工具呼叫。"""
    enabled: bool = True  # 是否啟用串流工具提前啟動
    safe_read_only_only: bool = True  # 僅對唯讀工具啟用（安全限制）
    require_allow_prediction: bool = True  # 需要允許預測標記才啟動


class StreamRenderingConfig(BaseModel):
    """串流渲染配置 — 控制終端/UI 的串流輸出渲染行為（深度、延遲、tick）。"""
    enabled: bool = True  # 是否啟用串流渲染
    enter_depth: int = 8  # 進入渲染的巢狀深度閾值
    enter_age_ms: int = 120  # 進入渲染的訊息存活時間（毫秒）
    exit_depth: int = 2  # 退出渲染的巢狀深度閾值
    exit_age_ms: int = 40  # 退出渲染的訊息存活時間（毫秒）
    exit_hold_ms: int = 250  # 退出後的保持時間（毫秒）
    reenter_hold_ms: int = 250  # 重新進入的保持時間（毫秒）
    tick_ms: int = 50  # 渲染 tick 間隔（毫秒）


class ContextGuardConfig(BaseModel):
    """上下文守衛配置 — 監控上下文視窗使用率並觸發壓縮/截斷。"""
    enabled: bool = True  # 是否啟用上下文守衛
    soft_threshold: float = 0.60  # 軟閾值（開始警告）
    hard_threshold: float = 0.80  # 硬閾值（強制壓縮）
    warn_remaining_pct: int = 15  # 剩餘百分比低於此值時警告
    tool_output_char_budget: int = 12_000  # 工具輸出字元預算
    shell_stdout_char_budget: int = 12_000  # Shell stdout 字元預算
    shell_stderr_char_budget: int = 6_000  # Shell stderr 字元預算


class VerificationContractConfig(BaseModel):
    """驗證契約配置 — 要求最終回覆包含明確的驗證狀態。"""
    enabled: bool = True  # 是否啟用驗證契約
    append_status_to_final: bool = True  # 在最終回覆附加驗證狀態
    require_explicit_status: bool = True  # 要求明確的驗證狀態標記


class WorktreeVenvConfig(BaseModel):
    """Worktree 虛擬環境配置 — 為子代理建立隔離的 Python 虛擬環境。"""
    enabled: bool = False  # 是否啟用 worktree venv
    provider: Literal["auto", "uv", "venv"] = "auto"  # 虛擬環境建立工具
    venv_dir: str = ".opc-venv"  # 虛擬環境目錄名稱
    editable_project: bool = True  # 是否以 editable 模式安裝專案
    requirements_files: list[str] = Field(default_factory=list)  # 額外 requirements 檔案
    auto_detect_requirements: bool = True  # 自動偵測 requirements.txt
    system_site_packages: bool = False  # 是否繼承系統 site-packages
    fail_if_prepare_fails: bool = False  # 準備失敗時是否中止


class SandboxPlatformConfig(BaseModel):
    """沙箱平台配置 — 單一平台的沙箱模式和包裝器設定。"""
    mode: Literal["inherit", "off", "workspace-write", "elevated"] = "inherit"  # 沙箱模式
    wrapper: Literal["auto", "none", "bwrap", "sandbox-exec"] = "auto"  # 沙箱包裝器


class SandboxExecutionConfig(BaseModel):
    """沙箱執行配置 — 控制工具執行的沙箱隔離策略（跨平台）。"""
    enabled: bool = False  # 是否啟用沙箱
    default_mode: Literal["off", "workspace-write", "elevated"] = "off"  # 預設沙箱模式
    fail_if_unavailable: bool = False  # 沙箱不可用時是否中止
    allow_direct_fallback: bool = True  # 允許降級為直接執行
    allow_network: bool = True  # 允許網路存取
    windows: SandboxPlatformConfig = Field(  # Windows 平台設定
        default_factory=lambda: SandboxPlatformConfig(mode="elevated", wrapper="none")
    )
    linux: SandboxPlatformConfig = Field(  # Linux 平台設定
        default_factory=lambda: SandboxPlatformConfig(mode="workspace-write", wrapper="auto")
    )
    macos: SandboxPlatformConfig = Field(  # macOS 平台設定
        default_factory=lambda: SandboxPlatformConfig(mode="workspace-write", wrapper="auto")
    )


class ExecutionEnvironmentConfig(BaseModel):
    """執行環境配置 — 聚合 worktree venv 和沙箱的頂層配置。"""
    worktree_venv: WorktreeVenvConfig = Field(default_factory=WorktreeVenvConfig)  # 虛擬環境配置
    sandbox: SandboxExecutionConfig = Field(default_factory=SandboxExecutionConfig)  # 沙箱配置


class ArtifactCompactionConfig(BaseModel):
    """產物壓縮配置 — 上下文壓縮後重新注入關鍵產物和狀態。"""
    enabled: bool = True  # 是否啟用產物壓縮
    session_memory_fast_path: bool = True  # 工作階段記憶快速路徑
    reinject_tool_surface_delta: bool = True  # 重新注入工具介面差異
    reinject_skills_delta: bool = True  # 重新注入技能差異
    reinject_active_subagents: bool = True  # 重新注入活動中的子代理
    reinject_verification_state: bool = True  # 重新注入驗證狀態
    reinject_permission_state: bool = True  # 重新注入權限狀態
    prompt_too_long_retry: bool = True  # prompt 過長時自動重試
    max_prompt_too_long_retries: int = 3  # 最大重試次數
    artifact_char_budget: int = 12_000  # 產物字元預算


class NativeRuntimeConfig(BaseModel):
    """原生運行時配置 — 內建代理運行時的核心參數（串流、事件、工具、壓縮等）。

    職責說明：
        控制原生子代理運行時的所有行為參數，包括 LLM 串流、
        事件協議、工具掛鉤、並行控制、歷史壓縮、子代理深度等。
        是 NativeRuntimeConfig 下所有子配置的聚合點。

    關聯關係：
        - 被 opc/layer3_agent/runtime_v2/ 讀取
        - 被 opc/engine.py 在建立運行時實例時使用
    """
    enabled: bool = True  # 是否啟用原生運行時
    stream_llm: bool = True  # 是否串流 LLM 輸出
    emit_runtime_events: bool = True  # 是否發送運行時事件
    event_protocol_version: str = "v2"  # 事件協議版本
    enable_tool_hooks: bool = True  # 是否啟用工具掛鉤
    converge_on_parallel_failure: bool = True  # 並行失敗時是否收斂
    max_parallel_read_tools: int = 6  # 最大並行唯讀工具數
    tool_result_budget_chars: int = 20_000  # 工具結果字元預算
    microcompact_chars: int = 8_000  # 微壓縮觸發字元數
    history_snip_trigger_messages: int = 40  # 歷史裁剪觸發訊息數
    subagent_max_depth: int = 3  # 子代理最大巢狀深度
    auto_extract_durable_memory: bool = False  # 自動提取持久記憶
    durable_memory_extract_min_messages: int = 4  # 持久記憶提取最少訊息數
    durable_memory_max_input_chars: int = 12_000  # 持久記憶提取最大輸入字元數
    prompt_prefix_stability: PromptPrefixStabilityConfig = Field(default_factory=PromptPrefixStabilityConfig)  # Prompt 前綴穩定性
    prompt_harness: PromptHarnessConfig = Field(default_factory=PromptHarnessConfig)  # Prompt 組裝
    reactive_compaction: ReactiveCompactionConfig = Field(default_factory=ReactiveCompactionConfig)  # 反應式壓縮
    context_usage_reporting: ContextUsageReportingConfig = Field(default_factory=ContextUsageReportingConfig)  # 上下文使用報告
    verification_policy: VerificationPolicyConfig = Field(default_factory=VerificationPolicyConfig)  # 驗證策略
    background_session_memory: BackgroundSessionMemoryConfig = Field(default_factory=BackgroundSessionMemoryConfig)  # 背景記憶
    prefetch: PrefetchConfig = Field(default_factory=PrefetchConfig)  # 預取
    tool_aware_microcompact: ToolAwareMicrocompactConfig = Field(default_factory=ToolAwareMicrocompactConfig)  # 工具感知微壓縮
    artifact_compaction: ArtifactCompactionConfig = Field(default_factory=ArtifactCompactionConfig)  # 產物壓縮
    task_ledger: TaskLedgerConfig = Field(default_factory=TaskLedgerConfig)  # 任務帳本
    streaming_tool_start: StreamingToolStartConfig = Field(default_factory=StreamingToolStartConfig)  # 串流工具啟動
    stream_rendering: StreamRenderingConfig = Field(default_factory=StreamRenderingConfig)  # 串流渲染
    context_guard: ContextGuardConfig = Field(default_factory=ContextGuardConfig)  # 上下文守衛
    verification_contract: VerificationContractConfig = Field(default_factory=VerificationContractConfig)  # 驗證契約
    execution_environment: ExecutionEnvironmentConfig = Field(default_factory=ExecutionEnvironmentConfig)  # 執行環境


class NativeSubagentProfileConfig(BaseModel):
    """原生子代理設定檔配置 — 定義單一子代理類型的運行參數。"""
    enabled: bool = True  # 是否啟用此設定檔
    model: str = ""  # 使用的 LLM 模型（空字串表示繼承預設）
    max_iterations: int = 24  # 最大迭代次數
    default_isolation: Literal["shared", "worktree"] = "shared"  # 預設隔離模式
    background: bool = False  # 是否在背景執行
    allowed_tools: list[str] = Field(default_factory=list)  # 允許使用的工具列表


class DenialMemoryConfig(BaseModel):
    """拒絕記憶配置 — 記住被拒絕的操作以避免重複詢問。"""
    enabled: bool = True  # 是否啟用拒絕記憶
    repeat_threshold: int = 2  # 重複拒絕次數閾值（超過後自動拒絕）


class GuardianConfig(BaseModel):
    """守護者配置 — 權限系統的安全守護策略。"""
    enabled: bool = True  # 是否啟用守護者
    auto_allow_read_only: bool = True  # 自動允許唯讀操作
    cache_upgrade_context: bool = True  # 快取權限升級上下文
    auto_retry_sandbox: bool = True  # 沙箱失敗時自動重試
    max_sandbox_retries: int = 1  # 沙箱最大重試次數


class PermissionsV2Config(BaseModel):
    """統一權限預測器配置（ApprovalEngine.predict）的運行時參數。

    職責說明：
        控制權限預測器的行為，包括工具白名單/黑名單、路徑限制、
        危險 Shell 模式偵測等。Shell 安全命令策略位於
        ``autonomy.safe_command_prefixes`` 加上內建的旗標審核分類器
        （``shell_safety.py``）；舊版重複欄位已棄用。

    關聯關係：
        - 被 opc/layer4_tools/ 的審批引擎使用
        - 被 opc/engine.py 在工具執行前進行權限裁決
    """

    enabled: bool = True  # 是否啟用權限預測器
    fail_closed: bool = True  # 失敗時是否關閉（安全優先）
    denial_memory: DenialMemoryConfig = Field(default_factory=DenialMemoryConfig)  # 拒絕記憶
    allow_tools: list[str] = Field(default_factory=list)  # 工具白名單
    deny_tools: list[str] = Field(default_factory=list)  # 工具黑名單
    allowed_paths: list[str] = Field(default_factory=list)  # 允許存取的路徑
    denied_paths: list[str] = Field(default_factory=list)  # 禁止存取的路徑
    guardian: GuardianConfig = Field(default_factory=GuardianConfig)  # 守護者配置
    dangerous_shell_patterns: list[str] = Field(default_factory=lambda: [  # 危險 Shell 命令正規表達式
        r"\brm\s+-rf\b",
        r"\bdrop\s+table\b",
        r"\btruncate\b",
        r"\bterraform\s+destroy\b",
        r"\bgit\s+push\s+--force\b",
    ])


def _default_native_subagents() -> dict[str, NativeSubagentProfileConfig]:
    """建立預設的原生子代理設定檔映射（內部輔助函數）。

    返回值：
        dict — 鍵為子代理類型名稱，值為對應的設定檔配置。
        包含 general、explore、plan、implement、verify 五種預設類型。
    """
    return {
        "general": NativeSubagentProfileConfig(),
        "explore": NativeSubagentProfileConfig(default_isolation="shared"),
        "plan": NativeSubagentProfileConfig(default_isolation="shared"),
        "implement": NativeSubagentProfileConfig(default_isolation="worktree"),
        "verify": NativeSubagentProfileConfig(default_isolation="worktree", background=True),
    }


class RuntimePolicyConfig(BaseModel):
    """運行時策略配置 — 公司模式中角色/組織的運行時行為策略集合。

    職責說明：
        聚合通訊、記憶、交接、產物、審查、門禁、並行、協調等
        所有運行時策略子配置。每個角色或組織設定檔可擁有獨立的策略。

    關聯關係：
        - 被 OrgConfig.runtime_policies 引用
        - 被 opc/layer2_organization/org_engine.py 解析和合併
    """
    communication: CommunicationPolicyConfig = Field(default_factory=CommunicationPolicyConfig)  # 通訊策略
    memory: MemoryPolicyConfig = Field(default_factory=MemoryPolicyConfig)  # 記憶策略
    handoff: HandoffPolicyConfig = Field(default_factory=HandoffPolicyConfig)  # 交接策略
    artifact: ArtifactPolicyConfig = Field(default_factory=ArtifactPolicyConfig)  # 產物策略
    review: ReviewPolicyConfig = Field(default_factory=ReviewPolicyConfig)  # 審查策略
    gate_harness: GateHarnessPolicyConfig = Field(default_factory=GateHarnessPolicyConfig)  # 門禁策略
    # 僅在公司模式工作項目運行時中使用
    parallel: ParallelPolicyConfig = Field(default_factory=ParallelPolicyConfig)  # 並行策略
    coordination: CoordinationPolicyConfig = Field(default_factory=CoordinationPolicyConfig)  # 協調策略


class CoordinatorPolicyConfig(BaseModel):
    """協調者策略配置 — 控制協調者角色的綜合/路由/生成行為。"""
    synthesis_mode: str = "on_work_item_complete"  # 綜合模式："on_work_item_complete" | "on_inbox_threshold" | "periodic"
    inbox_threshold: int = 3  # 收件匣閾值（達到後觸發綜合）
    auto_route: bool = True  # 是否自動路由訊息
    can_spawn_tasks: bool = True  # 是否可以生成新任務
    # 子項目完成比例達到此閾值後可提前開始綜合/交付
    partial_completion_threshold: float = 0.5
    # 硬依賴停滯超過此秒數後自動降級為軟依賴
    auto_downgrade_stall_seconds: int = 300
    # 連續無進展迴圈迭代超過此次數後強制推進
    max_stall_iterations: int = 3


class RoleConfig(BaseModel):
    """角色配置 — 公司模式中組織角色的完整定義。

    職責說明：
        定義一個組織角色的身份、職責、報告關係、可用工具、
        技能引用、運行時策略等。是公司模式組織架構的基本單元。

    關聯關係：
        - 被 OrgConfig.roles 持有
        - 被 opc/layer2_organization/ 用於建立代理實例
        - 被 opc/engine.py 在公司模式中讀取
    """
    id: str  # 角色唯一 ID
    name: str  # 角色顯示名稱
    responsibility: str  # 角色職責描述
    reports_to: str = "owner"  # 上級角色 ID（"owner" 表示直接報告給使用者）
    icon: str | None = None  # 角色圖示（可選）
    can_spawn: list[str] = Field(default_factory=list)  # 可以生成的子角色 ID 列表
    tools: list[str] = Field(default_factory=list)  # 可用工具列表
    preferred_external_agent: str | None = None  # 首選外部代理
    model: str = ""  # 角色專用 LLM 模型（空字串表示使用全域預設）
    prompt_refs: list[str] = Field(default_factory=list)  # Prompt 引用列表
    skill_refs: list[str] = Field(default_factory=list)  # 技能引用列表
    handoff_template_ref: str | None = None  # 交接模板引用
    memory_policy_ref: str | None = None  # 記憶策略引用
    artifact_contract_ref: str | None = None  # 產物契約引用
    runtime_policy: RoleRuntimePolicyConfig = Field(default_factory=RoleRuntimePolicyConfig)  # 角色運行時策略
    capabilities: list[str] = Field(default_factory=list)  # 能力標籤列表
    role_type: str = "worker"  # 角色類型："worker" | "coordinator" | "reviewer"
    coordinator_policy: CoordinatorPolicyConfig | None = None  # 協調者策略（僅 coordinator 類型）


class TalentTemplateConfig(BaseModel):
    """人才模板配置 — 可重複使用的角色模板（用於招聘系統）。"""
    id: str  # 模板唯一 ID
    name: str  # 模板名稱
    description: str = ""  # 模板描述
    category: str = ""  # 模板分類
    domains: list[str] = Field(default_factory=list)  # 適用領域
    tags: list[str] = Field(default_factory=list)  # 標籤
    prompt_ref: str = ""  # Prompt 引用路徑
    preferred_external_agent: str | None = None  # 首選外部代理
    source_repo: str = ""  # 來源倉庫
    source_path: str = ""  # 來源路徑
    source_revision: str = ""  # 來源版本
    metadata: dict[str, Any] = Field(default_factory=dict)  # 擴展 metadata


class EmployeeConfig(BaseModel):
    """員工配置 — 公司模式中的員工（席位的具體填充者）。

    職責說明：
        代表一個被「招聘」到組織中的 AI 員工實例。
        員工綁定到特定角色，可擁有獨立的 Prompt、技能和外部代理偏好。

    關聯關係：
        - 由 opc/core/employee_registry.py 管理持久化
        - 被 opc/layer2_organization/ 在公司模式中使用
        - 屬於某個 RoleConfig（透過 role_id）
    """
    employee_id: str  # 員工唯一 ID
    template_id: str = ""  # 來源人才模板 ID
    name: str  # 員工名稱
    role_id: str  # 所屬角色 ID
    description: str = ""  # 員工描述
    category: str = ""  # 員工分類
    domains: list[str] = Field(default_factory=list)  # 擅長領域
    tags: list[str] = Field(default_factory=list)  # 標籤
    prompt_refs: list[str] = Field(default_factory=list)  # Prompt 引用列表
    skill_refs: list[str] = Field(default_factory=list)  # 技能引用列表
    preferred_external_agent: str | None = None  # 首選外部代理
    seniority: str = "junior"  # 資歷等級（junior/mid/senior/lead）
    status: str = "active"  # 員工狀態（active/inactive/on_leave）
    metadata: dict[str, Any] = Field(default_factory=dict)  # 擴展 metadata


class EscalationRule(BaseModel):
    """升級規則 — 定義何時及如何向人類升級。"""
    condition: str  # 觸發條件描述
    action: str  # 執行的升級動作


class SeatConfig(BaseModel):
    """席位配置 — 組織中的工作席位（角色的執行實例槽位）。"""
    seat_id: str  # 席位唯一 ID
    name: str = ""  # 席位名稱
    role_id: str = ""  # 關聯的角色 ID
    seat_kind: str = "workspace"  # 席位類型（workspace/shared/manager）
    manager_seat_id: str | None = None  # 上級席位 ID
    manager_role_id: str | None = None  # 上級角色 ID
    shared_executor: bool = False  # 是否為共享執行器
    metadata: dict[str, Any] = Field(default_factory=dict)  # 擴展 metadata


class TeamConfig(BaseModel):
    """團隊配置 — 組織中的團隊定義（席位的邏輯分組）。"""
    team_id: str  # 團隊唯一 ID
    name: str = ""  # 團隊名稱
    description: str = ""  # 團隊描述
    seat_ids: list[str] = Field(default_factory=list)  # 團隊包含的席位 ID 列表
    seats: list[SeatConfig] = Field(default_factory=list)  # 團隊包含的席位配置
    metadata: dict[str, Any] = Field(default_factory=dict)  # 擴展 metadata


class TeamRuntimeConfig(BaseModel):
    """團隊運行時配置 — 團隊的運行時行為參數。"""
    default_team_id: str = ""  # 預設團隊 ID
    shared_role_session_scope: str = "team"  # 共享角色工作階段範圍
    allow_shared_role_sessions: bool = True  # 是否允許共享角色工作階段
    seat_refresh_interval_seconds: int = 30  # 席位刷新間隔（秒）
    metadata: dict[str, Any] = Field(default_factory=dict)  # 擴展 metadata


class OrgConfig(BaseModel):
    """組織配置 — 公司模式的完整組織架構定義。

    職責說明：
        定義整個公司模式的組織架構，包括角色、員工、團隊、
        運行時策略、升級規則等。是 OPCConfig.org 的型別。

    關聯關係：
        - 被 OPCConfig 持有（config.org）
        - 被 opc/layer2_organization/ 用於建立組織運行時
        - 由 load_company_org_payload() 從 YAML 載入
        - 由 OPCConfig.save() 持久化

    使用範例：
        config = OPCConfig.load()
        for role in config.org.roles:
            print(role.id, role.responsibility)
    """
    organization_id: str = DEFAULT_ORGANIZATION_ID  # 組織 ID
    organization_name: str = "My One-Person Company"  # 組織名稱
    organization_config_file: str = ""  # 組織配置檔案相對路徑
    company_name: str = "My One-Person Company"  # 公司名稱
    topology: str = "Corporate Structure"  # 組織拓撲描述
    default_mode: str = "task"  # 預設執行模式："task" 或 "company"
    company_profile: str = "corporate"  # 公司設定檔："corporate" 或 "custom"
    execution_model: str = "actor_runtime"  # 執行模型
    final_decider_role_id: str | None = None  # 最終決策者角色 ID
    company_profiles: list[str] = Field(default_factory=lambda: ["corporate", "custom"])  # 可用設定檔列表
    runtime_policies: dict[str, RuntimePolicyConfig] = Field(default_factory=dict)  # 運行時策略映射
    roles: list[RoleConfig] = Field(default_factory=list)  # 角色列表
    talent_templates: list[TalentTemplateConfig] = Field(default_factory=list)  # 人才模板列表
    employees: list[EmployeeConfig] = Field(default_factory=list)  # 員工列表
    teams: list[TeamConfig] = Field(default_factory=list)  # 團隊列表
    team_runtime: TeamRuntimeConfig = Field(default_factory=TeamRuntimeConfig)  # 團隊運行時配置
    escalation_rules: list[EscalationRule] = Field(default_factory=list)  # 升級規則列表
    installed_packages: list[Any] = Field(default_factory=list)  # 已安裝的套件列表
    # Fix 5 PR3 功能旗標。啟用後，當角色的工作階段正忙於其他事項時，
    # 可執行的工作項目會被附加到 role_runtime_session.pending_work_item_ids
    # 而非立即被認領。預設關閉以安全推出。
    role_serial_queue_enabled: bool = True

    @field_validator("default_mode", mode="before")
    @classmethod
    def _normalize_default_mode(cls, value: Any) -> Any:
        """將舊版 "project" 模式名稱正規化為 "task"。"""
        if isinstance(value, str) and value.strip().lower() == "project":
            return "task"
        return value


class MCPServerConfig(BaseModel):
    """MCP 伺服器配置 — Model Context Protocol 外部工具伺服器的連接設定。"""
    name: str = ""  # 伺服器名稱
    type: str = "local"  # 連接類型："local"（本地命令）或 "remote"（遠端 URL）
    command: list[str] = Field(default_factory=list)  # 本地啟動命令（僅 local）
    url: str = ""  # 遠端 URL（僅 remote）
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP 標頭（僅 remote）
    enabled: bool = True  # 是否啟用
    env: dict[str, str] = Field(default_factory=dict)  # 環境變數
    tools_filter: list[str] = Field(default_factory=list)  # 工具過濾器（僅暴露指定工具）
    startup_timeout: float = 30.0  # 啟動逾時（秒）


class BudgetConfig(BaseModel):
    """預算配置 — 三級預算控制（任務/會話/月度）。

    職責說明：
        定義 LLM API 使用的預算限制和降級策略。
        當接近或超過預算時，系統會自動降級到較便宜的模型或停止執行。

    關聯關係：
        - 被 SystemConfig.budget 持有
        - 被 opc/llm/budget_guard.py 讀取並執行預算檢查
        - 被 opc/layer6_observability/cost_tracker.py 用於成本報告

    使用範例：
        budget:
          task_limit_usd: 2.0      # 單任務上限
          session_limit_usd: 10.0  # 單會話上限
          monthly_limit_usd: 100.0 # 月度上限
          warn_threshold: 0.8      # 80% 時預警
          degrade_threshold: 0.9   # 90% 時降級模型
          hard_stop: false         # 超限後降級而非停止
    """
    task_limit_usd: float = 0.0  # 單任務上限（美元，0=不限制）
    session_limit_usd: float = 0.0  # 單會話上限（美元，0=不限制）
    monthly_limit_usd: float = 0.0  # 月度上限（美元，0=不限制）
    warn_threshold: float = 0.8  # 預警閾值（0.0-1.0，預設 80%）
    degrade_threshold: float = 0.9  # 降級閾值（0.0-1.0，預設 90%）
    hard_stop: bool = False  # 超限後是否硬停止（False=降級到廉价模型繼續）

    def get_effective_limit(self, level: str) -> float:
        """取得指定層級的有效預算限制。

        Args:
            level: 預算層級（"task", "session", "monthly"）

        Returns:
            預算限制（美元），0 表示不限制。
        """
        limits = {
            "task": self.task_limit_usd,
            "session": self.session_limit_usd,
            "monthly": self.monthly_limit_usd,
        }
        return limits.get(level, 0.0)

    def should_warn(self, level: str, spent: float) -> bool:
        """檢查是否應該發出預警。"""
        limit = self.get_effective_limit(level)
        if limit <= 0:
            return False
        return spent >= limit * self.warn_threshold

    def should_degrade(self, level: str, spent: float) -> bool:
        """檢查是否應該降級模型。"""
        limit = self.get_effective_limit(level)
        if limit <= 0:
            return False
        return spent >= limit * self.degrade_threshold

    def is_exceeded(self, level: str, spent: float) -> bool:
        """檢查是否超過預算。"""
        limit = self.get_effective_limit(level)
        if limit <= 0:
            return False
        return spent >= limit


class SystemConfig(BaseModel):
    """系統配置 — OPC 系統的全域運行參數。

    職責說明：
        定義系統層級的全域設定，包括日誌等級、代理迭代上限、
        上下文壓縮閾值、MCP 伺服器、心跳、瀏覽器、原生運行時等。

    關聯關係：
        - 被 OPCConfig.system 持有
        - 被 opc/engine.py 在啟動時讀取
    """
    opc_home: str = ""  # OPC 主目錄路徑（空字串表示自動偵測）
    default_channel: str = "cli"  # 預設通訊頻道
    log_level: str = "INFO"  # 日誌等級
    max_agent_iterations: int = 50  # 代理最大迭代次數
    context_compression_threshold: float = 0.85  # 上下文壓縮觸發閾值
    escalation_timeout_seconds: int = 3600  # 升級逾時（秒）
    auto_approve_below_cost: float = 10.0  # 低於此成本自動核准
    require_confirmation: list[str] = Field(  # 需要使用者確認的操作关键词
        default_factory=lambda: ["deploy to production", "send external emails", "modify database schema"]
    )
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)  # MCP 伺服器列表
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)  # 心跳配置
    browser: BrowserConfig = Field(default_factory=BrowserConfig)  # 瀏覽器配置
    native_runtime: NativeRuntimeConfig = Field(default_factory=NativeRuntimeConfig)  # 原生運行時配置
    task_mode: TaskModeConfig = Field(  # 任務模式配置（相容舊版 "project_mode" 鍵名）
        default_factory=TaskModeConfig,
        validation_alias=AliasChoices("task_mode", "project_mode"),
        serialization_alias="task_mode",
    )
    budget: BudgetConfig = Field(default_factory=BudgetConfig)  # 預算配置


class AutonomyConfig(BaseModel):
    """自主性配置 — 控制 AI 代理的自主決策和審批行為。

    職責說明：
        定義代理的自主性邊界：哪些操作可自動執行、哪些需要人類審批、
        風險閾值、敏感關鍵詞偵測、安全命令白名單等。

    關聯關係：
        - 被 OPCConfig.autonomy 持有
        - 被 opc/layer4_tools/ 的審批引擎讀取
        - 被 opc/engine.py 在工具執行前檢查
    """
    enabled: bool = True  # 是否啟用自主性控制
    mode: str = "bounded"  # 自主性模式（bounded/full/supervised）
    approval_model: str = ""  # 審批模型（空字串表示使用預設）
    approval_confidence_threshold: float = 0.7  # 審批信心閾值
    learned_policy_threshold: float = 0.8  # 學習策略信心閾值
    max_auto_approve_risk: str = "medium"  # 自動核准的最大風險等級
    allow_native_tool_auto_approval: bool = True  # 允許原生工具自動核准
    allow_external_agent_auto_approval: bool = False  # 允許外部代理自動核准
    learn_from_feedback: bool = True  # 從使用者回饋中學習
    save_external_sessions: bool = True  # 儲存外部代理工作階段
    tool_first_use_approval: bool = True  # 工具首次使用需要審批
    tool_approval_exemptions: list[str] = Field(default_factory=lambda: [  # 審批豁免工具列表
        *COMPANY_APPROVAL_EXEMPT_TOOL_NAMES,
        "request_user_input",
        "todo_read",
        "todo_write",
    ])
    command_review_window: int = 20  # 命令審查視窗（最近 N 條命令）
    sensitive_keywords: list[str] = Field(default_factory=lambda: [  # 敏感關鍵詞（觸發審批）
        "token", "password", "secret", "api key", "credential", "private key",
        "payment", "invoice", "wire transfer", "database schema", "drop table",
        "truncate", "delete from", "rm -rf", "terraform destroy", "deploy to production",
        "send email", "external email", "publish", "post to", "webhook",
    ])
    safe_command_prefixes: list[str] = Field(default_factory=lambda: [  # 安全命令前綴白名單
        "ls", "pwd", "echo", "rg", "find", "git status", "git diff", "python -V",
        "python3 -V", "node -v", "npm -v", "curl", "wget", "yt-dlp", "aria2c", "ffmpeg",
        # 代理頻繁鏈式呼叫的唯讀命令；複合命令的每個段都必須匹配
        # 這些前綴之一，整個命令才被視為低風險。
        "cd", "cat", "head", "tail", "grep", "wc", "sort", "uniq", "cut", "tr",
        "stat", "file", "which", "date", "du", "df", "tree", "basename", "dirname",
        "realpath", "readlink", "uname", "nproc", "whoami", "hostname", "git log",
        "git show", "git rev-parse",
    ])
    permissions_v2: PermissionsV2Config = Field(default_factory=PermissionsV2Config)  # 權限預測器配置


class SkillHubConfig(BaseModel):
    """技能中心配置 — 遠端技能市場（SkillHub）的連接和使用設定。"""
    enabled: bool = False  # 是否啟用技能中心
    api_base: str = "https://www.skillhub.club/api/v1"  # API 基礎 URL
    api_key: str = ""  # API 金鑰
    api_key_env: str = "SKILLHUB_API_KEY"  # API 金鑰環境變數名
    search_limit: int = 5  # 搜尋結果數量限制
    method: str = "hybrid"  # 搜尋方法（hybrid/semantic/keyword）
    cache_remote_skills: bool = True  # 是否快取遠端技能
    promote_after_successes: int = 2  # 成功使用幾次後提升為本地技能


class CapabilityConfig(BaseModel):
    """能力配置 — 代理能力系統的頂層設定（本地優先 + 遠端技能）。"""
    enable_recovery: bool = True  # 是否啟用能力恢復
    local_first: bool = True  # 本地技能優先
    attach_remote_skill_summaries: bool = True  # 附加遠端技能摘要
    promote_remote_skills: bool = True  # 提升遠端技能為本地
    remote_skill_source: str = "skillhub"  # 遠端技能來源
    max_remote_skill_results: int = 5  # 遠端技能最大結果數
    tool_failure_threshold: int = 2  # 工具失敗閾值（觸發能力恢復）
    skillhub: SkillHubConfig = Field(default_factory=SkillHubConfig)  # 技能中心配置


# ---------------------------------------------------------------------------
# 頻道配置 — 各通訊頻道的連接和行為設定
# ---------------------------------------------------------------------------

class BaseChannelConfig(BaseModel):
    """頻道基礎配置 — 所有頻道的公共欄位（啟用狀態、允許來源）。"""
    enabled: bool = False  # 是否啟用此頻道
    allow_from: list[str] = Field(default_factory=list)  # 允許的使用者/群組 ID 列表


class TelegramChannelConfig(BaseChannelConfig):
    """Telegram 頻道配置 — Telegram Bot 的連接設定。"""
    token: str = ""  # Bot Token
    proxy: str | None = None  # 代理伺服器 URL（可選）
    reply_to_message: bool = False  # 是否以回覆模式發送訊息


class WhatsAppChannelConfig(BaseChannelConfig):
    """WhatsApp 頻道配置 — 透過 WebSocket 橋接連接 WhatsApp。"""
    bridge_url: str = "ws://localhost:3001"  # 橋接 WebSocket URL
    bridge_token: str = ""  # 橋接驗證 Token


class DiscordChannelConfig(BaseChannelConfig):
    """Discord 頻道配置 — Discord Bot 的連接設定。"""
    token: str = ""  # Bot Token
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"  # Gateway WebSocket URL
    intents: int = 37377  # Gateway Intents 位元遮罩
    group_policy: str = "mention"  # 群組訊息策略（mention/all/none）


class FeishuChannelConfig(BaseChannelConfig):
    """飛書頻道配置 — 飛書（Lark）機器人的連接設定。"""
    app_id: str = ""  # 應用 ID
    app_secret: str = ""  # 應用密鑰
    encrypt_key: str = ""  # 事件加密金鑰
    verification_token: str = ""  # 事件驗證 Token
    react_emoji: str = "THUMBSUP"  # 收到訊息時的反應表情


class MochatMentionConfig(BaseModel):
    """Mochat 提及配置 — 群組中是否需要 @提及才回應。"""
    require_in_groups: bool = False  # 群組中是否要求被提及


class MochatGroupRule(BaseModel):
    """Mochat 群組規則 — 單一群組的回應規則。"""
    require_mention: bool = False  # 此群組是否要求被提及


class MochatChannelConfig(BaseChannelConfig):
    """Mochat 頻道配置 — Mochat 即時通訊平台的連接設定。"""
    base_url: str = "https://mochat.io"  # Mochat 基礎 URL
    socket_url: str = ""  # Socket.IO URL（空字串表示使用 base_url）
    socket_path: str = "/socket.io"  # Socket.IO 路徑
    socket_disable_msgpack: bool = False  # 禁用 msgpack 編碼
    socket_reconnect_delay_ms: int = 1000  # 重連延遲（毫秒）
    socket_max_reconnect_delay_ms: int = 10000  # 最大重連延遲（毫秒）
    socket_connect_timeout_ms: int = 10000  # 連接逾時（毫秒）
    refresh_interval_ms: int = 30000  # 刷新間隔（毫秒）
    watch_timeout_ms: int = 25000  # 監聽逾時（毫秒）
    watch_limit: int = 100  # 監聽訊息數量限制
    retry_delay_ms: int = 500  # 重試延遲（毫秒）
    max_retry_attempts: int = 0  # 最大重試次數（0 表示無限）
    claw_token: str = ""  # Claw 驗證 Token
    agent_user_id: str = ""  # 代理使用者 ID
    sessions: list[str] = Field(default_factory=list)  # 監聽的工作階段列表
    panels: list[str] = Field(default_factory=list)  # 監聽的面板列表
    mention: MochatMentionConfig = Field(default_factory=MochatMentionConfig)  # 提及配置
    groups: dict[str, MochatGroupRule] = Field(default_factory=dict)  # 群組規則映射
    reply_delay_mode: str = "non-mention"  # 回覆延遲模式
    reply_delay_ms: int = 120000  # 回覆延遲（毫秒）


class DingTalkChannelConfig(BaseChannelConfig):
    """釘釘頻道配置 — 釘釘機器人的連接設定。"""
    client_id: str = ""  # 客戶端 ID
    client_secret: str = ""  # 客戶端密鑰


class EmailChannelConfig(BaseChannelConfig):
    """Email 頻道配置 — IMAP/SMTP 郵件頻道的連接設定。"""
    consent_granted: bool = False  # 使用者是否已授權郵件存取
    imap_host: str = ""  # IMAP 伺服器主機
    imap_port: int = 993  # IMAP 埠號
    imap_username: str = ""  # IMAP 使用者名稱
    imap_password: str = ""  # IMAP 密碼
    imap_mailbox: str = "INBOX"  # IMAP 信箱名稱
    imap_use_ssl: bool = True  # IMAP 是否使用 SSL
    smtp_host: str = ""  # SMTP 伺服器主機
    smtp_port: int = 587  # SMTP 埠號
    smtp_username: str = ""  # SMTP 使用者名稱
    smtp_password: str = ""  # SMTP 密碼
    smtp_use_tls: bool = True  # SMTP 是否使用 TLS
    smtp_use_ssl: bool = False  # SMTP 是否使用 SSL
    from_address: str = ""  # 寄件人地址
    auto_reply_enabled: bool = True  # 是否啟用自動回覆
    poll_interval_seconds: int = 30  # 輪詢間隔（秒）
    mark_seen: bool = True  # 處理後標記為已讀
    max_body_chars: int = 12000  # 郵件正文最大字元數
    subject_prefix: str = "Re: "  # 回覆主旨前綴


class SlackDMChannelConfig(BaseModel):
    """Slack 私訊配置 — Slack DM 的回應策略。"""
    enabled: bool = True  # 是否啟用私訊回應
    policy: str = "open"  # 私訊策略（open/allowlist/closed）
    allow_from: list[str] = Field(default_factory=list)  # 允許的使用者 ID 列表


class SlackChannelConfig(BaseChannelConfig):
    """Slack 頻道配置 — Slack Bot 的連接設定。"""
    mode: str = "socket"  # 連接模式（socket/webhook）
    webhook_path: str = "/slack/events"  # Webhook 路徑（僅 webhook 模式）
    bot_token: str = ""  # Bot Token（xoxb-...）
    app_token: str = ""  # App Token（xapp-...，僅 socket 模式）
    user_token_read_only: bool = True  # 使用者 Token 僅唯讀
    reply_in_thread: bool = True  # 是否在執行緒中回覆
    react_emoji: str = "eyes"  # 收到訊息時的反應表情
    group_policy: str = "mention"  # 群組訊息策略
    group_allow_from: list[str] = Field(default_factory=list)  # 允許的群組 ID 列表
    dm: SlackDMChannelConfig = Field(default_factory=SlackDMChannelConfig)  # 私訊配置


class QQChannelConfig(BaseChannelConfig):
    """QQ 頻道配置 — QQ 機器人的連接設定。"""
    app_id: str = ""  # 應用 ID
    secret: str = ""  # 應用密鑰


class MatrixChannelConfig(BaseChannelConfig):
    """Matrix 頻道配置 — Matrix 協議的連接設定。"""
    homeserver: str = "https://matrix.org"  # Homeserver URL
    access_token: str = ""  # 存取 Token
    user_id: str = ""  # Matrix 使用者 ID（@user:server）
    device_id: str = ""  # 裝置 ID
    e2ee_enabled: bool = True  # 是否啟用端對端加密
    sync_stop_grace_seconds: int = 2  # 同步停止寬限時間（秒）
    max_media_bytes: int = 20 * 1024 * 1024  # 媒體檔案大小上限（位元組）
    group_policy: str = "open"  # 群組訊息策略
    group_allow_from: list[str] = Field(default_factory=list)  # 允許的房間 ID 列表
    allow_room_mentions: bool = False  # 是否允許房間提及


class ChannelsConfig(BaseModel):
    """頻道總配置 — 聚合所有通訊頻道的設定。

    職責說明：
        持有所有支援的通訊頻道配置。每個頻道可獨立啟用/停用。
        被 OPCConfig.channels 持有，由 opc/channels/ 各適配器讀取。
    """
    send_progress: bool = True  # 是否發送進度更新
    send_tool_hints: bool = False  # 是否發送工具使用提示
    telegram: TelegramChannelConfig = Field(default_factory=TelegramChannelConfig)  # Telegram
    whatsapp: WhatsAppChannelConfig = Field(default_factory=WhatsAppChannelConfig)  # WhatsApp
    discord: DiscordChannelConfig = Field(default_factory=DiscordChannelConfig)  # Discord
    feishu: FeishuChannelConfig = Field(default_factory=FeishuChannelConfig)  # 飛書
    mochat: MochatChannelConfig = Field(default_factory=MochatChannelConfig)  # Mochat
    dingtalk: DingTalkChannelConfig = Field(default_factory=DingTalkChannelConfig)  # 釘釘
    email: EmailChannelConfig = Field(default_factory=EmailChannelConfig)  # Email
    slack: SlackChannelConfig = Field(default_factory=SlackChannelConfig)  # Slack
    qq: QQChannelConfig = Field(default_factory=QQChannelConfig)  # QQ
    matrix: MatrixChannelConfig = Field(default_factory=MatrixChannelConfig)  # Matrix


# ---------------------------------------------------------------------------
# 配置序列化/反序列化輔助函數
# ---------------------------------------------------------------------------

def _dump_config_item(item: Any) -> Any:
    """將單一配置項目序列化為 dict（內部輔助函數）。"""
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if isinstance(item, dict):
        return dict(item)
    return item


def _dump_config_list(items: Any) -> list[Any]:
    """將配置項目列表序列化為 dict 列表（內部輔助函數）。"""
    return [_dump_config_item(item) for item in list(items or [])]


def _runtime_policy_payload_from_org(org: OrgConfig) -> dict[str, Any]:
    """從 OrgConfig 提取運行時策略的序列化 payload（內部輔助函數）。"""
    return {str(key): _dump_config_item(value) for key, value in dict(org.runtime_policies or {}).items()}


def _materialized_roles_for_org_payload(config: Any, profile: str) -> list[RoleConfig]:
    """根據公司設定檔取得具體化的角色列表（內部輔助函數）。

    參數：
        config：OPCConfig 實例。
        profile (str)：公司設定檔名稱（"corporate" 或 "custom"）。

    返回值：
        list[RoleConfig] — 具體化後的角色列表（corporate 會合併內建角色）。
    """
    org = config.org
    if profile == "corporate":
        from opc.layer2_organization.company_runtime_profiles import get_builtin_roles

        return get_builtin_roles("corporate", configured_roles=list(org.roles or []))
    return list(org.roles or [])


def _infer_final_decider_role_id(roles: list[RoleConfig], profile: str, configured: Any) -> str | None:
    """推斷最終決策者角色 ID（內部輔助函數）。

    邏輯：
        1. 若已明確配置，直接返回。
        2. corporate 設定檔且有 "ceo" 角色，返回 "ceo"。
        3. 若只有一個頂層角色（reports_to 為 owner 或不存在），返回該角色。
        4. 否則返回 None。
    """
    configured_id = str(configured or "").strip()
    if configured_id:
        return configured_id
    role_ids = {str(role.id or "").strip() for role in roles if str(role.id or "").strip()}
    if profile == "corporate" and "ceo" in role_ids:
        return "ceo"
    top_level_role_ids = [
        str(role.id or "").strip()
        for role in roles
        if str(role.id or "").strip()
        and (
            str(role.reports_to or "owner").strip() == "owner"
            or str(role.reports_to or "").strip() not in role_ids
        )
    ]
    top_level_role_ids = sorted(dict.fromkeys(top_level_role_ids))
    if len(top_level_role_ids) == 1:
        return top_level_role_ids[0]
    return None


def _effective_runtime_policy_payload(config: Any, profile: str) -> dict[str, Any]:
    """取得指定設定檔的有效運行時策略 payload（內部輔助函數）。

    嘗試透過 OrgEngine 解析完整策略；失敗時降級為內建策略。
    """
    try:
        from opc.layer2_organization.org_engine import OrgEngine

        policy_config = config
        current_profile = str(getattr(config.org, "company_profile", "") or "").strip()
        if current_profile != profile and hasattr(config, "model_copy"):
            policy_config = config.model_copy(deep=True)
            policy_config.org.company_profile = profile
        effective = OrgEngine(policy_config).get_runtime_policy(profile)
        if hasattr(effective, "model_dump"):
            effective = effective.model_dump()
        return dict(effective or {})
    except Exception:
        try:
            from opc.layer2_organization.company_runtime_profiles import get_builtin_runtime_policies

            policy = get_builtin_runtime_policies().get(profile)
            return policy.model_dump() if policy else {}
        except Exception:
            return {}


def _runtime_policy_payload_for_org(config: Any, profile: str) -> dict[str, Any]:
    """為組織 payload 組裝完整的運行時策略映射（內部輔助函數）。"""
    policies = _runtime_policy_payload_from_org(config.org)
    policy_key = "corporate" if profile == "corporate" else "custom" if profile == "custom" else profile
    if policy_key:
        effective = _effective_runtime_policy_payload(config, policy_key)
        if effective:
            policies[policy_key] = effective
    return policies


def _should_keep_org_employee_payload(employee: Any) -> bool:
    """判斷員工是否應保留在組織配置中（內部輔助函數）。

    系統預設員工和招聘回退員工不寫入組織配置檔案，
    僅保留使用者明確儲存或持久化的員工。
    """
    if isinstance(employee, EmployeeConfig):
        metadata = dict(employee.metadata or {})
    elif isinstance(employee, dict):
        metadata = dict(employee.get("metadata") or {})
    else:
        return True
    if metadata.get("persist_to_org") or metadata.get("user_saved_default"):
        return True
    if metadata.get("employee_origin") in {"system_default", "recruitment_fallback"}:
        return False
    if metadata.get("auto_created_for_role") and (
        metadata.get("is_default_employee") or metadata.get("is_fallback_employee")
    ):
        return False
    return True


def _should_persist_org_employee(employee: EmployeeConfig) -> bool:
    """判斷 EmployeeConfig 是否應持久化到組織配置（內部輔助函數）。"""
    return _should_keep_org_employee_payload(employee)


def _organization_identity_from_org(org: OrgConfig) -> tuple[str, str]:
    """從 OrgConfig 解析組織 ID 和顯示名稱（內部輔助函數）。

    返回值：
        tuple[str, str] — (organization_id, display_name)。
    """
    display_name = str(org.organization_name or org.company_name or "My One-Person Company").strip()
    profile = str(org.company_profile or "").strip().lower()
    raw_id = str(org.organization_id or "").strip()
    if raw_id and _COMPANY_ORG_ID_RE.match(raw_id):
        org_id = raw_id
    elif profile == "custom":
        org_id = slugify_organization_name(display_name)
    else:
        org_id = DEFAULT_ORGANIZATION_ID
    if profile == "custom" and org_id == DEFAULT_ORGANIZATION_ID and display_name:
        org_id = slugify_organization_name(display_name)
    return validate_organization_id(org_id), display_name


def build_company_org_payload_from_config(
    config: Any,
    *,
    organization_id: str | None = None,
    organization_name: str | None = None,
    force_profile: str | None = None,
) -> dict[str, Any]:
    """從 OPCConfig 建立公司組織配置的完整 payload（用於持久化）。

    參數：
        config：OPCConfig 實例。
        organization_id (str | None)：覆蓋組織 ID。
        organization_name (str | None)：覆蓋組織名稱。
        force_profile (str | None)：強制公司設定檔。

    返回值：
        dict — 可直接寫入 YAML 的組織配置 payload。

    被誰引用：
        - OPCConfig.save()：儲存公司模式配置
        - _default_company_org_payload()：產生預設 corporate payload
    """
    org = config.org
    resolved_id, resolved_name = _organization_identity_from_org(org)
    org_id = validate_organization_id(organization_id or resolved_id)
    org_name = str(organization_name or resolved_name or org.company_name or org_id).strip()
    profile = force_profile if force_profile is not None else org.company_profile
    profile = str(profile or "").strip() or ("custom" if org_id != DEFAULT_ORGANIZATION_ID else "corporate")
    roles = _materialized_roles_for_org_payload(config, profile)
    final_decider_role_id = _infer_final_decider_role_id(roles, profile, org.final_decider_role_id)
    return {
        "schema_version": COMPANY_ORG_SCHEMA_VERSION,
        "kind": COMPANY_ORG_KIND,
        "organization_id": org_id,
        "organization_name": org_name,
        "company": {
            "name": org.company_name or org_name,
            "topology": org.topology,
            "company_profile": profile,
            "execution_model": org.execution_model,
            "final_decider_role_id": final_decider_role_id,
            "company_profiles": list(org.company_profiles),
        },
        "roles": [role.model_dump() for role in roles],
        # 員工儲存在 .opc/company_state/<org>/employees/*.yaml。
        # 組織配置專注於組織結構；load 僅將此欄位視為舊版輸入。
        "employees": [],
        "escalation_rules": [rule.model_dump() for rule in org.escalation_rules],
        "runtime_policies": _runtime_policy_payload_for_org(config, profile),
        # 人才模板儲存在 .opc/prompts/talent/*.md 和內建預設中。
        # 組織配置專注於組織結構，不含人才索引。
        "talent_templates": [],
        "teams": [team.model_dump() for team in org.teams],
        "team_runtime": org.team_runtime.model_dump(),
        "installed_packages": _dump_config_list(org.installed_packages),
        "role_serial_queue_enabled": bool(org.role_serial_queue_enabled),
        "metadata": {
            "source": "opc_config",
            "organization_config_file": company_org_relative_path(org_id),
        },
    }


def _default_company_org_payload() -> dict[str, Any]:
    """產生預設的 corporate 組織配置 payload（內部輔助函數）。"""
    cfg = OPCConfig()
    return build_company_org_payload_from_config(
        cfg,
        organization_id=DEFAULT_ORGANIZATION_ID,
        organization_name=cfg.org.company_name,
        force_profile="corporate",
    )


def _validate_company_org_payload(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """驗證組織配置 payload 的 schema 版本和 kind（內部輔助函數）。

    參數：
        path (Path)：配置檔案路徑（用於錯誤訊息）。
        data (dict)：原始 YAML 資料。

    返回值：
        dict — 驗證後的資料（schema_version 已被正規化）。

    異常：
        ValueError — schema 版本過高或 kind 不支援。
    """
    schema_version = int(data.get("schema_version", 1) or 1)
    if schema_version > COMPANY_ORG_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version {schema_version} is not supported by this version of OpenOPC"
        )
    kind = str(data.get("kind", "") or "").strip()
    if schema_version >= COMPANY_ORG_SCHEMA_VERSION and kind and kind != COMPANY_ORG_KIND:
        raise ValueError(f"Unsupported organization config kind in {path.name}: {kind}")
    data["schema_version"] = schema_version
    return data


def _company_org_payload_to_org_mapping(data: dict[str, Any], *, source_path: Path | None = None) -> dict[str, Any]:
    """將公司組織 payload 轉換為 OrgConfig 可接受的映射格式（內部輔助函數）。

    參數：
        data (dict)：公司組織配置 payload。
        source_path (Path | None)：來源檔案路徑。

    返回值：
        dict — 符合 OrgConfig 欄位結構的映射。
    """
    company = data.get("company") if isinstance(data.get("company"), dict) else {}
    raw_org_id = data.get("organization_id") or DEFAULT_ORGANIZATION_ID
    org_id = validate_organization_id(raw_org_id)
    org_name = str(data.get("organization_name") or company.get("name") or org_id).strip()
    company_name = str(company.get("name") or org_name or "My OPC").strip()
    org: dict[str, Any] = {
        "organization_id": org_id,
        "organization_name": org_name or company_name,
        "organization_config_file": str(source_path or company_org_relative_path(org_id)),
        "company_name": company_name,
        "topology": company.get("topology", ""),
        "company_profile": company.get("company_profile") or ("custom" if org_id != DEFAULT_ORGANIZATION_ID else "corporate"),
        "execution_model": company.get("execution_model") or "actor_runtime",
        "final_decider_role_id": company.get("final_decider_role_id"),
        "company_profiles": company.get("company_profiles") or ["corporate", "custom"],
    }
    for key in _ORG_STRUCTURE_KEYS:
        if key in data:
            value = data.get(key) or []
            if key == "employees" and isinstance(value, list):
                value = [item for item in value if _should_keep_org_employee_payload(item)]
            org[key] = value
    for key in _ORG_RUNTIME_KEYS:
        if key == "role_serial_queue_enabled":
            if key in data:
                org[key] = bool(data.get(key))
            continue
        if key == "talent_templates":
            continue
        if key in data:
            org[key] = data.get(key) or ({} if key in {"runtime_policies", "team_runtime"} else [])
    return org


# ---------------------------------------------------------------------------
# 舊版配置遷移 — 從 legacy 檔案格式遷移到統一 company_orgs/ 儲存
# ---------------------------------------------------------------------------

def _legacy_corporate_config_candidates(config_dir: Path) -> list[Path]:
    """列出舊版 corporate 配置檔案的候選路徑（內部輔助函數）。"""
    candidates: list[Path] = []
    project_root_candidate = _find_project_root() / "config" / "company_corporate_config.yaml"
    local_candidate = Path(config_dir) / "company_corporate_config.yaml"
    candidate_order = [local_candidate]
    try:
        if Path(config_dir).resolve() == (get_opc_home() / "config").resolve():
            candidate_order.insert(0, project_root_candidate)
    except Exception:
        pass
    for path in candidate_order:
        if path not in candidates:
            candidates.append(path)
    return candidates


def _read_legacy_corporate_config(config_dir: Path) -> dict[str, Any]:
    """讀取舊版 company_corporate_config.yaml（內部輔助函數）。"""
    for path in _legacy_corporate_config_candidates(config_dir):
        if not path.exists():
            continue
        data = _read_yaml_file(path)
        schema_ver = int(data.get("schema_version", 1) or 1)
        if schema_ver > 1:
            raise ValueError(
                f"company_corporate_config.yaml schema_version {schema_ver} is not supported by this version of OpenOPC"
            )
        return data
    return {}


def _read_legacy_org_runtime_config(config_dir: Path) -> dict[str, Any]:
    """讀取舊版 org_config.yaml（內部輔助函數）。"""
    path = Path(config_dir) / "org_config.yaml"
    if not path.exists():
        return {}
    return _read_yaml_file(path)


def _legacy_company_org_payload(config_dir: Path) -> dict[str, Any]:
    """從舊版配置檔案組裝公司組織 payload（內部輔助函數）。

    合併 company_corporate_config.yaml 和 org_config.yaml 的內容，
    若兩者都不存在則返回預設 corporate payload。
    """
    corporate_data = _read_legacy_corporate_config(config_dir)
    org_runtime_data = _read_legacy_org_runtime_config(config_dir)
    if not corporate_data and not org_runtime_data:
        return _default_company_org_payload()

    company = dict(corporate_data.get("company", {}) or org_runtime_data.get("company", {}) or {})
    company_name = str(company.get("name") or "My One-Person Company").strip()
    profile = str(company.get("company_profile") or "").strip() or "corporate"
    org_id = (
        slugify_organization_name(company_name)
        if profile == "custom"
        else DEFAULT_ORGANIZATION_ID
    )
    payload: dict[str, Any] = {
        "schema_version": COMPANY_ORG_SCHEMA_VERSION,
        "kind": COMPANY_ORG_KIND,
        "organization_id": org_id,
        "organization_name": company_name,
        "company": {
            "name": company_name,
            "topology": company.get("topology", ""),
            "company_profile": profile,
            "execution_model": company.get("execution_model") or "actor_runtime",
            "final_decider_role_id": company.get("final_decider_role_id"),
            "company_profiles": company.get("company_profiles") or ["corporate", "custom"],
        },
        "roles": corporate_data.get("roles") or org_runtime_data.get("roles") or [],
        "employees": corporate_data.get("employees") or org_runtime_data.get("employees") or [],
        "escalation_rules": corporate_data.get("escalation_rules") or org_runtime_data.get("escalation_rules") or [],
        "runtime_policies": org_runtime_data.get("runtime_policies") or {},
        "talent_templates": [],
        "teams": org_runtime_data.get("teams") or [],
        "team_runtime": org_runtime_data.get("team_runtime") or {},
        "installed_packages": org_runtime_data.get("installed_packages") or [],
        "role_serial_queue_enabled": bool(org_runtime_data.get("role_serial_queue_enabled", True)),
        "metadata": {"source": "legacy_migration"},
    }
    return payload


def _normalize_corporate_company_org_payload(data: dict[str, Any]) -> dict[str, Any]:
    """將 payload 正規化為 corporate 範圍的公司組織配置（內部輔助函數）。

    公司模式是內建的 corporate 運行時。使用者自定義的組織儲存在
    ``company_orgs/`` 下，不應因為舊版 ``company_index.yaml`` 指向它們
    就成為處理序範圍的公司架構。
    """
    payload = dict(data or {})
    company = dict(payload.get("company", {}) or {})
    org_name = str(
        payload.get("organization_name")
        or company.get("name")
        or "My One-Person Company"
    ).strip()
    payload["organization_id"] = DEFAULT_ORGANIZATION_ID
    payload["organization_name"] = org_name
    company["name"] = str(company.get("name") or org_name).strip() or org_name
    company["company_profile"] = "corporate"
    company.setdefault("topology", "")
    company.setdefault("execution_model", "actor_runtime")
    company.setdefault("final_decider_role_id", None)
    company.setdefault("company_profiles", ["corporate", "custom"])
    payload["company"] = company
    payload.setdefault("roles", [])
    payload.setdefault("employees", [])
    payload.setdefault("escalation_rules", [])
    payload.setdefault("runtime_policies", {})
    payload["talent_templates"] = []
    payload.setdefault("teams", [])
    payload.setdefault("team_runtime", {})
    payload.setdefault("installed_packages", [])
    payload.setdefault("role_serial_queue_enabled", True)
    payload.setdefault("metadata", {})
    return payload


def _company_payload_profile(data: dict[str, Any]) -> str:
    """從 payload 提取 company_profile 值（內部輔助函數）。"""
    company = data.get("company") if isinstance(data.get("company"), dict) else {}
    return str(company.get("company_profile") or "").strip().lower()


def _company_payload_with_org_storage_path(data: dict[str, Any], org_id: str) -> dict[str, Any]:
    """為 payload 附加組織儲存路徑 metadata（內部輔助函數）。"""
    payload = dict(data or {})
    payload["organization_id"] = validate_organization_id(org_id)
    payload.setdefault("schema_version", COMPANY_ORG_SCHEMA_VERSION)
    payload.setdefault("kind", COMPANY_ORG_KIND)
    payload["metadata"] = {
        **dict(payload.get("metadata", {}) or {}),
        "organization_config_file": company_org_relative_path(org_id),
    }
    return payload


def _company_org_payload_needs_externalization(data: dict[str, Any]) -> bool:
    """判斷 payload 是否包含需要外部化的員工/人才模板（內部輔助函數）。"""
    return bool(data.get("employees") or data.get("talent_templates"))


def _repoint_org_index_if_active_corporate(config_dir: Path, archive_id: str) -> None:
    """若 org_index 指向 corporate，重新指向歸檔 ID（內部輔助函數）。"""
    path = Path(config_dir) / "org_index.yaml"
    if not path.exists():
        return
    try:
        data = _read_yaml_file(path)
    except Exception:
        return
    active_id = str(data.get("active_organization_id") or "").strip()
    if active_id != DEFAULT_ORGANIZATION_ID:
        return
    _write_yaml_preserving_unicode(
        path,
        {
            "schema_version": int(data.get("schema_version", 1) or 1),
            "active_organization_id": archive_id,
        },
    )


def _archive_conflicting_corporate_org_config(config_dir: Path, target_path: Path) -> str | None:
    """在寫入 corporate 配置前保留衝突的自定義 ``org_corporate`` 檔案（內部輔助函數）。

    舊版 org-mode 允許使用者自定義架構使用 ``corporate`` ID。
    統一 ``company_orgs/`` 儲存後，該檔案名稱保留給內建 corporate 設定檔。
    若發現舊版自定義檔案，將其歸檔到新 ID 而非覆寫。

    返回值：
        str | None — 歸檔後的組織 ID，若無衝突則返回 None。
    """

    if not target_path.exists():
        return None
    try:
        existing = _validate_company_org_payload(target_path, _read_yaml_file(target_path))
    except Exception:
        return None
    if _company_payload_profile(existing) != "custom":
        return None

    company = dict(existing.get("company", {}) or {})
    org_name = str(
        existing.get("organization_name")
        or company.get("name")
        or "Corporate Custom"
    ).strip()
    archive_id = allocate_organization_id(
        config_dir,
        org_name,
        preferred_id=f"{DEFAULT_ORGANIZATION_ID}_custom",
    )
    archived = _company_payload_with_org_storage_path(existing, archive_id)
    archived["organization_name"] = org_name
    company["company_profile"] = "custom"
    archived["company"] = company
    archived.setdefault("metadata", {})["source"] = "archived_custom_corporate_conflict"
    write_company_org_payload(config_dir, archive_id, archived)
    _repoint_org_index_if_active_corporate(config_dir, archive_id)
    return archive_id


def _should_persist_company_migration(config_dir: Path) -> bool:
    """判斷是否應持久化遷移結果（僅當 config_dir 是 OPC 主目錄時）（內部輔助函數）。"""
    try:
        return Path(config_dir).resolve() == (get_opc_home() / "config").resolve()
    except Exception:
        return False


def _migrate_legacy_saved_orgs(config_dir: Path) -> None:
    """將舊版 config/orgs/*.yaml 遷移到統一 company_orgs/ 目錄（內部輔助函數）。"""
    legacy_dir = _find_project_root() / "config" / "orgs"
    if not legacy_dir.is_dir():
        return
    for legacy_path in sorted(legacy_dir.glob("*.yaml")):
        try:
            data = _read_yaml_file(legacy_path)
            company = dict(data.get("company", {}) or {})
            org_name = str(
                data.get("organization_name")
                or company.get("name")
                or legacy_path.stem
            ).strip()
            raw_org_id = str(data.get("organization_id") or "").strip()
            org_id = raw_org_id if _COMPANY_ORG_ID_RE.match(raw_org_id) else slugify_organization_name(legacy_path.stem)
            target_path = company_org_path(config_dir, org_id)
            if target_path.exists():
                continue
            company.setdefault("name", org_name)
            company.setdefault("company_profile", "custom")
            data.update({
                "schema_version": COMPANY_ORG_SCHEMA_VERSION,
                "kind": COMPANY_ORG_KIND,
                "organization_id": org_id,
                "organization_name": org_name,
                "company": company,
            })
            data.setdefault("roles", [])
            data.setdefault("employees", [])
            data.setdefault("escalation_rules", [])
            data.setdefault("runtime_policies", {})
            data["talent_templates"] = []
            data.setdefault("teams", [])
            data.setdefault("team_runtime", {})
            data.setdefault("installed_packages", [])
            data.setdefault("role_serial_queue_enabled", True)
            data.setdefault("metadata", {})["source"] = "legacy_saved_org_migration"
            write_company_org_payload(config_dir, org_id, data)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# 公司組織配置載入 — 公開 API
# ---------------------------------------------------------------------------

def load_company_org_payload(
    config_dir: Path,
    organization_id: Any = DEFAULT_ORGANIZATION_ID,
) -> tuple[dict[str, Any], Path]:
    """載入指定組織 ID 的公司模式組織配置 payload。

    ``OPCConfig.load`` 刻意以 ``corporate`` 呼叫此函數，
    而非查詢 ``company_index.yaml``。活動組織索引由 org mode 擁有。

    參數：
        config_dir (Path)：配置目錄路徑。
        organization_id：目標組織 ID（預設 "corporate"）。

    返回值：
        tuple[dict, Path] — (組織配置 payload, 配置檔案路徑)。

    被誰引用：
        - OPCConfig.load()：載入 corporate 組織配置
        - opc/cli/app.py：CLI 命令中讀取組織配置
    """
    config_dir = Path(config_dir)
    org_id = validate_organization_id(organization_id or DEFAULT_ORGANIZATION_ID)
    if _should_persist_company_migration(config_dir):
        _migrate_legacy_saved_orgs(config_dir)
    path = company_org_path(config_dir, org_id)
    if path.exists():
        payload = _validate_company_org_payload(path, _read_yaml_file(path))
        if org_id == DEFAULT_ORGANIZATION_ID:
            if _company_payload_profile(payload) == "custom":
                if _should_persist_company_migration(config_dir):
                    _archive_conflicting_corporate_org_config(config_dir, path)
                    payload = _normalize_corporate_company_org_payload(_legacy_company_org_payload(config_dir))
                    write_company_org_payload(config_dir, org_id, _company_payload_with_org_storage_path(payload, org_id))
                    return payload, path
                payload = _legacy_company_org_payload(config_dir)
            payload = _normalize_corporate_company_org_payload(payload)
        if _should_persist_company_migration(config_dir) and _company_org_payload_needs_externalization(payload):
            write_company_org_payload(config_dir, org_id, payload)
            payload = _validate_company_org_payload(path, _read_yaml_file(path))
        return payload, path
    if org_id != DEFAULT_ORGANIZATION_ID:
        raise FileNotFoundError(f"Company organization config does not exist: {path}")

    payload = _normalize_corporate_company_org_payload(_legacy_company_org_payload(config_dir))
    if _should_persist_company_migration(config_dir):
        write_company_org_payload(config_dir, org_id, payload)
        payload = _read_yaml_file(path)
    return _validate_company_org_payload(path, payload), path


def load_active_company_org_payload(config_dir: Path) -> tuple[dict[str, Any], Path]:
    """載入活動組織的公司模式配置 payload（根據 company_index.yaml）。

    參數：
        config_dir (Path)：配置目錄路徑。

    返回值：
        tuple[dict, Path] — (組織配置 payload, 配置檔案路徑)。

    被誰引用：
        - opc/cli/app.py：org mode 相關命令
    """
    config_dir = Path(config_dir)
    if _should_persist_company_migration(config_dir):
        _migrate_legacy_saved_orgs(config_dir)
    active_id = read_company_index(config_dir)
    if active_id:
        path = company_org_path(config_dir, active_id)
        if not path.exists():
            raise FileNotFoundError(f"Active organization config does not exist: {path}")
        payload = _validate_company_org_payload(path, _read_yaml_file(path))
        if _should_persist_company_migration(config_dir) and _company_org_payload_needs_externalization(payload):
            write_company_org_payload(config_dir, active_id, payload)
            payload = _validate_company_org_payload(path, _read_yaml_file(path))
        return payload, path

    payload = _legacy_company_org_payload(config_dir)
    org_id = validate_organization_id(payload.get("organization_id") or DEFAULT_ORGANIZATION_ID)
    path = company_org_path(config_dir, org_id)
    if _should_persist_company_migration(config_dir):
        write_company_org_payload(config_dir, org_id, payload)
        payload = _read_yaml_file(path)
    return _validate_company_org_payload(path, payload), path


# ---------------------------------------------------------------------------
# OPCConfig — 頂層配置模型（聚合所有子系統配置）
# ---------------------------------------------------------------------------

class OPCConfig(BaseModel):
    """OPC 頂層配置模型 — 聚合系統所有子系統的配置。

    職責說明：
        作為整個 OPC 系統的配置根節點，持有 system、llm、agents、
        org、channels、autonomy、capabilities 七大配置段。
        提供 load() 和 save() 類方法/實例方法管理配置的生命週期。

    關聯關係：
        - 被 opc/engine.py 在啟動時透過 OPCConfig.load() 建立
        - 被 opc/cli/app.py 在 CLI 命令中讀寫
        - 被所有 layer 模組讀取各自相關的配置段

    使用範例：
        config = OPCConfig.load()
        print(config.llm.default_model)
        config.save()
    """
    system: SystemConfig = Field(default_factory=SystemConfig)  # 系統配置
    llm: LLMConfig = Field(default_factory=LLMConfig)  # LLM 配置
    agents: AgentsConfig = Field(default_factory=AgentsConfig)  # 代理配置
    org: OrgConfig = Field(default_factory=OrgConfig)  # 組織配置
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)  # 頻道配置
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)  # 自主性配置
    capabilities: CapabilityConfig = Field(default_factory=CapabilityConfig)  # 能力配置

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "OPCConfig":
        """從配置目錄載入完整的 OPCConfig（類方法）。

        生命週期：
            1. 讀取 system_config.yaml、llm_config.yaml、agent_config.yaml、channel_config.yaml
            2. 載入 corporate 組織配置（load_company_org_payload）
            3. 合併所有配置段並驗證
            4. 載入員工註冊表

        參數：
            config_dir (Path | None)：配置目錄路徑。None 時使用 {opc_home}/config。

        返回值：
            OPCConfig — 完整的配置實例。

        被誰引用：
            - opc/engine.py：引擎啟動時
            - opc/cli/app.py：CLI 命令中
        """
        if config_dir is None:
            config_dir = get_opc_home() / "config"

        merged: dict[str, Any] = {}
        for name in ("system_config", "llm_config", "agent_config", "channel_config"):
            path = config_dir / f"{name}.yaml"
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if name == "agent_config":
                    data = _migrate_agent_config_approval_modes(path, data)
                    data = _migrate_agent_config_external_agent_defaults(path, data)
                merged.update(data)

        org_payload, org_payload_path = load_company_org_payload(config_dir, DEFAULT_ORGANIZATION_ID)

        mapping = {}
        if "system" in merged:
            mapping["system"] = merged["system"]
        if "llm" in merged:
            mapping["llm"] = merged["llm"]
        if "external_agents" in merged:
            agents_data = merged["external_agents"]
            mapping["agents"] = {
                "preferred_order": agents_data.get("preferred_order", []),
                "agents": {
                    k: v for k, v in agents_data.items() if k != "preferred_order"
                },
            }
        if "native_subagents" in merged:
            agent_mapping = mapping.get("agents", {"preferred_order": [], "agents": {}})
            if isinstance(agent_mapping, dict):
                agent_mapping["native_subagents"] = merged["native_subagents"]
                mapping["agents"] = agent_mapping
        mapping["org"] = _company_org_payload_to_org_mapping(org_payload, source_path=org_payload_path)
        if "channels" in merged:
            mapping["channels"] = merged["channels"]
        if "autonomy" in merged:
            mapping["autonomy"] = merged["autonomy"]
        if "capabilities" in merged:
            mapping["capabilities"] = merged["capabilities"]
        if "mcp_servers" in merged:
            system_data = mapping.get("system", {})
            if isinstance(system_data, dict):
                system_data["mcp_servers"] = merged["mcp_servers"]
                mapping["system"] = system_data
        agent_mapping = mapping.get("agents")
        if isinstance(agent_mapping, dict) and "native_subagents" not in agent_mapping:
            agent_mapping["native_subagents"] = {
                key: value.model_dump()
                for key, value in _default_native_subagents().items()
            }
            mapping["agents"] = agent_mapping

        config = cls.model_validate(mapping)
        config.org.talent_templates = []
        try:
            from opc.core.employee_registry import load_company_employees

            organization_id, _ = _organization_identity_from_org(config.org)
            config.org.employees = load_company_employees(
                Path(config_dir).parent,
                organization_id,
                list(config.org.employees),
            )
        except Exception:
            pass
        return config

    @classmethod
    def from_quickstart(
        cls,
        intent: str,
        overrides: dict[str, Any] | None = None,
        api_key: str | None = None,
        api_key_env: str | None = None,
    ) -> "OPCConfig":
        """從自然語言意圖建立臨時配置（零配置啟動）。

        不需要 `opc init` 或磁碟上的配置檔，直接使用預設值 + 推斷覆蓋。

        參數：
            intent: 使用者輸入的自然語言意圖描述。
            overrides: 額外配置覆蓋（優先級最高）。
            api_key: 直接提供的 API key。
            api_key_env: API key 環境變數名稱。

        返回值：
            OPCConfig — 記憶體中的臨時配置實例。

        使用範例：
            config = OPCConfig.from_quickstart("幫我寫一個 Python 爬蟲")
            engine = OPCEngine(config=config)
        """
        from opc.core.quickstart import QuickStartEngine

        engine = QuickStartEngine()
        result = engine.infer_config(intent)
        inferred = engine.build_ephemeral_config(
            result,
            api_key=api_key,
            api_key_env=api_key_env,
        )

        # 合併覆蓋配置
        if overrides:
            for key, value in overrides.items():
                if isinstance(value, dict) and key in inferred and isinstance(inferred[key], dict):
                    inferred[key].update(value)
                else:
                    inferred[key] = value

        # 使用 model_validate 建立配置（使用預設值填充缺失欄位）
        config = cls.model_validate(inferred)
        return config

    def save(self, config_dir: Path | None = None) -> None:
        """將當前配置持久化到配置目錄（實例方法）。

        生命週期：
            1. 寫入 system_config.yaml（含 autonomy、capabilities）
            2. 寫入 llm_config.yaml
            3. 寫入員工註冊表
            4. 根據 company_profile 寫入組織配置（custom → org_config / corporate → company_org）
            5. 寫入 agent_config.yaml
            6. 寫入 channel_config.yaml

        參數：
            config_dir (Path | None)：配置目錄路徑。None 時使用 {opc_home}/config。

        被誰引用：
            - opc/cli/app.py：CLI 配置修改命令
        """
        if config_dir is None:
            config_dir = get_opc_home() / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        system_path = config_dir / "system_config.yaml"
        _atomic_write_yaml(system_path, {
            "system": self.system.model_dump(),
            "autonomy": self.autonomy.model_dump(),
            "capabilities": self.capabilities.model_dump(),
        })

        llm_path = config_dir / "llm_config.yaml"
        _atomic_write_yaml(llm_path, {"llm": self.llm.model_dump()})

        organization_id, organization_name = _organization_identity_from_org(self.org)
        from opc.core.employee_registry import write_employee_registry

        self.org.employees, _ = write_employee_registry(
            Path(config_dir).parent,
            organization_id,
            list(self.org.employees),
        )
        profile = str(self.org.company_profile or "").strip().lower()
        if profile == "custom":
            from opc.core.org_config import (
                build_org_config_payload_from_config,
                org_config_path,
                write_org_config_payload,
                write_org_index,
            )

            payload = build_org_config_payload_from_config(
                self,
                organization_id=organization_id,
                organization_name=organization_name,
            )
            custom_path = org_config_path(config_dir, organization_id)
            if not self.org.roles and custom_path.exists():
                try:
                    with open(custom_path, encoding="utf-8") as f:
                        existing = yaml.safe_load(f) or {}
                    existing_roles = existing.get("roles") or []
                    if existing_roles:
                        import logging, os as _os
                        logging.getLogger(__name__).error(
                            "OPCConfig.save(): REFUSED to wipe %d existing roles with "
                            "empty list in custom mode. pid=%d, path=%s. "
                            "If this save was intentional, use reset_architecture.",
                            len(existing_roles), _os.getpid(), custom_path,
                        )
                        return
                except Exception:
                    pass
            write_org_config_payload(config_dir, organization_id, payload)
            write_org_index(config_dir, organization_id)
        else:
            corporate_data = build_company_org_payload_from_config(
                self,
                organization_id=organization_id,
                organization_name=organization_name,
            )
            if organization_id == DEFAULT_ORGANIZATION_ID:
                corporate_path = company_org_path(config_dir, organization_id)
                _archive_conflicting_corporate_org_config(config_dir, corporate_path)
            write_company_org_payload(config_dir, organization_id, corporate_data)

        agent_path = config_dir / "agent_config.yaml"
        agent_data = {
            "external_agents": {
                "preferred_order": self.agents.preferred_order,
                **{k: v.model_dump() for k, v in self.agents.agents.items()},
            },
            "native_subagents": {
                key: value.model_dump()
                for key, value in (self.agents.native_subagents or _default_native_subagents()).items()
            },
        }
        _atomic_write_yaml(agent_path, agent_data)

        channel_path = config_dir / "channel_config.yaml"
        _atomic_write_yaml(channel_path, {"channels": self.channels.model_dump()})


AgentsConfig.model_rebuild()
