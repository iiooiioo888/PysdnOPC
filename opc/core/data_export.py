"""公司數據聚合與匯出模組。

職責說明：
    將分散在 OPCStore 不同資料表中的任務、工作項、協作運行記錄
    聚合為統一的 CompanyDataSnapshot，支援 JSON/CSV 格式匯出。
    提供統計摘要（任務完成率、角色工作量、時間分佈等）。

關聯關係：
    - 被 opc/layer4_tools/company_data.py 的 Agent 工具調用
    - 被 opc/plugins/office_ui/services/data_export.py 的 UI 服務調用
    - 依賴 opc/database/store.py 的 OPCStore 讀取資料

使用範例：
    exporter = CompanyDataExporter(store)
    snapshot = await exporter.build_snapshot(project_id="proj1")
    json_str = exporter.to_json(snapshot)
    csv_str = exporter.tasks_to_csv(snapshot)
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from loguru import logger

from opc.core.models import CompanyDataSnapshot

if TYPE_CHECKING:
    from opc.database.store import OPCStore


def _serialize_value(obj: Any) -> Any:
    """將資料模型轉為可 JSON 序列化的字典。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):  # Enum
        return obj.value
    return obj


def _task_to_dict(task: Any) -> dict[str, Any]:
    """將 Task 物件轉為精簡字典。"""
    return {
        "task_id": getattr(task, "task_id", ""),
        "title": getattr(task, "title", ""),
        "description": getattr(task, "description", ""),
        "status": getattr(task, "status", ""),
        "priority": getattr(task, "priority", 5),
        "assigned_to": getattr(task, "assigned_to", ""),
        "project_id": getattr(task, "project_id", ""),
        "created_at": getattr(task, "created_at", ""),
        "updated_at": getattr(task, "updated_at", ""),
        "result": getattr(task, "result", ""),
        "tags": getattr(task, "tags", []),
    }


def _work_item_to_dict(item: Any) -> dict[str, Any]:
    """將 DelegationWorkItem 物件轉為精簡字典。"""
    return {
        "work_item_id": getattr(item, "work_item_id", ""),
        "run_id": getattr(item, "run_id", ""),
        "title": getattr(item, "title", ""),
        "description": getattr(item, "description", ""),
        "status": getattr(item, "status", ""),
        "phase": getattr(item, "phase", ""),
        "assigned_role_id": getattr(item, "assigned_role_id", ""),
        "assigned_seat_id": getattr(item, "assigned_seat_id", ""),
        "priority": getattr(item, "priority", 5),
        "created_at": getattr(item, "created_at", ""),
        "updated_at": getattr(item, "updated_at", ""),
        "result_summary": getattr(item, "result_summary", ""),
    }


def _run_to_dict(run: Any) -> dict[str, Any]:
    """將 DelegationRun 物件轉為精簡字典。"""
    return {
        "run_id": getattr(run, "run_id", ""),
        "project_id": getattr(run, "project_id", ""),
        "session_id": getattr(run, "session_id", ""),
        "status": getattr(run, "status", ""),
        "lifecycle_status": getattr(run, "lifecycle_status", ""),
        "created_at": getattr(run, "created_at", ""),
        "updated_at": getattr(run, "updated_at", ""),
    }


