"""SQLite 持久化儲存層 — 任務、協作狀態和可觀測性資料的資料庫。

職責說明：
    提供所有領域物件（Task、DelegationWorkItem、ExecutionCheckpoint 等）的
    CRUD 操作，基於 SQLite 實現輕量級持久化。

關聯關係：
    - 被 opc/engine.py 的 OPCEngine 建立和使用
    - 被 opc/cli/app.py 的 CLI 命令查詢
    - 被 opc/plugins/office_ui/ 的 API 層驅動

使用範例：
    store = OPCStore(db_path)
    await store.save_task(task)
    task = await store.get_task(task_id)
"""

from __future__ import annotations  # 啟用延遲型別註解評估

import json  # 標準庫：JSON 序列化（metadata 欄位）
import inspect  # 標準庫：introspection（函數簽名檢查）
import uuid  # 標準庫：UUID 產生
from dataclasses import asdict, is_dataclass  # 標準庫：資料類別序列化
from datetime import datetime, timedelta  # 標準庫：日期時間
from enum import Enum  # 標準庫：列舉
from pathlib import Path  # 標準庫：路徑操作
import sqlite3  # 標準庫：SQLite 資料庫
from typing import Any  # 標準庫：型別註解

from loguru import logger  # 第三方庫：結構化日誌

from opc.core.models import (  # 領域資料模型
    AgentCompactionRecord,
    AgentMemorySnapshotRecord,
    AgentMessage,
    ApprovalDecision,
    ArtifactRecord,
    CommsSemanticType,
    CommsState,
    CommsTransportKind,
    CostEvent,
    DelegationCell,
    DelegationEvent,
    DelegationRoleSession,
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    ExternalSession,
    Goal,
    GoalLevel,
    GoalStatus,
    HandoffRecord,
    MeetingRoom,
    MeetingStatus,
    MessageUrgency,
    MessageStatus,
    OrgAgent,
    OrgSnapshot,
    Organization,
    OPCEvent,
    ReorgEventKind,
    ReorgEventRecord,
    ReorgProposal,
    ReorgProposalStatus,
    ReorgRiskLevel,
    ReorgScope,
    RoleCollaboration,
    RoleCommunicationRecord,
    RoleMemoryRecord,
    RoleOrientation,
    RoleOutputMetrics,
    RolePersonality,
    RoleResourceUsage,
    RoleRuntimeSession,
    RoleSkillProficiency,
    RoleTaskAssignment,
    RoleWorkRecord,
    SeatState,
    SessionCompactionRecord,
    SessionMemorySnapshotRecord,
    SessionLinkRecord,
    SessionMessageRecord,
    SessionPartRecord,
    SessionRecord,
    TeamInstance,
    Task,
    TaskStatus,
    WorkItemDecisionRecord,
    normalize_role_runtime_status,
)
import aiosqlite
from opc.core.models import Phase  # Phase 狀態機
from opc.core.transcript_visibility import (  # 對話可見性
    normalize_transcript_detail_level,
    transcript_visibility_sql,
)
from opc.layer2_organization.phase import (  # Phase 狀態機（Layer 2）
    DONE_PHASES,
    IN_PROGRESS_PHASES,
    IN_REVIEW_PHASES,
    InvalidPhaseTransition,
    TODO_PHASES,
    coerce_phase,
    is_stale_claim_releasable,
    is_terminal,
    kanban_column,
    on_phase_transition,
    validate_transition,
)
from opc.layer2_organization.work_item_identity import (  # 工作項目身份
    WORK_ITEM_PROJECTION_ID_KEY,
    WORK_ITEM_TURN_TYPE_KEY,
    migrate_work_item_projection_metadata,
    projection_id_for_work_item,
)
from opc.layer2_organization.work_item_links import (  # 工作項目連結
    linked_work_item_id_for_task,
    set_linked_work_item_id,
)
from opc.layer2_organization.work_item_runtime import (  # 工作項目運行時
    is_work_item_runtime_metadata,
    migrate_work_item_runtime_metadata,
)
from opc.layer2_organization.work_item_runtime_invariants import (  # 工作項目運行時不變量
    validate_work_item_runtime_projection,
)


from opc.database._utils import _json_dumps, _json_loads


class _SQLiteCursorAdapter:
    """SQLite 游標的非同步適配器 — 包裝同步 sqlite3.Cursor 為 async 介面。"""
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    async def __aenter__(self) -> "_SQLiteCursorAdapter":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._cursor.close()
        return False

    async def fetchone(self) -> Any:
        return self._cursor.fetchone()

    async def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    @property
    def description(self) -> Any:
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount


