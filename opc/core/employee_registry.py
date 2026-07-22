"""公司員工註冊表持久化輔助模組。

職責說明：
    管理公司員工記錄的持久化儲存。員工是運行時/公司資產，而組織設定檔
    描述的是角色圖譜和策略。本模組將員工記錄獨立於組織 YAML 之外儲存，
    並將基於模板的員工正規化為標準的 template_id。

    核心功能：
    - 員工記錄的載入、寫入、正規化
    - ID 別名管理（legacy_employee_ids → canonical_id）
    - 員工進化資料（evolution）的 ID 遷移
    - 多來源員工記錄的合併去重

關聯關係：
    - 被 opc/core/org_config.py 在寫入/載入組織設定時調用
    - 被 opc/layer2_organization/company_mode.py 在初始化員工列表時調用
    - 被 opc/layer5_memory/employee_evolution.py 在更新進化資料時調用
    - 儲存路徑：{opc_home}/company_state/{org_id}/employees/{employee_id}.yaml

使用範例：
    from opc.core.employee_registry import load_company_employees, write_employee_registry
    employees = load_company_employees(opc_home, "my-org", legacy_list)
    normalized, aliases = write_employee_registry(opc_home, "my-org", updated_list)
"""

from __future__ import annotations  # 啟用延遲型別註解評估

import json  # 標準庫：讀寫員工進化資料（JSON 格式）
import re  # 標準庫：正規化員工 ID 為 slug 格式
from pathlib import Path  # 標準庫：跨平台路徑操作
from typing import Any  # 標準庫：payload 字典的值型別註解

import yaml  # 第三方庫 PyYAML：員工記錄的 YAML 序列化/反序列化

from opc.core.config import EmployeeConfig, validate_organization_id  # 員工配置模型和組織 ID 驗證

# 員工註冊表的 schema 版本號
EMPLOYEE_REGISTRY_SCHEMA_VERSION = 1

# 員工註冊表檔案的 kind 標記
EMPLOYEE_REGISTRY_KIND = "company_employee"

# 進化資料中需要累加（而非覆蓋）的計數欄位集合
_COUNT_KEYS = {
    "successes",  # 成功次數
    "partial_successes",  # 部分成功次數
    "failures",  # 失敗次數
    "reflection_count",  # 反思次數
}


