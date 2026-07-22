"""WsCompanyMixin — 公司運行時/委派/kanban 相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opc.plugins.office_ui.ws_handler import WSHandler


class WsCompanyMixin:
    """Mixin providing 公司運行時/委派/kanban 相關方法 for WSHandler."""

    async def _resolve_company_runtime_target(
        self,
        task_id: str,
        *,
        engine: Any | None = None,
        runtime_session_id: str = "",
        checkpoint_id: str = "",
    ) -> dict[str, Any] | None:
        """Resolve a Task/UI channel to the canonical session-first scope."""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        if not task_id or not self._store_is_ready(store):
            return None
        task = await store.get_task(task_id)
        if task is None:
            return None
        project_id = self._normalize_project_id(
            getattr(task, "project_id", None) or getattr(runtime_engine, "project_id", None)
        )
        identity_index = await load_company_runtime_identity_index(store, project_id)
        identity = identity_index.resolve(
            task_id=task_id,
            runtime_session_id=runtime_session_id,
            checkpoint_id=checkpoint_id,
        )
        if identity is None:
            return None
        ui_anchor_task_id = identity.ui_anchor_task_id
        config_task = identity_index.task(identity.config_source_task_id) or task

        return {
            "task": task,
            "engine": runtime_engine,
            "identity": identity,
            "runtime_session_id": identity.runtime_session_id,
            "ui_channel_task_id": task_id,
            "ui_anchor_task_id": ui_anchor_task_id,
            "config_source_task_id": identity.config_source_task_id,
            "config_task": config_task,
            "checkpoint": identity.checkpoint,
            "origin_task_id": ui_anchor_task_id,
            "affected_task_ids": list(identity.runtime_task_ids),
        }

    async def _broadcast_company_runtime_control(
        self,
        target: dict[str, Any],
        *,
        state: str,
        checkpoint_id: str = "",
        stop_intent_id: str = "",
    ) -> None:
        affected_task_ids = [
            str(item).strip()
            for item in list(target.get("affected_task_ids", []) or [])
            if str(item).strip()
        ]
        runtime_engine = target.get("engine") or self.engine
        project_id = self._normalize_project_id(getattr(runtime_engine, "project_id", None))
        payload = {
            "project_id": project_id,
            "runtime_control_state": state,
            "can_stop": state == "running",
            "can_resume": state == "suspended",
            "resume_parent_session_id": str(target.get("runtime_session_id", "") or "").strip(),
            "pending_runtime_checkpoint_id": checkpoint_id,
            "stop_intent_id": stop_intent_id,
            "task_ids": affected_task_ids,
        }
        await self.broadcast({"type": "session_runtime_control", "payload": payload})

    async def _set_company_runtime_control(
        self,
        target: dict[str, Any] | None,
        *,
        state: str,
        checkpoint_id: str = "",
        stop_intent_id: str = "",
    ) -> None:
        if target is None:
            return
        await self._broadcast_company_runtime_control(
            target,
            state=state,
            checkpoint_id=checkpoint_id,
            stop_intent_id=stop_intent_id,
        )

    async def _finalize_company_runtime_stop(self, target: dict[str, Any], *, stop_intent_id: str) -> None:
        runtime_engine = target.get("engine") or self.engine
        parent_session_id = str(target.get("runtime_session_id", "") or "").strip()
        origin_task_id = str(target.get("origin_task_id", "") or "").strip()
        affected_task_ids = [
            str(item).strip()
            for item in list(target.get("affected_task_ids", []) or [])
            if str(item).strip()
        ]
        suspended: dict[str, Any] | None = None
        try:
            suspended = await runtime_engine.suspend_company_runtime(
                origin_task_id=origin_task_id,
                session_id=parent_session_id or None,
                reason="user_stop",
                checkpoint_type="company_runtime_suspended",
                stop_intent_id=stop_intent_id,
            )
        except Exception:
            logger.opt(exception=True).warning(f"suspend_company_runtime failed for {origin_task_id}")

        if suspended is not None:
            for candidate in list(suspended.get("task_ids", []) or []):
                candidate_id = str(candidate or "").strip()
                if candidate_id and candidate_id not in affected_task_ids:
                    affected_task_ids.append(candidate_id)
            target["affected_task_ids"] = affected_task_ids
            self._stop_requested_task_ids.update(affected_task_ids)
            for tid in affected_task_ids:
                self._progress_buffer.pop(tid, None)
                self._progress_project_ids.pop(tid, None)
                self._cancel_session_tasks(tid)
            try:
                await self._set_company_runtime_control(
                    target,
                    state="suspended",
                    checkpoint_id=str(suspended.get("checkpoint_id", "") or ""),
                    stop_intent_id=stop_intent_id,
                )
            except Exception:
                logger.opt(exception=True).debug("failed to broadcast company runtime suspended state")
            channel_id = f"session:{target.get('ui_channel_task_id', '')}"
            pid = self._normalize_project_id(getattr(runtime_engine, "project_id", None))
            try:
                msg = await self.chat_store.insert_message(
                    channel_id=channel_id,
                    sender="system",
                    sender_name="System",
                    content="Company runtime suspended by user",
                    project_id=pid,
                    metadata={
                        "type": "system",
                        "stop_reason": "user_stop",
                        "checkpoint_type": suspended.get("checkpoint_type"),
                        "checkpoint_id": suspended.get("checkpoint_id"),
                        "stop_intent_id": stop_intent_id,
                    },
                )
                await self.broadcast({"type": "session_message", "payload": msg})
            except Exception:
                logger.opt(exception=True).warning(f"Failed to insert suspend message for {origin_task_id}")
        else:
            try:
                await self._set_company_runtime_control(
                    target,
                    state="running" if affected_task_ids else "idle",
                    stop_intent_id=stop_intent_id,
                )
            except Exception:
                pass
        self._company_stop_intents.pop(parent_session_id, None)
        self._company_stop_finalize_tasks.pop(parent_session_id, None)

    async def _company_delivery_feedback_parent_target(
        self,
        *,
        task_id: str,
        waiting_task_id: str,
        waiting_task: Any | None,
        checkpoint: Any,
        payload: dict[str, Any],
        engine: Any,
    ) -> dict[str, str]:
        expected_runtime_session_id = str(
            payload.get("parent_session_id")
            or getattr(waiting_task, "parent_session_id", "")
            or getattr(checkpoint, "session_id", "")
            or ""
        ).strip()

        for candidate_task_id in (waiting_task_id, task_id):
            if not candidate_task_id:
                continue
            try:
                target = await self._resolve_company_runtime_target(candidate_task_id, engine=engine)
            except Exception:
                logger.opt(exception=True).debug("failed to resolve company runtime target for delivery feedback")
                target = None
            if not target:
                continue
            runtime_session_id = str(target.get("runtime_session_id", "") or "").strip()
            if expected_runtime_session_id and runtime_session_id != expected_runtime_session_id:
                continue
            ui_anchor_task_id = str(target.get("ui_anchor_task_id", "") or "").strip()
            if runtime_session_id and ui_anchor_task_id:
                return {
                    "parent_task_id": ui_anchor_task_id,
                    "parent_session_id": runtime_session_id,
                }

        # Delivery feedback is a company checkpoint action, not a generic
        # chat operation.  Ambiguous/missing durable identity must be rejected
        # instead of selecting the first Task sharing the root session.
        return {"parent_task_id": "", "parent_session_id": ""}

    async def _delivery_feedback_checkpoint_visible_to_session(
        self,
        checkpoint: Any,
        *,
        task_id: str,
        session_id: str | None,
        engine: Any,
    ) -> bool:
        requested_session_id = str(session_id or "").strip()
        if not requested_session_id:
            return False

        payload = dict(getattr(checkpoint, "payload", {}) or {})
        review_level = str(payload.get("review_level", "") or "").strip().lower()
        if review_level == "manager":
            return False

        checker = getattr(engine, "_checkpoint_visible_to_reply_session", None)
        if callable(checker):
            try:
                maybe_visible = checker(checkpoint, requested_session_id)
                visible = await maybe_visible if inspect.isawaitable(maybe_visible) else maybe_visible
                if visible is True:
                    return True
            except Exception:
                logger.opt(exception=True).debug("failed to evaluate delivery feedback checkpoint visibility")

        checkpoint_session_id = str(getattr(checkpoint, "session_id", "") or "").strip()
        if checkpoint_session_id == requested_session_id:
            return True

        for key in ("parent_session_id", "runtime_session_id", "origin_session_id"):
            if str(payload.get(key, "") or "").strip() == requested_session_id:
                return True

        normalized_task_id = str(task_id or "").strip()
        raw_task_ids = payload.get("task_ids", [])
        task_ids = {
            str(item or "").strip()
            for item in (raw_task_ids if isinstance(raw_task_ids, list) else [])
            if str(item or "").strip()
        }
        payload_task_id = str(payload.get("task_id", "") or "").strip()
        waiting_task_id = str(payload.get("waiting_task_id", "") or payload_task_id or "").strip()
        if normalized_task_id and normalized_task_id in ({payload_task_id, waiting_task_id} | task_ids):
            return True

        store = getattr(engine, "store", None)
        get_task = getattr(store, "get_task", None) if self._store_is_ready(store) else None
        if callable(get_task) and waiting_task_id:
            try:
                waiting_task = await get_task(waiting_task_id)
            except Exception:
                logger.opt(exception=True).debug("failed to load waiting task for delivery feedback visibility")
                waiting_task = None
            if waiting_task is not None:
                waiting_session_id = str(getattr(waiting_task, "session_id", "") or "").strip()
                parent_session_id = str(getattr(waiting_task, "parent_session_id", "") or "").strip()
                if requested_session_id in {waiting_session_id, parent_session_id}:
                    return True
                if normalized_task_id and normalized_task_id == str(getattr(waiting_task, "id", "") or "").strip():
                    return True

        return False

    @staticmethod
    def _checkpoint_created_timestamp(checkpoint: Any | None) -> float | None:
        raw_value = getattr(checkpoint, "created_at", None) if checkpoint is not None else None
        if raw_value is None:
            return None
        if isinstance(raw_value, (int, float)):
            return float(raw_value)
        if isinstance(raw_value, datetime):
            return raw_value.timestamp()
        if isinstance(raw_value, str):
            try:
                return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
        return None

    def _delivery_feedback_checkpoint_channel_id(
        self,
        checkpoint: Any | None,
        *,
        fallback_channel_id: str | None = None,
    ) -> str:
        normalized_fallback = str(fallback_channel_id or "").strip()
        if normalized_fallback:
            return normalized_fallback
        payload = dict(getattr(checkpoint, "payload", {}) or {}) if checkpoint is not None else {}
        for key in ("parent_task_id", "ui_task_id", "origin_task_id", "task_id", "waiting_task_id"):
            task_id = str(payload.get(key, "") or "").strip()
            if task_id:
                return f"session:{task_id}"
        checkpoint_task_id = str(getattr(checkpoint, "task_id", "") or "").strip() if checkpoint is not None else ""
        return f"session:{checkpoint_task_id}" if checkpoint_task_id else ""

    async def _update_or_emit_checkpoint_card_status(
        self,
        checkpoint_id: str,
        *,
        checkpoint_type: str,
        status: str,
        project_id: str,
        channel_id: str | None = None,
        checkpoint: Any | None = None,
        response_message_id: str | None = None,
        response_metadata: dict[str, Any] | None = None,
        status_metadata: dict[str, Any] | None = None,
        broadcast_update: bool = True,
    ) -> dict[str, Any] | None:
        """Update a persisted checkpoint card, or persist a terminal synthetic card.

        Snapshot-built delivery review cards can be synthetic-only. In that case
        there is no chat_store row for update_checkpoint_status() to mutate, so
        the client keeps the old pending card in the floating Pending Actions
        section. Persisting the terminal synthetic card with the same checkpoint
        identity lets the frontend merge it and move it back into the timeline.
        """
        normalized_checkpoint_id = str(checkpoint_id or "").strip()
        normalized_checkpoint_type = str(checkpoint_type or "").strip()
        normalized_status = str(status or "resolved").strip().lower() or "resolved"
        normalized_project_id = self._normalize_project_id(project_id)
        if not normalized_checkpoint_id or not normalized_checkpoint_type:
            return None

        updated = await self.chat_store.update_checkpoint_status(
            normalized_checkpoint_id,
            channel_id=channel_id,
            checkpoint_type=normalized_checkpoint_type,
            status=normalized_status,
            response_message_id=response_message_id,
            response_metadata=response_metadata,
            status_metadata=status_metadata,
            project_id=normalized_project_id,
        )
        if updated is not None:
            if broadcast_update:
                await self.broadcast({"type": "session_message", "payload": updated})
            return updated

        if normalized_checkpoint_type != "company_delivery_feedback":
            return None

        fallback_channel_id = self._delivery_feedback_checkpoint_channel_id(
            checkpoint,
            fallback_channel_id=channel_id,
        )
        if not fallback_channel_id:
            return None

        if checkpoint is not None:
            base_meta = self._build_delivery_feedback_meta(checkpoint)
        else:
            base_meta = {
                "checkpoint_type": normalized_checkpoint_type,
                "checkpoint_id": normalized_checkpoint_id,
                "summary": "Human review requested.",
                "prompt": "Human review requested.",
            }

        now = time.time()
        metadata = dict(base_meta)
        metadata["checkpoint_type"] = normalized_checkpoint_type
        metadata["checkpoint_id"] = normalized_checkpoint_id
        metadata["checkpoint_status"] = normalized_status
        if normalized_status == "responded":
            metadata["checkpoint_responded_at"] = now
        else:
            metadata["checkpoint_resolved_at"] = now
        if response_message_id:
            metadata["checkpoint_response_message_id"] = response_message_id
        if isinstance(status_metadata, dict):
            for key, value in status_metadata.items():
                metadata[str(key)] = value
        if isinstance(response_metadata, dict):
            raw_reply_kind = str(response_metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
            if raw_reply_kind in {"approve", "deny", "feedback", "ignore"}:
                metadata["checkpoint_reply_kind"] = raw_reply_kind

        content = str(
            metadata.get("prompt")
            or metadata.get("summary")
            or "Human review requested."
        ).strip() or "Human review requested."
        sender_name = str(
            metadata.get("work_item_projection_title")
            or metadata.get("requesting_role_id")
            or "Company Member"
        ).strip() or "Company Member"
        message_id = f"checkpoint::{normalized_checkpoint_id}"
        try:
            created = await self.chat_store.insert_message(
                channel_id=fallback_channel_id,
                sender="assistant",
                sender_name=sender_name,
                content=content,
                metadata=metadata,
                message_id=message_id,
                project_id=normalized_project_id,
                created_at=self._checkpoint_created_timestamp(checkpoint),
            )
        except Exception:
            logger.opt(exception=True).debug(
                "failed to insert terminal synthetic checkpoint card; retrying status update: checkpoint_id={}",
                normalized_checkpoint_id,
            )
            updated = await self.chat_store.update_checkpoint_status(
                normalized_checkpoint_id,
                channel_id=None,
                checkpoint_type=normalized_checkpoint_type,
                status=normalized_status,
                response_message_id=response_message_id,
                response_metadata=response_metadata,
                status_metadata=status_metadata,
                project_id=normalized_project_id,
            )
            if updated is not None:
                if broadcast_update:
                    await self.broadcast({"type": "session_message", "payload": updated})
                return updated
            return None
        if broadcast_update:
            await self.broadcast({"type": "session_message", "payload": created})
        return created

    async def _supersede_pending_delivery_feedback_for_new_company_turn(
        self,
        *,
        task_id: str,
        session_id: str | None,
        run_engine: Any,
        run_project_id: str,
    ) -> list[str]:
        project_id = self._normalize_project_id(run_project_id or getattr(run_engine, "project_id", None))
        store = getattr(run_engine, "store", None)
        if not session_id or not self._store_is_ready(store):
            return []
        getter = getattr(store, "get_pending_checkpoints", None)
        resolver = getattr(store, "resolve_execution_checkpoint", None)
        if not callable(getter) or not callable(resolver):
            return []
        try:
            checkpoints = await getter(
                project_id=project_id,
                checkpoint_types=["company_delivery_feedback"],
            )
        except Exception:
            logger.opt(exception=True).debug("failed to list pending delivery feedback checkpoints")
            return []

        superseded_ids: list[str] = []
        for checkpoint in list(checkpoints or []):
            checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
            if (
                not checkpoint_id
                or str(getattr(checkpoint, "checkpoint_type", "") or "").strip() != "company_delivery_feedback"
                or str(getattr(checkpoint, "status", "") or "").strip().lower() != "pending"
            ):
                continue
            visible = await self._delivery_feedback_checkpoint_visible_to_session(
                checkpoint,
                task_id=task_id,
                session_id=session_id,
                engine=run_engine,
            )
            if not visible:
                continue

            try:
                terminalizer = getattr(run_engine, "_terminalize_company_delivery_feedback_checkpoint", None)
                if inspect.iscoroutinefunction(terminalizer):
                    superseded_at = datetime.now().isoformat()
                    await terminalizer(
                        checkpoint,
                        status="superseded",
                        resolution="superseded_by_new_company_turn",
                        payload_updates={
                            "feedback_superseded": True,
                            "feedback_superseded_at": superseded_at,
                            "feedback_resolution": "superseded_by_new_company_turn",
                        },
                        task_metadata_updates={
                            "feedback_superseded": True,
                            "feedback_superseded_at": superseded_at,
                        },
                    )
                else:
                    await resolver(checkpoint_id, status="superseded")
                    waiting_task_id = str(
                        dict(getattr(checkpoint, "payload", {}) or {}).get("waiting_task_id")
                        or getattr(checkpoint, "task_id", "")
                        or ""
                    ).strip()
                    if waiting_task_id and hasattr(store, "get_task") and hasattr(store, "save_task"):
                        from opc.core.models import TaskStatus
                        waiting_task = await store.get_task(waiting_task_id)
                        if waiting_task is not None:
                            superseded_at = datetime.now().isoformat()
                            waiting_task.metadata = dict(getattr(waiting_task, "metadata", {}) or {})
                            waiting_task.metadata.update({
                                "requires_user_feedback": False,
                                "human_review_closed": True,
                                "human_review_closed_at": superseded_at,
                                "human_review_resolution": "superseded_by_new_company_turn",
                                "feedback_closed": True,
                                "feedback_resolved": True,
                                "feedback_superseded": True,
                                "feedback_superseded_at": superseded_at,
                                "feedback_resolution": "superseded_by_new_company_turn",
                            })
                            await apply_task_status_transition(
                                store,
                                waiting_task,
                                target_status_or_phase=TaskStatus.DONE,
                                reason="superseded_by_new_company_turn",
                            )
            except Exception:
                logger.opt(exception=True).debug("failed to mark delivery feedback checkpoint superseded")
                continue

            superseded_ids.append(checkpoint_id)
            await self._update_or_emit_checkpoint_card_status(
                checkpoint_id,
                checkpoint_type="company_delivery_feedback",
                status="superseded",
                checkpoint=checkpoint,
                channel_id=f"session:{task_id}",
                status_metadata={"checkpoint_resolution_reason": "new_company_turn_started"},
                project_id=project_id,
            )
        return superseded_ids

    async def _route_company_delivery_feedback_reply_if_pending(
        self,
        *,
        task_id: str,
        content: str,
        session_id: str | None,
        task: Any | None,
        attachment_refs: list[dict] | None,
        message_metadata: dict[str, Any] | None,
        user_message_id: str | None,
        user_message_created_at: float | None,
        run_engine: Any,
        run_project_id: str,
        reply_channel_id: str,
    ) -> bool:
        metadata = dict(message_metadata or {})
        checkpoint_id = str(metadata.get("response_to_checkpoint_id", "") or "").strip()
        checkpoint_type = str(metadata.get("response_to_checkpoint_type", "") or "").strip()
        reply_kind = str(metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
        if checkpoint_type != "company_delivery_feedback" or not checkpoint_id:
            return False

        checkpoint = await self._load_execution_checkpoint_for_reply(
            engine=run_engine,
            project_id=run_project_id,
            checkpoint_id=checkpoint_id,
            checkpoint_type=checkpoint_type,
        )
        if checkpoint is None:
            await self._update_or_emit_checkpoint_card_status(
                checkpoint_id,
                channel_id=reply_channel_id,
                checkpoint_type=checkpoint_type,
                status="stale",
                response_message_id=user_message_id,
                response_metadata=metadata,
                project_id=run_project_id,
            )
            if reply_kind == "ignore":
                return True
            helper = await self.chat_store.insert_message(
                channel_id=reply_channel_id,
                sender="assistant",
                sender_name="OPC",
                content=(
                    "This delivery self-evolution review is no longer active. "
                    "The review card has been marked inactive in the session history."
                ),
                project_id=run_project_id,
                metadata={"type": "system"},
            )
            await self.broadcast({"type": "session_message", "payload": helper})
            return True

        status = str(getattr(checkpoint, "status", "") or "").strip().lower()
        if status and status != "pending":
            await self._update_or_emit_checkpoint_card_status(
                checkpoint_id,
                channel_id=reply_channel_id,
                checkpoint_type=checkpoint_type,
                status=status,
                checkpoint=checkpoint,
                response_message_id=user_message_id,
                response_metadata=metadata,
                project_id=run_project_id,
            )
            if reply_kind == "ignore":
                return True
            if status == "superseded":
                helper_text = (
                    "This delivery self-evolution review was superseded by a newer company turn. "
                    "The review card has been marked inactive in the session history."
                )
            else:
                helper_text = (
                    "This delivery self-evolution review is no longer active. "
                    "The review card has been updated in the session history."
                )
            helper = await self.chat_store.insert_message(
                channel_id=reply_channel_id,
                sender="assistant",
                sender_name="OPC",
                content=helper_text,
                project_id=run_project_id,
                metadata={"type": "system"},
            )
            await self.broadcast({"type": "session_message", "payload": helper})
            return True

        payload = dict(getattr(checkpoint, "payload", {}) or {})
        waiting_task_id = str(payload.get("waiting_task_id", "") or payload.get("task_id", "") or "").strip()
        if reply_kind == "ignore":
            lock_key = checkpoint_id
            lock = self._company_delivery_feedback_reply_locks.get(lock_key)
            if lock is None:
                lock = asyncio.Lock()
                self._company_delivery_feedback_reply_locks[lock_key] = lock

            async with lock:
                checkpoint = await self._load_execution_checkpoint_for_reply(
                    engine=run_engine,
                    project_id=run_project_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_type=checkpoint_type,
                )
                if checkpoint is None:
                    await self._update_or_emit_checkpoint_card_status(
                        checkpoint_id,
                        channel_id=reply_channel_id,
                        checkpoint_type=checkpoint_type,
                        status="stale",
                        response_message_id=user_message_id,
                        response_metadata=metadata,
                        project_id=run_project_id,
                    )
                    return True

                status = str(getattr(checkpoint, "status", "") or "").strip().lower()
                if status and status != "pending":
                    await self._update_or_emit_checkpoint_card_status(
                        checkpoint_id,
                        channel_id=reply_channel_id,
                        checkpoint_type=checkpoint_type,
                        status=status,
                        checkpoint=checkpoint,
                        response_message_id=user_message_id,
                        response_metadata=metadata,
                        project_id=run_project_id,
                    )
                    return True

                started = time.monotonic()
                await self._update_or_emit_checkpoint_card_status(
                    checkpoint_id,
                    channel_id=reply_channel_id,
                    checkpoint_type="company_delivery_feedback",
                    status="ignored",
                    checkpoint=checkpoint,
                    response_message_id=user_message_id,
                    response_metadata=metadata,
                    status_metadata={"checkpoint_resolution_reason": "ignored_by_user"},
                    project_id=run_project_id,
                )

                payload = dict(getattr(checkpoint, "payload", {}) or {})
                ignored_at = datetime.now().isoformat()
                try:
                    runner = getattr(run_engine, "ignore_company_delivery_feedback_checkpoint", None)
                    if callable(runner):
                        result = runner(checkpoint, reply_metadata=metadata or None)
                        if inspect.isawaitable(result):
                            await result
                    else:
                        terminalizer = getattr(run_engine, "_terminalize_company_delivery_feedback_checkpoint", None)
                        if callable(terminalizer):
                            result = terminalizer(
                                checkpoint,
                                status="ignored",
                                resolution="self_evolution_review_ignored",
                                payload_updates={
                                    **payload,
                                    "feedback_ignored": True,
                                    "feedback_ignored_at": ignored_at,
                                    "feedback_resolution": "self_evolution_review_ignored",
                                },
                                task_metadata_updates={
                                    "self_evolution_review_ignored": True,
                                    "self_evolution_review_ignored_at": ignored_at,
                                    "feedback_ignored": True,
                                    "feedback_ignored_at": ignored_at,
                                },
                            )
                            if inspect.isawaitable(result):
                                await result
                        else:
                            resolver = getattr(getattr(run_engine, "store", None), "resolve_execution_checkpoint", None)
                            if callable(resolver):
                                result = resolver(checkpoint_id, status="ignored")
                                if inspect.isawaitable(result):
                                    await result
                except Exception:
                    logger.exception(
                        "failed to terminalize ignored delivery feedback checkpoint: checkpoint_id={}",
                        checkpoint_id,
                    )
                logger.info(
                    "delivery feedback ignore handled: checkpoint_id={} elapsed_ms={:.1f}",
                    checkpoint_id,
                    (time.monotonic() - started) * 1000,
                )
            return True
        waiting_task = None
        store = getattr(run_engine, "store", None)
        if waiting_task_id and self._store_is_ready(store):
            try:
                waiting_task = await store.get_task(waiting_task_id)
            except Exception:
                logger.opt(exception=True).debug("failed to load delivery feedback waiting task")
        target = await self._company_delivery_feedback_parent_target(
            task_id=task_id,
            waiting_task_id=waiting_task_id,
            waiting_task=waiting_task,
            checkpoint=checkpoint,
            payload=payload,
            engine=run_engine,
        )
        parent_task_id = str(target.get("parent_task_id", "") or "").strip()
        parent_session_id = str(target.get("parent_session_id", "") or "").strip()
        if not parent_task_id or not parent_session_id:
            # This is already known to be an explicit delivery-feedback reply.
            # Consuming it here prevents a missing/ambiguous durable identity
            # from falling through as an ordinary turn on the work-item Task.
            if self._chat_store_is_ready(self.chat_store):
                try:
                    helper = await self.chat_store.insert_message(
                        channel_id=reply_channel_id,
                        sender="assistant",
                        sender_name="OPC",
                        content=(
                            "This review no longer matches an active company runtime. "
                            "Refresh the session before replying again."
                        ),
                        project_id=run_project_id,
                        metadata={
                            "type": "system",
                            "reason": "company_runtime_identity_mismatch",
                        },
                    )
                    await self.broadcast({"type": "session_message", "payload": helper})
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to emit delivery feedback identity rejection"
                    )
            return True

        lock_key = checkpoint_id or parent_session_id
        lock = self._company_delivery_feedback_reply_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._company_delivery_feedback_reply_locks[lock_key] = lock

        self._track_session(
            parent_task_id,
            self._process_company_delivery_feedback_reply(
                parent_task_id=parent_task_id,
                parent_session_id=parent_session_id,
                reply_channel_id=reply_channel_id,
                content=content,
                attachment_refs=attachment_refs,
                message_metadata=metadata,
                user_message_id=user_message_id,
                user_message_created_at=user_message_created_at,
                run_engine=run_engine,
                run_project_id=run_project_id,
                checkpoint=checkpoint,
                waiting_task_id=waiting_task_id,
                lock=lock,
            ),
            project_id=run_project_id,
            engine=run_engine,
        )
        return True

    async def _process_company_delivery_feedback_reply(
        self,
        *,
        parent_task_id: str,
        parent_session_id: str,
        reply_channel_id: str,
        content: str,
        attachment_refs: list[dict] | None,
        message_metadata: dict[str, Any] | None,
        user_message_id: str | None,
        user_message_created_at: float | None,
        run_engine: Any,
        run_project_id: str,
        checkpoint: Any,
        waiting_task_id: str,
        lock: asyncio.Lock,
    ) -> None:
        pid = self._normalize_project_id(run_project_id or getattr(run_engine, "project_id", None))
        async with lock:
            try:
                parent_task = None
                if self._store_is_ready(run_engine.store):
                    try:
                        parent_task = await run_engine.store.get_task(parent_task_id)
                    except Exception:
                        logger.opt(exception=True).debug("failed to load parent task for delivery feedback reply")
                session_exec_mode = self._normalize_session_exec_mode(self._exec_mode)
                session_company_profile = self._normalize_session_company_profile(self._company_profile)
                session_org_id = ""
                if parent_task is not None:
                    session_exec_mode, session_company_profile = self._resolve_task_session_config(parent_task)
                    session_org_id = self._resolve_task_org_id(parent_task)
                engine_mode, company_profile = self._resolve_engine_mode(
                    session_exec_mode,
                    session_company_profile,
                )
                engine_message_metadata = dict(message_metadata or {})
                engine_message_metadata.update(_ui_message_identity_metadata(
                    message_id=user_message_id,
                    conversation_turn_id=_ui_conversation_turn_id(user_message_id),
                    created_at=user_message_created_at,
                ))
                self._active_runtime_children[parent_task_id] = parent_task_id
                self._session_to_task[parent_session_id] = parent_task_id
                payload = dict(getattr(checkpoint, "payload", {}) or {})
                for child_task_id in list(payload.get("task_ids", []) or []):
                    child_id = str(child_task_id or "").strip()
                    if child_id:
                        self._active_runtime_children[child_id] = parent_task_id
                if waiting_task_id:
                    self._active_runtime_children[waiting_task_id] = parent_task_id

                reply_kind = str(engine_message_metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
                if reply_kind not in {"approve", "feedback"}:
                    normalized_content = str(content or "").strip().lower()
                    reply_kind = "approve" if normalized_content in {"approve", "approved", "i approve this delivery."} else "feedback"
                feedback_text = str(content or "").strip() if reply_kind == "feedback" else ""
                runner = getattr(run_engine, "run_company_delivery_self_evolution_checkpoint", None)
                if not callable(runner):
                    raise RuntimeError("Company delivery self-evolution is not available in this runtime.")
                result_text = await runner(
                    checkpoint,
                    action=reply_kind,
                    feedback=feedback_text,
                    reply_metadata=engine_message_metadata or None,
                )
                assistant_msg = await self.chat_store.insert_message(
                    channel_id=reply_channel_id or f"session:{parent_task_id}",
                    sender="assistant",
                    sender_name="OPC",
                    content=str(result_text or "Self-evolution completed.").strip(),
                    project_id=pid,
                    metadata={
                        "type": "system",
                        "kind": "company_self_evolution_result",
                        "response_to_checkpoint_type": "company_delivery_feedback",
                        "response_to_checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "") or "").strip(),
                        "checkpoint_reply_kind": reply_kind,
                        "self_evolution_completed": True,
                    },
                )
                await self.broadcast({"type": "session_message", "payload": assistant_msg})
                updated_checkpoint_msg = await self._mark_checkpoint_card_after_engine_response(
                    channel_id=reply_channel_id,
                    project_id=pid,
                    engine=run_engine,
                    message_metadata=engine_message_metadata,
                    response_message_id=user_message_id,
                )
                if updated_checkpoint_msg is not None:
                    await self.broadcast({"type": "session_message", "payload": updated_checkpoint_msg})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Company delivery feedback reply processing error: {}", exc)
                if self._chat_store_is_ready(self.chat_store):
                    try:
                        msg = await self.chat_store.insert_message(
                            channel_id=reply_channel_id or f"session:{parent_task_id}",
                            sender="system",
                            sender_name="OPC",
                            content=f"Error: {exc}",
                            project_id=pid,
                        )
                        await self.broadcast({"type": "session_message", "payload": msg})
                    except Exception:
                        logger.opt(exception=True).debug("failed to write delivery feedback reply error")
            finally:
                if self._chat_store_is_ready(self.chat_store):
                    await self._flush_progress(parent_task_id, project_id=pid)
                if waiting_task_id:
                    await self._flush_progress(waiting_task_id, project_id=pid)

    async def _route_company_suspend_reply_if_pending(
        self,
        *,
        task_id: str,
        content: str,
        session_id: str | None,
        task: Any | None,
        attachment_refs: list[dict] | None,
        message_metadata: dict[str, Any] | None,
        user_message_id: str | None,
        user_message_created_at: float | None,
        run_engine: Any,
        run_project_id: str,
    ) -> bool:
        """Route text after company Stop without waiting on the parent task lock.

        A live company run can still hold the parent session lock while Stop is
        finalizing or already suspended. Plain text after Stop must reach the
        engine's company-runtime suspend checkpoint immediately so the
        CEO/final-decider can arbitrate with edit/delete/delegate tools. The
        Continue button uses ``session_resume`` and keeps the original forced
        resume path.
        """
        explicit_checkpoint_type = str((message_metadata or {}).get("response_to_checkpoint_type", "") or "").strip()
        explicit_runtime_handoff = (
            explicit_checkpoint_type in COMPANY_RUNTIME_CHECKPOINT_TYPES
        )
        if explicit_checkpoint_type and not explicit_runtime_handoff:
            return False
        if task is None:
            return False
        explicit_checkpoint_id = str(
            (message_metadata or {}).get("response_to_checkpoint_id", "") or ""
        ).strip()

        async def reject(reason: str, content: str) -> bool:
            if self._chat_store_is_ready(self.chat_store):
                try:
                    helper = await self.chat_store.insert_message(
                        channel_id=f"session:{task_id}",
                        sender="assistant",
                        sender_name="OPC",
                        content=content,
                        project_id=run_project_id,
                        metadata={"type": "system", "reason": reason},
                    )
                    await self.broadcast({"type": "session_message", "payload": helper})
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to emit company runtime handoff rejection"
                    )
            return True

        if explicit_runtime_handoff and not explicit_checkpoint_id:
            return await reject(
                "missing_runtime_checkpoint_id",
                "This Continue request has no runtime checkpoint identity. Refresh the session and try again.",
            )

        try:
            base_target = await self._resolve_company_runtime_target(
                task_id,
                engine=run_engine,
            )
        except Exception:
            logger.opt(exception=True).debug("failed to resolve company suspend reply target")
            base_target = None
        if base_target is None:
            if explicit_runtime_handoff or is_company_runtime_task(task):
                return await reject(
                    "company_runtime_identity_mismatch",
                    "This message no longer matches an active company runtime. Refresh the session before retrying.",
                )
            return False

        target = base_target
        if explicit_checkpoint_id:
            try:
                target = await self._resolve_company_runtime_target(
                    task_id,
                    engine=run_engine,
                    checkpoint_id=explicit_checkpoint_id,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to resolve explicit company runtime checkpoint"
                )
                target = None
            if target is None:
                if explicit_runtime_handoff or base_target.get("checkpoint") is not None:
                    return await reject(
                        "company_runtime_identity_mismatch",
                        "This runtime checkpoint does not match the current company session. Refresh before retrying.",
                    )
                return False

        runtime_session_id = str(target.get("runtime_session_id", "") or "").strip()
        if not runtime_session_id:
            return await reject(
                "company_runtime_identity_mismatch",
                "The active company runtime has no canonical session identity. Refresh before retrying.",
            )

        finalizer = self._company_stop_finalize_tasks.get(runtime_session_id)
        if finalizer is not None and not finalizer.done():
            try:
                await asyncio.wait_for(asyncio.shield(finalizer), timeout=10.0)
            except asyncio.TimeoutError:
                helper = await self.chat_store.insert_message(
                    channel_id=f"session:{task_id}",
                    sender="assistant",
                    sender_name="OPC",
                    content="Stop is still finalizing. Send your update again after the runtime reaches Suspended.",
                    project_id=run_project_id,
                    metadata={"type": "system", "reason": "company_stop_finalize_in_progress"},
                )
                await self.broadcast({"type": "session_message", "payload": helper})
                return True
            except Exception:
                logger.opt(exception=True).debug("company stop finalizer failed before follow-up routing")

            # Stop finalization may have created the checkpoint after the first
            # index load.  Reload durable identity before deciding whether this
            # message is an ordinary turn.
            try:
                target = await self._resolve_company_runtime_target(
                    task_id,
                    engine=run_engine,
                    checkpoint_id=explicit_checkpoint_id,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to reload company runtime identity after stop finalization"
                )
                target = None
            if target is None:
                return await reject(
                    "company_runtime_identity_mismatch",
                    "The stopped runtime could not be matched to this session. Refresh before retrying.",
                )

        checkpoint = target.get("checkpoint")
        if checkpoint is None:
            if explicit_runtime_handoff:
                return await reject(
                    "company_runtime_checkpoint_not_found",
                    "This company runtime checkpoint is no longer active. Refresh before retrying.",
                )
            return False
        checkpoint_status = str(
            getattr(checkpoint, "status", "") or ""
        ).strip().lower()
        if checkpoint_status != "pending":
            return await reject(
                "company_runtime_checkpoint_not_pending",
                (
                    "This company runtime is already being resumed. Wait for the current handoff to finish."
                    if checkpoint_status == "resuming"
                    else "This company runtime checkpoint is no longer pending. Refresh before retrying."
                ),
            )

        lock = self._company_suspend_reply_locks.get(runtime_session_id)
        if lock is not None:
            self._release_current_execution_handoff()
            return await reject(
                "checkpoint_handoff_in_progress",
                "This company runtime is already being resumed by another request.",
            )
        lock = asyncio.Lock()
        self._company_suspend_reply_locks[runtime_session_id] = lock

        try:
            bg = self._track_session(
                task_id,
                self._process_company_suspend_reply(
                    ui_task_id=task_id,
                    runtime_session_id=runtime_session_id,
                    content=content,
                    attachment_refs=attachment_refs,
                    message_metadata=message_metadata,
                    user_message_id=user_message_id,
                    user_message_created_at=user_message_created_at,
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
        return True

    async def _process_company_suspend_reply(
        self,
        *,
        ui_task_id: str,
        runtime_session_id: str,
        content: str,
        attachment_refs: list[dict] | None,
        message_metadata: dict[str, Any] | None,
        user_message_id: str | None,
        user_message_created_at: float | None,
        run_engine: Any,
        run_project_id: str,
        target: dict[str, Any],
        checkpoint: Any,
        lock: asyncio.Lock,
    ) -> None:
        channel_id = f"session:{ui_task_id}"
        pid = self._normalize_project_id(run_project_id or getattr(run_engine, "project_id", None))
        async with lock:
            try:
                try:
                    await self._set_company_runtime_control(
                        target,
                        state="resuming",
                        checkpoint_id=str(
                            getattr(checkpoint, "checkpoint_id", "") or ""
                        ).strip(),
                    )
                except Exception:
                    logger.opt(exception=True).debug("failed to broadcast company suspend reply routing state")

                config_task = target.get("config_task")
                session_exec_mode = self._normalize_session_exec_mode(self._exec_mode)
                session_company_profile = self._normalize_session_company_profile(self._company_profile)
                session_org_id = ""
                if config_task is not None:
                    session_exec_mode, session_company_profile = self._resolve_task_session_config(config_task)
                    session_org_id = self._resolve_task_org_id(config_task)
                engine_mode, company_profile = self._resolve_engine_mode(
                    session_exec_mode,
                    session_company_profile,
                )
                engine_message_metadata = dict(message_metadata or {})
                engine_message_metadata.update({
                    "response_to_checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "") or ""),
                    "response_to_checkpoint_type": str(getattr(checkpoint, "checkpoint_type", "") or ""),
                })
                engine_message_metadata.update(_ui_message_identity_metadata(
                    message_id=user_message_id,
                    conversation_turn_id=_ui_conversation_turn_id(user_message_id),
                    created_at=user_message_created_at,
                ))
                execution_anchor_task_id = str(
                    target.get("ui_anchor_task_id", "") or ""
                ).strip()
                if execution_anchor_task_id:
                    self._active_runtime_children[execution_anchor_task_id] = execution_anchor_task_id
                self._session_to_task[runtime_session_id] = (
                    execution_anchor_task_id or ui_task_id
                )
                await run_engine.process_message(
                    content,
                    project_id=pid,
                    session_id=runtime_session_id,
                    mode=engine_mode,
                    org_id=session_org_id or None,
                    company_profile=company_profile,
                    preferred_agent=None,
                    origin_task_id=execution_anchor_task_id or None,
                    attachment_refs=attachment_refs,
                    message_metadata=engine_message_metadata or None,
                )
                checkpoint_meta = await self._extract_checkpoint_metadata(
                    ui_task_id,
                    session_id=runtime_session_id,
                    engine=run_engine,
                )
                await self._sync_task_transcript_messages(
                    ui_task_id,
                    engine=run_engine,
                    latest_assistant_metadata=checkpoint_meta if checkpoint_meta else None,
                )
                # Replace the optimistic ``resuming`` projection with the
                # checkpoint state committed by the engine.  Successful and
                # already-consumed handoffs need the same durable refresh as
                # failed handoffs so every client converges on snapshot state.
                try:
                    await self.on_kanban_changed(engine=run_engine)
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to refresh company runtime control after handoff"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._is_expected_shutdown_error(exc) or self._is_closed_database_error(exc):
                    logger.debug(
                        "Company suspend reply skipped during shutdown/closed store: {}: {}",
                        type(exc).__name__,
                        exc,
                    )
                else:
                    logger.exception("Company suspend reply processing error: {}", exc)
                    if self._chat_store_is_ready(self.chat_store):
                        try:
                            msg = await self.chat_store.insert_message(
                                channel_id=channel_id,
                                sender="system",
                                sender_name="OPC",
                                content=f"Error: {exc}",
                                project_id=pid,
                            )
                            await self.broadcast({"type": "session_message", "payload": msg})
                        except Exception as chat_exc:
                            if self._is_expected_shutdown_error(chat_exc) or self._is_closed_database_error(chat_exc):
                                logger.debug(
                                    "Skipped company suspend reply error message during shutdown/closed store: {}: {}",
                                    type(chat_exc).__name__,
                                    chat_exc,
                                )
                            else:
                                logger.opt(exception=True).debug(
                                    "Failed to write company suspend reply error message",
                                )
                    # The engine restores a failed handoff checkpoint to
                    # pending.  Replace the optimistic ``resuming`` projection
                    # for every client with that durable state immediately.
                    await self.on_kanban_changed(engine=run_engine)
            finally:
                if self._chat_store_is_ready(self.chat_store):
                    await self._flush_progress(ui_task_id, project_id=pid)
                self._task_bg_context.pop(asyncio.current_task(), None)
                self._company_suspend_reply_locks.pop(runtime_session_id, None)

    def _build_company_work_item_gate_meta(self, cp: Any) -> dict[str, Any]:
        payload = dict(cp.payload or {})
        gate = dict(payload.get("gate", {}) or {})
        projection_id = str(payload.get("work_item_projection_id") or "").strip()
        turn_type = str(payload.get("work_item_turn_type") or "").strip()
        projection_title = str(
            payload.get("work_item_projection_title") or projection_id or "Work item gate"
        ).strip()
        gate_type = str(gate.get("type", "") or "review").strip()
        prompt_lines = [
            f"{projection_title} requires confirmation.",
            f"Gate type: {gate_type}",
        ]
        instructions = str(gate.get("instructions", "") or "").strip()
        if instructions:
            prompt_lines.append(f"Instructions: {instructions}")
        return {
            "checkpoint_type": "company_work_item_gate",
            "checkpoint_id": cp.checkpoint_id,
            **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type),
            "work_item_projection_title": projection_title,
            "company_profile": payload.get("company_profile", ""),
            "summary": instructions or f"Pending {gate_type} confirmation",
            "prompt": "\n".join(prompt_lines),
            "options": [
                {"id": "approve", "label": "Approve"},
                {"id": "deny", "label": "Deny"},
            ],
            "default_action": "deny",
            "runtime_session_id": str(payload.get("runtime_session_id", "") or "").strip(),
            "resume_cursor": payload.get("resume_cursor"),
            "active_subagents": list(payload.get("active_subagents", []) or []),
            "permission_requests": list(payload.get("permission_requests", []) or []),
            "worktree_path": str(payload.get("worktree_path", "") or "").strip(),
        }

    def _build_recruitment_meta(self, cp: Any, *, engine: Any | None = None) -> dict[str, Any]:
        """Extract recruitment plan data into frontend-friendly metadata."""
        runtime_engine = engine or self.engine
        payload = cp.payload
        rp = payload.get("recruitment_plan", {})
        plan_metadata = dict(rp.get("metadata", {}) or {})
        recruitment_agent = self._normalize_session_preferred_agent(
            payload.get("recruitment_agent") or plan_metadata.get("recruitment_agent") or "native",
            default="native",
        )
        proposals_raw = rp.get("proposals", [])
        proposals = []
        payload_role_agents: dict[str, str] = {}
        raw_payload_role_agents = payload.get("recruitment_role_agents")
        if isinstance(raw_payload_role_agents, dict):
            payload_role_agents = {
                str(raw_role_id or "").strip(): self._normalize_session_preferred_agent(raw_agent, default="codex")
                for raw_role_id, raw_agent in raw_payload_role_agents.items()
                if str(raw_role_id or "").strip()
            }
        recruitment_role_agents: dict[str, str] = {}
        employee_payloads: list[dict[str, Any]] = []
        template_payloads: list[dict[str, Any]] = []
        org_engine = getattr(runtime_engine, "org_engine", None)
        talent_market = getattr(runtime_engine, "talent_market", None)
        is_placeholder = getattr(runtime_engine, "_is_placeholder_staffing_employee", lambda _employee: False)
        employee_payload = getattr(runtime_engine, "_staffing_employee_payload", None)
        template_payload = getattr(runtime_engine, "_staffing_template_payload", None)
        if org_engine and callable(employee_payload):
            try:
                employee_payloads = [
                    employee_payload(employee)
                    for employee in org_engine.list_employees()
                    if not is_placeholder(employee)
                ]
            except Exception:
                employee_payloads = []
        if talent_market and callable(template_payload):
            try:
                template_payloads = [
                    template_payload(template)
                    for template in talent_market.list_available_templates()
                    if str(getattr(template, "id", "") or "").strip()
                ]
            except Exception:
                template_payloads = []
        employees_by_id = {
            str(item.get("employee_id", "") or "").strip(): item
            for item in employee_payloads
            if str(item.get("employee_id", "") or "").strip()
        }
        templates_by_id = {
            str(item.get("template_id", "") or "").strip(): item
            for item in template_payloads
            if str(item.get("template_id", "") or "").strip()
        }
        employees_by_role: dict[str, list[dict[str, Any]]] = {}
        for item in employee_payloads:
            role_id = str(item.get("role_id", "") or "").strip()
            if role_id:
                employees_by_role.setdefault(role_id, []).append(item)
        staffing_roles: list[dict[str, Any]] = []
        staffing_selections: dict[str, dict[str, str]] = {}
        recruitment_rationales: list[dict[str, Any]] = []
        for p in proposals_raw:
            role_id = str(p.get("role_id", "") or "").strip()
            proposal_metadata = dict(p.get("metadata", {}) or {})
            entry: dict[str, Any] = {
                "role_id": role_id,
                "status": p.get("status", ""),
                "rationale": p.get("rationale", ""),
                "role_labels": p.get("role_labels", []),
            }
            cand = p.get("candidate")
            if cand:
                entry["candidate"] = {
                    "template_id": cand.get("template_id", ""),
                    "template_name": cand.get("template_name", ""),
                    "category": cand.get("category", ""),
                    "domains": cand.get("domains", []),
                    "proposed_name": cand.get("proposed_employee_name", ""),
                    "rationale": cand.get("rationale", ""),
                }
                template_id = str(cand.get("template_id", "") or "").strip()
                if template_id and template_id not in templates_by_id:
                    templates_by_id[template_id] = {
                        "kind": "template",
                        "template_id": template_id,
                        "template_name": cand.get("template_name", "") or template_id,
                        "category": cand.get("category", ""),
                        "domains": cand.get("domains", []),
                        "tags": [],
                        "description": "",
                        "preferred_external_agent": cand.get("preferred_external_agent"),
                        "source_path": cand.get("source_path", ""),
                    }
            emp = p.get("existing_employee")
            if emp:
                entry["existing_employee"] = {
                    "employee_id": emp.get("employee_id", ""),
                    "employee_name": emp.get("employee_name", ""),
                    "role_id": emp.get("role_id", ""),
                    "domains": emp.get("domains", []),
                    "experience_score": emp.get("experience_score", 0),
                    "rationale": emp.get("rationale", ""),
                }
                employee_id = str(emp.get("employee_id", "") or "").strip()
                if employee_id and employee_id not in employees_by_id:
                    employee_payload_item = {
                        "kind": "employee",
                        "employee_id": employee_id,
                        "employee_name": emp.get("employee_name", "") or employee_id,
                        "role_id": emp.get("role_id", "") or role_id,
                        "category": emp.get("category", ""),
                        "domains": emp.get("domains", []),
                        "tags": [],
                        "description": "",
                        "preferred_external_agent": None,
                        "experience_score": emp.get("experience_score", 0),
                    }
                    employees_by_id[employee_id] = employee_payload_item
                    if role_id:
                        employees_by_role.setdefault(role_id, []).append(employee_payload_item)
            default_agent = self._normalize_session_preferred_agent("codex", default="codex")
            entry["default_agent"] = default_agent
            entry["selected_agent"] = self._normalize_session_preferred_agent(
                payload_role_agents.get(role_id) or proposal_metadata.get("selected_execution_agent"),
                default=default_agent,
            )
            if role_id:
                recruitment_role_agents[role_id] = entry["selected_agent"]
            proposals.append(entry)
            role_label = str((p.get("role_labels", []) or [role_id])[0] or role_id)
            default_selection: dict[str, str] = {"kind": "fallback", "id": ""}
            selection_label = "Fallback role-only"
            cand = p.get("candidate")
            emp = p.get("existing_employee")
            if emp and str(emp.get("employee_id", "") or "").strip():
                employee_id = str(emp.get("employee_id", "") or "").strip()
                default_selection = {"kind": "employee", "id": employee_id, "employee_id": employee_id}
                selection_label = str(emp.get("employee_name", "") or employee_id)
            elif cand and str(cand.get("template_id", "") or "").strip():
                template_id = str(cand.get("template_id", "") or "").strip()
                default_selection = {"kind": "template", "id": template_id, "template_id": template_id}
                selection_label = str(cand.get("proposed_employee_name") or cand.get("template_name") or template_id)
            if role_id:
                staffing_selections[role_id] = dict(default_selection)
                same_role_ids = {
                    str(item.get("employee_id", "") or "").strip()
                    for item in employees_by_role.get(role_id, [])
                    if str(item.get("employee_id", "") or "").strip()
                }
                same_role_ids.update(
                    str(item or "").strip()
                    for item in list(p.get("existing_employee_ids", []) or [])
                    if str(item or "").strip()
                )
                staffing_roles.append(
                    {
                        "role_id": role_id,
                        "role_label": role_label,
                        "role_responsibility": "",
                        "default_selection": default_selection,
                        "same_role_employee_ids": sorted(same_role_ids),
                        "fallback_available": True,
                        "default_agent": default_agent,
                        "selected_agent": entry["selected_agent"],
                        "default_source": "recruitment",
                    }
                )
                reason_parts = [
                    str((cand or {}).get("rationale", "") or "").strip(),
                    str((emp or {}).get("rationale", "") or "").strip(),
                    str(p.get("rationale", "") or "").strip(),
                ]
                rationale = next((item for item in reason_parts if item), "")
                recruitment_rationales.append(
                    {
                        "role_id": role_id,
                        "role_label": role_label,
                        "status": p.get("status", ""),
                        "selection_label": selection_label,
                        "rationale": rationale,
                    }
                )
        employee_payloads = list(employees_by_id.values())
        template_payloads = list(templates_by_id.values())
        return {
            "checkpoint_type": "company_recruitment_confirmation",
            "checkpoint_id": cp.checkpoint_id,
            "company_profile": rp.get("company_profile", "corporate"),
            "previous_checkpoint_id": payload.get("previous_checkpoint_id", ""),
            "recruitment_revision": payload.get("recruitment_revision") or dict(rp.get("metadata", {}) or {}).get("recruitment_revision"),
            "recruiter_feedback": list(rp.get("recruiter_feedback", []) or []),
            "recruitment_agent": recruitment_agent,
            "recruitment_role_agents": recruitment_role_agents,
            "proposals": proposals,
            "summary": rp.get("summary", ""),
            "recruitment_rationales": recruitment_rationales,
            "staffing_roles": staffing_roles,
            "staffing_pool": {
                "employees": employee_payloads,
                "templates": template_payloads,
            },
            "staffing_selections": staffing_selections,
        }

    def _build_staffing_selection_meta(self, cp: Any) -> dict[str, Any]:
        """Extract manual staffing data into frontend-friendly metadata."""
        payload = dict(cp.payload or {})
        raw_role_agents = payload.get("recruitment_role_agents")
        payload_role_agents = raw_role_agents if isinstance(raw_role_agents, dict) else {}
        recruitment_agent = self._normalize_session_preferred_agent(
            payload.get("recruitment_agent") or "native",
            default="native",
        )
        staffing_roles: list[dict[str, Any]] = []
        recruitment_role_agents: dict[str, str] = {}
        for raw_role in list(payload.get("staffing_roles", []) or []):
            if not isinstance(raw_role, dict):
                continue
            role = dict(raw_role)
            role_id = str(role.get("role_id", "") or "").strip()
            selected_agent = self._normalize_session_preferred_agent(
                payload_role_agents.get(role_id) or role.get("selected_agent") or role.get("default_agent") or "codex",
                default="codex",
            )
            if role_id:
                role["selected_agent"] = selected_agent
                recruitment_role_agents[role_id] = selected_agent
            staffing_roles.append(role)
        return {
            "checkpoint_type": "company_staffing_selection",
            "checkpoint_id": cp.checkpoint_id,
            "company_profile": payload.get("company_profile", "corporate"),
            "summary": payload.get("summary") or "Select staff manually, or run automatic recruitment.",
            "staffing_strategy": payload.get("staffing_strategy", ""),
            "recommended_action": payload.get("recommended_action", ""),
            "staffing_defaults": dict(payload.get("staffing_defaults", {}) or {}),
            "staffing_roles": staffing_roles,
            "recruitment_agent": recruitment_agent,
            "recruitment_role_agents": recruitment_role_agents,
            "staffing_pool": dict(payload.get("staffing_pool", {}) or {}),
        }

    async def _build_reorg_meta(
        self,
        cp: Any,
        *,
        engine: Any | None = None,
    ) -> dict[str, Any] | None:
        """Extract reorg proposal data into frontend-friendly metadata."""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        if not store:
            return None
        proposal_id = cp.payload.get("proposal_id", "")
        if not proposal_id:
            return None
        proposal = await store.get_reorg_proposal(proposal_id)
        if not proposal:
            return None
        changeset = proposal.changeset
        role_changes = []
        if hasattr(changeset, "role_changes"):
            for rc in changeset.role_changes:
                role_changes.append({
                    "action": rc.action,
                    "role_id": rc.role_id,
                    "replacement_role_id": rc.replacement_role_id,
                    "reason": rc.reason,
                })
        return {
            "checkpoint_type": "company_reorg_pending",
            "checkpoint_id": cp.checkpoint_id,
            "proposal_id": proposal.proposal_id,
            "scope": proposal.scope.value,
            "risk_level": proposal.risk_level.value,
            "status": proposal.status.value,
            "title": proposal.title,
            "summary": proposal.summary,
            "rationale": proposal.rationale,
            "role_changes": role_changes,
            "impact_summary": proposal.impact_summary,
            "user_confirmation_required": proposal.user_confirmation_required,
        }

    def _build_delivery_feedback_meta(self, cp: Any) -> dict[str, Any]:
        payload = dict(cp.payload or {})
        prompt = str(payload.get("prompt", "") or "").strip()
        projection_title = str(payload.get("work_item_projection_title", "") or "").strip()
        feedback_scope = str(payload.get("feedback_scope", "work_item") or "work_item").strip()
        delivery_package = payload.get("delivery_package")
        if not isinstance(delivery_package, dict):
            delivery_package = {}
        result_content = str(payload.get("result_content", "") or "").strip()
        summary = str(
            delivery_package.get("executive_summary")
            or delivery_package.get("summary")
            or payload.get("work_item_summary")
            or payload.get("work_item_summary_for_downstream")
            or result_content
            or projection_title
            or ("Final delivery review" if feedback_scope == "final" else "Work item review")
        ).strip()
        waiting_task_id = str(
            payload.get("waiting_task_id")
            or payload.get("task_id")
            or getattr(cp, "task_id", "")
            or ""
        ).strip()
        return {
            "checkpoint_type": "company_delivery_feedback",
            "checkpoint_id": cp.checkpoint_id,
            "waiting_task_id": waiting_task_id,
            "task_id": waiting_task_id,
            **work_item_identity_payload(
                projection_id=str(payload.get("work_item_projection_id", "") or "").strip(),
                turn_type=str(payload.get("work_item_turn_type", "") or "").strip(),
            ),
            "work_item_projection_title": projection_title,
            "company_profile": payload.get("company_profile", ""),
            "feedback_scope": feedback_scope,
            "summary": summary,
            "prompt": prompt,
            "options": [
                {"id": "approve", "label": "Fully Agree / 完全同意"},
                {"id": "ignore", "label": "Ignore / 忽略"},
                {"id": "feedback", "label": "Feedback / 反馈"},
            ],
            "delivery_package": delivery_package,
            "result_content": result_content,
            "delivery_revision": payload.get("delivery_revision", ""),
            "owner_directive_revision": payload.get("owner_directive_revision", ""),
            "latest_user_directive": str(payload.get("latest_user_directive", "") or "").strip(),
            "waiting_work_item_id": str(payload.get("waiting_work_item_id", "") or "").strip(),
            "runtime_session_id": str(payload.get("runtime_session_id", "") or "").strip(),
            "resume_cursor": payload.get("resume_cursor"),
            "active_subagents": list(payload.get("active_subagents", []) or []),
            "permission_requests": list(payload.get("permission_requests", []) or []),
            "worktree_path": str(payload.get("worktree_path", "") or "").strip(),
        }
