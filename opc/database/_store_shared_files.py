"""SharedFileStoreMixin — 共用文件庫 CRUD 相關方法。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from opc.core.models import SharedFileRecord
from opc.database._utils import _json_dumps, _json_loads

if TYPE_CHECKING:
    from opc.database.store import OPCStore


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SharedFileStoreMixin:
    """Mixin providing 共用文件庫 CRUD 相關方法 for OPCStore."""

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    async def _create_shared_files_table(self) -> None:
        """建立 shared_files 表（在 _create_tables 中調用）。"""
        assert self._db is not None
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS shared_files (
                file_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                folder TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '[]',
                description TEXT NOT NULL DEFAULT '',
                uploaded_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shared_files_folder ON shared_files(folder);
            CREATE INDEX IF NOT EXISTS idx_shared_files_uploaded_by ON shared_files(uploaded_by);
        """)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def save_shared_file(self, record: SharedFileRecord) -> None:
        """插入或更新一筆共用文件記錄。"""
        assert self._db is not None
        record.updated_at = datetime.now(timezone.utc)
        await self._db.execute(
            """INSERT OR REPLACE INTO shared_files
               (file_id, filename, folder, mime_type, size_bytes, tags, description, uploaded_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.file_id,
                record.filename,
                record.folder,
                record.mime_type,
                record.size_bytes,
                _json_dumps(record.tags),
                record.description,
                record.uploaded_by,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )

    async def get_shared_file(self, file_id: str) -> SharedFileRecord | None:
        """依 file_id 取得單筆記錄。"""
        assert self._db is not None
        fid = str(file_id or "").strip()
        if not fid:
            return None
        async with self._db.execute(
            "SELECT * FROM shared_files WHERE file_id = ?", (fid,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_shared_file(row, cursor.description)

    async def delete_shared_file(self, file_id: str) -> bool:
        """刪除一筆共用文件記錄，回傳是否成功刪除。"""
        assert self._db is not None
        fid = str(file_id or "").strip()
        if not fid:
            return False
        cursor = await self._db.execute(
            "DELETE FROM shared_files WHERE file_id = ?", (fid,)
        )
        return cursor.rowcount > 0

    async def list_shared_files(
        self,
        *,
        folder: str | None = None,
        uploaded_by: str | None = None,
        tags: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SharedFileRecord]:
        """列出共用文件，支援資料夾、上傳者、標籤篩選。"""
        assert self._db is not None
        clauses: list[str] = []
        params: list[Any] = []

        if folder is not None:
            clauses.append("folder = ?")
            params.append(folder)
        if uploaded_by:
            clauses.append("uploaded_by = ?")
            params.append(uploaded_by)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM shared_files {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with self._db.execute(sql, tuple(params)) as cursor:
            rows = await cursor.fetchall()
            desc = cursor.description

        results = [self._row_to_shared_file(row, desc) for row in rows]

        # 標籤篩選在記憶體中執行（tags 為 JSON 欄位）
        if tags:
            tag_set = set(tags)
            results = [r for r in results if tag_set & set(r.tags)]

        return results

    async def search_shared_files(self, query: str, *, limit: int = 50) -> list[SharedFileRecord]:
        """依關鍵字搜尋檔名、描述、標籤。"""
        assert self._db is not None
        q = str(query or "").strip()
        if not q:
            return []
        pattern = f"%{q}%"
        sql = """
            SELECT * FROM shared_files
            WHERE filename LIKE ? OR description LIKE ? OR tags LIKE ?
            ORDER BY updated_at DESC
            LIMIT ?
        """
        async with self._db.execute(sql, (pattern, pattern, pattern, limit)) as cursor:
            rows = await cursor.fetchall()
            desc = cursor.description
        return [self._row_to_shared_file(row, desc) for row in rows]

    async def list_shared_file_folders(self) -> list[str]:
        """列出所有不重複的資料夾路徑。"""
        assert self._db is not None
        async with self._db.execute(
            "SELECT DISTINCT folder FROM shared_files ORDER BY folder"
        ) as cursor:
            rows = await cursor.fetchall()
        return [str(row[0] or "") for row in rows]

    async def count_shared_files(self, *, folder: str | None = None) -> int:
        """計算共用文件數量。"""
        assert self._db is not None
        if folder is not None:
            async with self._db.execute(
                "SELECT COUNT(*) FROM shared_files WHERE folder = ?", (folder,)
            ) as cursor:
                row = await cursor.fetchone()
        else:
            async with self._db.execute("SELECT COUNT(*) FROM shared_files") as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_shared_file(row: Any, description: Any) -> SharedFileRecord:
        """將資料庫行轉換為 SharedFileRecord。"""
        columns = [col[0] for col in description]
        data = dict(zip(columns, row))
        return SharedFileRecord(
            file_id=str(data.get("file_id", "")),
            filename=str(data.get("filename", "")),
            folder=str(data.get("folder", "")),
            mime_type=str(data.get("mime_type", "")),
            size_bytes=int(data.get("size_bytes", 0)),
            tags=_json_loads(data.get("tags"), []),
            description=str(data.get("description", "")),
            uploaded_by=str(data.get("uploaded_by", "")),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.now(timezone.utc),
        )
