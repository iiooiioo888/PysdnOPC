"""使用者自訂組織架構的專用儲存輔助模組。

職責說明：
    管理使用者自訂的組織架構設定檔（org architecture config）的完整生命週期：
    - 索引管理：追蹤當前活動的組織架構（org_index.yaml）
    - 設定檔 CRUD：讀取、寫入、列出、驗證組織設定檔
    - ID 分配：為新組織產生唯一的 slug 識別碼
    - 設定轉換：在 OPCConfig 和 YAML payload 之間轉換
    - 執行前驗證：確保自訂組織有明確的角色定義

關聯關係：
    - 被 opc/cli/app.py 的 org 相關命令調用（org save/load/list/activate）
    - 被 opc/layer2_organization/company_mode.py 在啟動公司模式時調用
    - 依賴 opc/core/config.py 中的基礎設定工具和常量
    - 設定檔儲存於 {opc_home}/.opc/config/company_orgs/ 目錄

使用範例：
    from opc.core.org_config import load_org_config_payload, write_org_config_payload
    payload, path = load_org_config_payload(config_dir, "my-org")
    write_org_config_payload(config_dir, "my-org", updated_payload)
"""

from __future__ import annotations  # 啟用延遲型別註解評估

from pathlib import Path  # 標準庫：跨平台路徑操作
from typing import Any  # 標準庫：payload 字典的值型別註解

import yaml  # 第三方庫 PyYAML：YAML 格式的序列化/反序列化

# 從核心設定模組匯入組織相關的常量和工具函數
from opc.core.config import (
    COMPANY_ORG_KIND,  # 組織設定檔的 kind 標記值（"company_org"）
    COMPANY_ORG_SCHEMA_VERSION,  # 當前支援的 schema 版本號
    DEFAULT_ORGANIZATION_ID,  # 內建公司設定的預設組織 ID（保留用）
    OPCConfig,  # OPC 核心設定 Pydantic 模型
    _atomic_write_text,  # 原子性寫入文字檔（防止寫入中斷導致損壞）
    _company_org_payload_to_org_mapping,  # 將公司 payload 轉為 org 設定映射
    _read_yaml_file,  # 安全讀取 YAML 檔案（含錯誤處理）
    build_company_org_payload_from_config,  # 從 OPCConfig 建構公司組織 payload
    slugify_organization_name,  # 將組織名稱轉為 URL 安全的 slug ID
    validate_organization_id,  # 驗證組織 ID 格式合法性
)

# 組織索引檔案名稱：記錄當前活動的組織架構 ID
ORG_INDEX_FILENAME = "org_index.yaml"

# 組織設定檔目錄名稱：所有自訂組織的 YAML 設定檔存放於此
ORG_CONFIGS_DIRNAME = "company_orgs"

# 組織設定檔的 kind 標記（繼承自核心設定）
ORG_CONFIG_KIND = COMPANY_ORG_KIND

# 組織設定檔的 schema 版本（繼承自核心設定）
ORG_CONFIG_SCHEMA_VERSION = COMPANY_ORG_SCHEMA_VERSION

# 保留的組織 ID 集合：這些 ID 為內建公司設定專用，不允許自訂組織使用
RESERVED_ORG_CONFIG_IDS = frozenset({DEFAULT_ORGANIZATION_ID})


class RunnableOrgConfigError(ValueError):
    """當已儲存的自訂組織無法安全啟動或執行時拋出的異常。

    使用場景：
        自訂組織必須有明確的角色定義才能執行。若缺少角色，
        系統拒絕啟動以避免靜默回退到內建角色（可能不符合使用者預期）。
    """


def org_index_path(config_dir: Path) -> Path:
    """取得組織索引檔案的完整路徑。

    參數：
        config_dir (Path)：設定目錄路徑（通常為 .opc/config/）。

    返回值：
        Path — org_index.yaml 的完整路徑。
    """
    return Path(config_dir) / ORG_INDEX_FILENAME