def _slugify(value: str) -> str:
    """將字串轉為檔案系統安全的 slug 格式（內部輔助函數）。

    功能：
        將任意字串轉為小寫，非法字元替換為連字號。
        空結果回退為 "employee"。

    參數：
        value (str)：原始字串（如員工 ID）。

    返回值：
        str — 安全的 slug 字串（僅含 a-z、0-9、.、_、-）。
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug or "employee"


def employee_registry_dir(opc_home: Path, organization_id: Any) -> Path:
    """取得指定組織的員工註冊表目錄路徑。

    參數：
        opc_home (Path)：OPC 主目錄。
        organization_id (Any)：組織 ID。

    返回值：
        Path — {opc_home}/company_state/{org_id}/employees/ 路徑。
    """
    org_id = validate_organization_id(organization_id)
    return Path(opc_home) / "company_state" / org_id / "employees"


def employee_registry_path(opc_home: Path, organization_id: Any, employee_id: str) -> Path:
    """取得指定員工的註冊表檔案路徑。

    參數：
        opc_home (Path)：OPC 主目錄。
        organization_id (Any)：組織 ID。
        employee_id (str)：員工 ID。

    返回值：
        Path — 員工 YAML 檔案的完整路徑。
    """
    filename = f"{_slugify(employee_id)}.yaml"
    return employee_registry_dir(opc_home, organization_id) / filename


def is_placeholder_employee(employee: EmployeeConfig | dict[str, Any]) -> bool:
    """判斷員工是否為佔位符（預設/回退員工）。

    功能：
        佔位符員工是系統自動產生的預設員工，不計入正式員工列表。
        透過 metadata 中的 is_default_employee 或 is_fallback_employee 標記判斷。

    參數：
        employee (EmployeeConfig | dict[str, Any])：員工配置物件或字典。

    返回值：
        bool — True 表示為佔位符員工。

    被誰引用：
        - normalize_employee_records()：分離佔位符和正式員工
        - write_employee_registry()：僅持久化正式員工
    """
    metadata = dict(employee.metadata if isinstance(employee, EmployeeConfig) else employee.get("metadata") or {})
    return bool(metadata.get("is_default_employee") or metadata.get("is_fallback_employee"))


def _employee_from_payload(raw: Any) -> EmployeeConfig | None:
    """從任意 payload 安全地建構 EmployeeConfig（內部輔助函數）。

    功能：
        支援多種輸入格式：EmployeeConfig 實例、包含 "employee" 鍵的字典、
        直接的員工字典。無法解析時返回 None。

    參數：
        raw (Any)：原始資料（EmployeeConfig、dict 或其他）。

    返回值：
        EmployeeConfig | None — 解析後的員工配置，或 None（解析失敗）。
    """
    if isinstance(raw, EmployeeConfig):
        return raw.model_copy(deep=True)
    if not isinstance(raw, dict):
        return None
    # 支援 {"employee": {...}} 包裝格式
    payload = raw.get("employee") if isinstance(raw.get("employee"), dict) else raw
    try:
        return EmployeeConfig.model_validate(payload)
    except Exception:
        return None


def _append_unique(items: list[Any], additions: list[Any]) -> list[Any]:
    """將新項目附加到列表中（去重）（內部輔助函數）。

    參數：
        items (list[Any])：基礎列表。
        additions (list[Any])：要附加的新項目。

    返回值：
        list[Any] — 合併後的去重列表（保持原始順序）。
    """
    result = list(items or [])
    for item in list(additions or []):
        if item not in result:
            result.append(item)
    return result


def _merge_metadata(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """合併兩個 metadata 字典（內部輔助函數）。

    功能：
        合併規則：
        - legacy_employee_ids、home_role_ids、staffed_role_ids：串列合併去重
        - 新鍵或基礎值為空：直接覆蓋
        - 嵌套字典：遞迴合併
        - 其他：保留基礎值

    參數：
        base (dict[str, Any])：基礎 metadata。
        incoming (dict[str, Any])：傳入的 metadata。

    返回值：
        dict[str, Any] — 合併後的 metadata。
    """
    merged = dict(base or {})
    for key, value in dict(incoming or {}).items():
        if key == "legacy_employee_ids":
            merged[key] = _append_unique(list(merged.get(key, []) or []), list(value or []))
        elif key in {"home_role_ids", "staffed_role_ids"}:
            merged[key] = _append_unique(list(merged.get(key, []) or []), list(value or []))
        elif key not in merged or _is_empty_value(merged.get(key)):
            merged[key] = value
        elif isinstance(merged.get(key), dict) and isinstance(value, dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
    return merged


def _canonicalize_employee(employee: EmployeeConfig) -> tuple[EmployeeConfig, dict[str, str]]:
    """將員工正規化為標準 ID（內部輔助函數）。

    功能：
        若員工有 template_id，則以 template_id 作為標準 ID，
        原始 ID 移入 legacy_employee_ids。同時維護 home_role_ids
        和 staffed_role_ids 的累積記錄。

    參數：
        employee (EmployeeConfig)：待正規化的員工配置。

    返回值：
        tuple[EmployeeConfig, dict[str, str]] — (正規化後的員工, {舊ID: 標準ID} 別名映射)。
    """
    # 佔位符員工不需要正規化
    if is_placeholder_employee(employee):
        return employee.model_copy(deep=True), {}

    old_id = str(employee.employee_id or "").strip()
    template_id = str(employee.template_id or "").strip()
    # 標準 ID：優先使用 template_id，其次使用原始 ID
    canonical_id = template_id or old_id
    metadata = dict(employee.metadata or {})
    aliases: dict[str, str] = {}

    # 維護 legacy_employee_ids 列表
    legacy_ids = [str(item).strip() for item in list(metadata.get("legacy_employee_ids", []) or []) if str(item).strip()]
    if old_id and old_id != canonical_id and old_id not in legacy_ids:
        legacy_ids.append(old_id)
    if legacy_ids:
        metadata["legacy_employee_ids"] = legacy_ids
        for legacy_id in legacy_ids:
            aliases[legacy_id] = canonical_id

    # 維護角色 ID 的累積記錄
    role_id = str(employee.role_id or "").strip()
    if role_id:
        metadata.setdefault("home_role_id", role_id)
        metadata["home_role_ids"] = _append_unique(list(metadata.get("home_role_ids", []) or []), [role_id])
        metadata["staffed_role_ids"] = _append_unique(list(metadata.get("staffed_role_ids", []) or []), [role_id])
    if template_id:
        metadata.setdefault("canonical_employee_id", canonical_id)

    return employee.model_copy(update={"employee_id": canonical_id, "metadata": metadata}), aliases


def _merge_employee(base: EmployeeConfig, incoming: EmployeeConfig) -> EmployeeConfig:
    """合併兩個員工記錄（內部輔助函數）。

    功能：
        合併規則：
        - 串列欄位（domains、tags 等）：去重合併
        - 字串欄位（name、description 等）：基礎為空時使用傳入值；
          description 特殊處理：取較長者
        - metadata：使用 _merge_metadata 合併

    參數：
        base (EmployeeConfig)：基礎員工記錄。
        incoming (EmployeeConfig)：傳入的員工記錄。

    返回值：
        EmployeeConfig — 合併後的員工配置。
    """
    merged = base.model_dump()
    other = incoming.model_dump()
    # 串列欄位：去重合併
    for field in ("domains", "tags", "prompt_refs", "skill_refs"):
        merged[field] = _append_unique(list(merged.get(field, []) or []), list(other.get(field, []) or []))
    # 字串欄位：基礎為空時填充；description 取較長者
    for field in ("name", "template_id", "description", "category", "preferred_external_agent", "seniority", "status"):
        if not merged.get(field) and other.get(field):
            merged[field] = other[field]
        elif field == "description" and len(str(other.get(field, ""))) > len(str(merged.get(field, ""))):
            merged[field] = other[field]
    if not merged.get("role_id") and other.get("role_id"):
        merged["role_id"] = other["role_id"]
    merged["metadata"] = _merge_metadata(dict(merged.get("metadata", {}) or {}), dict(other.get("metadata", {}) or {}))
    return EmployeeConfig.model_validate(merged)


def normalize_employee_records(employees: list[Any]) -> tuple[list[EmployeeConfig], dict[str, str]]:
    """正規化員工記錄列表：去重、合併、正規化 ID。

    功能：
        處理來自多個來源的員工記錄：
        1. 分離佔位符員工和正式員工
        2. 正規化每個員工的 ID（template_id 優先）
        3. 相同 ID 的員工進行合併
        4. 按 category → name → id 排序

    參數：
        employees (list[Any])：原始員工記錄列表（可為 EmployeeConfig 或 dict）。

    返回值：
        tuple[list[EmployeeConfig], dict[str, str]] —
        (正規化後的員工列表（正式 + 佔位符）, {舊ID: 標準ID} 別名映射)。

    被誰引用：
        - load_company_employees()：載入時正規化
        - write_employee_registry()：寫入前正規化
    """
    real_by_id: dict[str, EmployeeConfig] = {}
    placeholders: list[EmployeeConfig] = []
    aliases: dict[str, str] = {}
    for raw in list(employees or []):
        employee = _employee_from_payload(raw)
        if employee is None:
            continue
        if is_placeholder_employee(employee):
            placeholders.append(employee)
            continue
        canonical, employee_aliases = _canonicalize_employee(employee)
        aliases.update(employee_aliases)
        existing = real_by_id.get(canonical.employee_id)
        real_by_id[canonical.employee_id] = _merge_employee(existing, canonical) if existing else canonical
    # 按 category → name → id 排序
    real = sorted(real_by_id.values(), key=lambda item: (item.category, item.name.lower(), item.employee_id))
    return [*real, *placeholders], aliases


def load_employee_registry(opc_home: Path, organization_id: Any) -> list[EmployeeConfig]:
    """從磁碟載入指定組織的員工註冊表。

    功能：
        讀取 {opc_home}/company_state/{org_id}/employees/ 目錄下
        所有 .yaml 檔案，解析為 EmployeeConfig 列表。

    參數：
        opc_home (Path)：OPC 主目錄。
        organization_id (Any)：組織 ID。

    返回值：
        list[EmployeeConfig] — 員工配置列表。目錄不存在時返回空列表。

    被誰引用：
        - load_company_employees()：作為主要資料來源
        - write_employee_registry()：讀取現有記錄
    """
    directory = employee_registry_dir(opc_home, organization_id)
    if not directory.is_dir():
        return []
    employees: list[EmployeeConfig] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue  # 跳過無法解析的檔案
        employee = _employee_from_payload(data)
        if employee is not None:
            employees.append(employee)
    return employees


def load_company_employees(
    opc_home: Path,
    organization_id: Any,
    legacy_employees: list[Any],
) -> list[EmployeeConfig]:
    """載入並合併公司員工（註冊表 + 舊版設定）。

    功能：
        將磁碟上的員工註冊表與舊版設定中的員工列表合併正規化，
        並觸發進化資料的 ID 遷移。

    參數：
        opc_home (Path)：OPC 主目錄。
        organization_id (Any)：組織 ID。
        legacy_employees (list[Any])：舊版設定中的員工列表。

    返回值：
        list[EmployeeConfig] — 正規化後的完整員工列表。

    被誰引用：
        - opc/core/org_config.py：載入組織設定時
        - opc/layer2_organization/company_mode.py：初始化公司模式時
    """
    registry_employees = load_employee_registry(opc_home, organization_id)
    employees, aliases = normalize_employee_records([*registry_employees, *list(legacy_employees or [])])
    # 觸發進化資料的 ID 遷移（舊 ID → 標準 ID）
    migrate_evolution_employee_ids(opc_home, aliases)
    return employees


def write_employee_registry(
    opc_home: Path,
    organization_id: Any,
    employees: list[Any],
) -> tuple[list[EmployeeConfig], dict[str, str]]:
    """將員工列表正規化並持久化到磁碟。

    功能：
        完整的寫入流程：
        1. 正規化所有員工記錄
        2. 僅持久化正式員工（排除佔位符）
        3. 每個員工寫入獨立的 YAML 檔案
        4. 刪除不再存在的舊檔案
        5. 觸發進化資料的 ID 遷移

    參數：
        opc_home (Path)：OPC 主目錄。
        organization_id (Any)：組織 ID。
        employees (list[Any])：要寫入的員工記錄列表。

    返回值：
        tuple[list[EmployeeConfig], dict[str, str]] —
        (正規化後的員工列表, 別名映射)。

    被誰引用：
        - opc/core/org_config.py：儲存組織設定時
        - opc/cli/app.py：員工管理命令
    """
    normalized, aliases = normalize_employee_records(employees)
    real_employees = [employee for employee in normalized if not is_placeholder_employee(employee)]
    directory = employee_registry_dir(opc_home, organization_id)
    directory.mkdir(parents=True, exist_ok=True)
    expected_paths: set[Path] = set()
    # 寫入每個正式員工的 YAML 檔案
    for employee in real_employees:
        path = employee_registry_path(opc_home, organization_id, employee.employee_id)
        expected_paths.add(path)
        payload = {
            "schema_version": EMPLOYEE_REGISTRY_SCHEMA_VERSION,
            "kind": EMPLOYEE_REGISTRY_KIND,
            "organization_id": validate_organization_id(organization_id),
            "employee": employee.model_dump(),
        }
        _atomic_write_text(
            path,
            yaml.dump(payload, default_flow_style=False, sort_keys=False, allow_unicode=True),
        )
    # 清理不再存在的舊檔案
    for path in directory.glob("*.yaml"):
        if path not in expected_paths:
            try:
                path.unlink()
            except OSError:
                pass
    # 觸發進化資料的 ID 遷移
    migrate_evolution_employee_ids(opc_home, aliases)
    return normalized, aliases


def migrate_evolution_employee_ids(opc_home: Path, aliases: dict[str, str]) -> None:
    """將員工進化資料中的舊 ID 遷移到標準 ID。

    功能：
        掃描所有進化資料檔案（employees.json），將使用舊 ID 的記錄
        合併到標準 ID 下。計數欄位累加，串列欄位去重合併。

    參數：
        opc_home (Path)：OPC 主目錄。
        aliases (dict[str, str])：{舊ID: 標準ID} 的別名映射。

    被誰引用：
        - load_company_employees()：載入後自動遷移
        - write_employee_registry()：寫入後自動遷移
    """
    # 過濾有效的別名（新舊不同）
    canonical_aliases = {
        str(old).strip(): str(new).strip()
        for old, new in dict(aliases or {}).items()
        if str(old).strip() and str(new).strip() and str(old).strip() != str(new).strip()
    }
    if not canonical_aliases:
        return
    # 收集所有進化資料檔案路徑
    paths: list[Path] = [Path(opc_home) / "evolution" / "employees.json"]
    projects_dir = Path(opc_home) / "projects"
    if projects_dir.is_dir():
        paths.extend(sorted(projects_dir.glob("*/employee_evolution.json")))
    # 逐一處理每個進化檔案
    for path in paths:
        if not path.exists():
            continue
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(profile, dict):
            continue
        employees = profile.get("employees")
        if not isinstance(employees, dict):
            continue
        changed = False
        # 將舊 ID 的記錄合併到標準 ID
        for legacy_id, canonical_id in canonical_aliases.items():
            if legacy_id not in employees:
                continue
            legacy_record = employees.pop(legacy_id)
            current = employees.get(canonical_id)
            employees[canonical_id] = _merge_evolution_records(current, legacy_record)
            changed = True
        # 有變更時原子性寫回
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _merge_evolution_records(base: Any, incoming: Any) -> Any:
    """合併兩筆進化記錄（內部輔助函數，遞迴處理）。

    功能：
        合併規則：
        - 計數欄位（_COUNT_KEYS）：累加
        - 串列：去重合併
        - 字典：遞迴合併
        - 其他：基礎為空時使用傳入值

    參數：
        base (Any)：基礎記錄。
        incoming (Any)：傳入記錄。

    返回值：
        Any — 合併後的記錄。
    """
    if isinstance(base, dict) and isinstance(incoming, dict):
        merged = dict(base)
        for key, value in incoming.items():
            if key in _COUNT_KEYS and isinstance(value, (int, float)):
                merged[key] = int(merged.get(key, 0) or 0) + int(value)
            elif isinstance(value, list):
                merged[key] = _append_unique(list(merged.get(key, []) or []), value)
            elif isinstance(value, dict):
                merged[key] = _merge_evolution_records(merged.get(key, {}), value)
            elif key not in merged or _is_empty_value(merged.get(key)):
                merged[key] = value
        return merged
    if isinstance(base, list) and isinstance(incoming, list):
        return _append_unique(base, incoming)
    return base if not _is_empty_value(base) else incoming


def _is_empty_value(value: Any) -> bool:
    """判斷值是否為「空」（內部輔助函數）。

    返回值：
        bool — None、空字串、空列表、空字典均視為空。
    """
    return value is None or value == "" or value == [] or value == {}


def _atomic_write_text(path: Path, content: str) -> None:
    """原子性寫入文字檔（內部輔助函數）。

    功能：
        先寫入臨時檔案再重命名，防止寫入中斷導致檔案損壞。

    參數：
        path (Path)：目標檔案路徑。
        content (str)：要寫入的文字內容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)  # 原子性重命名
