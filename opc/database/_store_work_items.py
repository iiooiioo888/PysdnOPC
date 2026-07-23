"""WorkItemStoreMixin — 工作項目 + runtime links 相關方法。"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from opc.core.models import (
    DelegationWorkItem,
    Phase,
    Task,
    TaskStatus,
)
import inspect
import logging

from opc.layer2_organization.phase import InvalidPhaseTransition

logger = logging.getLogger(__name__)
from opc.layer2_organization.phase import (
    DONE_PHASES,
    IN_PROGRESS_PHASES,
    IN_REVIEW_PHASES,
    TODO_PHASES,
    coerce_phase,
    is_terminal,
    kanban_column,
    on_phase_transition,
    validate_transition,
)
from opc.layer2_organization.work_item_identity import (
    WORK_ITEM_PROJECTION_ID_KEY,
    WORK_ITEM_TURN_TYPE_KEY,
    migrate_work_item_projection_metadata,
    projection_id_for_work_item,
)
from opc.layer2_organization.work_item_links import (
    linked_work_item_id_for_task,
    set_linked_work_item_id,
)
from opc.layer2_organization.work_item_runtime import (
    is_work_item_runtime_metadata,
    migrate_work_item_runtime_metadata,
)
from opc.layer2_organization.work_item_runtime_invariants import (
    validate_work_item_runtime_projection,
)
from opc.database._utils import _json_dumps, _json_loads

if TYPE_CHECKING:
    from opc.database.store import OPCStore


class WorkItemStoreMixin:
    """Mixin providing 工作項目 + runtime links 相關方法 for OPCStore."""

    async def _work_item_exists(self, work_item_id: str) -> bool:
        assert self._db is not None
        wid = str(work_item_id or "").strip()
        if not wid:
            return False
        async with self._db.execute(
            "SELECT 1 FROM delegation_work_items WHERE work_item_id=? LIMIT 1",
            (wid,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _runtime_link_task_id_for_work_item(self, work_item_id: str) -> str:
        assert self._db is not None
        wid = str(work_item_id or "").strip()
        if not wid:
            return ""
        async with self._db.execute(
            "SELECT runtime_task_id FROM work_item_runtime_links WHERE work_item_id=?",
            (wid,),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0] or "").strip() if row else ""

    async def _runtime_link_work_item_id_for_task(self, task_id: str) -> str:
        assert self._db is not None
        tid = str(task_id or "").strip()
        if not tid:
            return ""
        async with self._db.execute(
            "SELECT work_item_id FROM work_item_runtime_links WHERE runtime_task_id=?",
            (tid,),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0] or "").strip() if row else ""

    async def _runtime_task_link_is_replaceable(self, task_id: str) -> bool:
        status = await self._task_status_for_id(task_id)
        return not status or status in self._terminal_task_statuses()

    @staticmethod
    def _runtime_task_matches_work_item(
        task: Task,
        item: DelegationWorkItem,
        *,
        preferred_task_id: str = "",
    ) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        wid = str(getattr(item, "work_item_id", "") or "").strip()
        if not wid:
            return False

        preferred = str(preferred_task_id or "").strip()
        if not is_work_item_runtime_metadata(metadata) and str(getattr(task, "id", "") or "").strip() != preferred:
            return False

        projection_id = projection_id_for_work_item(item)
        task_projection_id = str(metadata.get(WORK_ITEM_PROJECTION_ID_KEY, "") or "").strip()
        if projection_id and task_projection_id != projection_id:
            return False

        item_run_id = str(getattr(item, "run_id", "") or "").strip()
        task_run_id = str(metadata.get("delegation_run_id", "") or "").strip()
        if item_run_id and task_run_id != item_run_id:
            return False

        item_role_id = str(getattr(item, "role_id", "") or "").strip()
        task_role_id = str(
            metadata.get("work_item_role_id", "")
            or metadata.get("role_id", "")
            or getattr(task, "assigned_to", "")
            or ""
        ).strip()
        if item_role_id and task_role_id and task_role_id != item_role_id:
            return False

        item_seat_id = str(
            getattr(item, "seat_id", "")
            or dict(getattr(item, "metadata", {}) or {}).get("seat_id", "")
            or ""
        ).strip()
        task_seat_id = str(
            metadata.get("delegation_seat_id", "")
            or metadata.get("seat_id", "")
            or ""
        ).strip()
        if item_seat_id and task_seat_id and task_seat_id != item_seat_id:
            return False

        return True

    async def _write_work_item_runtime_link(
        self,
        work_item_id: str,
        runtime_task_id: str,
        *,
        link_kind: str = "primary",
        commit: bool = True,
    ) -> bool:
        assert self._db is not None
        wid = str(work_item_id or "").strip()
        tid = str(runtime_task_id or "").strip()
        if not wid or not tid:
            return False
        now = datetime.now().isoformat()
        # runtime_task_id is UNIQUE. If stale legacy data points this Task at
        # another WorkItem, the explicit WorkItem link wins.
        await self._db.execute(
            "DELETE FROM work_item_runtime_links WHERE runtime_task_id=? AND work_item_id != ?",
            (tid, wid),
        )
        await self._db.execute(
            """INSERT INTO work_item_runtime_links
               (work_item_id, runtime_task_id, link_kind, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(work_item_id) DO UPDATE SET
                   runtime_task_id=excluded.runtime_task_id,
                   link_kind=excluded.link_kind,
                   updated_at=excluded.updated_at""",
            (wid, tid, str(link_kind or "primary").strip() or "primary", now, now),
        )
        if commit:
            await self._db.commit()
        return True

    async def _validate_work_item_runtime_links(self) -> dict[str, int]:
        """Validate canonical WorkItem/Task links.

        WorkItem is the business source of truth; runtime Tasks are connected
        only through ``work_item_runtime_links``.
        """
        if self._db is None:
            return {}
        required_tables = ("tasks", "delegation_work_items", "work_item_runtime_links")
        for table in required_tables:
            if not await self._table_exists(table):
                return {"existing": 0, "missing": 0}

        stats = {"existing": 0, "missing": 0}
        diagnostics: list[str] = []
        linked_task_ids: set[str] = set()

        async with self._db.execute(
            "SELECT work_item_id, runtime_task_id FROM work_item_runtime_links"
        ) as cursor:
            for work_item_id, task_id in await cursor.fetchall():
                wid = str(work_item_id or "").strip()
                tid = str(task_id or "").strip()
                if not wid or not tid:
                    continue
                stats["existing"] += 1
                linked_task_ids.add(tid)
                if not await self._work_item_exists(wid):
                    diagnostics.append(f"runtime link points to missing WorkItem: work_item={wid} task={tid}")
                if not await self._task_exists(tid):
                    diagnostics.append(f"runtime link points to missing Task: work_item={wid} task={tid}")

        async with self._db.execute(
            """SELECT id, metadata
               FROM tasks
               WHERE metadata LIKE '%work_item_runtime%'
                 AND metadata LIKE '%work_item_projection_id%'"""
        ) as cursor:
            projection_rows = await cursor.fetchall()
        for task_id, metadata_json in projection_rows:
            tid = str(task_id or "").strip()
            try:
                metadata = _json_loads(metadata_json, {})
            except Exception as exc:
                diagnostics.append(f"invalid company runtime projection metadata: task={tid} error={exc}")
                continue
            if not isinstance(metadata, dict) or not is_work_item_runtime_metadata(metadata):
                continue
            projection_id = str(metadata.get(WORK_ITEM_PROJECTION_ID_KEY, "") or "").strip()
            if projection_id and tid not in linked_task_ids:
                stats["missing"] += 1
                diagnostics.append(
                    "company runtime projection missing canonical link: "
                    f"task={tid} projection={projection_id}"
                )

        if diagnostics:
            sample = "; ".join(diagnostics[:12])
            suffix = "" if len(diagnostics) <= 12 else f"; ... {len(diagnostics) - 12} more"
            raise RuntimeError(
                "canonical WorkItem runtime link validation failed "
                f"(db_path={self.db_path}): {sample}{suffix}"
            )

        return stats

    async def _find_existing_runtime_task_for_work_item(
        self,
        work_item: DelegationWorkItem,
        candidate_task: Task,
    ) -> Task | None:
        assert self._db is not None
        session_id = str(getattr(candidate_task, "session_id", "") or "").strip()
        project_id = str(getattr(candidate_task, "project_id", "") or "").strip()
        projection_id = projection_id_for_work_item(work_item)
        run_id = str(getattr(work_item, "run_id", "") or "").strip()
        if not session_id or not project_id or not projection_id or not run_id:
            return None
        async with self._db.execute(
            """SELECT * FROM tasks
               WHERE session_id=?
                 AND project_id=?
                 AND metadata LIKE ?
                 AND metadata LIKE ?
               ORDER BY created_at ASC, id ASC""",
            (
                session_id,
                project_id,
                "%work_item_runtime%",
                f"%{projection_id}%",
            ),
        ) as cursor:
            rows = await cursor.fetchall()
            description = cursor.description

        candidates: list[Task] = []
        for row in rows:
            task = self._row_to_task(row, description)
            metadata = dict(task.metadata or {})
            if str(metadata.get("delegation_run_id", "") or "").strip() != run_id:
                continue
            if str(metadata.get(WORK_ITEM_PROJECTION_ID_KEY, "") or "").strip() != projection_id:
                continue
            if not self._runtime_task_matches_work_item(task, work_item):
                continue
            candidates.append(task)
        if not candidates:
            return None

        def _candidate_sort_key(task: Task) -> tuple[int, int, datetime, str]:
            metadata = dict(task.metadata or {})
            status = str(task.status.value if hasattr(task.status, "value") else task.status or "").strip().lower()
            terminal = 1 if status in self._terminal_task_statuses() else 0
            duplicate = 1 if str(metadata.get("duplicate_runtime_task_of", "") or "").strip() else 0
            return (duplicate, terminal, task.created_at, str(task.id or ""))

        candidates.sort(key=_candidate_sort_key)
        if len(candidates) > 1:
            logger.warning(
                "ensure_runtime_task_for_work_item: multiple exact runtime Tasks "
                f"for work_item={work_item.work_item_id}; using task={candidates[0].id} "
                f"candidates={[task.id for task in candidates[:5]]}"
            )
        return candidates[0]

    async def ensure_runtime_task_for_work_item(
        self,
        work_item: DelegationWorkItem,
        task_factory: Any,
        *,
        replace_policy: str = "never_active",
    ) -> Task:
        """Return the authoritative runtime Task for a WorkItem.

        This is the only hot-path creation/reuse entry point for company-mode
        runtime Tasks. It writes the structured link in the same transaction as
        task creation, and ordinary read paths do not repair or rescore links.
        """
        if self._db is None:
            raise RuntimeError("store is not initialized")
        wid = str(getattr(work_item, "work_item_id", "") or "").strip()
        if not wid:
            raise ValueError("work_item_id is required")
        policy = str(replace_policy or "never_active").strip() or "never_active"
        if policy != "never_active":
            raise ValueError(f"unsupported runtime link replace_policy: {replace_policy}")

        await self._db.execute("BEGIN IMMEDIATE")
        try:
            if not await self._work_item_exists(wid):
                raise ValueError(f"WorkItem does not exist: {wid}")

            linked_task_id = await self._runtime_link_task_id_for_work_item(wid)
            if linked_task_id:
                linked_task = await self._get_task_unhydrated(linked_task_id)
                if linked_task is not None:
                    set_linked_work_item_id(linked_task, wid)
                    issues = [
                        issue for issue in validate_work_item_runtime_projection(linked_task, work_item)
                        if issue.severity == "error"
                    ]
                    if issues:
                        raise RuntimeError(
                            "work-item runtime invariant failed for linked Task "
                            f"{linked_task.id}: "
                            + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
                        )
                    await self._db.commit()
                    return linked_task
                await self._db.execute(
                    "DELETE FROM work_item_runtime_links WHERE work_item_id=?",
                    (wid,),
                )

            candidate = task_factory() if callable(task_factory) else task_factory
            if inspect.isawaitable(candidate):
                candidate = await candidate
            if not isinstance(candidate, Task):
                raise TypeError("task_factory must return a Task")
            candidate.metadata = dict(candidate.metadata or {})

            task = await self._find_existing_runtime_task_for_work_item(work_item, candidate)
            if task is None:
                task = candidate
                await self._save_task_row(task, commit=False)

            existing_owner_id = await self._runtime_link_work_item_id_for_task(task.id)
            if existing_owner_id and existing_owner_id != wid:
                raise RuntimeError(
                    "runtime Task is already linked to another WorkItem: "
                    f"task={task.id} owner={existing_owner_id} requested={wid}"
                )

            linked = await self._write_work_item_runtime_link(
                wid,
                task.id,
                link_kind="primary",
                commit=False,
            )
            if not linked:
                raise RuntimeError(
                    f"failed to link runtime Task {task.id} for WorkItem {wid}"
                )
            set_linked_work_item_id(task, wid)
            issues = [
                issue for issue in validate_work_item_runtime_projection(task, work_item)
                if issue.severity == "error"
            ]
            if issues:
                raise RuntimeError(
                    "work-item runtime invariant failed for Task "
                    f"{task.id}: "
                    + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
                )
            await self._db.commit()
            return task
        except Exception:
            await self._db.rollback()
            raise

    async def link_work_item_runtime_task(
        self,
        work_item_id: str,
        runtime_task_id: str,
        *,
        link_kind: str = "primary",
        allow_replace: bool = False,
    ) -> bool:
        """Persist the authoritative WorkItem -> runtime Task link."""
        if self._db is None:
            return False
        wid = str(work_item_id or "").strip()
        tid = str(runtime_task_id or "").strip()
        if not wid or not tid:
            return False
        if not await self._work_item_exists(wid) or not await self._task_exists(tid):
            return False
        await self._db.execute("BEGIN IMMEDIATE")
        try:
            existing_task_id = await self._runtime_link_task_id_for_work_item(wid)
            existing_owner_id = await self._runtime_link_work_item_id_for_task(tid)
            if existing_task_id and existing_task_id != tid and not allow_replace:
                if not await self._runtime_task_link_is_replaceable(existing_task_id):
                    logger.warning(
                        "work-item runtime link refused to overwrite active link: "
                        f"work_item={wid} existing_task={existing_task_id} requested_task={tid}"
                    )
                    await self._db.rollback()
                    return False
            if existing_owner_id and existing_owner_id != wid and not allow_replace:
                logger.warning(
                    "work-item runtime link refused to steal task from another work item: "
                    f"task={tid} existing_work_item={existing_owner_id} requested_work_item={wid}"
                )
                await self._db.rollback()
                return False
            wrote = await self._write_work_item_runtime_link(
                wid,
                tid,
                link_kind=link_kind,
                commit=False,
            )
            await self._db.commit()
            return wrote
        except Exception:
            await self._db.rollback()
            raise

    async def hydrate_task_work_item_links(self, tasks: list[Task]) -> list[Task]:
        """Attach non-persisted WorkItem link ids from the link table only."""
        if self._db is None or not tasks:
            return tasks
        task_by_id = {
            str(task.id or "").strip(): task
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
        }
        if not task_by_id:
            return tasks
        placeholders = ", ".join("?" for _ in task_by_id)
        async with self._db.execute(
            f"""SELECT work_item_id, runtime_task_id
                FROM work_item_runtime_links
                WHERE runtime_task_id IN ({placeholders})""",
            list(task_by_id),
        ) as cursor:
            rows = await cursor.fetchall()
        linked_task_ids: set[str] = set()
        for work_item_id, task_id in rows:
            tid = str(task_id or "").strip()
            wid = str(work_item_id or "").strip()
            task = task_by_id.get(tid)
            if task is None or not wid:
                continue
            set_linked_work_item_id(task, wid)
            linked_task_ids.add(tid)

        for tid, task in task_by_id.items():
            if tid not in linked_task_ids:
                set_linked_work_item_id(task, "")
        return tasks

    async def get_runtime_links_for_work_items(self, work_item_ids: list[str]) -> dict[str, str]:
        if self._db is None:
            return {}
        ids = [
            str(work_item_id or "").strip()
            for work_item_id in work_item_ids
            if str(work_item_id or "").strip()
        ]
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        async with self._db.execute(
            f"""SELECT work_item_id, runtime_task_id
                FROM work_item_runtime_links
                WHERE work_item_id IN ({placeholders})""",
            ids,
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            str(work_item_id or "").strip(): str(task_id or "").strip()
            for work_item_id, task_id in rows
            if str(work_item_id or "").strip() and str(task_id or "").strip()
        }

    async def get_runtime_task_for_work_item(self, work_item_id: str) -> Task | None:
        if self._db is None:
            return None
        wid = str(work_item_id or "").strip()
        if not wid:
            return None
        task_id = await self._runtime_link_task_id_for_work_item(wid)
        if not task_id:
            return None
        task = await self._get_task_unhydrated(task_id)
        if task is None:
            return None
        set_linked_work_item_id(task, wid)
        return task

    async def get_work_item_for_runtime_task(self, task_id: str) -> DelegationWorkItem | None:
        if self._db is None:
            return None
        tid = str(task_id or "").strip()
        if not tid:
            return None
        work_item_id = await self._runtime_link_work_item_id_for_task(tid)
        if not work_item_id:
            return None
        return await self.get_delegation_work_item(work_item_id)

    async def save_delegation_work_item(
        self,
        item: DelegationWorkItem,
    ) -> None:
        await self._write_delegation_work_item(item, if_absent=False)

    async def _write_delegation_work_item(
        self,
        item: DelegationWorkItem,
        *,
        if_absent: bool,
    ) -> bool:
        # Single-source-of-truth gate: every write — whether it goes through
        # update_delegation_work_item or directly mutates `item.phase` and
        # then calls save — passes through validate_transition. Skipping the
        # validation requires a separate code path; there is no way to write
        # an invalid phase by accident.
        existing = await self.get_delegation_work_item(item.work_item_id)
        if if_absent and existing is not None:
            return False
        previous_phase = existing.phase if existing is not None else None
        validate_transition(previous_phase, item.phase)
        item.metadata = dict(item.metadata or {})
        if (
            self._metadata_has_work_item_projection_identity(item.metadata)
            or str(item.projection_id or "").strip()
            or str(item.kind or "").strip()
        ):
            item.metadata, _ = migrate_work_item_projection_metadata(
                item.metadata,
                projection_id_fallback=str(item.projection_id or item.work_item_id or "").strip(),
                turn_type_fallback=str(item.kind or "").strip(),
            )
        # Capture pre-write state we need for hook context (the persisted
        # row's previous claim, etc.) — hooks fire on the saved item but
        # may want to know "what changed".
        target_phase = item.phase
        db = self._require_db()
        conflict_action = (
            "DO NOTHING"
            if if_absent
            else """DO UPDATE SET
                run_id=excluded.run_id,
                cell_id=excluded.cell_id,
                team_instance_id=excluded.team_instance_id,
                team_id=excluded.team_id,
                role_id=excluded.role_id,
                seat_id=excluded.seat_id,
                seat_state_id=excluded.seat_state_id,
                role_runtime_session_id=excluded.role_runtime_session_id,
                parent_work_item_id=excluded.parent_work_item_id,
                source_role_id=excluded.source_role_id,
                source_seat_id=excluded.source_seat_id,
                title=excluded.title,
                summary=excluded.summary,
                kind=excluded.kind,
                projection_id=excluded.projection_id,
                phase=excluded.phase,
                batch_id=excluded.batch_id,
                batch_index=excluded.batch_index,
                deliverable_summary=excluded.deliverable_summary,
                blocked_reason=excluded.blocked_reason,
                handoff_status=excluded.handoff_status,
                continuation_source=excluded.continuation_source,
                manager_role_id=excluded.manager_role_id,
                manager_seat_id=excluded.manager_seat_id,
                claimed_by_role_runtime_session_id=excluded.claimed_by_role_runtime_session_id,
                claimed_by_seat_id=excluded.claimed_by_seat_id,
                metadata=excluded.metadata,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at"""
        )
        cursor = await db.execute(
            f"""INSERT INTO delegation_work_items
            (work_item_id, run_id, cell_id, team_instance_id, team_id, role_id, seat_id, seat_state_id,
             role_runtime_session_id, parent_work_item_id, source_role_id, source_seat_id, title, summary,
             kind, projection_id, phase, batch_id, batch_index,
             deliverable_summary, blocked_reason, handoff_status, continuation_source, manager_role_id,
             manager_seat_id, claimed_by_role_runtime_session_id, claimed_by_seat_id, metadata,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_item_id) {conflict_action}""",
            (
                item.work_item_id,
                item.run_id,
                item.cell_id,
                item.team_instance_id,
                item.team_id,
                item.role_id,
                item.seat_id,
                item.seat_state_id,
                item.role_runtime_session_id,
                item.parent_work_item_id,
                item.source_role_id,
                item.source_seat_id,
                item.title,
                item.summary,
                item.kind,
                item.projection_id,
                item.phase.value,
                item.batch_id,
                int(item.batch_index or 0),
                item.deliverable_summary,
                item.blocked_reason,
                item.handoff_status,
                item.continuation_source,
                item.manager_role_id,
                item.manager_seat_id,
                item.claimed_by_role_runtime_session_id,
                item.claimed_by_seat_id,
                _json_dumps(item.metadata),
                item.created_at.isoformat(),
                item.updated_at.isoformat(),
            ),
        )
        await db.commit()
        if if_absent and not (getattr(cursor, "rowcount", 0) or 0):
            return False
        # D2 hook fire — propagate phase change to dependent layers
        # (task.status, role_session.status, dispatcher wake, etc.). All
        # writes to delegation_work_items pass through here, so this is
        # the single chokepoint where hooks fire.
        try:
            await on_phase_transition(previous_phase, target_phase, item, store=self)
        except Exception:  # never let hook failures break the write
            logger.opt(exception=True).debug("on_phase_transition raised at top level")
        return True

    async def insert_delegation_work_item_if_absent(
        self,
        item: DelegationWorkItem,
    ) -> bool:
        """Atomically create a WorkItem without overwriting a concurrent claim.

        Auxiliary report/review IDs are deterministic.  A read-before-write
        check is insufficient because another dispatcher can create and claim
        the same card between those operations; the conflict decision must be
        made by SQLite in the insert statement itself.
        """
        return await self._write_delegation_work_item(item, if_absent=True)

    async def list_delegation_work_items(
        self,
        run_id: str,
        *,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        seat_id: str | None = None,
        role_runtime_session_id: str | None = None,
        role_id: str | None = None,
        batch_id: str | None = None,
    ) -> list[DelegationWorkItem]:
        db = self._require_db()
        query = "SELECT * FROM delegation_work_items WHERE run_id = ?"
        params: list[Any] = [run_id]
        if team_instance_id:
            query += " AND team_instance_id = ?"
            params.append(team_instance_id)
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        if role_runtime_session_id:
            query += " AND role_runtime_session_id = ?"
            params.append(role_runtime_session_id)
        if role_id:
            query += " AND role_id = ?"
            params.append(role_id)
        if batch_id:
            query += " AND batch_id = ?"
            params.append(batch_id)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_work_item(row, cursor.description) for row in rows]

    async def list_manager_board(
        self,
        run_id: str,
        *,
        manager_seat_id: str,
        parent_work_item_id: str | None = None,
    ) -> list[DelegationWorkItem]:
        db = self._require_db()
        query = "SELECT * FROM delegation_work_items WHERE run_id = ? AND manager_seat_id = ?"
        params: list[Any] = [run_id, str(manager_seat_id or "").strip()]
        normalized_parent = str(parent_work_item_id or "").strip()
        if normalized_parent:
            query += " AND parent_work_item_id = ?"
            params.append(normalized_parent)
        query += " ORDER BY batch_index ASC, created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_work_item(row, cursor.description) for row in rows]

    async def summarize_parent_status(
        self,
        run_id: str,
        *,
        manager_seat_id: str,
        parent_work_item_id: str,
    ) -> dict[str, Any]:
        parent_id = str(parent_work_item_id or "").strip()
        manager_id = str(manager_seat_id or "").strip()
        if not run_id or not parent_id or not manager_id:
            return {
                "run_id": str(run_id or "").strip(),
                "manager_seat_id": manager_id,
                "parent_work_item_id": parent_id,
                "total_children": 0,
                "phase_counts": {},
                "column_counts": {},
                "releasable_work_item_ids": [],
                "blocked_reasons": [],
                "blocker_count": 0,
                "rework_count": 0,
                "upstream_summary": [],
                "derived_parent_column": "todo",
            }
        children = await self.list_manager_board(
            run_id,
            manager_seat_id=manager_id,
            parent_work_item_id=parent_id,
        )
        phase_counts: dict[str, int] = {}
        column_counts: dict[str, int] = {}
        blocked_reasons: list[str] = []
        releasable_work_item_ids: list[str] = []
        upstream_summary: list[dict[str, Any]] = []
        blocker_count = 0
        rework_count = 0
        done_children = 0
        active_children = 0
        review_children = 0
        todo_children = 0
        visible_children = 0
        for item in children:
            phase = item.phase
            metadata = dict(item.metadata or {})
            column_value = kanban_column(phase)
            phase_counts[phase.value] = phase_counts.get(phase.value, 0) + 1
            column_counts[column_value] = column_counts.get(column_value, 0) + 1
            if item.blocked_reason:
                blocked_reasons.append(str(item.blocked_reason).strip())
            if item.blocked_reason or phase in {
                Phase.WAITING_FOR_PEER,
                Phase.WAITING_FOR_CHILDREN,
                Phase.NEEDS_ATTENTION,
                Phase.WAITING_DEPENDENCIES,
            }:
                blocker_count += 1
            if str(metadata.get("rework_feedback", "") or "").strip():
                rework_count += 1
            if phase == Phase.QUEUED:
                releasable_work_item_ids.append(str(item.work_item_id))
            if metadata.get("hidden_from_company_kanban"):
                continue
            visible_children += 1
            if phase in DONE_PHASES:
                done_children += 1
            elif phase in IN_PROGRESS_PHASES:
                active_children += 1
            elif phase in IN_REVIEW_PHASES:
                review_children += 1
            else:
                todo_children += 1
            visibility = str(metadata.get("upstream_visibility", "summary_only") or "summary_only").strip().lower()
            if visibility != "hidden":
                payload = {
                    "work_item_id": str(item.work_item_id),
                    "title": str(item.title or "").strip(),
                    "role_id": str(item.role_id or "").strip(),
                    "phase": phase.value,
                    "kanban_column": column_value,
                    "deliverable_summary": str(item.deliverable_summary or "").strip(),
                    "blocked_reason": str(item.blocked_reason or "").strip(),
                    "completion_report": str(metadata.get("completion_report", "") or "").strip(),
                    "review_owner_role_id": str(
                        metadata.get("review_owner_role_id")
                        or item.manager_role_id
                        or ""
                    ).strip(),
                    "review_owner_seat_id": str(
                        metadata.get("review_owner_seat_id")
                        or item.manager_seat_id
                        or ""
                    ).strip(),
                    "review_evidence": dict(metadata.get("review_evidence", {}) or {}),
                }
                if visibility == "debug":
                    payload["summary"] = str(item.summary or "").strip()
                    payload["dependency_work_item_ids"] = [
                        str(dep).strip()
                        for dep in list(metadata.get("dependency_work_item_ids", []) or [])
                        if str(dep).strip()
                    ]
                upstream_summary.append(payload)
        derived_parent_column = "todo"
        if visible_children and done_children == visible_children:
            derived_parent_column = "done"
        elif review_children:
            derived_parent_column = "in_review"
        elif active_children:
            derived_parent_column = "in_progress"
        return {
            "run_id": str(run_id or "").strip(),
            "manager_seat_id": manager_id,
            "parent_work_item_id": parent_id,
            "total_children": visible_children,
            "phase_counts": phase_counts,
            "column_counts": column_counts,
            "releasable_work_item_ids": releasable_work_item_ids,
            "blocked_reasons": list(dict.fromkeys(item for item in blocked_reasons if item)),
            "blocker_count": blocker_count,
            "rework_count": rework_count,
            "upstream_summary": upstream_summary[:12],
            "derived_parent_column": derived_parent_column,
        }

    async def get_delegation_work_item(self, work_item_id: str) -> DelegationWorkItem | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM delegation_work_items WHERE work_item_id = ?",
            (work_item_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_delegation_work_item(row, cursor.description)

    async def update_delegation_work_item(
        self,
        work_item_id: str,
        *,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        seat_id: str | None = None,
        seat_state_id: str | None = None,
        role_runtime_session_id: str | None = None,
        phase: Phase | str | None = None,
        summary: str | None = None,
        batch_id: str | None = None,
        batch_index: int | None = None,
        deliverable_summary: str | None = None,
        blocked_reason: str | None = None,
        handoff_status: str | None = None,
        continuation_source: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
        metadata_unset: list[str] | tuple[str, ...] | None = None,
        manager_role_id: str | None = None,
        manager_seat_id: str | None = None,
        claimed_by_role_runtime_session_id: str | None = None,
        claimed_by_seat_id: str | None = None,
    ) -> DelegationWorkItem | None:
        item = await self.get_delegation_work_item(work_item_id)
        if item is None:
            return None
        previous_phase = item.phase
        if team_instance_id is not None:
            item.team_instance_id = str(team_instance_id or "").strip()
        if team_id is not None:
            item.team_id = str(team_id or "").strip()
        if seat_id is not None:
            item.seat_id = str(seat_id or "").strip()
        if seat_state_id is not None:
            item.seat_state_id = str(seat_state_id or "").strip()
        if role_runtime_session_id is not None:
            item.role_runtime_session_id = str(role_runtime_session_id or "").strip()
        if phase is not None:
            target_phase = coerce_phase(phase)
            validate_transition(previous_phase, target_phase)
            item.phase = target_phase
        if summary is not None:
            item.summary = summary
        if batch_id is not None:
            item.batch_id = str(batch_id or "").strip()
        if batch_index is not None:
            item.batch_index = int(batch_index)
        if deliverable_summary is not None:
            item.deliverable_summary = str(deliverable_summary or "").strip()
        if blocked_reason is not None:
            item.blocked_reason = str(blocked_reason or "").strip()
        if handoff_status is not None:
            item.handoff_status = str(handoff_status or "").strip()
        if continuation_source is not None:
            item.continuation_source = str(continuation_source or "").strip()
        if manager_role_id is not None:
            item.manager_role_id = str(manager_role_id or "").strip()
        if manager_seat_id is not None:
            item.manager_seat_id = str(manager_seat_id or "").strip()
        if claimed_by_role_runtime_session_id is not None:
            item.claimed_by_role_runtime_session_id = str(claimed_by_role_runtime_session_id or "").strip()
        if claimed_by_seat_id is not None:
            item.claimed_by_seat_id = str(claimed_by_seat_id or "").strip()
        if metadata_unset or metadata_updates:
            metadata = dict(item.metadata or {})
            for key in list(metadata_unset or []):
                metadata.pop(str(key), None)
            if metadata_updates:
                metadata.update(dict(metadata_updates))
            item.metadata = metadata
        item.updated_at = datetime.now()
        await self.save_delegation_work_item(item)
        return item

    async def claim_delegation_work_item_if_dispatchable(
        self,
        work_item_id: str,
        *,
        expected_phase: Phase | str,
        role_runtime_session_id: str,
        seat_id: str,
        task_id: str,
        work_item_revision: int = 0,
    ) -> DelegationWorkItem | None:
        """Atomically claim an unheld, unowned dispatchable WorkItem.

        The dispatcher may be operating on a snapshot loaded before a Stop or
        shutdown transition.  Keeping the phase, claim, queue, and durable
        hold predicates in the same UPDATE prevents that stale snapshot from
        resurrecting a suspended WorkItem.
        """

        phase = coerce_phase(expected_phase)
        dispatchable_phases = {
            Phase.READY,
            Phase.READY_FOR_REWORK,
            *IN_PROGRESS_PHASES,
        }
        if phase not in dispatchable_phases:
            return None
        if phase != Phase.RUNNING:
            validate_transition(phase, Phase.RUNNING)
        role_session_id = str(role_runtime_session_id or "").strip()
        claimed_task_id = str(task_id or "").strip()
        if not work_item_id or not role_session_id or not claimed_task_id:
            return None

        updated_at = datetime.now()
        db = self._require_db()
        cursor = await db.execute(
            """UPDATE delegation_work_items
               SET phase = ?,
                   role_runtime_session_id = ?,
                   claimed_by_role_runtime_session_id = ?,
                   claimed_by_seat_id = ?,
                   metadata = json_set(
                       COALESCE(NULLIF(metadata, ''), '{}'),
                       '$.claimed_by_role_session_id', ?,
                       '$.claimed_task_id', ?,
                       '$.claimed_work_item_revision', ?
                   ),
                   updated_at = ?
               WHERE work_item_id = ?
                 AND phase = ?
                 AND COALESCE(claimed_by_role_runtime_session_id, '') = ''
                 AND COALESCE(claimed_by_seat_id, '') = ''
                 AND COALESCE(json_extract(metadata, '$.claimed_by_role_session_id'), '') = ''
                 AND COALESCE(json_extract(metadata, '$.claimed_task_id'), '') = ''
                 AND COALESCE(json_extract(metadata, '$.dispatch_hold'), '') = ''
                 AND COALESCE(json_extract(metadata, '$.queued_behind_session'), '') = ''
                 AND COALESCE(
                       CAST(json_extract(metadata, '$.manager_mutation_revision') AS INTEGER),
                       0
                     ) = ?""",
            (
                Phase.RUNNING.value,
                role_session_id,
                role_session_id,
                str(seat_id or "").strip(),
                role_session_id,
                claimed_task_id,
                int(work_item_revision or 0),
                updated_at.isoformat(),
                str(work_item_id or "").strip(),
                phase.value,
                int(work_item_revision or 0),
            ),
        )
        await db.commit()
        if not (getattr(cursor, "rowcount", 0) or 0):
            return None
        persisted = await self.get_delegation_work_item(work_item_id)
        if persisted is None:
            return None
        persisted_metadata = dict(persisted.metadata or {})
        if (
            persisted.phase != Phase.RUNNING
            or str(persisted.claimed_by_role_runtime_session_id or "").strip()
            != role_session_id
            or str(persisted_metadata.get("claimed_by_role_session_id", "") or "").strip()
            != role_session_id
            or str(persisted_metadata.get("claimed_task_id", "") or "").strip()
            != claimed_task_id
            or str(persisted_metadata.get("dispatch_hold", "") or "").strip()
            or str(persisted_metadata.get("queued_behind_session", "") or "").strip()
        ):
            # Stop/shutdown may have won immediately after the CAS commit and
            # cleared this claim while adding a durable hold.  The committed
            # UPDATE is not permission to spawn once that newer state exists.
            return None
        # Do not fire the generic phase hooks here.  The executor's first
        # idempotent RUNNING transition performs projection.  Deferring it
        # avoids a post-commit hook racing after Stop and projecting the Task
        # back to RUNNING from an already-held WorkItem.
        return persisted

    async def apply_delegation_review_resolution(
        self,
        work_item_id: str,
        *,
        source_report_work_item_id: str,
        target_phase: Phase | str,
        blocked_reason: str,
        metadata_updates: dict[str, Any],
    ) -> DelegationWorkItem | None:
        """Atomically apply a manager verdict to its exact report generation.

        The phase predicate and latest-applied-report predicate live in the
        same SQLite UPDATE as the child phase + applied-stamp write. This
        prevents a late manager turn from crossing an AWAITING_HUMAN
        transition or approving an older report after a newer report landed.
        ``updated_at`` provides optimistic metadata concurrency; a few retries
        preserve unrelated same-phase updates without weakening either guard.
        """
        target = coerce_phase(target_phase)
        validate_transition(Phase.AWAITING_MANAGER_REVIEW, target)
        expected_source = str(source_report_work_item_id or "").strip()
        db = self._require_db()

        for _attempt in range(3):
            item = await self.get_delegation_work_item(work_item_id)
            if item is None or item.phase != Phase.AWAITING_MANAGER_REVIEW:
                return None
            metadata = dict(item.metadata or {})
            metadata.update(dict(metadata_updates or {}))
            if (
                self._metadata_has_work_item_projection_identity(metadata)
                or str(item.projection_id or "").strip()
                or str(item.kind or "").strip()
            ):
                metadata, _ = migrate_work_item_projection_metadata(
                    metadata,
                    projection_id_fallback=str(
                        item.projection_id or item.work_item_id or ""
                    ).strip(),
                    turn_type_fallback=str(item.kind or "").strip(),
                )
            previous_updated_at = item.updated_at.isoformat()
            updated_at = datetime.now()
            cursor = await db.execute(
                """UPDATE delegation_work_items
                   SET phase = ?, blocked_reason = ?, metadata = ?, updated_at = ?
                   WHERE work_item_id = ?
                     AND phase = ?
                     AND updated_at = ?
                     AND COALESCE((
                         SELECT report.work_item_id
                         FROM delegation_work_items AS report
                         WHERE report.parent_work_item_id = ?
                           AND report.kind = 'report'
                           AND json_extract(
                               report.metadata,
                               '$.report_target_work_item_id'
                           ) = ?
                           AND json_extract(
                               report.metadata,
                               '$.report_card_outcome'
                           ) = 'applied'
                         ORDER BY report.batch_index DESC,
                                  report.created_at DESC,
                                  report.work_item_id DESC
                         LIMIT 1
                     ), '') = ?""",
                (
                    target.value,
                    str(blocked_reason or ""),
                    _json_dumps(metadata),
                    updated_at.isoformat(),
                    work_item_id,
                    Phase.AWAITING_MANAGER_REVIEW.value,
                    previous_updated_at,
                    work_item_id,
                    work_item_id,
                    expected_source,
                ),
            )
            await db.commit()
            if not (getattr(cursor, "rowcount", 0) or 0):
                continue
            persisted = await self.get_delegation_work_item(work_item_id)
            if persisted is None:
                return None
            try:
                await on_phase_transition(
                    Phase.AWAITING_MANAGER_REVIEW,
                    target,
                    persisted,
                    store=self,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "on_phase_transition raised after review-resolution CAS"
                )
            return persisted
        return None

    async def reopen_approved_delegation_work_item_for_rework(
        self,
        work_item_id: str,
        *,
        target_phase: Phase | str = Phase.READY_FOR_REWORK,
        summary: str | None = None,
        deliverable_summary: str | None = "",
        blocked_reason: str | None = "",
        metadata_updates: dict[str, Any] | None = None,
        metadata_unset: list[str] | tuple[str, ...] | None = None,
        release_claim: bool = True,
    ) -> DelegationWorkItem | None:
        """Named bypass for invalidating approved work after manager/user feedback."""
        item = await self.get_delegation_work_item(work_item_id)
        if item is None:
            return None
        previous_phase = item.phase
        target = coerce_phase(target_phase)
        if previous_phase != Phase.APPROVED:
            raise InvalidPhaseTransition(
                f"executive rework can only reopen approved work items, got {previous_phase.value}"
            )
        if target not in {Phase.READY_FOR_REWORK, Phase.READY}:
            raise InvalidPhaseTransition(
                f"executive rework target must be ready_for_rework or ready, got {target.value}"
            )

        item.phase = target
        if summary is not None:
            item.summary = str(summary or "").strip()
        if deliverable_summary is not None:
            item.deliverable_summary = str(deliverable_summary or "").strip()
        if blocked_reason is not None:
            item.blocked_reason = str(blocked_reason or "").strip()
        if release_claim:
            item.claimed_by_role_runtime_session_id = ""
            item.claimed_by_seat_id = ""

        metadata = dict(item.metadata or {})
        for key in list(metadata_unset or []):
            metadata.pop(str(key), None)
        if metadata_updates:
            metadata.update(dict(metadata_updates))
        if (
            self._metadata_has_work_item_projection_identity(metadata)
            or str(item.projection_id or "").strip()
            or str(item.kind or "").strip()
        ):
            metadata, _ = migrate_work_item_projection_metadata(
                metadata,
                projection_id_fallback=str(item.projection_id or item.work_item_id or "").strip(),
                turn_type_fallback=str(item.kind or "").strip(),
            )
        item.metadata = metadata
        item.updated_at = datetime.now()

        db = self._require_db()
        await db.execute(
            """UPDATE delegation_work_items
               SET phase=?,
                   summary=?,
                   deliverable_summary=?,
                   blocked_reason=?,
                   claimed_by_role_runtime_session_id=?,
                   claimed_by_seat_id=?,
                   metadata=?,
                   updated_at=?
               WHERE work_item_id=?""",
            (
                item.phase.value,
                item.summary,
                item.deliverable_summary,
                item.blocked_reason,
                item.claimed_by_role_runtime_session_id,
                item.claimed_by_seat_id,
                _json_dumps(item.metadata),
                item.updated_at.isoformat(),
                item.work_item_id,
            ),
        )
        await db.commit()
        try:
            await on_phase_transition(previous_phase, target, item, store=self)
        except Exception:
            logger.opt(exception=True).debug("on_phase_transition raised during approved work-item reopen")
        return item

    async def amend_delegation_work_item(
        self,
        work_item_id: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        kind: str | None = None,
        role_id: str | None = None,
        seat_id: str | None = None,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        role_runtime_session_id: str | None = None,
        claimed_by_role_runtime_session_id: str | None = None,
        claimed_by_seat_id: str | None = None,
        dependency_work_item_ids: list[str] | None = None,
        phase: Phase | str | None = None,
        metadata_set: dict[str, Any] | None = None,
        metadata_unset: list[str] | None = None,
    ) -> DelegationWorkItem | None:
        item = await self.get_delegation_work_item(work_item_id)
        if item is None:
            return None
        previous_phase = item.phase
        if title is not None:
            item.title = str(title or "").strip()
        if summary is not None:
            item.summary = str(summary or "").strip()
        if kind is not None:
            item.kind = str(kind or "").strip() or item.kind
        if role_id is not None:
            item.role_id = str(role_id or "").strip()
        if seat_id is not None:
            item.seat_id = str(seat_id or "").strip()
        if team_instance_id is not None:
            item.team_instance_id = str(team_instance_id or "").strip()
        if team_id is not None:
            item.team_id = str(team_id or "").strip()
        if role_runtime_session_id is not None:
            item.role_runtime_session_id = str(role_runtime_session_id or "").strip()
        if claimed_by_role_runtime_session_id is not None:
            item.claimed_by_role_runtime_session_id = str(claimed_by_role_runtime_session_id or "").strip()
        if claimed_by_seat_id is not None:
            item.claimed_by_seat_id = str(claimed_by_seat_id or "").strip()
        if phase is not None:
            target_phase = coerce_phase(phase)
            validate_transition(previous_phase, target_phase)
            item.phase = target_phase

        metadata = dict(item.metadata or {})
        if dependency_work_item_ids is not None:
            raw_dependencies = (
                [dependency_work_item_ids]
                if isinstance(dependency_work_item_ids, str)
                else list(dependency_work_item_ids or [])
            )
            metadata["dependency_work_item_ids"] = [
                str(dep).strip()
                for dep in raw_dependencies
                if str(dep).strip()
            ]
        if metadata_set:
            metadata.update(dict(metadata_set))
        for key in list(metadata_unset or []):
            metadata.pop(str(key), None)
        item.metadata = metadata
        item.updated_at = datetime.now()
        await self.save_delegation_work_item(item)
        return item

    async def replace_work_item_dependency(
        self,
        run_id: str,
        old_work_item_id: str,
        new_work_item_ids: list[str],
    ) -> list[DelegationWorkItem]:
        rid = str(run_id or "").strip()
        old_id = str(old_work_item_id or "").strip()
        raw_replacements = (
            [new_work_item_ids]
            if isinstance(new_work_item_ids, str)
            else list(new_work_item_ids or [])
        )
        replacements = [
            str(item).strip()
            for item in raw_replacements
            if str(item).strip()
        ]
        if not rid or not old_id:
            return []
        updated: list[DelegationWorkItem] = []
        for item in await self.list_delegation_work_items(rid):
            metadata = dict(item.metadata or {})
            dependency_ids = [
                str(dep).strip()
                for dep in list(metadata.get("dependency_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            waiting_ids = [
                str(dep).strip()
                for dep in list(metadata.get("waiting_on_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            if old_id not in dependency_ids and old_id not in waiting_ids:
                continue
            rewritten: list[str] = []
            for dep in dependency_ids:
                if dep == old_id:
                    rewritten.extend(replacements)
                else:
                    rewritten.append(dep)
            deduped = list(dict.fromkeys(dep for dep in rewritten if dep))
            rewritten_waiting: list[str] = []
            for dep in waiting_ids:
                if dep == old_id:
                    rewritten_waiting.extend(replacements)
                else:
                    rewritten_waiting.append(dep)
            item.metadata = {
                **metadata,
                "dependency_work_item_ids": deduped,
                "waiting_on_work_item_ids": list(
                    dict.fromkeys(dep for dep in rewritten_waiting if dep)
                ),
                "dependency_rewritten_from_work_item_id": old_id,
                "dependency_rewritten_at": datetime.now().isoformat(),
            }
            item.updated_at = datetime.now()
            await self.save_delegation_work_item(item)
            updated.append(item)
        return updated

    async def release_manager_work_items(
        self,
        run_id: str,
        *,
        manager_seat_id: str,
        work_item_ids: list[str] | None = None,
        parent_work_item_id: str | None = None,
        release_note: str = "",
        released_by_message_id: str = "",
        action_hint: str = "",
    ) -> list[DelegationWorkItem]:
        target_ids = {
            str(item).strip()
            for item in list(work_item_ids or [])
            if str(item).strip()
        }
        candidates = await self.list_manager_board(
            run_id,
            manager_seat_id=manager_seat_id,
            parent_work_item_id=parent_work_item_id,
        )
        released: list[DelegationWorkItem] = []
        for item in candidates:
            if target_ids and item.work_item_id not in target_ids:
                continue
            metadata_updates = {
                "last_release_note": str(release_note or "").strip(),
                "last_released_by_message_id": str(released_by_message_id or "").strip(),
                "last_release_action_hint": str(action_hint or "").strip(),
                "released_at": datetime.now().isoformat(),
            }
            target_phase = Phase.READY if item.phase == Phase.QUEUED else item.phase
            updated = await self.update_delegation_work_item(
                item.work_item_id,
                phase=target_phase,
                metadata_updates=metadata_updates,
            )
            if updated is not None:
                released.append(updated)
        return released

    async def rollup_manager_board(
        self,
        run_id: str,
        *,
        manager_seat_id: str,
        parent_work_item_id: str,
        summary: str = "",
        phase: Phase | str | None = None,
        blocked_reason: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rollup = await self.summarize_parent_status(
            run_id,
            manager_seat_id=manager_seat_id,
            parent_work_item_id=parent_work_item_id,
        )
        parent = await self.get_delegation_work_item(parent_work_item_id)
        if parent is not None:
            merged_updates = {
                "manager_board_rollup": dict(rollup),
                "manager_board_rollup_updated_at": datetime.now().isoformat(),
            }
            if summary:
                merged_updates["manager_board_rollup_summary"] = str(summary).strip()
            if metadata_updates:
                merged_updates.update(dict(metadata_updates))
            await self.update_delegation_work_item(
                parent_work_item_id,
                phase=phase,
                deliverable_summary=summary or None,
                blocked_reason=blocked_reason,
                metadata_updates=merged_updates,
            )
            refreshed_parent = await self.get_delegation_work_item(parent_work_item_id)
            if refreshed_parent is not None:
                rollup["parent_phase"] = refreshed_parent.phase.value
                rollup["parent_column"] = kanban_column(refreshed_parent.phase)
                rollup["parent_deliverable_summary"] = str(getattr(refreshed_parent, "deliverable_summary", "") or "").strip()
                rollup["parent_blocked_reason"] = str(getattr(refreshed_parent, "blocked_reason", "") or "").strip()
        return rollup
