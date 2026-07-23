"""Company data export service for Office UI."""

from __future__ import annotations

from typing import Any

from opc.core.data_export import CompanyDataExporter

from .context import OfficeServiceContext
from .models import ServiceError, ServiceResult


class DataExportService:
    """Service layer for company data aggregation and export.

    Provides dashboard summary, task querying, and data export
    capabilities for the Office UI frontend.
    """

    def __init__(self, context: OfficeServiceContext) -> None:
        self.context = context

    def _get_store(self) -> Any:
        """Get the OPCStore from the engine."""
        engine = self.context.root_engine
        store = getattr(engine, "store", None)
        if not store:
            raise ServiceError("store_not_ready", "store_not_ready")
        return store

    def _get_exporter(self) -> CompanyDataExporter:
        """Create a CompanyDataExporter instance."""
        return CompanyDataExporter(self._get_store())

    async def get_summary(self, *, project_id: str = "") -> ServiceResult:
        """Get dashboard summary statistics."""
        exporter = self._get_exporter()
        summary = await exporter.get_summary(project_id=project_id)
        return ServiceResult({
            "project_id": project_id or "(all)",
            "summary": summary,
        })

    async def query_tasks(
        self,
        *,
        project_id: str = "",
        status: str = "",
        assigned_to: str = "",
        limit: int = 100,
    ) -> ServiceResult:
        """Query tasks with filters."""
        exporter = self._get_exporter()
        tasks = await exporter.query_tasks(
            project_id=project_id,
            status=status or None,
            assigned_to=assigned_to or None,
            limit=limit,
        )
        return ServiceResult({
            "tasks": tasks,
            "count": len(tasks),
            "filters": {
                "project_id": project_id or "(all)",
                "status": status or "(all)",
                "assigned_to": assigned_to or "(all)",
            },
        })

    async def export_snapshot(
        self,
        *,
        project_id: str = "",
        format: str = "json",
        include_work_items: bool = True,
    ) -> ServiceResult:
        """Export a full data snapshot."""
        exporter = self._get_exporter()
        snapshot = await exporter.build_snapshot(
            project_id=project_id,
            include_work_items=include_work_items,
        )

        if format.lower() == "csv":
            content = exporter.tasks_to_csv(snapshot)
            content_type = "text/csv"
        else:
            content = exporter.to_json(snapshot)
            content_type = "application/json"

        return ServiceResult({
            "format": format,
            "content_type": content_type,
            "content": content,
            "exported_at": snapshot.exported_at.isoformat(),
            "summary": snapshot.summary,
            "stats": {
                "total_tasks": len(snapshot.tasks),
                "total_work_items": len(snapshot.work_items),
                "total_delegation_runs": len(snapshot.delegation_runs),
            },
        })

    async def list_exports(self, *, limit: int = 50) -> ServiceResult:
        """List historical export files from the shared file library's exports folder."""
        store = self._get_store()
        records = await store.list_shared_files(folder="exports", limit=limit)
        exports = [
            {
                "file_id": r.file_id,
                "filename": r.filename,
                "mime_type": r.mime_type,
                "size_bytes": r.size_bytes,
                "description": r.description,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in records
        ]
        return ServiceResult({"exports": exports, "count": len(exports)})