def org_configs_dir(config_dir: Path) -> Path:
    """取得組織設定檔目錄的完整路徑。

    參數：
        config_dir (Path)：設定目錄路徑。

    返回值：
        Path — company_orgs/ 目錄的完整路徑。
    """
    return Path(config_dir) / ORG_CONFIGS_DIRNAME


def is_reserved_org_config_id(value: Any) -> bool:
    """判斷給定的 ID 是否為保留的內建組織 ID。

    參數：
        value (Any)：待檢查的 ID 值。

    返回值：
        bool — True 表示為保留 ID（不可用於自訂組織）。
    """
    try:
        org_id = validate_organization_id(value)
    except ValueError:
        return False
    return org_id in RESERVED_ORG_CONFIG_IDS


def validate_saved_org_id(value: Any) -> str:
    """驗證自訂組織 ID（排除保留 ID）。

    功能：
        先驗證 ID 格式合法性，再確認不是保留的內建 ID。

    參數：
        value (Any)：待驗證的組織 ID。

    返回值：
        str — 正規化後的合法組織 ID。

    異常：
        ValueError：ID 格式無效或為保留 ID 時拋出。
    """
    org_id = validate_organization_id(value)
    if org_id in RESERVED_ORG_CONFIG_IDS:
        raise ValueError(f"Reserved organization_id for built-in company profile: {org_id!r}")
    return org_id


def org_config_filename(organization_id: Any) -> str:
    """根據組織 ID 產生設定檔檔案名稱。

    參數：
        organization_id (Any)：組織 ID（會經過驗證）。

    返回值：
        str — 格式為 "org_{id}_config.yaml" 的檔案名稱。
    """
    return f"org_{validate_saved_org_id(organization_id)}_config.yaml"


def organization_id_from_org_config_filename(path: Path) -> str | None:
    """從設定檔檔案名稱中反解析組織 ID。

    功能：
        解析 "org_{id}_config.yaml" 格式的檔名，提取並驗證組織 ID。

    參數：
        path (Path)：設定檔路徑。

    返回值：
        str | None — 合法的組織 ID，或 None（檔名格式不符或 ID 無效）。
    """
    name = Path(path).name
    prefix = "org_"
    suffix = "_config.yaml"
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    candidate = name[len(prefix):-len(suffix)]
    try:
        return validate_saved_org_id(candidate)
    except ValueError:
        return None


def org_config_path(config_dir: Path, organization_id: Any) -> Path:
    """取得指定組織的設定檔完整路徑。

    參數：
        config_dir (Path)：設定目錄路徑。
        organization_id (Any)：組織 ID。

    返回值：
        Path — 設定檔的完整路徑。
    """
    return org_configs_dir(config_dir) / org_config_filename(organization_id)


def org_config_relative_path(organization_id: Any) -> str:
    """取得組織設定檔的相對路徑字串（用於 metadata 記錄）。

    參數：
        organization_id (Any)：組織 ID。

    返回值：
        str — 格式為 "company_orgs/org_{id}_config.yaml" 的相對路徑。
    """
    return f"{ORG_CONFIGS_DIRNAME}/{org_config_filename(organization_id)}"


def read_org_index(config_dir: Path) -> str | None:
    """讀取當前活動的自訂組織 ID。

    功能：
        從 org_index.yaml 中讀取 active_organization_id。
        若為保留 ID（內建設定）或檔案不存在則返回 None。

    參數：
        config_dir (Path)：設定目錄路徑。

    返回值：
        str | None — 活動的自訂組織 ID，或 None（無活動自訂組織）。

    被誰引用：
        - opc/cli/app.py：org status 命令顯示當前組織
        - load_org_config_payload()：未指定 ID 時讀取預設
    """
    path = org_index_path(config_dir)
    if not path.exists():
        return None
    data = _read_yaml_file(path)
    active_id = data.get("active_organization_id")
    if not active_id:
        return None
    org_id = validate_organization_id(active_id)
    return None if org_id in RESERVED_ORG_CONFIG_IDS else org_id


