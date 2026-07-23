"""共用文件庫 Agent 工具 — 提供 AI 角色存取公司共享檔案的能力。

職責說明：
    定義 5 個 Agent 工具，允許 AI 角色在執行任務時上傳、列出、讀取、
    搜尋和刪除公司共用文件庫中的檔案。

關聯關係：
    - 被引擎工具棧註冊（create_shared_file_tools 回傳 ToolDefinition 列表）
    - 依賴 opc/core/shared_file_store.py 進行磁碟操作
    - 依賴 opc/database/store.py 的 OPCStore 進行索引 CRUD

使用範例：
    tools = create_shared_file_tools(store=engine.store, file_store=shared_file_store)
    for tool in tools:
        registry.register(tool)
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from opc.core.models import SharedFileRecord
from opc.core.shared_file_store import SharedFileStore
from opc.layer4_tools.registry import ToolDefinition


def create_shared_file_tools(
    store: Any,
    file_store: SharedFileStore,
) -> list[ToolDefinition]:
    """建立共用文件庫的 5 個 Agent 工具。

    參數：
        store: OPCStore 實例（提供 SQLite 索引 CRUD）
        file_store: SharedFileStore 實例（提供磁碟檔案操作）
    """

    async def shared_file_upload(
        filename: str,
        content_base64: str = "",
        source_path: str = "",
        folder: str = "",
        tags: str = "",
        description: str = "",
        task: Any | None = None,
    ) -> dict[str, Any]:
        """上傳檔案到公司共用文件庫。"""
        uploaded_by = ""
        if task:
            uploaded_by = str(getattr(task, "assigned_to", "") or "")

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

        if content_base64:
            record = await file_store.upload_from_base64(
                filename=filename,
                b64_data=content_base64,
                folder=folder,
                tags=tag_list,
                description=description,
                uploaded_by=uploaded_by,
            )
        elif source_path:
            record = await file_store.upload_from_path(
                source_path=source_path,
                filename=filename or None,
                folder=folder,
                tags=tag_list,
                description=description,
                uploaded_by=uploaded_by,
            )
        else:
            return {"error": "必須提供 content_base64 或 source_path 其中之一", "success": False}

        # 寫入索引
        await store.save_shared_file(record)
        return {
            "success": True,
            "file_id": record.file_id,
            "filename": record.filename,
            "folder": record.folder,
            "size_bytes": record.size_bytes,
            "mime_type": record.mime_type,
            "message": f"檔案 '{record.filename}' 已上傳到共用文件庫",
        }

    async def shared_file_list(
        folder: str | None = None,
        tags: str = "",
        uploaded_by: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """列出共用文件庫中的檔案。"""
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        records = await store.list_shared_files(
            folder=folder,
            uploaded_by=uploaded_by or None,
            tags=tag_list,
            limit=limit,
        )
        files = [
            {
                "file_id": r.file_id,
                "filename": r.filename,
                "folder": r.folder,
                "mime_type": r.mime_type,
                "size_bytes": r.size_bytes,
                "tags": r.tags,
                "description": r.description,
                "uploaded_by": r.uploaded_by,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in records
        ]
        return {
            "success": True,
            "count": len(files),
            "files": files,
        }

    async def shared_file_read(
        file_id: str,
        max_chars: int = 10000,
    ) -> dict[str, Any]:
        """讀取共用文件庫中的檔案內容。"""
        record = await store.get_shared_file(file_id)
        if not record:
            return {"error": f"找不到檔案：{file_id}", "success": False}

        if not file_store.file_exists_on_disk(record):
            return {"error": f"檔案不存在於磁碟：{record.filename}", "success": False}

        result: dict[str, Any] = {
            "success": True,
            "file_id": record.file_id,
            "filename": record.filename,
            "folder": record.folder,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "tags": record.tags,
            "description": record.description,
        }

        if file_store.is_text_file(record):
            content = file_store.read_text(record)
            if len(content) > max_chars:
                result["content"] = content[:max_chars]
                result["truncated"] = True
                result["total_chars"] = len(content)
            else:
                result["content"] = content
                result["truncated"] = False
        else:
            result["content"] = None
            result["note"] = f"二進位檔案（{record.mime_type}），無法直接顯示內容。可使用 shared_file_download 取得 base64 編碼。"

        return result

    async def shared_file_search(
        query: str,
        limit: int = 30,
    ) -> dict[str, Any]:
        """搜尋共用文件庫（依檔名、描述、標籤）。"""
        records = await store.search_shared_files(query, limit=limit)
        files = [
            {
                "file_id": r.file_id,
                "filename": r.filename,
                "folder": r.folder,
                "mime_type": r.mime_type,
                "size_bytes": r.size_bytes,
                "tags": r.tags,
                "description": r.description,
                "uploaded_by": r.uploaded_by,
            }
            for r in records
        ]
        return {
            "success": True,
            "query": query,
            "count": len(files),
            "files": files,
        }

    async def shared_file_delete(
        file_id: str,
    ) -> dict[str, Any]:
        """從共用文件庫刪除檔案。"""
        record = await store.get_shared_file(file_id)
        if not record:
            return {"error": f"找不到檔案：{file_id}", "success": False}

        # 刪除磁碟檔案
        file_store.delete_from_disk(record)
        # 刪除索引
        await store.delete_shared_file(file_id)

        return {
            "success": True,
            "file_id": file_id,
            "filename": record.filename,
            "message": f"檔案 '{record.filename}' 已從共用文件庫刪除",
        }

    # ------------------------------------------------------------------
    # 工具定義
    # ------------------------------------------------------------------

    return [
        ToolDefinition(
            name="shared_file_upload",
            description=(
                "上傳檔案到公司共用文件庫。支援 Base64 編碼內容或本地路徑。"
                "所有 AI 角色共享此文件空間，可透過資料夾和標籤分類管理。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "檔案名稱（含副檔名）"},
                    "content_base64": {"type": "string", "description": "Base64 編碼的檔案內容"},
                    "source_path": {"type": "string", "description": "本地檔案路徑（與 content_base64 二選一）"},
                    "folder": {"type": "string", "description": "目標資料夾路徑（如 'reports/2026'）", "default": ""},
                    "tags": {"type": "string", "description": "標籤，逗號分隔（如 'report,finance'）", "default": ""},
                    "description": {"type": "string", "description": "檔案描述", "default": ""},
                },
                "required": ["filename"],
            },
            func=shared_file_upload,
            category="data_management",
            concurrency_safe=False,
            read_only=False,
        ),
        ToolDefinition(
            name="shared_file_list",
            description="列出公司共用文件庫中的檔案。支援按資料夾、標籤、上傳者篩選。",
            parameters={
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "資料夾路徑篩選"},
                    "tags": {"type": "string", "description": "標籤篩選，逗號分隔"},
                    "uploaded_by": {"type": "string", "description": "上傳者角色 ID 篩選"},
                    "limit": {"type": "integer", "description": "最大回傳數量", "default": 50},
                },
            },
            func=shared_file_list,
            category="data_management",
            concurrency_safe=True,
            read_only=True,
        ),
        ToolDefinition(
            name="shared_file_read",
            description="讀取共用文件庫中的檔案。文字檔案回傳內容，二進位檔案回傳元資訊。",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "檔案 ID"},
                    "max_chars": {"type": "integer", "description": "文字內容最大字元數", "default": 10000},
                },
                "required": ["file_id"],
            },
            func=shared_file_read,
            category="data_management",
            concurrency_safe=True,
            read_only=True,
        ),
        ToolDefinition(
            name="shared_file_search",
            description="搜尋公司共用文件庫。依關鍵字匹配檔名、描述和標籤。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜尋關鍵字"},
                    "limit": {"type": "integer", "description": "最大回傳數量", "default": 30},
                },
                "required": ["query"],
            },
            func=shared_file_search,
            category="data_management",
            concurrency_safe=True,
            read_only=True,
        ),
        ToolDefinition(
            name="shared_file_delete",
            description="從公司共用文件庫中刪除檔案（同時刪除磁碟檔案和索引記錄）。",
            parameters={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "要刪除的檔案 ID"},
                },
                "required": ["file_id"],
            },
            func=shared_file_delete,
            category="data_management",
            concurrency_safe=False,
            read_only=False,
            requires_confirmation=True,
        ),
    ]
