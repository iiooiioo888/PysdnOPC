"""CheckpointMixin — 檢查點保存/恢復相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from opc.engine._core import OPCEngine


class CheckpointMixin:
    """Mixin providing 檢查點保存/恢復相關方法 for OPCEngine."""

    @staticmethod
    def _external_approval_info(result: TaskResult) -> dict[str, Any]:
        artifacts = result.artifacts or {}
        approval = artifacts.get("approval", {})
        return dict(approval) if isinstance(approval, dict) else {}

    def _external_result_requires_user_review(self, result: TaskResult) -> bool:
        if result.status in _REVIEW_WAITING_STATUSES:
            return True
        artifacts = result.artifacts or {}
        return bool(artifacts.get("requires_user_input"))

    def _external_result_denied_by_user(self, result: TaskResult) -> bool:
        approval = self._external_approval_info(result)
        return (
            result.status == TaskStatus.FAILED
            and str(approval.get("action", "")).lower() == ApprovalAction.REJECT.value
            and str(approval.get("policy_source", "")).lower() == "human_escalation"
        )

    @staticmethod
    def _extract_runtime_state_from_artifacts(artifacts: dict[str, Any] | None) -> dict[str, Any]:
        data = dict(artifacts or {})
        runtime_session_id = str(data.get("runtime_session_id", "") or "").strip()
        if not runtime_session_id:
            return {}
        active_subagents = data.get("active_subagents", [])
        permission_requests = data.get("permission_requests", [])
        compaction_boundaries = data.get("compaction_boundaries", [])
        compaction_records = data.get("compaction_records", compaction_boundaries)
        resume_cursor = data.get("resume_cursor")
        worktree_path = str(data.get("worktree_path", "") or "").strip()
        task_ledger = data.get("task_ledger", [])
        prefetch_hits = data.get("prefetch_hits", [])
        verification = data.get("verification", {})
        verification_evidence = data.get("verification_evidence", {})
        verification_verdict = str(data.get("verification_verdict", "") or "").strip()
        artifact_manifest = data.get("artifact_manifest", [])
        resume_state = dict(data.get("resume_state", {}) or {})
        runtime_state = {
            "runtime_session_id": runtime_session_id,
            "active_subagents": active_subagents if isinstance(active_subagents, list) else [],
            "permission_requests": permission_requests if isinstance(permission_requests, list) else [],
            "compaction_boundaries": compaction_boundaries if isinstance(compaction_boundaries, list) else [],
            "compaction_records": compaction_records if isinstance(compaction_records, list) else [],
            "resume_cursor": resume_cursor,
            "worktree_path": worktree_path,
            "task_ledger": task_ledger if isinstance(task_ledger, list) else [],
            "prefetch_hits": prefetch_hits if isinstance(prefetch_hits, list) else [],
            "verification": verification if isinstance(verification, dict) else {},
            "verification_evidence": verification_evidence if isinstance(verification_evidence, dict) else {},
            "verification_verdict": verification_verdict,
            "artifact_manifest": artifact_manifest if isinstance(artifact_manifest, list) else [],
            "resume_state": resume_state,
        }
        return runtime_state

    def _apply_runtime_state_to_task(self, task: Task, result: TaskResult) -> None:
        runtime_state = self._extract_runtime_state_from_artifacts(result.artifacts)
        if not runtime_state:
            return
        task.metadata = dict(task.metadata)
        task.metadata["runtime_v2"] = runtime_state
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["runtime_v2"] = runtime_state

    def _build_runtime_checkpoint_payload(self, task: Task, result: TaskResult | None = None) -> dict[str, Any]:
        source_artifacts: dict[str, Any] | None = result.artifacts if result else None
        if not source_artifacts and isinstance(task.result, dict):
            source_artifacts = dict(task.result.get("artifacts", {}) or {})
        runtime_state = self._extract_runtime_state_from_artifacts(source_artifacts)
        if not runtime_state:
            runtime_state = dict(task.metadata.get("runtime_v2", {}) or {})
        if not runtime_state:
            return {}
        payload = {
            "runtime_v2": runtime_state,
            "runtime_session_id": runtime_state.get("runtime_session_id", ""),
            "resume_cursor": runtime_state.get("resume_cursor"),
            "active_subagents": list(runtime_state.get("active_subagents", []) or []),
            "permission_requests": list(runtime_state.get("permission_requests", []) or []),
            "compaction_boundaries": list(runtime_state.get("compaction_boundaries", []) or []),
            "compaction_records": list(runtime_state.get("compaction_records", []) or []),
            "worktree_path": runtime_state.get("worktree_path", ""),
            "task_ledger": list(runtime_state.get("task_ledger", []) or []),
            "prefetch_hits": list(runtime_state.get("prefetch_hits", []) or []),
            "verification": dict(runtime_state.get("verification", {}) or {}),
            "verification_evidence": dict(runtime_state.get("verification_evidence", {}) or {}),
            "verification_verdict": runtime_state.get("verification_verdict", ""),
            "artifact_manifest": list(runtime_state.get("artifact_manifest", []) or []),
            "resume_state": dict(runtime_state.get("resume_state", {}) or {}),
        }
        for key in (
            "work_item_turn_type",
            "work_item_runtime_plan",
            "work_item_artifact_index",
            "work_item_summary",
            "work_item_orchestration_profile",
            "work_item_verification_required",
            "structured_review_verdict",
            "verification_status",
            "verification_evidence",
            "artifact_contract_status",
            "member_session_id",
            "member_session_state",
            "message_priority",
            "ownership_contract",
        ):
            if key in task.metadata and task.metadata.get(key) not in (None, "", [], {}):
                payload[key] = task.metadata.get(key)
        return payload

    def _restore_runtime_state_from_checkpoint(self, task: Task, payload: dict[str, Any]) -> None:
        runtime_state = dict(payload.get("runtime_v2", {}) or {})
        task.metadata = dict(task.metadata)
        task.context_snapshot = dict(task.context_snapshot)
        if runtime_state:
            task.metadata["runtime_v2"] = runtime_state
            task.context_snapshot["runtime_resume"] = {
                **runtime_state,
                "restored_from_checkpoint": True,
                "restored_at": datetime.now().isoformat(),
            }
        for key in (
            "work_item_turn_type",
            "work_item_runtime_plan",
            "work_item_artifact_index",
            "work_item_summary",
            "work_item_orchestration_profile",
            "work_item_verification_required",
            "structured_review_verdict",
            "verification_status",
            "verification_evidence",
            "artifact_contract_status",
            "member_session_id",
            "member_session_state",
            "message_priority",
            "ownership_contract",
        ):
            if key not in payload or payload.get(key) in (None, "", [], {}):
                continue
            task.metadata[key] = payload.get(key)
            task.context_snapshot[key] = payload.get(key)

    def _generated_runtime_session_id(self, task: Task, checkpoint_type: str = "") -> str:
        seed = "::".join([
            str(task.project_id or "default"),
            str(task.session_id or ""),
            str(task.id or ""),
            str(checkpoint_type or ""),
        ])
        return f"rtmig_{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex[:24]}"

    async def _build_migrated_runtime_state(
        self,
        task: Task,
        *,
        checkpoint_type: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_data = dict(payload or {})
        runtime_state = dict(task.metadata.get("runtime_v2", {}) or {})
        if runtime_state.get("runtime_session_id"):
            return runtime_state

        transcript_count = 0
        compaction_boundaries: list[dict[str, Any]] = []
        if self.store and task.session_id:
            try:
                transcript = await self.store.get_session_transcript(task.session_id)
                transcript_count = len(transcript)
            except Exception:
                transcript_count = 0
            try:
                latest_compaction = await self.store.get_latest_session_compaction(task.session_id)
            except Exception:
                latest_compaction = None
            if latest_compaction:
                compaction_boundaries.append({
                    "summary": "Migrated from legacy session compaction.",
                    "source_boundary_message_id": latest_compaction.source_boundary_message_id,
                    "compaction_message_id": latest_compaction.compaction_message_id,
                    "created_at": latest_compaction.created_at.isoformat(),
                })

        permission_requests: list[dict[str, Any]] = []
        approval = dict(payload_data.get("approval", {}) or {})
        if approval:
            permission_requests.append({
                "tool_name": str(payload_data.get("tool_name", "") or ""),
                "resolution": "ask",
                "scope": "once",
                "risk_level": str(approval.get("risk_level", "medium") or "medium"),
                "rationale": str(approval.get("rationale", "") or payload_data.get("prompt", "") or "Migrated legacy approval request."),
                "source": str(approval.get("policy_source", "legacy_checkpoint") or "legacy_checkpoint"),
            })
        pause_request = dict(payload_data.get("pause_request", {}) or {})
        if pause_request and not permission_requests:
            permission_requests.append({
                "tool_name": str(payload_data.get("tool_name", "") or ""),
                "resolution": "ask",
                "scope": "once",
                "risk_level": "medium",
                "rationale": str(pause_request.get("reason", "") or "Migrated legacy pause request."),
                "source": "legacy_checkpoint",
            })

        runtime_state = {
            "runtime_session_id": self._generated_runtime_session_id(task, checkpoint_type=checkpoint_type),
            "active_subagents": list(payload_data.get("active_subagents", []) or []),
            "permission_requests": permission_requests,
            "compaction_boundaries": compaction_boundaries,
            "resume_cursor": max(transcript_count, int(payload_data.get("resume_cursor", 0) or 0)),
            "worktree_path": str(
                payload_data.get("worktree_path")
                or task.metadata.get("target_output_dir")
                or ""
            ).strip(),
            "checkpoint_source": checkpoint_type or "legacy_checkpoint",
            "migrated_from_legacy": True,
        }
        return runtime_state

    async def _ensure_checkpoint_runtime_v2_payload(
        self,
        checkpoint: ExecutionCheckpoint,
        task: Task | None = None,
    ) -> ExecutionCheckpoint:
        payload = dict(checkpoint.payload or {})
        runtime_state = dict(payload.get("runtime_v2", {}) or {})
        if runtime_state.get("runtime_session_id"):
            return checkpoint
        if not task:
            task_id = str(
                checkpoint.task_id
                or payload.get("waiting_task_id")
                or payload.get("task_id")
                or ""
            ).strip()
            if task_id and self.store:
                task = await self.store.get_task(task_id)
        if not task:
            return checkpoint

        runtime_state = await self._build_migrated_runtime_state(
            task,
            checkpoint_type=checkpoint.checkpoint_type,
            payload=payload,
        )
        if not runtime_state.get("runtime_session_id"):
            return checkpoint

        task.metadata = dict(task.metadata)
        task.metadata["runtime_v2"] = runtime_state
        task.metadata["migration_status"] = "runtime_v2_migrated"
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["runtime_v2"] = runtime_state
        if self.store:
            await self.store.save_task(task)
            if getattr(self.store, "save_runtime_session", None):
                await self.store.save_runtime_session(
                    runtime_session_id=runtime_state["runtime_session_id"],
                    task_id=task.id,
                    session_id=task.session_id,
                    project_id=task.project_id,
                    status="migrated",
                    metadata=runtime_state,
                )

        payload = {
            **payload,
            "runtime_v2": runtime_state,
            "runtime_session_id": runtime_state.get("runtime_session_id", ""),
            "resume_cursor": runtime_state.get("resume_cursor"),
            "active_subagents": list(runtime_state.get("active_subagents", []) or []),
            "permission_requests": list(runtime_state.get("permission_requests", []) or []),
            "compaction_boundaries": list(runtime_state.get("compaction_boundaries", []) or []),
            "worktree_path": runtime_state.get("worktree_path", ""),
            "migrated_to_runtime_v2": True,
        }
        checkpoint.payload = payload
        checkpoint.updated_at = datetime.now()
        if self.store:
            await self.store.save_execution_checkpoint(checkpoint)
        return checkpoint

    async def _supersede_stale_task_wait_checkpoints(self, task_id: str, *, reason: str) -> None:
        """終止過期的任務等待檢查點 — 任務已不再等待時清除 pending 行。

        功能說明：
            公司運行時可能透過自身機制（審批卡授權、新審查嘗試）推進暫停的
            工作項目，而不經過引擎檢查點回覆。若檢查點行保持 pending，會錯誤
            捕獲使用者下一則無關訊息並路由到已不再等待的任務。

        參數：
            task_id (str)：任務 ID。
            reason (str)：終止原因（用於日誌）。

        被誰引用：
            - _execute_registered_task_attempt()：任務完結時呼叫
        """
        if not task_id or not self.store:
            return
        supersede = getattr(self.store, "supersede_pending_checkpoints", None)
        if not callable(supersede):
            return
        try:
            superseded = await supersede(
                project_id=self.project_id or "default",
                task_id=task_id,
                checkpoint_types=list(self._TASK_WAIT_CHECKPOINT_TYPES),
            )
        except Exception:
            logger.opt(exception=True).warning(
                f"Failed to supersede stale task-wait checkpoints for task {task_id}"
            )
            return
        if superseded:
            logger.info(
                f"Superseded {len(superseded)} stale task-wait checkpoint(s) for task {task_id} ({reason})"
            )

    @staticmethod
    def _checkpoint_awaits_approval_decision(checkpoint: ExecutionCheckpoint) -> bool:
        """判斷暫停的任務等待檢查點是否正在等待權限審批決策。

        功能說明：
            審批升級會將任務暫停，並將待處理的權限請求記錄在
            payload.runtime_v2.permission_requests 中。這些提示透過審批卡
            決定，其回覆始終明確指向檢查點；自由文字聊天永遠不是決策。

        參數：
            checkpoint (ExecutionCheckpoint)：待檢查的檢查點。

        返回值：
            bool — 若為 task_user_input 類型且含 permission_requests 則 True。
        """
        if str(checkpoint.checkpoint_type or "").strip() != "task_user_input":
            return False
        payload = dict(checkpoint.payload or {})
        runtime_state = payload.get("runtime_v2")
        if not isinstance(runtime_state, dict):
            return False
        requests = runtime_state.get("permission_requests")
        return isinstance(requests, list) and len(requests) > 0

    async def _checkpoint_task_still_waiting(self, checkpoint: ExecutionCheckpoint) -> bool:
        """判斷任務等待檢查點是否仍對應真正等待中的任務。

        功能說明：
            延遲解析孤立行（任務已完成、失敗、被新審查嘗試取代或已刪除），
            標記為 stale，使歷史脏資料在首次考慮恢復時自動修復。
            非任務等待類型的檢查點始終視為有效。

        參數：
            checkpoint (ExecutionCheckpoint)：待驗證的檢查點。

        返回值：
            bool — True 表示檢查點仍有效可恢復；False 表示已過期。
        """
        if str(checkpoint.checkpoint_type or "").strip() not in self._TASK_WAIT_CHECKPOINT_TYPES:
            return True
        task_id = str(
            checkpoint.task_id or dict(checkpoint.payload or {}).get("task_id") or ""
        ).strip()
        if not task_id or not self.store:
            return True
        try:
            task = await self.store.get_task(task_id)
        except Exception:
            logger.opt(exception=True).debug(
                f"Could not verify task {task_id} for checkpoint {checkpoint.checkpoint_id}; keeping it"
            )
            return True
        if task is None:
            stale_reason = f"task {task_id} no longer exists"
        elif task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            stale_reason = f"task {task_id} settled as {task.status.value}"
        else:
            # A non-terminal task status proves nothing on its own:
            # suspend/restart flows legitimately park a waiting task back at
            # PENDING or RUNNING. The linked delegation work item is the
            # authoritative signal — once its phase is terminal (a later review
            # attempt or the manager closed it) or the item is gone, no runtime
            # will ever come back to consume this checkpoint.
            stale_reason = await self._task_work_item_closed_reason(task)
        if not stale_reason:
            return True
        try:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="stale")
            logger.info(
                f"Resolved stale {checkpoint.checkpoint_type} checkpoint "
                f"{checkpoint.checkpoint_id} ({stale_reason})"
            )
        except Exception:
            logger.opt(exception=True).warning(
                f"Failed to resolve stale checkpoint {checkpoint.checkpoint_id}"
            )
        return False

    async def _task_work_item_closed_reason(self, task: Task) -> str:
        """回傳任務關聯的委派工作項目已關閉的原因（空字串表示仍活躍）。

        參數：
            task (Task)：待檢查的任務。

        返回值：
            str — 非空表示工作項目已關閉（含原因）；空字串表示保留檢查點。
        """
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id or not self.store:
            return ""
        getter = getattr(self.store, "get_delegation_work_item", None)
        if not callable(getter):
            return ""
        try:
            work_item = await getter(work_item_id)
        except Exception:
            logger.opt(exception=True).debug(
                f"Could not load work item {work_item_id} while validating a checkpoint; keeping it"
            )
            return ""
        if work_item is None:
            return f"work item {work_item_id} no longer exists"
        phase_raw = getattr(work_item, "phase", "")
        phase = str(getattr(phase_raw, "value", phase_raw) or "").strip()
        if phase in {Phase.APPROVED.value, Phase.FAILED.value, Phase.CANCELLED.value}:
            return f"work item {work_item_id} closed with phase={phase}"
        return ""

    async def _save_execution_checkpoint(self, data: dict[str, Any]) -> None:
        """儲存執行檢查點到資料庫並取代同任務同類型的舊檢查點。

        參數：
            data (dict)：檢查點資料（含 project_id, session_id, checkpoint_type, task_id, payload）。

        被誰引用：
            - _save_routing_checkpoint()、_save_task_pause_checkpoint() 等
        """
        assert self.store
        payload = dict(data.get("payload", {}))
        if not str(payload.get("basis_hash", "") or "").strip():
            payload["basis_hash"] = self._checkpoint_basis_hash(payload)
        checkpoint = ExecutionCheckpoint(
            project_id=data.get("project_id", self.project_id or "default"),
            session_id=data.get("session_id"),
            checkpoint_type=data.get("checkpoint_type", "generic"),
            task_id=data.get("task_id"),
            payload=payload,
        )
        await self.store.save_execution_checkpoint(checkpoint)
        supersede = getattr(self.store, "supersede_pending_checkpoints", None)
        if callable(supersede) and checkpoint.task_id:
            superseded_ids = await supersede(
                project_id=checkpoint.project_id,
                task_id=checkpoint.task_id,
                checkpoint_types=[checkpoint.checkpoint_type],
                basis_hash=str(payload.get("basis_hash", "") or "").strip() or None,
                exclude_checkpoint_id=checkpoint.checkpoint_id,
            )
            if superseded_ids:
                checkpoint.payload = dict(checkpoint.payload or {})
                checkpoint.payload["superseded_checkpoint_ids"] = superseded_ids
                checkpoint.updated_at = datetime.now()
                await self.store.save_execution_checkpoint(checkpoint)
        payload = dict(checkpoint.payload or {})
        runtime_session_id = str(payload.get("runtime_session_id", "") or "").strip()
        if runtime_session_id:
            await self.event_bus.publish(OPCEvent(
                event_type="runtime_event",
                payload={
                    "type": "checkpoint_saved",
                    "timestamp_ms": int(time.time() * 1000),
                    "runtime_session_id": runtime_session_id,
                    "task_id": checkpoint.task_id,
                    "session_id": checkpoint.session_id,
                    "checkpoint_type": checkpoint.checkpoint_type,
                    "execution_mode": payload.get("execution_mode", ""),
                    "review_level": payload.get("review_level", ""),
                    "review_target_role_id": payload.get("review_target_role_id", ""),
                    **work_item_identity_payload_from_metadata(
                        payload,
                        projection_id_fallback=str(payload.get("work_item_projection_id", "") or ""),
                        turn_type_fallback=str(payload.get("work_item_turn_type", "") or ""),
                    ),
                    "work_item_projection_title": payload.get("work_item_projection_title", ""),
                },
            ))

    @staticmethod
    def _checkpoint_basis_hash(payload: dict[str, Any]) -> str:
        """根據檢查點 payload 的關鍵欄位計算 SHA1 雜湊（用於去重/取代判斷）。"""
        basis = {
            "task_id": str(payload.get("task_id", "") or payload.get("waiting_task_id", "") or "").strip(),
            **work_item_identity_payload_from_metadata(
                payload,
                projection_id_fallback=str(payload.get("work_item_projection_id", "") or ""),
                turn_type_fallback=str(payload.get("work_item_turn_type", "") or ""),
            ),
            "delivery_revision": str(payload.get("delivery_revision", "") or "").strip(),
            "owner_directive_revision": str(payload.get("owner_directive_revision", "") or "").strip(),
            "latest_user_directive": str(payload.get("latest_user_directive", "") or "").strip(),
            "prompt": str(payload.get("prompt", "") or "").strip(),
            "result_content": str(payload.get("result_content", "") or "").strip(),
            "work_item_summary": str(payload.get("work_item_summary", "") or "").strip(),
            "work_item_summary_for_downstream": str(payload.get("work_item_summary_for_downstream", "") or "").strip(),
            "artifact_index": payload.get("work_item_artifact_index", []),
            "artifact_manifest": payload.get("artifact_manifest", []),
            "verification_status": payload.get("verification_status", {}),
            "verification_evidence": payload.get("verification_evidence", {}),
            "verification_verdict": str(payload.get("verification_verdict", "") or "").strip(),
            "delivery_package": payload.get("delivery_package", {}),
        }
        encoded = json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()

    async def _save_routing_checkpoint(
        self,
        checkpoint_type: str,
        original_message: str,
        payload: dict[str, Any],
        session_id: str | None = None,
    ) -> None:
        """儲存路由檢查點 — 記錄使用者原始訊息和路由決策供後續恢復。"""
        await self._save_execution_checkpoint(
            {
                "project_id": self.project_id or "default",
                "session_id": session_id,
                "checkpoint_type": checkpoint_type,
                "payload": {
                    "original_message": original_message,
                    **payload,
                },
            }
        )

    def _checkpoint_execution_mode_for_task(self, task: Task) -> str:
        """決定暫停檢查點應記錄的執行模式。

        功能說明：
            工作項目運行時成員關係是持久信號；execution_mode metadata 欄位
            是易變的，觀察到恢復後會退化為 task_mode，導致下次恢復錯誤路由。

        參數：
            task (Task)：待記錄的任務。

        返回值：
            str — 執行模式字串（company_mode 或 single_agent）。
        """
        if is_work_item_runtime_metadata(task.metadata):
            return ExecutionMode.COMPANY_MODE.value
        return str(task.metadata.get("execution_mode", ExecutionMode.SINGLE_AGENT.value))

    async def _save_task_pause_checkpoint(self, task: Task, result: TaskResult) -> None:
        """儲存任務暫停檢查點 — 任務進入等待審查/人類輸入狀態時呼叫。

        參數：
            task (Task)：暫停的任務。
            result (TaskResult)：含 pause_request 的結果。

        被誰引用：
            - _execute_registered_task_attempt()：結果狀態為 AWAITING_* 時
        """
        pause_request = dict(result.artifacts.get("pause_request", {})) if result.artifacts else {}
        runtime_payload = self._build_runtime_checkpoint_payload(task, result)
        review_level = str(
            pause_request.get("review_level")
            or ("manager" if result.status == TaskStatus.AWAITING_MANAGER_REVIEW else "human")
        ).strip().lower()
        review_target_role_id = str(
            pause_request.get("review_target_role_id")
            or (task.metadata.get("manager_role_id", "") if review_level == "manager" else "")
            or ""
        ).strip()
        review_chain_role_ids = [
            str(item).strip()
            for item in list(
                pause_request.get("review_chain_role_ids")
                or ([review_target_role_id] if review_target_role_id else [])
            )
            if str(item).strip()
        ]
        pending_reorg_id = str(task.metadata.get("pending_reorg_proposal_id", "") or "").strip()
        if pending_reorg_id:
            await self._save_execution_checkpoint(
                {
                    "project_id": task.project_id,
                    "session_id": task.session_id,
                    "checkpoint_type": "company_reorg_pending",
                    "task_id": task.id,
                    "payload": {
                        "proposal_id": pending_reorg_id,
                        "waiting_task_id": task.id,
                        "task_ids": list(task.metadata.get("execution_task_ids", [task.id])),
                        "parent_session_id": task.parent_session_id or task.metadata.get("parent_session_id"),
                        "org_version": task.metadata.get("org_version", 1),
                        "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                        "company_work_item_plan": task.metadata.get("company_work_item_plan"),
                        "review_level": review_level,
                        "review_target_role_id": review_target_role_id,
                        "review_chain_role_ids": review_chain_role_ids,
                        **runtime_payload,
                    },
                }
            )
            return
        await self._save_execution_checkpoint(
            {
                "project_id": task.project_id,
                "session_id": task.session_id,
                "checkpoint_type": "task_user_input",
                "task_id": task.id,
                "payload": {
                    "task_id": task.id,
                    "session_id": task.session_id,
                    "execution_mode": self._checkpoint_execution_mode_for_task(task),
                    "task_ids": list(task.metadata.get("execution_task_ids", [task.id])),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "company_work_item_plan": task.metadata.get("company_work_item_plan"),
                    "prompt": result.content,
                    "pause_request": pause_request,
                    "review_level": review_level,
                    "review_target_role_id": review_target_role_id,
                    "review_chain_role_ids": review_chain_role_ids,
                    **runtime_payload,
                },
            }
        )

    async def _save_peer_pause_checkpoint(self, task: Task, result: TaskResult) -> None:
        """儲存同儕等待檢查點 — 任務進入 AWAITING_PEER 狀態時呼叫。

        參數：
            task (Task)：等待同儕回應的任務。
            result (TaskResult)：執行結果。

        被誰引用：
            - _execute_registered_task_attempt()：結果狀態為 AWAITING_PEER 時
        """
        peer_wait = dict(task.metadata.get("peer_wait", {}))
        runtime_payload = self._build_runtime_checkpoint_payload(task, result)
        await self._save_execution_checkpoint(
            {
                "project_id": task.project_id,
                "session_id": task.session_id,
                "checkpoint_type": "task_peer_wait",
                "task_id": task.id,
                "payload": {
                    "task_id": task.id,
                    "session_id": task.session_id,
                    "execution_mode": self._checkpoint_execution_mode_for_task(task),
                    "task_ids": list(task.metadata.get("execution_task_ids", [task.id])),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "company_work_item_plan": task.metadata.get("company_work_item_plan"),
                    "peer_wait": peer_wait,
                    "result_content": result.content,
                    **runtime_payload,
                },
            }
        )

    async def get_latest_pending_checkpoint_for_session(
        self,
        session_id: str | None = None,
    ) -> ExecutionCheckpoint | None:
        """取得指定工作階段最新的 pending 檢查點（含公司運行時暫停檢查點）。

        參數：
            session_id (str | None)：工作階段 ID。

        返回值：
            ExecutionCheckpoint | None — 最新 pending 檢查點或 None。

        被誰引用：
            - process_message()：判斷使用者回覆是否應恢復檢查點
            - UI 快照建構器：每次同步時查詢
        """
        if not self.store:
            return None
        project_id = self.project_id or "default"
        requested_session_id = str(session_id or "").strip()
        # Fast path: with no live checkpoint rows in the project there is
        # nothing to surface, so skip the parent-session resolution below.
        # Snapshot builders call this once per task on every UI sync tick, and
        # that resolution loads (and JSON-parses) task rows each time.
        checkpoint_probe = getattr(self.store, "get_execution_checkpoints", None)
        if callable(checkpoint_probe):
            try:
                live_checkpoints = await checkpoint_probe(
                    project_id=project_id,
                    statuses=["pending", "resuming"],
                )
            except Exception:
                live_checkpoints = None
            if live_checkpoints is not None and len(live_checkpoints) == 0:
                return None
        company_parent_session_id = await self._company_runtime_parent_session_for_session_id(
            requested_session_id,
        )
        runtime_session_id = company_parent_session_id or requested_session_id
        active_suspend_checkpoint: ExecutionCheckpoint | None = None
        if runtime_session_id:
            active_suspend_checkpoint = await self.get_active_company_runtime_suspend_checkpoint(runtime_session_id)
        if (
            active_suspend_checkpoint is not None
            and company_parent_session_id
            and company_parent_session_id != requested_session_id
        ):
            return await self._ensure_checkpoint_runtime_v2_payload(active_suspend_checkpoint)
        checkpoint = await self.store.get_latest_pending_checkpoint(
            project_id,
            session_id=requested_session_id or None,
        )
        # Skip (and lazily resolve) orphaned task-wait checkpoints; each stale
        # row is marked resolved before re-querying, so this terminates.
        while checkpoint is not None and not await self._checkpoint_task_still_waiting(checkpoint):
            checkpoint = await self.store.get_latest_pending_checkpoint(
                project_id,
                session_id=requested_session_id or None,
            )
        deferred_suspend_checkpoint: ExecutionCheckpoint | None = None
        if checkpoint and self._checkpoint_is_user_visible(checkpoint):
            if not self._is_company_runtime_suspend_checkpoint(checkpoint.checkpoint_type):
                return await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
            deferred_suspend_checkpoint = checkpoint
        if not requested_session_id:
            return await self._ensure_checkpoint_runtime_v2_payload(deferred_suspend_checkpoint) if deferred_suspend_checkpoint else None

        # Company-mode gates are persisted on child work-item sessions. When the
        # user comes back through the primary session, surface the newest child
        # checkpoint so a plain "continue" resumes the runtime correctly.
        snapshot = await self._load_company_runtime_snapshot(runtime_session_id)
        if not snapshot:
            selected_suspend_checkpoint = deferred_suspend_checkpoint or active_suspend_checkpoint
            return await self._ensure_checkpoint_runtime_v2_payload(selected_suspend_checkpoint) if selected_suspend_checkpoint else None
        _, tasks = snapshot
        visible_session_ids = {
            str(getattr(task, "session_id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "session_id", "") or "").strip()
        }
        visible_task_ids = {
            str(getattr(task, "id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
        }
        if not visible_session_ids and not visible_task_ids:
            selected_suspend_checkpoint = deferred_suspend_checkpoint or active_suspend_checkpoint
            return await self._ensure_checkpoint_runtime_v2_payload(selected_suspend_checkpoint) if selected_suspend_checkpoint else None

        checkpoints = await self.store.get_pending_checkpoints(project_id=project_id)
        for pending in checkpoints:
            if not self._checkpoint_is_user_visible(pending):
                continue
            if not await self._checkpoint_task_still_waiting(pending):
                continue
            if self._is_company_runtime_suspend_checkpoint(pending.checkpoint_type):
                if deferred_suspend_checkpoint is None and str(pending.session_id or "").strip() == session_id:
                    deferred_suspend_checkpoint = pending
                continue
            pending_session_id = str(pending.session_id or "").strip()
            pending_task_id = str(
                pending.task_id
                or pending.payload.get("waiting_task_id")
                or pending.payload.get("task_id")
                or ""
            ).strip()
            if pending_session_id in visible_session_ids or pending_task_id in visible_task_ids:
                return await self._ensure_checkpoint_runtime_v2_payload(pending)
        selected_suspend_checkpoint = deferred_suspend_checkpoint or active_suspend_checkpoint
        return await self._ensure_checkpoint_runtime_v2_payload(selected_suspend_checkpoint) if selected_suspend_checkpoint else None

    async def _company_runtime_parent_session_for_session_id(self, session_id: str | None) -> str:
        """將任意持久任務工作階段解析為其所屬的公司運行時工作階段。"""
        if not self.store:
            return ""
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        try:
            get_by_session = getattr(self.store, "get_tasks_by_session_id", None)
            if callable(get_by_session):
                tasks = await get_by_session(
                    sid,
                    project_id=self.project_id or "default",
                )
                identity_index = build_company_runtime_identity_index(tasks)
            else:
                identity_index = await load_company_runtime_identity_index(
                    self.store,
                    self.project_id or "default",
                )
            identity = identity_index.resolve(task_session_id=sid)
        except Exception:
            logger.opt(exception=True).debug(
                "failed to resolve company runtime session identity"
            )
            return ""
        return str(getattr(identity, "runtime_session_id", "") or "").strip()

    @staticmethod
    def _checkpoint_is_user_visible(checkpoint: ExecutionCheckpoint) -> bool:
        """判斷檢查點是否對使用者可見（manager 級別審查不直接展示）。"""
        payload = dict(checkpoint.payload or {})
        review_level = str(payload.get("review_level", "") or "").strip().lower()
        return review_level != "manager"

    @classmethod
    def _checkpoint_is_company_scoped(cls, checkpoint_type: str | None) -> bool:
        """判斷檢查點類型是否屬於公司模式範圍。"""
        normalized = str(checkpoint_type or "").strip()
        return (
            normalized.startswith("company_")
            or normalized == "company_peer_wait"
            or cls._is_company_runtime_suspend_checkpoint(normalized)
        )

    @staticmethod
    def _reply_metadata_targets_checkpoint(
        reply_metadata: dict[str, Any] | None,
        checkpoint: ExecutionCheckpoint,
    ) -> bool:
        """判斷回覆 metadata 是否明確指向指定檢查點（UI 顯式回覆）。"""
        metadata = dict(reply_metadata or {})
        explicit_id = str(metadata.get("response_to_checkpoint_id", "") or "").strip()
        explicit_type = str(metadata.get("response_to_checkpoint_type", "") or "").strip()
        if not explicit_id and not explicit_type:
            return False
        checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
        checkpoint_type = str(getattr(checkpoint, "checkpoint_type", "") or "").strip()
        if explicit_id and explicit_id != checkpoint_id:
            return False
        if explicit_type and explicit_type != checkpoint_type:
            return False
        return True

    @staticmethod
    def _explicit_checkpoint_reply(
        reply_metadata: dict[str, Any] | None,
    ) -> tuple[str, str]:
        """從回覆 metadata 提取顯式指定的檢查點 ID 和類型。"""
        metadata = dict(reply_metadata or {})
        return (
            str(metadata.get("response_to_checkpoint_id", "") or "").strip(),
            str(metadata.get("response_to_checkpoint_type", "") or "").strip(),
        )

    async def _load_execution_checkpoint_by_id(
        self,
        checkpoint_id: str,
    ) -> ExecutionCheckpoint | None:
        """根據 ID 載入執行檢查點（先嘗試直接查詢，再 fallback 到列表掃描）。"""
        checkpoint_id = str(checkpoint_id or "").strip()
        if not checkpoint_id or not self.store:
            return None
        direct_getter = getattr(self.store, "get_execution_checkpoint", None)
        if callable(direct_getter):
            try:
                checkpoint = await direct_getter(checkpoint_id)
                if checkpoint is not None:
                    return checkpoint
            except TypeError:
                try:
                    checkpoint = await direct_getter(
                        checkpoint_id,
                        project_id=self.project_id or "default",
                    )
                    if checkpoint is not None:
                        return checkpoint
                except Exception:
                    logger.opt(exception=True).debug("direct checkpoint lookup failed")
            except Exception:
                logger.opt(exception=True).debug("direct checkpoint lookup failed")

        listing_getter = getattr(self.store, "get_execution_checkpoints", None)
        if not callable(listing_getter):
            return None
        try:
            checkpoints = await listing_getter(project_id=self.project_id or "default")
        except TypeError:
            checkpoints = await listing_getter(self.project_id or "default")
        for checkpoint in checkpoints:
            if str(getattr(checkpoint, "checkpoint_id", "") or "").strip() == checkpoint_id:
                return checkpoint
        return None

    async def _checkpoint_visible_to_reply_session(
        self,
        checkpoint: ExecutionCheckpoint,
        session_id: str | None,
    ) -> bool:
        """判斷檢查點是否對回覆所在的工作階段可見（含公司運行時子工作階段）。"""
        requested_session_id = str(session_id or "").strip()
        if not requested_session_id:
            return False

        checkpoint_session_id = str(getattr(checkpoint, "session_id", "") or "").strip()
        checkpoint_task_id = str(
            getattr(checkpoint, "task_id", "")
            or dict(getattr(checkpoint, "payload", {}) or {}).get("waiting_task_id")
            or dict(getattr(checkpoint, "payload", {}) or {}).get("task_id")
            or ""
        ).strip()
        if checkpoint_session_id == requested_session_id:
            return True

        runtime_session_id = await self._company_runtime_parent_session_for_session_id(
            requested_session_id,
        )
        runtime_session_id = runtime_session_id or requested_session_id
        if checkpoint_session_id == runtime_session_id:
            return True

        snapshot = await self._load_company_runtime_snapshot(runtime_session_id)
        if not snapshot:
            return False
        _, tasks = snapshot
        visible_session_ids = {
            str(getattr(task, "session_id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "session_id", "") or "").strip()
        }
        visible_task_ids = {
            str(getattr(task, "id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
        }
        return (
            bool(checkpoint_session_id and checkpoint_session_id in visible_session_ids)
            or bool(checkpoint_task_id and checkpoint_task_id in visible_task_ids)
        )

    async def _maybe_resume_checkpoint(
        self,
        user_reply: str,
        session_id: str | None = None,
        reply_metadata: dict[str, Any] | None = None,
        requested_mode: str | None = None,
    ) -> str | None:
        """嘗試恢復檢查點 — 根據使用者回覆和 metadata 路由到對應的恢復處理器。

        參數：
            user_reply (str)：使用者回覆文字。
            session_id (str | None)：工作階段 ID。
            reply_metadata (dict | None)：回覆 metadata（含顯式檢查點 ID 等）。
            requested_mode (str | None)：請求的模式（task/company）。

        返回值：
            str | None — 恢復結果文字；None 表示無檢查點可恢復。

        被誰引用：
            - process_message()：優先嘗試恢復檢查點
        """
        explicit_checkpoint_id, explicit_checkpoint_type = self._explicit_checkpoint_reply(reply_metadata)
        if explicit_checkpoint_id:
            checkpoint = await self._load_execution_checkpoint_by_id(explicit_checkpoint_id)
            if not checkpoint:
                return "This request is no longer active."
            if explicit_checkpoint_type and explicit_checkpoint_type != str(checkpoint.checkpoint_type or "").strip():
                return "This request is no longer active or does not match the selected checkpoint."
            if not await self._checkpoint_visible_to_reply_session(checkpoint, session_id):
                return "This request is no longer active."
            if str(getattr(checkpoint, "status", "") or "").strip().lower() != "pending":
                if str(getattr(checkpoint, "checkpoint_type", "") or "").strip() == "company_delivery_feedback":
                    return None
                return "This request is no longer active."
            if not await self._checkpoint_task_still_waiting(checkpoint):
                return "This request is no longer active."
        else:
            checkpoint = await self.get_latest_pending_checkpoint_for_session(session_id)
            if not checkpoint:
                return None
            if self._checkpoint_awaits_approval_decision(checkpoint):
                # A parked permission prompt is answered by its approval card
                # (the card reply carries an explicit response_to_checkpoint_id).
                # Deferred cards stay pending indefinitely, so a plain chat
                # message must not be consumed as the approval answer — let it
                # continue as a normal conversation turn instead.
                return None
        metadata_mode = str(dict(reply_metadata or {}).get("mode", "") or "").strip()
        inferred_mode = requested_mode or metadata_mode
        if not inferred_mode and self._checkpoint_is_company_scoped(checkpoint.checkpoint_type):
            inferred_mode = "company"
        normalized_requested_mode = self._normalize_requested_mode(inferred_mode or "task")
        if (
            normalized_requested_mode != "company"
            and self._checkpoint_is_company_scoped(checkpoint.checkpoint_type)
            and not self._reply_metadata_targets_checkpoint(reply_metadata, checkpoint)
            and not self._is_company_runtime_suspend_checkpoint(checkpoint.checkpoint_type)
        ):
            return None
        if checkpoint.checkpoint_type in {"route_clarification", "company_runtime_selection"}:
            return await self._resume_routing_checkpoint(checkpoint, user_reply)
        if checkpoint.checkpoint_type == "task_user_input":
            return await self._resume_task_checkpoint(checkpoint, user_reply)
        if checkpoint.checkpoint_type in {"task_peer_wait", "company_peer_wait"}:
            return await self._resume_peer_checkpoint(checkpoint, user_reply)
        if checkpoint.checkpoint_type == "company_work_item_gate":
            return await self._resume_company_runtime_checkpoint(checkpoint, user_reply)
        if self._is_company_runtime_suspend_checkpoint(checkpoint.checkpoint_type):
            if self._reply_metadata_requests_force_resume(reply_metadata):
                return await self._resume_company_suspend_checkpoint(checkpoint, user_reply)
            return await self._resume_company_suspend_checkpoint_via_final_decider(checkpoint, user_reply)
        if checkpoint.checkpoint_type == "company_delivery_feedback":
            if not explicit_checkpoint_id:
                return None
            reply_kind = str(dict(reply_metadata or {}).get("checkpoint_reply_kind", "") or "").strip().lower()
            if not str(user_reply or "").strip() and reply_kind not in {"approve", "feedback", "ignore"}:
                return "There is a pending delivery self-evolution review. Use the review card to fully agree, ignore, or send feedback."
            if reply_kind not in {"approve", "feedback", "ignore"}:
                normalized_reply = str(user_reply or "").strip().lower()
                if normalized_reply in {"ignore", "ignored", "skip"}:
                    reply_kind = "ignore"
                else:
                    reply_kind = "approve" if normalized_reply in {"approve", "approved", "i approve this delivery."} else "feedback"
            if reply_kind == "ignore":
                return await self.ignore_company_delivery_feedback_checkpoint(
                    checkpoint,
                    reply_metadata=reply_metadata,
                )
            return await self.run_company_delivery_self_evolution_checkpoint(
                checkpoint,
                action=reply_kind,
                feedback=user_reply if reply_kind == "feedback" else "",
                reply_metadata=reply_metadata,
            )
        if checkpoint.checkpoint_type == "company_staffing_selection":
            return await self._resume_staffing_selection_checkpoint(
                checkpoint,
                user_reply,
                reply_metadata=reply_metadata,
            )
        if checkpoint.checkpoint_type == "company_recruitment_confirmation":
            return await self._resume_recruitment_checkpoint(
                checkpoint,
                user_reply,
                reply_metadata=reply_metadata,
            )
        if checkpoint.checkpoint_type == "company_reorg_pending":
            return await self._resume_reorg_checkpoint(checkpoint, user_reply)
        return None

    async def _resume_routing_checkpoint(self, checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        """恢復路由檢查點 — 將使用者補充資訊附加到原始訊息後重新處理。"""
        payload = checkpoint.payload
        original_message = payload.get("original_message", "")
        if not original_message:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending request because the original message is missing."

        if checkpoint.checkpoint_type == "company_runtime_selection":
            combined = f"{original_message}\n\nCompany runtime selection: {user_reply.strip()}"
        else:
            combined = f"{original_message}\n\nAdditional information from user:\n{user_reply.strip()}"

        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        message = UserMessage(
            channel="cli",
            user_id="owner",
            content=combined,
            session_id=checkpoint.session_id or payload.get("session_id") or str(uuid.uuid4()),
            project_context=self.project_id,
        )
        response = await self.message_bus.process_single(message)
        return response.content if response else "No response generated after resume."

    async def _release_work_item_human_wait(self, task: Task, *, reason: str) -> bool:
        """將暫停於 awaiting_human 的工作項目推回 ready 狀態。

        功能說明：
            這是恢復已回答的人類等待的狀態機部分：phase 通過合法的
            AWAITING_HUMAN → READY 恢復出口移動，並釋放過期的 claim，
            使公司調度器能在下次遍歷時重新認領該項目。

        參數：
            task (Task)：關聯的任務。
            reason (str)：釋放原因。

        返回值：
            bool — True 表示 phase 寫入成功。
        """
        if not self.store or not hasattr(self.store, "update_delegation_work_item"):
            return False
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id:
            return False
        try:
            work_item = await self.store.get_delegation_work_item(work_item_id)
        except Exception:
            logger.opt(exception=True).debug(
                "release_work_item_human_wait: work item load failed for task {}", task.id
            )
            return False
        if work_item is None or getattr(work_item, "phase", None) != Phase.AWAITING_HUMAN:
            return False
        metadata = dict(getattr(work_item, "metadata", {}) or {})
        metadata_unset: list[str] = []
        if str(metadata.get("dispatch_hold", "") or "").strip() == "company_runtime_suspended":
            metadata_unset = ["dispatch_hold", "suspended_at", "suspend_reason", "suspended_phase"]
        try:
            await self.store.update_delegation_work_item(
                work_item_id,
                phase=Phase.READY,
                blocked_reason="",
                metadata_updates={
                    "human_wait_released_at": datetime.now().isoformat(),
                    "human_wait_release_reason": reason,
                    "claimed_by_role_session_id": "",
                    "claimed_task_id": "",
                },
                metadata_unset=metadata_unset or None,
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
            )
        except InvalidPhaseTransition:
            logger.opt(exception=True).warning(
                "release_work_item_human_wait: phase transition rejected for work item {}",
                work_item_id,
            )
            return False
        except Exception:
            logger.opt(exception=True).warning(
                "release_work_item_human_wait: phase write failed for work item {}",
                work_item_id,
            )
            return False
        logger.info(
            "Released human wait on work item {} (task {}, reason={}): awaiting_human -> ready",
            work_item_id,
            task.id,
            reason,
        )
        return True

    async def _resume_task_checkpoint(self, checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        """恢復任務等待檢查點 — 將使用者輸入注入任務後重新執行。"""
        assert self.store
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        payload = checkpoint.payload
        task_id = payload.get("task_id")
        if not task_id:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending task because the task reference is missing."

        task = await self.store.get_task(task_id)
        if not task:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending task because it no longer exists."

        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["user_supplied_input"] = user_reply.strip()
        pause_request = dict(payload.get("pause_request", {}))
        if pause_request:
            task.context_snapshot["requested_user_input"] = pause_request
        self._restore_runtime_state_from_checkpoint(task, payload)
        task.status = TaskStatus.PENDING
        task.result = None
        task.metadata = dict(task.metadata)
        progress = list(task.metadata.get("progress_log", []))
        progress.append(f"Resumed with user input: {user_reply.strip()}")
        task.metadata["progress_log"] = progress
        await self.store.save_task(task)

        # Sibling ids persisted by older checkpoints can be work-item ids rather
        # than task UUIDs; unresolvable entries are skipped, but the primary
        # task must always be part of the resumed set so the resume can never
        # degenerate into executing an empty task list (which used to return an
        # empty reply and silently swallow the user's message).
        tasks: list[Task] = [task]
        for sibling_id in payload.get("task_ids", [task_id]):
            if str(sibling_id) == str(task_id):
                continue
            sibling = await self.store.get_task(sibling_id)
            if not sibling:
                logger.warning(
                    f"Checkpoint {checkpoint.checkpoint_id} references unknown sibling task {sibling_id!r}; skipping it"
                )
                continue
            if sibling.status == TaskStatus.BLOCKED:
                sibling.status = TaskStatus.PENDING
                await self.store.save_task(sibling)
            tasks.append(sibling)

        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")

        raw_execution_mode = str(payload.get("execution_mode", ExecutionMode.SINGLE_AGENT.value))
        try:
            # MULTI_AGENT is a value alias of COMPANY_MODE, so normalizing to the
            # enum collapses both onto one branch instead of letting the legacy
            # multi-agent branch shadow the company-mode one.
            execution_mode = ExecutionMode(raw_execution_mode)
        except ValueError:
            execution_mode = ExecutionMode.SINGLE_AGENT
        # Work-item runtime tasks must resume through the delegation state
        # machine, never through a detached single-agent re-run: the recorded
        # execution_mode is volatile task metadata and has been observed to
        # degrade to task_mode after a first resume, which detaches the re-run
        # from the work item and leaves it parked in awaiting_human forever.
        is_work_item_task = bool(
            is_work_item_runtime_metadata(task.metadata) or linked_work_item_id_for_task(task)
        )
        if execution_mode == ExecutionMode.COMPANY_MODE or is_work_item_task:
            if is_work_item_task:
                await self._release_work_item_human_wait(task, reason="approval_resume")
            # Re-register child tasks so WSHandler can dual-route progress
            # events from child work items to the parent session channel.
            self._reregister_company_runtime_children(tasks, checkpoint_session_id=checkpoint.session_id)
            plan_data = payload.get("company_work_item_plan") or task.metadata.get("company_work_item_plan")
            if isinstance(plan_data, dict) and plan_data:
                return await self._execute_company_mode(tasks, deserialize_company_work_item_runtime_plan(plan_data))
            if is_work_item_task:
                parent_session_id = str(
                    getattr(task, "parent_session_id", "")
                    or task.metadata.get("parent_session_id", "")
                    or checkpoint.session_id
                    or ""
                ).strip()
                snapshot = await self._load_company_runtime_snapshot(parent_session_id)
                if snapshot is not None:
                    snapshot_plan, _snapshot_tasks = snapshot
                    logger.info(
                        f"Resuming company-mode checkpoint {checkpoint.checkpoint_id} via runtime "
                        f"snapshot for parent session {parent_session_id}"
                    )
                    return await self._execute_company_mode(tasks, snapshot_plan)
            logger.info(
                f"Resuming company-mode checkpoint {checkpoint.checkpoint_id} without a runtime plan; "
                f"re-running the paused task {task.id} directly"
            )
        return await self._execute_single_agent([task], task.assigned_external_agent)

    async def _resume_peer_checkpoint(self, checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        """恢復同儕等待檢查點 — 解析同儕等待後重新執行任務。"""
        assert self.store and self.communication
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        payload = checkpoint.payload
        task_id = payload.get("task_id") or payload.get("waiting_task_id")
        if not task_id:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending peer wait because the task reference is missing."
        task = await self.store.get_task(task_id)
        if not task:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending peer wait because the task no longer exists."
        if task.status != TaskStatus.AWAITING_PEER:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        else:
            resolved = False
            wait = dict(task.metadata.get("peer_wait", {}))
            wait_kind = str(wait.get("kind") or "")
            if wait_kind == "meeting":
                resolved = await self.communication.resolve_task_meeting_wait(task)
            elif wait_kind == "comms_blocking" or not wait:
                # Comms-blocking (and orphaned) waits resolve from durable
                # inbox files owned by the company dispatcher's per-tick
                # unpark. Re-enter the runtime and let it converge: it
                # either releases the park or re-parks and re-checkpoints
                # consistently.
                resolved = True
            else:
                resolved = await self.communication.resolve_task_peer_wait(task)
            if not resolved:
                hint = user_reply.strip()
                if hint:
                    task.context_snapshot = dict(task.context_snapshot)
                    task.context_snapshot["peer_resume_hint"] = hint
                    self._restore_runtime_state_from_checkpoint(task, payload)
                    await self.store.save_task(task)
                return (
                    "There is still a pending peer coordination wait. "
                    "Reply again after the peer has answered, or continue execution so the peer task can respond."
                )
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        self._restore_runtime_state_from_checkpoint(task, payload)
        tasks: list[Task] = []
        for sibling_id in payload.get("task_ids", [task_id]):
            sibling = await self.store.get_task(sibling_id)
            if sibling:
                tasks.append(sibling)
        if not tasks:
            return "Peer wait resolved, but no runtime work-item tasks were available to resume."
        execution_mode = str(payload.get("execution_mode", ExecutionMode.COMPANY_MODE.value))
        if execution_mode == ExecutionMode.COMPANY_MODE.value:
            # Re-register child tasks for WSHandler dual-routing
            self._reregister_company_runtime_children(tasks, checkpoint_session_id=checkpoint.session_id)
            plan_data = payload.get("company_work_item_plan") or task.metadata.get("company_work_item_plan")
            if isinstance(plan_data, dict) and plan_data:
                return await self._execute_company_mode(tasks, deserialize_company_work_item_runtime_plan(plan_data))
        return await self._execute_single_agent([task], task.assigned_external_agent)
