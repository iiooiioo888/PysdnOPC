"""CompanyRuntimeMixin — 公司運行時暫停/恢復/反饋相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger
import asyncio
import json
from contextlib import nullcontext
from datetime import datetime

from opc.core.active_task_runs import ActiveTaskRunAdmissionClosed
from opc.core.models import ExecutionCheckpoint, Task, TaskStatus
from opc.engine._core import (
    COMPANY_FEEDBACK_ATTRIBUTION_PROMPT,
    _COMPANY_RUNTIME_CONTROL_METADATA_KEYS,
    _COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES,
)
from opc.layer2_organization.company_mode import (
    CompanyExecutorDriverOwnership,
    CompanyWorkItemExecutor,
    deserialize_company_work_item_runtime_plan,
    serialize_company_work_item_runtime_plan,
)
from opc.layer2_organization.company_runtime_identity import load_company_runtime_identity_index
from opc.layer2_organization.org_work_item_planner import CompanyWorkItemRuntimePlan
from opc.layer2_organization.phase import DONE_PHASES
from opc.layer2_organization.work_item_identity import (
    projection_id_for_task,
    rework_projection_id_for_gate,
    work_item_identity_payload_for_task,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task

if TYPE_CHECKING:
    from opc.engine._core import OPCEngine


class CompanyRuntimeMixin:
    """Mixin providing 公司運行時暫停/恢復/反饋相關方法 for OPCEngine."""

    async def _mark_company_runtime_checkpoint_status(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        status: str,
        payload_updates: dict[str, Any] | None = None,
        expected_statuses: set[str] | None = None,
    ) -> bool:
        """更新公司運行時檢查點狀態（支援 compare-and-set 原子轉換）。"""
        if not self.store:
            return False
        payload = {**dict(checkpoint.payload or {}), **dict(payload_updates or {})}
        updated_at = datetime.now()
        expected = {
            str(item).strip()
            for item in set(expected_statuses or set())
            if str(item).strip()
        }
        compare_and_set = getattr(
            self.store,
            "compare_and_set_execution_checkpoint",
            None,
        )
        if expected and callable(compare_and_set):
            transitioned = await compare_and_set(
                checkpoint.checkpoint_id,
                expected_statuses=expected,
                status=status,
                payload=payload,
                updated_at=updated_at,
            )
            if not transitioned:
                return False
        checkpoint.payload = payload
        checkpoint.status = status
        checkpoint.updated_at = updated_at
        save_checkpoint = getattr(self.store, "save_execution_checkpoint", None)
        if callable(save_checkpoint) and not (expected and callable(compare_and_set)):
            await save_checkpoint(checkpoint)
        elif not (expected and callable(compare_and_set)):
            await self.store.resolve_execution_checkpoint(
                checkpoint.checkpoint_id,
                status=status,
            )
        return True

    async def _reset_company_executor_runtime_for_resume(
        self,
        tasks: list[Task],
        payload: dict[str, Any],
    ) -> None:
        """重置公司執行器運行時狀態以準備恢復。"""
        runtime = getattr(getattr(self, "company_executor", None), "runtime", None)
        reset = getattr(runtime, "reset_for_company_runtime_resume", None)
        if callable(reset):
            await reset(tasks, payload=payload)

    async def _load_company_suspend_checkpoint_runtime(
        self,
        checkpoint: ExecutionCheckpoint,
    ) -> tuple[dict[str, Any], str, CompanyWorkItemRuntimePlan, list[Task]] | None:
        """載入公司暫停檢查點的運行時上下文（payload、session、plan、tasks）。"""
        assert self.store
        payload = dict(checkpoint.payload or {})
        parent_session_id = str(
            checkpoint.session_id
            or payload.get("parent_session_id")
            or payload.get("session_id")
            or ""
        ).strip()
        task_ids = [
            str(item).strip()
            for item in list(payload.get("task_ids", []) or [])
            if str(item).strip()
        ]
        tasks: list[Task] = []
        for task_id in task_ids:
            task = await self.store.get_task(task_id)
            if task:
                tasks.append(task)
        if not tasks and parent_session_id:
            snapshot = await self._load_company_runtime_snapshot(parent_session_id)
            if snapshot:
                _plan, tasks = snapshot
        if not tasks:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return None

        plan_data = payload.get("company_work_item_plan") or payload.get("plan") or {}
        plan = deserialize_company_work_item_runtime_plan(plan_data if isinstance(plan_data, dict) else {})
        payload["checkpoint_id"] = checkpoint.checkpoint_id
        payload["checkpoint_type"] = checkpoint.checkpoint_type
        return payload, parent_session_id, plan, tasks

    async def _resolve_company_runtime_ui_anchor_task_id(
        self,
        *,
        parent_session_id: str,
        checkpoint_id: str,
    ) -> str:
        """解析公司運行時的 UI 錨點任務 ID（用於前端定位）。"""
        if not self.store:
            return ""
        identity_index = await load_company_runtime_identity_index(
            self.store,
            self.project_id or "default",
        )
        identity = identity_index.resolve(
            runtime_session_id=parent_session_id,
            checkpoint_id=checkpoint_id,
        )
        return str(getattr(identity, "ui_anchor_task_id", "") or "").strip()

    async def _restore_company_suspend_checkpoint_pending(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        parent_session_id: str,
        tasks: list[Task],
        resume_state: str,
        error: BaseException | str,
    ) -> None:
        """恢復失敗時還原檢查點為 pending — 重新建立持久暫停並發佈事件。"""

        if not self.store:
            return
        project_id = str(self.project_id or "default").strip() or "default"
        async with self._active_task_run_registry.scope_lock(
            project_id,
            parent_session_id,
        ):
            current = await self._load_execution_checkpoint_by_id(
                checkpoint.checkpoint_id
            )
            if current is None or str(current.status or "").strip() == "resolved":
                return
            payload = dict(current.payload or checkpoint.payload or {})
            await self._suspend_company_runtime_tasks(
                tasks,
                reason="resume_failed",
                checkpoint_type=str(
                    current.checkpoint_type
                    or checkpoint.checkpoint_type
                    or "company_runtime_interrupted"
                ),
                stop_intent_id=str(payload.get("stop_intent_id", "") or ""),
            )
            error_text = str(error).strip() or type(error).__name__
            transitioned = await self._mark_company_runtime_checkpoint_status(
                current,
                status="pending",
                payload_updates={
                    "resume_state": resume_state,
                    "resume_failed_at": datetime.now().isoformat(),
                    "resume_error": error_text,
                },
                expected_statuses={"resuming"},
            )
            if not transitioned:
                refreshed = await self._load_execution_checkpoint_by_id(
                    checkpoint.checkpoint_id
                )
                if refreshed is None:
                    return
                current = refreshed
            checkpoint.status = current.status
            checkpoint.payload = dict(current.payload or {})
            checkpoint.updated_at = current.updated_at

    async def _restore_company_suspend_checkpoint_pending_after_cancellation(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        parent_session_id: str,
        tasks: list[Task],
        resume_state: str,
        error: BaseException,
    ) -> None:
        """取消時還原檢查點為 pending（shield 保護異步恢復）。"""
        recovery = asyncio.create_task(
            self._restore_company_suspend_checkpoint_pending(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state=resume_state,
                error=error,
            )
        )
        try:
            await asyncio.shield(recovery)
        except asyncio.CancelledError:
            await recovery

    async def _complete_company_suspend_checkpoint_resume(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        parent_session_id: str,
    ) -> None:
        """完成公司暫停檢查點恢復 — 標記 resolved 並重新開啟 UI 錨點。"""

        if not self.store:
            return
        project_id = str(self.project_id or "default").strip() or "default"
        async with self._active_task_run_registry.scope_lock(
            project_id,
            parent_session_id,
        ):
            current = await self._load_execution_checkpoint_by_id(
                checkpoint.checkpoint_id
            )
            if current is None or str(current.status or "").strip() != "resuming":
                raise RuntimeError(
                    "company runtime resume was interrupted before completion"
                )
            payload = dict(current.payload or {})
            resolved_at = datetime.now()
            resolved_payload = {
                **payload,
                "resume_state": "handoff_complete",
                "resume_handoff_at": resolved_at.isoformat(),
                "resume_resolved_at": resolved_at.isoformat(),
            }
            transitioned = (
                await self.store.complete_execution_checkpoint_and_reopen_ui_anchor(
                    current.checkpoint_id,
                    project_id=project_id,
                    session_id=parent_session_id,
                    expected_status="resuming",
                    status="resolved",
                    payload=resolved_payload,
                    ui_anchor_task_id=str(
                        payload.get("ui_anchor_task_id", "") or ""
                    ).strip(),
                    updated_at=resolved_at,
                )
            )
            if not transitioned:
                raise RuntimeError(
                    "company runtime checkpoint changed during resume completion"
                )
            current.status = "resolved"
            current.payload = resolved_payload
            current.updated_at = resolved_at
            checkpoint.status = current.status
            checkpoint.payload = dict(current.payload or {})
            checkpoint.updated_at = current.updated_at

    async def _handoff_company_suspend_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        payload: dict[str, Any],
        parent_session_id: str,
        tasks: list[Task],
        resume_task_ids: set[str] | None = None,
    ) -> tuple[list[Task], CompanyExecutorDriverOwnership | None] | None:
        """交接公司暫停檢查點 — 原子性地將檢查點從 pending 轉為 resuming 並準備任務。"""
        assert self.company_executor
        if not self.store:
            return None
        project_id = str(self.project_id or "default").strip() or "default"
        claimed = False
        driver_ownership: CompanyExecutorDriverOwnership | None = None
        try:
            async with self._active_task_run_registry.scope_lock(
                project_id,
                parent_session_id,
            ):
                current = await self._load_execution_checkpoint_by_id(
                    checkpoint.checkpoint_id
                )
                if current is None or str(current.status or "").strip() != "pending":
                    return None
                checkpoint.status = current.status
                checkpoint.payload = dict(current.payload or {})
                checkpoint.updated_at = current.updated_at
                ui_anchor_task_id = await self._resolve_company_runtime_ui_anchor_task_id(
                    parent_session_id=parent_session_id,
                    checkpoint_id=checkpoint.checkpoint_id,
                )
                transitioned = await self._mark_company_runtime_checkpoint_status(
                    checkpoint,
                    status="resuming",
                    payload_updates={
                        **payload,
                        "resume_state": "resuming",
                        "resume_started_at": datetime.now().isoformat(),
                        "ui_anchor_task_id": ui_anchor_task_id,
                    },
                    expected_statuses={"pending"},
                )
                if transitioned is False:
                    return None
                claimed = True
                driver_ownership = self._acquire_company_executor_driver_ownership(
                    tasks,
                    preferred_task_ids=resume_task_ids,
                )
                tasks = await self._prepare_company_runtime_tasks_for_resume(
                    tasks,
                    payload,
                    resume_task_ids=resume_task_ids,
                )
                await self._reset_company_executor_runtime_for_resume(tasks, payload)
                await self._clear_company_runtime_parent_stop_state(
                    parent_session_id,
                    payload,
                )
                if parent_session_id:
                    self._reregister_company_runtime_children(
                        tasks,
                        checkpoint_session_id=parent_session_id,
                    )
                notify_kanban_changed = getattr(
                    self.company_executor,
                    "_notify_kanban_changed",
                    None,
                )
                if callable(notify_kanban_changed):
                    # The registry attempt above belongs to this successful
                    # checkpoint handoff, and resume preparation has now
                    # cleared its durable holds.  Publish through the existing
                    # canonical snapshot path so UI control state becomes
                    # running/stoppable without introducing a second
                    # liveness signal in the WS layer.
                    await notify_kanban_changed()
        except asyncio.CancelledError as exc:
            if driver_ownership is not None:
                driver_ownership.release()
            if claimed:
                await self._restore_company_suspend_checkpoint_pending_after_cancellation(
                    checkpoint,
                    parent_session_id=parent_session_id,
                    tasks=tasks,
                    resume_state="failed_before_handoff",
                    error=exc,
                )
            raise
        except Exception as exc:
            if driver_ownership is not None:
                driver_ownership.release()
            if claimed:
                await self._restore_company_suspend_checkpoint_pending(
                    checkpoint,
                    parent_session_id=parent_session_id,
                    tasks=tasks,
                    resume_state="failed_before_handoff",
                    error=exc,
                )
            raise
        return tasks, driver_ownership

    def _acquire_company_executor_driver_ownership(
        self,
        tasks: list[Task],
        *,
        preferred_task_ids: set[str] | None = None,
    ) -> CompanyExecutorDriverOwnership | None:
        """取得公司執行器驅動所有權（註冊到 ActiveTaskRunRegistry 防止並發）。"""
        task = CompanyWorkItemExecutor._driver_ownership_task(
            tasks,
            preferred_task_ids=preferred_task_ids,
        )
        if task is None:
            return None
        project_id = str(task.project_id or self.project_id or "default").strip() or "default"
        try:
            attempt_token = self._active_task_run_registry.register(
                project_id,
                task.id,
            )
        except ActiveTaskRunAdmissionClosed as exc:
            raise asyncio.CancelledError(str(exc)) from exc
        return CompanyExecutorDriverOwnership(
            registry=self._active_task_run_registry,
            project_id=project_id,
            task_id=task.id,
            attempt_token=attempt_token,
        )

    @staticmethod
    def _company_executor_driver_context(
        ownership: CompanyExecutorDriverOwnership | None,
    ):
        """回傳驅動所有權的上下文管理器（無所有權時回傳 nullcontext）。"""
        return ownership.bind() if ownership is not None else nullcontext()

    async def _company_suspend_resume_candidate_task_ids(
        self,
        tasks: list[Task],
        *,
        exclude_task_ids: set[str] | None = None,
    ) -> set[str]:
        """篩選公司暫停恢復的候選任務 ID（仍持有持久暫停標記者）。"""
        if not self.store:
            return set()
        excluded = {str(item).strip() for item in set(exclude_task_ids or set()) if str(item).strip()}
        get_work_item = getattr(self.store, "get_delegation_work_item", None)
        candidate_ids: set[str] = set()
        for task in tasks:
            task_id = str(getattr(task, "id", "") or "").strip()
            if not task_id or task_id in excluded:
                continue
            metadata = dict(getattr(task, "metadata", {}) or {})
            task_is_held = any(str(metadata.get(key, "") or "").strip() for key in _COMPANY_RUNTIME_CONTROL_METADATA_KEYS)
            work_item_id = linked_work_item_id_for_task(task)
            work_item_is_held = False
            work_item = None
            if work_item_id and callable(get_work_item):
                try:
                    work_item = await get_work_item(work_item_id)
                except Exception:
                    work_item = None
                if work_item is not None:
                    if getattr(work_item, "phase", None) in DONE_PHASES:
                        continue
                    work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
                    work_item_is_held = str(work_item_metadata.get("dispatch_hold", "") or "").strip() == "company_runtime_suspended"
            if (
                task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
                and (
                    work_item is None
                    or getattr(work_item, "phase", None) in DONE_PHASES
                )
            ):
                continue
            if task_is_held or work_item_is_held:
                candidate_ids.add(task_id)
        return candidate_ids

    async def _resume_remaining_company_runtime_after_final_decider(
        self,
        *,
        checkpoint: ExecutionCheckpoint,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        payload: dict[str, Any],
        parent_session_id: str,
        final_decider_task_id: str,
    ) -> tuple[bool, str | None]:
        """最終決策者完成後恢復剩餘的公司運行時任務。"""
        assert self.company_executor
        target_progressed = await self._company_followup_target_progressed(
            final_decider_task_id
        )
        project_id = str(self.project_id or "default").strip() or "default"
        driver_ownership: CompanyExecutorDriverOwnership | None = None
        awaiting_final_decider_action = False
        try:
            async with self._active_task_run_registry.scope_lock(
                project_id,
                parent_session_id,
            ):
                current = await self._load_execution_checkpoint_by_id(
                    checkpoint.checkpoint_id
                )
                if (
                    current is None
                    or str(current.status or "").strip() != "resuming"
                    or str(current.project_id or "default").strip() != project_id
                    or str(current.session_id or "").strip() != parent_session_id
                    or str(current.checkpoint_type or "").strip()
                    not in _COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES
                ):
                    logger.info(
                        "company runtime resume: remaining handoff skipped because checkpoint {} "
                        "is no longer the resuming owner of scope {}",
                        checkpoint.checkpoint_id,
                        parent_session_id,
                    )
                    return False, None
                checkpoint.status = current.status
                checkpoint.payload = dict(current.payload or {})
                checkpoint.updated_at = current.updated_at
                refreshed_snapshot = await self._load_company_runtime_snapshot(
                    parent_session_id
                )
                if refreshed_snapshot:
                    plan, tasks = refreshed_snapshot
                resume_task_ids = await self._company_suspend_resume_candidate_task_ids(
                    tasks,
                    exclude_task_ids={final_decider_task_id},
                )
                if not resume_task_ids:
                    return True, None
                if not target_progressed:
                    awaiting_final_decider_action = True
                else:
                    # Acquire the scheduler attempt while the scope lock still
                    # owns the checkpoint.  Stop/shutdown can therefore never
                    # observe the released WorkItems without a live coroutine.
                    driver_ownership = self._acquire_company_executor_driver_ownership(
                        tasks,
                        preferred_task_ids=resume_task_ids,
                    )
                    tasks = await self._prepare_company_runtime_tasks_for_resume(
                        tasks,
                        payload,
                        resume_task_ids=resume_task_ids,
                    )
                    await self._reset_company_executor_runtime_for_resume(tasks, payload)
                    if parent_session_id:
                        self._reregister_company_runtime_children(
                            tasks,
                            checkpoint_session_id=parent_session_id,
                        )
        except BaseException:
            if driver_ownership is not None:
                driver_ownership.release()
            raise
        if awaiting_final_decider_action:
            logger.info(
                "company runtime resume: restoring checkpoint {} to pending because "
                "remaining work is held and final decider has no durable arbitration action",
                checkpoint.checkpoint_id,
            )
            await self._restore_company_suspend_checkpoint_pending(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="awaiting_final_decider_action",
                error="final decider returned without a durable arbitration action",
            )
            return False, None
        try:
            with self._company_executor_driver_context(driver_ownership):
                if self.on_company_runtime_children and parent_session_id and tasks:
                    self.on_company_runtime_children(
                        parent_session_id,
                        [t.id for t in tasks],
                    )
                result = await self.company_executor.execute(plan, tasks)
                return True, result
        finally:
            if driver_ownership is not None:
                driver_ownership.release()

    async def _company_followup_target_progressed(self, task_id: str) -> bool:
        """判斷最終決策者任務是否已記錄持久仲裁動作。"""
        if not self.store:
            return False
        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        task = await self.store.get_task(task_id)
        if task is None:
            return False
        task_metadata = dict(task.metadata or {})
        if bool(task_metadata.get("manager_board_mutation_performed", False)):
            return True
        if str(task_metadata.get("manager_no_delegation_justification", "") or "").strip():
            return True
        if str(task_metadata.get("manager_dispatch_guard_unresolved", "") or "").strip():
            return True
        return False

    async def _resume_company_suspend_checkpoint_via_final_decider(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
    ) -> str:
        """透過最終決策者恢復公司暫停檢查點（先執行 CEO 任務再恢復剩餘）。"""
        assert self.store and self.company_executor
        loaded = await self._load_company_suspend_checkpoint_runtime(checkpoint)
        if loaded is None:
            return "Could not route the suspended company runtime because its task set could not be restored."
        payload, parent_session_id, plan, tasks = loaded
        target_task = self._company_followup_target_task(plan, tasks)
        if target_task is None:
            return "Could not route the suspended company runtime because no CEO/final-decider work item was available."

        handoff = await self._handoff_company_suspend_checkpoint(
            checkpoint,
            payload=payload,
            parent_session_id=parent_session_id,
            tasks=tasks,
            resume_task_ids={target_task.id},
        )
        if handoff is None:
            return (
                "This company runtime is already being resumed by another request."
            )
        tasks, driver_ownership = handoff
        try:
            with self._company_executor_driver_context(driver_ownership):
                followup_result = await self._resume_company_runtime_via_final_decider(
                    plan=plan,
                    tasks=tasks,
                    user_reply=user_reply,
                    session_id=parent_session_id,
                )
                if followup_result is None:
                    await self._restore_company_suspend_checkpoint_pending(
                        checkpoint,
                        parent_session_id=parent_session_id,
                        tasks=tasks,
                        resume_state="failed_during_execution",
                        error="final-decider work item was unavailable",
                    )
                    return (
                        "Could not route the suspended company runtime because the CEO/final-decider "
                        "work item was unavailable after resume handoff."
                    )
                continuation_owned, continuation_result = await self._resume_remaining_company_runtime_after_final_decider(
                    checkpoint=checkpoint,
                    plan=plan,
                    tasks=tasks,
                    payload=payload,
                    parent_session_id=parent_session_id,
                    final_decider_task_id=target_task.id,
                )
                if not continuation_owned:
                    if (
                        str(checkpoint.payload.get("resume_state", "") or "").strip()
                        == "awaiting_final_decider_action"
                    ):
                        return (
                            f"{followup_result}\n\nThe CEO/final decider has not yet recorded "
                            "a durable arbitration action. Remaining work items stay suspended "
                            "and this checkpoint remains available to continue."
                        ).strip()
                    return (
                        f"{followup_result}\n\nThe company runtime was suspended before "
                        "remaining work items were released; they remain held."
                    ).strip()
                await self._complete_company_suspend_checkpoint_resume(
                    checkpoint,
                    parent_session_id=parent_session_id,
                )
        except asyncio.CancelledError as exc:
            await self._restore_company_suspend_checkpoint_pending_after_cancellation(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="failed_during_execution",
                error=exc,
            )
            raise
        except Exception as exc:
            await self._restore_company_suspend_checkpoint_pending(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="failed_during_execution",
                error=exc,
            )
            raise
        finally:
            if driver_ownership is not None:
                driver_ownership.release()
        if continuation_result:
            return f"{followup_result}\n\nResumed remaining company runtime after CEO/final-decider arbitration.\n\n{continuation_result}".strip()
        return followup_result

    async def _resume_company_suspend_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
    ) -> str:
        """強制恢復公司暫停檢查點 — 直接執行公司執行器完成所有任務。"""
        assert self.store and self.company_executor
        loaded = await self._load_company_suspend_checkpoint_runtime(checkpoint)
        if loaded is None:
            return "Could not resume the suspended company runtime because its task set could not be restored."
        payload, parent_session_id, plan, tasks = loaded
        handoff = await self._handoff_company_suspend_checkpoint(
            checkpoint,
            payload=payload,
            parent_session_id=parent_session_id,
            tasks=tasks,
        )
        if handoff is None:
            return "This company runtime is already being resumed by another request."
        tasks, driver_ownership = handoff
        try:
            with self._company_executor_driver_context(driver_ownership):
                result = await self.company_executor.execute(plan, tasks)
                await self._complete_company_suspend_checkpoint_resume(
                    checkpoint,
                    parent_session_id=parent_session_id,
                )
        except asyncio.CancelledError as exc:
            await self._restore_company_suspend_checkpoint_pending_after_cancellation(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="failed_during_execution",
                error=exc,
            )
            raise
        except Exception as exc:
            await self._restore_company_suspend_checkpoint_pending(
                checkpoint,
                parent_session_id=parent_session_id,
                tasks=tasks,
                resume_state="failed_during_execution",
                error=exc,
            )
            raise
        finally:
            if driver_ownership is not None:
                driver_ownership.release()
        prefix = (
            "Resuming the suspended company runtime"
            if checkpoint.checkpoint_type == "company_runtime_suspended"
            else "Resuming the interrupted company runtime"
        )
        return f"{prefix}.\n\n{result}".strip()

    async def _resume_company_runtime_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
    ) -> str:
        """恢復公司運行時門禁檢查點 — 處理人類審批/拒絕決策後繼續執行。"""
        assert self.store and self.company_executor
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        payload = checkpoint.payload
        waiting_task_id = payload.get("waiting_task_id")
        if not waiting_task_id:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending runtime because the waiting task reference is missing."

        waiting_task = await self.store.get_task(waiting_task_id)
        if not waiting_task:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending runtime because the waiting task no longer exists."
        self._restore_runtime_state_from_checkpoint(waiting_task, payload)

        # Pre-register all child tasks so that any rework progress events
        # emitted below can be dual-routed to the parent session channel.
        _early_tasks: list[Task] = []
        for _tid in payload.get("task_ids", []):
            _t = await self.store.get_task(str(_tid))
            if _t:
                _early_tasks.append(_t)
        if _early_tasks:
            self._reregister_company_runtime_children(_early_tasks, checkpoint_session_id=checkpoint.session_id)

        reply_text = user_reply.strip()
        reply = reply_text.lower()
        approved_tokens = {"y", "yes", "ok", "okay", "approve", "approved", "confirm", "continue", "proceed", "go"}
        denied_tokens = {"n", "no", "deny", "denied", "reject", "rejected", "stop", "cancel", "abort"}
        gate_data = dict(payload.get("gate", {}))
        gate_metadata = dict(gate_data.get("metadata", {}) or {})
        gate_source = str(gate_metadata.get("source", "") or "").strip()
        if reply in approved_tokens:
            waiting_task.status = TaskStatus.DONE
            if gate_source == "gate_harness":
                waiting_task.metadata = dict(waiting_task.metadata)
                waiting_task.metadata.pop("gate_harness_pending_decision", None)
                constraints = [
                    str(item).strip()
                    for item in list(gate_metadata.get("constraints", []) or [])
                    if str(item).strip()
                ]
                if constraints:
                    waiting_task.metadata["gate_harness_constraints"] = constraints
                    waiting_task.metadata["gate_harness_status"] = "passed_with_constraints"
                    waiting_task.metadata["risks"] = list(dict.fromkeys([
                        *list(waiting_task.metadata.get("risks", []) or []),
                        *constraints,
                    ]))
                else:
                    waiting_task.metadata["gate_harness_status"] = "passed"
            progress = list(waiting_task.metadata.get("progress_log", []))
            progress.append(f"Human confirmed via resume message: {reply_text}")
            waiting_task.metadata["progress_log"] = progress
            await self.store.save_task(waiting_task)
            # Emit a visible progress signal so the UI shows the resume actually
            # took effect, instead of leaving the user staring at the same gate
            # card. Without this, "approve" looks like a no-op when the runtime
            # then proceeds silently.
            resume_progress = self._make_task_progress_callback(waiting_task)
            if resume_progress:
                projection_label = projection_id_for_task(waiting_task) or waiting_task.title
                await resume_progress(
                    f"[Company:{projection_label}] human approved gate; resuming runtime"
                )
        else:
            rejection_feedback = ""
            if reply in denied_tokens:
                rejection_feedback = reply_text
            elif reply_text:
                rejection_feedback = reply_text
            else:
                return (
                    "There is a pending runtime waiting for confirmation. "
                    "Reply with `approve` / `continue` to proceed, or `deny` / `stop` to halt it."
                )
            progress = list(waiting_task.metadata.get("progress_log", []))
            progress.append(f"Human review feedback via resume message: {rejection_feedback}")
            waiting_task.metadata["progress_log"] = progress
            if gate_source == "gate_harness":
                waiting_task.metadata = dict(waiting_task.metadata)
                waiting_task.metadata.pop("gate_harness_pending_decision", None)
            gate = self.company_executor._gate_from_metadata(gate_data)
            rework_projection_id = rework_projection_id_for_gate(gate) if gate else ""
            if gate and gate.on_reject == "rework" and rework_projection_id:
                task_by_projection_id: dict[str, Task] = {waiting_task.id: waiting_task}
                waiting_projection_id = projection_id_for_task(waiting_task)
                if waiting_projection_id:
                    task_by_projection_id[waiting_projection_id] = waiting_task
                for task_id in payload.get("task_ids", []):
                    task = await self.store.get_task(task_id)
                    if not task:
                        continue
                    task_by_projection_id[task.id] = task
                    projection_id = projection_id_for_task(task)
                    if projection_id:
                        task_by_projection_id[projection_id] = task
                rework_task = await self.company_executor.prepare_gate_rework(
                    waiting_task,
                    gate,
                    task_by_projection_id,
                    rejection_feedback,
                )
                if rework_task is None:
                    waiting_task.metadata = dict(waiting_task.metadata)
                    waiting_task.metadata["last_gate_review_feedback"] = rejection_feedback
                    await self._fail_task_via_phase(
                        waiting_task,
                        reason="gate_rework_restore_failed",
                    )
                    await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
                    return (
                        f"Runtime halted after human rejection for work item `{waiting_task.title}` because "
                        f"the configured rework projection `{rework_projection_id}` could not be restored."
                    )
                if rework_task is not waiting_task:
                    await self.store.save_task(rework_task)
                await self.store.save_task(waiting_task)
            else:
                waiting_task.metadata = dict(waiting_task.metadata)
                waiting_task.metadata["last_gate_review_feedback"] = rejection_feedback
                await self._fail_task_via_phase(
                    waiting_task,
                    reason="human_gate_denied",
                )
                await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
                return f"Runtime halted after human denial for work item `{waiting_task.title}`."

        tasks: list[Task] = []
        for task_id in payload.get("task_ids", []):
            task = await self.store.get_task(task_id)
            if task:
                tasks.append(task)

        if not tasks:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending runtime because its task set could not be restored."

        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        return (
            "This checkpoint belongs to a legacy company runtime run. "
            "Legacy runs are available for inspection only and cannot be resumed."
        )

    async def _resume_company_feedback_checkpoint(self, checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        assert self.store and self.company_executor
        reply = str(user_reply or "").strip()
        if not reply:
            return "There is a pending delivery self-evolution review. Use the review card to fully agree, ignore, or send feedback."
        normalized = reply.lower()
        if normalized in {"ignore", "ignored", "skip"}:
            return await self.ignore_company_delivery_feedback_checkpoint(checkpoint)
        action = "approve" if normalized in {"approve", "approved", "i approve this delivery.", "fully agree"} else "feedback"
        return await self.run_company_delivery_self_evolution_checkpoint(
            checkpoint,
            action=action,
            feedback=reply if action == "feedback" else "",
        )

    async def _save_company_feedback_followup_checkpoint(
        self,
        task: Task,
        tasks: list[Task],
        plan: CompanyWorkItemRuntimePlan,
    ) -> None:
        """儲存公司交付回饋追蹤檢查點（供使用者後續approve/feedback/ignore）。"""
        if self.company_executor and hasattr(self.company_executor, "_save_feedback_checkpoint"):
            self.company_executor._active_plan = plan
            self.company_executor._active_tasks = tasks
            await self.company_executor._save_feedback_checkpoint(task)  # type: ignore[attr-defined]
            return
        result_content = ""
        if isinstance(task.result, dict):
            result_content = str(task.result.get("content", "") or "").strip()
        elif task.result:
            result_content = str(task.result or "").strip()
        context_snapshot = dict(task.context_snapshot or {})
        output_metadata = dict(context_snapshot.get("work_item_owned_outputs", {}) or {})
        delivery_package = output_metadata.get("delivery_package") or task.metadata.get("delivery_package") or {}
        await self._save_execution_checkpoint(
            {
                "project_id": task.project_id,
                "session_id": task.session_id,
                "checkpoint_type": "company_delivery_feedback",
                "task_id": task.id,
                "payload": {
                    "waiting_task_id": task.id,
                    "waiting_work_item_id": linked_work_item_id_for_task(task),
                    "task_ids": [item.id for item in tasks],
                    "feedback_scope": str(task.metadata.get("feedback_scope", "") or "work_item").strip() or "work_item",
                    "prompt": (
                        str(task.metadata.get("feedback_followup_message", "") or "").strip()
                        or "Use this card only to record full agreement, ignore, or feedback for employee self-evolution."
                    ),
                    "review_level": "human",
                    "review_target_role_id": "owner",
                    "review_chain_role_ids": [],
                    "delivery_revision": task.metadata.get("delivery_revision", ""),
                    "owner_directive_revision": task.metadata.get("owner_directive_revision", ""),
                    "latest_user_directive": str(task.metadata.get("latest_user_directive", "") or "").strip(),
                    "result_content": result_content,
                    "delivery_package": delivery_package if isinstance(delivery_package, dict) else {},
                    "company_work_item_plan": serialize_company_work_item_runtime_plan(plan),
                    **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                },
            }
        )

    async def _evaluate_company_feedback(
        self,
        delivery_task: Task,
        work_item_tasks: list[Task],
        feedback: dict[str, Any],
    ) -> dict[str, Any]:
        """評估公司回饋 — 使用 LLM 將使用者回饋歸因到具體員工和工作項目。"""
        fallback = self._fallback_company_feedback_evaluation(work_item_tasks, feedback)
        if not self.llm:
            return fallback

        employees_payload: list[dict[str, Any]] = []
        seen_employee_ids: set[str] = set()
        for task in work_item_tasks:
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            employee_id = str(assignment.get("employee_id", "")).strip()
            if not employee_id or employee_id in seen_employee_ids:
                continue
            seen_employee_ids.add(employee_id)
            history = ""
            if self.memory:
                organization_id = str(getattr(getattr(self.config, "org", None), "organization_id", "") or "").strip()
                history = self.memory.employee_evolution.build_employee_delta_context(
                    employee_id,
                    project_id=task.project_id,
                    organization_id=organization_id or None,
                )
            employees_payload.append(
                {
                    "employee_id": employee_id,
                    "employee_name": assignment.get("name", ""),
                    "role_id": assignment.get("role_id") or task.assigned_to,
                    "history": history,
                }
            )

        task_payload = []
        for task in work_item_tasks:
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            work_item_summary = str(task.metadata.get("work_item_summary_for_downstream", "") or "").strip()
            if not work_item_summary and task.result:
                work_item_summary = str(task.result.get("content", "") or "").strip()
            task_payload.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                    "projection_id": projection_id_for_task(task),
                    "work_item_projection_title": task.title,
                    "employee_id": assignment.get("employee_id", ""),
                    "employee_name": assignment.get("name", ""),
                    "role_id": assignment.get("role_id") or task.assigned_to,
                    "status": getattr(task.status, "value", str(task.status)),
                    "summary": work_item_summary,
                    "work_item_feedback": list(task.metadata.get("feedback_records", [])),
                }
            )

        prompt = {
            "project_id": delivery_task.project_id,
            "feedback_scope": feedback.get("scope", "final"),
            "user_feedback": feedback,
            "delivery_projection_id": projection_id_for_task(delivery_task),
            **work_item_identity_payload_for_task(delivery_task, fallback_turn_type=""),
            "work_item_tasks": task_payload,
            "employees": employees_payload,
        }
        try:
            raw = await self.llm.simple_chat(
                prompt=json.dumps(prompt, ensure_ascii=False),
                system=COMPANY_FEEDBACK_ATTRIBUTION_PROMPT,
                task_type="quick_tasks",
            )
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            data = json.loads(text)
            if not isinstance(data, dict):
                return fallback
            employees = data.get("employees", [])
            if not isinstance(employees, list):
                data["employees"] = []
            data.setdefault("overall_outcome", fallback["overall_outcome"])
            data.setdefault("summary", fallback["summary"])
            data.setdefault("strengths", fallback["strengths"])
            data.setdefault("weaknesses", fallback["weaknesses"])
            return data
        except Exception as exc:
            logger.debug(f"Company feedback evaluation failed: {exc}")
            return fallback

    def _fallback_company_feedback_evaluation(self, work_item_tasks: list[Task], feedback: dict[str, Any]) -> dict[str, Any]:
        """LLM 不可用時的簡化回饋評估（將整體結果歸因到所有員工）。"""
        label = str(feedback.get("label", "")).strip()
        if label == "fully_approved":
            overall_outcome = "success"
        elif label == "fully_rejected":
            overall_outcome = "failure"
        else:
            overall_outcome = "partial_success"
        employees: list[dict[str, Any]] = []
        seen_employee_ids: set[str] = set()
        for task in work_item_tasks:
            assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            employee_id = str(assignment.get("employee_id", "")).strip()
            if not employee_id or employee_id in seen_employee_ids:
                continue
            seen_employee_ids.add(employee_id)
            employees.append(
                {
                    "employee_id": employee_id,
                    "outcome": overall_outcome,
                    "reason": "Fallback attribution based on the overall user feedback.",
                    "strengths": [],
                    "weaknesses": [],
                }
            )
        return {
            "overall_outcome": overall_outcome,
            "summary": str(feedback.get("raw_feedback", "")).strip(),
            "strengths": [],
            "weaknesses": [],
            "employees": employees,
        }

    def _runtime_topology_from_tasks(self, tasks: list[Task], waiting_task: Task) -> dict[str, Any]:
        """從任務 metadata 中提取運行時拓撲資訊。"""
        for task in [waiting_task, *list(tasks or [])]:
            topology = dict(getattr(task, "metadata", {}).get("runtime_topology", {}) or {})
            if topology:
                return topology
        return {}

    @staticmethod
    def _runtime_seat_for_role(runtime_topology: dict[str, Any], role_id: str) -> dict[str, Any]:
        """從運行時拓撲中取得指定角色的席位資料。"""
        role = str(role_id or "").strip()
        for seat in list(runtime_topology.get("seats", []) or []):
            seat_data = dict(seat or {})
            if str(seat_data.get("role_id", "") or "").strip() == role:
                return seat_data
        return {}

    @staticmethod
    def _task_runtime_value(tasks: list[Task], key: str, default: str = "") -> str:
        """從任務列表中取得第一個非空的指定 metadata 值。"""
        for task in list(tasks or []):
            value = str(getattr(task, "metadata", {}).get(key, "") or "").strip()
            if value:
                return value
        return default
