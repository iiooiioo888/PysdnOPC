"""CollaborationStoreMixin — 訊息/交接/會議/檢查點/組織相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opc.database._utils import _json_dumps, _json_loads
import uuid
from datetime import datetime

from opc.core.models import (
    AgentMessage,
    ApprovalDecision,
    CommsSemanticType,
    CommsState,
    CommsTransportKind,
    CostEvent,
    ExecutionCheckpoint,
    ExternalSession,
    Goal,
    GoalLevel,
    GoalStatus,
    MessageStatus,
    MessageUrgency,
    OPCEvent,
    OrgAgent,
    Organization,
    TaskStatus,
)

if TYPE_CHECKING:
    from opc.database.store import OPCStore


class CollaborationStoreMixin:
    """Mixin providing 訊息/交接/會議/檢查點/組織相關方法 for OPCStore."""

    async def save_message(self, msg: AgentMessage) -> None:
        assert self._db
        metadata = {
            **dict(msg.metadata or {}),
            "transport_kind": getattr(getattr(msg, "transport_kind", None), "value", getattr(msg, "transport_kind", "")) or "",
            "semantic_type": getattr(getattr(msg, "semantic_type", None), "value", getattr(msg, "semantic_type", "")) or "",
            "comms_state": getattr(getattr(msg, "comms_state", None), "value", getattr(msg, "comms_state", "")) or "",
            "correlation_id": str(getattr(msg, "correlation_id", "") or "").strip(),
            "refs": dict(getattr(msg, "refs", {}) or {}),
        }
        await self._db.execute(
            """INSERT OR REPLACE INTO agent_messages
            (msg_id, msg_type, from_agent, to_agents, subject, body, context_ref, urgency,
             reply_needed, requires_ack, timeout_action, reply_to_msg_id, task_id, status,
             timestamp, processed_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.msg_id,
                msg.msg_type,
                msg.from_agent,
                _json_dumps(msg.to_agents),
                msg.subject,
                msg.body,
                msg.context_ref,
                msg.urgency.value,
                int(msg.reply_needed),
                int(msg.requires_ack),
                msg.timeout_action,
                msg.reply_to_msg_id,
                msg.task_id,
                msg.status.value,
                msg.timestamp.isoformat(),
                msg.processed_at.isoformat() if msg.processed_at else None,
                _json_dumps(metadata),
            ),
        )
        await self._db.commit()

    def _row_to_message(self, row: Any, description: Any) -> AgentMessage:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        metadata = _json_loads(data.get("metadata"), {})
        return AgentMessage(
            msg_id=data["msg_id"],
            msg_type=data["msg_type"],
            from_agent=data["from_agent"],
            to_agents=_json_loads(data["to_agents"], []),
            subject=data["subject"],
            body=data["body"],
            context_ref=data["context_ref"],
            urgency=MessageUrgency(data["urgency"]),
            reply_needed=bool(data["reply_needed"]),
            requires_ack=bool(data.get("requires_ack", 0)),
            timeout_action=data["timeout_action"],
            reply_to_msg_id=data.get("reply_to_msg_id"),
            task_id=data.get("task_id"),
            status=MessageStatus(data.get("status") or MessageStatus.SENT.value),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            processed_at=datetime.fromisoformat(data["processed_at"]) if data.get("processed_at") else None,
            transport_kind=CommsTransportKind(str(metadata.get("transport_kind") or CommsTransportKind.DM.value)),
            semantic_type=CommsSemanticType(str(metadata.get("semantic_type") or CommsSemanticType.WORK_UPDATE.value)),
            comms_state=CommsState(str(metadata.get("comms_state") or CommsState.OPEN.value)),
            correlation_id=str(metadata.get("correlation_id", "") or "").strip(),
            refs=dict(metadata.get("refs", {}) or {}),
            metadata=metadata,
        )

    async def get_message(self, msg_id: str) -> AgentMessage | None:
        assert self._db
        async with self._db.execute("SELECT * FROM agent_messages WHERE msg_id = ?", (msg_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_message(row, cursor.description)

    async def update_message_status(
        self,
        msg_id: str,
        status: MessageStatus,
        processed_at: datetime | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            "UPDATE agent_messages SET status = ?, processed_at = ? WHERE msg_id = ?",
            (status.value, processed_at.isoformat() if processed_at else None, msg_id),
        )
        await self._db.commit()

    async def get_messages_for_agent(
        self,
        agent_id: str,
        limit: int = 20,
        unread_only: bool = False,
        task_id: str | None = None,
        task_ids: list[str] | None = None,
    ) -> list[AgentMessage]:
        assert self._db
        query = """SELECT * FROM agent_messages
        WHERE to_agents LIKE ?"""
        params: list[Any] = [f'%"{agent_id}"%']
        if unread_only:
            query += " AND status IN (?, ?)"
            params.extend([MessageStatus.SENT.value, MessageStatus.DELIVERED.value])
        scope_ids = [str(item).strip() for item in list(task_ids or []) if str(item).strip()]
        if not scope_ids and task_id:
            scope_ids = [str(task_id).strip()]
        if scope_ids:
            scope_clauses: list[str] = []
            for scope_id in scope_ids:
                scope_clauses.append("(task_id = ? OR context_ref = ?)")
                params.extend([scope_id, scope_id])
            query += " AND (" + " OR ".join(scope_clauses) + ")"
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    async def get_outbox_for_agent(
        self,
        agent_id: str,
        limit: int = 20,
        task_id: str | None = None,
        task_ids: list[str] | None = None,
    ) -> list[AgentMessage]:
        assert self._db
        query = "SELECT * FROM agent_messages WHERE from_agent = ?"
        params: list[Any] = [agent_id]
        scope_ids = [str(item).strip() for item in list(task_ids or []) if str(item).strip()]
        if not scope_ids and task_id:
            scope_ids = [str(task_id).strip()]
        if scope_ids:
            scope_clauses: list[str] = []
            for scope_id in scope_ids:
                scope_clauses.append("(task_id = ? OR context_ref = ?)")
                params.extend([scope_id, scope_id])
            query += " AND (" + " OR ".join(scope_clauses) + ")"
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    async def list_agent_messages_for_tasks(
        self,
        task_ids: list[str],
        limit: int = 50,
    ) -> list[AgentMessage]:
        assert self._db
        clean_ids = [str(item).strip() for item in list(task_ids or []) if str(item).strip()]
        if not clean_ids:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        for task_id in clean_ids:
            clauses.append("(task_id = ? OR context_ref = ?)")
            params.extend([task_id, task_id])
        query = "SELECT * FROM agent_messages WHERE " + " OR ".join(clauses)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(max(1, int(limit or 50)))
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    async def get_replies_for_message(self, msg_id: str) -> list[AgentMessage]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM agent_messages WHERE reply_to_msg_id = ? ORDER BY timestamp ASC",
            (msg_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_message(row, cursor.description) for row in rows]

    async def save_event(self, event: OPCEvent) -> None:
        assert self._db
        await self._db.execute(
            "INSERT INTO events (event_id, event_type, payload, timestamp) VALUES (?, ?, ?, ?)",
            (event.event_id, event.event_type, _json_dumps(event.payload), event.timestamp.isoformat()),
        )
        await self._db.commit()

    async def get_events(self, event_type: str | None = None, limit: int = 50) -> list[dict]:
        assert self._db
        query = "SELECT * FROM events"
        params: list[Any] = []
        if event_type:
            query += " WHERE event_type = ?"
            params.append(event_type)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in rows]

    async def record_cost(
        self,
        task_id: str | None,
        agent_id: str | None,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost: float,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT INTO cost_records (task_id, agent_id, model, tokens_in, tokens_out, cost, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, agent_id, model, tokens_in, tokens_out, cost, datetime.now().isoformat()),
        )
        await self._db.commit()

    async def get_total_cost(self, project_id: str | None = None) -> dict:
        assert self._db
        if project_id:
            query = """SELECT SUM(tokens_in), SUM(tokens_out), SUM(cost), COUNT(*)
                      FROM cost_records cr JOIN tasks t ON cr.task_id = t.id
                      WHERE t.project_id = ?"""
            params: tuple[Any, ...] = (project_id,)
        else:
            query = "SELECT SUM(tokens_in), SUM(tokens_out), SUM(cost), COUNT(*) FROM cost_records"
            params = ()
        async with self._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return {
                "total_tokens_in": row[0] or 0,
                "total_tokens_out": row[1] or 0,
                "total_cost": row[2] or 0.0,
                "total_calls": row[3] or 0,
            }

    async def record_approval(
        self,
        decision: ApprovalDecision,
        task_id: str | None,
        project_id: str,
        action_kind: str,
        action_name: str,
        target_agent: str = "",
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT INTO approval_records
            (task_id, project_id, action_kind, action_name, target_agent, decision_action,
             risk_level, confidence, rationale, policy_source, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                project_id or "default",
                action_kind,
                action_name,
                target_agent,
                decision.action.value,
                decision.risk_level.value,
                decision.confidence,
                decision.rationale,
                decision.policy_source,
                _json_dumps(decision.metadata),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def get_recent_approvals(
        self,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        assert self._db
        query = "SELECT * FROM approval_records"
        params: list[Any] = []
        if project_id:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in rows]

    async def get_autonomy_stats(self, project_id: str | None = None) -> dict[str, Any]:
        assert self._db
        query = "SELECT decision_action, COUNT(*) FROM approval_records"
        params: list[Any] = []
        if project_id:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " GROUP BY decision_action"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        counts = {row[0]: row[1] for row in rows}
        total = sum(counts.values())
        auto = counts.get("auto_approve", 0)
        escalate = counts.get("escalate", 0)
        reject = counts.get("reject", 0)
        return {
            "total": total,
            "auto_approved": auto,
            "escalated": escalate,
            "rejected": reject,
            "auto_approval_rate": (auto / total) if total else 0.0,
        }

    async def save_external_session(self, session: ExternalSession) -> None:
        assert self._db
        self._assert_project_write_scope(
            getattr(session, "project_id", None),
            operation="save_external_session",
            entity=f"external session task={getattr(session, 'task_id', '')!r}",
        )
        session_key = self._external_session_key(session)
        await self._db.execute(
            """INSERT OR REPLACE INTO external_sessions
            (session_key, agent_type, project_id, session_id, opc_session_id, task_id, workspace_path, run_mode, status, metadata, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_key,
                session.agent_type,
                session.project_id,
                session.session_id,
                session.opc_session_id,
                session.task_id,
                session.workspace_path,
                session.run_mode,
                session.status,
                _json_dumps(session.metadata),
                session.updated_at.isoformat(),
            ),
        )
        await self._close_replaced_external_session_rows(session, session_key=session_key)
        await self._db.commit()

    async def _close_replaced_external_session_rows(
        self,
        session: ExternalSession,
        *,
        session_key: str,
    ) -> None:
        """Close stale placeholder rows once the real provider session finishes."""
        assert self._db
        status = str(session.status or "").strip().lower()
        if status not in {
            "done",
            "completed",
            "complete",
            "finished",
            "failed",
            "cancelled",
            "canceled",
            "suspended",
            "hard_timeout",
            "idle_timeout",
            "startup_timeout",
            "denied",
            "rejected",
        }:
            return
        task_id = str(session.task_id or "").strip()
        if not task_id:
            return
        await self._db.execute(
            """
            UPDATE external_sessions
            SET status = ?, updated_at = ?
            WHERE project_id = ?
              AND agent_type = ?
              AND task_id = ?
              AND COALESCE(opc_session_id, '') = ?
              AND session_key != ?
              AND status IN ('starting', 'running', 'working')
            """,
            (
                session.status,
                session.updated_at.isoformat(),
                session.project_id,
                session.agent_type,
                task_id,
                str(session.opc_session_id or "").strip(),
                session_key,
            ),
        )

    async def get_external_session(
        self,
        agent_type: str,
        project_id: str = "default",
        *,
        opc_session_id: str | None = None,
        task_id: str | None = None,
    ) -> ExternalSession | None:
        assert self._db
        query = "SELECT * FROM external_sessions WHERE agent_type = ? AND project_id = ?"
        params: list[Any] = [agent_type, project_id]
        if opc_session_id:
            query += " AND opc_session_id = ?"
            params.append(opc_session_id)
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        query += " ORDER BY updated_at DESC LIMIT 1"
        async with self._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            data = dict(zip(cols, row))
            return ExternalSession(
                agent_type=data["agent_type"],
                project_id=data["project_id"],
                session_id=data["session_id"],
                opc_session_id=data.get("opc_session_id"),
                task_id=data["task_id"],
                workspace_path=data["workspace_path"],
                run_mode=data["run_mode"],
                status=data["status"],
                metadata=_json_loads(data["metadata"], {}),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )

    async def get_latest_external_session_for_task(
        self,
        project_id: str,
        task_id: str,
    ) -> ExternalSession | None:
        assert self._db
        async with self._db.execute(
            """
            SELECT * FROM external_sessions
            WHERE project_id = ? AND task_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (project_id, task_id),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            data = dict(zip(cols, row))
            return ExternalSession(
                agent_type=data["agent_type"],
                project_id=data["project_id"],
                session_id=data["session_id"],
                opc_session_id=data.get("opc_session_id"),
                task_id=data["task_id"],
                workspace_path=data["workspace_path"],
                run_mode=data["run_mode"],
                status=data["status"],
                metadata=_json_loads(data["metadata"], {}),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )

    async def list_external_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        opc_session_id: str | None = None,
        limit: int = 50,
    ) -> list[ExternalSession]:
        assert self._db
        query = "SELECT * FROM external_sessions WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if opc_session_id:
            query += " AND opc_session_id = ?"
            params.append(opc_session_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit or 50)))
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        return [
            ExternalSession(
                agent_type=data["agent_type"],
                project_id=data["project_id"],
                session_id=data["session_id"],
                opc_session_id=data.get("opc_session_id"),
                task_id=data["task_id"],
                workspace_path=data["workspace_path"],
                run_mode=data["run_mode"],
                status=data["status"],
                metadata=_json_loads(data["metadata"], {}),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )
            for data in (dict(zip(cols, row)) for row in rows)
        ]

    def _external_session_key(self, session: ExternalSession) -> str:
        return "|".join(
            [
                session.agent_type,
                session.project_id or "default",
                session.opc_session_id or "",
                session.task_id or "",
                session.session_id,
            ]
        )

    async def save_execution_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO execution_checkpoints
            (checkpoint_id, project_id, session_id, checkpoint_type, status, task_id, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                checkpoint.checkpoint_id,
                checkpoint.project_id,
                checkpoint.session_id,
                checkpoint.checkpoint_type,
                checkpoint.status,
                checkpoint.task_id,
                _json_dumps(checkpoint.payload),
                checkpoint.created_at.isoformat(),
                checkpoint.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_or_create_active_execution_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        checkpoint_types: list[str] | tuple[str, ...] | set[str],
        create_if_missing: bool = True,
    ) -> tuple[ExecutionCheckpoint | None, bool]:
        """Atomically reuse or create one active checkpoint for a session scope.

        ``BEGIN IMMEDIATE`` serializes checkpoint creation across independent
        controller/store connections.  It also repairs historical duplicate
        active rows in the same transaction, so concurrent startup/shutdown
        reconcilers can never supersede each other's newly-created checkpoint
        and leave the scope without a durable recovery point.
        """

        assert self._db
        clean_types = sorted(
            {
                str(item).strip()
                for item in checkpoint_types
                if str(item).strip()
            }
        )
        project_id = str(checkpoint.project_id or "default").strip() or "default"
        session_id = str(checkpoint.session_id or "").strip()
        checkpoint_type = str(checkpoint.checkpoint_type or "").strip()
        if not session_id:
            raise ValueError("active execution checkpoint requires session_id")
        if checkpoint_type not in clean_types:
            raise ValueError(
                "checkpoint_type must be included in the active checkpoint scope"
            )

        placeholders = ", ".join("?" for _ in clean_types)
        try:
            await self._db.execute("BEGIN IMMEDIATE")
            async with self._db.execute(
                f"""SELECT * FROM execution_checkpoints
                WHERE project_id = ?
                  AND session_id = ?
                  AND checkpoint_type IN ({placeholders})
                  AND status IN ('pending', 'resuming')
                ORDER BY updated_at DESC, created_at DESC, checkpoint_id DESC""",
                (project_id, session_id, *clean_types),
            ) as cursor:
                rows = await cursor.fetchall()
                cols = [description[0] for description in cursor.description]

            if rows:
                decoded = [dict(zip(cols, row)) for row in rows]
                winner_data = decoded[0]
                winner_id = str(winner_data["checkpoint_id"])
                now = datetime.now().isoformat()
                for duplicate in decoded[1:]:
                    duplicate_id = str(duplicate.get("checkpoint_id", "") or "").strip()
                    if not duplicate_id:
                        continue
                    payload = _json_loads(duplicate.get("payload"), {})
                    payload["superseded_at"] = now
                    payload["superseded_by_checkpoint_id"] = winner_id
                    await self._db.execute(
                        """UPDATE execution_checkpoints
                        SET status = 'superseded', payload = ?, updated_at = ?
                        WHERE checkpoint_id = ? AND status IN ('pending', 'resuming')""",
                        (_json_dumps(payload), now, duplicate_id),
                    )
                await self._db.commit()
                return (
                    ExecutionCheckpoint(
                        checkpoint_id=winner_id,
                        project_id=str(winner_data["project_id"]),
                        session_id=winner_data.get("session_id"),
                        checkpoint_type=str(winner_data["checkpoint_type"]),
                        status=str(winner_data["status"]),
                        task_id=winner_data.get("task_id"),
                        payload=_json_loads(winner_data.get("payload"), {}),
                        created_at=datetime.fromisoformat(str(winner_data["created_at"])),
                        updated_at=datetime.fromisoformat(str(winner_data["updated_at"])),
                    ),
                    False,
                )

            if not create_if_missing:
                await self._db.commit()
                return None, False

            await self._db.execute(
                """INSERT INTO execution_checkpoints
                (checkpoint_id, project_id, session_id, checkpoint_type, status,
                 task_id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint.checkpoint_id,
                    project_id,
                    session_id,
                    checkpoint_type,
                    checkpoint.status,
                    checkpoint.task_id,
                    _json_dumps(checkpoint.payload),
                    checkpoint.created_at.isoformat(),
                    checkpoint.updated_at.isoformat(),
                ),
            )
            await self._db.commit()
            return checkpoint, True
        except Exception:
            await self._db.rollback()
            raise

    async def normalize_active_execution_checkpoints(
        self,
        *,
        project_id: str,
        session_id: str,
        checkpoint_types: list[str] | tuple[str, ...] | set[str],
    ) -> ExecutionCheckpoint | None:
        """Return one active scope owner while superseding duplicate rows.

        Unlike creation, startup reconciliation must not manufacture a
        checkpoint merely because none exists.  This wrapper reuses the same
        serialized transaction and duplicate winner rule with insertion
        explicitly disabled.
        """

        clean_types = sorted(
            {
                str(item).strip()
                for item in checkpoint_types
                if str(item).strip()
            }
        )
        if not clean_types or not str(session_id or "").strip():
            return None
        candidate = ExecutionCheckpoint(
            project_id=str(project_id or "default").strip() or "default",
            session_id=str(session_id or "").strip(),
            checkpoint_type=clean_types[0],
        )
        checkpoint, _created = await self.get_or_create_active_execution_checkpoint(
            candidate,
            checkpoint_types=clean_types,
            create_if_missing=False,
        )
        return checkpoint

    async def compare_and_set_execution_checkpoint(
        self,
        checkpoint_id: str,
        *,
        expected_statuses: list[str] | tuple[str, ...] | set[str],
        status: str,
        payload: dict[str, Any],
        updated_at: datetime | None = None,
    ) -> bool:
        """Atomically transition a checkpoint only from an expected state.

        The conditional UPDATE is the cross-controller guard for checkpoint
        consumption.  In-memory scope locks serialize one Office controller;
        this CAS prevents a standalone CLI/controller from claiming the same
        pending runtime concurrently.
        """

        assert self._db
        expected = [
            str(item).strip()
            for item in expected_statuses
            if str(item).strip()
        ]
        if not checkpoint_id or not expected:
            return False
        placeholders = ", ".join("?" for _ in expected)
        cursor = await self._db.execute(
            f"""UPDATE execution_checkpoints
            SET status = ?, payload = ?, updated_at = ?
            WHERE checkpoint_id = ? AND status IN ({placeholders})""",
            (
                str(status or "").strip(),
                _json_dumps(dict(payload or {})),
                (updated_at or datetime.now()).isoformat(),
                checkpoint_id,
                *expected,
            ),
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def complete_execution_checkpoint_and_reopen_ui_anchor(
        self,
        checkpoint_id: str,
        *,
        project_id: str,
        session_id: str,
        expected_status: str,
        status: str,
        payload: dict[str, Any],
        ui_anchor_task_id: str = "",
        updated_at: datetime | None = None,
    ) -> bool:
        """Atomically complete a runtime handoff and reopen its UI anchor.

        The anchor must never become chat-runnable if a concurrent Stop already
        took checkpoint ownership back.  Keeping both writes in one SQLite
        transaction makes the checkpoint CAS the gate for the UI projection.
        """

        assert self._db
        checkpoint_id = str(checkpoint_id or "").strip()
        project_id = str(project_id or "default").strip() or "default"
        session_id = str(session_id or "").strip()
        expected_status = str(expected_status or "").strip()
        status = str(status or "").strip()
        ui_anchor_task_id = str(ui_anchor_task_id or "").strip()
        if not checkpoint_id or not session_id or not expected_status or not status:
            return False
        now = updated_at or datetime.now()
        try:
            await self._db.execute("BEGIN IMMEDIATE")
            cursor = await self._db.execute(
                """UPDATE execution_checkpoints
                SET status = ?, payload = ?, updated_at = ?
                WHERE checkpoint_id = ?
                  AND project_id = ?
                  AND session_id = ?
                  AND status = ?""",
                (
                    status,
                    _json_dumps(dict(payload or {})),
                    now.isoformat(),
                    checkpoint_id,
                    project_id,
                    session_id,
                    expected_status,
                ),
            )
            if cursor.rowcount != 1:
                await self._db.rollback()
                return False
            if ui_anchor_task_id:
                await self._db.execute(
                    """UPDATE tasks
                    SET status = ?, execution_lock = 0, execution_locked_at = NULL
                    WHERE id = ? AND project_id = ? AND status = ?""",
                    (
                        TaskStatus.IDLE.value,
                        ui_anchor_task_id,
                        project_id,
                        TaskStatus.CANCELLED.value,
                    ),
                )
            await self._db.commit()
            return True
        except Exception:
            await self._db.rollback()
            raise

    async def get_execution_checkpoints(
        self,
        project_id: str = "default",
        session_id: str | None = None,
        checkpoint_types: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> list[ExecutionCheckpoint]:
        assert self._db
        query = "SELECT * FROM execution_checkpoints WHERE project_id = ?"
        params: list[Any] = [project_id]
        clean_statuses = [str(status).strip() for status in list(statuses or []) if str(status).strip()]
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(clean_statuses)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if checkpoint_types:
            placeholders = ", ".join("?" for _ in checkpoint_types)
            query += f" AND checkpoint_type IN ({placeholders})"
            params.extend(checkpoint_types)
        query += " ORDER BY updated_at DESC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                ExecutionCheckpoint(
                    checkpoint_id=data["checkpoint_id"],
                    project_id=data["project_id"],
                    session_id=data.get("session_id"),
                    checkpoint_type=data["checkpoint_type"],
                    status=data["status"],
                    task_id=data["task_id"],
                    payload=_json_loads(data["payload"], {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                    updated_at=datetime.fromisoformat(data["updated_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]

    async def get_pending_checkpoints(
        self,
        project_id: str = "default",
        session_id: str | None = None,
        checkpoint_types: list[str] | None = None,
    ) -> list[ExecutionCheckpoint]:
        return await self.get_execution_checkpoints(
            project_id=project_id,
            session_id=session_id,
            checkpoint_types=checkpoint_types,
            statuses=["pending"],
        )

    async def get_latest_pending_checkpoint(
        self,
        project_id: str = "default",
        session_id: str | None = None,
    ) -> ExecutionCheckpoint | None:
        checkpoints = await self.get_pending_checkpoints(project_id=project_id, session_id=session_id)
        return checkpoints[0] if checkpoints else None

    async def resolve_execution_checkpoint(self, checkpoint_id: str, status: str = "resolved") -> None:
        assert self._db
        await self._db.execute(
            "UPDATE execution_checkpoints SET status = ?, updated_at = ? WHERE checkpoint_id = ?",
            (status, datetime.now().isoformat(), checkpoint_id),
        )
        await self._db.commit()

    async def supersede_pending_checkpoints(
        self,
        *,
        project_id: str = "default",
        task_id: str | None = None,
        session_id: str | None = None,
        checkpoint_types: list[str] | None = None,
        basis_hash: str | None = None,
        exclude_checkpoint_id: str | None = None,
    ) -> list[str]:
        assert self._db
        query = "SELECT * FROM execution_checkpoints WHERE project_id = ? AND status = 'pending'"
        params: list[Any] = [project_id]
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if checkpoint_types:
            placeholders = ", ".join("?" for _ in checkpoint_types)
            query += f" AND checkpoint_type IN ({placeholders})"
            params.extend(checkpoint_types)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]

        superseded_ids: list[str] = []
        now = datetime.now().isoformat()
        for data in (dict(zip(cols, row)) for row in rows):
            checkpoint_id = str(data.get("checkpoint_id", "") or "").strip()
            if not checkpoint_id or checkpoint_id == str(exclude_checkpoint_id or "").strip():
                continue
            payload = _json_loads(data.get("payload"), {})
            existing_basis_hash = str(payload.get("basis_hash", "") or "").strip()
            if basis_hash and existing_basis_hash and existing_basis_hash == basis_hash:
                continue
            payload["superseded_at"] = now
            if exclude_checkpoint_id:
                payload["superseded_by_checkpoint_id"] = str(exclude_checkpoint_id)
            await self._db.execute(
                "UPDATE execution_checkpoints SET status = ?, payload = ?, updated_at = ? WHERE checkpoint_id = ?",
                ("superseded", _json_dumps(payload), now, checkpoint_id),
            )
            superseded_ids.append(checkpoint_id)
        if superseded_ids:
            await self._db.commit()
        return superseded_ids

    async def save_runtime_session(
        self,
        *,
        runtime_session_id: str,
        project_id: str = "default",
        session_id: str | None = None,
        task_id: str | None = None,
        status: str = "running",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        self._assert_project_write_scope(
            project_id,
            operation="save_runtime_session",
            entity=f"runtime session {runtime_session_id!r}",
        )
        now = datetime.now().isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_sessions
            (runtime_session_id, project_id, session_id, task_id, status, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM runtime_sessions WHERE runtime_session_id = ?), ?), ?)""",
            (
                runtime_session_id,
                project_id,
                session_id,
                task_id,
                status,
                _json_dumps(metadata or {}),
                runtime_session_id,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def get_runtime_session(self, runtime_session_id: str) -> dict[str, Any] | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM runtime_sessions WHERE runtime_session_id = ?",
            (runtime_session_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cursor.description]
        data = dict(zip(cols, row))
        data["metadata"] = _json_loads(data.get("metadata"), {})
        return data

    async def list_runtime_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        assert self._db
        query = "SELECT * FROM runtime_sessions WHERE 1=1"
        params: list[Any] = []
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        if task_id:
            query += " AND task_id = ?"
            params.append(task_id)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit or 50)))
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        sessions: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["metadata"] = _json_loads(data.get("metadata"), {})
            sessions.append(data)
        return sessions

    async def save_runtime_event(
        self,
        runtime_session_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT INTO runtime_events
            (event_id, runtime_session_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                runtime_session_id,
                event_type,
                _json_dumps(payload or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def list_runtime_events(
        self,
        runtime_session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        assert self._db
        async with self._db.execute(
            """
            SELECT * FROM (
                SELECT * FROM runtime_events
                WHERE runtime_session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
            )
            ORDER BY created_at ASC
            """,
            (runtime_session_id, max(1, int(limit or 100))),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["payload"] = _json_loads(data.get("payload"), {})
            results.append(data)
        return results

    async def save_runtime_transcript_entry(
        self,
        *,
        runtime_session_id: str,
        task_id: str | None = None,
        session_id: str | None = None,
        message_id: str = "",
        role: str = "assistant",
        entry_type: str = "message",
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_transcript_entries
            (entry_id, runtime_session_id, task_id, session_id, message_id, role, entry_type, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                runtime_session_id,
                task_id,
                session_id,
                message_id,
                role,
                entry_type,
                content,
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def save_runtime_tool_call(
        self,
        *,
        runtime_session_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_tool_calls
            (call_record_id, runtime_session_id, task_id, session_id, message_id, tool_call_id, tool_name, arguments, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{runtime_session_id}|{tool_call_id or uuid.uuid4().hex}",
                runtime_session_id,
                task_id,
                session_id,
                message_id,
                tool_call_id,
                tool_name,
                _json_dumps(arguments or {}),
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def save_runtime_tool_result(
        self,
        *,
        runtime_session_id: str,
        tool_name: str,
        payload: dict[str, Any] | None = None,
        tool_call_id: str = "",
        task_id: str | None = None,
        session_id: str | None = None,
        message_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_tool_results
            (result_record_id, runtime_session_id, task_id, session_id, message_id, tool_call_id, tool_name, payload, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{runtime_session_id}|{tool_call_id or tool_name}|{uuid.uuid4().hex}",
                runtime_session_id,
                task_id,
                session_id,
                message_id,
                tool_call_id,
                tool_name,
                _json_dumps(payload or {}),
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def list_runtime_transcript_entries(
        self,
        runtime_session_id: str,
    ) -> list[dict[str, Any]]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM runtime_transcript_entries WHERE runtime_session_id = ? ORDER BY created_at ASC",
            (runtime_session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        return [
            {**dict(zip(cols, row)), "metadata": _json_loads(dict(zip(cols, row)).get("metadata"), {})}
            for row in rows
        ]

    async def list_runtime_tool_calls(
        self,
        runtime_session_id: str,
    ) -> list[dict[str, Any]]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM runtime_tool_calls WHERE runtime_session_id = ? ORDER BY created_at ASC",
            (runtime_session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["arguments"] = _json_loads(data.get("arguments"), {})
            data["metadata"] = _json_loads(data.get("metadata"), {})
            results.append(data)
        return results

    async def list_runtime_tool_results(
        self,
        runtime_session_id: str,
    ) -> list[dict[str, Any]]:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM runtime_tool_results WHERE runtime_session_id = ? ORDER BY created_at ASC",
            (runtime_session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["payload"] = _json_loads(data.get("payload"), {})
            data["metadata"] = _json_loads(data.get("metadata"), {})
            results.append(data)
        return results

    async def save_runtime_permission_grant(
        self,
        *,
        runtime_session_id: str,
        project_id: str = "default",
        scope: str,
        tool_name: str,
        candidate: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_permission_grants
            (grant_id, runtime_session_id, project_id, scope, tool_name, candidate, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{runtime_session_id}|{scope}|{tool_name}|{candidate}",
                runtime_session_id,
                project_id,
                scope,
                tool_name,
                candidate,
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def list_runtime_permission_grants(
        self,
        *,
        runtime_session_id: str | None = None,
        project_id: str | None = None,
        scopes: list[str] | None = None,
        tool_name: str | None = None,
    ) -> list[dict[str, Any]]:
        assert self._db
        query = "SELECT * FROM runtime_permission_grants WHERE 1=1"
        params: list[Any] = []
        if runtime_session_id:
            query += " AND runtime_session_id = ?"
            params.append(runtime_session_id)
        if project_id:
            query += " AND project_id = ?"
            params.append(project_id)
        if scopes:
            placeholders = ",".join("?" for _ in scopes)
            query += f" AND scope IN ({placeholders})"
            params.extend(scopes)
        if tool_name:
            query += " AND tool_name = ?"
            params.append(tool_name)
        query += " ORDER BY created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        results: list[dict[str, Any]] = []
        for row in rows:
            data = dict(zip(cols, row))
            data["metadata"] = _json_loads(data.get("metadata"), {})
            results.append(data)
        return results

    async def save_runtime_subagent_run(
        self,
        *,
        subagent_run_id: str,
        runtime_session_id: str,
        agent_id: str,
        profile: str,
        status: str,
        task_id: str | None = None,
        worktree_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        now = datetime.now().isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_subagent_runs
            (subagent_run_id, runtime_session_id, task_id, agent_id, profile, status, worktree_path, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM runtime_subagent_runs WHERE subagent_run_id = ?), ?), ?)""",
            (
                subagent_run_id,
                runtime_session_id,
                task_id,
                agent_id,
                profile,
                status,
                worktree_path,
                _json_dumps(metadata or {}),
                subagent_run_id,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def save_runtime_worktree_session(
        self,
        *,
        worktree_session_id: str,
        runtime_session_id: str,
        path: str,
        status: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        now = datetime.now().isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_worktree_sessions
            (worktree_session_id, runtime_session_id, task_id, path, status, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM runtime_worktree_sessions WHERE worktree_session_id = ?), ?), ?)""",
            (
                worktree_session_id,
                runtime_session_id,
                task_id,
                path,
                status,
                _json_dumps(metadata or {}),
                worktree_session_id,
                now,
                now,
            ),
        )
        await self._db.commit()

    async def save_runtime_compaction_boundary(
        self,
        *,
        boundary_id: str,
        runtime_session_id: str,
        summary: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO runtime_compaction_boundaries
            (boundary_id, runtime_session_id, task_id, summary, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                boundary_id,
                runtime_session_id,
                task_id,
                summary,
                _json_dumps(metadata or {}),
                datetime.now().isoformat(),
            ),
        )
        await self._db.commit()

    async def save_organization(self, org: Organization) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO organizations
            (org_id, name, description, status, company_profile,
             budget_monthly_cents, spent_monthly_cents, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                org.org_id,
                org.name,
                org.description,
                org.status,
                org.company_profile,
                org.budget_monthly_cents,
                org.spent_monthly_cents,
                _json_dumps(org.metadata),
                org.created_at.isoformat(),
                org.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_organization(self, org_id: str) -> Organization | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM organizations WHERE org_id = ?", (org_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            data = dict(zip(cols, row))
            return Organization(
                org_id=data["org_id"],
                name=data["name"],
                description=data.get("description") or "",
                status=data.get("status") or "active",
                company_profile=data.get("company_profile") or "corporate",
                budget_monthly_cents=int(data.get("budget_monthly_cents") or 0),
                spent_monthly_cents=int(data.get("spent_monthly_cents") or 0),
                metadata=_json_loads(data.get("metadata"), {}),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )

    async def list_organizations(self, status: str | None = None) -> list[Organization]:
        assert self._db
        query = "SELECT * FROM organizations"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            results: list[Organization] = []
            for row in rows:
                data = dict(zip(cols, row))
                results.append(Organization(
                    org_id=data["org_id"],
                    name=data["name"],
                    description=data.get("description") or "",
                    status=data.get("status") or "active",
                    company_profile=data.get("company_profile") or "corporate",
                    budget_monthly_cents=int(data.get("budget_monthly_cents") or 0),
                    spent_monthly_cents=int(data.get("spent_monthly_cents") or 0),
                    metadata=_json_loads(data.get("metadata"), {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                    updated_at=datetime.fromisoformat(data["updated_at"]),
                ))
            return results

    async def update_organization(self, org_id: str, **kwargs: Any) -> None:
        assert self._db
        allowed = {"name", "description", "status", "company_profile",
                    "budget_monthly_cents", "spent_monthly_cents", "metadata"}
        sets: list[str] = []
        params: list[Any] = []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key == "metadata":
                value = _json_dumps(value)
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(org_id)
        await self._db.execute(
            f"UPDATE organizations SET {', '.join(sets)} WHERE org_id = ?", params
        )
        await self._db.commit()

    async def save_goal(self, goal: Goal) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO goals
            (goal_id, org_id, parent_id, owner_agent_id, level, title, description,
             status, priority, deadline, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                goal.goal_id,
                goal.org_id,
                goal.parent_id,
                goal.owner_agent_id,
                goal.level.value if isinstance(goal.level, GoalLevel) else goal.level,
                goal.title,
                goal.description,
                goal.status.value if isinstance(goal.status, GoalStatus) else goal.status,
                goal.priority,
                goal.deadline.isoformat() if goal.deadline else None,
                _json_dumps(goal.metadata),
                goal.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_goal(self, goal_id: str) -> Goal | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM goals WHERE goal_id = ?", (goal_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_goal(row, cursor.description)

    async def list_goals(
        self,
        org_id: str,
        status: str | None = None,
        parent_id: str | None = "__unset__",
    ) -> list[Goal]:
        assert self._db
        query = "SELECT * FROM goals WHERE org_id = ?"
        params: list[Any] = [org_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        if parent_id != "__unset__":
            if parent_id is None:
                query += " AND parent_id IS NULL"
            else:
                query += " AND parent_id = ?"
                params.append(parent_id)
        query += " ORDER BY priority ASC, created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_goal(row, cursor.description) for row in rows]

    async def get_goal_tree(self, org_id: str) -> list[Goal]:
        return await self.list_goals(org_id, parent_id="__unset__")

    def _row_to_goal(self, row: Any, description: Any) -> Goal:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return Goal(
            goal_id=data["goal_id"],
            org_id=data["org_id"],
            parent_id=data.get("parent_id"),
            owner_agent_id=data.get("owner_agent_id"),
            level=GoalLevel(data.get("level") or "task"),
            title=data["title"],
            description=data.get("description") or "",
            status=GoalStatus(data.get("status") or "active"),
            priority=int(data.get("priority") or 5),
            deadline=datetime.fromisoformat(data["deadline"]) if data.get("deadline") else None,
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def save_org_agent(self, agent: OrgAgent) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO org_agents
            (agent_id, org_id, role_id, name, reports_to,
             budget_monthly_cents, spent_monthly_cents,
             heartbeat_enabled, heartbeat_interval_sec, last_heartbeat_at,
             status, capabilities, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent.agent_id,
                agent.org_id,
                agent.role_id,
                agent.name,
                agent.reports_to,
                agent.budget_monthly_cents,
                agent.spent_monthly_cents,
                int(agent.heartbeat_enabled),
                agent.heartbeat_interval_sec,
                agent.last_heartbeat_at.isoformat() if agent.last_heartbeat_at else None,
                agent.status,
                agent.capabilities,
                _json_dumps(agent.metadata),
                agent.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_org_agent(self, agent_id: str) -> OrgAgent | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM org_agents WHERE agent_id = ?", (agent_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_org_agent(row, cursor.description)

    async def list_org_agents(
        self,
        org_id: str,
        status: str | None = None,
    ) -> list[OrgAgent]:
        assert self._db
        query = "SELECT * FROM org_agents WHERE org_id = ?"
        params: list[Any] = [org_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_org_agent(row, cursor.description) for row in rows]

    def _row_to_org_agent(self, row: Any, description: Any) -> OrgAgent:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return OrgAgent(
            agent_id=data["agent_id"],
            org_id=data["org_id"],
            role_id=data["role_id"],
            name=data.get("name") or "",
            reports_to=data.get("reports_to"),
            budget_monthly_cents=int(data.get("budget_monthly_cents") or 0),
            spent_monthly_cents=int(data.get("spent_monthly_cents") or 0),
            heartbeat_enabled=bool(data.get("heartbeat_enabled", 0)),
            heartbeat_interval_sec=int(data.get("heartbeat_interval_sec") or 300),
            last_heartbeat_at=(
                datetime.fromisoformat(data["last_heartbeat_at"])
                if data.get("last_heartbeat_at")
                else None
            ),
            status=data.get("status") or "idle",
            capabilities=data.get("capabilities") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def record_cost_event(self, event: CostEvent) -> None:
        assert self._db
        await self._db.execute(
            """INSERT INTO cost_events
            (event_id, org_id, agent_id, task_id, model, tokens_in, tokens_out, cost_usd, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.org_id,
                event.agent_id,
                event.task_id,
                event.model,
                event.tokens_in,
                event.tokens_out,
                event.cost_usd,
                event.timestamp.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_agent_spend(self, agent_id: str) -> dict[str, Any]:
        assert self._db
        async with self._db.execute(
            "SELECT SUM(tokens_in), SUM(tokens_out), SUM(cost_usd), COUNT(*) FROM cost_events WHERE agent_id = ?",
            (agent_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return {
                "tokens_in": row[0] or 0,
                "tokens_out": row[1] or 0,
                "cost_usd": row[2] or 0.0,
                "calls": row[3] or 0,
            }

    async def get_org_spend(self, org_id: str) -> dict[str, Any]:
        assert self._db
        async with self._db.execute(
            "SELECT SUM(tokens_in), SUM(tokens_out), SUM(cost_usd), COUNT(*) FROM cost_events WHERE org_id = ?",
            (org_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return {
                "tokens_in": row[0] or 0,
                "tokens_out": row[1] or 0,
                "cost_usd": row[2] or 0.0,
                "calls": row[3] or 0,
            }