class CompanyDataExporter:
    """公司數據聚合器與匯出器。"""

    def __init__(self, store: OPCStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # 數據聚合
    # ------------------------------------------------------------------

    async def build_snapshot(
        self,
        project_id: str = "",
        *,
        include_work_items: bool = True,
        include_runs: bool = True,
    ) -> CompanyDataSnapshot:
        """建構完整的公司數據快照。"""
        # 1. 取得所有任務
        tasks = await self._store.get_tasks(project_id=project_id or None)
        task_dicts = [_task_to_dict(t) for t in tasks]

        # 2. 取得工作項（需要遍歷所有 delegation runs）
        work_item_dicts: list[dict[str, Any]] = []
        run_dicts: list[dict[str, Any]] = []

        runs = await self._store.list_delegation_runs(project_id=project_id or None)
        for run in runs:
            run_dicts.append(_run_to_dict(run))
            if include_work_items:
                run_id = getattr(run, "run_id", "")
                if run_id:
                    items = await self._store.list_delegation_work_items(run_id)
                    work_item_dicts.extend(_work_item_to_dict(item) for item in items)

        # 3. 計算統計摘要
        summary = self._compute_summary(task_dicts, work_item_dicts, run_dicts)

        snapshot = CompanyDataSnapshot(
            exported_at=datetime.now(timezone.utc),
            project_id=project_id,
            tasks=task_dicts,
            work_items=work_item_dicts if include_work_items else [],
            delegation_runs=run_dicts if include_runs else [],
            summary=summary,
        )
        logger.info(
            f"數據匯出：建構快照完成 — {len(task_dicts)} 任務, "
            f"{len(work_item_dicts)} 工作項, {len(run_dicts)} 運行記錄"
        )
        return snapshot

    async def query_tasks(
        self,
        project_id: str = "",
        *,
        status: str | None = None,
        assigned_to: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查詢任務（帶篩選）。"""
        from opc.core.models import TaskStatus

        task_status = None
        if status:
            try:
                task_status = TaskStatus(status.lower())
            except ValueError:
                pass

        tasks = await self._store.get_tasks(
            project_id=project_id or None,
            status=task_status,
        )
        results = [_task_to_dict(t) for t in tasks]

        # 指派者篩選
        if assigned_to:
            results = [t for t in results if t.get("assigned_to") == assigned_to]

        return results[:limit]

    async def get_summary(self, project_id: str = "") -> dict[str, Any]:
        """取得統計摘要（輕量級，不含完整數據）。"""
        tasks = await self._store.get_tasks(project_id=project_id or None)
        task_dicts = [_task_to_dict(t) for t in tasks]

        runs = await self._store.list_delegation_runs(project_id=project_id or None)
        run_dicts = [_run_to_dict(r) for r in runs]

        work_item_dicts: list[dict[str, Any]] = []
        for run in runs:
            run_id = getattr(run, "run_id", "")
            if run_id:
                items = await self._store.list_delegation_work_items(run_id)
                work_item_dicts.extend(_work_item_to_dict(item) for item in items)

        return self._compute_summary(task_dicts, work_item_dicts, run_dicts)

    # ------------------------------------------------------------------
    # 統計計算
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_summary(
        tasks: list[dict],
        work_items: list[dict],
        runs: list[dict],
    ) -> dict[str, Any]:
        """計算統計摘要。"""
        # 任務狀態分佈
        task_status_counts = Counter(str(t.get("status", "unknown")) for t in tasks)
        total_tasks = len(tasks)
        done_tasks = task_status_counts.get("done", 0)
        completion_rate = round(done_tasks / total_tasks * 100, 1) if total_tasks > 0 else 0.0

        # 角色工作量（依 assigned_to 統計）
        role_workload: dict[str, int] = {}
        for t in tasks:
            assignee = str(t.get("assigned_to", "") or "unassigned")
            role_workload[assignee] = role_workload.get(assignee, 0) + 1

        # 工作項階段分佈
        phase_counts = Counter(str(w.get("phase", "") or w.get("status", "unknown")) for w in work_items)

        # 運行狀態分佈
        run_status_counts = Counter(str(r.get("status", "unknown")) for r in runs)

        return {
            "total_tasks": total_tasks,
            "task_status_distribution": dict(task_status_counts),
            "completion_rate_percent": completion_rate,
            "role_workload": role_workload,
            "total_work_items": len(work_items),
            "work_item_phase_distribution": dict(phase_counts),
            "total_delegation_runs": len(runs),
            "run_status_distribution": dict(run_status_counts),
        }

    # ------------------------------------------------------------------
    # 匯出格式
    # ------------------------------------------------------------------

    @staticmethod
    def to_json(snapshot: CompanyDataSnapshot, *, indent: int = 2) -> str:
        """將快照匯出為 JSON 字串。"""
        data = {
            "exported_at": snapshot.exported_at.isoformat(),
            "project_id": snapshot.project_id,
            "summary": snapshot.summary,
            "tasks": snapshot.tasks,
            "work_items": snapshot.work_items,
            "delegation_runs": snapshot.delegation_runs,
        }
        return json.dumps(data, ensure_ascii=False, indent=indent, default=str)

    @staticmethod
    def tasks_to_csv(snapshot: CompanyDataSnapshot) -> str:
        """將任務列表匯出為 CSV 字串。"""
        if not snapshot.tasks:
            return ""
        output = io.StringIO()
        fieldnames = ["task_id", "title", "status", "priority", "assigned_to", "created_at", "updated_at", "result"]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for task in snapshot.tasks:
            writer.writerow(task)
        return output.getvalue()

    @staticmethod
    def work_items_to_csv(snapshot: CompanyDataSnapshot) -> str:
        """將工作項列表匯出為 CSV 字串。"""
        if not snapshot.work_items:
            return ""
        output = io.StringIO()
        fieldnames = ["work_item_id", "title", "status", "phase", "assigned_role_id", "priority", "created_at", "updated_at"]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for item in snapshot.work_items:
            writer.writerow(item)
        return output.getvalue()
