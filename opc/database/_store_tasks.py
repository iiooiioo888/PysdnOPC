"""TaskStoreMixin — 任務 CRUD 相關方法。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from opc.core.models import (
    Task,
    TaskStatus,
)
from opc.layer2_organization.work_item_identity import (
    WORK_ITEM_PROJECTION_ID_KEY,
    WORK_ITEM_TURN_TYPE_KEY,
    migrate_work_item_projection_metadata,
)
from opc.layer2_organization.work_item_links import (
    linked_work_item_id_for_task,
    set_linked_work_item_id,
)
from opc.database._utils import _json_dumps, _json_loads

if TYPE_CHECKING:
    from opc.database.store import OPCStore


class TaskStoreMixin:
    """Mixin providing 任務 CRUD 相關方法 for OPCStore."""

    async def _task_exists(self, task_id: str) -> bool:
        assert self._db is not None
        tid = str(task_id or "").strip()
        if not tid:
            return False
        async with self._db.execute("SELECT 1 FROM tasks WHERE id=? LIMIT 1", (tid,)) as cursor:
            return await cursor.fetchone() is not None

    async def _get_task_unhydrated(self, task_id: str) -> Task | None:
        assert self._db is not None
        tid = str(task_id or "").strip()
        if not tid:
            return None
        async with self._db.execute("SELECT * FROM tasks WHERE id = ?", (tid,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row, cursor.description)

    async def _task_status_for_id(self, task_id: str) -> str:
        assert self._db is not None
        tid = str(task_id or "").strip()
        if not tid:
            return ""
        async with self._db.execute(
            "SELECT status FROM tasks WHERE id=? LIMIT 1",
            (tid,),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0] or "").strip().lower() if row else ""

    @staticmethod
    def _terminal_task_statuses() -> set[str]:
        return {
            TaskStatus.DONE.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        }

    async def _delete_company_runtime_artifacts_for_task(
        self,
        task_id: str,
        session_id: str | None,
        *,
        shared_session: bool,
    ) -> None:
        """Delete company-mode and runtime rows tied to one task/session.

        Top-level chat deletion removes the full delegation run for that
        session. Child work-item deletion removes only the linked work item and
        its runtime traces so a sibling/parent run is not destroyed.
        """
        if not self._db:
            return

        clean_task_id = str(task_id or "").strip()
        clean_session_id = str(session_id or "").strip()
        if not clean_task_id and not clean_session_id:
            return

        task_row: dict[str, Any] = {}
        if clean_task_id:
            async with self._db.execute(
                "SELECT id, session_id, parent_session_id, project_id, metadata FROM tasks WHERE id = ?",
                (clean_task_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    cols = [description[0] for description in cursor.description]
                    task_row = dict(zip(cols, row))

        parent_session_id = str(task_row.get("parent_session_id") or "").strip()
        if not clean_session_id and task_row:
            clean_session_id = str(task_row.get("session_id") or parent_session_id or "").strip()
        is_primary_session_task = bool(clean_session_id and not shared_session and not parent_session_id)

        task_ids: set[str] = set()
        if clean_task_id:
            task_ids.add(clean_task_id)
        if clean_session_id and not shared_session:
            task_ids.update(
                await self._fetch_text_column(
                    "SELECT id FROM tasks WHERE session_id = ? OR parent_session_id = ?",
                    (clean_session_id, clean_session_id),
                )
            )
        session_ids = {clean_session_id} if clean_session_id and not shared_session else set()
        full_run_ids: set[str] = set()
        work_item_ids: set[str] = set()
        role_runtime_session_ids: set[str] = set()
        runtime_session_ids: set[str] = set()

        if is_primary_session_task:
            full_run_ids.update(
                await self._fetch_text_column(
                    "SELECT run_id FROM delegation_runs WHERE session_id = ?",
                    (clean_session_id,),
                )
            )

        if task_ids:
            work_item_ids.update(
                await self._fetch_text_column_where_in(
                    "work_item_runtime_links",
                    "work_item_id",
                    "runtime_task_id",
                    task_ids,
                )
            )

        metadata_paths = (
            "$.task_id",
            "$.runtime_task_id",
            "$.execution_task_id",
            "$.origin_task_id",
            "$.worker_task_id",
        )
        if task_ids:
            for path in metadata_paths:
                for chunk in self._chunked_ids(task_ids):
                    placeholders = ", ".join("?" for _ in chunk)
                    work_item_ids.update(
                        await self._fetch_text_column(
                            f"""
                            SELECT work_item_id FROM delegation_work_items
                            WHERE json_valid(metadata)
                              AND json_extract(metadata, ?) IN ({placeholders})
                            """,
                            tuple([path, *chunk]),
                        )
                    )

        if session_ids:
            for chunk in self._chunked_ids(session_ids):
                placeholders = ", ".join("?" for _ in chunk)
                work_item_ids.update(
                    await self._fetch_text_column(
                        f"""
                        SELECT work_item_id FROM delegation_work_items
                        WHERE json_valid(metadata)
                          AND (
                            json_extract(metadata, '$.session_id') IN ({placeholders})
                            OR json_extract(metadata, '$.parent_session_id') IN ({placeholders})
                            OR json_extract(metadata, '$.session_scope_id') IN ({placeholders})
                          )
                        """,
                        tuple([*chunk, *chunk, *chunk]),
                    )
                )

        if full_run_ids:
            work_item_ids.update(
                await self._fetch_text_column_where_in(
                    "delegation_work_items",
                    "work_item_id",
                    "run_id",
                    full_run_ids,
                )
            )

        if work_item_ids:
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "delegation_work_items",
                    "role_runtime_session_id",
                    "work_item_id",
                    work_item_ids,
                    extra_where="COALESCE(role_runtime_session_id, '') != ''",
                )
            )
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "delegation_work_items",
                    "claimed_by_role_runtime_session_id",
                    "work_item_id",
                    work_item_ids,
                    extra_where="COALESCE(claimed_by_role_runtime_session_id, '') != ''",
                )
            )

        if full_run_ids:
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "role_runtime_sessions",
                    "role_session_id",
                    "run_id",
                    full_run_ids,
                )
            )
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "delegation_role_sessions",
                    "role_session_id",
                    "run_id",
                    full_run_ids,
                )
            )
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "seat_states",
                    "role_runtime_session_id",
                    "run_id",
                    full_run_ids,
                    extra_where="COALESCE(role_runtime_session_id, '') != ''",
                )
            )
            role_runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "seat_states",
                    "member_session_id",
                    "run_id",
                    full_run_ids,
                    extra_where="COALESCE(member_session_id, '') != ''",
                )
            )

        if task_ids:
            runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "runtime_sessions",
                    "runtime_session_id",
                    "task_id",
                    task_ids,
                )
            )
        if session_ids:
            runtime_session_ids.update(
                await self._fetch_text_column_where_in(
                    "runtime_sessions",
                    "runtime_session_id",
                    "session_id",
                    session_ids,
                )
            )
        if full_run_ids:
            runtime_session_ids.update(role_runtime_session_ids)
            # Some company runtime events are scoped directly to the run id
            # rather than a role/runtime session id.
            runtime_session_ids.update(full_run_ids)
        runtime_lookup_ids = task_ids | session_ids | full_run_ids | work_item_ids
        if full_run_ids:
            runtime_lookup_ids.update(role_runtime_session_ids)
        runtime_session_ids.update(
            await self._fetch_text_column_where_text_contains(
                "runtime_sessions",
                "runtime_session_id",
                "runtime_session_id",
                runtime_lookup_ids,
            )
        )
        runtime_session_ids.update(
            await self._fetch_text_column_where_text_contains(
                "runtime_sessions",
                "runtime_session_id",
                "metadata",
                runtime_lookup_ids,
            )
        )

        task_tables = (
            "runtime_transcript_entries",
            "runtime_tool_calls",
            "runtime_tool_results",
            "runtime_subagent_runs",
            "runtime_worktree_sessions",
            "runtime_compaction_boundaries",
        )
        for table in task_tables:
            await self._delete_where_in(table, "task_id", task_ids)
            if session_ids and table in {
                "runtime_transcript_entries",
                "runtime_tool_calls",
                "runtime_tool_results",
            }:
                await self._delete_where_in(table, "session_id", session_ids)

        if runtime_session_ids:
            for table in (
                "runtime_events",
                "runtime_transcript_entries",
                "runtime_tool_calls",
                "runtime_tool_results",
                "runtime_permission_grants",
                "runtime_subagent_runs",
                "runtime_worktree_sessions",
                "runtime_compaction_boundaries",
                "runtime_sessions",
            ):
                await self._delete_where_in(table, "runtime_session_id", runtime_session_ids)

        await self._delete_where_in("execution_checkpoints", "task_id", task_ids)
        await self._delete_where_in("execution_checkpoints", "session_id", session_ids)
        await self._delete_by_json_path_or_text_ids(
            "execution_checkpoints",
            "payload",
            ("$.task_id", "$.session_id", "$.run_id", "$.work_item_id"),
            task_ids | session_ids | full_run_ids | work_item_ids,
        )
        await self._delete_where_in("external_sessions", "task_id", task_ids)
        await self._delete_where_in("external_sessions", "opc_session_id", session_ids)
        await self._delete_where_in("external_sessions", "session_id", session_ids)
        await self._delete_where_text_contains(
            "external_sessions",
            "metadata",
            task_ids | session_ids | full_run_ids | work_item_ids | runtime_session_ids,
        )
        if await self._table_exists("external_sessions_v2"):
            await self._delete_where_in("external_sessions_v2", "task_id", task_ids)
            await self._delete_where_in("external_sessions_v2", "opc_session_id", session_ids)
            await self._delete_where_in("external_sessions_v2", "session_id", session_ids)
            await self._delete_where_text_contains(
                "external_sessions_v2",
                "metadata",
                task_ids | session_ids | full_run_ids | work_item_ids | runtime_session_ids,
            )

        if clean_task_id:
            await self._db.execute(
                """
                DELETE FROM events
                WHERE json_valid(payload)
                  AND (
                    json_extract(payload, '$.task_id') = ?
                    OR json_extract(payload, '$.runtime_task_id') = ?
                    OR json_extract(payload, '$.execution_task_id') = ?
                    OR json_extract(payload, '$.escalation_id') LIKE ?
                  )
                """,
                (clean_task_id, clean_task_id, clean_task_id, f"esc_{clean_task_id}_%"),
            )
        for path in (
            "$.task_id",
            "$.runtime_task_id",
            "$.execution_task_id",
            "$.origin_task_id",
            "$.worker_task_id",
        ):
            await self._delete_events_by_payload_ids(path, task_ids)
        for path in ("$.session_id", "$.parent_session_id", "$.opc_session_id"):
            await self._delete_events_by_payload_ids(path, session_ids)
        await self._delete_events_by_payload_ids("$.run_id", full_run_ids)
        await self._delete_events_by_payload_ids("$.work_item_id", work_item_ids)
        await self._delete_events_by_payload_ids("$.runtime_session_id", runtime_session_ids)
        role_event_session_ids = set(runtime_session_ids)
        if full_run_ids:
            role_event_session_ids.update(role_runtime_session_ids)
        await self._delete_events_by_payload_ids("$.role_runtime_session_id", role_event_session_ids)
        await self._delete_events_by_payload_ids("$.member_session_id", role_event_session_ids)
        event_scope_ids = task_ids | session_ids | full_run_ids | work_item_ids | runtime_session_ids
        if full_run_ids:
            event_scope_ids.update(role_runtime_session_ids)
        await self._delete_where_text_contains(
            "events",
            "payload",
            event_scope_ids,
        )

        if full_run_ids:
            await self._delete_where_in("work_item_runtime_links", "work_item_id", work_item_ids)
            await self._delete_where_in("delegation_events", "run_id", full_run_ids)
            await self._delete_where_in("delegation_work_items", "run_id", full_run_ids)
            await self._delete_where_in("delegation_cells", "run_id", full_run_ids)
            await self._delete_where_in("delegation_role_sessions", "run_id", full_run_ids)
            await self._delete_where_in("role_runtime_sessions", "run_id", full_run_ids)
            await self._delete_where_in("seat_states", "run_id", full_run_ids)
            await self._delete_where_in("team_instances", "run_id", full_run_ids)
            await self._delete_where_in("delegation_runs", "run_id", full_run_ids)
        elif work_item_ids:
            await self._delete_where_in("work_item_runtime_links", "work_item_id", work_item_ids)
            await self._delete_where_in("delegation_events", "work_item_id", work_item_ids)
            await self._delete_where_in("delegation_work_items", "work_item_id", work_item_ids)
            if await self._table_exists("external_sessions_v2"):
                await self._delete_where_text_contains(
                    "external_sessions_v2",
                    "metadata",
                    task_ids | session_ids | full_run_ids | work_item_ids | runtime_session_ids,
                )
            await self._delete_where_in("work_item_runtime_links", "runtime_task_id", task_ids)
            for chunk in self._chunked_ids(work_item_ids):
                placeholders = ", ".join("?" for _ in chunk)
                await self._db.execute(
                    f"""
                    UPDATE seat_states
                    SET current_work_item_id = '',
                        current_task_id = ''
                    WHERE current_work_item_id IN ({placeholders})
                    """,
                    tuple(chunk),
                )
                await self._db.execute(
                    f"""
                    UPDATE role_runtime_sessions
                    SET focused_work_item_id = ''
                    WHERE focused_work_item_id IN ({placeholders})
                    """,
                    tuple(chunk),
                )

    async def delete_company_runtime_artifacts_for_session(
        self,
        session_id: str,
    ) -> None:
        """Delete company runtime rows tied to a chat session id.

        This is intentionally session-driven rather than task-driven so a hard
        chat delete can clean orphan company runtime rows even if the task row
        was already removed by an earlier or partial delete path.
        """
        if not self._db:
            return
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return
        await self._delete_company_runtime_artifacts_for_task(
            "",
            clean_session_id,
            shared_session=False,
        )
        await self._db.commit()

    async def delete_session_data(self, task_id: str, session_id: str | None = None) -> None:
        """Delete all data associated with a session/task.

        Cleans: agent_messages, session_messages, session_parts,
        session_compactions, execution_checkpoints, external_sessions.
        The task row itself is NOT deleted (caller marks it CANCELLED).
        """
        if not self._db:
            return
        await self._db.execute("DELETE FROM agent_messages WHERE task_id = ?", (task_id,))
        shared_session = False
        if session_id:
            async with self._db.execute(
                "SELECT 1 FROM tasks WHERE session_id = ? AND id != ? LIMIT 1",
                (session_id, task_id),
            ) as cursor:
                shared_session = await cursor.fetchone() is not None
        if session_id:
            if not shared_session:
                await self._db.execute("DELETE FROM session_parts WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM session_compactions WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM session_memory_snapshots WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM agent_compactions WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM agent_memory_snapshots WHERE session_id = ?", (session_id,))
                await self._db.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
        else:
            await self._db.execute("DELETE FROM session_messages WHERE task_id = ?", (task_id,))
        if session_id and not shared_session:
            await self._db.execute(
                "DELETE FROM execution_checkpoints WHERE task_id = ? OR session_id = ?",
                (task_id, session_id),
            )
            await self._db.execute(
                "DELETE FROM external_sessions WHERE task_id = ? OR opc_session_id = ?",
                (task_id, session_id),
            )
            if await self._table_exists("external_sessions_v2"):
                await self._db.execute(
                    "DELETE FROM external_sessions_v2 WHERE task_id = ? OR opc_session_id = ?",
                    (task_id, session_id),
                )
        else:
            await self._db.execute("DELETE FROM execution_checkpoints WHERE task_id = ?", (task_id,))
            await self._db.execute("DELETE FROM external_sessions WHERE task_id = ?", (task_id,))
            if await self._table_exists("external_sessions_v2"):
                await self._db.execute("DELETE FROM external_sessions_v2 WHERE task_id = ?", (task_id,))
        await self._db.commit()

    async def hard_delete_task(self, task_id: str, session_id: str | None = None) -> None:
        """Permanently delete a task row and all persisted lifecycle data."""
        if not self._db:
            return
        shared_session = False
        if session_id:
            async with self._db.execute(
                "SELECT 1 FROM tasks WHERE session_id = ? AND id != ? LIMIT 1",
                (session_id, task_id),
            ) as cursor:
                shared_session = await cursor.fetchone() is not None
        await self._delete_company_runtime_artifacts_for_task(
            task_id,
            session_id,
            shared_session=shared_session,
        )
        await self.delete_session_data(task_id, session_id)
        await self._db.execute("DELETE FROM meetings WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM work_item_decisions WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM artifact_records WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM cost_records WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM cost_events WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM approval_records WHERE task_id = ?", (task_id,))
        if session_id and not shared_session:
            await self._db.execute(
                "DELETE FROM handoff_records WHERE task_id = ? OR session_id = ?",
                (task_id, session_id),
            )
            await self._db.execute(
                "DELETE FROM reorg_proposals WHERE task_id = ? OR session_id = ?",
                (task_id, session_id),
            )
            await self._db.execute(
                "DELETE FROM session_links WHERE task_id = ? OR session_id = ? OR linked_session_id = ?",
                (task_id, session_id, session_id),
            )
            await self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        else:
            await self._db.execute("DELETE FROM handoff_records WHERE task_id = ?", (task_id,))
            await self._db.execute("DELETE FROM reorg_proposals WHERE task_id = ?", (task_id,))
            await self._db.execute("DELETE FROM session_links WHERE task_id = ?", (task_id,))
        await self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await self._db.commit()

    @staticmethod
    def _metadata_has_work_item_projection_identity(metadata: dict[str, Any]) -> bool:
        return any(
            str(metadata.get(key, "") or "").strip()
            for key in (
                WORK_ITEM_PROJECTION_ID_KEY,
                WORK_ITEM_TURN_TYPE_KEY,
            )
        )

    async def _save_task_row(self, task: Task, *, commit: bool = True) -> None:
        assert self._db
        self._assert_project_write_scope(
            getattr(task, "project_id", None),
            operation="save_task",
            entity=f"task {getattr(task, 'id', '')!r}",
        )
        task.metadata = dict(task.metadata or {})
        if self._metadata_has_work_item_projection_identity(task.metadata):
            task.metadata, _ = migrate_work_item_projection_metadata(
                task.metadata,
                turn_type_fallback="",
            )
        await self._db.execute(
            """INSERT INTO tasks
            (id, session_id, parent_session_id, title, description, assigned_to, status, priority, dependencies,
             execution_lock, context_snapshot, assigned_external_agent, created_at,
             deadline, result, parent_id, project_id, tags, comments,
             retry_count, max_retries, metadata,
             org_id, goal_id, checkout_run_id, execution_locked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                session_id=excluded.session_id,
                parent_session_id=excluded.parent_session_id,
                title=excluded.title,
                description=excluded.description,
                assigned_to=excluded.assigned_to,
                status=excluded.status,
                priority=excluded.priority,
                dependencies=excluded.dependencies,
                execution_lock=excluded.execution_lock,
                context_snapshot=excluded.context_snapshot,
                assigned_external_agent=excluded.assigned_external_agent,
                created_at=excluded.created_at,
                deadline=excluded.deadline,
                result=excluded.result,
                parent_id=excluded.parent_id,
                project_id=excluded.project_id,
                tags=excluded.tags,
                comments=excluded.comments,
                retry_count=excluded.retry_count,
                max_retries=excluded.max_retries,
                metadata=excluded.metadata,
                org_id=excluded.org_id,
                goal_id=excluded.goal_id,
                checkout_run_id=excluded.checkout_run_id,
                execution_locked_at=excluded.execution_locked_at""",
            (
                task.id,
                task.session_id,
                task.parent_session_id,
                task.title,
                task.description,
                task.assigned_to,
                task.status.value,
                task.priority,
                _json_dumps(task.dependencies),
                int(task.execution_lock),
                _json_dumps(task.context_snapshot),
                task.assigned_external_agent,
                task.created_at.isoformat(),
                task.deadline.isoformat() if task.deadline else None,
                _json_dumps(task.result) if task.result else None,
                task.parent_id,
                task.project_id,
                _json_dumps(task.tags),
                _json_dumps(task.comments),
                task.retry_count,
                task.max_retries,
                _json_dumps(task.metadata),
                task.org_id,
                task.goal_id,
                task.checkout_run_id,
                task.execution_locked_at.isoformat() if task.execution_locked_at else None,
            ),
        )
        if commit:
            await self._db.commit()

    async def save_task(self, task: Task) -> None:
        await self._save_task_row(task, commit=True)

    async def get_task(self, task_id: str) -> Task | None:
        assert self._db
        task = await self._get_task_unhydrated(task_id)
        if task is None:
            return None
        await self.hydrate_task_work_item_links([task])
        return task

    async def get_tasks(
        self,
        project_id: str | None = None,
        status: TaskStatus | None = None,
        parent_id: str | None = None,
    ) -> list[Task]:
        assert self._db
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status.value)
        if parent_id is not None:
            query += " AND parent_id = ?"
            params.append(parent_id)
        query += " ORDER BY priority ASC, created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            tasks = [self._row_to_task(row, cursor.description) for row in rows]
        await self.hydrate_task_work_item_links(tasks)
        return tasks

    async def get_tasks_by_session_id(
        self,
        session_id: str,
        project_id: str | None = None,
    ) -> list[Task]:
        assert self._db
        query = "SELECT * FROM tasks WHERE session_id = ?"
        params: list[Any] = [session_id]
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        query += " ORDER BY priority ASC, created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            tasks = [self._row_to_task(row, cursor.description) for row in rows]
        await self.hydrate_task_work_item_links(tasks)
        return tasks

    async def update_task_status(self, task_id: str, status: TaskStatus) -> None:
        assert self._db
        await self._db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status.value, task_id))
        await self._db.commit()

    async def append_task_comment(self, task_id: str, comment: dict[str, Any]) -> None:
        task = await self.get_task(task_id)
        if not task:
            return
        task.comments = list(task.comments)
        task.comments.append(comment)
        await self.save_task(task)

    async def acquire_task_lock(self, task_id: str, *, lease_seconds: int | None = None) -> bool:
        """Atomically acquire the execution lock on a task.

        When ``lease_seconds`` is provided, a stale lock (one whose
        ``execution_locked_at`` timestamp is older than ``now - lease_seconds``)
        can be stolen. This lets a new process claim an execution slot whose
        original holder died without releasing — the common crash-recovery case.

        Always refreshes ``execution_locked_at`` to ``now()`` on successful
        acquire, so the returned lease starts fresh.
        """
        assert self._db
        now_iso = datetime.now().isoformat()
        if lease_seconds is not None and lease_seconds > 0:
            cutoff_iso = (datetime.now() - timedelta(seconds=int(lease_seconds))).isoformat()
            query = (
                "UPDATE tasks SET execution_lock = 1, execution_locked_at = ? "
                "WHERE id = ? AND ("
                "execution_lock = 0 "
                "OR execution_locked_at IS NULL "
                "OR execution_locked_at < ?"
                ")"
            )
            params: tuple[Any, ...] = (now_iso, task_id, cutoff_iso)
        else:
            query = (
                "UPDATE tasks SET execution_lock = 1, execution_locked_at = ? "
                "WHERE id = ? AND execution_lock = 0"
            )
            params = (now_iso, task_id)
        async with self._db.execute(query, params) as cursor:
            await self._db.commit()
            return cursor.rowcount > 0

    async def release_task_lock(self, task_id: str) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE tasks SET execution_lock = 0, execution_locked_at = NULL WHERE id = ?",
            (task_id,),
        )
        await self._db.commit()

    def _row_to_task(self, row: Any, description: Any) -> Task:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return Task(
            id=data["id"],
            session_id=data.get("session_id"),
            parent_session_id=data.get("parent_session_id"),
            title=data["title"],
            description=data["description"],
            assigned_to=data["assigned_to"],
            status=TaskStatus(data["status"]),
            priority=data["priority"],
            dependencies=_json_loads(data["dependencies"], []),
            execution_lock=bool(data["execution_lock"]),
            context_snapshot=_json_loads(data["context_snapshot"], {}),
            assigned_external_agent=data["assigned_external_agent"],
            created_at=datetime.fromisoformat(data["created_at"]),
            deadline=datetime.fromisoformat(data["deadline"]) if data["deadline"] else None,
            result=_json_loads(data["result"], None),
            parent_id=data["parent_id"],
            project_id=data["project_id"],
            tags=_json_loads(data["tags"], []),
            comments=_json_loads(data["comments"], []),
            retry_count=data["retry_count"],
            max_retries=data["max_retries"],
            metadata=_json_loads(data["metadata"], {}),
            org_id=data.get("org_id"),
            goal_id=data.get("goal_id"),
            checkout_run_id=data.get("checkout_run_id"),
            execution_locked_at=(
                datetime.fromisoformat(data["execution_locked_at"])
                if data.get("execution_locked_at")
                else None
            ),
        )

    async def get_tasks_by_ids(self, task_ids: list[str]) -> list[Task]:
        if not task_ids:
            return []
        assert self._db
        placeholders = ", ".join("?" for _ in task_ids)
        query = f"SELECT * FROM tasks WHERE id IN ({placeholders}) ORDER BY priority ASC, created_at ASC"
        async with self._db.execute(query, task_ids) as cursor:
            rows = await cursor.fetchall()
            tasks = [self._row_to_task(row, cursor.description) for row in rows]
        await self.hydrate_task_work_item_links(tasks)
        return tasks

    async def checkout_task(self, task_id: str, agent_id: str, run_id: str | None = None) -> bool:
        """Atomically claim a task for execution. Returns True on success."""
        assert self._db
        import uuid as _uuid
        run_id = run_id or str(_uuid.uuid4())
        now = datetime.now().isoformat()
        async with self._db.execute(
            """UPDATE tasks
               SET status = 'running',
                   assigned_to = ?,
                   checkout_run_id = ?,
                   execution_locked_at = ?
               WHERE id = ?
                 AND status IN ('pending', 'todo')
                 AND (assigned_to IS NULL OR assigned_to = '' OR assigned_to = ?)""",
            (agent_id, run_id, now, task_id, agent_id),
        ) as cursor:
            await self._db.commit()
            return cursor.rowcount > 0

    async def release_task(self, task_id: str, agent_id: str) -> bool:
        """Release a checked-out task back to pending."""
        assert self._db
        async with self._db.execute(
            """UPDATE tasks
               SET status = 'pending',
                   checkout_run_id = NULL,
                   execution_locked_at = NULL
               WHERE id = ?
                 AND assigned_to = ?
                 AND status = 'running'""",
            (task_id, agent_id),
        ) as cursor:
            await self._db.commit()
            return cursor.rowcount > 0
