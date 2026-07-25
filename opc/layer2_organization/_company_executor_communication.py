"""Auto-extracted Mixin for CompanyWorkItemExecutor."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from loguru import logger

from opc.core.config import DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS, DEFAULT_ORGANIZATION_ID
from opc.core.models import (
    CompanyMemberSession,
    DelegationWorkItem,
    Phase,
    Task,
    TaskResult,
    TaskStatus,
    normalize_role_runtime_status,
)
from opc.core.worker_envelope import classify_worker_message, worker_message_is_actionable
from opc.layer2_organization.company_runtime import CompanyRuntime, canonical_role_session_id
from opc.layer2_organization.phase import (
    DONE_PHASES,
    IN_PROGRESS_PHASES,
    TODO_PHASES,
    kanban_column,
    should_hide_work_item_from_company_kanban,
    task_status_for_phase,
)
from opc.layer2_organization.collaboration_service import CollaborationContext
from opc.layer2_organization.phase_hooks import reconcile_role_serial_queues
from opc.layer2_organization.session_scoping import task_session_scope_id
from opc.layer2_organization.turn_mode import reset_manager_dispatch_turn_metadata
from opc.layer2_organization.gate_harness import GateHarness, GateHarnessDecision
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.prompt_contract import (
    has_prompt_contract,
    make_prompt_contract,
)
from opc.layer2_organization.seat_executor import SeatExecutor
from opc.layer2_organization.work_item_transition import (
    transition_work_item_from_task,
)
from opc.layer2_organization.work_item_identity import (
    canonical_work_item_turn_type_for_kind,
    mark_work_item_projection,
)
from opc.layer2_organization.work_item_links import (
    linked_work_item_id_for_task,
)
from opc.layer2_organization.work_item_runtime import (
    mark_work_item_runtime,
    work_item_runtime_version,
)
from opc.layer4_tools.output_budget import clip_text
from opc.llm.retry import LLMRetryError, call_llm_json_with_retry

from opc.layer2_organization._company_mode_shared import (  # noqa: E402
    CEO_PRE_DELIVERY_ASSESSMENT_PROMPT,
    DEFAULT_MAX_PRE_DELIVERY_REWORKS,
    DEFAULT_MAX_REVIEW_REWORKS,
    EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT,
    MAX_VERDICT_PARSE_RETRIES,
    _CANONICAL_COORDINATION_SIGNALS,
    _COMPANY_RUNTIME_CONTROL_TASK_METADATA_KEYS,
    _DEFAULT_CONTRACT_REWORK_MAX_RETRIES,
    _DEFAULT_DATA_ACQUISITION_EXECUTION_RECORD_PATH,
    _DEFAULT_DATA_ACQUISITION_LOG_PATH,
    _DEFAULT_DATA_ACQUISITION_REPORT_PATH,
    _DEFAULT_WORKSPACE_LAYOUT,
    _HUMAN_WAIT_MAX_STALL_TICKS,
    _MAX_GATE_REVIEW_FEEDBACK_CHARS,
    _NO_DELEGATION_JUSTIFICATION_LINE,
    _REVIEW_VERDICT_PARSE_RETRY_HINT,
    _REVIEW_WAITING_STATUSES,
    _SERIALIZED_PLAN_MARKER_KEYS,
    _STALE_REWORK_TASK_METADATA_KEYS,
    _WAITING_TASK_STATUSES,
    _coerce_company_work_item_runtime_plan,
    _fallback_comms_root,
    deserialize_company_work_item_runtime_plan,
    is_serialized_company_work_item_runtime_plan,
    report_work_item_id_for_attempt,
    review_work_item_id_for_attempt,
    serialize_company_work_item_runtime_plan,
    serialized_company_plan_from_metadata,
)

if TYPE_CHECKING:
    from opc.layer2_organization.company_mode import CompanyWorkItemExecutor


class CompanyExecutorCommunicationMixin:
    """Mixin extracted from CompanyWorkItemExecutor."""

    @staticmethod
    def _multi_team_notification_requires_attention(message: dict[str, Any]) -> bool:
        metadata = dict(message.get("metadata", {}) or {})
        notification_kind = str(
            message.get("notification_kind", "")
            or metadata.get("notification_kind", "")
            or ""
        ).strip().lower()
        semantic_type = str(
            message.get("semantic_type", "")
            or metadata.get("semantic_type", "")
            or ""
        ).strip().lower()
        return notification_kind in {"idle", "blocked", "completion", "status_digest", "task_complete"} or semantic_type in {
            "completion",
            "status_digest",
            "blocker",
        }


    def _multi_team_session_requires_attention(self, session: CompanyMemberSession) -> bool:
        if any(isinstance(item, dict) for item in list(session.protocol_backlog or [])):
            return True
        if any(isinstance(item, dict) and bool(item.get("actionable", True)) for item in list(session.actionable_chat or [])):
            return True
        return any(
            isinstance(item, dict) and self._multi_team_notification_requires_attention(item)
            for item in list(session.notification_backlog or [])
        )


    def _synthetic_inbox_task_exists(self, tasks: list[Task], *, role_id: str, source_message_id: str) -> bool:
        for task in tasks:
            if (
                str(task.assigned_to or "").strip() == role_id
                and bool((task.metadata or {}).get("synthetic_inbox_turn", False))
                and str((task.metadata or {}).get("source_message_id", "") or "").strip() == source_message_id
                and task.status not in {TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.FAILED}
            ):
                return True
        return False


    @staticmethod
    def _unblock_attention_session(session: CompanyMemberSession) -> None:
        """Flip a parked manager session back to ``idle`` so it can claim the
        attention work item we just created/revived.

        ``claim_runnable_tasks`` in :class:`CompanyRuntime` skips sessions
        whose normalized role status is ``"blocked"`` unless a review
        soft-wake is available. When a manager delegates children and parks, ``complete_claim``
        leaves them ``blocked``; without this hook, an attention work item
        queued against that session will never be claimed — the review queue
        stays untouched, the worker's "Review needed" message rots in the
        inbox, and the whole runtime stalls.

        We clear the focused work item too; in the three-state model an
        ``idle`` role must not retain focus. ``prepare_task_for_session`` will
        overwrite the current task/assignment when the attention turn is
        actually claimed.
        """
        current_status = normalize_role_runtime_status(
            session.status,
            session.focused_work_item_id,
        )
        raw_status = str(session.status or "").strip().lower()
        if raw_status and raw_status not in {"idle", "running", "blocked"}:
            session.status = "idle"
            session.resident_status = "idle"
            session.focused_work_item_id = ""
            session.updated_at = datetime.now()
            return
        if current_status != "blocked":
            session.status = current_status
            session.resident_status = current_status
            return
        session.status = "idle"
        session.resident_status = "idle"
        session.focused_work_item_id = ""
        session.updated_at = datetime.now()


    @staticmethod
    def _attention_work_kind_for_session(session: CompanyMemberSession) -> str:
        turn_mode = str(
            session.current_turn_mode
            or dict(session.inbox_state or {}).get("current_turn_mode", "")
            or ""
        ).strip().lower()
        if turn_mode == "deliver_required":
            return "deliver"
        if turn_mode == "synthesize_required":
            return "aggregate"
        if turn_mode == "dispatch_required":
            return "dispatch"
        if turn_mode == "monitor_children":
            return "monitor"
        if turn_mode in {"review_execute", "review_pending"}:
            return "review"
        return "plan"


    @staticmethod
    def _attention_title_for_session(session: CompanyMemberSession, work_kind: str) -> str:
        role_label = str(session.role_id or "seat").strip() or "seat"
        mapping = {
            "deliver": f"Delivery Turn: {role_label}",
            "aggregate": f"Aggregation Turn: {role_label}",
            "dispatch": f"Dispatch Turn: {role_label}",
            "monitor": f"Monitor Children: {role_label}",
            "review": f"Review Turn: {role_label}",
            "plan": f"Attention Turn: {role_label}",
        }
        return mapping.get(work_kind, f"Attention Turn: {role_label}")


    @staticmethod
    def _store_is_ready(store: Any | None) -> bool:
        if store is None:
            return False
        ready = getattr(store, "is_ready", True)
        if callable(ready):
            try:
                return bool(ready())
            except Exception:
                return False
        return bool(ready)


    def _attention_parent_context_metadata(
        self,
        *,
        parent_work_item: DelegationWorkItem | None,
        parent_task: Task | None,
        work_kind: str,
        attention_title: str,
    ) -> dict[str, Any]:
        if parent_work_item is None:
            return {}
        parent_meta = dict(parent_work_item.metadata or {})
        parent_task_meta = dict((parent_task.metadata if parent_task is not None else {}) or {})
        parent_task_snapshot = dict((parent_task.context_snapshot if parent_task is not None else {}) or {})
        parent_work_item_id = str(parent_work_item.work_item_id or "").strip()
        parent_title = str(parent_work_item.title or "").strip()
        parent_summary = str(parent_work_item.summary or parent_meta.get("brief", "") or "").strip()
        latest_directive = str(
            parent_meta.get("latest_user_directive")
            or parent_meta.get("manager_mutation_user_input")
            or parent_task_snapshot.get("user_supplied_input")
            or parent_task_meta.get("latest_user_directive")
            or parent_task_meta.get("manager_mutation_user_input")
            or parent_task_meta.get("user_supplied_input")
            or ""
        ).strip()
        inherited: dict[str, Any] = {
            "attention_business_parent_work_item_id": parent_work_item_id,
            "business_parent_work_item_id": parent_work_item_id,
            "business_parent_title": parent_title,
            "business_parent_summary": parent_summary,
        }
        if latest_directive:
            inherited["latest_user_directive"] = latest_directive
            inherited["manager_mutation_user_input"] = str(
                parent_meta.get("manager_mutation_user_input") or latest_directive
            ).strip()

        parent_contract = dict(parent_meta.get("prompt_contract", {}) or parent_task_meta.get("prompt_contract", {}) or {})
        if has_prompt_contract(parent_contract):
            attention_contract = copy.deepcopy(parent_contract)
            parent_brief = str(
                attention_contract.get("task_brief")
                or parent_summary
                or parent_title
                or ""
            ).strip()
            brief_lines = [
                f"{attention_title}: monitor and reconcile the child board for business parent `{parent_work_item_id}`.",
            ]
            if latest_directive:
                brief_lines.append(
                    "Latest user directive is authoritative for this manager turn: "
                    + clip_text(latest_directive, limit=1000, marker="latest directive truncated").text
                )
            if parent_brief:
                brief_lines.append("Business parent brief: " + parent_brief)
            attention_contract["task_brief"] = "\n\n".join(line for line in brief_lines if line).strip()
            assignment = dict(attention_contract.get("assignment_context", {}) or {})
            upstream = str(assignment.get("upstream_intent_summary", "") or "").strip()
            if latest_directive:
                directive_line = f"Latest user directive: {latest_directive}"
                if directive_line not in upstream:
                    upstream = (
                        directive_line
                        + ("\n\nBusiness parent upstream context: " + upstream if upstream else "")
                    )
            assignment["upstream_intent_summary"] = upstream
            assignment["owned_outcome_kind"] = str(work_kind or assignment.get("owned_outcome_kind") or "monitor").strip()
            attention_contract["assignment_context"] = assignment
            attention_contract["source"] = {
                "kind": "attention_parent_context",
                "parent_work_item_id": parent_work_item_id,
                "attention_work_kind": str(work_kind or "").strip(),
            }
            inherited["prompt_contract"] = attention_contract
        elif parent_summary or parent_title or latest_directive:
            task_brief = parent_summary or parent_title
            if latest_directive:
                task_brief = (
                    f"Latest user directive is authoritative: {latest_directive}"
                    + (f"\n\nBusiness parent brief: {task_brief}" if task_brief else "")
                )
            inherited["prompt_contract"] = make_prompt_contract(
                task_brief=task_brief,
                upstream_intent_summary=(
                    f"Latest user directive: {latest_directive}" if latest_directive else ""
                ),
                owned_outcome_kind=str(work_kind or "monitor").strip() or "monitor",
                source={
                    "kind": "attention_parent_context",
                    "parent_work_item_id": parent_work_item_id,
                    "attention_work_kind": str(work_kind or "").strip(),
                },
            )
        return inherited


    async def _upsert_attention_work_item(
        self,
        *,
        root_task: Task,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
        session: CompanyMemberSession,
        source_message: dict[str, Any],
    ) -> tuple[list[Task], list[DelegationWorkItem]]:
        if not self.store:
            return tasks, work_items
        run_id = str((root_task.metadata or {}).get("delegation_run_id", "") or "").strip()
        seat_id = str(session.seat_id or (session.metadata or {}).get("seat_id", "") or "").strip()
        team_id = str(session.team_id or (session.metadata or {}).get("team_id", "") or "").strip()
        if not run_id or not seat_id or not team_id:
            return tasks, work_items
        work_kind = self._attention_work_kind_for_session(session)
        attention_key = f"{seat_id}:{work_kind}"
        source_message_id = str(source_message.get("msg_id", "") or "").strip()
        summary = str(source_message.get("body", "") or source_message.get("subject", "") or "").strip()
        # Fix 2: resolve session.role_session_id, else build canonical ID
        # from (run_id, role_id, team_instance_id). Never construct a
        # seat-scoped fallback — that was one of the three divergent
        # generator paths that produced duplicate DB rows.
        role_id_for_session = str(
            session.role_id or (session.metadata or {}).get("role_id", "") or ""
        ).strip()
        team_instance_id_for_session = str(
            session.team_instance_id
            or (session.metadata or {}).get("team_instance_id", "")
            or ""
        ).strip()
        role_runtime_session_id = (
            str(session.role_session_id or "").strip()
            or (
                canonical_role_session_id(
                    run_id=run_id,
                    role_id=role_id_for_session,
                    team_instance_id=team_instance_id_for_session,
                )
                if role_id_for_session
                else ""
            )
        )
        seat_state_id = str(session.seat_state_id or f"seat-state::{run_id}::{seat_id}").strip()
        manager_role_id = str(session.manager_role_id or (session.metadata or {}).get("manager_role_id", "") or "").strip()
        manager_seat_id = str((session.metadata or {}).get("manager_seat_id", "") or "").strip()
        current_work_item_id = str(
            session.focused_work_item_id
            or dict(session.current_work_item or {}).get("work_item_id", "")
            or ""
        ).strip()
        current_work_item = next(
            (item for item in work_items if str(item.work_item_id or "").strip() == current_work_item_id),
            None,
        )
        current_task = next(
            (
                task
                for task in tasks
                if linked_work_item_id_for_task(task) == current_work_item_id
            ),
            None,
        )
        current_dependency_ids: list[str] = []
        current_dependencies_done = True
        if current_work_item is not None:
            current_dependency_ids = [
                str(item).strip()
                for item in list((current_work_item.metadata or {}).get("dependency_work_item_ids", []) or [])
                if str(item).strip()
            ]
            work_item_by_id = {item.work_item_id: item for item in work_items}
            current_dependencies_done = all(
                (work_item_by_id.get(dep_id).phase if work_item_by_id.get(dep_id) is not None else None) == Phase.APPROVED
                for dep_id in current_dependency_ids
            )
            if work_kind in {"deliver", "aggregate"} and current_dependency_ids and not current_dependencies_done:
                return tasks, work_items
        attention_inherited_metadata = self._attention_parent_context_metadata(
            parent_work_item=current_work_item,
            parent_task=current_task,
            work_kind=work_kind,
            attention_title=self._attention_title_for_session(session, work_kind),
        )
        if current_work_item is not None and current_task is not None:
            if not current_dependency_ids or current_dependencies_done:
                target_phase = current_work_item.phase
                if current_work_item.phase in {Phase.WAITING_DEPENDENCIES, Phase.WAITING_FOR_CHILDREN, Phase.PAUSED, Phase.NEEDS_ATTENTION}:
                    target_phase = (
                        Phase.RUNNING
                        if current_work_item.phase in IN_PROGRESS_PHASES
                        else Phase.READY
                    )
                    await self.store.update_delegation_work_item(
                        current_work_item.work_item_id,
                        phase=target_phase,
                        metadata_updates={
                            "last_attention_source_message_id": source_message_id,
                            "last_attention_at": datetime.now().isoformat(),
                        },
                    )
                if current_task.status in {TaskStatus.BLOCKED, TaskStatus.AWAITING_PEER, TaskStatus.IDLE}:
                    # Phase A: sync local task.status to match the phase the
                    # hook just projected. Hardcoded PENDING was a latent bug
                    # when target_phase was RUNNING (work_item was in
                    # IN_PROGRESS_PHASES) — the subsequent save_task would
                    # overwrite the hook's task.status=RUNNING with PENDING.
                    current_task.status = task_status_for_phase(target_phase)
                    current_task.metadata = dict(current_task.metadata or {})
                    current_task.metadata["message_priority"] = "seat_attention"
                    await self.save_task(current_task)
                # A manager who parked "blocked" waiting on children becomes
                # eligible again the moment they receive actionable mail
                # (review request, completion update, blocker). Without
                # this explicit unblock, ``claim_runnable_tasks`` skips
                # "blocked" sessions and the attention work item we just
                # resumed never gets claimed — the manager silently never
                # comes back to review children.
                self._unblock_attention_session(session)
                # Push the resumed attention target to the UI immediately.
                await self._notify_kanban_changed()
                return tasks, await self.store.list_delegation_work_items(run_id)
        attention_work_item = next(
            (
                item
                for item in work_items
                if str(item.seat_id or "").strip() == seat_id
                and bool(dict(item.metadata or {}).get("attention_work_item", False))
                and str(dict(item.metadata or {}).get("attention_key", "") or "").strip() == attention_key
                and item.phase not in DONE_PHASES
            ),
            None,
        )
        target_phase: Phase | None = None
        if attention_work_item is None:
            attention_projection_id = f"attention::{seat_id}::{work_kind}::{uuid.uuid4().hex[:8]}"
            attention_work_item = DelegationWorkItem(
                run_id=run_id,
                cell_id=team_id,
                team_instance_id=str(session.team_instance_id or "").strip(),
                team_id=team_id,
                role_id=str(session.role_id or "").strip(),
                seat_id=seat_id,
                seat_state_id=seat_state_id,
                role_runtime_session_id=role_runtime_session_id,
                parent_work_item_id=current_work_item_id or None,
                source_role_id=str(source_message.get("from_agent", "") or "").strip() or None,
                title=self._attention_title_for_session(session, work_kind),
                summary=summary,
                kind=work_kind,
                projection_id=attention_projection_id,
                phase=Phase.READY,
                batch_id=f"attention::{run_id}::{seat_id}",
                batch_index=0,
                manager_role_id=manager_role_id,
                manager_seat_id=manager_seat_id,
                metadata=mark_work_item_projection(mark_work_item_runtime({
                    **attention_inherited_metadata,
                    "runtime_model": "multi_team_org",
                    "session_scope_id": str((session.metadata or {}).get("session_scope_id", "") or "").strip(),
                    "delegation_turn_kind": work_kind,
                    "work_kind": work_kind,
                    "team_id": team_id,
                    "seat_id": seat_id,
                    "seat_state_id": seat_state_id,
                    "assigned_role_runtime_id": role_runtime_session_id,
                    "contact_role_ids": list((session.metadata or {}).get("contact_role_ids", []) or []),
                    "allowed_delegate_role_ids": list((session.metadata or {}).get("allowed_delegate_role_ids", []) or []),
                    "attention_work_item": True,
                    "attention_key": attention_key,
                    "attention_source_message_id": source_message_id,
                    "needs_manager_attention": False,
                    "user_visible": False,
                    "authoritative_output": False,
                }, version=work_item_runtime_version(root_task.metadata)),
                    projection_id=attention_projection_id,
                    turn_type=self._runtime_work_kind_to_work_item_turn_type(work_kind),
                ),
            )
            await self.store.save_delegation_work_item(attention_work_item)
        else:
            # Re-trigger an existing attention card: bring it back to a
            # runnable state so the dispatcher will re-spawn the agent loop.
            if attention_work_item.phase == Phase.PAUSED:
                target_phase = Phase.RUNNING
            elif attention_work_item.phase in TODO_PHASES and attention_work_item.phase != Phase.READY:
                target_phase = Phase.READY
            await self.store.update_delegation_work_item(
                attention_work_item.work_item_id,
                phase=target_phase,
                summary=summary or attention_work_item.summary,
                metadata_updates={
                    **attention_inherited_metadata,
                    "attention_source_message_id": source_message_id,
                    "last_attention_source_message_id": source_message_id,
                    "last_attention_at": datetime.now().isoformat(),
                },
            )
            attention_work_item = await self.store.get_delegation_work_item(
                attention_work_item.work_item_id
            ) or attention_work_item
        updated_work_items = await self.store.list_delegation_work_items(run_id)
        updated_tasks = await self._materialize_work_item_tasks(tasks, updated_work_items)
        projected_task = next(
            (
                task
                for task in updated_tasks
                if linked_work_item_id_for_task(task) == attention_work_item.work_item_id
            ),
            None,
        )
        if projected_task is not None and projected_task.status in {TaskStatus.BLOCKED, TaskStatus.AWAITING_PEER, TaskStatus.IDLE}:
            # Phase A: sync from the phase we just wrote (if any). When
            # target_phase is None (work_item stayed at its current phase),
            # materialize already projected the current phase — but the
            # status check above said it's still BLOCKED/AWAITING_PEER/IDLE,
            # meaning materialize projected them. Fall back to PENDING
            # (historical default) in that edge case.
            projected_task.status = (
                task_status_for_phase(target_phase)
                if target_phase is not None
                else TaskStatus.PENDING
            )
            projected_task.metadata = dict(projected_task.metadata or {})
            projected_task.metadata["message_priority"] = "seat_attention"
            await self.save_task(projected_task)
        # Same rationale as the current_work_item branch above: a manager
        # session parked "blocked" after delegating children will be
        # skipped by ``claim_runnable_tasks`` unless we flip it back to
        # "idle" now that a fresh attention work item is queued for them.
        self._unblock_attention_session(session)
        # Push the newly-created attention work item to the UI immediately
        # so reviewers / dispatchers surface on the kanban without waiting
        # for the next gather boundary.
        await self._notify_kanban_changed()
        return updated_tasks, updated_work_items


    async def _queue_multi_team_response_tasks(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> tuple[list[Task], list[DelegationWorkItem]]:
        if not self.store or not tasks:
            return tasks, work_items
        refreshed_tasks = list(tasks)
        root_task = sorted(refreshed_tasks, key=lambda item: (item.created_at, item.id))[0]
        work_item_by_id = {str(item.work_item_id or "").strip(): item for item in work_items if str(item.work_item_id or "").strip()}
        task_by_work_item_id = await self._task_by_work_item_id(refreshed_tasks)
        for session in self.runtime.member_sessions.values():
            session_status = normalize_role_runtime_status(
                session.status,
                session.focused_work_item_id,
            )
            session.status = session_status
            session.resident_status = session_status
            if session_status == "idle":
                session.focused_work_item_id = ""
            if session_status not in {"idle", "blocked"}:
                continue
            if not self._multi_team_session_requires_attention(session):
                continue
            source_message = None
            for bucket_name, bucket in (
                ("protocol", list(session.protocol_backlog or [])),
                ("chat", list(session.actionable_chat or [])),
                ("notification", list(session.notification_backlog or [])),
            ):
                for item in bucket:
                    if not isinstance(item, dict):
                        continue
                    if bucket_name == "notification" and not self._multi_team_notification_requires_attention(item):
                        continue
                    source_message = dict(item)
                    break
                if source_message is not None:
                    break
            if source_message is None:
                continue
            source_message_id = str(source_message.get("msg_id", "") or "").strip()
            refreshed_tasks, work_items = await self._upsert_attention_work_item(
                root_task=root_task,
                tasks=refreshed_tasks,
                work_items=work_items,
                session=session,
                source_message=source_message,
            )
            work_item_by_id = {str(item.work_item_id or "").strip(): item for item in work_items if str(item.work_item_id or "").strip()}
            task_by_work_item_id = await self._task_by_work_item_id(refreshed_tasks)
        return refreshed_tasks, work_items


    @staticmethod
    def _manager_inbox_fingerprint(session: CompanyMemberSession) -> str:
        digest = dict(session.manager_digest or {})
        messages = [
            *(list(digest.get("actionable_chat", []) or [])),
            *(list(digest.get("pending_decisions", []) or [])),
        ]
        if not messages:
            return ""
        normalized = [
            {
                "msg_id": str(item.get("msg_id", "") or item.get("message_id", "") or "").strip(),
                "from_agent": str(item.get("from_agent", "") or "").strip(),
                "subject": str(item.get("subject", "") or "").strip(),
                "body": str(item.get("body", "") or item.get("summary", "") or "").strip()[:240],
                "notification_kind": str(item.get("notification_kind", "") or "").strip(),
            }
            for item in messages
            if isinstance(item, dict)
        ]
        if not normalized:
            return ""
        encoded = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


    @staticmethod
    def _session_mailbox_messages(session: CompanyMemberSession) -> list[dict[str, Any]]:
        return [
            *(list(session.actionable_chat or [])),
            *(list(session.protocol_backlog or [])),
            *(list(session.notification_backlog or [])),
        ]


    @classmethod
    def _mailbox_release_matched(
        cls,
        session: CompanyMemberSession,
        work_item: DelegationWorkItem,
    ) -> tuple[bool, str]:
        metadata = dict(work_item.metadata or {})
        if str(metadata.get("release_policy", "auto") or "auto").strip().lower() != "mailbox_ack":
            return (False, "")
        source_message_id = str(metadata.get("source_message_id", "") or "").strip()
        required_semantic_type = str(metadata.get("release_on_semantic_type", "") or "").strip().lower()
        if not source_message_id and not required_semantic_type:
            return (False, "")
        for item in cls._session_mailbox_messages(session):
            if not isinstance(item, dict):
                continue
            msg_id = str(item.get("msg_id", "") or "").strip()
            semantic_type = str(
                item.get("semantic_type")
                or dict(item.get("metadata", {}) or {}).get("semantic_type")
                or ""
            ).strip().lower()
            if source_message_id and msg_id != source_message_id:
                continue
            if required_semantic_type and semantic_type != required_semantic_type:
                continue
            return (True, msg_id)
        return (False, "")


    def _is_coordinator_role(self, role_cfg: Any, session: CompanyMemberSession | None = None) -> bool:
        role_id = str(getattr(role_cfg, "id", "") or getattr(session, "role_id", "") or "").strip()
        if role_cfg is None and self.org_engine is not None and role_id:
            agent = self.org_engine.get_agent(role_id)
            if agent is not None and list(getattr(agent, "can_spawn", []) or []):
                return True
        if role_cfg is None:
            return False
        explicit_role_type = str(getattr(role_cfg, "role_type", "") or "").strip().lower()
        if explicit_role_type == "coordinator":
            return True
        can_spawn = [str(item).strip() for item in list(getattr(role_cfg, "can_spawn", []) or []) if str(item).strip()]
        if can_spawn:
            return True
        if session is not None:
            current_work_item = dict(session.current_work_item or {})
            work_kind = str(
                current_work_item.get("kind")
                or current_work_item.get("work_kind")
                or ""
            ).strip().lower()
            work_kind = canonical_work_item_turn_type_for_kind(work_kind, fallback=work_kind)
            if work_kind in {"delegate", "dispatch", "monitor", "aggregate", "synthesize", "deliver"}:
                return True
        return False


    def _comms_layout_for_task(self, task: Task):
        """Best-effort comms layout resolution for `task`. Returns None if unavailable.

        Prefers `comms_workspace_root` (workspace root, sibling of
        deliverable folders) over `target_output_dir` (the project's
        deliverable folder itself) so the comms tree never pollutes
        deliverables.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return None
        workspace_root = (
            str(task.metadata.get("comms_workspace_root", "") or "").strip()
            or str(task.metadata.get("workspace_root", "") or "").strip()
            or str(task.metadata.get("target_output_dir", "") or "").strip()
            or str(task.metadata.get("setup_workspace_prepared", "") or "").strip()
        )
        if not workspace_root:
            return None
        project_id = str(task.project_id or "default") or "default"
        session_id = (
            str(task.parent_session_id or "").strip()
            or str(task.session_id or "").strip()
            or "default"
        )
        try:
            return _comms.resolve_layout(workspace_root, project_id, session_id)
        except Exception:
            return None


    def _inject_inbox_into_context(self, task: Task, member_session: CompanyMemberSession | None) -> None:
        """Inject unread inbox messages into the agent's context so it sees
        them without having to poll the mailbox manually."""
        if member_session is None:
            return
        inbox_messages = list(member_session.inbox_state.get("actionable_chat", []) or [])
        if not inbox_messages:
            return
        capped = inbox_messages[:5]
        task.context_snapshot = dict(task.context_snapshot or {})
        task.context_snapshot["injected_inbox"] = capped
        summary_lines = []
        for m in capped:
            if not isinstance(m, dict):
                continue
            from_agent = str(m.get("from_agent", "?"))
            subject = str(m.get("subject", ""))
            body_clip = clip_text(str(m.get("body", "")), limit=200, marker="inbox message preview truncated")
            msg_id = str(m.get("msg_id", "") or m.get("message_id", "") or "").strip()
            id_suffix = f" (msg_id={msg_id})" if msg_id else ""
            summary_lines.append(f"- **[{from_agent}]** {subject}{id_suffix}: {body_clip.text}")
        if summary_lines:
            inbox_section = "\n\n## Messages From Other Teams\n" + "\n".join(summary_lines) + "\n"
            task.description = str(task.description or "") + inbox_section


    @staticmethod
    def _is_attention_work_item(item: Any | None) -> bool:
        return bool(dict(getattr(item, "metadata", {}) or {}).get("attention_work_item", False))


    @staticmethod
    def _work_item_kind(item: Any | None) -> str:
        return str(getattr(item, "kind", "") or "").strip().lower()


    async def _resolve_manager_board_parent_for_task(self, task: Task) -> tuple[str, str]:
        current_work_item_id = linked_work_item_id_for_task(task)
        explicit_parent = str(
            (task.metadata or {}).get("manager_board_parent_work_item_id", "")
            or (task.metadata or {}).get("attention_business_parent_work_item_id", "")
            or ""
        ).strip()
        if explicit_parent:
            current_item = None
            if current_work_item_id and self.store and hasattr(self.store, "get_delegation_work_item"):
                current_item = await self.store.get_delegation_work_item(current_work_item_id)
            return explicit_parent, current_work_item_id if self._is_attention_work_item(current_item) else ""
        if not current_work_item_id or not self.store or not hasattr(self.store, "get_delegation_work_item"):
            return current_work_item_id, ""
        current_item = await self.store.get_delegation_work_item(current_work_item_id)
        if current_item is None or not self._is_attention_work_item(current_item):
            return current_work_item_id, ""
        attention_work_item_id = str(getattr(current_item, "work_item_id", "") or current_work_item_id).strip()
        parent_id = str(getattr(current_item, "parent_work_item_id", "") or "").strip()
        if not parent_id:
            return current_work_item_id, attention_work_item_id
        parent_item = await self.store.get_delegation_work_item(parent_id)
        if (
            parent_item is not None
            and self._work_item_kind(parent_item) in {"deliver", "delivery"}
            and str(getattr(parent_item, "parent_work_item_id", "") or "").strip()
        ):
            return str(getattr(parent_item, "parent_work_item_id", "") or "").strip(), attention_work_item_id
        return parent_id, attention_work_item_id


    async def _inject_manager_board_into_context(
        self,
        task: Task,
        member_session: CompanyMemberSession | None,
    ) -> None:
        if not self.store or not hasattr(self.store, "list_manager_board"):
            return
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        seat_id = str((task.metadata or {}).get("delegation_seat_id", "") or "").strip()
        if not run_id or not seat_id:
            return
        turn_mode = self._manager_dispatch_turn_mode(task, member_session=member_session)
        current_work_item_id = linked_work_item_id_for_task(task)
        current_item = None
        if current_work_item_id and hasattr(self.store, "get_delegation_work_item"):
            current_item = await self.store.get_delegation_work_item(current_work_item_id)
        is_attention_turn = self._is_attention_work_item(current_item)
        if turn_mode not in {"dispatch_required", "monitor_children", "synthesize_required", "deliver_required"} and not is_attention_turn:
            return
        parent_work_item_id, attention_work_item_id = await self._resolve_manager_board_parent_for_task(task)
        if not parent_work_item_id:
            return
        parent_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            parent_item = await self.store.get_delegation_work_item(parent_work_item_id)
        board_items = await self.store.list_manager_board(
            run_id,
            manager_seat_id=seat_id,
            parent_work_item_id=parent_work_item_id,
        )
        board_items = [
            item for item in board_items
            if not should_hide_work_item_from_company_kanban(dict(item.metadata or {}))
            and str((item.metadata or {}).get("upstream_visibility", "") or "").strip().lower() != "hidden"
        ]
        if not board_items and not is_attention_turn:
            return

        def _child_payload(item: DelegationWorkItem) -> dict[str, Any]:
            meta = dict(item.metadata or {})
            deps = [
                str(dep).strip()
                for dep in list(meta.get("dependency_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            return {
                "work_item_id": str(item.work_item_id or "").strip(),
                "role_id": str(item.role_id or "").strip(),
                "title": str(item.title or "").strip(),
                "kind": str(item.kind or "").strip(),
                "phase": item.phase.value,
                "kanban_column": kanban_column(item.phase),
                "scope_key": str(meta.get("scope_key", "") or "").strip(),
                "dependency_work_item_ids": deps,
            }

        child_payloads = [_child_payload(item) for item in board_items]
        counts: dict[str, int] = {}
        for child in child_payloads:
            phase = str(child.get("phase", "") or "").strip()
            counts[phase] = counts.get(phase, 0) + 1
        task.metadata = dict(task.metadata or {})
        task.metadata["manager_board_parent_work_item_id"] = parent_work_item_id
        if attention_work_item_id:
            task.metadata["attention_business_parent_work_item_id"] = parent_work_item_id
            task.metadata["attention_work_item_id"] = attention_work_item_id
        task.context_snapshot = dict(task.context_snapshot or {})
        task.context_snapshot["manager_board_parent_work_item_id"] = parent_work_item_id
        task.context_snapshot["manager_board_attention_work_item_id"] = attention_work_item_id
        if parent_item is not None:
            parent_meta = dict(parent_item.metadata or {})
            parent_latest_for_snapshot = str(
                parent_meta.get("latest_user_directive")
                or parent_meta.get("manager_mutation_user_input")
                or ""
            ).strip()
            task.context_snapshot["manager_board_parent"] = {
                "work_item_id": str(parent_item.work_item_id or "").strip(),
                "role_id": str(parent_item.role_id or "").strip(),
                "title": str(parent_item.title or "").strip(),
                "summary": str(parent_item.summary or "").strip(),
                "kind": str(parent_item.kind or "").strip(),
                "latest_user_directive": parent_latest_for_snapshot,
            }
        task.context_snapshot["manager_board_children"] = child_payloads
        task.context_snapshot["manager_board_phase_counts"] = counts

        lines = [
            "\n\n## Current Manager Board",
            f"Business parent work_item_id: `{parent_work_item_id}`",
        ]
        parent_latest_directive = ""
        if parent_item is not None:
            parent_meta = dict(parent_item.metadata or {})
            parent_latest_directive = str(
                parent_meta.get("latest_user_directive")
                or parent_meta.get("manager_mutation_user_input")
                or ""
            ).strip()
            if parent_latest_directive:
                task.metadata["latest_user_directive"] = parent_latest_directive
                task.context_snapshot["latest_user_directive"] = parent_latest_directive
                parent_mutation_input = str(parent_meta.get("manager_mutation_user_input", "") or "").strip()
                if parent_mutation_input:
                    task.metadata["manager_mutation_user_input"] = parent_mutation_input
            if is_attention_turn:
                inherited = self._attention_parent_context_metadata(
                    parent_work_item=parent_item,
                    parent_task=None,
                    work_kind=self._work_item_kind(current_item) or turn_mode,
                    attention_title=str(task.title or "Attention Turn").strip() or "Attention Turn",
                )
                for key, value in inherited.items():
                    if value not in (None, "", [], {}):
                        task.metadata[key] = value
            parent_title = str(parent_item.title or "").strip()
            parent_summary = str(parent_item.summary or parent_meta.get("brief", "") or "").strip()
            if parent_title:
                lines.append(f"Business parent title: {parent_title}")
            if parent_summary:
                lines.append(
                    "Business parent brief: "
                    + clip_text(parent_summary, limit=600, marker="business parent brief truncated").text
                )
            if parent_latest_directive:
                lines.append(
                    "Latest user directive for this business parent: "
                    + clip_text(parent_latest_directive, limit=800, marker="business parent directive truncated").text
                )
        if attention_work_item_id:
            lines.append(
                f"Current attention work_item_id: `{attention_work_item_id}`. "
                "This turn is a wake-up wrapper; do not treat it as a fresh empty dispatch board."
            )
        if counts:
            counts_text = ", ".join(f"{phase}={count}" for phase, count in sorted(counts.items()))
            lines.append(f"Children by phase: {counts_text}")
        settlement = {}
        if current_item is not None:
            settlement = dict(
                (current_item.metadata or {}).get("dependency_settlement", {}) or {}
            )
        failed_board_items = [
            item for item in board_items
            if getattr(item, "phase", None) in (Phase.FAILED, Phase.CANCELLED)
        ]
        if failed_board_items or settlement:
            lines.append("### Failed or cancelled children (triage needed)")
            for item in failed_board_items[:8]:
                item_meta = dict(item.metadata or {})
                reason = str(item_meta.get("last_transition_reason", "") or "").strip()
                summary_text = str(item.summary or "").strip()
                entry = (
                    f"- `{str(item.work_item_id or '').strip()}` [{item.phase.value}] "
                    f"{str(item.role_id or '').strip()}"
                )
                if reason:
                    entry += f" reason={reason}"
                if summary_text:
                    entry += ": " + clip_text(
                        summary_text, limit=240, marker="failed child summary truncated"
                    ).text
                lines.append(entry)
                preserved = str(item_meta.get("last_turn_preserved_content", "") or "").strip()
                if preserved:
                    lines.append(
                        "  Output preserved from its final turn (not lost): "
                        + clip_text(preserved, limit=700, marker="preserved output truncated").text
                    )
            stuck_ids = [
                str(item).strip()
                for item in list(settlement.get("stuck", []) or [])
                if str(item).strip()
            ]
            if stuck_ids:
                lines.append(
                    "Downstream children blocked by these failures: "
                    + ", ".join(f"`{sid}`" for sid in stuck_ids[:8])
                    + (" ..." if len(stuck_ids) > 8 else "")
                    + ". They are cancelled automatically when this turn completes "
                    "unless you rebuild or rewire them."
                )
            lines.append(
                "You can rebuild the failed work with `delegate_work` (pair it with "
                "`delete_work_item` + `replacement_dependency_work_item_ids` to rewire "
                "dependents), continue with the successful results only, or record the "
                "gap in your handoff so the upper role or user can decide."
            )
        lines.append(
            "Use `manager_board_read` without `parent_work_item_id` to inspect this business board. "
            "Do not call `delegate_work` again for any existing `scope_key`; use `modify_work_item` or `delete_work_item` "
            "for wrong existing children, otherwise review, release, monitor, or synthesize the children below."
        )
        followup_text = str(task.context_snapshot.get("user_supplied_input", "") or "").strip()
        is_final_decider_followup = bool((task.metadata or {}).get("followup_routed_to_final_decider", False)) or bool(followup_text)
        if is_final_decider_followup:
            if followup_text:
                followup_preview = clip_text(
                    followup_text,
                    limit=800,
                    marker="follow-up truncated",
                ).text
                lines.append(f"Latest user follow-up: {followup_preview}")
            lines.append(
                "Reconcile this existing board before continuing: classify current children as keep, revise, delete, "
                "or replace. If `delegate_work` creates a replacement for an obsolete child, also call "
                "`delete_work_item` with `replacement_dependency_work_item_ids` or `modify_work_item` so stale "
                "running work and downstream delivery dependencies do not keep the old direction alive."
            )
        for child in child_payloads[:12]:
            scope = str(child.get("scope_key", "") or "").strip() or "(no scope_key)"
            title = clip_text(str(child.get("title", "") or ""), limit=140, marker="child title truncated").text
            deps = list(child.get("dependency_work_item_ids", []) or [])
            dep_text = f", deps={len(deps)}" if deps else ""
            lines.append(
                f"- `{child['work_item_id']}` [{child['phase']}] "
                f"{child['role_id']} scope=`{scope}`{dep_text}: {title}"
            )
        if len(child_payloads) > 12:
            lines.append(f"- ... {len(child_payloads) - 12} more children omitted; call `manager_board_read` for the full board.")
        task.description = str(task.description or "") + "\n".join(lines) + "\n"


    def _inject_scratchpad_into_context(self, task: Task) -> None:
        """Load shared team scratchpad into the agent's context."""
        layout = self._comms_layout_for_task(task)
        if layout is None:
            return
        scratchpad_path = layout.scratchpad_path
        if not scratchpad_path.exists():
            return
        try:
            scratchpad_text = scratchpad_path.read_text(encoding="utf-8")
            content_clip = clip_text(scratchpad_text, limit=4000, marker="team scratchpad preview truncated")
            content = content_clip.text
        except Exception:
            return
        if content.strip():
            task.context_snapshot = dict(task.context_snapshot or {})
            task.context_snapshot["team_scratchpad"] = content
            task.context_snapshot["team_scratchpad_path"] = str(scratchpad_path)
            task.context_snapshot["team_scratchpad_truncated"] = content_clip.truncated
            task.context_snapshot["team_scratchpad_omitted_chars"] = content_clip.omitted_chars


    def _append_to_scratchpad(self, task: Task, result: TaskResult | None) -> None:
        """Append a completion summary to the shared team scratchpad."""
        layout = self._comms_layout_for_task(task)
        if layout is None:
            return
        shared_dir = layout.shared_root
        try:
            shared_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        scratchpad_path = layout.scratchpad_path
        role = task.assigned_to or task.metadata.get("work_item_role_id", "unknown")
        title = task.title or "Untitled"
        output_preview = ""
        if result and hasattr(result, "output") and result.output:
            output_preview = clip_text(str(result.output), limit=300, marker="scratchpad output preview truncated").text
        task_ref = str(linked_work_item_id_for_task(task) or task.id or "").strip()
        ref_line = f"task_ref={task_ref}\n" if task_ref else ""
        entry = f"\n---\n### [{role}] {title} — DONE\n{ref_line}{output_preview}\n"
        try:
            with open(scratchpad_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            pass


    def _snapshot_inbox_for_turn(self, task: Task) -> None:
        """Record the set of unread message filenames at turn start.

        Used by `_archive_consumed_inbox_snapshot` after the turn ends:
        only files captured in the snapshot get moved to seen/, so mail
        that arrived mid-turn (which the agent never had a chance to
        read) stays as `new` and naturally triggers a follow-up turn.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return
        layout = self._comms_layout_for_task(task)
        role_id = (
            str(task.assigned_to or "").strip()
            or str(task.metadata.get("work_item_role_id", "") or "").strip()
        )
        if layout is None or not role_id:
            return
        try:
            headers = _comms.list_unread(layout, role_id)
        except Exception:
            return
        snapshot = [str(h.path.name) for h in headers]
        task.metadata = dict(task.metadata)
        task.metadata["_comms_turn_inbox_snapshot"] = snapshot


    def _archive_consumed_inbox_snapshot(self, task: Task) -> None:
        """Clear turn-start inbox bookkeeping without marking mail as read.

        Mailbox consumption is now explicit: `reply_message` acknowledges the
        original message, and non-reply work uses `inbox(action="ack")`.
        Prompt injection only means the agent had a chance to see a message;
        it is not a read receipt.
        """
        task.metadata = dict(task.metadata)
        task.metadata.pop("_comms_turn_inbox_snapshot", None)

    # Hard stop: refuse to re-open the same task more than this many times
    # due to inbound comms. A cap well above typical multi-round coordination
    # (~2–3) but below any reasonable runaway. Exceeding this triggers a
    # warning and the task stays DONE so upstream senders time out cleanly.
    COMMS_REACTIVATION_DEPTH_LIMIT = 8
    # Size of the short rolling window used to detect cross-role ping-pong
    # loops (e.g. A→B→A→B…). A window of 20 is plenty to catch the pattern
    # without growing task metadata unboundedly.
    COMMS_CROSS_ROLE_HISTORY_MAX = 20
    # If the same (from_role, subject_hash) appears this many times in the
    # recent history, treat it as a ping-pong loop and refuse reactivation.
    COMMS_CROSS_ROLE_REPEAT_THRESHOLD = 4
    COMMS_REACTIVATION_WARNING_COOLDOWN_SECONDS = 60


    def _resolve_task_inbox_context(
        self,
        task: Task,
    ) -> tuple[str, Any] | None:
        """Return ``(role_id, comms_layout)`` for ``task``'s role, or None.

        Returns None when the task has no role, no workspace, or when the
        comms layout cannot be constructed — in every case the caller should
        skip reactivation. Kept as a shared helper so both the end-of-turn
        hook and the background sweeper follow the exact same resolution
        rules.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return None
        role_id = (
            str(task.assigned_to or "").strip()
            or str(task.metadata.get("work_item_role_id", "") or "").strip()
        )
        if not role_id:
            return None
        workspace_root = (
            str(task.metadata.get("comms_workspace_root", "") or "").strip()
            or str(task.metadata.get("workspace_root", "") or "").strip()
            or str(task.metadata.get("target_output_dir", "") or "").strip()
            or str(task.metadata.get("setup_workspace_prepared", "") or "").strip()
        )
        if not workspace_root:
            return None
        project_id = str(task.project_id or "default") or "default"
        session_id = (
            str(task.parent_session_id or "").strip()
            or str(task.session_id or "").strip()
            or "default"
        )
        try:
            layout = _comms.resolve_layout(workspace_root, project_id, session_id)
        except Exception:
            return None
        return role_id, layout


    def _collect_actionable_unread(
        self,
        layout: Any,
        role_id: str,
        *,
        work_item_id: str = "",
        task_id: str = "",
        require_work_item_scope: bool = False,
    ) -> list[dict[str, Any]]:
        """Scan the role's `inbox/new/` and return classified actionable messages.

        In multi-team company mode, reactivation must be scoped to the
        relevant work item. A role-level inbox can contain messages for many
        child/review cards; treating the whole inbox as actionable for every
        DONE task cross-contaminates review state and triggers false
        ping-pong warnings.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return []
        try:
            unread_headers = _comms.list_unread(layout, role_id)
        except Exception:
            return []
        actionable: list[dict[str, Any]] = []
        for header in unread_headers:
            frontmatter = dict(getattr(header, "raw_frontmatter", {}) or {})
            if not self._unread_header_matches_scope(
                frontmatter,
                work_item_id=work_item_id,
                task_id=task_id,
                require_work_item_scope=require_work_item_scope,
            ):
                continue
            classified = classify_worker_message(
                {
                    "msg_id": str(getattr(header, "message_id", "") or "").strip(),
                    "msg_type": str(frontmatter.get("msg_type", "") or "question"),
                    "from_agent": str(getattr(header, "from_role", "") or "").strip(),
                    "subject": str(getattr(header, "subject", "") or "").strip(),
                    "reply_needed": bool(getattr(header, "blocking", False)),
                    "urgency": str(getattr(header, "priority", "") or "").strip() or "normal",
                    "task_id": str(frontmatter.get("task_id", "") or "").strip(),
                    "metadata": frontmatter,
                    "transport_kind": str(frontmatter.get("transport_kind", "") or "").strip(),
                    "semantic_type": str(frontmatter.get("semantic_type") or frontmatter.get("kind") or "").strip(),
                }
            )
            if worker_message_is_actionable(classified):
                actionable.append(classified)
        return actionable


    async def _block_completion_for_unread_inbox(self, task: Task) -> bool:
        """Hold completion when the current role has unacknowledged actionable mail."""
        context = self._resolve_task_inbox_context(task)
        if context is None:
            return False
        role_id, layout = context
        work_item_id = linked_work_item_id_for_task(task)
        multi_team_org = str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org"
        actionable_unread = self._collect_actionable_unread(
            layout,
            role_id,
            work_item_id=work_item_id,
            task_id=str(task.id or "").strip(),
            require_work_item_scope=bool(multi_team_org and work_item_id),
        )
        if not actionable_unread:
            task.metadata = dict(task.metadata or {})
            task.metadata.pop("inbox_gate_pending_message_ids", None)
            task.context_snapshot = dict(task.context_snapshot or {})
            task.context_snapshot.pop("inbox_completion_gate", None)
            return False

        pending_ids = [
            str(item.get("msg_id", "") or item.get("message_id", "") or "").strip()
            for item in actionable_unread
            if str(item.get("msg_id", "") or item.get("message_id", "") or "").strip()
        ]
        summaries = [
            {
                "msg_id": str(item.get("msg_id", "") or item.get("message_id", "") or "").strip(),
                "from_agent": str(item.get("from_agent", "") or "").strip(),
                "subject": str(item.get("subject", "") or "").strip(),
                "reply_needed": bool(item.get("reply_needed", False)),
                "urgency": str(item.get("urgency", "") or "normal").strip() or "normal",
                "message_class": str(item.get("message_class", "") or "").strip(),
                "protocol_type": str(item.get("protocol_type", "") or "").strip(),
            }
            for item in actionable_unread[:8]
        ]
        task.metadata = dict(task.metadata or {})
        task.context_snapshot = dict(task.context_snapshot or {})
        task.metadata["inbox_gate_pending_message_ids"] = pending_ids
        task.metadata["inbox_gate_blocked_at"] = datetime.now().isoformat()
        task.context_snapshot["inbox_completion_gate"] = {
            "reason": "Actionable inbox messages must be replied to or acknowledged before this work item can complete.",
            "pending_message_ids": pending_ids,
            "messages": summaries,
        }
        await self._append_progress(
            task,
            "Inbox completion gate blocked finish: "
            f"{len(pending_ids)} actionable unread message(s) require reply or `inbox(action=\"ack\")`.",
        )
        await transition_work_item_from_task(
            self.store,
            task,
            target_status_or_phase=TaskStatus.PENDING,
            reason="inbox_completion_gate",
            metadata_updates={
                "inbox_gate_pending_message_ids": pending_ids,
                "inbox_gate_blocked_at": task.metadata["inbox_gate_blocked_at"],
            },
        )
        await self.save_task(task)
        return True


    @staticmethod
    def _work_item_refs_from_frontmatter(frontmatter: dict[str, Any]) -> set[str]:
        refs = {
            str(frontmatter.get("target_work_item_id", "") or "").strip(),
            str(frontmatter.get("source_work_item_id", "") or "").strip(),
            str(frontmatter.get("work_item_id", "") or "").strip(),
        }
        metadata = dict(frontmatter.get("metadata", {}) or {})
        refs.update(
            {
                str(metadata.get("target_work_item_id", "") or "").strip(),
                str(metadata.get("source_work_item_id", "") or "").strip(),
                str(metadata.get("work_item_id", "") or "").strip(),
            }
        )
        nested_refs = dict(frontmatter.get("refs", {}) or {})
        refs.update(
            {
                str(nested_refs.get("target_work_item_id", "") or "").strip(),
                str(nested_refs.get("source_work_item_id", "") or "").strip(),
                str(nested_refs.get("work_item_id", "") or "").strip(),
            }
        )
        return {item for item in refs if item}


    @classmethod
    def _unread_header_matches_scope(
        cls,
        frontmatter: dict[str, Any],
        *,
        work_item_id: str = "",
        task_id: str = "",
        require_work_item_scope: bool = False,
    ) -> bool:
        target_work_item_id = str(work_item_id or "").strip()
        target_task_id = str(task_id or "").strip()
        refs = cls._work_item_refs_from_frontmatter(frontmatter)
        if target_work_item_id:
            if refs:
                return target_work_item_id in refs
            if require_work_item_scope:
                legacy_task_id = str(frontmatter.get("task_id", "") or "").strip()
                return bool(target_task_id and legacy_task_id == target_task_id)
            return True
        if require_work_item_scope:
            return False
        if target_task_id:
            legacy_task_id = str(frontmatter.get("task_id", "") or "").strip()
            return not legacy_task_id or legacy_task_id == target_task_id
        return True


    @staticmethod
    def _subject_hash(subject: str) -> str:
        """Stable short hash for ping-pong detection on (from_role, subject)."""
        normalized = (subject or "").strip().lower()
        if not normalized:
            return ""
        digest = hashlib.sha1(normalized.encode("utf-8", errors="ignore"))
        return digest.hexdigest()[:10]


    def _reactivation_blocked_by_history(
        self,
        task: Task,
        actionable_unread: list[dict[str, Any]],
        unread_fingerprint: list[str],
    ) -> str | None:
        """Return a skip reason when loop-protection rejects reactivation.

        Three guards, evaluated in order:
          1. Same unread fingerprint as the previous reactivation → no new
             information, nothing would change if we re-ran the agent.
          2. Hard depth cap — stop re-opening the same task after
             ``COMMS_REACTIVATION_DEPTH_LIMIT`` rounds.
          3. Cross-role ping-pong — if ``(from_role, subject_hash)`` has
             appeared ≥ ``COMMS_CROSS_ROLE_REPEAT_THRESHOLD`` times in the
             recent rolling window, treat it as a loop.
        """
        last_fingerprint = sorted(
            {
                str(item).strip()
                for item in list(task.metadata.get("comms_last_reactivation_fingerprint", []) or [])
                if str(item).strip()
            }
        )
        if unread_fingerprint and unread_fingerprint == last_fingerprint:
            return (
                "Unread comms set is unchanged from the previous reactivation; "
                "skipping another auto-reactivation to avoid a no-progress loop."
            )
        current_depth = int(task.metadata.get("comms_reactivation_depth", 0) or 0)
        if current_depth >= self.COMMS_REACTIVATION_DEPTH_LIMIT:
            return (
                f"Comms reactivation depth={current_depth} has reached the hard "
                f"limit ({self.COMMS_REACTIVATION_DEPTH_LIMIT}); refusing to re-open "
                "this task again. Upstream senders will time out or escalate."
            )
        # Cross-role ping-pong: look for (from_role, subject_hash) repeats
        # in the short rolling window. Each history entry was recorded at a
        # prior successful reactivation. If the same sender keeps resurfacing
        # the same subject, that's a loop even when msg_ids differ.
        history = list(task.metadata.get("comms_cross_role_history", []) or [])
        repeat_counts: dict[tuple[str, str, str], int] = {}
        for entry in history:
            key = (
                str(entry.get("from_role", "") or "").strip(),
                str(entry.get("subject_hash", "") or "").strip(),
                str(entry.get("target_work_item_id", "") or "").strip(),
            )
            if not key[0]:
                continue
            repeat_counts[key] = repeat_counts.get(key, 0) + 1
        current_work_item_id = linked_work_item_id_for_task(task)
        for msg in actionable_unread:
            from_role = str(msg.get("from_agent", "") or "").strip()
            if not from_role:
                continue
            subject_hash = self._subject_hash(str(msg.get("subject", "") or ""))
            msg_metadata = dict(msg.get("metadata", {}) or {})
            msg_work_item_refs = self._work_item_refs_from_frontmatter(msg_metadata)
            target_work_item_id = (
                current_work_item_id
                if current_work_item_id in msg_work_item_refs or current_work_item_id
                else sorted(msg_work_item_refs)[0]
                if msg_work_item_refs
                else ""
            )
            key = (from_role, subject_hash, target_work_item_id)
            prior = repeat_counts.get(key, 0)
            if prior >= self.COMMS_CROSS_ROLE_REPEAT_THRESHOLD:
                return (
                    f"Cross-role comms ping-pong detected: role `{from_role}` has "
                    f"re-sent the same subject {prior + 1} times; refusing to "
                    "reactivate. Escalate to a human reviewer instead."
                )
        return None


    async def _reactivate_for_unread_mail(self, task: Task) -> bool:
        """Re-open `task` as PENDING if its role has unread actionable mail.

        Returns True when the task was re-opened (caller should NOT mark
        completion progress), False otherwise.

        Three loop-protection guards are enforced via
        ``_reactivation_blocked_by_history``:
        (a) unread-set fingerprint equality, (b) hard depth cap, and
        (c) per-(from_role, subject_hash) cross-role ping-pong detection.
        This helper is also called by ``CommsReactivationSweeper`` so any
        evolution of the guard logic applies uniformly to both callers.
        """
        multi_team_org = str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org"
        if multi_team_org:
            # Company runtime routes inbound attention through role/session
            # inbox refresh + work-item attention/review cards. Re-opening
            # arbitrary DONE tasks from a role-wide mailbox is the old
            # task-centric path that caused cross-work-item contamination.
            return False
        context = self._resolve_task_inbox_context(task)
        if context is None:
            return False
        role_id, layout = context
        work_item_id = linked_work_item_id_for_task(task)
        actionable_unread = self._collect_actionable_unread(
            layout,
            role_id,
            work_item_id=work_item_id,
            task_id=str(task.id or "").strip(),
            require_work_item_scope=False,
        )
        if not actionable_unread:
            return False

        task.metadata = dict(task.metadata)
        unread_fingerprint = sorted(
            {
                str(item.get("msg_id", "") or "").strip()
                for item in actionable_unread
                if str(item.get("msg_id", "") or "").strip()
            }
        )
        skip_reason = self._reactivation_blocked_by_history(
            task,
            actionable_unread,
            unread_fingerprint,
        )
        if skip_reason:
            now = datetime.now()
            last_key = str(task.metadata.get("comms_last_blocked_reactivation_key", "") or "").strip()
            last_at_raw = str(task.metadata.get("comms_last_blocked_reactivation_at", "") or "").strip()
            block_key = hashlib.sha1(
                json.dumps(
                    {
                        "role_id": role_id,
                        "work_item_id": work_item_id,
                        "reason": skip_reason,
                        "fingerprint": unread_fingerprint,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            should_log = True
            if last_key == block_key and last_at_raw:
                try:
                    last_at = datetime.fromisoformat(last_at_raw)
                    should_log = (now - last_at).total_seconds() >= self.COMMS_REACTIVATION_WARNING_COOLDOWN_SECONDS
                except Exception:
                    should_log = True
            if should_log:
                await self._append_progress(task, skip_reason)
                task.metadata["comms_last_blocked_reactivation_at"] = now.isoformat()
                task.metadata["comms_last_blocked_reactivation_key"] = block_key
            await self.save_task(task)
            if should_log:
                logger.warning(
                    "[comms_reactivation] task={} role={} work_item={} blocked: {}",
                    getattr(task, "id", ""),
                    role_id,
                    work_item_id,
                    skip_reason,
                )
            return False

        depth = int(task.metadata.get("comms_reactivation_depth", 0) or 0) + 1
        task.metadata["comms_reactivation_depth"] = depth
        task.metadata["comms_last_reactivation_fingerprint"] = unread_fingerprint

        # Extend the rolling cross-role history with one entry per actionable
        # sender in this reactivation round. Trimmed to COMMS_CROSS_ROLE_HISTORY_MAX.
        history = list(task.metadata.get("comms_cross_role_history", []) or [])
        for msg in actionable_unread:
            from_role = str(msg.get("from_agent", "") or "").strip()
            if not from_role:
                continue
            msg_metadata = dict(msg.get("metadata", {}) or {})
            msg_work_item_refs = self._work_item_refs_from_frontmatter(msg_metadata)
            history.append(
                {
                    "from_role": from_role,
                    "subject_hash": self._subject_hash(str(msg.get("subject", "") or "")),
                    "target_work_item_id": work_item_id if work_item_id else sorted(msg_work_item_refs)[0] if msg_work_item_refs else "",
                    "semantic_type": str(msg.get("semantic_type") or msg_metadata.get("semantic_type") or "").strip(),
                    "msg_id": str(msg.get("msg_id", "") or "").strip(),
                    "depth": depth,
                }
            )
        task.metadata["comms_cross_role_history"] = history[-self.COMMS_CROSS_ROLE_HISTORY_MAX:]

        # Re-open the task. The scheduler picks up PENDING tasks each
        # tick; the agent will see the unread mail in its next prompt
        # via context_assembler.render_inbox_section. For external agents
        # the broker's _restore_session_resume_from_store then re-hydrates
        # the prior codex/claude_code session_id so the resumed turn has
        # full context.
        await self._append_progress(
            task,
            f"Reactivated by inbound comms (depth={depth}); "
            "agent will read inbox/new/ on next turn.",
        )
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=TaskStatus.PENDING,
            reason="reactivated_by_inbound_comms",
        )
        await self.save_task(task)
        return True


    async def _park_for_blocking_comms(self, task: Task) -> bool:
        """If `task`'s role sent any blocking messages this turn whose
        replies have not yet arrived, park the work item in AWAITING_PEER.

        Returns True if parking happened (caller should bail out of
        the normal completion flow), False if there is nothing to wait
        for. Tolerant of missing comms layout / missing role.
        """
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return False
        layout = self._comms_layout_for_task(task)
        role_id = (
            str(task.assigned_to or "").strip()
            or str(task.metadata.get("work_item_role_id", "") or "").strip()
        )
        if layout is None or not role_id:
            return False
        try:
            unresolved = _comms.find_unresolved_blocking_outbox(layout, role_id)
        except Exception:
            return False
        if not unresolved:
            return False

        task.metadata = dict(task.metadata)
        peer_wait = dict(task.metadata.get("peer_wait", {}) or {})
        peer_wait["kind"] = "comms_blocking"
        peer_wait["blocking_message_ids"] = [h.message_id for h in unresolved]
        peer_wait["awaiting_replies_from"] = sorted({h.to_role for h in unresolved})
        peer_wait["parked_at"] = datetime.now(timezone.utc).isoformat()
        task.metadata["peer_wait"] = peer_wait
        for h in unresolved:
            await self._append_progress(
                task,
                f"Parked awaiting blocking reply: msg `{h.message_id}` "
                f"sent to `{h.to_role}` ({h.subject!r}).",
            )
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=Phase.WAITING_FOR_PEER,
            reason="park_for_blocking_comms",
        )
        await self.save_task(task)
        return True


    async def _try_unpark_blocking_comms(self, task: Task) -> bool:
        """For an AWAITING_PEER task with kind=comms_blocking, check
        whether all expected replies have arrived. If yes, unpark by
        flipping back to PENDING and recording the reply paths in
        metadata so the next prompt can surface them.

        Returns True if the task was unparked, False otherwise.
        """
        if task.status != TaskStatus.AWAITING_PEER:
            return False
        peer_wait = dict(task.metadata.get("peer_wait", {}) or {})
        wait_kind = str(peer_wait.get("kind") or "")
        # An empty kind is an orphaned wait (e.g. a legacy resolver popped
        # `peer_wait` while the work item stayed WAITING_FOR_PEER); those
        # are recoverable from the durable comms state below. Waits with a
        # different explicit kind (meeting, message-id) have their own
        # resolvers.
        if wait_kind and wait_kind != "comms_blocking":
            return False
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
            return False
        layout = self._comms_layout_for_task(task)
        role_id = (
            str(task.assigned_to or "").strip()
            or str(task.metadata.get("work_item_role_id", "") or "").strip()
        )
        if layout is None or not role_id:
            return False
        blocking_ids = list(peer_wait.get("blocking_message_ids", []) or [])
        if not blocking_ids:
            # No recorded ids (orphaned or empty wait): fall back to the
            # park predicate itself — any unanswered blocking outbox
            # message keeps the task parked, none means release.
            if _comms.find_unresolved_blocking_outbox(layout, role_id):
                return False
            task.metadata = dict(task.metadata)
            task.metadata.pop("peer_wait", None)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=TaskStatus.PENDING,
                reason="unpark_blocking_comms_empty",
            )
            await self.save_task(task)
            return True
        replies: dict[str, str] = {}
        for mid in blocking_ids:
            reply = _comms.find_reply_to(layout, role_id, mid)
            if reply is None:
                return False  # at least one still unresolved
            replies[str(mid)] = str(reply.path)
        # All replies present — unpark.
        task.metadata = dict(task.metadata)
        task.metadata["comms_resolved_blocking_replies"] = replies
        task.metadata.pop("peer_wait", None)
        await self._append_progress(
            task,
            f"All blocking replies received ({len(replies)}); resuming work item.",
        )
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=TaskStatus.PENDING,
            reason="unpark_blocking_comms_replies_received",
        )
        await self.save_task(task)
        return True


    async def _check_collaboration_awareness(self, task: Task) -> str:
        """Check whether a completing work item had any meaningful collaboration with parallel peers.

        Returns a warning string if the work item had parallel peers but zero inter-agent
        messages (questions, DMs, broadcasts, etc.).  The warning is informational —
        it does NOT block completion — but it is injected into downstream context so
        that QA / review work items can flag potential integration gaps.
        """
        parallel_peer_count = int(task.metadata.get("_parallel_peer_count", 0) or 0)
        if parallel_peer_count == 0:
            return ""
        # Count non-handoff messages sent or received by this role during execution
        comm_store = getattr(self.communication, "store", None) if self.communication else None
        if comm_store is None:
            return ""
        role_id = str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()
        if not role_id:
            return ""
        peer_roles = {
            str(peer.get("role_id", "")).strip()
            for peer in list(task.metadata.get("_work_item_plan_projections", []) or [])
            if isinstance(peer, dict) and str(peer.get("role_id", "")).strip() and str(peer.get("role_id", "")).strip() != role_id
        }
        try:
            all_messages = await comm_store.get_messages_for_agent(
                agent_id=role_id,
                task_id=task.id,
                unread_only=False,
                limit=50,
            )
        except Exception:
            return ""
        # Also count messages this role *sent* (not just received)
        try:
            sent_messages = await comm_store.get_outbox_for_agent(
                agent_id=role_id,
                task_id=task.id,
                limit=50,
            )
        except (AttributeError, TypeError):
            sent_messages = []
        collaboration_types = {"question", "answer", "flag_issue", "decision_needed", "request_review"}
        collab_message_ids: set[str] = set()
        for msg in list(all_messages) + list(sent_messages):
            msg_type = str(getattr(msg, "msg_type", "") or "").strip()
            from_agent = str(getattr(msg, "from_agent", "") or "").strip()
            recipients = {str(item).strip() for item in list(getattr(msg, "to_agents", []) or []) if str(item).strip()}
            if msg_type in collaboration_types and (
                from_agent in peer_roles
                or bool(peer_roles & recipients)
                or not peer_roles
            ):
                collab_message_ids.add(str(getattr(msg, "msg_id", "") or "").strip())
        layout = self._comms_layout_for_task(task)
        if layout is not None:
            try:
                from opc.layer2_organization import comms as _comms

                file_headers = _comms.list_role_messages(
                    layout,
                    role_id,
                    include_new=True,
                    include_seen=True,
                    include_outbox=True,
                )
            except Exception:
                file_headers = []
            for header in file_headers:
                msg_id = str(header.message_id or header.path.name).strip()
                if not msg_id:
                    continue
                fm = dict(header.raw_frontmatter or {})
                semantic_type = str(fm.get("semantic_type") or fm.get("kind") or "").strip().lower()
                msg_type = str(fm.get("msg_type") or "").strip().lower()
                peer_involved = (
                    header.from_role in peer_roles
                    or header.to_role in peer_roles
                    or not peer_roles
                )
                if not peer_involved:
                    continue
                if msg_type in collaboration_types or semantic_type in {
                    "work_update",
                    "blocked_on_decision",
                } or bool(header.blocking):
                    collab_message_ids.add(msg_id)
        collab_count = len({item for item in collab_message_ids if item})
        if collab_count > 0:
            return ""
        peer_projections = list(task.metadata.get("_work_item_plan_projections", []) or [])
        peer_names = ", ".join(
            str(p.get("role_id", "")).strip()
            for p in peer_projections[:6]
            if isinstance(p, dict) and str(p.get("role_id", "")).strip()
        )
        return (
            f"Work item `{self._projection_id_for_task(task)}` (role: {role_id}) "
            f"completed with {parallel_peer_count} parallel peer(s) ({peer_names}) "
            f"but had ZERO inter-agent collaboration messages. "
            f"Potential integration gaps may exist."
        )