def write_org_index(config_dir: Path, organization_id: Any) -> None:
    """寫入組織索引，設定活動的自訂組織。

    功能：
        原子性地寫入 org_index.yaml，記錄當前選擇的組織架構。

    參數：
        config_dir (Path)：設定目錄路徑。
        organization_id (Any)：要設為活動的組織 ID。

    被誰引用：
        - opc/cli/app.py：org activate 命令
    """
    org_id = validate_saved_org_id(organization_id)
    _atomic_write_text(
        org_index_path(config_dir),
        yaml.dump(
            {
                "schema_version": 1,
                "active_organization_id": org_id,
            },
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        ),
    )


def list_org_config_paths(config_dir: Path) -> list[Path]:
    """列出所有已儲存的自訂組織設定檔路徑。

    參數：
        config_dir (Path)：設定目錄路徑。

    返回值：
        list[Path] — 按字母排序的設定檔路徑列表。目錄不存在時返回空列表。

    被誰引用：
        - opc/cli/app.py：org list 命令
        - allocate_org_config_id()：檢查 ID 是否已使用
    """
    org_dir = org_configs_dir(config_dir)
    if not org_dir.is_dir():
        return []
    return sorted(path for path in org_dir.glob("org_*_config.yaml") if organization_id_from_org_config_filename(path))


def allocate_org_config_id(config_dir: Path, organization_name: Any, *, preferred_id: Any = "") -> str:
    """為新組織分配唯一的 ID。

    功能：
        優先使用 preferred_id（若合法且未佔用），否則從組織名稱
        產生 slug。若 ID 已存在則附加數字後綴（_2、_3...）。

    參數：
        config_dir (Path)：設定目錄路徑（用於檢查已有 ID）。
        organization_name (Any)：組織名稱（用於產生 slug）。
        preferred_id (Any)：首選 ID（可選）。

    返回值：
        str — 唯一的組織 ID（最長 64 字元）。

    被誰引用：
        - opc/cli/app.py：org save 命令建立新組織時
    """
    base = str(preferred_id or "").strip()
    try:
        candidate = validate_saved_org_id(base) if base else ""
    except ValueError:
        candidate = ""
    if not candidate:
        candidate = slugify_organization_name(organization_name)
    # 收集所有已使用的 ID（包含保留 ID）
    existing = {
        org_id
        for path in list_org_config_paths(config_dir)
        for org_id in [organization_id_from_org_config_filename(path)]
        if org_id
    }
    existing.update(RESERVED_ORG_CONFIG_IDS)
    # 首選 ID 可用則直接返回
    if candidate not in existing:
        return candidate
    # 附加數字後綴直到找到可用 ID
    suffix = 2
    while True:
        tail = f"_{suffix}"
        stem = candidate[: max(1, 64 - len(tail))].rstrip("_-") or "org"
        next_id = f"{stem}{tail}"
        if next_id not in existing:
            return next_id
        suffix += 1


def validate_org_config_payload(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """驗證組織設定檔 payload 的 schema 版本和 kind。

    功能：
        檢查 schema_version 不超過當前支援版本，
        且 kind（若指定）符合預期值。

    參數：
        path (Path)：設定檔路徑（用於錯誤訊息）。
        data (dict[str, Any])：解析後的 YAML 資料。

    返回值：
        dict[str, Any] — 驗證通過的資料（schema_version 已正規化為 int）。

    異常：
        ValueError：schema 版本過高或 kind 不支援時拋出。
    """
    schema_version = int(data.get("schema_version", 1) or 1)
    if schema_version > ORG_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version {schema_version} is not supported by this version of OpenOPC"
        )
    kind = str(data.get("kind", "") or "").strip()
    if schema_version >= ORG_CONFIG_SCHEMA_VERSION and kind and kind != ORG_CONFIG_KIND:
        raise ValueError(f"Unsupported org architecture kind in {path.name}: {kind}")
    data["schema_version"] = schema_version
    return data


