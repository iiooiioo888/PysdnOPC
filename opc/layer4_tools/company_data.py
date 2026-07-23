"""公司數據管理 Agent 工具 — 提供 AI 角色查詢和匯出公司任務數據的能力。

職責說明：
    定義 3 個 Agent 工具，允許 AI 角色在執行任務時查詢公司任務數據、
    匯出完整數據快照、取得統計摘要。

關聯關係：
    - 被引擎工具棧註冊（create_company_data_tools 回傳 ToolDefinition 列表）
    - 依賴 opc/core/data_export.py 進行數據聚合與匯出
    - 依賴 opc/database/store.py 的 OPCStore 讀取資料
    - 匯出檔案可存入共用文件庫（opc/core/shared_file_store.py）

使用範例：
    tools = create_company_data_tools(store=engine.store, file_store=shared_file_store)
    for tool in tools:
        registry.register(tool)
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from opc.core.data_export import CompanyDataExporter
from opc.core.shared_file_store import SharedFileStore
from opc.layer4_tools.registry import ToolDefinition


def create_company_data_tools(
    store: Any,
    file_store: SharedFileStore | None = None,
) -> list[ToolDefinition]:
    """建立公司數據管理的 3 個 Agent 工具。

    參數：
        store: OPCStore 實例（提供資料讀取）
        file_store: SharedFileStore 實例（選用，用於將匯出檔案存入共用文件庫）
    """
    exporter = CompanyDataExporter(store)

    async def company_data_query(
        status: str = "",
        assigned_to: str = "",
        project_id: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """查詢公司任務數據（按狀態、角色篩選）。"""
        results = await exporter.query_tasks(
            project_id=project_id,
            status=status or None,
            assigned_to=assigned_to or None,
            limit=limit,
        )
        return {
            "success": True,
            "count": len(results),
            "tasks": results,
            "filters": {
                "status": status or "(all)",
                "assigned_to": assigned_to or "(all)",
                "project_id": project_id or "(all)",
            },
        }

    async def company_data_export(
        format: str = "json",
        project_id: str = "",
        save_to_library: bool = True,
        include_work_items: bool = True,
    ) -> dict[str, Any]:
        """匯出公司數據快照（JSON 或 CSV 格式）。"""
        snapshot = await exporter.build_snapshot(
            project_id=project_id,
            include_work_items=include_work_items,
        )

        result: dict[str, Any] = {
            "success": True,
            "format": format,
            "summary": snapshot.summary,
            "exported_at": snapshot.exported_at.isoformat(),
        }

        if format.lower() == "csv":
            csv_content = exporter.tasks_to_csv(snapshot)
            result["content_preview"] = csv_content[:2000] if csv_content else "(empty)"
            result["total_tasks"] = len(snapshot.tasks)

            # 存入共用文件庫
            if save_to_library and file_store and csv_content:
                import base64
                from datetime import datetime, timezone
                filename = f"company_data_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
                b64 = base64.b64encode(csv_content.encode("utf-8")).decode("ascii")
                record = await file_store.upload_from_base64(
                    filename=filename,
                    b64_data=b64,
                    folder="exports",
                    tags=["export", "csv"],
                    description=f"公司數據匯出（CSV）— {len(snapshot.tasks)} 筆任務",
                    uploaded_by="system",
                )
                await store.save_shared_file(record)
                result["saved_file_id"] = record.file_id
                result["saved_filename"] = filename
        else:
            json_content = exporter.to_json(snapshot)
            result["content_preview"] = json_content[:2000] if json_content else "(empty)"
            result["total_tasks"] = len(snapshot.tasks)
            result["total_work_items"] = len(snapshot.work_items)
            result["total_delegation_runs"] = len(snapshot.delegation_runs)

            # 存入共用文件庫
            if save_to_library and file_store and json_content:
                import base64
                from datetime import datetime, timezone
                filename = f"company_data_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
                b64 = base64.b64encode(json_content.encode("utf-8")).decode("ascii")
                record = await file_store.upload_from_base64(
                    filename=filename,
                    b64_data=b64,
                    folder="exports",
                    tags=["export", "json"],
                    description=f"公司數據匯出（JSON）— {len(snapshot.tasks)} 筆任務, {len(snapshot.work_items)} 筆工作項",
                    uploaded_by="system",
                )
                await store.save_shared_file(record)
                result["saved_file_id"] = record.file_id
                result["saved_filename"] = filename

        return result

    async def company_data_summary(
        project_id: str = "",
    ) -> dict[str, Any]:
        """取得公司數據統計摘要。"""
        summary = await exporter.get_summary(project_id=project_id)
        return {
            "success": True,
            "project_id": project_id or "(all)",
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # 工具定義
    # ------------------------------------------------------------------

    return [
        ToolDefinition(
            name="company_data_query",
            description=(
                "查詢公司任務數據。支援按狀態（pending/running/done/failed/cancelled）、"
                "指派角色、專案篩選。回傳任務列表。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "任務狀態篩選（pending/running/done/failed/cancelled）"},
                    "assigned_to": {"type": "string", "description": "指派角色 ID 篩選"},
                    "project_id": {"type": "string", "description": "專案 ID 篩選"},
                    "limit": {"type": "integer", "description": "最大回傳數量", "default": 50},
                },
            },
            func=company_data_query,
            category="data_management",
            concurrency_safe=True,
            read_only=True,
        ),
        ToolDefinition(
            name="company_data_export",
            description=(
                "匯出公司完整數據快照（任務 + 工作項 + 協作記錄）。"
                "支援 JSON 和 CSV 格式，可自動存入共用文件庫的 exports 資料夾。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "format": {"type": "string", "enum": ["json", "csv"], "description": "匯出格式", "default": "json"},
                    "project_id": {"type": "string", "description": "專案 ID 篩選"},
                    "save_to_library": {"type": "boolean", "description": "是否存入共用文件庫", "default": True},
                    "include_work_items": {"type": "boolean", "description": "是否包含工作項數據", "default": True},
                },
            },
            func=company_data_export,
            category="data_management",
            concurrency_safe=False,
            read_only=False,
            self_bounded_output=True,
        ),
        ToolDefinition(
            name="company_data_summary",
            description=(
                "取得公司數據統計摘要：任務完成率、狀態分佈、角色工作量、"
                "工作項階段分佈、協作運行統計。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "專案 ID 篩選"},
                },
            },
            func=company_data_summary,
            category="data_management",
            concurrency_safe=True,
            read_only=True,
        ),
    ]
