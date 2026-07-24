"""角色每日任務調度模組。

將生命週期意圖檢測結果路由至角色任務調度的最小方案：
在 .opc/config/daily_task_templates.yaml 中定義角色每日任務模板，
本模組負責讀取模板、查詢角色每日任務列表、並將模板實例化為
與 work-item 狀態機完全兼容的 DelegationWorkItem。

設計原則：
- 每日任務模板是靜態配置，實例化後的 work item 進入標準 Phase 狀態機
- 初始 Phase 為 QUEUED（等待 manager 釋放），完全兼容 ALLOWED_TRANSITIONS
- turn_type 對應 CANONICAL_WORK_ITEM_TURN_TYPES 中的合法值
- 不修改現有任何 work-item 模型或狀態機邏輯
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from opc.core.config import get_opc_home
from opc.core.models import DelegationWorkItem, Phase
from opc.layer2_organization.phase import ALLOWED_TRANSITIONS, coerce_phase
from opc.layer2_organization.work_item_identity import (
    CANONICAL_WORK_ITEM_TURN_TYPES,
    normalize_work_item_turn_type,
)

__all__ = [
    "DailyTaskTemplate",
    "DailyTaskScheduleSettings",
    "RoleDailyTaskList",
    "load_daily_task_config",
    "get_role_daily_tasks",
    "get_all_roles_with_daily_tasks",
    "instantiate_daily_work_items",
    "validate_daily_task_phase_compat",
    "display_name_for_employee",
    "DAILY_TASK_TEMPLATES_FILENAME",
]

DAILY_TASK_TEMPLATES_FILENAME = "daily_task_templates.yaml"


@dataclass(frozen=True)
class DailyTaskTemplate:
    """單個每日任務模板定義。"""

    task_id: str
    title: str
    summary: str = ""
    turn_type: str = "execute"
    priority: str = "medium"
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """確保 turn_type 為合法值。"""
        normalized = normalize_work_item_turn_type(self.turn_type, fallback="execute")
        if normalized not in CANONICAL_WORK_ITEM_TURN_TYPES:
            normalized = "execute"
        object.__setattr__(self, "turn_type", normalized)


@dataclass(frozen=True)
class DailyTaskScheduleSettings:
    """每日任務調度的全域設定。"""

    default_initial_phase: str = "queued"
    default_turn_type: str = "execute"
    show_on_kanban: bool = True


@dataclass
class RoleDailyTaskList:
    """某個角色的每日任務列表。"""

    role_id: str
    tasks: list[DailyTaskTemplate] = field(default_factory=list)

    @property
    def task_count(self) -> int:
        return len(self.tasks)

    def get_task(self, task_id: str) -> DailyTaskTemplate | None:
        """按 task_id 查找單個任務模板。"""
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        return None


def _config_path(opc_home: Path | None = None) -> Path:
    """取得每日任務模板配置檔案路徑。"""
    home = opc_home or get_opc_home()
    return home / "config" / DAILY_TASK_TEMPLATES_FILENAME


def load_daily_task_config(
    opc_home: Path | None = None,
) -> tuple[DailyTaskScheduleSettings, dict[str, RoleDailyTaskList]]:
    """載入每日任務模板配置。

    Returns:
        (settings, role_tasks_map) — 全域設定與角色任務映射。
        配置不存在時返回預設設定和空映射。
    """
    path = _config_path(opc_home)
    if not path.exists():
        logger.debug(f"daily task config not found: {path}")
        return DailyTaskScheduleSettings(), {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning(f"failed to load daily task config {path}: {exc}")
        return DailyTaskScheduleSettings(), {}

    # 解析 settings
    raw_settings = dict(data.get("settings", {}) or {})
    settings = DailyTaskScheduleSettings(
        default_initial_phase=str(raw_settings.get("default_initial_phase", "queued") or "queued").strip().lower(),
        default_turn_type=str(raw_settings.get("default_turn_type", "execute") or "execute").strip().lower(),
        show_on_kanban=bool(raw_settings.get("show_on_kanban", True)),
    )

    # 解析 role_daily_tasks
    raw_roles = dict(data.get("role_daily_tasks", {}) or {})
    role_tasks_map: dict[str, RoleDailyTaskList] = {}

    for role_id, raw_tasks in raw_roles.items():
        role_id_clean = str(role_id or "").strip()
        if not role_id_clean or not isinstance(raw_tasks, list):
            continue
        tasks: list[DailyTaskTemplate] = []
        for raw_task in raw_tasks:
            if not isinstance(raw_task, dict):
                continue
            task_id = str(raw_task.get("task_id", "") or "").strip()
            if not task_id:
                continue
            tasks.append(
                DailyTaskTemplate(
                    task_id=task_id,
                    title=str(raw_task.get("title", "") or task_id).strip(),
                    summary=str(raw_task.get("summary", "") or "").strip(),
                    turn_type=str(raw_task.get("turn_type", "") or settings.default_turn_type).strip(),
                    priority=str(raw_task.get("priority", "medium") or "medium").strip().lower(),
                    tags=[str(t).strip() for t in list(raw_task.get("tags", []) or []) if str(t).strip()],
                    metadata={k: v for k, v in dict(raw_task.get("metadata", {}) or {}).items()},
                )
            )
        if tasks:
            role_tasks_map[role_id_clean] = RoleDailyTaskList(role_id=role_id_clean, tasks=tasks)

    return settings, role_tasks_map


def get_role_daily_tasks(
    role_id: str,
    *,
    opc_home: Path | None = None,
) -> RoleDailyTaskList | None:
    """取得指定角色的每日任務列表。

    Args:
        role_id: 角色 ID
        opc_home: OPC 主目錄（預設自動偵測）

    Returns:
        RoleDailyTaskList 或 None（角色無每日任務時）
    """
    _, role_tasks_map = load_daily_task_config(opc_home)
    return role_tasks_map.get(str(role_id or "").strip())


def get_all_roles_with_daily_tasks(
    opc_home: Path | None = None,
) -> list[str]:
    """取得所有擁有每日任務的角色 ID 列表。"""
    _, role_tasks_map = load_daily_task_config(opc_home)
    return sorted(role_tasks_map.keys())


def validate_daily_task_phase_compat(
    initial_phase: str | Phase = Phase.QUEUED,
) -> bool:
    """驗證每日任務的初始 Phase 與狀態機兼容。

    條件：
    1. initial_phase 是合法的 Phase 值
    2. initial_phase 在 ALLOWED_TRANSITIONS 中有定義（即狀態機可從該狀態出發）
    """
    try:
        phase = coerce_phase(initial_phase)
    except (ValueError, TypeError):
        return False
    return phase in ALLOWED_TRANSITIONS


def display_name_for_employee(employee: Any) -> str:
    """取得員工的顯示名稱：優先使用 persona_name，fallback 到 name。"""
    persona = str(getattr(employee, "persona_name", "") or "").strip()
    if persona:
        return persona
    return str(getattr(employee, "name", "") or "").strip()


def instantiate_daily_work_items(
    role_id: str,
    *,
    run_id: str = "",
    cell_id: str = "",
    team_instance_id: str = "",
    team_id: str = "",
    seat_id: str = "",
    manager_role_id: str = "",
    batch_id: str = "",
    initial_phase: str | Phase | None = None,
    persona_name: str = "",
    repo_manager: Any | None = None,
    opc_home: Path | None = None,
) -> list[DelegationWorkItem]:
    """將角色的每日任務模板實例化為 DelegationWorkItem 列表。

    實例化後的 work item 使用 Phase.QUEUED（或配置的 default_initial_phase），
    完全兼容現有的 work-item 狀態機（ALLOWED_TRANSITIONS）。

    Args:
        role_id: 目標角色 ID
        run_id: 所屬委派執行 ID
        cell_id: 所屬委派單元 ID
        team_instance_id: 團隊實例 ID
        team_id: 團隊 ID
        seat_id: 席位 ID
        manager_role_id: 經理角色 ID
        batch_id: 批次 ID
        initial_phase: 初始 Phase（預設使用配置的 default_initial_phase）
        opc_home: OPC 主目錄

    Returns:
        DelegationWorkItem 列表（可能為空）
    """
    settings, role_tasks_map = load_daily_task_config(opc_home)
    role_tasks = role_tasks_map.get(str(role_id or "").strip())
    if not role_tasks:
        return []

    # 決定初始 Phase
    if initial_phase is None:
        phase_str = settings.default_initial_phase
    elif isinstance(initial_phase, Phase):
        phase_str = initial_phase.value
    else:
        phase_str = str(initial_phase).strip().lower()

    # 驗證 Phase 兼容性
    if not validate_daily_task_phase_compat(phase_str):
        logger.warning(
            f"daily task initial phase '{phase_str}' is not state-machine compatible, "
            f"falling back to 'queued'"
        )
        phase_str = "queued"

    phase = coerce_phase(phase_str)

    work_items: list[DelegationWorkItem] = []
    for index, task in enumerate(role_tasks.tasks):
        metadata: dict[str, Any] = {
            "daily_task": True,
            "daily_task_id": task.task_id,
            "daily_task_priority": task.priority,
            "daily_task_tags": list(task.tags),
            "work_kind": task.turn_type,
            "work_item_turn_type": task.turn_type,
            "source": "daily_task_schedule",
        }
        if persona_name:
            metadata["persona_name"] = persona_name
        # 從角色技能庫讀取 skill_refs
        if repo_manager and hasattr(repo_manager, "list_role_skills"):
            try:
                role_skills = repo_manager.list_role_skills(str(role_id or "").strip())
                if role_skills:
                    metadata["skill_refs"] = list(role_skills)
            except Exception:
                pass
        if not settings.show_on_kanban:
            metadata["hidden_from_company_kanban"] = True
        metadata.update(task.metadata)

        work_items.append(
            DelegationWorkItem(
                run_id=run_id,
                cell_id=cell_id,
                team_instance_id=team_instance_id,
                team_id=team_id,
                role_id=str(role_id or "").strip(),
                seat_id=seat_id,
                manager_role_id=manager_role_id,
                title=task.title,
                summary=task.summary,
                kind=task.turn_type,
                phase=phase,
                batch_id=batch_id,
                batch_index=index,
                metadata=metadata,
            )
        )

    return work_items
