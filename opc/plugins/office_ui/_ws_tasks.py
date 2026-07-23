"""WsTaskMixin — 任務操作/執行/狀態相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from opc.core.models import normalize_role_runtime_status

from opc.plugins.office_ui.snapshot_builder import (
    _build_company_runtime_control_by_task,
    _build_session_context_preview,
    _extract_markdown_text,
    _normalize_session_company_profile,
    _normalize_session_exec_mode,
    _normalize_transcript_detail_level,
    _resolve_task_preferred_agent,
    _resolve_task_session_config,
    _sanitize_ui_message_dict,
    _task_parent_session_link,
)
from opc.plugins.office_ui._ws_utils import (
    _TASK_MODE_PREFERRED_AGENTS,
    _compact_session_title,
    _looks_like_escalation_reply,
    _normalize_escalation_reply,
    _ui_conversation_turn_id,
    _ui_message_identity_metadata,
)
from opc.presentation.kanban import STATUS_TO_COLUMN

if TYPE_CHECKING:
    from opc.plugins.office_ui.ws_handler import WSHandler


class WsTaskMixin:
    """Mixin providing 任務操作/執行/狀態相關方法 for WSHandler."""

    async def _handle_run_task(self, ws: Any, data: dict) -> None:
        title = data.get("title", "")
        description = data.get("description", "")
        mode = data.get("mode", self._exec_mode)
        profile = data.get("profile", self._company_profile)
        org_id = self._normalize_session_org_id(data.get("org_id") or data.get("organization_id"))
        task_id = data.get("task_id")
        run_engine, run_project_id = await self._engine_for_request(data)
        self._track(self._run_task(
            title,
            description,
            mode,
            profile,
            task_id=task_id,
            run_engine=run_engine,
            run_project_id=run_project_id,
            org_id=org_id,
        ))
        await self._send_ack(ws, ok=True)

    async def _handle_cross_office(self, ws: Any, data: dict) -> None:
        """Visual-only: broadcasts collab event for frontend animation. No engine dispatch."""
        await self.broadcast({"type": "cross_office_collab", "payload": {
            "agent_ids": data.get("agent_ids", []),
            "task_id": data.get("task_id", ""),
            "action": data.get("action", ""),
        }})

    async def _handle_agent_workload(self, ws: Any, data: dict) -> None:
        """Return per-agent task counts (active, pending, done, failed)."""
        if not self.engine.store:
            await self._send_ack(ws, ok=True, workload={})
            return
        agents = await self.agent_store.get_all()
        # Fetch all tasks once, then group by assigned_to (opc_role_id)
        all_tasks = await self.engine.store.get_tasks(
            project_id=self.engine.project_id or "default",
        )
        # Build role_id → task list mapping
        role_tasks: dict[str, list] = {}
        for t in all_tasks:
            role_tasks.setdefault(t.assigned_to, []).append(t)

        workload: dict[str, dict[str, int]] = {}
        for agent in agents:
            agent_id = agent.get("agent_id", "")
            if not agent_id:
                continue
            # Tasks are assigned by opc_role_id, not agent_id
            role_id = agent.get("opc_role_id", agent_id)
            counts = {"active": 0, "pending": 0, "done": 0, "failed": 0}
            for t in role_tasks.get(role_id, []):
                sv = t.status.value if hasattr(t.status, "value") else str(t.status)
                if sv == "running":
                    counts["active"] += 1
                elif sv == "pending":
                    counts["pending"] += 1
                elif sv == "done":
                    counts["done"] += 1
                elif sv == "failed":
                    counts["failed"] += 1
            workload[agent_id] = counts
        await self._send_ack(ws, ok=True, workload=workload)

    async def _run_task(
        self,
        title: str,
        description: str,
        mode: str,
        profile: str,
        task_id: str | None = None,
        *,
        run_engine: Any | None = None,
        run_project_id: str | None = None,
        org_id: str | None = None,
    ) -> None:
        """Execute a task with the selected mode."""
        engine = run_engine or self.engine
        pid = self._normalize_project_id(run_project_id or getattr(engine, "project_id", None))
        error_channel = f"session:{task_id}" if task_id else f"activity:{pid}"

        # Look up session_id from task
        session_id: str | None = None
        task = None
        preferred_agent = self._task_preferred_agent
        session_org_id = self._normalize_session_org_id(org_id)
        if task_id and getattr(engine, "store", None):
            task = await engine.store.get_task(task_id)
            if task:
                session_id = task.session_id
                identity = self._resolve_task_identity(
                    task,
                    default_exec_mode=mode,
                    default_company_profile=profile,
                    default_preferred_agent=preferred_agent,
                    default_org_id=session_org_id,
                )
                mode = identity.exec_mode
                profile = identity.company_profile
                session_org_id = identity.org_id
                preferred_agent = identity.preferred_agent

        company_runtime_target: dict[str, Any] | None = None
        try:
            content = f"{title}\n{description}".strip()
            engine_mode, company_profile = self._resolve_engine_mode(mode, profile)
            engine_preferred_agent = preferred_agent if engine_mode == "project" else None
            response = None

            if task_id:
                # Per-task lock: same session serialized, different sessions concurrent
                async with self._get_task_lock(task_id):
                    if self._is_company_session_exec_mode(mode) and task is not None and self._store_is_ready(engine.store):
                        from opc.core.models import TaskStatus
                        await apply_task_status_transition(
                            engine.store,
                            task,
                            target_status_or_phase=TaskStatus.RUNNING,
                            reason="run_task_started",
                        )
                    await self.broadcast({"type": "board_task_status_changed", "payload": {
                        "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "running",
                    }})
                    if task is not None:
                        await self._persist_session_config(
                            task,
                            exec_mode=mode,
                            company_profile=profile,
                            preferred_agent=preferred_agent,
                            org_id=session_org_id,
                            engine=engine,
                        )
                    if self._is_company_session_exec_mode(mode):
                        try:
                            company_runtime_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                            await self._set_company_runtime_control(company_runtime_target, state="running")
                        except Exception:
                            logger.opt(exception=True).debug("failed to mark run_task company runtime running")
                    response = await engine.process_message(
                        content,
                        project_id=pid,
                        session_id=session_id,
                        mode=engine_mode,
                        org_id=session_org_id or None,
                        company_profile=company_profile,
                        preferred_agent=engine_preferred_agent,
                        origin_task_id=task_id,
                    )
                await self._sync_task_transcript_messages(task_id, engine=engine)
            else:
                response = await engine.process_message(
                    content,
                    project_id=pid,
                    mode=engine_mode,
                    org_id=session_org_id or None,
                    company_profile=company_profile,
                    preferred_agent=engine_preferred_agent,
                )

            # Broadcast: task idle (agent responded, waiting for user)
            if task_id:
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "idle",
                }})
                if getattr(engine, "store", None):
                    from opc.core.models import TaskStatus as TS
                    t = await engine.store.get_task(task_id)
                    if t:
                        t.status = TS.IDLE
                        await engine.store.save_task(t)
                if self._is_company_session_exec_mode(mode):
                    try:
                        idle_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                        await self._set_company_runtime_control(idle_target or company_runtime_target, state="idle")
                    except Exception:
                        logger.opt(exception=True).debug("failed to mark run_task company runtime idle")
        except Exception as e:
            logger.opt(exception=True).error(f"Task execution error: {e}")
            # Broadcast: task failed (stays in in-progress, user can retry)
            if task_id:
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "failed",
                }})
                if self._is_company_session_exec_mode(mode):
                    try:
                        failed_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                        await self._set_company_runtime_control(failed_target or company_runtime_target, state="idle")
                    except Exception:
                        logger.opt(exception=True).debug("failed to clear run_task company runtime after error")
            err_meta: dict[str, Any] = {}
            if task_id:
                err_meta["task_id"] = task_id
            msg = await self.chat_store.insert_message(
                channel_id=error_channel,
                sender="system",
                sender_name="OPC",
                content=f"Task error: {e}",
                metadata=err_meta or None,
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})
        finally:
            # Always refresh the frontend after a task run so new delegation
            # runs / work items are reflected on the kanban immediately.
            try:
                collab = await build_collab_sync(
                    engine, self.agent_store, self.chat_store,
                    self.event_adapter,
                    exec_mode=mode,
                )
                await self.broadcast({"type": "collab_sync_push", "payload": collab})
            except Exception:
                logger.opt(exception=True).warning("Post-run collab_sync broadcast failed (non-fatal)")

    async def _handle_create_session(self, ws: Any, data: dict) -> None:
        """Create a new Task (PENDING) + engine session. Broadcasts session_created."""
        try:
            run_engine, pid = await self._engine_for_request(data)
            result = await self.services.session.create(
                project_id=pid,
                title=data.get("title", "New Chat"),
                exec_mode=data.get("exec_mode", self._exec_mode),
                company_profile=data.get("company_profile"),
                preferred_agent=data.get("preferred_agent", self._task_preferred_agent),
                org_id=data.get("org_id") or data.get("organization_id"),
                interface="office_ui",
            )
            self._session_to_task.update(self.services_context.session_to_task)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="create_session", **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="create_session")

    async def _handle_session_update_config(self, ws: Any, data: dict) -> None:
        """Update a session's persisted execution configuration."""
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.session.update_config(
                project_id=project_id,
                task_id=str(data.get("task_id", "") or ""),
                exec_mode=data.get("exec_mode"),
                company_profile=data.get("company_profile"),
                preferred_agent=data.get("preferred_agent"),
                org_id=data.get("org_id") or data.get("organization_id"),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="session_update_config")

    async def _handle_session_detail(self, ws: Any, data: dict) -> None:
        """Return a paginated persisted transcript/context for a single session."""
        if self._shutting_down:
            return

        task_id = str(data.get("task_id", "") or "").strip()
        if not task_id:
            await self._send_ack(ws, ok=False, error="task_id required")
            return

        run_engine, request_project_id = await self._engine_for_request(data)
        view_generation = data.get("view_generation")
        store = run_engine.store
        if not self._store_is_ready(store):
            await self._send_ack(ws, ok=False, error="store_not_ready", project_id=request_project_id, view_generation=view_generation)
            return

        task = await store.get_task(task_id)
        if not task:
            await self._send_ack(ws, ok=False, error="task_not_found", project_id=request_project_id, task_id=task_id, view_generation=view_generation, action="session_detail")
            return

        channel_id = f"session:{task_id}"
        session_id = getattr(task, "session_id", None)
        project_id = getattr(task, "project_id", None) or run_engine.project_id or request_project_id
        request_limit = max(1, min(int(data.get("limit", 200) or 200), 500))
        detail_level = _normalize_transcript_detail_level(data.get("detail_level", "summary"))
        raw_include = data.get("include")
        if isinstance(raw_include, list):
            include_set = {str(item or "").strip() for item in raw_include if str(item or "").strip()}
        else:
            include_set = {"messages", "session_state"}
            if detail_level == "full":
                include_set.update({"progress", "work_items", "runtime_context"})
        before_timestamp = self._normalize_session_detail_timestamp(data.get("before_created_at"))
        before_message_id = str(data.get("before_message_id", "") or "").strip() or None

        try:
            await self.chat_store.create_session_channel(
                task_id,
                getattr(task, "title", "") or "Session",
                project_id=project_id,
            )
        except Exception as exc:
            if self._is_expected_shutdown_error(exc):
                logger.debug(f"session_detail: channel bootstrap skipped during shutdown for {task_id}")
                return
            raise
        try:
            transcript_page, transcript_total_count, transcript_has_more = await self._load_session_transcript_page(
                task,
                limit=request_limit,
                detail_level=detail_level,
                before_timestamp=before_timestamp,
                before_message_id=before_message_id,
                engine=run_engine,
            )
            if transcript_page:
                await self.chat_store.backfill_messages(
                    channel_id,
                    transcript_page,
                    project_id=project_id,
                )
        except Exception:
            logger.opt(exception=True).debug(f"session_detail: transcript page load failed for {task_id}")
            transcript_total_count = 0
            transcript_has_more = False

        await self._reconcile_inactive_human_escalation_cards(
            channel_id,
            task_id=task_id,
            project_id=project_id,
        )
        await self._reconcile_execution_checkpoint_cards(
            channel_id,
            project_id=project_id,
            engine=run_engine,
        )

        try:
            cache_page = await self.chat_store.get_channel_messages_page_info(
                channel_id,
                limit=request_limit,
                before_timestamp=before_timestamp,
                before_message_id=before_message_id,
                detail_level=detail_level,
                project_id=project_id,
            )
            messages = list(cache_page.get("messages", []) or [])
            visible_cache_count = int(cache_page.get("total_count", len(messages)) or 0)
            cache_has_more = bool(cache_page.get("has_more", False))
            messages = self._filter_ui_messages_for_detail_level(messages, detail_level)
            messages = [_sanitize_ui_message_dict(message) for message in messages]
        except Exception as exc:
            if self._is_expected_shutdown_error(exc):
                logger.debug(f"session_detail: cache page skipped during shutdown for {task_id}")
                return
            messages = []
            visible_cache_count = len(messages)
            cache_has_more = False
        total_message_count = max(transcript_total_count, visible_cache_count, len(messages))
        has_more = transcript_has_more or cache_has_more

        task_meta = task.metadata if isinstance(getattr(task, "metadata", None), dict) else {}
        handoff_context = _extract_markdown_text(task_meta.get("handoff_context"), max_chars=None)
        task_description_context = _extract_markdown_text(getattr(task, "description", ""), max_chars=None)
        parent_session_link = _task_parent_session_link(task, task_meta)
        if not handoff_context and parent_session_link:
            handoff_context = task_description_context or await _build_session_context_preview(
                run_engine,
                session_id,
                max_chars=None,
            )
        handoff_to = _extract_markdown_text(task_meta.get("handoff_to"), max_chars=None)
        status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
        created_at = getattr(task, "created_at", None)
        created_ts = created_at.timestamp() if hasattr(created_at, "timestamp") else time.time()
        updated_at = getattr(task, "updated_at", None)
        updated_ts = updated_at.timestamp() if hasattr(updated_at, "timestamp") else created_ts
        assigned_to = str(getattr(task, "assigned_to", "") or "").strip()
        assignee_ids: list[str] = []
        if assigned_to:
            try:
                assignee_ids = [self.event_adapter._resolve_role_to_agent(assigned_to)] if self.event_adapter else [assigned_to]
            except Exception:
                assignee_ids = [assigned_to]
        identity = self._resolve_task_identity(task)
        exec_mode_val = identity.exec_mode
        company_profile_val = identity.company_profile
        org_id_val = identity.org_id
        project_tasks = [task]
        if self._is_company_session_exec_mode(exec_mode_val):
            try:
                project_tasks = await store.get_tasks(project_id=project_id)
            except Exception:
                logger.opt(exception=True).debug("session_detail: failed to load project tasks for runtime control")
                project_tasks = [task]
        try:
            runtime_control_meta = (
                await _build_company_runtime_control_by_task(run_engine, project_tasks, project_id)
            ).get(task_id, {})
        except Exception:
            logger.opt(exception=True).debug("session_detail: failed to build runtime control payload")
            runtime_control_meta = {}
        runtime_meta = dict(task_meta.get("runtime_v2", {}) or {})
        member_session_meta = dict(task_meta.get("member_session_state", {}) or {})
        session_state: dict[str, Any] = {
            "project_id": project_id,
            "task_id": task_id,
            "runtime_task_id": task_id,
            "execution_turn_id": task_id,
            "session_id": session_id,
            "parent_session_id": parent_session_link or None,
            "mode": "child" if parent_session_link else "primary",
            "exec_mode": exec_mode_val,
            "company_profile": company_profile_val,
            "org_id": org_id_val,
            "channel_id": channel_id,
            "title": getattr(task, "title", "") or "Session",
            "status": status_val,
            "column_id": STATUS_TO_COLUMN.get(status_val, "todo"),
            "assignee_ids": assignee_ids,
            "priority": None,
            "tags": list(getattr(task, "tags", []) or []),
            "created_at": created_ts,
            "updated_at": updated_ts,
            "message_count": total_message_count,
            "handoff_context": handoff_context,
            "handoff_to": handoff_to,
            "origin_task_id": task_meta.get("origin_task_id") or task_id,
            "artifacts": task_meta.get("artifacts"),
            "runtime_session_id": runtime_meta.get("runtime_session_id"),
            "resume_cursor": runtime_meta.get("resume_cursor"),
            "active_subagents": list(runtime_meta.get("active_subagents", []) or []),
            "permission_requests": list(runtime_meta.get("permission_requests", []) or []),
            "worktree_path": runtime_meta.get("worktree_path"),
            "context_tokens": runtime_meta.get("context_tokens"),
            "context_window": runtime_meta.get("context_window"),
            "context_remaining_pct": runtime_meta.get("context_remaining_pct"),
            "input_tokens": runtime_meta.get("input_tokens"),
            "output_tokens": runtime_meta.get("output_tokens"),
            "total_tokens": runtime_meta.get("total_tokens"),
            "turn_cost_usd": runtime_meta.get("turn_cost_usd"),
            "session_cost_usd": runtime_meta.get("session_cost_usd"),
            "pending_permission_count": runtime_meta.get("pending_permission_count"),
            "drain_mode": runtime_meta.get("drain_mode"),
            "resident_status": normalize_role_runtime_status(
                member_session_meta.get("status") or member_session_meta.get("resident_status"),
                member_session_meta.get("focused_work_item_id"),
            ),
            "actionable_inbox_count": member_session_meta.get("actionable_inbox_count"),
            "protocol_backlog_count": member_session_meta.get("protocol_backlog_count"),
            "notification_backlog_count": member_session_meta.get("notification_backlog_count"),
            "latest_notification": member_session_meta.get("latest_notification"),
            "detail_loaded": True,
            "full_loaded": detail_level == "full" and not has_more,
            "has_more": has_more,
            "detail_loading": False,
            "view_generation": view_generation,
            **runtime_control_meta,
        }
        if "progress" in include_set or "work_items" in include_set or detail_level == "full":
            try:
                # Flush the in-memory progress buffer first: entries are
                # broadcast to clients before they reach the DB, so a snapshot
                # built from the DB alone would erase freshly streamed entries
                # from the client's live log (visible as flickering rows).
                await self._flush_progress(task_id, project_id=project_id)
                progress_log = await self.chat_store.get_progress(task_id, project_id=project_id)
            except Exception:
                progress_log = []
            session_state["progress_log"] = progress_log
        if "work_items" in include_set or detail_level == "full":
            session_state["work_item_log"] = list(task_meta.get("work_item_log", []) or [])
            if task_meta.get("role_work_items"):
                session_state["role_work_items"] = task_meta.get("role_work_items")
            if task_meta.get("executor_role_work_items"):
                session_state["executor_role_work_items"] = task_meta.get("executor_role_work_items")

        await self._send_ack(
            ws,
            ok=True,
            action="session_detail",
            project_id=project_id,
            view_generation=view_generation,
            task_id=task_id,
            channel_id=channel_id,
            session_id=session_id,
            detail_level=detail_level,
            include=sorted(include_set),
            message_count=total_message_count,
            loaded_count=len(messages),
            has_more=has_more,
            messages=messages,
            session_state=session_state,
            handoff_context=handoff_context,
            handoff_to=handoff_to,
        )

    async def _handle_session_send(self, ws: Any, data: dict) -> None:
        """Handle user message in a session. Auto-titles from first message."""
        task_id = data.get("task_id", "")
        content = data.get("content", "")
        if not content or not task_id:
            return
        run_engine, run_project_id = await self._engine_for_request(data)

        # Process file attachments via AttachmentStore (disk storage, lightweight refs)
        raw_attachments = data.get("attachments", [])
        attachment_refs: list[dict] = []
        attachment_errors: list[str] = []
        if raw_attachments:
            ensure_attachment_store = getattr(run_engine, "_ensure_attachment_store", None)
            if callable(ensure_attachment_store):
                try:
                    ensure_attachment_store()
                except Exception as exc:
                    logger.warning(f"Failed to refresh attachment store for project {run_project_id!r}: {exc}")
        att_store = getattr(run_engine, "attachment_store", None)
        if raw_attachments and att_store:
            for att in raw_attachments:
                filename = str(att.get("filename", "upload") or "upload").strip() or "upload"
                mime_type = str(att.get("mime_type", "") or "").strip() or None
                try:
                    ref = await att_store.save_from_base64(
                        filename,
                        att.get("data", ""),
                        mime_type=mime_type,
                    )
                    attachment_refs.append(ref.to_dict())
                except Exception as exc:
                    logger.warning(f"Attachment save failed: {exc}")
                    attachment_errors.append(f"{filename}: {exc}")
        elif raw_attachments and not att_store:
            attachment_errors.append("Attachment store is not available for the active project.")

        channel_id = f"session:{task_id}"
        pid = run_project_id

        # Look up session_id from task
        session_id: str | None = None
        task = None
        task_session_exec_mode = self._normalize_session_exec_mode(self._exec_mode)
        store = run_engine.store
        if self._store_is_ready(store):
            task = await store.get_task(task_id)
            if task:
                session_id = task.session_id
                task_session_exec_mode, _ = self._resolve_task_session_config(task)
            else:
                await self._send_ack(ws, ok=False, error="task_not_found", project_id=run_project_id, task_id=task_id)
                return

        # Only cancelled/deleted sessions are terminal — their session data has
        # been torn down, so no further input can be accepted. A DONE session is
        # NOT terminal: task-mode reuses the same primary task across follow-up
        # turns (engine `_find_reusable_task_mode_task` reopens a DONE task in the
        # same session), and company sessions accept follow-up text after final
        # delivery. Blocking DONE here made task mode one-shot — the second user
        # message was rejected before it ever reached the engine.
        if task:
            from opc.core.models import TaskStatus
            if task.status == TaskStatus.CANCELLED:
                try:
                    cancelled_target = await self._resolve_company_runtime_target(
                        task_id,
                        engine=run_engine,
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to resolve cancelled company UI anchor"
                    )
                    cancelled_target = None
                identity = (cancelled_target or {}).get("identity")
                active_cancelled_anchor = bool(
                    identity is not None
                    and str(getattr(identity, "ui_anchor_task_id", "") or "") == task_id
                    and str(getattr(identity, "pending_checkpoint_id", "") or "").strip()
                    and str(getattr(identity, "pending_checkpoint_type", "") or "").strip()
                    in COMPANY_RUNTIME_CHECKPOINT_TYPES
                    and str(getattr(identity, "pending_checkpoint_status", "") or "").strip().lower()
                    in ACTIVE_COMPANY_RUNTIME_CHECKPOINT_STATUSES
                )
                if not active_cancelled_anchor:
                    await self._send_ack(ws, ok=False, error="session_ended")
                    return

        if attachment_errors:
            helper_lines = [
                "Some attachments could not be included, so they were not sent to the model:",
                *[f"- {item}" for item in attachment_errors],
            ]
            helper = await self.chat_store.insert_message(
                channel_id=channel_id,
                sender="assistant",
                sender_name="OPC",
                content="\n".join(helper_lines),
                project_id=pid,
                metadata={"type": "system"},
            )
            await self.broadcast({"type": "session_message", "payload": helper})
            if raw_attachments and not attachment_refs and content.strip() == "Sent with attachments":
                return

        pending_escalation: dict[str, Any] | None = None
        background_pending_escalation: dict[str, Any] | None = None
        normalized_pending_reply: str | None = None

        # Insert user message to chat_store (UI rendering layer)
        reply_metadata: dict[str, Any] = {}
        raw_metadata = data.get("metadata")
        if isinstance(raw_metadata, dict):
            for key in (
                "response_to_checkpoint_id",
                "response_to_checkpoint_type",
                "response_to_escalation_id",
            ):
                value = raw_metadata.get(key)
                if value is None:
                    continue
                normalized_value = str(value).strip()
                if normalized_value:
                    reply_metadata[key] = normalized_value
            raw_checkpoint_reply_kind = str(raw_metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
            if raw_checkpoint_reply_kind in {"approve", "deny", "feedback", "ignore"}:
                reply_metadata["checkpoint_reply_kind"] = raw_checkpoint_reply_kind
            raw_ui_message_id = str(raw_metadata.get("ui_message_id", "") or "").strip()
            if raw_ui_message_id:
                reply_metadata["ui_message_id"] = raw_ui_message_id
            raw_role_agents = raw_metadata.get("recruitment_role_agents")
            if isinstance(raw_role_agents, dict):
                normalized_role_agents: dict[str, str] = {}
                for raw_role_id, raw_agent in raw_role_agents.items():
                    role_id = str(raw_role_id or "").strip()
                    agent_name = str(raw_agent or "").strip().lower()
                    if role_id and agent_name in _TASK_MODE_PREFERRED_AGENTS:
                        normalized_role_agents[role_id] = agent_name
                if normalized_role_agents:
                    reply_metadata["recruitment_role_agents"] = normalized_role_agents
            raw_recruitment_agent = str(raw_metadata.get("recruitment_agent", "") or "").strip().lower().replace("-", "_")
            if raw_recruitment_agent in _TASK_MODE_PREFERRED_AGENTS:
                reply_metadata["recruitment_agent"] = raw_recruitment_agent
            raw_staffing_action = str(raw_metadata.get("staffing_action", "") or "").strip().lower()
            if raw_staffing_action in {"manual_approve", "approve", "auto_recruit", "deny"}:
                reply_metadata["staffing_action"] = (
                    "manual_approve" if raw_staffing_action == "approve" else raw_staffing_action
                )
            raw_staffing_selections = raw_metadata.get("staffing_selections")
            if isinstance(raw_staffing_selections, dict):
                normalized_selections: dict[str, dict[str, str]] = {}
                for raw_role_id, raw_selection in raw_staffing_selections.items():
                    role_id = str(raw_role_id or "").strip()
                    if not role_id or not isinstance(raw_selection, dict):
                        continue
                    kind = str(raw_selection.get("kind", "") or "").strip().lower()
                    selected_id = str(
                        raw_selection.get("id")
                        or raw_selection.get("employee_id")
                        or raw_selection.get("template_id")
                        or ""
                    ).strip()
                    if kind in {"employee", "template"} and selected_id:
                        normalized_selections[role_id] = {"kind": kind, "id": selected_id}
                    elif kind == "fallback":
                        normalized_selections[role_id] = {"kind": "fallback", "id": ""}
                if normalized_selections:
                    reply_metadata["staffing_selections"] = normalized_selections
            raw_user_input_answers = raw_metadata.get("user_input_answers")
            if isinstance(raw_user_input_answers, dict):
                normalized_answers: dict[str, dict[str, Any]] = {}
                for raw_question_id, raw_answer in raw_user_input_answers.items():
                    question_id = str(raw_question_id or "").strip()
                    if not question_id:
                        continue
                    if isinstance(raw_answer, dict):
                        answer: dict[str, Any] = {}
                        for field in (
                            "question_id",
                            "question",
                            "selected_option_id",
                            "selected_label",
                            "freeform_text",
                            "answer_text",
                        ):
                            value = raw_answer.get(field)
                            if value is None:
                                continue
                            normalized_value = str(value).strip()
                            if normalized_value:
                                answer[field] = normalized_value
                        if answer:
                            answer.setdefault("question_id", question_id)
                            normalized_answers[question_id] = answer
                    else:
                        normalized_value = str(raw_answer or "").strip()
                        if normalized_value:
                            normalized_answers[question_id] = {
                                "question_id": question_id,
                                "answer_text": normalized_value,
                            }
                if normalized_answers:
                    reply_metadata["user_input_answers"] = normalized_answers

        # Idempotency on the client-generated message id: the WS client queues
        # sends while disconnected and flushes the queue after a reconnect, so
        # one typed message can be delivered more than once. The first delivery
        # persisted a row under this id in this channel; later copies are
        # acknowledged and dropped instead of dispatching a duplicate turn.
        client_message_id = str(reply_metadata.get("ui_message_id", "") or "").strip()
        if client_message_id:
            existing_scope = await self.chat_store.message_scope(client_message_id)
            if existing_scope == (channel_id, pid):
                logger.info(
                    f"session_send deduplicated re-delivered client message {client_message_id} "
                    f"for task {task_id}"
                )
                await self._send_ack(
                    ws,
                    ok=True,
                    action="session_send",
                    task_id=task_id,
                    project_id=pid,
                    deduplicated=True,
                    message_id=client_message_id,
                )
                return
            if existing_scope is not None:
                # Same id already used in another channel/project: never reuse it
                # as a row id there (insert_message REPLACEs by primary key).
                client_message_id = ""

        explicit_checkpoint_id = str(reply_metadata.get("response_to_checkpoint_id", "")).strip()
        explicit_checkpoint_type = str(reply_metadata.get("response_to_checkpoint_type", "")).strip()
        explicit_escalation_id = str(reply_metadata.get("response_to_escalation_id", "")).strip()
        explicit_human_escalation = (
            explicit_checkpoint_type == "human_escalation"
            or bool(explicit_escalation_id)
        )
        if explicit_human_escalation:
            pending_escalation_id = explicit_escalation_id
            if (
                not pending_escalation_id
                and reply_metadata.get("response_to_checkpoint_type") == "human_escalation"
            ):
                pending_escalation_id = reply_metadata.get("response_to_checkpoint_id")

            pending_escalation = self._find_pending_escalation(
                task_id=task_id,
                escalation_id=pending_escalation_id,
                project_id=pid,
            )
        elif not explicit_checkpoint_id and not explicit_checkpoint_type:
            background_pending_escalation = self._find_pending_escalation(
                task_id=task_id,
                project_id=pid,
            )

        stale_human_escalation = (
            explicit_human_escalation
            and not pending_escalation
            and bool(explicit_checkpoint_id or explicit_escalation_id)
        )
        if stale_human_escalation:
            handled_as_deferred = False
            if _looks_like_escalation_reply(content):
                stale_checkpoint_id = explicit_escalation_id or explicit_checkpoint_id
                # Duplicate clicks on an approval card that was JUST resolved
                # (e.g. the user's own first click) are a normal occurrence
                # when the server is slow: answer idempotently instead of
                # flipping the card to "stale" and spamming inactive warnings.
                card = None
                try:
                    card = await self.chat_store.get_checkpoint_message(
                        stale_checkpoint_id,
                        channel_id=channel_id,
                        checkpoint_type="human_escalation",
                        project_id=pid,
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        "Failed to load checkpoint card for stale escalation reply"
                    )
                card_meta = dict((card or {}).get("metadata", {}) or {})
                card_status = str(card_meta.get("checkpoint_status", "") or "").strip().lower()
                helper_text: str | None = None
                if card_status in {"resolved", "responded"}:
                    resolution_reply = str(
                        card_meta.get("checkpoint_resolution_reply", "") or ""
                    ).strip()
                    helper_text = (
                        "This approval was already handled"
                        + (f" (decision: {resolution_reply})" if resolution_reply else "")
                        + ". No further action is needed."
                    )
                else:
                    approval_context = card_meta.get("approval_context")
                    deferred_option = (
                        _normalize_escalation_reply(content, list(card_meta.get("options") or []))
                        if isinstance(approval_context, dict) and approval_context
                        else None
                    )
                    if deferred_option:
                        # The inline wait expired (or the server restarted), but
                        # the decision is still the user's to make: apply the
                        # grant, resolve the card, and resume the parked task.
                        outcome = await self._resolve_deferred_escalation_click(
                            engine=run_engine,
                            project_id=pid,
                            channel_id=channel_id,
                            checkpoint_id=stale_checkpoint_id,
                            card_meta=card_meta,
                            option_id=deferred_option,
                        )
                        if outcome.get("action") == "flow_through":
                            content = str(outcome.get("content") or content)
                            for key in (
                                "response_to_checkpoint_id",
                                "response_to_checkpoint_type",
                                "response_to_escalation_id",
                            ):
                                reply_metadata.pop(key, None)
                            reply_metadata.update(dict(outcome.get("reply_metadata") or {}))
                            handled_as_deferred = True
                        else:
                            helper_text = str(outcome.get("text") or "Decision recorded.")
                    else:
                        await self._mark_human_escalation_checkpoint_status(
                            stale_checkpoint_id,
                            status="stale",
                            project_id=pid,
                            channel_id=channel_id,
                            reason="reply_to_inactive_escalation",
                        )
                        helper_text = (
                            "That approval request is no longer active. "
                            "The approval card has been marked inactive in the session history."
                        )
                if helper_text is not None:
                    if await self._recent_identical_helper_exists(
                        channel_id, helper_text, project_id=pid
                    ):
                        return
                    helper = await self.chat_store.insert_message(
                        channel_id=channel_id,
                        sender="assistant",
                        sender_name="OPC",
                        content=helper_text,
                        project_id=pid,
                        metadata={"type": "system"},
                    )
                    await self.broadcast({"type": "session_message", "payload": helper})
                    return
            if not handled_as_deferred:
                for key in (
                    "response_to_checkpoint_id",
                    "response_to_checkpoint_type",
                    "response_to_escalation_id",
                ):
                    reply_metadata.pop(key, None)

        if (
            explicit_checkpoint_type == "company_delivery_feedback"
            and str(reply_metadata.get("checkpoint_reply_kind", "") or "").strip().lower() == "ignore"
        ):
            if await self._route_company_delivery_feedback_reply_if_pending(
                task_id=task_id,
                content=content,
                session_id=session_id,
                task=task,
                attachment_refs=attachment_refs or None,
                message_metadata=reply_metadata if reply_metadata else None,
                user_message_id=None,
                user_message_created_at=None,
                run_engine=run_engine,
                run_project_id=run_project_id,
                reply_channel_id=channel_id,
            ):
                return

        msg_metadata: dict = dict(reply_metadata)
        if attachment_refs:
            msg_metadata["attachment_refs"] = attachment_refs
        msg = await self.chat_store.insert_message(
            channel_id=channel_id,
            sender="user",
            sender_name="You",
            content=content,
            project_id=pid,
            metadata=msg_metadata if msg_metadata else None,
            # Persist under the client-generated id so re-deliveries of the same
            # send are detectable and the optimistic bubble merges with the echo.
            message_id=client_message_id or None,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

        if (
            explicit_checkpoint_id
            and explicit_checkpoint_type
            and explicit_checkpoint_type not in {"human_escalation", "company_delivery_feedback"}
        ):
            updated_checkpoint_msg = await self.chat_store.mark_checkpoint_responded(
                channel_id,
                explicit_checkpoint_id,
                checkpoint_type=explicit_checkpoint_type,
                response_message_id=str(msg.get("message_id") or "").strip() or None,
                response_metadata=reply_metadata if reply_metadata else None,
                project_id=pid,
            )
            if updated_checkpoint_msg is not None:
                await self.broadcast({"type": "session_message", "payload": updated_checkpoint_msg})

        # Auto-generate title from first message if task title is still default
        store = run_engine.store
        if self._store_is_ready(store) and task:
            if task.title in ("New Chat", ""):
                auto_title = _compact_session_title(content)
                task.title = auto_title
                await store.save_task(task)
                await self.chat_store.update_channel_name(channel_id, auto_title, project_id=pid)
                # Also update engine session title
                if run_engine.memory and session_id:
                    await run_engine.memory.update_session_title(session_id, auto_title)
                await self.broadcast({"type": "session_title_updated", "payload": {
                    "project_id": pid,
                    "task_id": task_id,
                    "title": auto_title,
                }})

        if background_pending_escalation:
            escalation_key = str(background_pending_escalation.get("escalation_id") or "").strip()
            helper_text = (
                "This approval is waiting for a card action. "
                "Please use the approval card buttons to approve or deny."
            )
            if not _looks_like_escalation_reply(content):
                allowed = [
                    str(opt.get("label") or opt.get("id") or "").strip()
                    for opt in background_pending_escalation.get("options", [])
                    if str(opt.get("id", "")).strip()
                ]
                if allowed:
                    helper_text = (
                        "This task is waiting for an approval decision. "
                        f"Use the approval card buttons: {', '.join(allowed)}."
                    )
            helper = await self.chat_store.insert_message(
                channel_id=channel_id,
                sender="assistant",
                sender_name="OPC",
                content=helper_text,
                project_id=pid,
                metadata={
                    "type": "system",
                    "pending_checkpoint_type": "human_escalation",
                    "pending_escalation_id": escalation_key,
                },
            )
            await self.broadcast({"type": "session_message", "payload": helper})
            return

        if pending_escalation:
            normalized = normalized_pending_reply
            if normalized is None:
                normalized = _normalize_escalation_reply(content, pending_escalation.get("options", []))
            if normalized is None:
                allowed = [
                    str(opt.get("label") or opt.get("id") or "").strip()
                    for opt in pending_escalation.get("options", [])
                    if str(opt.get("id", "")).strip()
                ]
                helper = await self.chat_store.insert_message(
                    channel_id=channel_id,
                    sender="assistant",
                    sender_name="OPC",
                    content=(
                        "This task is waiting for your escalation decision. "
                        f"Choose one of: {', '.join(allowed)}."
                    ),
                    metadata={
                        "checkpoint_type": "human_escalation",
                        "checkpoint_id": pending_escalation.get("escalation_id"),
                        "escalation_id": pending_escalation.get("escalation_id"),
                        "escalation_type": pending_escalation.get("escalation_type"),
                        "prompt": pending_escalation.get("message", ""),
                        "summary": pending_escalation.get("message", ""),
                        "options": pending_escalation.get("options", []),
                        "default_action": pending_escalation.get("default_action"),
                    },
                    project_id=pid,
                )
                await self.broadcast({"type": "session_message", "payload": helper})
                return
            future = pending_escalation.get("future")
            if future and not future.done():
                future.set_result(normalized)
            auto_resolved_ids = self._resolve_related_pending_escalations(pending_escalation, normalized)
            for escalation_id in auto_resolved_ids:
                updated = await self.chat_store.mark_checkpoint_responded(
                    channel_id,
                    escalation_id,
                    checkpoint_type="human_escalation",
                    response_message_id=msg.get("message_id"),
                    response_metadata=reply_metadata if reply_metadata else None,
                    project_id=pid,
                )
                if updated is not None:
                    await self.broadcast({"type": "session_message", "payload": updated})
            return

        if await self._route_company_delivery_feedback_reply_if_pending(
            task_id=task_id,
            content=content,
            session_id=session_id,
            task=task,
            attachment_refs=attachment_refs or None,
            message_metadata=reply_metadata if reply_metadata else None,
            user_message_id=str(msg.get("message_id") or "").strip() or None,
            user_message_created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
            run_engine=run_engine,
            run_project_id=run_project_id,
            reply_channel_id=channel_id,
        ):
            return

        if (
            task
            and self._is_company_session_exec_mode(task_session_exec_mode)
            and not explicit_checkpoint_id
            and not explicit_checkpoint_type
            and not explicit_escalation_id
        ):
            await self._supersede_pending_delivery_feedback_for_new_company_turn(
                task_id=task_id,
                session_id=session_id,
                run_engine=run_engine,
                run_project_id=run_project_id,
            )

        if await self._route_company_suspend_reply_if_pending(
            task_id=task_id,
            content=content,
            session_id=session_id,
            task=task,
            attachment_refs=attachment_refs or None,
            message_metadata=reply_metadata if reply_metadata else None,
            user_message_id=str(msg.get("message_id") or "").strip() or None,
            user_message_created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
            run_engine=run_engine,
            run_project_id=run_project_id,
        ):
            return

        if task and self._is_company_session_exec_mode(task_session_exec_mode):
            self._track_session(
                task_id,
                self._process_session_message(
                    task_id,
                    content,
                    session_id=session_id,
                    attachment_refs=attachment_refs or None,
                    message_metadata=reply_metadata if reply_metadata else None,
                    user_message_id=str(msg.get("message_id") or "").strip() or None,
                    user_message_created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
                    run_engine=run_engine,
                    run_project_id=run_project_id,
                ),
                project_id=run_project_id,
                engine=run_engine,
            )
            return

        # Route task-mode sessions through Dispatcher: classify intent, then either engine or direct reply.
        self._track_session(
            task_id,
            self._dispatch_session_message(
                task_id,
                content,
                session_id=session_id,
                attachment_refs=attachment_refs or None,
                message_metadata=reply_metadata if reply_metadata else None,
                user_message_id=str(msg.get("message_id") or "").strip() or None,
                user_message_created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
                run_engine=run_engine,
                run_project_id=run_project_id,
            ),
            project_id=run_project_id,
            engine=run_engine,
        )

    async def _cancel_task_tree(
        self,
        task_id: str,
        *,
        hard: bool = False,
        preserve_history: bool = False,
        store: Any | None = None,
    ) -> list[str]:
        """Cancel a task and its children, clean up engine store data.

        Args:
            hard: If True, hard-delete task rows and all lifecycle data
                  (used by session_delete).  If False, soft-cancel and
                  partial cleanup (used by session_stop).

        Returns all affected task_ids (parent + children).
        """
        all_task_ids: list[str] = [task_id]
        store = store or self.engine.store
        if not store:
            return all_task_ids
        from opc.core.models import TaskStatus
        task = await store.get_task(task_id)
        if not task:
            return all_task_ids

        # Collect child tasks first (before any deletion)
        child_tasks: list[tuple[str, str | None]] = []
        parent_sid = task.session_id or task_id
        project_id = task.project_id or getattr(store, "project_id", None) or self.engine.project_id or "default"
        try:
            siblings = await store.get_tasks(project_id=project_id)
            for sib in siblings:
                if sib.id == task_id:
                    continue
                if getattr(sib, "parent_session_id", None) == parent_sid:
                    child_tasks.append((sib.id, sib.session_id))
                    all_task_ids.append(sib.id)
        except Exception:
            logger.debug(f"Failed to find children of {task_id}")

        async def _cancel_child_via_phase(child_task: Any) -> None:
            """Cascade CANCELLED to the child's work_item.phase so the UI
            column + all projection layers update. Plain tasks keep legacy
            Task.status behavior through the shared transition fallback.
            """
            child_task.metadata = dict(getattr(child_task, "metadata", {}) or {})
            child_task.metadata["last_stop_reason"] = "user_stop"
            await apply_task_status_transition(
                store,
                child_task,
                target_status_or_phase=TaskStatus.CANCELLED,
                reason="user_stop",
                metadata_updates={"last_stop_reason": "user_stop"},
                release_claim=True,
            )

        if hard:
            # Hard-delete: children first, then parent (preserves session ref-count accuracy)
            for child_id, child_sid in child_tasks:
                try:
                    await store.hard_delete_task(child_id, child_sid)
                except Exception:
                    logger.debug(f"hard_delete_task failed for child {child_id}")
            try:
                await store.hard_delete_task(task_id, task.session_id)
            except Exception:
                logger.debug(f"hard_delete_task failed for {task_id}")
        elif preserve_history:
            # Parent stays IDLE (suspend-for-resume — user hit Stop, may resume
            # later). We do NOT transition parent's work_item here; resuming
            # should pick it back up in whatever phase it was parked in.
            task.metadata["last_stop_reason"] = "user_stop"
            task.status = TaskStatus.IDLE
            await store.save_task(task)
            for child_id, _child_sid in child_tasks:
                try:
                    child_task = await store.get_task(child_id)
                    if child_task:
                        await _cancel_child_via_phase(child_task)
                except Exception:
                    logger.debug(f"Failed to preserve stop state for child task {child_id}")
        else:
            # Soft-cancel: cascade CANCELLED phase to parent + children
            await apply_task_status_transition(
                store,
                task,
                target_status_or_phase=TaskStatus.CANCELLED,
                reason="user_cancel",
                release_claim=True,
            )
            try:
                await store.delete_session_data(task_id, task.session_id)
            except Exception:
                logger.debug(f"Failed to clean session data for {task_id}")
            for child_id, child_sid in child_tasks:
                try:
                    child_task = await store.get_task(child_id)
                    if child_task:
                        await _cancel_child_via_phase(child_task)
                    await store.delete_session_data(child_id, child_sid)
                except Exception:
                    pass

        # Clean progress buffers + cancel background tasks for all affected
        for tid in all_task_ids:
            self._progress_buffer.pop(tid, None)
            self._progress_project_ids.pop(tid, None)
            if hard:
                try:
                    await self.chat_store.delete_progress(tid, project_id=project_id)
                except Exception:
                    pass
            self._cancel_session_tasks(tid)

        return all_task_ids

    async def _handle_session_stop(self, ws: Any, data: dict) -> None:
        """Stop a running task: suspend company runtime, cancel legacy task mode."""
        task_id = data.get("task_id", "")
        if not task_id:
            return

        run_engine, run_project_id = await self._engine_for_request(data)
        store = run_engine.store
        task = None
        if self._store_is_ready(store):
            try:
                task = await store.get_task(task_id)
            except Exception:
                logger.opt(exception=True).debug(f"Failed to load task for stop: {task_id}")
            if task is None:
                await self._send_ack(ws, ok=False, error="task_not_found", project_id=run_project_id, task_id=task_id)
                return

        target = None
        if task is not None:
            try:
                target = await self._resolve_company_runtime_target(task_id, engine=run_engine)
            except Exception:
                logger.opt(exception=True).warning(
                    f"failed to resolve company runtime stop target for {task_id}"
                )
            if target is not None:
                parent_session_id = str(target.get("runtime_session_id", "") or "").strip()
                existing_checkpoint = None
                try:
                    existing_checkpoint = await run_engine.get_pending_company_runtime_suspend_checkpoint(parent_session_id)
                except Exception:
                    logger.opt(exception=True).debug("failed to check existing company suspend checkpoint")
                existing_intent = self._company_stop_intents.get(parent_session_id)
                existing_finalizer = self._company_stop_finalize_tasks.get(parent_session_id)
                if existing_checkpoint is not None:
                    await self._set_company_runtime_control(
                        target,
                        state="suspended",
                        checkpoint_id=existing_checkpoint.checkpoint_id,
                        stop_intent_id=str((existing_checkpoint.payload or {}).get("stop_intent_id", "") or ""),
                    )
                    await self._send_ack(ws, ok=True, idempotent=True)
                    return
                if existing_intent or (existing_finalizer is not None and not existing_finalizer.done()):
                    await self._set_company_runtime_control(
                        target,
                        state="suspending",
                        stop_intent_id=str((existing_intent or {}).get("stop_intent_id", "") or ""),
                    )
                    await self._send_ack(ws, ok=True, idempotent=True)
                    return

                stop_intent_id = str(uuid.uuid4())
                self._company_stop_intents[parent_session_id] = {
                    "stop_intent_id": stop_intent_id,
                    "requested_at": datetime.now().isoformat(),
                    "origin_task_id": target.get("origin_task_id", task_id),
                }
                await self._set_company_runtime_control(
                    target,
                    state="suspending",
                    stop_intent_id=stop_intent_id,
                )
                finalizer = self._track(
                    self._finalize_company_runtime_stop(target, stop_intent_id=stop_intent_id)
                )
                self._company_stop_finalize_tasks[parent_session_id] = finalizer
                await self._send_ack(ws, ok=True, stop_intent_id=stop_intent_id)
                return

            if is_company_runtime_task(task):
                await self._send_ack(
                    ws,
                    ok=False,
                    error="company_runtime_identity_mismatch",
                    project_id=run_project_id,
                    task_id=task_id,
                )
                return

        try:
            all_task_ids = await self._cancel_task_tree(task_id, preserve_history=True, store=store)
        except Exception:
            logger.opt(exception=True).warning(f"_cancel_task_tree failed for {task_id}")
            all_task_ids = [task_id]
        self._stop_requested_task_ids.update(all_task_ids)

        # Broadcast status updates for all affected tasks
        for tid in all_task_ids:
            is_primary_task = tid == task_id
            try:
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": run_project_id,
                    "task_id": tid,
                    "column_id": "in-progress" if is_primary_task else "done",
                    "status": "idle" if is_primary_task else "cancelled",
                }})
                stop_payload: dict[str, Any] = {
                    "project_id": run_project_id,
                    "task_id": tid, "status": "idle", "current_tool": None, "iteration": 0,
                }
                # Resolve agent_id from event_adapter map or task.assigned_to
                stop_task = None
                if self._store_is_ready(store):
                    try:
                        stop_task = await store.get_task(tid)
                    except Exception:
                        pass
                resolved_agent = self._resolve_agent_for_idle(tid, stop_task)
                if resolved_agent:
                    stop_payload["agent_id"] = resolved_agent
                await self.broadcast({"type": "agent_runtime_update", "payload": stop_payload})
            except Exception:
                logger.opt(exception=True).warning(f"Failed to broadcast stop status for {tid}")

        # Insert system message (only for the primary task — children are internal)
        channel_id = f"session:{task_id}"
        pid = run_project_id
        try:
            msg = await self.chat_store.insert_message(
                channel_id=channel_id,
                sender="system",
                sender_name="System",
                content="Task stopped by user",
                project_id=pid,
                metadata={"type": "system", "stop_reason": "user_stop"},
            )
            await self.broadcast({"type": "session_message", "payload": msg})
        except Exception:
            logger.opt(exception=True).warning(f"Failed to insert stop message for {task_id}")
        await self._send_ack(ws, ok=True)

    async def _handle_session_resume(self, ws: Any, data: dict) -> None:
        """Resume by durable runtime-session/checkpoint identity."""
        task_id = str(data.get("task_id", "") or "").strip()
        if not task_id:
            await self._send_ack(ws, ok=False, error="missing_task_id")
            return
        run_engine, run_project_id = await self._engine_for_request(data)

        async def reject(error: str, **extra: Any) -> None:
            await self._send_ack(ws, ok=False, error=error, **extra)
            await self._refresh_runtime_control_for_client(
                ws,
                engine=run_engine,
                project_id=run_project_id,
            )

        task = None
        session_id_override = str(data.get("session_id", "") or "").strip() or None
        if self._store_is_ready(run_engine.store):
            try:
                task = await run_engine.store.get_task(task_id)
            except Exception:
                logger.opt(exception=True).warning(f"session_resume: get_task failed for {task_id}")
            if task is None:
                await reject("task_not_found", project_id=run_project_id, task_id=task_id)
                return
        session_id = session_id_override
        if not session_id and task is not None:
            session_id = str(task.session_id or task.parent_session_id or "").strip() or None
        exec_mode, _company_profile = self._resolve_task_session_config(task)
        runtime_session_id = str(data.get("runtime_session_id", "") or "").strip()
        checkpoint_id = str(data.get("checkpoint_id", "") or "").strip()
        is_company_resume = bool(runtime_session_id or checkpoint_id) or exec_mode in {
            "company",
            "org",
            "custom",
        }
        if is_company_resume and task is not None:
            if not runtime_session_id:
                await reject("missing_runtime_session_id")
                return
            if not checkpoint_id:
                await reject("missing_checkpoint_id")
                return
            try:
                target = await self._resolve_company_runtime_target(
                    task_id,
                    engine=run_engine,
                    runtime_session_id=runtime_session_id,
                    checkpoint_id=checkpoint_id,
                )
            except Exception:
                logger.opt(exception=True).warning(f"session_resume: failed to resolve company runtime target for {task_id}")
                target = None
            if target is None:
                await reject(
                    "company_runtime_identity_mismatch",
                    project_id=run_project_id,
                    task_id=task_id,
                )
                return
            checkpoint = target.get("checkpoint")
            if checkpoint is None or str(getattr(checkpoint, "status", "") or "").strip().lower() != "pending":
                await reject("checkpoint_not_pending", checkpoint_id=checkpoint_id)
                return
            session_id = runtime_session_id
            finalizer = self._company_stop_finalize_tasks.get(runtime_session_id)
            if finalizer is not None and not finalizer.done():
                try:
                    await asyncio.wait_for(asyncio.shield(finalizer), timeout=10.0)
                except asyncio.TimeoutError:
                    await reject("stop_finalize_in_progress")
                    return
                except Exception:
                    logger.opt(exception=True).debug("session_resume: stop finalizer ended with error")
            self._company_stop_intents.pop(runtime_session_id, None)
            content = str(data.get("content", "") or "").strip() or "Resume the existing runtime."
            lock = self._company_suspend_reply_locks.get(runtime_session_id)
            if lock is not None:
                self._release_current_execution_handoff()
                await reject(
                    "checkpoint_handoff_in_progress",
                    checkpoint_id=checkpoint_id,
                )
                return
            lock = asyncio.Lock()
            self._company_suspend_reply_locks[runtime_session_id] = lock
            try:
                bg = self._track_session(
                    task_id,
                    self._process_company_suspend_reply(
                        ui_task_id=task_id,
                        runtime_session_id=runtime_session_id,
                        content=content,
                        attachment_refs=None,
                        message_metadata={
                            "ui_force_resume": True,
                            "response_to_checkpoint_id": checkpoint_id,
                            "response_to_checkpoint_type": str(getattr(checkpoint, "checkpoint_type", "") or ""),
                        },
                        user_message_id=None,
                        user_message_created_at=None,
                        run_engine=run_engine,
                        run_project_id=run_project_id,
                        target=target,
                        checkpoint=checkpoint,
                        lock=lock,
                    ),
                    project_id=run_project_id,
                    engine=run_engine,
                )
            except BaseException:
                self._company_suspend_reply_locks.pop(runtime_session_id, None)
                raise
            self._task_bg_context[bg]["company_suspend_reply"] = True
            await self._send_ack(
                ws,
                ok=True,
                runtime_session_id=runtime_session_id,
                checkpoint_id=checkpoint_id,
            )
            return
        if not session_id:
            await reject("session_not_found")
            return

        content = str(data.get("content", "") or "").strip() or "Resume the existing runtime."

        self._track_session(
            task_id,
            self._process_session_message(
                task_id,
                content,
                session_id=session_id,
                message_metadata={"ui_force_resume": True},
                run_engine=run_engine,
                run_project_id=run_project_id,
            ),
            project_id=run_project_id,
            engine=run_engine,
        )
        await self._send_ack(ws, ok=True)

    async def _load_execution_checkpoint_for_reply(
        self,
        *,
        engine: Any,
        project_id: str,
        checkpoint_id: str,
        checkpoint_type: str,
    ) -> Any | None:
        normalized_checkpoint_id = str(checkpoint_id or "").strip()
        normalized_checkpoint_type = str(checkpoint_type or "").strip()
        if not normalized_checkpoint_id:
            return None

        store = getattr(engine, "store", None)
        getter = getattr(store, "get_execution_checkpoints", None)
        if callable(getter):
            try:
                checkpoints = await getter(project_id=project_id)
            except TypeError:
                checkpoints = await getter(project_id)
            except Exception:
                logger.opt(exception=True).debug("failed to list checkpoints for explicit reply routing")
                checkpoints = []
            for checkpoint in list(checkpoints or []):
                if str(getattr(checkpoint, "checkpoint_id", "") or "").strip() != normalized_checkpoint_id:
                    continue
                if (
                    normalized_checkpoint_type
                    and str(getattr(checkpoint, "checkpoint_type", "") or "").strip() != normalized_checkpoint_type
                ):
                    return None
                return checkpoint

        direct_lookup = getattr(engine, "_load_execution_checkpoint_by_id", None)
        if callable(direct_lookup):
            try:
                maybe_checkpoint = direct_lookup(normalized_checkpoint_id)
                checkpoint = await maybe_checkpoint if inspect.isawaitable(maybe_checkpoint) else maybe_checkpoint
            except Exception:
                logger.opt(exception=True).debug("failed to load checkpoint by id for explicit reply routing")
                checkpoint = None
            if checkpoint is not None:
                if str(getattr(checkpoint, "checkpoint_id", "") or "").strip() != normalized_checkpoint_id:
                    return None
                if (
                    normalized_checkpoint_type
                    and str(getattr(checkpoint, "checkpoint_type", "") or "").strip() != normalized_checkpoint_type
                ):
                    return None
                return checkpoint
        return None

    async def _handle_session_delete(self, ws: Any, data: dict) -> None:
        """Delete task/session data and notify frontend."""
        task_id = data.get("task_id", "")
        if not task_id:
            return
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().session.delete(project_id=project_id, task_id=task_id)
            self._session_to_task.update(self.services_context.session_to_task)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, project_id=project_id)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="session_delete")

    async def _handle_session_complete(self, ws: Any, data: dict) -> None:
        """Mark a session's task as DONE (user explicitly completes it)."""
        task_id = data.get("task_id", "")
        if not task_id:
            return
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().session.complete(project_id=project_id, task_id=task_id)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, project_id=project_id)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="session_complete")

    async def _handle_session_update_title(self, ws: Any, data: dict) -> None:
        """Update session/task title."""
        task_id = data.get("task_id", "")
        title = data.get("title", "")
        if not task_id or not title:
            return
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().session.rename(
                project_id=project_id,
                task_id=task_id,
                title=title,
            )
            await self._publish_service_result(result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="session_update_title")

    async def _dispatch_session_message(
        self, task_id: str, content: str, *, session_id: str | None = None,
        attachment_refs: list[dict] | None = None,
        message_metadata: dict[str, Any] | None = None,
        user_message_id: str | None = None,
        user_message_created_at: float | None = None,
        run_engine: Any | None = None,
        run_project_id: str | None = None,
    ) -> None:
        """Route through Dispatcher: classify → engine pipeline or direct reply."""
        captured_context = run_engine is not None or run_project_id is not None
        engine = run_engine or self.engine
        pid = self._normalize_project_id(run_project_id or getattr(engine, "project_id", None))
        channel_id = f"session:{task_id}"
        dispatcher = self.dispatcher if engine is self.engine else Dispatcher(engine, self.chat_store)
        try:
            result = await dispatcher.handle(
                task_id,
                content,
                session_id=session_id,
                has_attachments=bool(attachment_refs),
            )
            if result.route == "engine":
                process_kwargs: dict[str, Any] = {
                    "session_id": session_id,
                    "attachment_refs": attachment_refs,
                }
                if message_metadata:
                    process_kwargs["message_metadata"] = message_metadata
                if user_message_id:
                    process_kwargs["user_message_id"] = user_message_id
                if user_message_created_at is not None:
                    process_kwargs["user_message_created_at"] = user_message_created_at
                if captured_context:
                    process_kwargs["run_engine"] = engine
                    process_kwargs["run_project_id"] = pid
                await self._process_session_message(task_id, content, **process_kwargs)
            else:
                # Direct reply from Dispatcher (status query, conversation, session control)
                msg = await self.chat_store.insert_message(
                    channel_id=channel_id,
                    sender="assistant",
                    sender_name="OPC",
                    content=result.response,
                    project_id=pid,
                )
                await self.broadcast({"type": "session_message", "payload": msg})
                # Record exchange in engine memory for context continuity
                if engine.memory and session_id:
                    user_turn_meta = _ui_message_identity_metadata(
                        kind="top_level_user_turn",
                        message_id=user_message_id,
                        conversation_turn_id=_ui_conversation_turn_id(user_message_id),
                        created_at=user_message_created_at,
                    )
                    await engine.memory.record_user_turn(
                        session_id, content,
                        project_id=pid,
                        metadata=user_turn_meta or None,
                    )
                    assistant_turn_meta = _ui_message_identity_metadata(
                        kind="top_level_reply",
                        message_id=str(msg.get("message_id") or "").strip() or None,
                        created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
                    )
                    await engine.memory.record_assistant_turn(
                        session_id, result.response,
                        project_id=pid,
                        metadata=assistant_turn_meta or None,
                    )
        except Exception as e:
            logger.opt(exception=True).error(f"Dispatcher error, falling back to engine: {e}")
            process_kwargs = {
                "session_id": session_id,
                "attachment_refs": attachment_refs,
            }
            if message_metadata:
                process_kwargs["message_metadata"] = message_metadata
            if user_message_id:
                process_kwargs["user_message_id"] = user_message_id
            if user_message_created_at is not None:
                process_kwargs["user_message_created_at"] = user_message_created_at
            if captured_context:
                process_kwargs["run_engine"] = engine
                process_kwargs["run_project_id"] = pid
            await self._process_session_message(task_id, content, **process_kwargs)

    async def _execution_checkpoint_status(
        self,
        *,
        engine: Any,
        project_id: str,
        checkpoint_id: str,
        checkpoint_type: str | None = None,
    ) -> str:
        normalized_checkpoint_id = str(checkpoint_id or "").strip()
        normalized_checkpoint_type = str(checkpoint_type or "").strip()
        if not normalized_checkpoint_id:
            return ""
        direct_lookup = getattr(engine, "_load_execution_checkpoint_by_id", None)
        checkpoint = None
        if callable(direct_lookup):
            try:
                checkpoint = await direct_lookup(normalized_checkpoint_id)
            except Exception:
                logger.opt(exception=True).debug("failed to load checkpoint by id from engine")
        if checkpoint is None:
            store = getattr(engine, "store", None)
            getter = getattr(store, "get_execution_checkpoints", None)
            if callable(getter):
                try:
                    checkpoints = await getter(project_id=project_id)
                except TypeError:
                    checkpoints = await getter(project_id)
                for item in checkpoints:
                    if str(getattr(item, "checkpoint_id", "") or "").strip() == normalized_checkpoint_id:
                        checkpoint = item
                        break
        if checkpoint is None:
            return ""
        if normalized_checkpoint_type and str(getattr(checkpoint, "checkpoint_type", "") or "").strip() != normalized_checkpoint_type:
            return ""
        return str(getattr(checkpoint, "status", "") or "").strip().lower()

    async def _mark_checkpoint_card_after_engine_response(
        self,
        *,
        channel_id: str,
        project_id: str,
        engine: Any,
        message_metadata: dict[str, Any] | None,
        response_message_id: str | None,
    ) -> dict[str, Any] | None:
        metadata = dict(message_metadata or {})
        checkpoint_id = str(metadata.get("response_to_checkpoint_id", "") or "").strip()
        checkpoint_type = str(metadata.get("response_to_checkpoint_type", "") or "").strip()
        if not checkpoint_id or checkpoint_type == "human_escalation":
            return None
        status = await self._execution_checkpoint_status(
            engine=engine,
            project_id=project_id,
            checkpoint_id=checkpoint_id,
            checkpoint_type=checkpoint_type,
        )
        if not status or status == "pending":
            return None
        try:
            if status == "resolved":
                updated = await self.chat_store.mark_checkpoint_responded(
                    channel_id,
                    checkpoint_id,
                    checkpoint_type=checkpoint_type,
                    response_message_id=response_message_id,
                    response_metadata=metadata,
                    project_id=project_id,
                )
                if updated is not None:
                    return updated
                updated = await self.chat_store.update_checkpoint_status(
                    checkpoint_id,
                    channel_id=None,
                    checkpoint_type=checkpoint_type,
                    status="responded",
                    response_message_id=response_message_id,
                    response_metadata=metadata,
                    project_id=project_id,
                )
                if updated is not None:
                    return updated
                checkpoint = await self._load_execution_checkpoint_for_reply(
                    engine=engine,
                    project_id=project_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_type=checkpoint_type,
                )
                return await self._update_or_emit_checkpoint_card_status(
                    checkpoint_id,
                    channel_id=channel_id,
                    checkpoint_type=checkpoint_type,
                    status="responded",
                    checkpoint=checkpoint,
                    response_message_id=response_message_id,
                    response_metadata=metadata,
                    project_id=project_id,
                    broadcast_update=False,
                )
            updated = await self.chat_store.update_checkpoint_status(
                checkpoint_id,
                channel_id=channel_id,
                checkpoint_type=checkpoint_type,
                status=status,
                response_message_id=response_message_id,
                response_metadata=metadata,
                project_id=project_id,
            )
            if updated is not None:
                return updated
            updated = await self.chat_store.update_checkpoint_status(
                checkpoint_id,
                channel_id=None,
                checkpoint_type=checkpoint_type,
                status=status,
                response_message_id=response_message_id,
                response_metadata=metadata,
                project_id=project_id,
            )
            if updated is not None:
                return updated
            checkpoint = await self._load_execution_checkpoint_for_reply(
                engine=engine,
                project_id=project_id,
                checkpoint_id=checkpoint_id,
                checkpoint_type=checkpoint_type,
            )
            return await self._update_or_emit_checkpoint_card_status(
                checkpoint_id,
                channel_id=channel_id,
                checkpoint_type=checkpoint_type,
                status=status,
                checkpoint=checkpoint,
                response_message_id=response_message_id,
                response_metadata=metadata,
                project_id=project_id,
                broadcast_update=False,
            )
        except Exception:
            logger.opt(exception=True).debug("failed to persist checkpoint card terminal state")
            return None

    async def _process_session_message(
        self, task_id: str, content: str, *,
        session_id: str | None = None,
        attachment_refs: list[dict] | None = None,
        message_metadata: dict[str, Any] | None = None,
        user_message_id: str | None = None,
        user_message_created_at: float | None = None,
        run_engine: Any | None = None,
        run_project_id: str | None = None,
    ) -> None:
        """Process user message in session context via engine.

        Passes session_id to engine.process_message so the engine can:
        - Record user/assistant turns to session memory
        - Build session-aware context for agent execution

        Temporarily overrides engine.on_progress so that all progress
        during this call routes to session:{task_id}.
        """
        engine = run_engine or self.engine
        pid = self._normalize_project_id(run_project_id or getattr(engine, "project_id", None))
        channel_id = f"session:{task_id}"

        # Register session→task mapping for company runtime child resolution
        if session_id:
            self._session_to_task[session_id] = task_id

        session_exec_mode = self._normalize_session_exec_mode(self._exec_mode)
        session_company_profile = self._normalize_session_company_profile(self._company_profile)
        session_preferred_agent = self._task_preferred_agent
        session_org_id = ""
        task = None
        store = engine.store
        if self._store_is_ready(store):
            from opc.core.models import TaskStatus
            task = await store.get_task(task_id)
            if task:
                session_exec_mode, session_company_profile = self._resolve_task_session_config(task)
                session_org_id = self._resolve_task_org_id(task)
                session_preferred_agent = self._resolve_task_preferred_agent(task)

        # Per-task lock: same session serialized, different sessions concurrent
        async with self._get_task_lock(task_id):
            current_task = asyncio.current_task()
            if current_task is not None:
                self._task_lock_holders[task_id] = current_task
            if self._store_is_ready(store):
                from opc.core.models import TaskStatus
                task = await store.get_task(task_id)
                if task:
                    session_exec_mode, session_company_profile = self._resolve_task_session_config(task)
                    session_org_id = self._resolve_task_org_id(task)
                    session_preferred_agent = self._resolve_task_preferred_agent(task)
                    if task.status == TaskStatus.DONE and self._is_company_session_exec_mode(session_exec_mode):
                        task.status = TaskStatus.IDLE
                        task.metadata = dict(getattr(task, "metadata", {}) or {})
                        task.metadata["company_session_reopened_at"] = datetime.now().isoformat()
                        await store.save_task(task)
                    await apply_task_status_transition(
                        store,
                        task,
                        target_status_or_phase=TaskStatus.RUNNING,
                        reason="session_message_started",
                    )
            await self.broadcast({"type": "board_task_status_changed", "payload": {
                "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "running",
            }})
            # Register company runtime origin so child-task progress can dual-route
            company_runtime_target: dict[str, Any] | None = None
            if session_exec_mode in ("company", "org", "custom"):
                self._active_runtime_children[task_id] = task_id  # primary maps to self
                try:
                    company_runtime_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                    await self._set_company_runtime_control(company_runtime_target, state="running")
                except Exception:
                    logger.opt(exception=True).debug("failed to mark company session runtime running")
            try:
                engine_mode, company_profile = self._resolve_engine_mode(
                    session_exec_mode,
                    session_company_profile,
                )
                engine_preferred_agent = session_preferred_agent if session_exec_mode == "task" else None
                if task is not None:
                    await self._persist_session_config(
                        task,
                        exec_mode=session_exec_mode,
                        company_profile=session_company_profile,
                        preferred_agent=session_preferred_agent,
                        org_id=session_org_id,
                        engine=engine,
                    )
                engine_message_metadata = dict(message_metadata or {})
                engine_message_metadata.update(_ui_message_identity_metadata(
                    message_id=user_message_id,
                    conversation_turn_id=_ui_conversation_turn_id(user_message_id),
                    created_at=user_message_created_at,
                ))
                response = await engine.process_message(
                    content,
                    project_id=pid,
                    session_id=session_id,
                    mode=engine_mode,
                    org_id=session_org_id or None,
                    company_profile=company_profile,
                    preferred_agent=engine_preferred_agent,
                    origin_task_id=task_id,
                    attachment_refs=attachment_refs,
                    message_metadata=engine_message_metadata or None,
                )
                updated_checkpoint_msg = await self._mark_checkpoint_card_after_engine_response(
                    channel_id=channel_id,
                    project_id=pid,
                    engine=engine,
                    message_metadata=engine_message_metadata,
                    response_message_id=user_message_id,
                )
                if updated_checkpoint_msg is not None:
                    await self.broadcast({"type": "session_message", "payload": updated_checkpoint_msg})
                # ── Check for pending checkpoint → attach structured metadata ──
                checkpoint_meta = await self._extract_checkpoint_metadata(
                    task_id, session_id=session_id, engine=engine,
                )
                await self._sync_task_transcript_messages(
                    task_id,
                    engine=engine,
                    latest_assistant_metadata=checkpoint_meta if checkpoint_meta else None,
                )
                await self._ensure_reply_projected(
                    channel_id=channel_id,
                    project_id=pid,
                    session_id=session_id or (str(getattr(task, "session_id", "") or "").strip() if task else None),
                    engine=engine,
                )

                # ── Status: idle only while the engine left the task active ──
                store = engine.store
                final_status = "idle"
                final_column_id = "in-progress"
                if self._store_is_ready(store):
                    from opc.core.models import TaskStatus as TS
                    t = await store.get_task(task_id)
                    if t:
                        try:
                            current_status = t.status if isinstance(t.status, TS) else TS(str(t.status))
                        except ValueError:
                            current_status = TS.IDLE
                        if current_status in {TS.PENDING, TS.RUNNING}:
                            t.status = TS.IDLE
                            await store.save_task(t)
                            current_status = TS.IDLE
                        final_status = current_status.value
                        if current_status in {TS.DONE, TS.CANCELLED}:
                            final_column_id = "done"
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": pid, "task_id": task_id, "column_id": final_column_id, "status": final_status,
                }})
                if session_exec_mode in ("company", "org", "custom"):
                    try:
                        idle_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                        await self._set_company_runtime_control(idle_target or company_runtime_target, state="idle")
                    except Exception:
                        logger.opt(exception=True).debug("failed to mark company session runtime idle")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"Session processing error: {e}")
                msg = await self.chat_store.insert_message(
                    channel_id=channel_id,
                    sender="system",
                    sender_name="OPC",
                    content=f"Error: {e}",
                    project_id=pid,
                )
                await self.broadcast({"type": "session_message", "payload": msg})

                # ── Status: failed ─────────────────────────────────────
                store = engine.store
                if self._store_is_ready(store):
                    from opc.core.models import TaskStatus as TS
                    t = await store.get_task(task_id)
                    if t:
                        t.status = TS.FAILED
                        await store.save_task(t)
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "failed",
                }})
                if session_exec_mode in ("company", "org", "custom"):
                    try:
                        failed_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                        await self._set_company_runtime_control(failed_target or company_runtime_target, state="idle")
                    except Exception:
                        logger.opt(exception=True).debug("failed to clear company session runtime after error")
            finally:
                # Flush progress buffers before clearing company runtime mappings
                child_ids = [k for k, v in self._active_runtime_children.items() if v == task_id and k != task_id]
                for cid in child_ids:
                    await self._flush_progress(cid, project_id=pid)
                await self._flush_progress(task_id, project_id=pid)
                # Clean up per-task state
                self._active_runtime_children.pop(task_id, None)
                for k in child_ids:
                    self._active_runtime_children.pop(k, None)
                    self._stop_requested_task_ids.discard(k)
                self._task_locks.pop(task_id, None)
                self._task_lock_holders.pop(task_id, None)
                self._stop_requested_task_ids.discard(task_id)
                if session_id:
                    self._session_to_task.pop(session_id, None)
                # Clear agent runtime indicator — include agent_id so the
                # frontend can also clear the swarm agent's reflecting/tool_active state.
                idle_payload: dict[str, Any] = {
                    "project_id": pid,
                    "task_id": task_id, "status": "idle", "current_tool": None, "iteration": 0,
                }
                resolved_agent = self._resolve_agent_for_idle(task_id, task)
                if resolved_agent:
                    idle_payload["agent_id"] = resolved_agent
                await self.broadcast({"type": "agent_runtime_update", "payload": idle_payload})

    async def _extract_checkpoint_metadata(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        engine: Any | None = None,
    ) -> dict[str, Any] | None:
        """Query pending checkpoint from engine store and build structured metadata.

        Called after engine.process_message() returns.  The engine saves the
        checkpoint *before* returning its summary string, so a pending
        checkpoint is guaranteed to exist here if the response represents an
        interactive confirmation prompt.

        Returns a dict suitable for ChatStore message ``metadata``, or None.
        """
        runtime_engine = engine or self.engine
        checkpoint = await runtime_engine.get_latest_pending_checkpoint_for_session(session_id)
        if not checkpoint:
            return None

        if checkpoint.checkpoint_type == "task_user_input":
            return await self._build_task_user_input_meta(checkpoint, engine=runtime_engine)
        if checkpoint.checkpoint_type == "company_work_item_gate":
            return self._build_company_work_item_gate_meta(checkpoint)
        if checkpoint.checkpoint_type == "company_staffing_selection":
            return self._build_staffing_selection_meta(checkpoint)
        if checkpoint.checkpoint_type == "company_recruitment_confirmation":
            return self._build_recruitment_meta(checkpoint, engine=runtime_engine)
        if checkpoint.checkpoint_type == "company_reorg_pending":
            return await self._build_reorg_meta(checkpoint, engine=runtime_engine)
        if checkpoint.checkpoint_type == "company_delivery_feedback":
            return self._build_delivery_feedback_meta(checkpoint)
        return None

    async def _build_task_user_input_meta(
        self,
        cp: Any,
        *,
        engine: Any | None = None,
    ) -> dict[str, Any] | None:
        payload = dict(cp.payload or {})
        pause_request = dict(payload.get("pause_request", {}) or {})
        task_id = str(payload.get("task_id", "")).strip()
        work_item_projection_id = ""
        work_item_projection_title = ""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        work_item_turn_type = ""
        if store and task_id:
            task = await store.get_task(task_id)
            if task:
                task_metadata = dict(getattr(task, "metadata", {}) or {})
                work_item_projection_id = str(task_metadata.get("work_item_projection_id", "") or "").strip()
                work_item_turn_type = str(task_metadata.get("work_item_turn_type", "") or "").strip()
                work_item_projection_title = str(task.title or "").strip()

        prompt = str(payload.get("prompt", "") or "").strip()
        summary = str(pause_request.get("reason", "") or prompt).strip()
        questions = [str(item).strip() for item in list(pause_request.get("questions", []) or []) if str(item).strip()]
        input_questions = [
            dict(item)
            for item in list(pause_request.get("input_questions", []) or [])
            if isinstance(item, dict) and str(item.get("question", "") or item.get("header", "") or "").strip()
        ]
        if not input_questions:
            input_questions = [
                {
                    "id": f"question_{index + 1}",
                    "header": "",
                    "question": question,
                    "options": [],
                    "allow_freeform": True,
                    "required": True,
                }
                for index, question in enumerate(questions)
            ]
        required_fields = [
            str(item).strip()
            for item in list(pause_request.get("required_fields", []) or [])
            if str(item).strip()
        ]
        resume_hint = str(pause_request.get("resume_hint", "") or "").strip()
        if not resume_hint and "blocked by autonomy policy" in f"{prompt} {summary}".lower():
            # This park came from a tool-approval timeout. The approval card
            # posted earlier stays pending and clickable indefinitely, so point
            # the user at it instead of leaving typed input as the only path.
            resume_hint = (
                "Tip: the tool-approval card above is still active — choose an option "
                "there (e.g. Approve) to grant the permission and resume this task "
                "automatically. Reply here only to give different instructions."
            )

        return {
            "checkpoint_type": "task_user_input",
            "checkpoint_id": cp.checkpoint_id,
            "task_id": task_id,
            **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
            "work_item_projection_title": work_item_projection_title,
            "summary": summary,
            "prompt": prompt,
            "questions": questions,
            "input_questions": input_questions,
            "required_fields": required_fields,
            "context_note": str(pause_request.get("context_note", "") or "").strip(),
            "resume_hint": resume_hint,
            "requesting_role_id": str(
                payload.get("requesting_role_id") or pause_request.get("requesting_role_id") or ""
            ).strip(),
            "requesting_task_id": str(
                payload.get("requesting_task_id") or pause_request.get("requesting_task_id") or ""
            ).strip(),
            "requesting_work_item_id": str(
                payload.get("requesting_work_item_id") or pause_request.get("requesting_work_item_id") or ""
            ).strip(),
            "seat_id": str(payload.get("seat_id") or pause_request.get("seat_id") or "").strip(),
            "runtime_session_id": str(payload.get("runtime_session_id", "") or "").strip(),
            "resume_cursor": payload.get("resume_cursor"),
            "active_subagents": list(payload.get("active_subagents", []) or []),
            "permission_requests": list(payload.get("permission_requests", []) or []),
            "worktree_path": str(payload.get("worktree_path", "") or "").strip(),
        }

    async def _handle_secretary_send(self, ws: Any, data: dict) -> None:
        """Handle user message in the secretary channel (no kanban task linkage)."""
        content = data.get("content", "")
        if not content:
            return

        run_engine, pid = await self._engine_for_request(data)

        # Ensure secretary channel exists
        await self.chat_store.ensure_secretary_channel(project_id=pid)
        secretary_channel = f"secretary:{pid}"

        # Lazily resolve or create a persistent secretary session
        secretary_session_id = self._secretary_session_ids.get(pid)
        if secretary_session_id is None:
            if getattr(run_engine, "secretary", None):
                sessions = await run_engine.secretary.list_sessions(
                    pid, limit=1,
                )
                if sessions:
                    secretary_session_id = sessions[0].session_id
            if secretary_session_id is None:
                import uuid as _uuid
                secretary_session_id = str(_uuid.uuid4())
            self._secretary_session_ids[pid] = secretary_session_id
            if self._active_engine_project_id() == pid:
                self._secretary_session_id = secretary_session_id

        # Insert user message to chat_store for UI rendering
        msg = await self.chat_store.insert_message(
            channel_id=secretary_channel,
            sender="user",
            sender_name="You",
            content=content,
            project_id=pid,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

        # Process via SecretaryService (shared with CLI `opc secretary`)
        self._track(self._process_secretary_message(content, engine=run_engine, project_id=pid, session_id=secretary_session_id))

    async def _process_secretary_message(
        self,
        content: str,
        *,
        engine: Any,
        project_id: str,
        session_id: str,
    ) -> None:
        """Call engine.process_secretary_message and write reply to secretary channel."""
        pid = project_id
        secretary_channel = f"secretary:{pid}"
        try:
            result = await engine.process_secretary_message(
                content,
                project_id=pid,
                session_id=session_id,
            )
            reply_text = result.get("response", "") if isinstance(result, dict) else str(result)

            # Show applied updates if any
            applied = result.get("applied_updates", []) if isinstance(result, dict) else []
            if applied:
                reply_text += "\n\n**Applied updates:**\n" + "\n".join(f"- {u}" for u in applied)

            msg = await self.chat_store.insert_message(
                channel_id=secretary_channel,
                sender="assistant",
                sender_name="Secretary",
                content=(reply_text or "No response."),
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})
        except Exception as e:
            logger.opt(exception=True).error(f"Secretary processing error: {e}")
            msg = await self.chat_store.insert_message(
                channel_id=secretary_channel,
                sender="system",
                sender_name="Secretary",
                content=f"Error: {e}",
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})