def build_org_config_payload_from_config(
    config: OPCConfig,
    *,
    organization_id: str | None = None,
    organization_name: str | None = None,
) -> dict[str, Any]:
    """從 OPCConfig 建構自訂組織的 YAML payload。

    功能：
        將運行時的 OPCConfig 轉換為可持久化的組織設定 payload，
        強制 profile 為 "custom" 並附加來源 metadata。

    參數：
        config (OPCConfig)：當前的 OPC 設定物件。
        organization_id (str | None)：組織 ID（覆蓋設定中的值）。
        organization_name (str | None)：組織名稱（覆蓋設定中的值）。

    返回值：
        dict[str, Any] — 可直接寫入 YAML 的 payload 字典。

    被誰引用：
        - opc/cli/app.py：org save 命令
    """
    payload = build_company_org_payload_from_config(
        config,
        organization_id=organization_id,
        organization_name=organization_name,
        force_profile="custom",
    )
    org_id = validate_saved_org_id(payload.get("organization_id"))
    payload["metadata"] = {
        **dict(payload.get("metadata", {}) or {}),
        "source": "org_mode",
        "organization_config_file": org_config_relative_path(org_id),
    }
    return payload


def apply_org_config_payload_to_config(
    base_config: OPCConfig,
    data: dict[str, Any],
    *,
    source_path: Path | None = None,
) -> OPCConfig:
    """將組織設定 payload 套用到基礎 OPCConfig 上。

    功能：
        將 YAML payload 中的組織設定合併到基礎設定中，
        產生新的 OPCConfig 實例。強制 profile 為 "custom"，
        清空 talent_templates，並嘗試載入員工註冊表。

    參數：
        base_config (OPCConfig)：基礎設定（提供非 org 欄位的預設值）。
        data (dict[str, Any])：組織設定 payload（從 YAML 讀取）。
        source_path (Path | None)：設定檔來源路徑（用於載入關聯的員工檔案）。

    返回值：
        OPCConfig — 合併後的新設定物件。

    被誰引用：
        - opc/layer2_organization/company_mode.py：啟動自訂組織時
        - opc/cli/app.py：org run 命令
    """
    merged = base_config.model_dump()
    org_mapping = _company_org_payload_to_org_mapping(data, source_path=source_path)
    org_mapping["organization_id"] = validate_saved_org_id(org_mapping.get("organization_id"))
    org_mapping["company_profile"] = "custom"
    if org_mapping.get("organization_id"):
        org_mapping["organization_config_file"] = org_config_relative_path(org_mapping["organization_id"])
    merged["org"] = org_mapping
    config = OPCConfig.model_validate(merged)
    # 自訂組織不使用人才模板
    config.org.talent_templates = []
    # 嘗試從設定檔目錄載入員工註冊表
    if source_path is not None:
        try:
            from opc.core.employee_registry import load_company_employees

            config_dir = Path(source_path).parent.parent
            config.org.employees = load_company_employees(
                config_dir.parent,
                config.org.organization_id,
                list(config.org.employees),
            )
        except Exception:
            pass  # 員工載入失敗不阻塞組織啟動
    return config


