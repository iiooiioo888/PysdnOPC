"""SessionStoreMixin — 工作階段記錄/訊息/記憶相關方法。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from opc.core.models import (
    AgentCompactionRecord,
    AgentMemorySnapshotRecord,
    SessionCompactionRecord,
    SessionLinkRecord,
    SessionMemorySnapshotRecord,
    SessionMessageRecord,
    SessionPartRecord,
    SessionRecord,
)
from opc.core.transcript_visibility import (
    normalize_transcript_detail_level,
    transcript_visibility_sql,
)
from opc.database._utils import _json_dumps, _json_loads

if TYPE_CHECKING:
    from opc.database.store import OPCStore


class SessionStoreMixin:
    """Mixin providing 工作階段記錄/訊息/記憶相關方法 for OPCStore."""

    async def save_session(self, session: SessionRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO sessions
            (session_id, project_id, parent_session_id, title, mode, status, summary, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.session_id,
                session.project_id,
                session.parent_session_id,
                session.title,
                session.mode,
                session.status,
                session.summary,
                _json_dumps(session.metadata),
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_session(self, row: Any, description: Any) -> SessionRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionRecord(
            session_id=data["session_id"],
            project_id=data["project_id"],
            parent_session_id=data["parent_session_id"],
            title=data["title"],
            mode=data["mode"],
            status=data["status"],
            summary=data.get("summary") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def get_session(self, session_id: str) -> SessionRecord | None:
        assert self._db
        async with self._db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_session(row, cursor.description)

    async def list_sessions(
        self,
        project_id: str = "default",
        parent_session_id: str | None = None,
        limit: int = 50,
    ) -> list[SessionRecord]:
        assert self._db
        query = "SELECT * FROM sessions WHERE project_id = ?"
        params: list[Any] = [project_id]
        if parent_session_id is None:
            query += " AND parent_session_id IS NULL"
        else:
            query += " AND parent_session_id = ?"
            params.append(parent_session_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_session(row, cursor.description) for row in rows]

    async def touch_session(self, session_id: str) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (datetime.now().isoformat(), session_id),
        )
        await self._db.commit()

    async def save_session_message(self, message: SessionMessageRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_messages
            (message_id, session_id, role, task_id, agent_id, parent_message_id, summary_flag, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.message_id,
                message.session_id,
                message.role,
                message.task_id,
                message.agent_id,
                message.parent_message_id,
                int(message.summary_flag),
                _json_dumps(message.metadata),
                message.created_at.isoformat(),
            ),
        )
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (message.created_at.isoformat(), message.session_id),
        )
        await self._db.commit()

    def _row_to_session_message(self, row: Any, description: Any) -> SessionMessageRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionMessageRecord(
            message_id=data["message_id"],
            session_id=data["session_id"],
            role=data["role"],
            task_id=data["task_id"],
            agent_id=data["agent_id"],
            parent_message_id=data["parent_message_id"],
            summary_flag=bool(data["summary_flag"]),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_session_message(self, message_id: str) -> SessionMessageRecord | None:
        assert self._db
        async with self._db.execute("SELECT * FROM session_messages WHERE message_id = ?", (message_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_session_message(row, cursor.description)

    async def list_session_messages(self, session_id: str) -> list[SessionMessageRecord]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_session_message(row, cursor.description) for row in rows]

    async def get_session_transcript_page(
        self,
        session_id: str,
        *,
        limit: int = 200,
        before_created_at: datetime | None = None,
        before_message_id: str | None = None,
        detail_level: str = "summary",
    ) -> dict[str, Any]:
        assert self._db
        normalized_limit = max(1, min(int(limit), 500))
        normalized_detail_level = normalize_transcript_detail_level(detail_level)
        visibility_sql, visibility_params = transcript_visibility_sql(
            detail_level=normalized_detail_level,
        )
        query = (
            "SELECT * FROM session_messages "
            "WHERE session_id = ? AND summary_flag = 0 "
        )
        params: list[Any] = [session_id]
        query += visibility_sql
        params.extend(visibility_params)
        normalized_before_id = str(before_message_id or "").strip()
        if before_created_at is not None:
            before_iso = before_created_at.isoformat()
            if normalized_before_id:
                query += " AND (created_at < ? OR (created_at = ? AND message_id < ?))"
                params.extend([before_iso, before_iso, normalized_before_id])
            else:
                query += " AND created_at < ?"
                params.append(before_iso)
        query += " ORDER BY created_at DESC, message_id DESC LIMIT ?"
        params.append(normalized_limit + 1)

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            description = cursor.description

        has_more = len(rows) > normalized_limit
        visible_rows = rows[:normalized_limit]
        messages_desc = [self._row_to_session_message(row, description) for row in visible_rows]
        messages = list(reversed(messages_desc))
        parts_by_message: dict[str, list[SessionPartRecord]] = {}
        if messages:
            placeholders = ",".join("?" for _ in messages)
            part_params = [session_id, *[message.message_id for message in messages]]
            async with self._db.execute(
                f"SELECT * FROM session_parts WHERE session_id = ? AND message_id IN ({placeholders}) "
                "ORDER BY created_at ASC",
                part_params,
            ) as cursor:
                part_rows = await cursor.fetchall()
                part_description = cursor.description
            for row in part_rows:
                part = self._row_to_session_part(row, part_description)
                parts_by_message.setdefault(part.message_id, []).append(part)

        count_query = (
            "SELECT COUNT(*) FROM session_messages "
            "WHERE session_id = ? AND summary_flag = 0 "
        )
        count_params: list[Any] = [session_id]
        count_query += visibility_sql
        count_params.extend(visibility_params)
        async with self._db.execute(count_query, count_params) as cursor:
            row = await cursor.fetchone()
        total_count = int(row[0] or 0) if row else 0

        return {
            "messages": [
                {
                    "message": message,
                    "parts": parts_by_message.get(message.message_id, []),
                }
                for message in messages
            ],
            "has_more": has_more,
            "total_count": total_count,
        }

    async def save_session_part(self, part: SessionPartRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_parts
            (part_id, message_id, session_id, part_type, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                part.part_id,
                part.message_id,
                part.session_id,
                part.part_type,
                _json_dumps(part.payload),
                part.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_session_part(self, row: Any, description: Any) -> SessionPartRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionPartRecord(
            part_id=data["part_id"],
            message_id=data["message_id"],
            session_id=data["session_id"],
            part_type=data["part_type"],
            payload=_json_loads(data.get("payload"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def list_session_parts(self, session_id: str, message_id: str | None = None) -> list[SessionPartRecord]:
        assert self._db
        query = "SELECT * FROM session_parts WHERE session_id = ?"
        params: list[Any] = [session_id]
        if message_id:
            query += " AND message_id = ?"
            params.append(message_id)
        query += " ORDER BY created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_session_part(row, cursor.description) for row in rows]

    async def get_session_transcript(self, session_id: str) -> list[dict[str, Any]]:
        from dataclasses import asdict, is_dataclass
        from datetime import datetime as dt_type

        messages = await self.list_session_messages(session_id)
        parts = await self.list_session_parts(session_id)
        parts_by_message: dict[str, list[SessionPartRecord]] = {}
        for part in parts:
            parts_by_message.setdefault(part.message_id, []).append(part)

        def _to_dict(obj: Any) -> Any:
            if is_dataclass(obj) and not isinstance(obj, type):
                d = asdict(obj)
                # Convert datetime fields to ISO strings
                for k, v in d.items():
                    if isinstance(v, dt_type):
                        d[k] = v.isoformat()
                return d
            return obj

        return [
            {
                "message": _to_dict(message),
                "parts": [_to_dict(p) for p in parts_by_message.get(message.message_id, [])],
            }
            for message in messages
        ]

    async def save_session_compaction(self, record: SessionCompactionRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_compactions
            (compaction_id, session_id, compaction_message_id, source_boundary_message_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                record.compaction_id,
                record.session_id,
                record.compaction_message_id,
                record.source_boundary_message_id,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_session_compaction(self, row: Any, description: Any) -> SessionCompactionRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionCompactionRecord(
            compaction_id=data["compaction_id"],
            session_id=data["session_id"],
            compaction_message_id=data["compaction_message_id"],
            source_boundary_message_id=data["source_boundary_message_id"],
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_latest_session_compaction(self, session_id: str) -> SessionCompactionRecord | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM session_compactions WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_session_compaction(row, cursor.description)

    async def save_session_memory_snapshot(self, record: SessionMemorySnapshotRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_memory_snapshots
            (snapshot_id, project_id, session_id, summary_message_id, source_boundary_message_id,
             summary_text, memory_text, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.snapshot_id,
                record.project_id,
                record.session_id,
                record.summary_message_id,
                record.source_boundary_message_id,
                record.summary_text,
                record.memory_text,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_session_memory_snapshot(self, row: Any, description: Any) -> SessionMemorySnapshotRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return SessionMemorySnapshotRecord(
            snapshot_id=data["snapshot_id"],
            project_id=data["project_id"],
            session_id=data["session_id"],
            summary_message_id=data["summary_message_id"],
            source_boundary_message_id=data["source_boundary_message_id"],
            summary_text=data.get("summary_text") or "",
            memory_text=data.get("memory_text") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def get_latest_session_memory_snapshot(self, session_id: str) -> SessionMemorySnapshotRecord | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM session_memory_snapshots WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_session_memory_snapshot(row, cursor.description)

    async def save_agent_compaction(self, record: AgentCompactionRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO agent_compactions
            (compaction_id, project_id, session_id, employee_id, role_id, compaction_message_id,
             source_boundary_message_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.compaction_id,
                record.project_id,
                record.session_id,
                record.employee_id,
                record.role_id,
                record.compaction_message_id,
                record.source_boundary_message_id,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_agent_compaction(self, row: Any, description: Any) -> AgentCompactionRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return AgentCompactionRecord(
            compaction_id=data["compaction_id"],
            project_id=data["project_id"],
            session_id=data["session_id"],
            employee_id=data["employee_id"],
            role_id=data.get("role_id") or "",
            compaction_message_id=data["compaction_message_id"],
            source_boundary_message_id=data["source_boundary_message_id"],
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_latest_agent_compaction(
        self,
        *,
        project_id: str,
        session_id: str,
        employee_id: str,
    ) -> AgentCompactionRecord | None:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM agent_compactions
            WHERE project_id = ? AND session_id = ? AND employee_id = ?
            ORDER BY created_at DESC LIMIT 1""",
            (project_id, session_id, employee_id),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_agent_compaction(row, cursor.description)

    async def save_agent_memory_snapshot(self, record: AgentMemorySnapshotRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO agent_memory_snapshots
            (snapshot_id, project_id, session_id, employee_id, role_id, memory_scope, memory_kind,
             summary_message_id, source_boundary_message_id, summary_text, memory_text,
             metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.snapshot_id,
                record.project_id,
                record.session_id,
                record.employee_id,
                record.role_id,
                record.memory_scope,
                record.memory_kind,
                record.summary_message_id,
                record.source_boundary_message_id,
                record.summary_text,
                record.memory_text,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_agent_memory_snapshot(self, row: Any, description: Any) -> AgentMemorySnapshotRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return AgentMemorySnapshotRecord(
            snapshot_id=data["snapshot_id"],
            project_id=data["project_id"],
            session_id=data["session_id"],
            employee_id=data["employee_id"],
            role_id=data.get("role_id") or "",
            memory_scope=data.get("memory_scope") or "session",
            memory_kind=data.get("memory_kind") or "process",
            summary_message_id=data["summary_message_id"],
            source_boundary_message_id=data["source_boundary_message_id"],
            summary_text=data.get("summary_text") or "",
            memory_text=data.get("memory_text") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def get_agent_memory_snapshot(
        self,
        *,
        project_id: str,
        session_id: str | None = None,
        employee_id: str,
        memory_kind: str | None = None,
        memory_scope: str | None = None,
    ) -> AgentMemorySnapshotRecord | None:
        assert self._db
        query = (
            "SELECT * FROM agent_memory_snapshots "
            "WHERE project_id = ? AND employee_id = ?"
        )
        params: list[Any] = [project_id, employee_id]
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        if memory_scope:
            query += " AND memory_scope = ?"
            params.append(memory_scope)
        if memory_kind:
            query += " AND memory_kind = ?"
            params.append(memory_kind)
        query += " ORDER BY updated_at DESC LIMIT 1"
        async with self._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_agent_memory_snapshot(row, cursor.description)

    async def list_agent_memory_snapshots(
        self,
        *,
        project_id: str,
        session_id: str | None = None,
        employee_id: str,
        memory_kind: str | None = None,
        memory_scope: str | None = None,
    ) -> list[AgentMemorySnapshotRecord]:
        assert self._db
        query = (
            "SELECT * FROM agent_memory_snapshots "
            "WHERE project_id = ? AND employee_id = ?"
        )
        params: list[Any] = [project_id, employee_id]
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        if memory_scope:
            query += " AND memory_scope = ?"
            params.append(memory_scope)
        if memory_kind:
            query += " AND memory_kind = ?"
            params.append(memory_kind)
        query += " ORDER BY updated_at DESC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_agent_memory_snapshot(row, cursor.description) for row in rows]

    async def delete_agent_memory_snapshots(
        self,
        *,
        project_id: str,
        session_id: str | None = None,
        employee_id: str,
        memory_kind: str | None = None,
        memory_scope: str | None = None,
    ) -> None:
        assert self._db
        query = (
            "DELETE FROM agent_memory_snapshots "
            "WHERE project_id = ? AND employee_id = ?"
        )
        params: list[Any] = [project_id, employee_id]
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        if memory_scope:
            query += " AND memory_scope = ?"
            params.append(memory_scope)
        if memory_kind:
            query += " AND memory_kind = ?"
            params.append(memory_kind)
        await self._db.execute(query, params)
        await self._db.commit()

    async def save_session_link(self, link: SessionLinkRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO session_links
            (link_id, project_id, session_id, linked_session_id, task_id, link_type, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                link.link_id,
                link.project_id,
                link.session_id,
                link.linked_session_id,
                link.task_id,
                link.link_type,
                _json_dumps(link.metadata),
                link.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_session_links(
        self,
        session_id: str,
        link_type: str | None = None,
        limit: int = 50,
    ) -> list[SessionLinkRecord]:
        assert self._db
        query = "SELECT * FROM session_links WHERE session_id = ?"
        params: list[Any] = [session_id]
        if link_type:
            query += " AND link_type = ?"
            params.append(link_type)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                SessionLinkRecord(
                    link_id=data["link_id"],
                    project_id=data["project_id"],
                    session_id=data["session_id"],
                    linked_session_id=data["linked_session_id"],
                    task_id=data["task_id"],
                    link_type=data["link_type"],
                    metadata=_json_loads(data.get("metadata"), {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]