class _SQLiteExecuteResult:
    """SQLite 執行結果的非同步適配器 — 支援 await 和 async with 兩種用法。"""
    def __init__(self, connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> None:
        self._connection = connection
        self._sql = sql
        self._params = tuple(params)
        self._cursor: sqlite3.Cursor | None = None

    def __await__(self):
        async def _run() -> _SQLiteCursorAdapter:
            cursor = self._connection.cursor()
            cursor.execute(self._sql, self._params)
            return _SQLiteCursorAdapter(cursor)

        return _run().__await__()

    async def __aenter__(self) -> _SQLiteCursorAdapter:
        cursor = self._connection.cursor()
        cursor.execute(self._sql, self._params)
        self._cursor = cursor
        return _SQLiteCursorAdapter(cursor)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._cursor is not None:
            self._cursor.close()
        return False


class _SQLiteConnectionAdapter:
    """同步 sqlite3 的輕量非同步外觀。

    注意：所有操作在呼叫執行緒上同步執行，會短暫阻塞事件循環。
    對於 WAL 模式的本地 SQLite 資料庫，每次操作延遲通常 < 1ms，
    對當前工作負載可接受。未來遷移到 aiosqlite 可完全消除阻塞。
    """

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        # NORMAL synchronous mode is safe with WAL and avoids a full fsync on
        # every commit, significantly reducing write latency.
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def execute(self, sql: str, parameters: tuple[Any, ...] | list[Any] = ()) -> _SQLiteExecuteResult:
        return _SQLiteExecuteResult(self._conn, sql, parameters)

    async def executescript(self, script: str) -> None:
        self._conn.executescript(script)

    async def commit(self) -> None:
        self._conn.commit()

    async def rollback(self) -> None:
        self._conn.rollback()

    async def close(self) -> None:
        try:
            # Checkpoint WAL to main database file before closing to release
            # all file handles on Windows (prevents PermissionError on temp cleanup).
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        self._conn.close()



# --- Mixin 匯入（按功能分組的儲存層）---
from opc.database._store_tasks import TaskStoreMixin  # 任務 CRUD
from opc.database._store_work_items import WorkItemStoreMixin  # 工作項目 CRUD
from opc.database._store_delegation import DelegationStoreMixin  # 委派執行 CRUD
from opc.database._store_sessions import SessionStoreMixin  # 工作階段 CRUD
from opc.database._store_collaboration import CollaborationStoreMixin  # 協作狀態 CRUD
from opc.database._store_shared_files import SharedFileStoreMixin  # 共用文件庫 CRUD

class OPCStore(
    TaskStoreMixin,
    WorkItemStoreMixin,
    DelegationStoreMixin,
    SessionStoreMixin,
    CollaborationStoreMixin,
    SharedFileStoreMixin,
):
    """OPC 資料的非同步 SQLite 儲存層（WAL 模式支援並發）。

    職責說明：
        透過 Mixin 組合提供所有領域物件的 CRUD 操作。
        使用 WAL 模式允許讀寫並發，適合本地單使用者場景。

    關聯關係：
        - 被 OPCEngine 在 initialize() 時建立
        - 被所有 layer 模組透過 engine.store 存取
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self.project_id = self._infer_project_id_from_db_path(db_path)
        self._db: _SQLiteConnectionAdapter | None = None
        # Fix 5 PR3 feature flag mirrored onto the store so phase hooks
        # (which receive ``store`` but not the top-level OPCConfig) can
        # consult it cheaply. Engine sets this during init from
        # ``OPCConfig.org.role_serial_queue_enabled``. Tests can flip it
        # directly to exercise both branches.
        self.role_serial_queue_enabled: bool = True

    @staticmethod
    def _infer_project_id_from_db_path(db_path: str | Path) -> str | None:
        path = Path(db_path)
        try:
            if path.name == "tasks.db" and path.parent.parent.name == "projects":
                return path.parent.name or None
        except Exception:
            return None
        return None

    def _assert_project_write_scope(
        self,
        value: str | None,
        *,
        operation: str,
        entity: str,
    ) -> None:
        store_project_id = str(self.project_id or "").strip()
        entity_project_id = str(value or "").strip()
        if not store_project_id or not entity_project_id:
            return
        if entity_project_id != store_project_id:
            raise RuntimeError(
                f"{operation} rejected cross-project {entity} write: "
                f"store_project={store_project_id!r} entity_project={entity_project_id!r} "
                f"db_path={self.db_path!r}"
            )

    @property
    def is_ready(self) -> bool:
        """Whether the SQLite connection has been initialized."""
        return self._db is not None

    def _require_db(self) -> aiosqlite.Connection:
        """Return the active DB connection or raise a descriptive error."""
        if self._db is None:
            raise RuntimeError(
                f"OPCStore database not initialized (db_path={self.db_path!r}). "
                "Call await store.initialize() before using store methods."
            )
        return self._db

    async def ensure_ready(self) -> None:
        """Initialize the store if not already connected."""
        if self._db is None:
            await self.initialize()

    async def initialize(self, *, run_startup_maintenance: bool = True) -> None:
        """Open the store.

        ``run_startup_maintenance`` is reserved for the owning OpenOPC runtime
        process. Lightweight collaboration clients such as ``opc-collab`` open
        the already-initialized project DB to service one tool call; they must
        not run schema migrations or stale-claim sweeps as a side effect of a
        read-only command.
        """
        if run_startup_maintenance:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        elif not Path(self.db_path).exists():
            raise FileNotFoundError(f"OPCStore database does not exist: {self.db_path}")
        self._db = _SQLiteConnectionAdapter(self.db_path)
        if not run_startup_maintenance:
            return
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        await self._ensure_schema()
        await self._sweep_stale_claims()
        await self._migrate_drop_runtime_topology_version_columns()
        # Fix 5 PR2: merge every (run_id, role_id) group into a single
        # canonical row with PK = ``role-runtime::{run_id}::{role_id}``.
        # Combines inbox, memory_slices, list fields, and picks the most
        # recent state for scalar fields — so historical data survives
        # the collapse. Subsumes the old Fix-2 duplicate-collapse and
        # _no_team-sentinel migrations (the 3-segment canonical form
        # eliminates both problems at the source).
        await self._migrate_role_sessions_merge_by_role()
        await self._migrate_work_item_runtime_metadata()
        await self._migrate_work_item_projection_metadata()
        await self._purge_cross_project_runtime_rows()
        await self._validate_work_item_runtime_links()
        await self._ensure_indexes()

    async def _table_columns(self, table: str) -> list[str]:
        assert self._db is not None
        if not await self._table_exists(table):
            return []
        async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        return [str(row[1]) for row in rows]

    async def _purge_cross_project_runtime_rows(self) -> dict[str, int]:
        """Remove rows that were written into the wrong project-scoped DB.

        Project databases under ``.opc/projects/<project_id>/tasks.db`` are
        single-project stores. A row whose explicit ``project_id`` names a
        different project is corruption from a prior mutable-store race; keeping
        it can break canonical WorkItem validation and project switching.
        """
        if self._db is None:
            return {}
        project_id = str(self.project_id or "").strip()
        if not project_id:
            return {}
        deleted: dict[str, int] = {}
        for table in ("external_sessions", "runtime_sessions", "tasks"):
            if not await self._table_exists(table):
                continue
            columns = set(await self._table_columns(table))
            if "project_id" not in columns:
                continue
            cursor = await self._db.execute(
                f"""DELETE FROM {table}
                    WHERE project_id IS NOT NULL
                      AND TRIM(project_id) != ''
                      AND project_id != ?""",
                (project_id,),
            )
            count = int(getattr(cursor, "rowcount", 0) or 0)
            if count:
                deleted[table] = count
        if deleted:
            await self._db.commit()
            logger.warning(
                "Purged cross-project runtime rows from project store: "
                f"project_id={project_id} db_path={self.db_path} deleted={deleted}"
            )
        return deleted

    async def _migrate_drop_runtime_topology_version_columns(self) -> None:
        # runtime_topology_version is no longer tracked: reorg never increments it,
        # snapshots never read it, and gates aren't keyed off it. Drop the
        # vestigial columns from legacy DBs (idempotent).
        assert self._db
        for table, column in (
            ("reorg_proposals", "old_runtime_topology_version"),
            ("reorg_proposals", "new_runtime_topology_version"),
            ("org_snapshots", "runtime_topology_version"),
        ):
            async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
                rows = await cursor.fetchall()
            cols = {row[1] for row in rows}
            if column in cols:
                try:
                    await self._db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                except Exception as exc:
                    logger.warning(f"Failed to drop {table}.{column}: {exc}")
        await self._db.commit()

    async def _migrate_work_item_runtime_metadata(self) -> dict[str, int]:
        """Normalize canonical company work-item runtime metadata."""
        if self._db is None:
            return {}

        targets = (
            ("tasks", "id"),
            ("delegation_runs", "run_id"),
            ("delegation_cells", "cell_id"),
            ("delegation_work_items", "work_item_id"),
            ("team_instances", "team_instance_id"),
            ("seat_states", "seat_state_id"),
            ("role_runtime_sessions", "role_session_id"),
            ("delegation_role_sessions", "role_session_id"),
        )
        stats: dict[str, int] = {}
        for table, key_column in targets:
            stats[table] = await self._migrate_work_item_runtime_metadata_table(
                table=table,
                key_column=key_column,
            )
        changed = sum(stats.values())
        if changed:
            await self._db.commit()
            logger.info(f"work-item runtime metadata migration: updated {changed} rows")
        return stats

    async def _migrate_work_item_runtime_metadata_table(
        self,
        *,
        table: str,
        key_column: str,
    ) -> int:
        assert self._db is not None
        if not await self._table_exists(table):
            return 0
        async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if key_column not in columns or "metadata" not in columns:
            return 0

        async with self._db.execute(
            f"""SELECT {key_column}, metadata
                FROM {table}
                WHERE metadata LIKE '%work_item_runtime%'"""
        ) as cursor:
            rows = await cursor.fetchall()

        updated = 0
        for row_id, metadata_json in rows:
            try:
                metadata = _json_loads(metadata_json, {})
            except Exception as exc:
                logger.warning(
                    f"work-item runtime metadata migration: skipping invalid "
                    f"{table}.{key_column}={row_id}: {exc}"
                )
                continue
            if not isinstance(metadata, dict):
                continue
            migrated, changed = migrate_work_item_runtime_metadata(metadata)
            if not changed:
                continue
            await self._db.execute(
                f"UPDATE {table} SET metadata=? WHERE {key_column}=?",
                (_json_dumps(migrated), row_id),
            )
            updated += 1
        return updated

    async def _migrate_work_item_projection_metadata(self) -> dict[str, int]:
        """Normalize canonical work-item projection identity metadata."""
        if self._db is None:
            return {}
        stats = {
            "tasks": await self._migrate_task_projection_metadata(),
            "delegation_work_items": await self._migrate_delegation_work_item_projection_metadata(),
        }
        changed = sum(stats.values())
        if changed:
            await self._db.commit()
            logger.info(f"work-item projection metadata migration: updated {changed} rows")
        return stats

    async def _migrate_task_projection_metadata(self) -> int:
        assert self._db is not None
        if not await self._table_exists("tasks"):
            return 0
        async with self._db.execute("PRAGMA table_info(tasks)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "id" not in columns or "metadata" not in columns:
            return 0
        async with self._db.execute(
            """SELECT id, metadata
               FROM tasks
               WHERE metadata LIKE '%work_item_projection_id%'
                  OR metadata LIKE '%work_item_turn_type%'"""
        ) as cursor:
            rows = await cursor.fetchall()
        updated = 0
        for task_id, metadata_json in rows:
            try:
                metadata = _json_loads(metadata_json, {})
            except Exception as exc:
                logger.warning(
                    f"work-item projection metadata migration: skipping invalid task {task_id}: {exc}"
                )
                continue
            if not isinstance(metadata, dict):
                continue
            migrated, changed = migrate_work_item_projection_metadata(metadata)
            if not changed:
                continue
            await self._db.execute(
                "UPDATE tasks SET metadata=? WHERE id=?",
                (_json_dumps(migrated), task_id),
            )
            updated += 1
        return updated

    async def _migrate_delegation_work_item_projection_metadata(self) -> int:
        assert self._db is not None
        if not await self._table_exists("delegation_work_items"):
            return 0
        async with self._db.execute("PRAGMA table_info(delegation_work_items)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        required = {"work_item_id", "projection_id", "kind", "metadata"}
        if not required.issubset(columns):
            return 0
        async with self._db.execute(
            """SELECT work_item_id, projection_id, kind, metadata
               FROM delegation_work_items
               WHERE COALESCE(projection_id, '') != ''
                  OR COALESCE(kind, '') != ''
                  OR metadata LIKE '%work_item_projection_id%'
                  OR metadata LIKE '%work_item_turn_type%'"""
        ) as cursor:
            rows = await cursor.fetchall()
        updated = 0
        for work_item_id, projection_id, kind, metadata_json in rows:
            try:
                metadata = _json_loads(metadata_json, {})
            except Exception as exc:
                logger.warning(
                    f"work-item projection metadata migration: skipping invalid work item {work_item_id}: {exc}"
                )
                continue
            if not isinstance(metadata, dict):
                continue
            migrated, changed = migrate_work_item_projection_metadata(
                metadata,
                projection_id_fallback=str(projection_id or work_item_id or "").strip(),
                turn_type_fallback=str(kind or "execute").strip(),
            )
            if not changed:
                continue
            await self._db.execute(
                "UPDATE delegation_work_items SET metadata=? WHERE work_item_id=?",
                (_json_dumps(migrated), work_item_id),
            )
            updated += 1
        return updated

    async def _create_tables(self) -> None:
        assert self._db
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                parent_session_id TEXT,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                assigned_to TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 5,
                dependencies TEXT DEFAULT '[]',
                execution_lock INTEGER DEFAULT 0,
                context_snapshot TEXT DEFAULT '{}',
                assigned_external_agent TEXT,
                created_at TEXT NOT NULL,
                deadline TEXT,
                result TEXT,
                parent_id TEXT,
                project_id TEXT DEFAULT 'default',
                tags TEXT DEFAULT '[]',
                comments TEXT DEFAULT '[]',
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                metadata TEXT DEFAULT '{}',
                org_id TEXT,
                goal_id TEXT,
                checkout_run_id TEXT,
                execution_locked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS agent_messages (
                msg_id TEXT PRIMARY KEY,
                msg_type TEXT NOT NULL,
                from_agent TEXT NOT NULL,
                to_agents TEXT NOT NULL,
                subject TEXT DEFAULT '',
                body TEXT DEFAULT '',
                context_ref TEXT,
                urgency TEXT DEFAULT 'normal',
                reply_needed INTEGER DEFAULT 0,
                requires_ack INTEGER DEFAULT 0,
                timeout_action TEXT,
                reply_to_msg_id TEXT,
                task_id TEXT,
                status TEXT DEFAULT 'sent',
                timestamp TEXT NOT NULL,
                processed_at TEXT,
                metadata TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS meetings (
                room_id TEXT PRIMARY KEY,
                task_id TEXT,
                topic TEXT NOT NULL,
                participants TEXT NOT NULL,
                shared_context TEXT DEFAULT '',
                agenda TEXT DEFAULT '[]',
                max_rounds INTEGER DEFAULT 5,
                decision_owner TEXT DEFAULT 'coordinator',
                status TEXT DEFAULT 'open',
                decision_method TEXT DEFAULT '',
                current_round INTEGER DEFAULT 0,
                pending_participants TEXT DEFAULT '[]',
                consensus TEXT DEFAULT '{}',
                outcome TEXT,
                transcript TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                deadline_at TEXT
            );

            CREATE TABLE IF NOT EXISTS work_item_decisions (
                decision_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                task_id TEXT,
                role_id TEXT DEFAULT '',
                projection_id TEXT DEFAULT '',
                category TEXT DEFAULT 'general',
                summary TEXT DEFAULT '',
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifact_records (
                artifact_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                task_id TEXT,
                projection_id TEXT DEFAULT '',
                role_id TEXT DEFAULT '',
                name TEXT DEFAULT '',
                artifact_type TEXT DEFAULT 'generic',
                location TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_memory (
                memory_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                scope TEXT DEFAULT 'project',
                summary TEXT DEFAULT '',
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_work_records (
                record_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                work_item_id TEXT DEFAULT '',
                title TEXT DEFAULT '',
                status TEXT DEFAULT 'in_progress',
                collaborators TEXT DEFAULT '[]',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                duration_seconds REAL DEFAULT 0.0,
                summary TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS role_orientations (
                orientation_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                goals TEXT DEFAULT '[]',
                capabilities TEXT DEFAULT '[]',
                values_list TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_personalities (
                personality_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                traits TEXT DEFAULT '{}',
                interaction_style TEXT DEFAULT '',
                behavior_notes TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_collaborations (
                collab_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                partner_role_id TEXT NOT NULL,
                interaction_count INTEGER DEFAULT 0,
                last_interaction_at TEXT,
                quality_score REAL DEFAULT 0.0,
                notes TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS role_skills (
                skill_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                category TEXT DEFAULT 'technical',
                skill_name TEXT NOT NULL,
                level REAL DEFAULT 0.0,
                learning_goals TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_output_metrics (
                metrics_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                week_label TEXT DEFAULT '',
                tasks_completed INTEGER DEFAULT 0,
                quality_score REAL DEFAULT 0.0,
                avg_duration REAL DEFAULT 0.0,
                rework_count INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_resource_usage (
                usage_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                period TEXT DEFAULT '',
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                duration_seconds REAL DEFAULT 0.0,
                model_breakdown TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_task_assignments (
                assignment_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                work_item_id TEXT DEFAULT '',
                title TEXT DEFAULT '',
                column_name TEXT DEFAULT 'upcoming',
                priority INTEGER DEFAULT 0,
                depends_on TEXT DEFAULT '[]',
                blocked_reason TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_communications (
                comm_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                role_id TEXT NOT NULL,
                comm_type TEXT DEFAULT 'discussion',
                title TEXT DEFAULT '',
                content TEXT DEFAULT '',
                participants TEXT DEFAULT '[]',
                outcome TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS handoff_records (
                handoff_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT,
                task_id TEXT,
                from_role TEXT DEFAULT '',
                to_role TEXT DEFAULT '',
                source_projection_id TEXT DEFAULT '',
                target_projection_id TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                payload TEXT DEFAULT '{}',
                requires_ack INTEGER DEFAULT 0,
                status TEXT DEFAULT 'sent',
                received_at TEXT,
                acked_at TEXT,
                accepted_at TEXT,
                rejected_at TEXT,
                response_summary TEXT DEFAULT '',
                ack_message_id TEXT,
                response_message_id TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delegation_runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                company_profile TEXT DEFAULT 'corporate',
                execution_model TEXT DEFAULT 'recursive_delegation',
                final_decider_role_id TEXT DEFAULT '',
                top_level_role_ids TEXT DEFAULT '[]',
                status TEXT DEFAULT 'pending',
                lifecycle_status TEXT DEFAULT 'active',
                current_revision INTEGER DEFAULT 1,
                latest_deliverable_summary TEXT DEFAULT '',
                recovery_pointer TEXT DEFAULT '{}',
                project_dossier TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delegation_cells (
                cell_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                manager_role_id TEXT DEFAULT '',
                member_role_ids TEXT DEFAULT '[]',
                status TEXT DEFAULT 'idle',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delegation_work_items (
                work_item_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                cell_id TEXT NOT NULL,
                team_instance_id TEXT DEFAULT '',
                team_id TEXT DEFAULT '',
                role_id TEXT DEFAULT '',
                seat_id TEXT DEFAULT '',
                seat_state_id TEXT DEFAULT '',
                role_runtime_session_id TEXT DEFAULT '',
                parent_work_item_id TEXT,
                source_role_id TEXT,
                source_seat_id TEXT,
                title TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                kind TEXT DEFAULT 'execute',
                projection_id TEXT DEFAULT '',
                phase TEXT NOT NULL DEFAULT 'ready',
                batch_id TEXT DEFAULT '',
                batch_index INTEGER DEFAULT 0,
                deliverable_summary TEXT DEFAULT '',
                blocked_reason TEXT DEFAULT '',
                handoff_status TEXT DEFAULT 'pending',
                continuation_source TEXT DEFAULT '',
                manager_role_id TEXT DEFAULT '',
                manager_seat_id TEXT DEFAULT '',
                claimed_by_role_runtime_session_id TEXT DEFAULT '',
                claimed_by_seat_id TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS work_item_runtime_links (
                work_item_id TEXT PRIMARY KEY,
                runtime_task_id TEXT NOT NULL UNIQUE,
                link_kind TEXT DEFAULT 'primary',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(work_item_id) REFERENCES delegation_work_items(work_item_id) ON DELETE CASCADE,
                FOREIGN KEY(runtime_task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS delegation_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                work_item_id TEXT,
                cell_id TEXT,
                role_id TEXT,
                event_type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delegation_role_sessions (
                role_session_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                team_instance_id TEXT DEFAULT '',
                team_id TEXT DEFAULT '',
                role_id TEXT NOT NULL,
                seat_id TEXT DEFAULT '',
                seat_state_id TEXT DEFAULT '',
                employee_id TEXT DEFAULT '',
                focused_work_item_id TEXT DEFAULT '',
                background_work_item_ids TEXT DEFAULT '[]',
                manager_role_ids TEXT DEFAULT '[]',
                manager_seat_ids TEXT DEFAULT '[]',
                seat_ids TEXT DEFAULT '[]',
                adapter_session_state TEXT DEFAULT '{}',
                inbox_state TEXT DEFAULT '{}',
                memory_slices_by_work_item TEXT DEFAULT '{}',
                resume_state TEXT DEFAULT '{}',
                current_work_item TEXT DEFAULT '{}',
                latest_notification TEXT DEFAULT '{}',
                manager_digest TEXT DEFAULT '{}',
                status TEXT DEFAULT 'idle',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS team_instances (
                team_instance_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                team_id TEXT NOT NULL,
                session_id TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                seat_ids TEXT DEFAULT '[]',
                role_ids TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seat_states (
                seat_state_id TEXT PRIMARY KEY,
                team_instance_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                team_id TEXT NOT NULL,
                seat_id TEXT NOT NULL,
                role_id TEXT DEFAULT '',
                employee_id TEXT DEFAULT '',
                member_session_id TEXT DEFAULT '',
                role_runtime_session_id TEXT DEFAULT '',
                status TEXT DEFAULT 'idle',
                resident_status TEXT DEFAULT 'idle',
                current_task_id TEXT DEFAULT '',
                current_work_item_id TEXT DEFAULT '',
                manager_role_id TEXT DEFAULT '',
                manager_seat_id TEXT DEFAULT '',
                manager_role_ids TEXT DEFAULT '[]',
                manager_seat_ids TEXT DEFAULT '[]',
                inbox_state TEXT DEFAULT '{}',
                resume_state TEXT DEFAULT '{}',
                current_work_item TEXT DEFAULT '{}',
                latest_notification TEXT DEFAULT '{}',
                manager_digest TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS role_runtime_sessions (
                role_session_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                team_instance_id TEXT DEFAULT '',
                team_id TEXT DEFAULT '',
                role_id TEXT NOT NULL,
                seat_id TEXT DEFAULT '',
                seat_state_id TEXT DEFAULT '',
                employee_id TEXT DEFAULT '',
                focused_work_item_id TEXT DEFAULT '',
                background_work_item_ids TEXT DEFAULT '[]',
                manager_role_ids TEXT DEFAULT '[]',
                manager_seat_ids TEXT DEFAULT '[]',
                seat_ids TEXT DEFAULT '[]',
                adapter_session_state TEXT DEFAULT '{}',
                inbox_state TEXT DEFAULT '{}',
                memory_slices_by_work_item TEXT DEFAULT '{}',
                resume_state TEXT DEFAULT '{}',
                current_work_item TEXT DEFAULT '{}',
                latest_notification TEXT DEFAULT '{}',
                manager_digest TEXT DEFAULT '{}',
                status TEXT DEFAULT 'idle',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reorg_proposals (
                proposal_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT,
                task_id TEXT,
                initiated_by TEXT DEFAULT 'owner',
                source_role_id TEXT DEFAULT '',
                scope TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                rationale TEXT DEFAULT '',
                user_confirmation_required INTEGER DEFAULT 1,
                old_org_version INTEGER DEFAULT 1,
                new_org_version INTEGER DEFAULT 1,
                changeset TEXT DEFAULT '{}',
                migration_plan TEXT DEFAULT '{}',
                impact_summary TEXT DEFAULT '{}',
                approval_notes TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS org_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                org_version INTEGER DEFAULT 1,
                company_name TEXT DEFAULT '',
                topology TEXT DEFAULT '',
                roles TEXT DEFAULT '[]',
                company_profile TEXT DEFAULT 'corporate',
                active_tasks TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reorg_events (
                event_id TEXT PRIMARY KEY,
                proposal_id TEXT DEFAULT '',
                project_id TEXT DEFAULT 'default',
                event_kind TEXT NOT NULL,
                summary TEXT DEFAULT '',
                details TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cost_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                agent_id TEXT,
                model TEXT,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                cost REAL DEFAULT 0.0,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS approval_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                project_id TEXT DEFAULT 'default',
                action_kind TEXT NOT NULL,
                action_name TEXT NOT NULL,
                target_agent TEXT DEFAULT '',
                decision_action TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                rationale TEXT DEFAULT '',
                policy_source TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_sessions (
                session_key TEXT PRIMARY KEY,
                agent_type TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                opc_session_id TEXT,
                task_id TEXT,
                workspace_path TEXT DEFAULT '',
                run_mode TEXT DEFAULT 'batch',
                status TEXT DEFAULT 'unknown',
                metadata TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT,
                checkpoint_type TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                task_id TEXT,
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_sessions (
                runtime_session_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT,
                task_id TEXT,
                status TEXT DEFAULT 'running',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_events (
                event_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_transcript_entries (
                entry_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                session_id TEXT,
                message_id TEXT DEFAULT '',
                role TEXT DEFAULT 'assistant',
                entry_type TEXT DEFAULT 'message',
                content TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_tool_calls (
                call_record_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                session_id TEXT,
                message_id TEXT DEFAULT '',
                tool_call_id TEXT NOT NULL,
                tool_name TEXT DEFAULT '',
                arguments TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_tool_results (
                result_record_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                session_id TEXT,
                message_id TEXT DEFAULT '',
                tool_call_id TEXT DEFAULT '',
                tool_name TEXT DEFAULT '',
                payload TEXT DEFAULT '{}',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_permission_grants (
                grant_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                scope TEXT DEFAULT 'once',
                tool_name TEXT DEFAULT '',
                candidate TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_subagent_runs (
                subagent_run_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                agent_id TEXT NOT NULL,
                profile TEXT DEFAULT 'general',
                status TEXT DEFAULT 'running',
                worktree_path TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_worktree_sessions (
                worktree_session_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                path TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_compaction_boundaries (
                boundary_id TEXT PRIMARY KEY,
                runtime_session_id TEXT NOT NULL,
                task_id TEXT,
                summary TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                parent_session_id TEXT,
                title TEXT DEFAULT '',
                mode TEXT DEFAULT 'primary',
                status TEXT DEFAULT 'active',
                summary TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                task_id TEXT,
                agent_id TEXT,
                parent_message_id TEXT,
                summary_flag INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_parts (
                part_id TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                part_type TEXT NOT NULL,
                payload TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_compactions (
                compaction_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                compaction_message_id TEXT NOT NULL,
                source_boundary_message_id TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_memory_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                summary_message_id TEXT NOT NULL,
                source_boundary_message_id TEXT NOT NULL,
                summary_text TEXT DEFAULT '',
                memory_text TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_compactions (
                compaction_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                employee_id TEXT NOT NULL,
                role_id TEXT DEFAULT '',
                compaction_message_id TEXT NOT NULL,
                source_boundary_message_id TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_memory_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                employee_id TEXT NOT NULL,
                role_id TEXT DEFAULT '',
                memory_scope TEXT DEFAULT 'session',
                memory_kind TEXT DEFAULT 'process',
                summary_message_id TEXT NOT NULL,
                source_boundary_message_id TEXT NOT NULL,
                summary_text TEXT DEFAULT '',
                memory_text TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_links (
                link_id TEXT PRIMARY KEY,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                linked_session_id TEXT,
                task_id TEXT,
                link_type TEXT DEFAULT 'child_session',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS organizations (
                org_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                company_profile TEXT DEFAULT 'corporate',
                budget_monthly_cents INTEGER DEFAULT 0,
                spent_monthly_cents INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL,
                parent_id TEXT,
                owner_agent_id TEXT,
                level TEXT DEFAULT 'task',
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                priority INTEGER DEFAULT 5,
                deadline TEXT,
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS org_agents (
                agent_id TEXT PRIMARY KEY,
                org_id TEXT NOT NULL,
                role_id TEXT NOT NULL,
                name TEXT DEFAULT '',
                reports_to TEXT,
                budget_monthly_cents INTEGER DEFAULT 0,
                spent_monthly_cents INTEGER DEFAULT 0,
                heartbeat_enabled INTEGER DEFAULT 0,
                heartbeat_interval_sec INTEGER DEFAULT 300,
                last_heartbeat_at TEXT,
                status TEXT DEFAULT 'idle',
                capabilities TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cost_events (
                event_id TEXT PRIMARY KEY,
                org_id TEXT,
                agent_id TEXT,
                task_id TEXT,
                model TEXT DEFAULT '',
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                timestamp TEXT NOT NULL
            );
        """)
        # 共用文件庫表（數據管理功能）
        await self._create_shared_files_table()
        await self._db.commit()

    async def _ensure_schema(self) -> None:
        assert self._db
        await self._ensure_external_session_layout()
        await self._ensure_columns(
            "tasks",
            {
                "session_id": "TEXT",
                "parent_session_id": "TEXT",
                "description": "TEXT DEFAULT ''",
                "assigned_to": "TEXT DEFAULT ''",
                "status": "TEXT DEFAULT 'pending'",
                "priority": "INTEGER DEFAULT 5",
                "dependencies": "TEXT DEFAULT '[]'",
                "execution_lock": "INTEGER DEFAULT 0",
                "context_snapshot": "TEXT DEFAULT '{}'",
                "assigned_external_agent": "TEXT",
                "deadline": "TEXT",
                "result": "TEXT",
                "parent_id": "TEXT",
                "project_id": "TEXT DEFAULT 'default'",
                "tags": "TEXT DEFAULT '[]'",
                "comments": "TEXT DEFAULT '[]'",
                "retry_count": "INTEGER DEFAULT 0",
                "max_retries": "INTEGER DEFAULT 3",
                "metadata": "TEXT DEFAULT '{}'",
                "org_id": "TEXT",
                "goal_id": "TEXT",
                "checkout_run_id": "TEXT",
                "execution_locked_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "agent_messages",
            {
                "requires_ack": "INTEGER DEFAULT 0",
                "reply_to_msg_id": "TEXT",
                "task_id": "TEXT",
                "status": "TEXT DEFAULT 'sent'",
                "processed_at": "TEXT",
                "metadata": "TEXT DEFAULT '{}'",
            },
        )
        await self._ensure_columns(
            "meetings",
            {
                "task_id": "TEXT",
                "status": "TEXT DEFAULT 'open'",
                "decision_method": "TEXT DEFAULT ''",
                "current_round": "INTEGER DEFAULT 0",
                "pending_participants": "TEXT DEFAULT '[]'",
                "consensus": "TEXT DEFAULT '{}'",
                "metadata": "TEXT DEFAULT '{}'",
                "updated_at": "TEXT",
                "last_activity_at": "TEXT",
                "deadline_at": "TEXT",
            },
        )
        await self._ensure_columns(
            "handoff_records",
            {
                "session_id": "TEXT",
                "source_work_item_id": "TEXT DEFAULT ''",
                "target_work_item_id": "TEXT DEFAULT ''",
                "requires_ack": "INTEGER DEFAULT 0",
                "status": "TEXT DEFAULT 'sent'",
                "received_at": "TEXT",
                "acked_at": "TEXT",
                "accepted_at": "TEXT",
                "rejected_at": "TEXT",
                "response_summary": "TEXT DEFAULT ''",
                "ack_message_id": "TEXT",
                "response_message_id": "TEXT",
                "metadata": "TEXT DEFAULT '{}'",
            },
        )
        await self._ensure_columns(
            "external_sessions",
            {
                "session_key": "TEXT",
                "opc_session_id": "TEXT",
            },
        )
        await self._ensure_columns(
            "execution_checkpoints",
            {
                "session_id": "TEXT",
            },
        )
        await self._ensure_columns(
            "agent_memory_snapshots",
            {
                "memory_scope": "TEXT DEFAULT 'session'",
            },
        )
        await self._ensure_columns(
            "delegation_runs",
            {
                "company_profile": "TEXT DEFAULT 'corporate'",
                "lifecycle_status": "TEXT DEFAULT 'active'",
                "current_revision": "INTEGER DEFAULT 1",
                "latest_deliverable_summary": "TEXT DEFAULT ''",
                "recovery_pointer": "TEXT DEFAULT '{}'",
                "project_dossier": "TEXT DEFAULT '{}'",
            },
        )
        await self._ensure_columns(
            "delegation_work_items",
            {
                "team_instance_id": "TEXT DEFAULT ''",
                "team_id": "TEXT DEFAULT ''",
                "role_id": "TEXT DEFAULT ''",
                "seat_id": "TEXT DEFAULT ''",
                "seat_state_id": "TEXT DEFAULT ''",
                "role_runtime_session_id": "TEXT DEFAULT ''",
                "parent_work_item_id": "TEXT",
                "source_role_id": "TEXT",
                "source_seat_id": "TEXT",
                "title": "TEXT DEFAULT ''",
                "summary": "TEXT DEFAULT ''",
                "kind": "TEXT DEFAULT 'execute'",
                "projection_id": "TEXT DEFAULT ''",
                "batch_id": "TEXT DEFAULT ''",
                "batch_index": "INTEGER DEFAULT 0",
                "deliverable_summary": "TEXT DEFAULT ''",
                "blocked_reason": "TEXT DEFAULT ''",
                "handoff_status": "TEXT DEFAULT 'pending'",
                "continuation_source": "TEXT DEFAULT ''",
                "manager_role_id": "TEXT DEFAULT ''",
                "manager_seat_id": "TEXT DEFAULT ''",
                "claimed_by_role_runtime_session_id": "TEXT DEFAULT ''",
                "claimed_by_seat_id": "TEXT DEFAULT ''",
                "metadata": "TEXT DEFAULT '{}'",
                # Added during the Phase unification refactor. SQLite
                # ALTER TABLE ADD COLUMN cannot enforce NOT NULL on a
                # populated table, so we use 'ready' as the default and
                # rely on writes to fill in the canonical value.
                "phase": "TEXT DEFAULT 'ready'",
            },
        )
        await self._drop_mismatched_delegation_work_item_indexes()
        await self._ensure_columns(
            "delegation_role_sessions",
            {
                "project_id": "TEXT DEFAULT 'default'",
                "team_instance_id": "TEXT DEFAULT ''",
                "team_id": "TEXT DEFAULT ''",
                "seat_id": "TEXT DEFAULT ''",
                "seat_state_id": "TEXT DEFAULT ''",
                "manager_seat_ids": "TEXT DEFAULT '[]'",
                "seat_ids": "TEXT DEFAULT '[]'",
                "current_work_item": "TEXT DEFAULT '{}'",
                "latest_notification": "TEXT DEFAULT '{}'",
                "manager_digest": "TEXT DEFAULT '{}'",
                # Fix 5 PR3: per-role serial task queue.
                "pending_work_item_ids": "TEXT DEFAULT '[]'",
            },
        )
        await self._ensure_columns(
            "role_runtime_sessions",
            {
                "current_work_item": "TEXT DEFAULT '{}'",
                "latest_notification": "TEXT DEFAULT '{}'",
                "manager_digest": "TEXT DEFAULT '{}'",
                "pending_work_item_ids": "TEXT DEFAULT '[]'",
            },
        )
        await self._ensure_columns(
            "seat_states",
            {
                "current_work_item": "TEXT DEFAULT '{}'",
                "latest_notification": "TEXT DEFAULT '{}'",
                "manager_digest": "TEXT DEFAULT '{}'",
            },
        )

    async def _ensure_indexes(self) -> None:
        assert self._db
        # These indexes depend on columns that may be added by migrations for
        # older databases, so create them only after _ensure_schema() runs.
        await self._db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_project_status_created ON tasks(project_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_project_priority_created ON tasks(project_id, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id);
            CREATE INDEX IF NOT EXISTS idx_messages_task ON agent_messages(task_id);
            CREATE INDEX IF NOT EXISTS idx_messages_status ON agent_messages(status);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON agent_messages(timestamp);
            CREATE INDEX IF NOT EXISTS idx_meetings_task ON meetings(task_id);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_cost_task ON cost_records(task_id);
            CREATE INDEX IF NOT EXISTS idx_approval_project ON approval_records(project_id);
            CREATE INDEX IF NOT EXISTS idx_approval_name ON approval_records(action_name);
            CREATE INDEX IF NOT EXISTS idx_decisions_project ON work_item_decisions(project_id, projection_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifact_records(project_id, projection_id);
            CREATE INDEX IF NOT EXISTS idx_role_memory_project ON role_memory(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_role_work_records_project ON role_work_records(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_role_orientations_project ON role_orientations(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_role_personalities_project ON role_personalities(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_role_collaborations_project ON role_collaborations(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_role_skills_project ON role_skills(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_role_output_metrics_project ON role_output_metrics(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_role_resource_usage_project ON role_resource_usage(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_role_task_assignments_project ON role_task_assignments(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_role_communications_project ON role_communications(project_id, role_id);
            CREATE INDEX IF NOT EXISTS idx_handoff_project ON handoff_records(project_id, target_projection_id);
            CREATE INDEX IF NOT EXISTS idx_handoff_status ON handoff_records(project_id, status, target_projection_id);
            CREATE INDEX IF NOT EXISTS idx_delegation_runs_session ON delegation_runs(session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_delegation_runs_project_lifecycle ON delegation_runs(project_id, lifecycle_status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_delegation_cells_run ON delegation_cells(run_id, manager_role_id);
            CREATE INDEX IF NOT EXISTS idx_delegation_work_items_run ON delegation_work_items(run_id, phase, role_id);
            CREATE INDEX IF NOT EXISTS idx_delegation_work_items_team ON delegation_work_items(team_instance_id, team_id, seat_id, phase);
            CREATE INDEX IF NOT EXISTS idx_delegation_work_items_batch ON delegation_work_items(run_id, batch_id, batch_index, phase);
            CREATE INDEX IF NOT EXISTS idx_delegation_work_items_manager_board ON delegation_work_items(run_id, manager_seat_id, parent_work_item_id, phase);
            CREATE INDEX IF NOT EXISTS idx_work_item_runtime_links_task ON work_item_runtime_links(runtime_task_id);
            CREATE INDEX IF NOT EXISTS idx_work_item_runtime_links_kind ON work_item_runtime_links(link_kind, updated_at);
            CREATE INDEX IF NOT EXISTS idx_delegation_events_run ON delegation_events(run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_delegation_role_sessions_run ON delegation_role_sessions(run_id, role_id, status);
            CREATE INDEX IF NOT EXISTS idx_delegation_role_sessions_team ON delegation_role_sessions(team_instance_id, team_id, seat_id, role_id, status);
            CREATE INDEX IF NOT EXISTS idx_team_instances_run ON team_instances(run_id, team_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_seat_states_team ON seat_states(team_instance_id, team_id, seat_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_role_runtime_sessions_team ON role_runtime_sessions(team_instance_id, team_id, seat_id, role_id, status);
            CREATE INDEX IF NOT EXISTS idx_reorg_proposals_project ON reorg_proposals(project_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_org_snapshots_project ON org_snapshots(project_id, org_version, created_at);
            CREATE INDEX IF NOT EXISTS idx_reorg_events_project ON reorg_events(project_id, proposal_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_checkpoint_project_status ON execution_checkpoints(project_id, status);
            CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_session_parts_session ON session_parts(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_session_compactions_session ON session_compactions(session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_session_memory_snapshots_session ON session_memory_snapshots(session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_agent_compactions_scope ON agent_compactions(project_id, session_id, employee_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_memory_snapshots_scope ON agent_memory_snapshots(project_id, session_id, employee_id, memory_kind, updated_at);
            CREATE INDEX IF NOT EXISTS idx_agent_memory_snapshots_scope_v2 ON agent_memory_snapshots(project_id, employee_id, memory_scope, memory_kind, updated_at);
            CREATE INDEX IF NOT EXISTS idx_session_links_session ON session_links(session_id, link_type);
            CREATE INDEX IF NOT EXISTS idx_external_sessions_agent_project ON external_sessions(agent_type, project_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_sessions_task ON runtime_sessions(task_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_events_session ON runtime_events(runtime_session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_transcript_session ON runtime_transcript_entries(runtime_session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_tool_calls_session ON runtime_tool_calls(runtime_session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_tool_results_session ON runtime_tool_results(runtime_session_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_subagent_runs_session ON runtime_subagent_runs(runtime_session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_worktrees_session ON runtime_worktree_sessions(runtime_session_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_organizations_status ON organizations(status);
            CREATE INDEX IF NOT EXISTS idx_goals_org ON goals(org_id, status);
            CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_id);
            CREATE INDEX IF NOT EXISTS idx_org_agents_org ON org_agents(org_id, status);
            CREATE INDEX IF NOT EXISTS idx_org_agents_reports_to ON org_agents(reports_to);
            CREATE INDEX IF NOT EXISTS idx_cost_events_org ON cost_events(org_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_cost_events_agent ON cost_events(agent_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_cost_events_task ON cost_events(task_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_org ON tasks(org_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id);
        """)
        await self._db.commit()

    async def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        assert self._db
        async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        existing = {row[1] for row in rows}
        for name, ddl in columns.items():
            if name in existing:
                continue
            await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        await self._db.commit()

    async def _drop_mismatched_delegation_work_item_indexes(self) -> None:
        assert self._db
        expected = {
            "idx_delegation_work_items_run": ["run_id", "phase", "role_id"],
            "idx_delegation_work_items_team": ["team_instance_id", "team_id", "seat_id", "phase"],
            "idx_delegation_work_items_batch": ["run_id", "batch_id", "batch_index", "phase"],
            "idx_delegation_work_items_manager_board": ["run_id", "manager_seat_id", "parent_work_item_id", "phase"],
        }
        async with self._db.execute("PRAGMA index_list(delegation_work_items)") as cursor:
            rows = await cursor.fetchall()
        existing = {str(row[1]) for row in rows}
        for index_name, expected_columns in expected.items():
            if index_name not in existing:
                continue
            async with self._db.execute(f"PRAGMA index_info({index_name})") as cursor:
                info = await cursor.fetchall()
            columns = [str(row[2]) for row in info]
            if columns != expected_columns:
                await self._db.execute(f"DROP INDEX IF EXISTS {index_name}")
        await self._db.commit()

    async def _ensure_external_session_layout(self) -> None:
        assert self._db
        async with self._db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'external_sessions'"
        ) as cursor:
            row = await cursor.fetchone()
        create_sql = row[0] if row else ""
        if create_sql and "PRIMARY KEY (agent_type, project_id)" not in create_sql:
            return
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_sessions_v2 (
                session_key TEXT PRIMARY KEY,
                agent_type TEXT NOT NULL,
                project_id TEXT DEFAULT 'default',
                session_id TEXT NOT NULL,
                opc_session_id TEXT,
                task_id TEXT,
                workspace_path TEXT DEFAULT '',
                run_mode TEXT DEFAULT 'batch',
                status TEXT DEFAULT 'unknown',
                metadata TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            INSERT OR REPLACE INTO external_sessions_v2
            (session_key, agent_type, project_id, session_id, opc_session_id, task_id, workspace_path, run_mode, status, metadata, updated_at)
            SELECT
                printf('%s|%s|%s|%s|%s', agent_type, project_id, ifnull(opc_session_id, ''), ifnull(task_id, ''), session_id),
                agent_type,
                project_id,
                session_id,
                opc_session_id,
                task_id,
                workspace_path,
                run_mode,
                status,
                metadata,
                updated_at
            FROM external_sessions;
            DROP TABLE external_sessions;
            ALTER TABLE external_sessions_v2 RENAME TO external_sessions;
            CREATE INDEX IF NOT EXISTS idx_external_sessions_agent_project ON external_sessions(agent_type, project_id, updated_at);
            """
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _table_exists(self, table: str) -> bool:
        assert self._db
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ) as cursor:
            return await cursor.fetchone() is not None

    @staticmethod
    def _clean_text_ids(values: Any) -> set[str]:
        return {
            str(value or "").strip()
            for value in values
            if str(value or "").strip()
        }

    @staticmethod
    def _chunked_ids(values: set[str] | list[str], size: int = 400) -> list[list[str]]:
        ids = sorted(OPCStore._clean_text_ids(values))
        return [ids[index : index + size] for index in range(0, len(ids), size)]

    async def _fetch_text_column(
        self,
        query: str,
        params: tuple[Any, ...] | list[Any] = (),
    ) -> set[str]:
        assert self._db
        async with self._db.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        return self._clean_text_ids(row[0] for row in rows)

    async def _fetch_text_column_where_in(
        self,
        table: str,
        select_column: str,
        where_column: str,
        values: set[str] | list[str],
        *,
        extra_where: str = "",
    ) -> set[str]:
        assert self._db
        results: set[str] = set()
        for chunk in self._chunked_ids(values):
            placeholders = ", ".join("?" for _ in chunk)
            query = f"SELECT {select_column} FROM {table} WHERE {where_column} IN ({placeholders})"
            if extra_where:
                query += f" AND {extra_where}"
            async with self._db.execute(query, tuple(chunk)) as cursor:
                rows = await cursor.fetchall()
            results.update(self._clean_text_ids(row[0] for row in rows))
        return results

    async def _delete_where_in(
        self,
        table: str,
        column: str,
        values: set[str] | list[str],
    ) -> None:
        assert self._db
        for chunk in self._chunked_ids(values):
            placeholders = ", ".join("?" for _ in chunk)
            await self._db.execute(
                f"DELETE FROM {table} WHERE {column} IN ({placeholders})",
                tuple(chunk),
            )

    async def _delete_events_by_payload_ids(
        self,
        json_path: str,
        values: set[str] | list[str],
    ) -> None:
        assert self._db
        for chunk in self._chunked_ids(values):
            placeholders = ", ".join("?" for _ in chunk)
            await self._db.execute(
                f"""
                DELETE FROM events
                WHERE json_valid(payload)
                  AND json_extract(payload, ?) IN ({placeholders})
                """,
                tuple([json_path, *chunk]),
            )

    async def _fetch_text_column_where_text_contains(
        self,
        table: str,
        select_column: str,
        text_column: str,
        values: set[str] | list[str],
    ) -> set[str]:
        assert self._db
        results: set[str] = set()
        for chunk in self._chunked_ids(values):
            conditions = " OR ".join(f"{text_column} LIKE ?" for _ in chunk)
            async with self._db.execute(
                f"SELECT {select_column} FROM {table} WHERE {conditions}",
                tuple(f"%{value}%" for value in chunk),
            ) as cursor:
                rows = await cursor.fetchall()
            results.update(self._clean_text_ids(row[0] for row in rows))
        return results

    async def _delete_where_text_contains(
        self,
        table: str,
        text_column: str,
        values: set[str] | list[str],
    ) -> None:
        assert self._db
        for chunk in self._chunked_ids(values):
            conditions = " OR ".join(f"{text_column} LIKE ?" for _ in chunk)
            await self._db.execute(
                f"DELETE FROM {table} WHERE {conditions}",
                tuple(f"%{value}%" for value in chunk),
            )

    async def _delete_by_json_path_or_text_ids(
        self,
        table: str,
        json_column: str,
        json_paths: tuple[str, ...],
        values: set[str] | list[str],
    ) -> None:
        assert self._db
        clean_values = self._clean_text_ids(values)
        if not clean_values:
            return
        for path in json_paths:
            for chunk in self._chunked_ids(clean_values):
                placeholders = ", ".join("?" for _ in chunk)
                await self._db.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE json_valid({json_column})
                      AND json_extract({json_column}, ?) IN ({placeholders})
                    """,
                    tuple([path, *chunk]),
                )
        await self._delete_where_text_contains(table, json_column, clean_values)

    # --- Tasks ---

    # --- Agent Messages ---

    async def get_latest_reply(self, msg_id: str) -> AgentMessage | None:
        replies = await self.get_replies_for_message(msg_id)
        return replies[-1] if replies else None

    async def get_unprocessed_messages(self, limit: int = 200) -> list[AgentMessage]:
        """Return messages with status SENT or DELIVERED (not yet read/replied/timed out)."""
        assert self._db
        query = """SELECT * FROM agent_messages
        WHERE status IN (?, ?)
        ORDER BY timestamp ASC LIMIT ?"""
        params = [MessageStatus.SENT.value, MessageStatus.DELIVERED.value, limit]
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    # --- Meetings ---

    async def save_meeting(self, meeting: MeetingRoom) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO meetings
            (room_id, task_id, topic, participants, shared_context, agenda, max_rounds,
             decision_owner, status, decision_method, current_round, pending_participants,
             consensus, outcome, transcript, metadata, created_at, updated_at,
             last_activity_at, deadline_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                meeting.room_id,
                meeting.task_id,
                meeting.topic,
                _json_dumps(meeting.participants),
                meeting.shared_context,
                _json_dumps(meeting.agenda),
                meeting.max_rounds,
                meeting.decision_owner,
                meeting.status.value,
                meeting.decision_method,
                int(meeting.current_round or 0),
                _json_dumps(meeting.pending_participants),
                _json_dumps(meeting.consensus),
                _json_dumps(meeting.outcome) if meeting.outcome is not None else None,
                _json_dumps(meeting.transcript),
                _json_dumps(meeting.metadata),
                meeting.created_at.isoformat(),
                meeting.updated_at.isoformat(),
                meeting.last_activity_at.isoformat(),
                meeting.deadline_at.isoformat() if meeting.deadline_at else None,
            ),
        )
        await self._db.commit()

    async def get_meeting(self, room_id: str) -> MeetingRoom | None:
        assert self._db
        async with self._db.execute("SELECT * FROM meetings WHERE room_id = ?", (room_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_meeting(row, cursor.description)

    async def get_meetings_for_task(self, task_id: str) -> list[MeetingRoom]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM meetings WHERE task_id = ? ORDER BY updated_at DESC",
            (task_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_meeting(row, cursor.description) for row in rows]

    def _row_to_meeting(self, row: Any, description: Any) -> MeetingRoom:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return MeetingRoom(
            room_id=data["room_id"],
            task_id=data.get("task_id"),
            topic=data["topic"],
            participants=_json_loads(data["participants"], []),
            shared_context=data["shared_context"],
            agenda=_json_loads(data["agenda"], []),
            max_rounds=data["max_rounds"],
            decision_owner=data["decision_owner"],
            status=MeetingStatus(data.get("status") or MeetingStatus.OPEN.value),
            decision_method=str(data.get("decision_method", "") or ""),
            current_round=int(data.get("current_round") or 0),
            pending_participants=_json_loads(data.get("pending_participants"), []),
            consensus=_json_loads(data.get("consensus"), {}),
            outcome=_json_loads(data["outcome"], None),
            transcript=_json_loads(data["transcript"], []),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            last_activity_at=datetime.fromisoformat(data.get("last_activity_at") or data["updated_at"]),
            deadline_at=datetime.fromisoformat(data["deadline_at"]) if data.get("deadline_at") else None,
        )

    # --- Work-item decisions, artifacts, role memory, handoffs ---

    async def record_work_item_decision(self, record: WorkItemDecisionRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO work_item_decisions
            (decision_id, project_id, task_id, role_id, projection_id, category, summary, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.decision_id,
                record.project_id,
                record.task_id,
                record.role_id,
                record.projection_id,
                record.category,
                record.summary,
                _json_dumps(record.details),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_work_item_decisions(
        self,
        project_id: str,
        projection_id: str | None = None,
        limit: int = 20,
    ) -> list[WorkItemDecisionRecord]:
        assert self._db
        query = "SELECT * FROM work_item_decisions WHERE project_id = ?"
        params: list[Any] = [project_id]
        if projection_id:
            query += " AND projection_id = ?"
            params.append(projection_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                WorkItemDecisionRecord(
                    decision_id=data["decision_id"],
                    project_id=data["project_id"],
                    task_id=data["task_id"],
                    role_id=data["role_id"],
                    projection_id=data["projection_id"],
                    category=data["category"],
                    summary=data["summary"],
                    details=_json_loads(data["details"], {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    async def record_artifact(self, record: ArtifactRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO artifact_records
            (artifact_id, project_id, task_id, projection_id, role_id, name, artifact_type, location, status, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.artifact_id,
                record.project_id,
                record.task_id,
                record.projection_id,
                record.role_id,
                record.name,
                record.artifact_type,
                record.location,
                record.status,
                _json_dumps(record.details),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_artifacts(
        self,
        project_id: str,
        projection_id: str | None = None,
        limit: int = 50,
    ) -> list[ArtifactRecord]:
        assert self._db
        query = "SELECT * FROM artifact_records WHERE project_id = ?"
        params: list[Any] = [project_id]
        if projection_id:
            query += " AND projection_id = ?"
            params.append(projection_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                ArtifactRecord(
                    artifact_id=data["artifact_id"],
                    project_id=data["project_id"],
                    task_id=data["task_id"],
                    projection_id=data["projection_id"],
                    role_id=data["role_id"],
                    name=data["name"],
                    artifact_type=data["artifact_type"],
                    location=data["location"],
                    status=data["status"],
                    details=_json_loads(data["details"], {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    async def record_role_memory(self, record: RoleMemoryRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_memory
            (memory_id, project_id, role_id, scope, summary, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                record.memory_id,
                record.project_id,
                record.role_id,
                record.scope,
                record.summary,
                _json_dumps(record.details),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_role_memory(
        self,
        project_id: str,
        role_id: str,
        limit: int = 10,
    ) -> list[RoleMemoryRecord]:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_memory
            WHERE project_id = ? AND role_id = ?
            ORDER BY created_at DESC LIMIT ?""",
            (project_id, role_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                RoleMemoryRecord(
                    memory_id=data["memory_id"],
                    project_id=data["project_id"],
                    role_id=data["role_id"],
                    scope=data["scope"],
                    summary=data["summary"],
                    details=_json_loads(data["details"], {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    # ------------------------------------------------------------------
    # Role Profile CRUD (十大模塊)
    # ------------------------------------------------------------------

    async def record_role_work_record(self, record: RoleWorkRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_work_records
            (record_id, project_id, role_id, work_item_id, title, status, collaborators, started_at, completed_at, duration_seconds, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.record_id, record.project_id, record.role_id, record.work_item_id,
             record.title, record.status, _json_dumps(record.collaborators),
             record.started_at.isoformat(),
             record.completed_at.isoformat() if record.completed_at else None,
             record.duration_seconds, record.summary),
        )
        await self._db.commit()

    async def get_role_work_records(self, project_id: str, role_id: str, limit: int = 50) -> list[RoleWorkRecord]:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_work_records WHERE project_id = ? AND role_id = ? ORDER BY started_at DESC LIMIT ?""",
            (project_id, role_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                RoleWorkRecord(
                    record_id=d["record_id"], project_id=d["project_id"], role_id=d["role_id"],
                    work_item_id=d["work_item_id"], title=d["title"], status=d["status"],
                    collaborators=_json_loads(d["collaborators"], []),
                    started_at=datetime.fromisoformat(d["started_at"]),
                    completed_at=datetime.fromisoformat(d["completed_at"]) if d["completed_at"] else None,
                    duration_seconds=d["duration_seconds"], summary=d["summary"],
                ) for d in (dict(zip(cols, row)) for row in rows)
            ]

    async def save_role_orientation(self, record: RoleOrientation) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_orientations
            (orientation_id, project_id, role_id, goals, capabilities, values_list, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (record.orientation_id, record.project_id, record.role_id,
             _json_dumps(record.goals), _json_dumps(record.capabilities),
             _json_dumps(record.values), record.updated_at.isoformat()),
        )
        await self._db.commit()

    async def get_role_orientation(self, project_id: str, role_id: str) -> RoleOrientation | None:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_orientations WHERE project_id = ? AND role_id = ? ORDER BY updated_at DESC LIMIT 1""",
            (project_id, role_id),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            d = dict(zip(cols, row))
            return RoleOrientation(
                orientation_id=d["orientation_id"], project_id=d["project_id"], role_id=d["role_id"],
                goals=_json_loads(d["goals"], []), capabilities=_json_loads(d["capabilities"], []),
                values=_json_loads(d["values_list"], []),
                updated_at=datetime.fromisoformat(d["updated_at"]),
            )

    async def save_role_personality(self, record: RolePersonality) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_personalities
            (personality_id, project_id, role_id, traits, interaction_style, behavior_notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (record.personality_id, record.project_id, record.role_id,
             _json_dumps(record.traits), record.interaction_style,
             _json_dumps(record.behavior_notes), record.updated_at.isoformat()),
        )
        await self._db.commit()

    async def get_role_personality(self, project_id: str, role_id: str) -> RolePersonality | None:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_personalities WHERE project_id = ? AND role_id = ? ORDER BY updated_at DESC LIMIT 1""",
            (project_id, role_id),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            d = dict(zip(cols, row))
            return RolePersonality(
                personality_id=d["personality_id"], project_id=d["project_id"], role_id=d["role_id"],
                traits=_json_loads(d["traits"], {}), interaction_style=d["interaction_style"],
                behavior_notes=_json_loads(d["behavior_notes"], []),
                updated_at=datetime.fromisoformat(d["updated_at"]),
            )

    async def record_role_collaboration(self, record: RoleCollaboration) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_collaborations
            (collab_id, project_id, role_id, partner_role_id, interaction_count, last_interaction_at, quality_score, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.collab_id, record.project_id, record.role_id, record.partner_role_id,
             record.interaction_count,
             record.last_interaction_at.isoformat() if record.last_interaction_at else None,
             record.quality_score, record.notes),
        )
        await self._db.commit()

    async def get_role_collaborations(self, project_id: str, role_id: str) -> list[RoleCollaboration]:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_collaborations WHERE project_id = ? AND role_id = ? ORDER BY interaction_count DESC""",
            (project_id, role_id),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                RoleCollaboration(
                    collab_id=d["collab_id"], project_id=d["project_id"], role_id=d["role_id"],
                    partner_role_id=d["partner_role_id"], interaction_count=d["interaction_count"],
                    last_interaction_at=datetime.fromisoformat(d["last_interaction_at"]) if d["last_interaction_at"] else None,
                    quality_score=d["quality_score"], notes=d["notes"],
                ) for d in (dict(zip(cols, row)) for row in rows)
            ]

    async def save_role_skill(self, record: RoleSkillProficiency) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_skills
            (skill_id, project_id, role_id, category, skill_name, level, learning_goals, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.skill_id, record.project_id, record.role_id, record.category,
             record.skill_name, record.level, _json_dumps(record.learning_goals),
             record.updated_at.isoformat()),
        )
        await self._db.commit()

    async def get_role_skills(self, project_id: str, role_id: str) -> list[RoleSkillProficiency]:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_skills WHERE project_id = ? AND role_id = ? ORDER BY category, level DESC""",
            (project_id, role_id),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                RoleSkillProficiency(
                    skill_id=d["skill_id"], project_id=d["project_id"], role_id=d["role_id"],
                    category=d["category"], skill_name=d["skill_name"], level=d["level"],
                    learning_goals=_json_loads(d["learning_goals"], []),
                    updated_at=datetime.fromisoformat(d["updated_at"]),
                ) for d in (dict(zip(cols, row)) for row in rows)
            ]

    async def record_role_output_metrics(self, record: RoleOutputMetrics) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_output_metrics
            (metrics_id, project_id, role_id, week_label, tasks_completed, quality_score, avg_duration, rework_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.metrics_id, record.project_id, record.role_id, record.week_label,
             record.tasks_completed, record.quality_score, record.avg_duration,
             record.rework_count, record.updated_at.isoformat()),
        )
        await self._db.commit()

    async def get_role_output_metrics(self, project_id: str, role_id: str, limit: int = 12) -> list[RoleOutputMetrics]:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_output_metrics WHERE project_id = ? AND role_id = ? ORDER BY week_label DESC LIMIT ?""",
            (project_id, role_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                RoleOutputMetrics(
                    metrics_id=d["metrics_id"], project_id=d["project_id"], role_id=d["role_id"],
                    week_label=d["week_label"], tasks_completed=d["tasks_completed"],
                    quality_score=d["quality_score"], avg_duration=d["avg_duration"],
                    rework_count=d["rework_count"],
                    updated_at=datetime.fromisoformat(d["updated_at"]),
                ) for d in (dict(zip(cols, row)) for row in rows)
            ]

    async def record_role_resource_usage(self, record: RoleResourceUsage) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_resource_usage
            (usage_id, project_id, role_id, period, tokens_in, tokens_out, cost_usd, duration_seconds, model_breakdown, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.usage_id, record.project_id, record.role_id, record.period,
             record.tokens_in, record.tokens_out, record.cost_usd,
             record.duration_seconds, _json_dumps(record.model_breakdown),
             record.updated_at.isoformat()),
        )
        await self._db.commit()

    async def get_role_resource_usage(self, project_id: str, role_id: str, limit: int = 12) -> list[RoleResourceUsage]:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_resource_usage WHERE project_id = ? AND role_id = ? ORDER BY period DESC LIMIT ?""",
            (project_id, role_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                RoleResourceUsage(
                    usage_id=d["usage_id"], project_id=d["project_id"], role_id=d["role_id"],
                    period=d["period"], tokens_in=d["tokens_in"], tokens_out=d["tokens_out"],
                    cost_usd=d["cost_usd"], duration_seconds=d["duration_seconds"],
                    model_breakdown=_json_loads(d["model_breakdown"], {}),
                    updated_at=datetime.fromisoformat(d["updated_at"]),
                ) for d in (dict(zip(cols, row)) for row in rows)
            ]

    async def save_role_task_assignment(self, record: RoleTaskAssignment) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_task_assignments
            (assignment_id, project_id, role_id, work_item_id, title, column_name, priority, depends_on, blocked_reason, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.assignment_id, record.project_id, record.role_id, record.work_item_id,
             record.title, record.column, record.priority,
             _json_dumps(record.depends_on), record.blocked_reason,
             record.updated_at.isoformat()),
        )
        await self._db.commit()

    async def get_role_task_assignments(self, project_id: str, role_id: str) -> list[RoleTaskAssignment]:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_task_assignments WHERE project_id = ? AND role_id = ? ORDER BY priority DESC, updated_at DESC""",
            (project_id, role_id),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                RoleTaskAssignment(
                    assignment_id=d["assignment_id"], project_id=d["project_id"], role_id=d["role_id"],
                    work_item_id=d["work_item_id"], title=d["title"], column=d["column_name"],
                    priority=d["priority"], depends_on=_json_loads(d["depends_on"], []),
                    blocked_reason=d["blocked_reason"],
                    updated_at=datetime.fromisoformat(d["updated_at"]),
                ) for d in (dict(zip(cols, row)) for row in rows)
            ]

    async def record_role_communication(self, record: RoleCommunicationRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO role_communications
            (comm_id, project_id, role_id, comm_type, title, content, participants, outcome, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (record.comm_id, record.project_id, record.role_id, record.comm_type,
             record.title, record.content, _json_dumps(record.participants),
             record.outcome, record.created_at.isoformat()),
        )
        await self._db.commit()

    async def get_role_communications(self, project_id: str, role_id: str, limit: int = 50) -> list[RoleCommunicationRecord]:
        assert self._db
        async with self._db.execute(
            """SELECT * FROM role_communications WHERE project_id = ? AND role_id = ? ORDER BY created_at DESC LIMIT ?""",
            (project_id, role_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                RoleCommunicationRecord(
                    comm_id=d["comm_id"], project_id=d["project_id"], role_id=d["role_id"],
                    comm_type=d["comm_type"], title=d["title"], content=d["content"],
                    participants=_json_loads(d["participants"], []), outcome=d["outcome"],
                    created_at=datetime.fromisoformat(d["created_at"]),
                ) for d in (dict(zip(cols, row)) for row in rows)
            ]

    async def save_handoff_record(self, record: HandoffRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO handoff_records
            (handoff_id, project_id, session_id, task_id, from_role, to_role, source_projection_id, target_projection_id,
             source_work_item_id, target_work_item_id, summary, payload, requires_ack, status, received_at, acked_at, accepted_at, rejected_at,
             response_summary, ack_message_id, response_message_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.handoff_id,
                record.project_id,
                record.session_id,
                record.task_id,
                record.from_role,
                record.to_role,
                record.source_projection_id,
                record.target_projection_id,
                record.source_work_item_id,
                record.target_work_item_id,
                record.summary,
                _json_dumps(record.payload),
                int(record.requires_ack),
                record.status,
                record.received_at.isoformat() if record.received_at else None,
                record.acked_at.isoformat() if record.acked_at else None,
                record.accepted_at.isoformat() if record.accepted_at else None,
                record.rejected_at.isoformat() if record.rejected_at else None,
                record.response_summary,
                record.ack_message_id,
                record.response_message_id,
                _json_dumps(record.metadata),
                record.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    def _row_to_handoff_record(self, row: Any, description: Any) -> HandoffRecord:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return HandoffRecord(
            handoff_id=data["handoff_id"],
            project_id=data["project_id"],
            session_id=data.get("session_id"),
            task_id=data["task_id"],
            from_role=data["from_role"],
            to_role=data["to_role"],
            source_projection_id=data["source_projection_id"],
            target_projection_id=data["target_projection_id"],
            source_work_item_id=data.get("source_work_item_id") or "",
            target_work_item_id=data.get("target_work_item_id") or "",
            summary=data["summary"],
            payload=_json_loads(data["payload"], {}),
            requires_ack=bool(data.get("requires_ack", 0)),
            status=data.get("status") or "sent",
            received_at=datetime.fromisoformat(data["received_at"]) if data.get("received_at") else None,
            acked_at=datetime.fromisoformat(data["acked_at"]) if data.get("acked_at") else None,
            accepted_at=datetime.fromisoformat(data["accepted_at"]) if data.get("accepted_at") else None,
            rejected_at=datetime.fromisoformat(data["rejected_at"]) if data.get("rejected_at") else None,
            response_summary=data.get("response_summary") or "",
            ack_message_id=data.get("ack_message_id"),
            response_message_id=data.get("response_message_id"),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def get_handoff_record(self, handoff_id: str) -> HandoffRecord | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM handoff_records WHERE handoff_id = ?",
            (handoff_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_handoff_record(row, cursor.description)

    async def get_handoff_records(
        self,
        project_id: str,
        target_projection_id: str | None = None,
        target_work_item_id: str | None = None,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[HandoffRecord]:
        assert self._db
        query = "SELECT * FROM handoff_records WHERE project_id = ?"
        params: list[Any] = [project_id]
        if target_projection_id:
            query += " AND target_projection_id = ?"
            params.append(target_projection_id)
        if target_work_item_id:
            query += " AND target_work_item_id = ?"
            params.append(target_work_item_id)
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_handoff_record(row, cursor.description) for row in rows]

    async def update_handoff_record(
        self,
        handoff_id: str,
        *,
        status: str | None = None,
        received_at: datetime | None = None,
        acked_at: datetime | None = None,
        accepted_at: datetime | None = None,
        rejected_at: datetime | None = None,
        response_summary: str | None = None,
        ack_message_id: str | None = None,
        response_message_id: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> HandoffRecord | None:
        record = await self.get_handoff_record(handoff_id)
        if record is None:
            return None
        if status is not None:
            record.status = str(status).strip() or record.status
        if received_at is not None:
            record.received_at = received_at
        if acked_at is not None:
            record.acked_at = acked_at
        if accepted_at is not None:
            record.accepted_at = accepted_at
        if rejected_at is not None:
            record.rejected_at = rejected_at
        if response_summary is not None:
            record.response_summary = str(response_summary)
        if ack_message_id is not None:
            record.ack_message_id = ack_message_id
        if response_message_id is not None:
            record.response_message_id = response_message_id
        if metadata_updates:
            record.metadata = {**dict(record.metadata or {}), **dict(metadata_updates)}
        await self.save_handoff_record(record)
        return record

    def _row_to_role_runtime_session(self, row: Any, description: Any) -> RoleRuntimeSession:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        focused_work_item_id = data.get("focused_work_item_id") or ""
        status = normalize_role_runtime_status(data.get("status"), focused_work_item_id)
        if status == "idle":
            focused_work_item_id = ""
        return RoleRuntimeSession(
            role_session_id=data["role_session_id"],
            run_id=data["run_id"],
            project_id=data.get("project_id") or "default",
            team_instance_id=data.get("team_instance_id") or "",
            team_id=data.get("team_id") or "",
            role_id=data.get("role_id") or "",
            seat_id=data.get("seat_id") or "",
            seat_state_id=data.get("seat_state_id") or "",
            employee_id=data.get("employee_id") or "",
            focused_work_item_id=focused_work_item_id,
            background_work_item_ids=_json_loads(data.get("background_work_item_ids"), []),
            manager_role_ids=_json_loads(data.get("manager_role_ids"), []),
            manager_seat_ids=_json_loads(data.get("manager_seat_ids"), []),
            seat_ids=_json_loads(data.get("seat_ids"), []),
            adapter_session_state=_json_loads(data.get("adapter_session_state"), {}),
            inbox_state=_json_loads(data.get("inbox_state"), {}),
            memory_slices_by_work_item=_json_loads(data.get("memory_slices_by_work_item"), {}),
            resume_state=_json_loads(data.get("resume_state"), {}),
            current_work_item=_json_loads(data.get("current_work_item"), {}),
            latest_notification=_json_loads(data.get("latest_notification"), {}),
            manager_digest=_json_loads(data.get("manager_digest"), {}),
            status=status,
            pending_work_item_ids=_json_loads(data.get("pending_work_item_ids"), []),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def _save_role_runtime_session_row(self, session: RoleRuntimeSession, *, table: str) -> None:
        db = self._require_db()
        session.focused_work_item_id = str(session.focused_work_item_id or "").strip()
        session.status = normalize_role_runtime_status(
            session.status,
            session.focused_work_item_id,
        )
        if session.status == "idle":
            session.focused_work_item_id = ""
        await db.execute(
            f"""INSERT OR REPLACE INTO {table}
            (role_session_id, run_id, project_id, team_instance_id, team_id, role_id, seat_id, seat_state_id,
             employee_id, focused_work_item_id, background_work_item_ids, manager_role_ids, manager_seat_ids,
             seat_ids, adapter_session_state, inbox_state, memory_slices_by_work_item, resume_state,
             current_work_item, latest_notification, manager_digest, status, pending_work_item_ids,
             metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session.role_session_id,
                session.run_id,
                session.project_id,
                session.team_instance_id,
                session.team_id,
                session.role_id,
                session.seat_id,
                session.seat_state_id,
                session.employee_id,
                session.focused_work_item_id,
                _json_dumps(session.background_work_item_ids),
                _json_dumps(session.manager_role_ids),
                _json_dumps(session.manager_seat_ids),
                _json_dumps(session.seat_ids),
                _json_dumps(session.adapter_session_state),
                _json_dumps(session.inbox_state),
                _json_dumps(session.memory_slices_by_work_item),
                _json_dumps(session.resume_state),
                _json_dumps(session.current_work_item),
                _json_dumps(session.latest_notification),
                _json_dumps(session.manager_digest),
                session.status,
                _json_dumps(list(getattr(session, "pending_work_item_ids", []) or [])),
                _json_dumps(session.metadata),
                session.created_at.isoformat(),
                session.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def save_role_runtime_session(self, session: RoleRuntimeSession) -> None:
        await self._save_role_runtime_session_row(session, table="role_runtime_sessions")
        await self._save_role_runtime_session_row(session, table="delegation_role_sessions")

    async def save_delegation_role_session(self, session: RoleRuntimeSession) -> None:
        await self.save_role_runtime_session(session)

    async def get_role_runtime_session(self, role_session_id: str) -> RoleRuntimeSession | None:
        db = self._require_db()
        for table in ("role_runtime_sessions", "delegation_role_sessions"):
            async with db.execute(
                f"SELECT * FROM {table} WHERE role_session_id = ?",
                (role_session_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row is not None:
                    return self._row_to_role_runtime_session(row, cursor.description)
        return None

    async def get_delegation_role_session(self, role_session_id: str) -> RoleRuntimeSession | None:
        return await self.get_role_runtime_session(role_session_id)

    async def get_delegation_role_session_for_role(
        self,
        run_id: str,
        role_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
    ) -> RoleRuntimeSession | None:
        db = self._require_db()
        query = "SELECT * FROM delegation_role_sessions WHERE run_id = ? AND role_id = ?"
        params: list[Any] = [run_id, role_id]
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        query += " ORDER BY created_at ASC LIMIT 1"
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if row is not None:
                return self._row_to_role_runtime_session(row, cursor.description)
        return None

    async def get_role_runtime_session_for_role(
        self,
        run_id: str,
        role_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
    ) -> RoleRuntimeSession | None:
        return await self.get_delegation_role_session_for_role(
            run_id,
            role_id,
            team_id=team_id,
            seat_id=seat_id,
        )

    async def list_role_runtime_sessions(
        self,
        run_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
        role_id: str | None = None,
        status: str | None = None,
    ) -> list[RoleRuntimeSession]:
        db = self._require_db()
        query = "SELECT * FROM role_runtime_sessions WHERE run_id = ?"
        params: list[Any] = [run_id]
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        if role_id:
            query += " AND role_id = ?"
            params.append(role_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_role_runtime_session(row, cursor.description) for row in rows]

    async def list_delegation_role_sessions(
        self,
        run_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
        role_id: str | None = None,
        status: str | None = None,
    ) -> list[RoleRuntimeSession]:
        db = self._require_db()
        query = "SELECT * FROM delegation_role_sessions WHERE run_id = ?"
        params: list[Any] = [run_id]
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        if role_id:
            query += " AND role_id = ?"
            params.append(role_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_role_runtime_session(row, cursor.description) for row in rows]

    async def update_delegation_role_session(
        self,
        role_session_id: str,
        *,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        seat_id: str | None = None,
        seat_state_id: str | None = None,
        focused_work_item_id: str | None = None,
        background_work_item_ids: list[str] | None = None,
        manager_role_ids: list[str] | None = None,
        manager_seat_ids: list[str] | None = None,
        seat_ids: list[str] | None = None,
        adapter_session_state: dict[str, Any] | None = None,
        inbox_state: dict[str, Any] | None = None,
        memory_slices_by_work_item: dict[str, list[str]] | None = None,
        resume_state: dict[str, Any] | None = None,
        current_work_item: dict[str, Any] | None = None,
        latest_notification: dict[str, Any] | None = None,
        manager_digest: dict[str, Any] | None = None,
        status: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> RoleRuntimeSession | None:
        session = await self.get_delegation_role_session(role_session_id)
        if session is None:
            return None
        if team_instance_id is not None:
            session.team_instance_id = str(team_instance_id or "").strip()
        if team_id is not None:
            session.team_id = str(team_id or "").strip()
        if seat_id is not None:
            session.seat_id = str(seat_id or "").strip()
        if seat_state_id is not None:
            session.seat_state_id = str(seat_state_id or "").strip()
        if focused_work_item_id is not None:
            session.focused_work_item_id = str(focused_work_item_id or "").strip()
        if background_work_item_ids is not None:
            session.background_work_item_ids = [
                str(item).strip() for item in background_work_item_ids if str(item).strip()
            ]
        if manager_role_ids is not None:
            session.manager_role_ids = [str(item).strip() for item in manager_role_ids if str(item).strip()]
        if manager_seat_ids is not None:
            session.manager_seat_ids = [str(item).strip() for item in manager_seat_ids if str(item).strip()]
        if seat_ids is not None:
            session.seat_ids = [str(item).strip() for item in seat_ids if str(item).strip()]
        if adapter_session_state is not None:
            session.adapter_session_state = dict(adapter_session_state)
        if inbox_state is not None:
            session.inbox_state = dict(inbox_state)
        if memory_slices_by_work_item is not None:
            session.memory_slices_by_work_item = {
                str(key).strip(): [str(item).strip() for item in list(value or []) if str(item).strip()]
                for key, value in dict(memory_slices_by_work_item or {}).items()
                if str(key).strip()
            }
        if resume_state is not None:
            session.resume_state = dict(resume_state)
        if current_work_item is not None:
            session.current_work_item = dict(current_work_item)
        if latest_notification is not None:
            session.latest_notification = dict(latest_notification)
        if manager_digest is not None:
            session.manager_digest = dict(manager_digest)
        if status is not None:
            session.status = normalize_role_runtime_status(
                status,
                session.focused_work_item_id,
            )
        if metadata_updates:
            session.metadata = {**dict(session.metadata or {}), **dict(metadata_updates)}
        session.status = normalize_role_runtime_status(
            session.status,
            session.focused_work_item_id,
        )
        if session.status == "idle":
            session.focused_work_item_id = ""
        session.updated_at = datetime.now()
        await self.save_delegation_role_session(session)
        return session

    async def update_role_runtime_session(
        self,
        role_session_id: str,
        **kwargs: Any,
    ) -> RoleRuntimeSession | None:
        return await self.update_delegation_role_session(role_session_id, **kwargs)

    # ── Fix 5 PR3: pending queue atomic helpers ────────────────────────

    async def enqueue_pending_work_item(
        self,
        role_session_id: str,
        work_item_id: str,
    ) -> bool:
        """Append ``work_item_id`` to the session's pending queue.

        Returns ``True`` if the item was enqueued, ``False`` if the session
        was not found or the item was already present. The write uses the
        SQL row as the source of truth (read → append → write in one
        transaction) so two concurrent dispatcher ticks cannot clobber
        each other's append. Idempotent on duplicate work_item_id: the
        queue is a set-in-FIFO-order, not a bag.
        """
        wid = str(work_item_id or "").strip()
        sid = str(role_session_id or "").strip()
        if not wid or not sid:
            return False
        db = self._require_db()
        # Atomic read-modify-write inside a transaction so concurrent
        # enqueues don't race each other. SQLite's default journal_mode
        # (WAL) serializes writes; the BEGIN IMMEDIATE here forces the
        # write lock upfront to avoid a lock upgrade halfway through.
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                """SELECT pending_work_item_ids, updated_at
                   FROM role_runtime_sessions
                   WHERE role_session_id = ?""",
                (sid,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute("ROLLBACK")
                return False
            pending_json, _updated_at = row
            pending = _json_loads(pending_json, [])
            if not isinstance(pending, list):
                pending = []
            if wid in pending:
                await db.execute("ROLLBACK")
                return False
            pending.append(wid)
            now_iso = datetime.now().isoformat()
            for table in ("role_runtime_sessions", "delegation_role_sessions"):
                await db.execute(
                    f"""UPDATE {table}
                        SET pending_work_item_ids = ?,
                            updated_at = ?
                        WHERE role_session_id = ?""",
                    (_json_dumps(pending), now_iso, sid),
                )
            await db.commit()
            return True
        except Exception:
            await db.execute("ROLLBACK")
            raise

    async def dequeue_pending_work_item(
        self,
        role_session_id: str,
    ) -> str | None:
        """Pop the FIFO head of the session's pending queue.

        Returns the dequeued ``work_item_id`` or ``None`` if the queue is
        empty / session missing. Atomic read-modify-write so the pop is
        safe against concurrent enqueues.
        """
        sid = str(role_session_id or "").strip()
        if not sid:
            return None
        db = self._require_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                """SELECT pending_work_item_ids
                   FROM role_runtime_sessions
                   WHERE role_session_id = ?""",
                (sid,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute("ROLLBACK")
                return None
            pending = _json_loads(row[0], [])
            if not isinstance(pending, list) or not pending:
                await db.execute("ROLLBACK")
                return None
            head = str(pending[0])
            remaining = pending[1:]
            now_iso = datetime.now().isoformat()
            for table in ("role_runtime_sessions", "delegation_role_sessions"):
                await db.execute(
                    f"""UPDATE {table}
                        SET pending_work_item_ids = ?,
                            updated_at = ?
                        WHERE role_session_id = ?""",
                    (_json_dumps(remaining), now_iso, sid),
                )
            await db.commit()
            return head
        except Exception:
            await db.execute("ROLLBACK")
            raise

    async def role_session_is_busy(self, role_session_id: str) -> bool:
        """Return ``True`` when the session is already focused on a work
        item (i.e. a new runnable work item should be queued, not claimed).

        A session is busy when ``focused_work_item_id`` is non-empty. The
        status column is intentionally not consulted: a session marked
        ``idle`` but still carrying a focus stamp is in the "claim
        completing" gap and should still hold new work back.
        """
        sid = str(role_session_id or "").strip()
        if not sid:
            return False
        db = self._require_db()
        async with db.execute(
            """SELECT focused_work_item_id FROM role_runtime_sessions
               WHERE role_session_id = ?""",
            (sid,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        return bool(str(row[0] or "").strip())

    # ── Fix 5 PR6: role-level adapter resume tokens ────────────────────

    async def update_role_session_adapter_state(
        self,
        role_session_id: str,
        agent_type: str,
        token_record: dict[str, Any] | None,
    ) -> bool:
        """Merge a single-agent entry into ``adapter_session_state``.

        PR6: the resume token for each external agent (codex,
        claude_code, opencode) lives under ``adapter_session_state[agent_type]``
        on the ROLE session — not per-task. Consecutive tasks for the
        same role resume the same external session (same codex thread,
        same claude-code session, same opencode session). A single role
        can hold independent tokens for different adapters simultaneously
        (keyed by ``agent_type``) so switching executor types is safe.

        ``token_record`` shape:
            {
                "resume_session_id": str,
                "provider_session_id": str,
                "updated_at": iso string,
                "last_task_id": str,
                "last_project_id": str,
            }

        Passing ``token_record=None`` clears the entry for ``agent_type``
        (used when the adapter signals the session is no longer resumable).
        Returns True on success, False when the role session is missing.

        Atomic read-modify-write inside ``BEGIN IMMEDIATE`` so concurrent
        broker writes (parallel roles, or two executors per role during
        rollover) don't clobber each other.
        """
        sid = str(role_session_id or "").strip()
        agent = str(agent_type or "").strip()
        if not sid or not agent:
            return False
        db = self._require_db()
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                """SELECT adapter_session_state FROM role_runtime_sessions
                   WHERE role_session_id = ?""",
                (sid,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await db.execute("ROLLBACK")
                return False
            current = _json_loads(row[0], {})
            if not isinstance(current, dict):
                current = {}
            if token_record is None:
                current.pop(agent, None)
            else:
                current[agent] = {
                    str(k): v for k, v in dict(token_record).items()
                }
            now_iso = datetime.now().isoformat()
            serialized = _json_dumps(current)
            for table in ("role_runtime_sessions", "delegation_role_sessions"):
                await db.execute(
                    f"""UPDATE {table}
                        SET adapter_session_state = ?,
                            updated_at = ?
                        WHERE role_session_id = ?""",
                    (serialized, now_iso, sid),
                )
            await db.commit()
            return True
        except Exception:
            await db.execute("ROLLBACK")
            raise

    async def get_role_session_adapter_state(
        self,
        role_session_id: str,
        agent_type: str,
    ) -> dict[str, Any] | None:
        """Read the per-agent token entry from ``adapter_session_state``.

        Returns the stored dict (``{"resume_session_id": ..., ...}``) or
        ``None`` when the role session, the dict, or the agent's entry
        is missing. Never raises on malformed JSON — returns None.
        """
        sid = str(role_session_id or "").strip()
        agent = str(agent_type or "").strip()
        if not sid or not agent:
            return None
        db = self._require_db()
        async with db.execute(
            """SELECT adapter_session_state FROM role_runtime_sessions
               WHERE role_session_id = ?""",
            (sid,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        state = _json_loads(row[0], {})
        if not isinstance(state, dict):
            return None
        entry = state.get(agent)
        if not isinstance(entry, dict):
            return None
        return entry

    def _row_to_team_instance(self, row: Any, description: Any) -> TeamInstance:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return TeamInstance(
            team_instance_id=data["team_instance_id"],
            run_id=data["run_id"],
            project_id=data.get("project_id") or "default",
            team_id=data["team_id"],
            session_id=data.get("session_id") or "",
            status=data.get("status") or "pending",
            seat_ids=_json_loads(data.get("seat_ids"), []),
            role_ids=_json_loads(data.get("role_ids"), []),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_team_instance(self, team: TeamInstance) -> None:
        db = self._require_db()
        await db.execute(
            """INSERT OR REPLACE INTO team_instances
            (team_instance_id, run_id, project_id, team_id, session_id, status, seat_ids, role_ids, metadata,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                team.team_instance_id,
                team.run_id,
                team.project_id,
                team.team_id,
                team.session_id,
                team.status,
                _json_dumps(team.seat_ids),
                _json_dumps(team.role_ids),
                _json_dumps(team.metadata),
                team.created_at.isoformat(),
                team.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def get_team_instance(self, team_instance_id: str) -> TeamInstance | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM team_instances WHERE team_instance_id = ?",
            (team_instance_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_team_instance(row, cursor.description)

    async def list_team_instances(
        self,
        *,
        run_id: str | None = None,
        team_id: str | None = None,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[TeamInstance]:
        db = self._require_db()
        query = "SELECT * FROM team_instances WHERE 1=1"
        params: list[Any] = []
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_team_instance(row, cursor.description) for row in rows]

    async def update_team_instance(
        self,
        team_instance_id: str,
        *,
        status: str | None = None,
        seat_ids: list[str] | None = None,
        role_ids: list[str] | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> TeamInstance | None:
        team = await self.get_team_instance(team_instance_id)
        if team is None:
            return None
        if status is not None:
            team.status = str(status).strip() or team.status
        if seat_ids is not None:
            team.seat_ids = [str(item).strip() for item in seat_ids if str(item).strip()]
        if role_ids is not None:
            team.role_ids = [str(item).strip() for item in role_ids if str(item).strip()]
        if metadata_updates:
            team.metadata = {**dict(team.metadata or {}), **dict(metadata_updates)}
        team.updated_at = datetime.now()
        await self.save_team_instance(team)
        return team

    def _row_to_seat_state(self, row: Any, description: Any) -> SeatState:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        current_work_item_id = data.get("current_work_item_id") or ""
        status = normalize_role_runtime_status(data.get("status"), current_work_item_id)
        if status == "idle":
            current_work_item_id = ""
        return SeatState(
            seat_state_id=data["seat_state_id"],
            team_instance_id=data["team_instance_id"],
            run_id=data["run_id"],
            project_id=data.get("project_id") or "default",
            team_id=data["team_id"],
            seat_id=data["seat_id"],
            role_id=data.get("role_id") or "",
            employee_id=data.get("employee_id") or "",
            member_session_id=data.get("member_session_id") or "",
            role_runtime_session_id=data.get("role_runtime_session_id") or "",
            status=status,
            resident_status=status,
            current_task_id=data.get("current_task_id") or "",
            current_work_item_id=current_work_item_id,
            manager_role_id=data.get("manager_role_id") or "",
            manager_seat_id=data.get("manager_seat_id") or "",
            manager_role_ids=_json_loads(data.get("manager_role_ids"), []),
            manager_seat_ids=_json_loads(data.get("manager_seat_ids"), []),
            inbox_state=_json_loads(data.get("inbox_state"), {}),
            resume_state=_json_loads(data.get("resume_state"), {}),
            current_work_item=_json_loads(data.get("current_work_item"), {}),
            latest_notification=_json_loads(data.get("latest_notification"), {}),
            manager_digest=_json_loads(data.get("manager_digest"), {}),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_seat_state(self, seat: SeatState) -> None:
        db = self._require_db()
        seat.current_work_item_id = str(seat.current_work_item_id or "").strip()
        seat.status = normalize_role_runtime_status(
            seat.status,
            seat.current_work_item_id,
        )
        if seat.status == "idle":
            seat.current_work_item_id = ""
        seat.resident_status = seat.status
        await db.execute(
            """INSERT OR REPLACE INTO seat_states
            (seat_state_id, team_instance_id, run_id, project_id, team_id, seat_id, role_id, employee_id,
             member_session_id, role_runtime_session_id, status, resident_status, current_task_id,
             current_work_item_id, manager_role_id, manager_seat_id, manager_role_ids, manager_seat_ids,
             inbox_state, resume_state, current_work_item, latest_notification, manager_digest, metadata,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                seat.seat_state_id,
                seat.team_instance_id,
                seat.run_id,
                seat.project_id,
                seat.team_id,
                seat.seat_id,
                seat.role_id,
                seat.employee_id,
                seat.member_session_id,
                seat.role_runtime_session_id,
                seat.status,
                seat.resident_status,
                seat.current_task_id,
                seat.current_work_item_id,
                seat.manager_role_id,
                seat.manager_seat_id,
                _json_dumps(seat.manager_role_ids),
                _json_dumps(seat.manager_seat_ids),
                _json_dumps(seat.inbox_state),
                _json_dumps(seat.resume_state),
                _json_dumps(seat.current_work_item),
                _json_dumps(seat.latest_notification),
                _json_dumps(seat.manager_digest),
                _json_dumps(seat.metadata),
                seat.created_at.isoformat(),
                seat.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def get_seat_state(self, seat_state_id: str) -> SeatState | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM seat_states WHERE seat_state_id = ?",
            (seat_state_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_seat_state(row, cursor.description)

    async def get_seat_state_for_seat(
        self,
        team_instance_id: str,
        seat_id: str,
    ) -> SeatState | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM seat_states WHERE team_instance_id = ? AND seat_id = ? ORDER BY created_at ASC LIMIT 1",
            (team_instance_id, seat_id),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_seat_state(row, cursor.description)

    async def list_seat_states(
        self,
        *,
        team_instance_id: str | None = None,
        team_id: str | None = None,
        seat_id: str | None = None,
        run_id: str | None = None,
    ) -> list[SeatState]:
        db = self._require_db()
        query = "SELECT * FROM seat_states WHERE 1=1"
        params: list[Any] = []
        if team_instance_id:
            query += " AND team_instance_id = ?"
            params.append(team_instance_id)
        if team_id:
            query += " AND team_id = ?"
            params.append(team_id)
        if seat_id:
            query += " AND seat_id = ?"
            params.append(seat_id)
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        query += " ORDER BY created_at ASC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_seat_state(row, cursor.description) for row in rows]

    async def update_seat_state(
        self,
        seat_state_id: str,
        *,
        status: str | None = None,
        resident_status: str | None = None,
        current_task_id: str | None = None,
        current_work_item_id: str | None = None,
        role_runtime_session_id: str | None = None,
        inbox_state: dict[str, Any] | None = None,
        resume_state: dict[str, Any] | None = None,
        current_work_item: dict[str, Any] | None = None,
        latest_notification: dict[str, Any] | None = None,
        manager_digest: dict[str, Any] | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> SeatState | None:
        seat = await self.get_seat_state(seat_state_id)
        if seat is None:
            return None
        if status is not None:
            seat.status = normalize_role_runtime_status(
                status,
                seat.current_work_item_id,
            )
        if resident_status is not None:
            seat.resident_status = normalize_role_runtime_status(
                resident_status,
                seat.current_work_item_id,
            )
        if current_task_id is not None:
            seat.current_task_id = str(current_task_id or "").strip()
        if current_work_item_id is not None:
            seat.current_work_item_id = str(current_work_item_id or "").strip()
        seat.status = normalize_role_runtime_status(
            seat.status,
            seat.current_work_item_id,
        )
        if seat.status == "idle":
            seat.current_work_item_id = ""
        seat.resident_status = seat.status
        if role_runtime_session_id is not None:
            seat.role_runtime_session_id = str(role_runtime_session_id or "").strip()
        if inbox_state is not None:
            seat.inbox_state = dict(inbox_state)
        if resume_state is not None:
            seat.resume_state = dict(resume_state)
        if current_work_item is not None:
            seat.current_work_item = dict(current_work_item)
        if latest_notification is not None:
            seat.latest_notification = dict(latest_notification)
        if manager_digest is not None:
            seat.manager_digest = dict(manager_digest)
        if metadata_updates:
            seat.metadata = {**dict(seat.metadata or {}), **dict(metadata_updates)}
        seat.updated_at = datetime.now()
        await self.save_seat_state(seat)
        return seat

    async def save_delegation_seat_state(self, seat: SeatState) -> None:
        await self.save_seat_state(seat)

    async def get_delegation_seat_state(self, seat_state_id: str) -> SeatState | None:
        return await self.get_seat_state(seat_state_id)

    async def list_delegation_seat_states(
        self,
        run_id: str,
        *,
        team_id: str | None = None,
        seat_id: str | None = None,
    ) -> list[SeatState]:
        return await self.list_seat_states(
            run_id=run_id,
            team_id=team_id,
            seat_id=seat_id,
        )

    async def update_delegation_seat_state(
        self,
        seat_state_id: str,
        **kwargs: Any,
    ) -> SeatState | None:
        return await self.update_seat_state(seat_state_id, **kwargs)

    def _row_to_delegation_run(self, row: Any, description: Any) -> DelegationRun:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return DelegationRun(
            run_id=data["run_id"],
            project_id=data["project_id"],
            session_id=data["session_id"],
            company_profile=data.get("company_profile") or "corporate",
            execution_model=data.get("execution_model") or "recursive_delegation",
            final_decider_role_id=data.get("final_decider_role_id") or "",
            top_level_role_ids=_json_loads(data.get("top_level_role_ids"), []),
            status=data.get("status") or "pending",
            lifecycle_status=data.get("lifecycle_status") or "active",
            current_revision=int(data.get("current_revision") or 1),
            latest_deliverable_summary=data.get("latest_deliverable_summary") or "",
            recovery_pointer=_json_loads(data.get("recovery_pointer"), {}),
            project_dossier=_json_loads(data.get("project_dossier"), {}),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_delegation_run(self, run: DelegationRun) -> None:
        db = self._require_db()
        await db.execute(
            """INSERT OR REPLACE INTO delegation_runs
            (run_id, project_id, session_id, company_profile, execution_model, final_decider_role_id,
             top_level_role_ids, status, lifecycle_status, current_revision, latest_deliverable_summary,
             recovery_pointer, project_dossier, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run.run_id,
                run.project_id,
                run.session_id,
                run.company_profile,
                run.execution_model,
                run.final_decider_role_id,
                _json_dumps(run.top_level_role_ids),
                run.status,
                run.lifecycle_status,
                int(run.current_revision or 1),
                run.latest_deliverable_summary,
                _json_dumps(run.recovery_pointer),
                _json_dumps(run.project_dossier),
                _json_dumps(run.metadata),
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def get_delegation_run(self, run_id: str) -> DelegationRun | None:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM delegation_runs WHERE run_id = ?",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_delegation_run(row, cursor.description)

    async def list_delegation_runs(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        lifecycle_status: str | None = None,
        session_id: str | None = None,
    ) -> list[DelegationRun]:
        db = self._require_db()
        query = "SELECT * FROM delegation_runs WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if lifecycle_status:
            query += " AND lifecycle_status = ?"
            params.append(lifecycle_status)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY updated_at DESC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_run(row, cursor.description) for row in rows]

    async def list_open_delegation_runs(
        self,
        *,
        project_id: str | None = None,
    ) -> list[DelegationRun]:
        db = self._require_db()
        open_states = ("active", "paused", "blocked", "awaiting_owner", "deliverable")
        query = (
            "SELECT * FROM delegation_runs WHERE lifecycle_status IN (?, ?, ?, ?, ?)"
        )
        params: list[Any] = list(open_states)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        query += " ORDER BY updated_at DESC"
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_run(row, cursor.description) for row in rows]

    async def get_latest_delegation_run(
        self,
        project_id: str,
        *,
        include_session_id: str | None = None,
    ) -> DelegationRun | None:
        runs = await self.list_delegation_runs(project_id=project_id)
        for run in runs:
            if include_session_id and run.session_id == include_session_id:
                continue
            return run
        return None

    def _row_to_delegation_cell(self, row: Any, description: Any) -> DelegationCell:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return DelegationCell(
            cell_id=data["cell_id"],
            run_id=data["run_id"],
            manager_role_id=data.get("manager_role_id") or "",
            member_role_ids=_json_loads(data.get("member_role_ids"), []),
            status=data.get("status") or "idle",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_delegation_cell(self, cell: DelegationCell) -> None:
        db = self._require_db()
        await db.execute(
            """INSERT OR REPLACE INTO delegation_cells
            (cell_id, run_id, manager_role_id, member_role_ids, status, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cell.cell_id,
                cell.run_id,
                cell.manager_role_id,
                _json_dumps(cell.member_role_ids),
                cell.status,
                _json_dumps(cell.metadata),
                cell.created_at.isoformat(),
                cell.updated_at.isoformat(),
            ),
        )
        await db.commit()

    async def list_delegation_cells(self, run_id: str) -> list[DelegationCell]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM delegation_cells WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_delegation_cell(row, cursor.description) for row in rows]

    def _row_to_delegation_work_item(self, row: Any, description: Any) -> DelegationWorkItem:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return DelegationWorkItem(
            work_item_id=data["work_item_id"],
            run_id=data["run_id"],
            cell_id=data["cell_id"],
            team_instance_id=data.get("team_instance_id") or "",
            team_id=data.get("team_id") or "",
            role_id=data.get("role_id") or "",
            seat_id=data.get("seat_id") or "",
            seat_state_id=data.get("seat_state_id") or "",
            role_runtime_session_id=data.get("role_runtime_session_id") or "",
            parent_work_item_id=data.get("parent_work_item_id"),
            source_role_id=data.get("source_role_id"),
            source_seat_id=data.get("source_seat_id"),
            title=data.get("title") or "",
            summary=data.get("summary") or "",
            kind=data.get("kind") or "execute",
            projection_id=data.get("projection_id") or "",
            phase=coerce_phase(data.get("phase") or "ready"),
            batch_id=data.get("batch_id") or "",
            batch_index=int(data.get("batch_index") or 0),
            deliverable_summary=data.get("deliverable_summary") or "",
            blocked_reason=data.get("blocked_reason") or "",
            handoff_status=data.get("handoff_status") or "pending",
            continuation_source=data.get("continuation_source") or "",
            manager_role_id=data.get("manager_role_id") or "",
            manager_seat_id=data.get("manager_seat_id") or "",
            claimed_by_role_runtime_session_id=data.get("claimed_by_role_runtime_session_id") or "",
            claimed_by_seat_id=data.get("claimed_by_seat_id") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def _sweep_stale_claims(self) -> int:
        """Crash-recovery sweep: clear claim metadata on every in-flight
        work item at startup.

        Process restart drops every in-memory runtime session, but the
        persistent ``claimed_by_*`` fields on the work items still point
        to those dead session IDs. Without this sweep, the dispatcher
        would treat those cards as "actively claimed" forever — they
        would become permanent zombies (Bug C).

        We only touch in-flight phases (RUNNING / WAITING_FOR_* /
        PAUSED / NEEDS_ATTENTION / AWAITING_*) so we never disturb
        terminal cards. The phase itself is left alone; the sweep only
        clears the claim. Active execution can subsequently be re-picked;
        passive AWAITING_* parents remain non-dispatchable while their
        report/review chain (or human) advances them.
        """
        if self._db is None:
            return 0
        async with self._db.execute(
            """SELECT work_item_id, phase, metadata
               FROM delegation_work_items
               WHERE claimed_by_role_runtime_session_id != ''
                  OR claimed_by_seat_id != ''"""
        ) as cursor:
            rows = await cursor.fetchall()
        cleared = 0
        for work_item_id, phase_str, metadata_json in rows:
            try:
                phase = coerce_phase(phase_str)
            except (TypeError, ValueError):
                continue
            if not is_stale_claim_releasable(phase):
                continue
            metadata = _json_loads(metadata_json, {})
            metadata["claimed_by_role_session_id"] = ""
            metadata["claimed_task_id"] = ""
            metadata["claim_swept_at"] = datetime.now().isoformat()
            await self._db.execute(
                """UPDATE delegation_work_items
                   SET claimed_by_role_runtime_session_id='',
                       claimed_by_seat_id='',
                       metadata=?,
                       updated_at=?
                   WHERE work_item_id=?""",
                (_json_dumps(metadata), datetime.now().isoformat(), work_item_id),
            )
            cleared += 1
        if cleared:
            await self._db.commit()
            logger.info(
                f"stale-claim sweep: released {cleared} in-flight work item claims on startup"
            )
        return cleared

    # ── Fix 5 PR2: rich field-level merge by (run_id, role_id) ──────────

    async def _migrate_role_sessions_merge_by_role(self) -> dict[str, int]:
        """Collapse every ``(run_id, role_id)`` group into a single canonical
        row, merging state field-by-field so inbox / memory / adapter
        session tokens survive the collapse.

        Design (see Fix 5 PR2 plan):

        Target PK       ``role-runtime::{run_id}::{role_id}`` (3-segment).
        Groups          every (run_id, role_id) with ≥1 row. A size-1 group
                        whose PK already equals the canonical form is
                        skipped (noop — common case after migration).
        Merge rules     inbox_state, memory_slices_by_work_item, and the
                        list-of-ids columns (background_work_item_ids,
                        manager_role_ids, manager_seat_ids, seat_ids) are
                        UNIONED across all rows. Scalar state columns
                        (focused_work_item_id, status, resume_state,
                        current_work_item, latest_notification,
                        manager_digest, adapter_session_state, team_*,
                        seat_id, seat_state_id) are taken from the
                        "active" row — the one with a populated focus and
                        the highest status priority, tiebroken by
                        updated_at. ``adapter_session_state`` is the
                        externally-observable LLM / codex session token:
                        losing rows' states are retained under
                        ``metadata.adapter_session_state_audit`` so the
                        old codex sessions can be recovered for debugging.
        References      foreign references in delegation_work_items
                        (role_runtime_session_id,
                        claimed_by_role_runtime_session_id), seat_states,
                        and JSON metadata (tasks.metadata.delegation_role_
                        session_id, delegation_work_items.metadata.
                        assigned_role_runtime_id) are all redirected to
                        the canonical PK before losers are deleted.
        Idempotent      re-running the migration is a noop (every row is
                        already canonical).

        Returns counters for observability / tests.
        """
        # Deferred import — the company_runtime module transitively imports
        # layer2 phase machinery, which we also use; importing at module
        # scope would create a cycle during initial bootstrap.
        from opc.layer2_organization.company_runtime import canonical_role_session_id

        if self._db is None:
            return {
                "groups": 0,
                "canonical_written": 0,
                "deleted": 0,
                "refs_updated": 0,
            }

        stats = {
            "groups": 0,
            "canonical_written": 0,
            "deleted": 0,
            "refs_updated": 0,
        }

        # Collect every (run_id, role_id) with any rows — unlike the old
        # "duplicate" migration, this pass also catches single legacy rows
        # whose PK is not the 3-segment canonical form.
        async with self._db.execute(
            """SELECT DISTINCT run_id, role_id
               FROM role_runtime_sessions
               WHERE run_id != '' AND role_id != ''"""
        ) as cursor:
            groups = await cursor.fetchall()

        if not groups:
            return stats

        for run_id, role_id in groups:
            try:
                canonical_id = canonical_role_session_id(
                    run_id=str(run_id), role_id=str(role_id)
                )
            except ValueError:
                logger.warning(
                    f"role-session merge: cannot build canonical ID for "
                    f"run={run_id} role={role_id}; skipping"
                )
                continue

            async with self._db.execute(
                """SELECT * FROM role_runtime_sessions
                   WHERE run_id=? AND role_id=?
                   ORDER BY updated_at DESC""",
                (str(run_id), str(role_id)),
            ) as cursor:
                rows = await cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]

            if not rows:
                continue

            row_dicts = [dict(zip(columns, row)) for row in rows]

            # Fast path: single row already at canonical PK → nothing to do.
            if len(row_dicts) == 1 and row_dicts[0]["role_session_id"] == canonical_id:
                continue

            stats["groups"] += 1

            merged = self._merge_role_session_rows(
                rows=row_dicts, canonical_id=canonical_id
            )

            # Upsert the merged row under the canonical PK in both tables.
            for table in ("role_runtime_sessions", "delegation_role_sessions"):
                await self._upsert_role_session_row(table=table, row=merged)
            stats["canonical_written"] += 1

            # Redirect every foreign reference from any non-canonical PK
            # in the group to the canonical PK. We do this before deleting
            # the rows so a crash mid-migration leaves references resolvable.
            losers = [
                rd["role_session_id"]
                for rd in row_dicts
                if rd["role_session_id"] != canonical_id
            ]
            for loser_id in losers:
                refs = await self._redirect_role_session_references(
                    source_id=loser_id, target_id=canonical_id
                )
                stats["refs_updated"] += refs

            # Delete the orphaned rows from both tables.
            for loser_id in losers:
                for table in ("role_runtime_sessions", "delegation_role_sessions"):
                    await self._db.execute(
                        f"DELETE FROM {table} WHERE role_session_id=?",
                        (loser_id,),
                    )
                stats["deleted"] += 1

        await self._db.commit()
        if stats["groups"]:
            logger.info(
                "role-session merge: "
                f"groups={stats['groups']} "
                f"canonical_written={stats['canonical_written']} "
                f"deleted={stats['deleted']} "
                f"refs_updated={stats['refs_updated']}"
            )
        return stats

    # --- Dynamic reorg persistence ---

    # --- Session memory ---

    # --- Events ---

    # --- Costs ---

    # --- Approvals and autonomy ---

    # --- External sessions ---

    # --- Execution checkpoints ---

    # --- Runtime V2 persistence ---

    # --- Organizations ---

    # --- Goals ---

    # --- Org Agents ---

    # --- Atomic task checkout / release ---

    # --- Cost events ---