def validate_runnable_org_config(config: OPCConfig, *, organization_id: Any = "") -> None:
    """驗證自訂組織是否有明確的角色定義（可安全執行）。

    功能：
        拒絕沒有角色的自訂組織，避免靜默回退到內建公司角色。
        內建公司設定（非 custom profile）不受此限制。

    參數：
        config (OPCConfig)：要驗證的設定物件。
        organization_id (Any)：組織 ID（用於錯誤訊息）。

    異常：
        RunnableOrgConfigError：自訂組織缺少角色時拋出。

    被誰引用：
        - opc/layer2_organization/company_mode.py：啟動前驗證
        - opc/cli/app.py：org run/activate 命令
    """
    org = config.org
    profile = str(getattr(org, "company_profile", "") or "").strip().lower()
    # 非 custom profile 不受限制（內建公司可回退到預設角色）
    if profile != "custom":
        return

    org_id = validate_saved_org_id(organization_id or getattr(org, "organization_id", ""))
    # 檢查是否有至少一個帶 ID 的角色
    roles = [
        role
        for role in list(getattr(org, "roles", []) or [])
        if str(getattr(role, "id", "") or "").strip()
    ]
    if roles:
        return

    raise RunnableOrgConfigError(
        f"Custom organization `{org_id}` has no roles. "
        "Refusing to activate or run it with corporate fallback roles."
    )


def write_org_config_payload(config_dir: Path, organization_id: Any, payload: dict[str, Any]) -> Path:
    """將組織設定 payload 持久化到磁碟。

    功能：
        完整的寫入流程：
        1. 驗證組織 ID
        2. 分離員工資料到獨立的員工註冊表檔案
        3. 清空 payload 中的 employees 和 talent_templates
        4. 附加 metadata 和 schema 標記
        5. 原子性寫入 YAML 檔案

    參數：
        config_dir (Path)：設定目錄路徑。
        organization_id (Any)：組織 ID。
        payload (dict[str, Any])：要寫入的組織設定 payload。

    返回值：
        Path — 寫入的設定檔路徑。

    被誰引用：
        - opc/cli/app.py：org save 命令
    """
    org_id = validate_saved_org_id(organization_id)
    path = org_config_path(config_dir, org_id)
    payload = dict(payload)
    # 分離員工資料到獨立的註冊表檔案
    raw_employees = list(payload.get("employees", []) or [])
    if raw_employees:
        from opc.core.employee_registry import load_employee_registry, write_employee_registry

        opc_home = Path(config_dir).parent
        existing = load_employee_registry(opc_home, org_id)
        write_employee_registry(opc_home, org_id, [*existing, *raw_employees])
    # 清空不寫入主設定檔的欄位
    payload["employees"] = []
    payload["talent_templates"] = []
    payload["organization_id"] = org_id
    payload.setdefault("schema_version", ORG_CONFIG_SCHEMA_VERSION)
    payload.setdefault("kind", ORG_CONFIG_KIND)
    payload["metadata"] = {
        **dict(payload.get("metadata", {}) or {}),
        "organization_config_file": org_config_relative_path(org_id),
    }
    # 原子性寫入（防止寫入中斷導致檔案損壞）
    _atomic_write_text(
        path,
        yaml.dump(payload, default_flow_style=False, sort_keys=False, allow_unicode=True),
    )
    return path


def load_org_config_payload(config_dir: Path, organization_id: Any | None = None) -> tuple[dict[str, Any], Path]:
    """載入指定（或當前活動）組織的設定 payload。

    功能：
        若未指定 organization_id，則從 org_index.yaml 讀取當前活動組織。
        載入後進行 schema 驗證。

    參數：
        config_dir (Path)：設定目錄路徑。
        organization_id (Any | None)：組織 ID。None 時使用活動組織。

    返回值：
        tuple[dict[str, Any], Path] — (驗證後的 payload, 設定檔路徑)。

    異常：
        FileNotFoundError：無活動組織或設定檔不存在時拋出。

    被誰引用：
        - opc/layer2_organization/company_mode.py：啟動公司模式時
        - opc/cli/app.py：org run/show 命令
    """
    config_dir = Path(config_dir)
    org_id = validate_saved_org_id(organization_id) if organization_id else read_org_index(config_dir)
    if not org_id:
        raise FileNotFoundError("No active org architecture is selected.")
    path = org_config_path(config_dir, org_id)
    if not path.exists():
        raise FileNotFoundError(f"Org architecture config does not exist: {path}")
    return validate_org_config_payload(path, _read_yaml_file(path)), path
