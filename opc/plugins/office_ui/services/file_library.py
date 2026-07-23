"""Company shared file library service for Office UI."""

from __future__ import annotations

from typing import Any

from opc.core.shared_file_store import SharedFileStore

from .context import OfficeServiceContext
from .models import ServiceError, ServiceResult


class FileLibraryService:
    """Service layer for the company shared file library.

    Provides file browsing, upload, download, delete, and search
    capabilities for the Office UI frontend.
    """

    def __init__(self, context: OfficeServiceContext) -> None:
        self.context = context
        self._file_store: SharedFileStore | None = None

    def _get_file_store(self) -> SharedFileStore:
        """Lazily create the SharedFileStore from engine's opc_home."""
        if self._file_store is None:
            engine = self.context.root_engine
            opc_home = getattr(engine, "opc_home", None)
            if not opc_home:
                raise ServiceError("not_configured", "opc_home not available")
            self._file_store = SharedFileStore(opc_home)
        return self._file_store

    def _get_store(self) -> Any:
        """Get the OPCStore from the engine."""
        engine = self.context.root_engine
        store = getattr(engine, "store", None)
        if not store:
            raise ServiceError("store_not_ready", "store_not_ready")
        return store

    async def list_folders(self) -> ServiceResult:
        """List all folders in the shared file library."""
        store = self._get_store()
        folders = await store.list_shared_file_folders()
        file_store = self._get_file_store()
        disk_folders = file_store.list_folders_on_disk()
        # Merge DB folders and disk folders
        all_folders = sorted(set(folders) | set(disk_folders))
        return ServiceResult({"folders": all_folders})

    async def list_files(
        self,
        *,
        folder: str | None = None,
        tags: str = "",
        uploaded_by: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> ServiceResult:
        """List files with optional filters."""
        store = self._get_store()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        records = await store.list_shared_files(
            folder=folder,
            uploaded_by=uploaded_by or None,
            tags=tag_list,
            limit=limit,
            offset=offset,
        )
        total = await store.count_shared_files(folder=folder)
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
                "updated_at": r.updated_at.isoformat() if r.updated_at else "",
            }
            for r in records
        ]
        return ServiceResult({
            "files": files,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    async def upload(
        self,
        *,
        filename: str,
        content_base64: str,
        folder: str = "",
        tags: str = "",
        description: str = "",
        uploaded_by: str = "",
    ) -> ServiceResult:
        """Upload a file to the shared library."""
        store = self._get_store()
        file_store = self._get_file_store()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

        record = await file_store.upload_from_base64(
            filename=filename,
            b64_data=content_base64,
            folder=folder,
            tags=tag_list,
            description=description,
            uploaded_by=uploaded_by,
        )
        await store.save_shared_file(record)
        return ServiceResult({
            "file_id": record.file_id,
            "filename": record.filename,
            "folder": record.folder,
            "size_bytes": record.size_bytes,
            "mime_type": record.mime_type,
        })

    async def download(self, *, file_id: str) -> ServiceResult:
        """Download a file (returns base64 content)."""
        store = self._get_store()
        file_store = self._get_file_store()
        record = await store.get_shared_file(file_id)
        if not record:
            raise ServiceError("not_found", f"File not found: {file_id}")
        if not file_store.file_exists_on_disk(record):
            raise ServiceError("file_missing", f"File missing on disk: {record.filename}")

        b64_content = file_store.read_as_base64(record)
        return ServiceResult({
            "file_id": record.file_id,
            "filename": record.filename,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "content_base64": b64_content,
        })

    async def delete(self, *, file_id: str) -> ServiceResult:
        """Delete a file from the shared library."""
        store = self._get_store()
        file_store = self._get_file_store()
        record = await store.get_shared_file(file_id)
        if not record:
            raise ServiceError("not_found", f"File not found: {file_id}")

        file_store.delete_from_disk(record)
        await store.delete_shared_file(file_id)
        return ServiceResult({"deleted": True, "file_id": file_id, "filename": record.filename})

    async def search(self, *, query: str, limit: int = 50) -> ServiceResult:
        """Search files by keyword."""
        store = self._get_store()
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
        return ServiceResult({"query": query, "count": len(files), "files": files})
