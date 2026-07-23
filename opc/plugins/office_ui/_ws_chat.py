"""WsChatMixin — 聊天/進度/轉錄相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from opc.plugins.office_ui.snapshot_builder import (
    _normalize_transcript_detail_level,
    build_transcript_ui_messages,
    collapse_adjacent_transcript_duplicates,
)
from opc.plugins.office_ui._ws_utils import (
    _PERSISTED_WORKER_NOTIFICATION_KINDS,
    _add_execution_turn_aliases,
    _ui_message_identity_metadata,
)

if TYPE_CHECKING:
    from opc.plugins.office_ui.ws_handler import WSHandler


class WsChatMixin:
    """Mixin providing 聊天/進度/轉錄相關方法 for WSHandler."""

    async def _load_session_transcript_page(
        self,
        task: Any,
        *,
        limit: int,
        detail_level: str = "summary",
        before_timestamp: float | None = None,
        before_message_id: str | None = None,
        engine: Any | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Load a transcript page for the requested detail level."""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        if not self._store_is_ready(store):
            return [], 0, False

        task_id = str(getattr(task, "id", "") or "").strip()
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not task_id or not session_id:
            return [], 0, False

        channel_id = f"session:{task_id}"
        page_loader = getattr(store, "get_session_transcript_page", None)
        if callable(page_loader):
            normalized_limit = max(1, min(int(limit), 500))
            normalized_detail_level = _normalize_transcript_detail_level(detail_level)
            # A database-visible transcript row can still disappear when the
            # renderer finds no content, and adjacent result surfaces can
            # collapse to one UI row.  Page raw rows in bounded chunks until
            # we have one *rendered* look-ahead row or exhaust the transcript.
            # This makes has_more describe the UI timeline rather than the SQL
            # row set and prevents an empty raw page from stalling history.
            chunk_limit = normalized_limit
            raw_before_dt = (
                datetime.fromtimestamp(before_timestamp)
                if before_timestamp is not None
                else None
            )
            raw_before_id = before_message_id
            seen_raw_cursors: set[tuple[datetime, str]] = set()
            formatted_messages: list[dict[str, Any]] = []
            total_count = 0
            raw_has_more = False

            while True:
                raw_page = page_loader(
                    session_id,
                    limit=chunk_limit,
                    before_created_at=raw_before_dt,
                    before_message_id=raw_before_id,
                    detail_level=normalized_detail_level,
                )
                page = await raw_page if inspect.isawaitable(raw_page) else raw_page
                transcript_chunk = list((page or {}).get("messages", []) or [])
                total_count = max(
                    total_count,
                    int((page or {}).get("total_count", 0) or 0),
                )
                raw_has_more = bool((page or {}).get("has_more", False))
                if not transcript_chunk:
                    break

                formatted_chunk = build_transcript_ui_messages(
                    transcript_chunk,
                    channel_id=channel_id,
                    task_id=task_id,
                    detail_level=normalized_detail_level,
                )
                formatted_messages = collapse_adjacent_transcript_duplicates([
                    *formatted_chunk,
                    *formatted_messages,
                ])
                if len(formatted_messages) > normalized_limit:
                    return (
                        formatted_messages[-normalized_limit:],
                        max(total_count, len(formatted_messages)),
                        True,
                    )
                if not raw_has_more:
                    break

                oldest_message = transcript_chunk[0].get("message")
                oldest_created_at = getattr(oldest_message, "created_at", None)
                oldest_message_id = str(
                    getattr(oldest_message, "message_id", "") or ""
                ).strip()
                if not isinstance(oldest_created_at, datetime) or not oldest_message_id:
                    break
                next_cursor = (oldest_created_at, oldest_message_id)
                if next_cursor in seen_raw_cursors:
                    break
                seen_raw_cursors.add(next_cursor)
                raw_before_dt, raw_before_id = next_cursor
                # Small client pages should not require one SQL round-trip per
                # empty row. Start at the requested size so duplicate/empty
                # boundaries are exact, then grow only while looking through
                # rows which did not fill the rendered page.
                chunk_limit = min(500, max(chunk_limit + 1, chunk_limit * 2))

            return (
                formatted_messages[-normalized_limit:],
                max(total_count, len(formatted_messages)),
                raw_has_more,
            )

        transcript_loader = getattr(store, "get_session_transcript", None)
        if not callable(transcript_loader):
            return [], 0, False

        raw_transcript = transcript_loader(session_id)
        transcript = list(await raw_transcript if inspect.isawaitable(raw_transcript) else raw_transcript)
        formatted_messages = build_transcript_ui_messages(
            transcript,
            channel_id=channel_id,
            task_id=task_id,
            detail_level=_normalize_transcript_detail_level(detail_level),
        )

        total_count = len(formatted_messages)
        if before_timestamp is None:
            has_more = total_count > limit
            return formatted_messages[-limit:], total_count, has_more

        filtered_messages: list[dict[str, Any]] = []
        normalized_before_id = str(before_message_id or "").strip()
        for message in formatted_messages:
            created_at = float(message.get("timestamp") or 0)
            message_id = str(message.get("message_id", "") or "").strip()
            if created_at < before_timestamp:
                filtered_messages.append(message)
                continue
            if (
                normalized_before_id
                and created_at == before_timestamp
                and message_id < normalized_before_id
            ):
                filtered_messages.append(message)
        has_more = len(filtered_messages) > limit
        return filtered_messages[-limit:], total_count, has_more

    async def _handle_runtime_event_progress(
        self,
        payload: dict[str, Any],
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        runtime_engine = engine or self.engine
        pid = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        payload = self._enrich_runtime_progress_payload(payload, engine=runtime_engine)
        raw_task_id = str(payload.get("task_id", "") or "").strip()
        if not raw_task_id:
            return
        task_id = await self._ui_task_id_for_runtime_task_id(raw_task_id, engine=runtime_engine)
        if not task_id:
            return
        runtime_type = str(payload.get("type", "") or "").strip()
        entry = self._runtime_event_to_progress_entry(payload)
        if not entry:
            if runtime_type in {"turn_completed", "turn_failed", "checkpoint_saved"}:
                await self._sync_task_transcript_messages(task_id, engine=runtime_engine)
                if self._store_is_ready(runtime_engine.store):
                    task = await runtime_engine.store.get_task(raw_task_id)
                    if task is not None:
                        for parent_task_id in self._related_parent_task_ids(task):
                            await self._sync_task_transcript_messages(parent_task_id, engine=runtime_engine)
            return
        entry["timestamp"] = time.time()
        _add_execution_turn_aliases(entry, raw_task_id)
        # Buffer BEFORE broadcasting: broadcast awaits can interleave a
        # session_detail read, and any entry a client has already seen must be
        # visible in buffer∪DB or the snapshot will erase it from the live log.
        buf = self._progress_buffer.setdefault(task_id, [])
        buf.append(entry)
        self._progress_project_ids[task_id] = pid
        await self.broadcast({
            "type": "session_progress",
                "payload": {
                    "project_id": pid,
                    "task_id": task_id,
                    **_add_execution_turn_aliases({}, raw_task_id),
                "entry": entry,
            },
        })
        origin = self._active_runtime_children.get(raw_task_id) or (task_id if task_id != raw_task_id else None)
        if entry.get("is_company_runtime") and origin:
            await self.broadcast({
                "type": "work_item_progress",
                "payload": {
                    "project_id": pid,
                    "task_id": origin,
                    **_add_execution_turn_aliases({}, raw_task_id),
                    "entry": entry,
                },
            })
        # Re-read the buffer: a concurrent flush during the broadcast awaits
        # may have popped it, leaving `buf` as a stale detached list.
        if len(self._progress_buffer.get(task_id, [])) >= self._PROGRESS_FLUSH_THRESHOLD:
            await self._flush_progress(task_id, project_id=pid)
        if runtime_type in {"turn_completed", "turn_failed", "checkpoint_saved"}:
            await self._sync_task_transcript_messages(task_id, engine=runtime_engine)
            if self._store_is_ready(runtime_engine.store):
                task = await runtime_engine.store.get_task(raw_task_id)
                if task is not None:
                    for parent_task_id in self._related_parent_task_ids(task):
                        await self._sync_task_transcript_messages(parent_task_id, engine=runtime_engine)

    async def _handle_worker_notification(
        self,
        payload: dict[str, Any],
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        runtime_engine = engine or self.engine
        remapped = dict(payload or {})
        mapped_task_id = await self._ui_task_id_for_runtime_task_id(remapped.get("task_id"), engine=runtime_engine)
        if mapped_task_id:
            remapped["task_id"] = mapped_task_id
        remapped["project_id"] = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        await self.broadcast({
            "type": "worker_notification",
            "payload": remapped,
        })
        await self._persist_worker_notification_message(payload, engine=runtime_engine, project_id=project_id)

    async def _persist_worker_notification_message(
        self,
        payload: dict[str, Any],
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        runtime_engine = engine or self.engine
        notification_kind = str(payload.get("notification_kind", "") or "").strip()
        raw_task_id = str(payload.get("task_id", "") or "").strip()
        if notification_kind not in _PERSISTED_WORKER_NOTIFICATION_KINDS or not raw_task_id:
            return
        task_id = await self._ui_task_id_for_runtime_task_id(raw_task_id, engine=runtime_engine)
        if not task_id:
            return
        project_id = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        title = "Task Update"
        if self._store_is_ready(runtime_engine.store):
            try:
                task = await runtime_engine.store.get_task(raw_task_id)
            except Exception:
                task = None
            if task is not None:
                title = str(getattr(task, "title", "") or title).strip() or title
        await self.chat_store.create_session_channel(task_id, title, project_id=project_id)
        timestamp = float(payload.get("timestamp") or time.time())
        worker_id = str(payload.get("worker_id", "") or "").strip() or "worker"
        worker_type = str(payload.get("worker_type", "") or "").strip() or "worker"
        message_id = (
            f"worker-note:{task_id}:{worker_id}:{notification_kind}:{int(timestamp * 1000)}"
        )
        summary = str(payload.get("summary", "") or "").strip()
        worker_name = str(payload.get("name", "") or "").strip()
        sender_name = worker_name or worker_type.replace("_", " ").title()
        content = summary or f"{sender_name}: {notification_kind.replace('_', ' ')}"
        metadata = {
            **_ui_message_identity_metadata(
                kind="worker_notification",
                message_id=message_id,
                created_at=timestamp,
            ),
            "source": "runtime_event",
            "role": "system",
            "worker_id": worker_id,
            "worker_type": worker_type,
            "notification_kind": notification_kind,
            "resident_status": payload.get("resident_status"),
        }
        message = await self.chat_store.insert_message(
            channel_id=f"session:{task_id}",
            sender="system",
            sender_name=sender_name,
            content=content,
            metadata=metadata,
            message_id=message_id,
            project_id=project_id,
        )
        await self.broadcast({
            "type": "session_message",
            "payload": message,
        })

    def _remember_pending_escalation(self, payload: dict[str, Any]) -> dict[str, Any]:
        escalation_id = str(payload.get("escalation_id") or f"esc_{uuid.uuid4()}")
        raw_project_id = str(payload.get("project_id") or "").strip()
        project_id = self._normalize_project_id(raw_project_id) if raw_project_id else ""
        approval_group_key = str(payload.get("approval_group_key") or "").strip() or self._approval_group_key(
            str(payload.get("message") or "")
        )
        existing = self._pending_escalations.get(escalation_id)
        if existing is not None:
            future = existing.get("future")
            if future is None or future.done():
                future = asyncio.get_running_loop().create_future()
            record = {
                **existing,
                **payload,
                "future": future,
                "escalation_id": escalation_id,
                "approval_group_key": approval_group_key,
            }
            if project_id:
                record["project_id"] = project_id
            self._pending_escalations[escalation_id] = record
            if escalation_id in self._pending_escalation_order:
                self._pending_escalation_order = [
                    item for item in self._pending_escalation_order
                    if item != escalation_id
                ]
            self._pending_escalation_order.append(escalation_id)
            return record

        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        record = {
            **payload,
            "future": future,
            "escalation_id": escalation_id,
            "approval_group_key": approval_group_key,
        }
        if project_id:
            record["project_id"] = project_id
        self._pending_escalations[escalation_id] = record
        self._pending_escalation_order.append(escalation_id)
        return record

    async def _mirror_agent_message(
        self,
        event: Any,
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        """Mirror agent_message_sent events into session channel or activity."""
        runtime_engine = engine or self.engine
        pid = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        p = event.payload or {}
        from_role = p.get("from", "")
        content = p.get("subject", "") or p.get("body", "") or "Message"
        task_id = p.get("task_id")

        # Resolve opc_role_id → agent_id for consistent sender identity
        from_agent = self.event_adapter._resolve_role_to_agent(from_role) if from_role else ""

        # Resolve human-readable name: agent store name → org role name → raw ID
        display_name = from_role or from_agent
        if from_agent:
            agents = await self.agent_store.get_all()
            match = next((a for a in agents if a["agent_id"] == from_agent), None)
            if match:
                display_name = match["name"]
        org_engine = getattr(runtime_engine, "org_engine", None)
        if display_name == from_role and org_engine:
            org_role = org_engine.get_agent(from_role)
            if org_role:
                display_name = org_role.name

        # Route to session channel if task_id is known, else activity
        target_channel = f"session:{task_id}" if task_id else f"activity:{pid}"
        mirror_meta: dict[str, Any] = {}
        if task_id:
            mirror_meta["task_id"] = task_id
        msg = await self.chat_store.insert_message(
            channel_id=target_channel,
            sender=from_agent,
            sender_name=display_name,
            content=content,
            metadata=mirror_meta or None,
            project_id=pid,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

    async def _mirror_escalation(
        self,
        event: Any,
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        """Mirror escalation_created events into session channel or activity."""
        runtime_engine = engine or self.engine
        pid = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        p = event.payload or {}
        message = p.get("message", "Escalation required")
        source_task_id = str(p.get("task_id") or "").strip() or None
        source_task = None
        if source_task_id and getattr(runtime_engine, "store", None):
            getter = getattr(runtime_engine.store, "get_task", None)
            if callable(getter):
                try:
                    source_task = await getter(source_task_id)
                except Exception:
                    source_task = None
        source_metadata = dict(getattr(source_task, "metadata", {}) or {}) if source_task is not None else {}
        is_task_mode = self._runtime_payload_is_task_mode(source_metadata)
        current_turn_title = str(
            source_metadata.get("original_message")
            or getattr(source_task, "description", "")
            or ""
        ).strip()
        display_message = self._task_mode_permission_prompt(message, current_turn_title) if is_task_mode else message
        session_task_id = await self._resolve_escalation_session_task_id(source_task_id, engine=runtime_engine)
        target_channel = f"session:{session_task_id}" if session_task_id else f"activity:{pid}"
        options = p.get("options", []) or []
        esc_record = self._remember_pending_escalation({
            "escalation_id": str(p.get("escalation_id") or ""),
            "project_id": pid,
            "task_id": session_task_id,
            "source_task_id": source_task_id,
            "message": message,
            "display_message": display_message,
            "options": options,
            "default_action": p.get("default_action"),
            "escalation_type": p.get("type", "decision_needed"),
            "approval_group_key": p.get("approval_group_key") or self._approval_group_key(message),
        })
        esc_meta: dict[str, Any] = {
            "checkpoint_type": "human_escalation",
            "checkpoint_id": esc_record.get("escalation_id"),
            "escalation_id": esc_record.get("escalation_id"),
            "escalation_type": esc_record.get("escalation_type"),
            "prompt": display_message,
            "summary": display_message,
            "options": options,
            "default_action": esc_record.get("default_action"),
            "source": "engine",
            "ui_message_id": f"escalation::{esc_record.get('escalation_id')}",
            "project_id": pid,
            "approval_group_key": esc_record.get("approval_group_key"),
        }
        approval_context = dict(p.get("approval_context") or {})
        if approval_context:
            # Persisted with the card so a click AFTER the inline wait expired
            # (or after a restart) can still apply the same allowlist grant and
            # resume the parked task.
            esc_meta["approval_context"] = approval_context
        if is_task_mode:
            esc_meta["execution_mode"] = "task_mode"
            esc_meta["permission_group_key"] = esc_record.get("approval_group_key")
            esc_meta["current_turn_title"] = current_turn_title
        if session_task_id:
            esc_meta["task_id"] = session_task_id
        if source_task_id and source_task_id != session_task_id:
            esc_meta["source_task_id"] = source_task_id
        msg = await self.chat_store.insert_message(
            channel_id=target_channel,
            sender="assistant",
            sender_name="OPC",
            content=display_message,
            metadata=esc_meta or None,
            message_id=f"escalation::{esc_record.get('escalation_id')}",
            project_id=pid,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

    async def _recent_identical_helper_exists(
        self,
        channel_id: str,
        content: str,
        *,
        project_id: str,
        window_seconds: float = 120.0,
        scan_limit: int = 10,
    ) -> bool:
        """True when an identical assistant helper was posted very recently.

        Used to collapse rapid duplicate user clicks into a single helper
        reply instead of one warning per click.
        """
        try:
            recent = await self.chat_store.get_channel_messages(
                channel_id, limit=scan_limit, project_id=project_id,
            )
        except Exception:
            return False
        now = time.time()
        for item in reversed(recent):
            if str(item.get("sender", "")) != "assistant":
                continue
            if str(item.get("content", "")) != content:
                continue
            try:
                created_at = float(item.get("created_at", 0) or 0)
            except (TypeError, ValueError):
                continue
            if now - created_at <= window_seconds:
                return True
        return False

    async def _find_pending_approval_park_checkpoint(
        self,
        engine: Any,
        task_id: str,
        project_id: str,
    ) -> Any | None:
        """Locate the pending checkpoint a tool-approval timeout parked on.

        When an approval card's inline wait expires, the blocked runtime task
        returns AWAITING_HUMAN and the engine saves a durable pause checkpoint
        (task mode: ``task_user_input``; company mode: ``company_work_item_gate``).
        A later click on the card resumes execution through that checkpoint.
        """
        source_task_id = str(task_id or "").strip()
        if not source_task_id:
            return None
        store = getattr(engine, "store", None)
        getter = getattr(store, "get_pending_checkpoints", None)
        if not callable(getter):
            return None
        try:
            pending = await getter(project_id=project_id)
        except Exception:
            logger.opt(exception=True).debug(
                "Failed to load pending checkpoints for deferred escalation resume"
            )
            return None
        candidates = []
        for checkpoint in pending or []:
            if str(getattr(checkpoint, "checkpoint_type", "") or "") not in {
                "task_user_input",
                "company_work_item_gate",
            }:
                continue
            payload = dict(getattr(checkpoint, "payload", {}) or {})
            linked_ids = {
                str(payload.get("task_id") or "").strip(),
                str(payload.get("waiting_task_id") or "").strip(),
                str(getattr(checkpoint, "task_id", "") or "").strip(),
            }
            linked_ids.update(str(item or "").strip() for item in list(payload.get("task_ids", []) or []))
            if source_task_id in linked_ids:
                candidates.append(checkpoint)
        if not candidates:
            return None

        def _checkpoint_timestamp(checkpoint: Any) -> float:
            created = getattr(checkpoint, "created_at", None)
            try:
                return float(created.timestamp())
            except (AttributeError, TypeError, ValueError, OSError):
                return 0.0

        return max(candidates, key=_checkpoint_timestamp)

    async def _resolve_deferred_escalation_click(
        self,
        *,
        engine: Any,
        project_id: str,
        channel_id: str,
        checkpoint_id: str,
        card_meta: dict[str, Any],
        option_id: str,
    ) -> dict[str, Any]:
        """Apply a decision clicked on an approval card whose inline wait has
        expired: persist the allowlist grant, resolve the card, and hand back
        either a flow-through rewrite (resume the parked task through the
        normal message pipeline) or a helper reply when nothing is parked."""
        approval_context = dict(card_meta.get("approval_context") or {})
        summary: dict[str, Any] = {
            "approved": option_id in {"approve_once", "approve_session", "always_project", "always_global"},
            "scope": None,
        }
        approval_engine = getattr(engine, "approval_engine", None)
        apply_decision = getattr(approval_engine, "apply_deferred_escalation_decision", None)
        if callable(apply_decision):
            try:
                summary = apply_decision(option_id, approval_context)
            except Exception:
                logger.opt(exception=True).warning(
                    "Deferred approval grant failed; resuming the parked task without a new allowlist entry"
                )
        await self._mark_human_escalation_checkpoint_status(
            checkpoint_id,
            status="resolved",
            project_id=project_id,
            channel_id=channel_id,
            reply=option_id,
            reason="deferred_decision",
        )

        source_task_id = str(
            card_meta.get("source_task_id") or card_meta.get("task_id") or ""
        ).strip()
        park_checkpoint = await self._find_pending_approval_park_checkpoint(
            engine, source_task_id, project_id
        )
        action_name = str(approval_context.get("action_name", "") or "").strip() or "action"
        approved = bool(summary.get("approved"))
        scope = str(summary.get("scope") or "").strip()
        if park_checkpoint is None:
            return {
                "action": "reply",
                "text": (
                    f"Decision `{option_id}` recorded"
                    + (f"; allowlist updated ({scope})" if approved and scope else "")
                    + ". No parked task is currently waiting on this approval — if the runtime "
                    "is still transitioning, the grant applies on its next attempt."
                ),
            }
        if approved:
            scope_note = f" (allowlisted: {scope})" if scope else ""
            crafted = (
                f"Approval decision: {option_id}. The previously blocked `{action_name}` action "
                f"is now permitted{scope_note}. Re-run it and continue the task."
            )
        else:
            crafted = (
                f"Approval decision: deny. Do not run the blocked `{action_name}` action; "
                "choose an alternative approach or report the limitation to your manager."
            )
        return {
            "action": "flow_through",
            "content": crafted,
            "reply_metadata": {
                "response_to_checkpoint_id": str(getattr(park_checkpoint, "checkpoint_id", "") or ""),
                "response_to_checkpoint_type": str(getattr(park_checkpoint, "checkpoint_type", "") or ""),
            },
        }

    async def _mark_human_escalation_checkpoint_status(
        self,
        escalation_id: str,
        *,
        status: str,
        project_id: str,
        channel_id: str | None = None,
        reply: str | None = None,
        default_action: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_escalation_id = str(escalation_id or "").strip()
        if not normalized_escalation_id:
            return None
        update_status = getattr(self.chat_store, "update_checkpoint_status", None)
        if not callable(update_status):
            return None
        status_metadata: dict[str, Any] = {
            "checkpoint_resolution_source": "escalation_lifecycle",
        }
        if reply is not None:
            status_metadata["checkpoint_resolution_reply"] = reply
        if default_action is not None:
            status_metadata["checkpoint_timeout_default_action"] = default_action
        if reason:
            status_metadata["checkpoint_resolution_reason"] = reason
        try:
            updated = await update_status(
                normalized_escalation_id,
                channel_id=channel_id,
                checkpoint_type="human_escalation",
                status=status,
                status_metadata=status_metadata,
                project_id=project_id,
            )
        except Exception:
            logger.opt(exception=True).debug(
                f"Failed to update human escalation checkpoint status for {normalized_escalation_id}",
            )
            return None
        if updated is not None:
            await self.broadcast({"type": "session_message", "payload": updated})
        return updated

    async def _mark_escalation_event_checkpoint_terminal(
        self,
        event: Any,
        *,
        project_id: str,
    ) -> None:
        payload = dict(getattr(event, "payload", {}) or {})
        escalation_id = str(payload.get("escalation_id", "") or "").strip()
        if not escalation_id:
            return
        if event.event_type == "escalation_timeout":
            default_action = str(payload.get("default_action", "") or "").strip() or None
            if default_action is None:
                # No default was applied on timeout — the decision is still the
                # user's to make. The task parks on AWAITING_HUMAN and the card
                # stays pending; clicking it later applies the decision and
                # resumes the parked task (deferred approval path).
                return
            await self._mark_human_escalation_checkpoint_status(
                escalation_id,
                status="timeout",
                project_id=project_id,
                default_action=default_action,
                reason="timeout",
            )
            return
        if event.event_type == "escalation_resolved":
            await self._mark_human_escalation_checkpoint_status(
                escalation_id,
                status="resolved",
                project_id=project_id,
                reply=str(payload.get("reply", "") or "").strip() or None,
                reason="resolved",
            )

    async def _reconcile_inactive_human_escalation_cards(
        self,
        channel_id: str,
        *,
        task_id: str,
        project_id: str,
    ) -> list[dict[str, Any]]:
        """Mark legacy human escalation cards stale when no live approval exists.

        Older Office UI builds persisted approval cards without a terminal
        ``checkpoint_status`` when the runtime timed out or auto-approved. On a
        later reload there is no in-memory escalation future for those cards, so
        session detail is the authoritative place to reconcile persisted UI
        state with runtime state.
        """
        getter = getattr(self.chat_store, "get_unresolved_checkpoint_messages", None)
        if not callable(getter):
            return []
        try:
            cards = await getter(
                channel_id,
                checkpoint_type="human_escalation",
                project_id=project_id,
            )
        except Exception:
            logger.opt(exception=True).debug(
                f"Failed to load unresolved human escalation cards for {channel_id}",
            )
            return []

        updated_cards: list[dict[str, Any]] = []
        for card in cards:
            metadata = dict(card.get("metadata", {}) or {})
            escalation_id = str(
                metadata.get("escalation_id")
                or metadata.get("checkpoint_id")
                or ""
            ).strip()
            if not escalation_id:
                continue
            if self._find_pending_escalation(
                task_id=task_id,
                escalation_id=escalation_id,
                project_id=project_id,
            ):
                continue
            if isinstance(metadata.get("approval_context"), dict) and metadata.get("approval_context"):
                # Deferred-capable approval card: it stays answerable after the
                # inline wait expired or across restarts, so a missing pending
                # future does NOT make it stale.
                continue
            updated = await self._mark_human_escalation_checkpoint_status(
                escalation_id,
                status="stale",
                project_id=project_id,
                channel_id=channel_id,
                reason="session_detail_reconcile_inactive_escalation",
            )
            if updated is not None:
                updated_cards.append(updated)
        return updated_cards

    async def _reconcile_execution_checkpoint_cards(
        self,
        channel_id: str,
        *,
        project_id: str,
        engine: Any,
    ) -> list[dict[str, Any]]:
        """Mark non-human checkpoint cards terminal once the engine checkpoint is terminal."""
        getter = getattr(self.chat_store, "get_unresolved_checkpoint_messages", None)
        if not callable(getter):
            return []
        try:
            cards = await getter(channel_id, project_id=project_id)
        except Exception:
            logger.opt(exception=True).debug(
                f"Failed to load unresolved execution checkpoint cards for {channel_id}",
            )
            return []

        updated_cards: list[dict[str, Any]] = []
        for card in cards:
            metadata = dict(card.get("metadata", {}) or {})
            checkpoint_type = str(metadata.get("checkpoint_type", "") or "").strip()
            if checkpoint_type in {"", "human_escalation"}:
                continue
            checkpoint_id = str(metadata.get("checkpoint_id", "") or "").strip()
            if not checkpoint_id:
                continue
            status = await self._execution_checkpoint_status(
                engine=engine,
                project_id=project_id,
                checkpoint_id=checkpoint_id,
                checkpoint_type=checkpoint_type,
            )
            if not status or status == "pending":
                continue
            try:
                updated = await self.chat_store.update_checkpoint_status(
                    checkpoint_id,
                    channel_id=channel_id,
                    checkpoint_type=checkpoint_type,
                    status=status,
                    status_metadata={
                        "checkpoint_resolution_source": "execution_checkpoint_lifecycle",
                    },
                    project_id=project_id,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    f"Failed to reconcile execution checkpoint card {checkpoint_id}",
                )
                continue
            if updated is not None:
                updated_cards.append(updated)
                await self.broadcast({"type": "session_message", "payload": updated})
        return updated_cards
