"""DelegationStoreMixin — 委派執行/重組/快照相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opc.database._utils import _json_dumps, _json_loads

if TYPE_CHECKING:
    from opc.database.store import OPCStore


class DelegationStoreMixin:
    """Mixin providing 委派執行/重組/快照相關方法 for OPCStore."""

    @staticmethod
    def _status_priority(status: str) -> int:
        """Higher priority wins when merging scalar state."""
        return {
            "running": 3,
            "reserved": 2,
            "blocked": 2,
            "idle": 1,
            "cold": 0,
        }.get((status or "").strip().lower(), 1)

    @classmethod
    def _merge_role_session_rows(
        cls, *, rows: list[dict[str, Any]], canonical_id: str
    ) -> dict[str, Any]:
        """Field-level merge of N role_runtime_sessions rows → single canonical
        row. See ``_migrate_role_sessions_merge_by_role`` for the policy.
        Rows must all belong to the same (run_id, role_id)."""
        # Pick the "active" row for scalar fields — the one most likely to
        # reflect the live state of the role. Ordering: has_focus desc,
        # status_priority desc, updated_at desc.
        active = max(
            rows,
            key=lambda r: (
                1 if (r.get("focused_work_item_id") or "").strip() else 0,
                cls._status_priority(r.get("status") or ""),
                str(r.get("updated_at") or ""),
            ),
        )

        # Team instance: prefer any non-empty value across the group. Old
        # short-form rows had it blank; the long-form row carries the truth.
        team_instance_id = str(active.get("team_instance_id") or "").strip()
        if not team_instance_id:
            for r in rows:
                candidate = str(r.get("team_instance_id") or "").strip()
                if candidate:
                    team_instance_id = candidate
                    break

        # Inbox: union + de-dup by message id, then sort by timestamp.
        inbox_messages: list[dict[str, Any]] = []
        seen_msg_ids: set[str] = set()
        for r in rows:
            state = _json_loads(r.get("inbox_state") or "{}", {})
            for msg in list(state.get("messages", []) or []):
                if not isinstance(msg, dict):
                    continue
                mid = str(msg.get("message_id") or msg.get("id") or "").strip()
                # Preserve order for messages that have no ID (rare) — use
                # a synthetic marker so they don't all collide on "".
                key = mid or f"__noid__::{len(inbox_messages)}"
                if key in seen_msg_ids:
                    continue
                seen_msg_ids.add(key)
                inbox_messages.append(dict(msg))
        inbox_messages.sort(key=lambda m: str(m.get("timestamp") or m.get("created_at") or ""))
        # Preserve any non-messages keys from the active row's inbox_state.
        active_inbox = _json_loads(active.get("inbox_state") or "{}", {})
        active_inbox["messages"] = inbox_messages
        merged_inbox_state = active_inbox

        # Memory slices: union dict[work_item_id → list], merging lists per key.
        # Iterate oldest-first so older notes appear before newer ones in the
        # merged list (natural reading order; rows come in DESC so reverse).
        merged_memory: dict[str, list[Any]] = {}
        for r in reversed(rows):
            slices = _json_loads(r.get("memory_slices_by_work_item") or "{}", {})
            if not isinstance(slices, dict):
                continue
            for wid, items in slices.items():
                if not isinstance(items, list):
                    continue
                merged_memory.setdefault(str(wid), []).extend(items)
        # De-dup identical entries per work item (keep order).
        for wid, items in list(merged_memory.items()):
            seen: set[str] = set()
            deduped: list[Any] = []
            for entry in items:
                sig = _json_dumps(entry) if not isinstance(entry, str) else entry
                if sig in seen:
                    continue
                seen.add(sig)
                deduped.append(entry)
            merged_memory[wid] = deduped

        # List unions (order: active row's entries first, others appended).
        def _union_list(field: str) -> list[str]:
            merged: list[str] = []
            seen: set[str] = set()
            for r in [active] + [r for r in rows if r is not active]:
                raw = _json_loads(r.get(field) or "[]", [])
                for item in raw if isinstance(raw, list) else []:
                    normalized = str(item)
                    if normalized in seen:
                        continue
                    seen.add(normalized)
                    merged.append(normalized)
            return merged

        background_ids = _union_list("background_work_item_ids")
        manager_role_ids = _union_list("manager_role_ids")
        manager_seat_ids = _union_list("manager_seat_ids")
        seat_ids = _union_list("seat_ids")
        # Pending queue: preserve FIFO — active row's queue first (these
        # are the ones already committed to this role's runtime), then any
        # extras from siblings. De-dup but keep earliest occurrence.
        pending_ids = _union_list("pending_work_item_ids")

        # Adapter session state (codex / LLM resume token): active row
        # wins; every other row's state is retained as audit breadcrumbs.
        active_adapter = _json_loads(active.get("adapter_session_state") or "{}", {})
        adapter_audit: list[dict[str, Any]] = []
        for r in rows:
            if r is active:
                continue
            raw_state = r.get("adapter_session_state") or ""
            if not raw_state or raw_state in ("{}", "null"):
                continue
            adapter_audit.append(
                {
                    "source_role_session_id": str(r.get("role_session_id") or ""),
                    "updated_at": str(r.get("updated_at") or ""),
                    "adapter_session_state": _json_loads(raw_state, {}),
                }
            )

        # Base metadata: start from active row's metadata, then append the
        # adapter audit list (append, don't overwrite — a role that has
        # been merged multiple times retains its full trail).
        merged_metadata = _json_loads(active.get("metadata") or "{}", {})
        if not isinstance(merged_metadata, dict):
            merged_metadata = {}
        if adapter_audit:
            existing_audit = list(merged_metadata.get("adapter_session_state_audit", []) or [])
            existing_audit.extend(adapter_audit)
            merged_metadata["adapter_session_state_audit"] = existing_audit

        # team_instance_id history — diagnostic trail of which team contexts
        # this role has been seen in. Useful when debugging cross-team flows.
        prior_team_instances = [
            str(r.get("team_instance_id") or "").strip()
            for r in rows
            if str(r.get("team_instance_id") or "").strip()
        ]
        if prior_team_instances:
            existing_history = list(merged_metadata.get("team_instance_history", []) or [])
            existing_history.extend(prior_team_instances)
            # De-dup preserving order.
            dedup_history: list[str] = []
            seen_tid: set[str] = set()
            for tid in existing_history:
                if tid in seen_tid:
                    continue
                seen_tid.add(tid)
                dedup_history.append(tid)
            merged_metadata["team_instance_history"] = dedup_history

        # Preserve the oldest created_at across the group (role has been
        # around since the earliest row was written), use the most recent
        # updated_at (merged row reflects the latest activity).
        created_at = min(str(r.get("created_at") or "") for r in rows if r.get("created_at"))
        updated_at = max(str(r.get("updated_at") or "") for r in rows if r.get("updated_at"))
        if not updated_at:
            updated_at = datetime.now().isoformat()

        return {
            "role_session_id": canonical_id,
            "run_id": str(active.get("run_id") or ""),
            "project_id": str(active.get("project_id") or "default"),
            "team_instance_id": team_instance_id,
            "team_id": str(active.get("team_id") or ""),
            "role_id": str(active.get("role_id") or ""),
            "seat_id": str(active.get("seat_id") or ""),
            "seat_state_id": str(active.get("seat_state_id") or ""),
            "employee_id": str(active.get("employee_id") or ""),
            "focused_work_item_id": str(active.get("focused_work_item_id") or ""),
            "background_work_item_ids": _json_dumps(background_ids),
            "manager_role_ids": _json_dumps(manager_role_ids),
            "manager_seat_ids": _json_dumps(manager_seat_ids),
            "seat_ids": _json_dumps(seat_ids),
            "adapter_session_state": _json_dumps(active_adapter),
            "inbox_state": _json_dumps(merged_inbox_state),
            "memory_slices_by_work_item": _json_dumps(merged_memory),
            "resume_state": active.get("resume_state") or "{}",
            "current_work_item": active.get("current_work_item") or "{}",
            "latest_notification": active.get("latest_notification") or "{}",
            "manager_digest": active.get("manager_digest") or "{}",
            "status": normalize_role_runtime_status(
                active.get("status"),
                active.get("focused_work_item_id"),
            ),
            "pending_work_item_ids": _json_dumps(pending_ids),
            "metadata": _json_dumps(merged_metadata),
            "created_at": created_at or updated_at,
            "updated_at": updated_at,
        }

    async def _upsert_role_session_row(self, *, table: str, row: dict[str, Any]) -> None:
        """Write the merged row under its canonical PK. ``INSERT OR REPLACE``
        so an earlier migration pass (or a pre-existing canonical row) is
        overwritten with the merged state."""
        assert self._db is not None
        columns = [
            "role_session_id", "run_id", "project_id", "team_instance_id",
            "team_id", "role_id", "seat_id", "seat_state_id", "employee_id",
            "focused_work_item_id", "background_work_item_ids",
            "manager_role_ids", "manager_seat_ids", "seat_ids",
            "adapter_session_state", "inbox_state", "memory_slices_by_work_item",
            "resume_state", "current_work_item", "latest_notification",
            "manager_digest", "status", "pending_work_item_ids",
            "metadata", "created_at", "updated_at",
        ]
        placeholders = ", ".join("?" for _ in columns)
        await self._db.execute(
            f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            tuple(row.get(c) for c in columns),
        )

    async def _redirect_role_session_references(
        self,
        *,
        source_id: str,
        target_id: str,
    ) -> int:
        """Rewrite every table/column that references ``source_id`` to
        ``target_id``. Returns the number of rows modified (for observability).

        Scope is intentionally explicit: we know which columns hold
        role_session_id references and update exactly those. JSON metadata
        columns (tasks.metadata, delegation_work_items.metadata) are
        rewritten with a targeted JSON-level replace that only touches the
        specific keys we control — we never blindly string-replace the
        JSON blob.
        """
        assert self._db is not None
        total = 0

        # delegation_work_items: two direct columns.
        cursor = await self._db.execute(
            """UPDATE delegation_work_items
               SET role_runtime_session_id=?,
                   updated_at=?
               WHERE role_runtime_session_id=?""",
            (target_id, datetime.now().isoformat(), source_id),
        )
        total += getattr(cursor, "rowcount", 0) or 0
        cursor = await self._db.execute(
            """UPDATE delegation_work_items
               SET claimed_by_role_runtime_session_id=?,
                   updated_at=?
               WHERE claimed_by_role_runtime_session_id=?""",
            (target_id, datetime.now().isoformat(), source_id),
        )
        total += getattr(cursor, "rowcount", 0) or 0

        # seat_states: single column.
        cursor = await self._db.execute(
            """UPDATE seat_states
               SET role_runtime_session_id=?,
                   updated_at=?
               WHERE role_runtime_session_id=?""",
            (target_id, datetime.now().isoformat(), source_id),
        )
        total += getattr(cursor, "rowcount", 0) or 0

        # JSON metadata references: load, rewrite keys, store.
        # delegation_work_items.metadata.assigned_role_runtime_id
        async with self._db.execute(
            """SELECT work_item_id, metadata FROM delegation_work_items
               WHERE metadata LIKE ?""",
            (f'%"{source_id}"%',),
        ) as cursor:
            wi_rows = await cursor.fetchall()
        for work_item_id, metadata_json in wi_rows:
            meta = _json_loads(metadata_json, {})
            mutated = False
            if str(meta.get("assigned_role_runtime_id", "")) == source_id:
                meta["assigned_role_runtime_id"] = target_id
                mutated = True
            if mutated:
                await self._db.execute(
                    """UPDATE delegation_work_items
                       SET metadata=?, updated_at=?
                       WHERE work_item_id=?""",
                    (_json_dumps(meta), datetime.now().isoformat(), work_item_id),
                )
                total += 1

        # tasks.metadata.delegation_role_session_id
        async with self._db.execute(
            """SELECT id, metadata FROM tasks
               WHERE metadata LIKE ?""",
            (f'%"{source_id}"%',),
        ) as cursor:
            task_rows = await cursor.fetchall()
        for task_id, metadata_json in task_rows:
            meta = _json_loads(metadata_json, {})
            mutated = False
            if str(meta.get("delegation_role_session_id", "")) == source_id:
                meta["delegation_role_session_id"] = target_id
                mutated = True
            if mutated:
                await self._db.execute(
                    """UPDATE tasks SET metadata=? WHERE id=?""",
                    (_json_dumps(meta), task_id),
                )
                total += 1

        return total

    async def save_delegation_event(self, event: DelegationEvent) -> None:
        db = self._require_db()
        await db.execute(
            """INSERT OR REPLACE INTO delegation_events
            (event_id, run_id, work_item_id, cell_id, role_id, event_type, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.run_id,
                event.work_item_id,
                event.cell_id,
                event.role_id,
                event.event_type,
                _json_dumps(event.payload),
                event.created_at.isoformat(),
            ),
        )
        await db.commit()

    async def list_delegation_events(self, run_id: str) -> list[DelegationEvent]:
        db = self._require_db()
        async with db.execute(
            "SELECT * FROM delegation_events WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        return [
            DelegationEvent(
                event_id=data["event_id"],
                run_id=data["run_id"],
                work_item_id=data.get("work_item_id"),
                cell_id=data.get("cell_id"),
                role_id=data.get("role_id"),
                event_type=data["event_type"],
                payload=_json_loads(data.get("payload"), {}),
                created_at=datetime.fromisoformat(data["created_at"]),
            )
            for data in (dict(zip(cols, row)) for row in rows)
        ]

    async def save_reorg_proposal(self, proposal: ReorgProposal) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO reorg_proposals
            (proposal_id, project_id, session_id, task_id, initiated_by, source_role_id, scope, risk_level,
             status, title, summary, rationale, user_confirmation_required, old_org_version, new_org_version,
             changeset, migration_plan, impact_summary,
             approval_notes, metadata, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal.proposal_id,
                proposal.project_id,
                proposal.session_id,
                proposal.task_id,
                proposal.initiated_by,
                proposal.source_role_id,
                proposal.scope.value,
                proposal.risk_level.value,
                proposal.status.value,
                proposal.title,
                proposal.summary,
                proposal.rationale,
                int(proposal.user_confirmation_required),
                proposal.old_org_version,
                proposal.new_org_version,
                _json_dumps(proposal.changeset.__dict__),
                _json_dumps(proposal.migration_plan.__dict__),
                _json_dumps(proposal.impact_summary),
                proposal.approval_notes,
                _json_dumps(proposal.metadata),
                proposal.created_at.isoformat(),
                proposal.updated_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_reorg_proposal(self, proposal_id: str) -> ReorgProposal | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM reorg_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_reorg_proposal(row, cursor.description)

    async def list_reorg_proposals(
        self,
        project_id: str,
        status: ReorgProposalStatus | None = None,
        limit: int = 20,
    ) -> list[ReorgProposal]:
        assert self._db
        query = "SELECT * FROM reorg_proposals WHERE project_id = ?"
        params: list[Any] = [project_id]
        if status:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_reorg_proposal(row, cursor.description) for row in rows]

    def _row_to_reorg_proposal(self, row: Any, description: Any) -> ReorgProposal:
        from dataclasses import fields as _dc_fields

        from opc.core.models import (
            ReorgChangeSet,
            ReorgMigrationPlan,
            ReorgRoleChange,
            ReorgTaskAdjustment,
        )

        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        changeset_data = _json_loads(data.get("changeset"), {})
        migration_plan_data = _json_loads(data.get("migration_plan"), {})
        migration_plan_data = {
            k: v
            for k, v in migration_plan_data.items()
            if k in {f.name for f in _dc_fields(ReorgMigrationPlan)}
        }
        return ReorgProposal(
            proposal_id=data["proposal_id"],
            project_id=data["project_id"],
            session_id=data.get("session_id"),
            task_id=data.get("task_id"),
            initiated_by=data.get("initiated_by") or "owner",
            source_role_id=data.get("source_role_id") or "",
            scope=ReorgScope(data["scope"]),
            risk_level=ReorgRiskLevel(data["risk_level"]),
            status=ReorgProposalStatus(data["status"]),
            title=data.get("title") or "",
            summary=data.get("summary") or "",
            rationale=data.get("rationale") or "",
            user_confirmation_required=bool(data.get("user_confirmation_required", 1)),
            old_org_version=int(data.get("old_org_version") or 1),
            new_org_version=int(data.get("new_org_version") or 1),
            old_runtime_topology_version=int(data.get("old_runtime_topology_version") or 1),
            new_runtime_topology_version=int(data.get("new_runtime_topology_version") or 1),
            changeset=ReorgChangeSet(
                role_changes=[ReorgRoleChange(**item) for item in changeset_data.get("role_changes", [])],
                task_adjustments=[ReorgTaskAdjustment(**item) for item in changeset_data.get("task_adjustments", [])],
                metadata=dict(changeset_data.get("metadata", {})),
            ),
            migration_plan=ReorgMigrationPlan(**migration_plan_data),
            impact_summary=_json_loads(data.get("impact_summary"), {}),
            approval_notes=data.get("approval_notes") or "",
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )

    async def save_org_snapshot(self, snapshot: OrgSnapshot) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO org_snapshots
            (snapshot_id, project_id, org_version, company_name, topology, roles,
             company_profile, active_tasks, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.snapshot_id,
                snapshot.project_id,
                snapshot.org_version,
                snapshot.company_name,
                snapshot.topology,
                _json_dumps(snapshot.roles),
                snapshot.company_profile,
                _json_dumps(snapshot.active_tasks),
                _json_dumps(snapshot.metadata),
                snapshot.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def get_org_snapshot(self, snapshot_id: str) -> OrgSnapshot | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM org_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_org_snapshot(row, cursor.description)

    async def get_latest_org_snapshot(self, project_id: str) -> OrgSnapshot | None:
        assert self._db
        async with self._db.execute(
            "SELECT * FROM org_snapshots WHERE project_id = ? ORDER BY org_version DESC, created_at DESC LIMIT 1",
            (project_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_org_snapshot(row, cursor.description)

    def _row_to_org_snapshot(self, row: Any, description: Any) -> OrgSnapshot:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return OrgSnapshot(
            snapshot_id=data["snapshot_id"],
            project_id=data["project_id"],
            org_version=int(data.get("org_version") or 1),
            runtime_topology_version=int(data.get("runtime_topology_version") or 1),
            company_name=data.get("company_name") or "",
            topology=data.get("topology") or "",
            roles=_json_loads(data.get("roles"), []),
            company_profile=data.get("company_profile") or "corporate",
            active_tasks=_json_loads(data.get("active_tasks"), []),
            metadata=_json_loads(data.get("metadata"), {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def record_reorg_event(self, event: ReorgEventRecord) -> None:
        assert self._db
        await self._db.execute(
            """INSERT OR REPLACE INTO reorg_events
            (event_id, proposal_id, project_id, event_kind, summary, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.proposal_id,
                event.project_id,
                event.event_kind.value,
                event.summary,
                _json_dumps(event.details),
                event.created_at.isoformat(),
            ),
        )
        await self._db.commit()

    async def list_reorg_events(
        self,
        project_id: str,
        proposal_id: str | None = None,
        limit: int = 50,
    ) -> list[ReorgEventRecord]:
        assert self._db
        query = "SELECT * FROM reorg_events WHERE project_id = ?"
        params: list[Any] = [project_id]
        if proposal_id:
            query += " AND proposal_id = ?"
            params.append(proposal_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [
                ReorgEventRecord(
                    event_id=data["event_id"],
                    proposal_id=data.get("proposal_id") or "",
                    project_id=data["project_id"],
                    event_kind=ReorgEventKind(data["event_kind"]),
                    summary=data.get("summary") or "",
                    details=_json_loads(data.get("details"), {}),
                    created_at=datetime.fromisoformat(data["created_at"]),
                )
                for data in (dict(zip(cols, row)) for row in rows)
            ]
