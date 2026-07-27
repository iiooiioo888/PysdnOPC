"""Auto-extracted Mixin for CompanyWorkItemExecutor."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from loguru import logger

from opc.core.active_task_runs import (
    ActiveTaskRunAdmissionClosed,
)
from opc.core.config import DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS, DEFAULT_ORGANIZATION_ID
from opc.core.models import (
    CompanyMemberSession,
    DataAcquisitionReport,
    DelegationEvent,
    DelegationWorkItem,
    EnvironmentManifest,
    Phase,
    Task,
    TaskResult,
    TaskStatus,
    WorkItemExecutionStrategy,
    WorkspaceManifest,
    normalize_role_runtime_status,
)
from opc.core.worker_envelope import classify_worker_message, worker_message_is_actionable
from opc.layer2_organization.company_runtime import CompanyRuntime, canonical_role_session_id
from opc.layer2_organization.phase import (
    DONE_PHASES,
    IN_PROGRESS_PHASES,
    IN_REVIEW_PHASES,
    is_dispatchable,
    is_orphaned,
    is_report_execution_work_item_metadata,
    is_review_execution_work_item_metadata,
    is_runtime_auxiliary_work_item,
    is_runnable,
    kanban_column,
    phase_for_task_status,
    should_hide_work_item_from_company_kanban,
    task_status_for_phase,
)
from opc.layer2_organization.collaboration_service import CollaborationContext
from opc.layer2_organization.phase_hooks import reconcile_role_serial_queues
from opc.layer2_organization.session_scoping import task_session_scope_id
from opc.layer2_organization.turn_mode import reset_manager_dispatch_turn_metadata
from opc.layer2_organization.data_acquisition_policy import (
    default_download_manifest_path,
    default_execution_record_path,
    default_source_candidates_path,
)
from opc.layer2_organization.gate_harness import GateHarness, GateHarnessDecision
from opc.layer2_organization.metadata_ownership import (
    append_work_item_progress,
    build_work_item_owner_execution_copy,
    copy_work_item_execution_metadata,
    strip_disallowed_work_item_metadata_from_runtime_task,
    update_runtime_task_owned_metadata,
    update_work_item_owned_metadata,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)
from opc.layer2_organization.recruiter import (
    resolve_effective_execution_agent,
)
from opc.layer2_organization.seat_executor import SeatExecutor
from opc.layer2_organization.work_item_transition import (
    DEPENDENCY_CLASS_DEFAULT,
    compute_doomed_work_item_ids,
    has_pending_settlement_release,
    normalize_dependency_work_item_ids,
    refresh_dependents_for_run,
    settled_failure_dependency_ids,
    transition_work_item_from_task,
)
from opc.layer2_organization.work_item_identity import (
    WORK_ITEM_TURN_TYPE_KEY,
    canonical_work_item_turn_type_for_kind,
    is_delivery_turn,
    is_manager_reviewable_turn,
    mark_projected_work_item_task,
    mark_work_item_projection,
    projection_id_for_task,
    projection_id_for_work_item,
    turn_type_for_task,
    turn_type_for_work_item,
    work_item_identity_payload,
    work_item_identity_payload_for_task,
    work_item_turn_type_from_metadata,
)
from opc.layer2_organization.work_item_links import (
    linked_work_item_id_for_task,
    set_linked_work_item_id,
    task_by_linked_work_item_id,
)
from opc.layer2_organization.work_item_runtime import (
    is_work_item_runtime_metadata,
    mark_work_item_runtime,
    work_item_runtime_version,
)
from opc.layer2_organization.work_item_runtime_invariants import (
    WORK_ITEM_RUNTIME_INVARIANT_EVENT_TYPE,
    WorkItemRuntimeInvariantIssue,
    diagnose_work_item_runtime_projections,
    validate_work_item_runtime_projection,
)
from opc.layer4_tools.output_budget import clip_text
from opc.llm.retry import LLMRetryError, call_llm_json_with_retry

from opc.layer2_organization._company_mode_shared import (  # noqa: E402
    CEO_PRE_DELIVERY_ASSESSMENT_PROMPT,
    CompanyExecutorDriverOwnership,
    CompanyExecutorRunState,
    DEFAULT_MAX_PRE_DELIVERY_REWORKS,
    DEFAULT_MAX_REVIEW_REWORKS,
    EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT,
    MAX_VERDICT_PARSE_RETRIES,
    _CANONICAL_COORDINATION_SIGNALS,
    _COMPANY_RUNTIME_CONTROL_TASK_METADATA_KEYS,
    _DATA_ACQUISITION_PROJECTION_ID,
    _DEFAULT_CONTRACT_REWORK_MAX_RETRIES,
    _DEFAULT_DATA_ACQUISITION_EXECUTION_RECORD_PATH,
    _DEFAULT_DATA_ACQUISITION_LOG_PATH,
    _DEFAULT_DATA_ACQUISITION_REPORT_PATH,
    _DEFAULT_WORKSPACE_LAYOUT,
    _HUMAN_WAIT_MAX_STALL_TICKS,
    _MULTI_TEAM_ORG_WALL_CLOCK_TIMEOUT_SEC,
    _MAX_GATE_REVIEW_FEEDBACK_CHARS,
    _NO_DELEGATION_JUSTIFICATION_LINE,
    _REVIEW_VERDICT_PARSE_RETRY_HINT,
    _REVIEW_WAITING_STATUSES,
    _SERIALIZED_PLAN_MARKER_KEYS,
    _STALE_REWORK_TASK_METADATA_KEYS,
    _WAITING_TASK_STATUSES,
    _WORKSPACE_BOOTSTRAP_PROJECTION_ID,
    _coerce_company_work_item_runtime_plan,
    _fallback_comms_root,
    deserialize_company_work_item_runtime_plan,
    is_serialized_company_work_item_runtime_plan,
    report_work_item_id_for_attempt,
    review_work_item_id_for_attempt,
    serialize_company_work_item_runtime_plan,
    serialized_company_plan_from_metadata,
    WorkItemOutputBundle,
)

if TYPE_CHECKING:
    from opc.layer2_organization.company_mode import CompanyWorkItemExecutor


class CompanyExecutorDispatchMixin:
    """Mixin extracted from CompanyWorkItemExecutor."""

    def _run_state(self) -> CompanyExecutorRunState:
        if not hasattr(self, "_default_run_state"):
            self._default_run_state = CompanyExecutorRunState()
        if not hasattr(self, "_run_state_var"):
            return self._default_run_state
        return self._run_state_var.get() or self._default_run_state


    def _use_run_state(self, state: CompanyExecutorRunState) -> Token[CompanyExecutorRunState | None]:
        return self._run_state_var.set(state)


    def _reset_run_state(self, token: Token[CompanyExecutorRunState | None]) -> None:
        self._run_state_var.reset(token)


    @property
    def _active_plan(self) -> CompanyWorkItemRuntimePlan | None:
        return self._run_state().active_plan


    @_active_plan.setter
    def _active_plan(self, value: CompanyWorkItemRuntimePlan | None) -> None:
        self._run_state().active_plan = value


    @property
    def _active_tasks(self) -> list[Task]:
        return self._run_state().active_tasks


    @_active_tasks.setter
    def _active_tasks(self, value: list[Task]) -> None:
        self._run_state().active_tasks = value


    @property
    def _dispatcher_wake(self) -> asyncio.Event:
        return self._run_state().dispatcher_wake


    @_dispatcher_wake.setter
    def _dispatcher_wake(self, value: asyncio.Event) -> None:
        self._run_state().dispatcher_wake = value


    @property
    def _kanban_dirty(self) -> bool:
        return self._run_state().kanban_dirty


    @_kanban_dirty.setter
    def _kanban_dirty(self, value: bool) -> None:
        self._run_state().kanban_dirty = bool(value)


    @property
    def _kanban_broadcast_task(self) -> asyncio.Task[None] | None:
        return self._run_state().kanban_broadcast_task


    @_kanban_broadcast_task.setter
    def _kanban_broadcast_task(self, value: asyncio.Task[None] | None) -> None:
        self._run_state().kanban_broadcast_task = value


    @property
    def _runtime_invariant_issue_keys(self) -> set[tuple[str, str, str, str]]:
        return self._run_state().runtime_invariant_issue_keys


    @_runtime_invariant_issue_keys.setter
    def _runtime_invariant_issue_keys(self, value: set[tuple[str, str, str, str]]) -> None:
        self._run_state().runtime_invariant_issue_keys = value


    async def _refresh_active_snapshot(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> tuple[CompanyWorkItemRuntimePlan, list[Task]]:
        if not self.store or not tasks:
            return plan, tasks
        parent_session_id = str(getattr(tasks[0], "parent_session_id", "") or tasks[0].metadata.get("parent_session_id", "") or "").strip()
        if not parent_session_id:
            return plan, tasks
        project_id = str(tasks[0].project_id or "default")
        all_tasks = await self.store.get_tasks(project_id=project_id)
        work_item_tasks = [
            task
            for task in all_tasks
            if str(getattr(task, "parent_session_id", "") or "").strip() == parent_session_id
            and projection_id_for_task(task)
        ]
        if not work_item_tasks:
            return plan, tasks
        latest_by_projection_id: dict[str, Task] = {}
        for task in sorted(work_item_tasks, key=lambda item: (item.created_at, item.id)):
            projection_id = projection_id_for_task(task)
            if projection_id:
                latest_by_projection_id[projection_id] = task
        if not latest_by_projection_id:
            return plan, tasks
        plan_data = None
        for task in sorted(latest_by_projection_id.values(), key=lambda item: (item.created_at, item.id), reverse=True):
            candidate = serialized_company_plan_from_metadata(task.metadata)
            if candidate:
                plan_data = candidate
                break
        if plan_data:
            plan = deserialize_company_work_item_runtime_plan(plan_data)
        projection_order = plan.projection_order_map()
        refreshed_tasks = sorted(
            latest_by_projection_id.values(),
            key=lambda task: (
                projection_order.get(projection_id_for_task(task), len(projection_order)),
                task.created_at,
                task.id,
            ),
        )
        return plan, refreshed_tasks


    async def _emit_runtime_signal(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.emit_runtime_event is None:
            return
        await self.emit_runtime_event(event_type, payload)


    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None


    def _configure_external_timeouts(self, task: Task) -> None:
        """Keep external-agent timeouts below the enclosing company work-item timeout."""
        task.metadata = dict(task.metadata)
        work_item_timeout = max(1, int(self.work_item_timeout))
        buffer_seconds = min(60, max(10, work_item_timeout // 4))
        hard_timeout = max(1, work_item_timeout - buffer_seconds)

        existing_hard = self._coerce_positive_int(task.metadata.get("external_hard_timeout_seconds"))
        if existing_hard is not None:
            hard_timeout = min(hard_timeout, existing_hard)

        suggested_idle = max(30, min(hard_timeout, work_item_timeout // 3 if work_item_timeout >= 90 else hard_timeout))
        existing_idle = self._coerce_positive_int(task.metadata.get("external_idle_timeout_seconds"))
        idle_timeout = min(existing_idle, hard_timeout) if existing_idle is not None else suggested_idle

        suggested_startup = min(
            idle_timeout,
            DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS,
        )
        existing_startup = self._coerce_positive_int(task.metadata.get("external_startup_timeout_seconds"))
        startup_timeout = min(existing_startup, idle_timeout) if existing_startup is not None else suggested_startup

        task.metadata["external_hard_timeout_seconds"] = hard_timeout
        task.metadata["external_idle_timeout_seconds"] = idle_timeout
        task.metadata["external_startup_timeout_seconds"] = startup_timeout


    async def _prepare_setup_workspace(self, task: Task) -> None:
        """Ensure workspace roots exist before execution and bootstrap setup layouts when requested."""
        task.metadata = dict(task.metadata)
        work_item_turn_type = self._turn_type_for_task(task)
        workspace_root = str(task.metadata.get("workspace_root", "") or "").strip()
        comms_workspace_root = str(task.metadata.get("comms_workspace_root", "") or "").strip()
        target_output_dir = str(task.metadata.get("target_output_dir", "") or "").strip()

        prepared_roots: list[str] = []
        for raw in [workspace_root, comms_workspace_root, target_output_dir]:
            path_text = str(raw or "").strip()
            if not path_text:
                continue
            target = Path(path_text).expanduser()
            target.mkdir(parents=True, exist_ok=True)
            resolved = str(target.resolve())
            if resolved not in prepared_roots:
                prepared_roots.append(resolved)

        if not prepared_roots:
            return

        primary_root = target_output_dir or workspace_root or comms_workspace_root
        if primary_root:
            task.metadata["setup_workspace_prepared"] = str(Path(primary_root).expanduser().resolve())
        wid = linked_work_item_id_for_task(task)
        progress = [] if wid and self.store else list(task.metadata.get("progress_log", []) or [])
        marker_prefix = "[Setup]" if work_item_turn_type == "setup" else "[Workspace]"
        marker = f"{marker_prefix} Prepared workspace roots: {', '.join(prepared_roots)}"
        if wid and self.store:
            task.metadata.pop("progress_log", None)
            progress = await append_work_item_progress(self.store, wid, marker, dedupe=True)
        elif marker not in progress:
            progress.append(marker)
            task.metadata["progress_log"] = progress[-20:]
        if work_item_turn_type != "setup" or not target_output_dir:
            return
        target = Path(target_output_dir).expanduser()
        projection_id = self._projection_id_for_task(task)
        if projection_id == _WORKSPACE_BOOTSTRAP_PROJECTION_ID:
            reserved_paths: dict[str, str] = {}
            for relative in _DEFAULT_WORKSPACE_LAYOUT:
                path = target / relative
                path.mkdir(parents=True, exist_ok=True)
                reserved_paths[relative] = str(path.resolve())
            manifest_path = target / ".openopc" / "manifests" / "workspace_manifest.json"
            manifest = WorkspaceManifest(
                root_path=str(target.resolve()),
                manifest_path=str(manifest_path.resolve()),
                reserved_paths=reserved_paths,
                status="ready",
                notes=["Prepared automatically by workspace bootstrap before downstream execution."],
            )
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
            task.metadata["workspace_manifest"] = manifest.__dict__
            artifacts = list(task.metadata.get("artifacts", []) or [])
            artifacts.append(str(manifest_path.resolve()))
            task.metadata["artifacts"] = list(dict.fromkeys(str(item).strip() for item in artifacts if str(item).strip()))
            layout_marker = f"[WorkspaceBootstrap] Prepared reserved layout under: {target.resolve()}"
            if wid and self.store:
                task.metadata.pop("progress_log", None)
                progress = await append_work_item_progress(self.store, wid, layout_marker, dedupe=True)
            elif layout_marker not in progress:
                progress.append(layout_marker)
                task.metadata["progress_log"] = progress[-20:]


    def _plan_view_for_task(self, task: Task) -> CompanyWorkItemRuntimePlan | None:
        if self._active_plan is not None:
            return self._active_plan
        plan_data = serialized_company_plan_from_metadata(task.metadata)
        if plan_data:
            return deserialize_company_work_item_runtime_plan(plan_data)
        return None


    def _projection_spec_for_task(self, task: Task) -> WorkItemProjectionSpec | None:
        plan = self._plan_view_for_task(task)
        if plan is None:
            return None
        projection_id = self._projection_id_for_task(task)
        for projection_spec in plan.projections:
            if str(projection_spec.projection_id).strip() == projection_id:
                return projection_spec
        return None


    def _inject_parallel_peers_metadata(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        """Inject metadata about parallel peer work items so agents know who else is running."""
        parallel_group = str(task.metadata.get("work_item_parallel_group", "") or "").strip()
        if not parallel_group:
            return
        plan = self._plan_view_for_task(task)
        if plan is None:
            return
        current_projection_id = self._projection_id_for_task(task)
        peer_projections: list[dict[str, str]] = []
        for projection in plan.projections:
            if str(projection.parallel_group or "").strip() != parallel_group:
                continue
            projection_id = str(projection.projection_id).strip()
            if projection_id == current_projection_id:
                continue
            peer_task = task_by_projection_id.get(projection_id)
            peer_role = str(projection.role_id or "").strip()
            peer_status = str(peer_task.status.value if peer_task else "unknown").strip()
            peer_projections.append({
                "projection_id": projection_id,
                "title": str(projection.title or "").strip(),
                "role_id": peer_role,
                "parallel_group": parallel_group,
                "status": peer_status,
            })
        if peer_projections:
            task.metadata = dict(task.metadata)
            task.metadata["_work_item_plan_projections"] = peer_projections
            task.metadata["_parallel_peer_count"] = len(peer_projections)


    def _resolve_work_item_assignment_before_execution(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> None:
        projection_spec = self._projection_spec_for_task(task)
        if projection_spec is None:
            return

        helper = self.work_item_helper
        work_item_turn_type = self._infer_work_item_turn_type_for_task(task, projection_spec)
        global_intent_summary = str(task.metadata.get("global_intent_summary", "") or "").strip()
        if not global_intent_summary:
            global_intent_summary = helper._fallback_global_intent_summary(
                str(task.metadata.get("original_message", "") or "")
            )
        current_assignment = helper._coerce_projection_assignment(
            dict(task.metadata.get("work_item_assignment", {}) or {}),
            projection=projection_spec,
            global_intent_summary=global_intent_summary,
        )
        assignment = dict(current_assignment)
        assignment_status = str(task.metadata.get("work_item_assignment_status", "") or "bootstrap").strip() or "bootstrap"
        source_projection_id = str(task.metadata.get("work_item_assignment_source_projection_id", "") or "").strip()

        plan = self._plan_view_for_task(task)
        downstream_consumers = helper._downstream_consumers(plan, projection_spec) if plan is not None else []
        ownership_contract = helper._build_ownership_contract(
            projection_spec=projection_spec,
            assignment=assignment,
            work_item_turn_type=work_item_turn_type,
            target_output_dir=str(task.metadata.get("target_output_dir", "") or "").strip() or None,
            downstream_consumers=downstream_consumers,
        )
        work_item_runtime_plan = helper._build_work_item_runtime_plan(
            projection_spec=projection_spec,
            assignment=assignment,
            work_item_turn_type=work_item_turn_type,
            runtime_policy=dict(task.metadata.get("policy") or task.metadata.get("runtime_policy", {}) or {}),
        )
        coordination_spec = helper._build_coordination_spec(
            projection_spec=projection_spec,
            assignment=assignment,
            work_item_turn_type=work_item_turn_type,
            runtime_policy=dict(task.metadata.get("policy") or task.metadata.get("runtime_policy", {}) or {}),
            employee_assignment=dict(task.metadata.get("employee_assignment", {}) or {}),
        )
        lint_issues = helper._lint_work_item_assignment(projection_spec=projection_spec, assignment=assignment)

        task.description = helper._build_work_item_description(assignment)
        task.metadata = dict(task.metadata)
        task.metadata["global_intent_summary"] = assignment["global_intent_summary"]
        task.metadata["work_item_assignment"] = dict(assignment)
        task.metadata["work_item_assignment_status"] = assignment_status
        task.metadata["work_item_assignment_source_projection_id"] = source_projection_id
        task.metadata["work_item_runtime_plan"] = work_item_runtime_plan
        task.metadata["adaptive"] = helper._coordination_spec_dict(coordination_spec)
        task.metadata["acceptance_criteria"] = list(assignment.get("acceptance_criteria", []))
        task.metadata["ownership_contract"] = ownership_contract.__dict__
        task.metadata["work_item_assignment_lint"] = lint_issues
        task.metadata = mark_work_item_projection(
            task.metadata,
            projection_id=str(projection_spec.projection_id or task.id).strip(),
            turn_type=work_item_turn_type,
        )


    def _infer_work_item_turn_type_for_task(self, task: Task, projection_spec: WorkItemProjectionSpec | None = None) -> str:
        if projection_spec is None:
            projection_spec = self._projection_spec_for_task(task)
        if projection_spec is None:
            existing = work_item_turn_type_from_metadata(task.metadata, fallback="")
            if existing:
                return existing
            return "execute"
        projection_id = str(projection_spec.projection_id or "").strip().lower()
        if projection_id in {_WORKSPACE_BOOTSTRAP_PROJECTION_ID, _DATA_ACQUISITION_PROJECTION_ID}:
            return self.work_item_helper._infer_work_item_turn_type(projection_spec)
        existing = work_item_turn_type_from_metadata(task.metadata, fallback="")
        if existing:
            return existing
        return self.work_item_helper._infer_work_item_turn_type(projection_spec)


    def _uses_cell_runtime(self, tasks: list[Task]) -> bool:
        if not self.store or not tasks:
            return False
        return any(str((task.metadata or {}).get("delegation_run_id", "") or "").strip() for task in tasks)


    @staticmethod
    def _uses_multi_team_org_runtime(tasks: list[Task], plan: CompanyWorkItemRuntimePlan | None = None) -> bool:
        if plan is not None and str(plan.metadata.get("execution_model", "") or "").strip() == "multi_team_org":
            return True
        return any(
            str((task.metadata or {}).get("execution_model", "") or "").strip() == "multi_team_org"
            or str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org"
            for task in tasks
        )


    async def _load_delegation_work_items(self, tasks: list[Task]) -> list[DelegationWorkItem]:
        if not self.store or not tasks:
            return []
        run_id = str((tasks[0].metadata or {}).get("delegation_run_id", "") or "").strip()
        if not run_id or not hasattr(self.store, "list_delegation_work_items"):
            return []
        return await self.store.list_delegation_work_items(run_id)


    @staticmethod
    def _checkpoint_basis_hash(task: Task) -> str:
        output_metadata = dict((getattr(task, "context_snapshot", {}) or {}).get("work_item_owned_outputs", {}) or {})
        if isinstance(task.result, dict):
            result_content = str(task.result.get("content", "") or "").strip()
        elif task.result:
            result_content = str(task.result or "").strip()
        else:
            result_content = ""
        payload = {
            "task_id": task.id,
            **work_item_identity_payload_for_task(task),
            "delivery_revision": task.metadata.get("delivery_revision", ""),
            "owner_directive_revision": task.metadata.get("owner_directive_revision", ""),
            "result_content": result_content,
            "work_item_summary": str(output_metadata.get("work_item_summary", "") or task.metadata.get("work_item_summary", "") or "").strip(),
            "work_item_summary_for_downstream": str(
                output_metadata.get("work_item_summary_for_downstream", "")
                or task.metadata.get("work_item_summary_for_downstream", "")
                or ""
            ).strip(),
            "artifact_index": list(output_metadata.get("work_item_artifact_index", []) or task.metadata.get("work_item_artifact_index", []) or []),
            "verification_status": dict(output_metadata.get("verification_status", {}) or task.metadata.get("verification_status", {}) or {}),
            "verification_evidence": dict(output_metadata.get("verification_evidence", {}) or task.metadata.get("verification_evidence", {}) or {}),
            "verification_verdict": str(task.metadata.get("verification_verdict", "") or "").strip(),
            "delivery_package": output_metadata.get("delivery_package") or task.metadata.get("delivery_package") or {},
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


    @classmethod
    def _work_item_is_runnable(
        cls,
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem],
        task_by_work_item_id: dict[str, Task] | None = None,
    ) -> bool:
        phase = work_item.phase
        metadata = dict(work_item.metadata or {})
        review_execution_work_item = is_review_execution_work_item_metadata(metadata)
        report_execution_work_item = is_report_execution_work_item_metadata(metadata)
        if str(metadata.get("dispatch_hold", "") or "").strip():
            return False
        # Hidden auxiliary cards (review / report) are still runnable —
        # they are the kanban-push primitives the dispatcher schedules.
        # Worker work items marked hidden for any other reason stay
        # excluded.
        if (
            should_hide_work_item_from_company_kanban(metadata)
            and not review_execution_work_item
            and not report_execution_work_item
        ):
            return False
        if review_execution_work_item:
            target_work_item_id = str(metadata.get("review_target_work_item_id", "") or "").strip()
            if not target_work_item_id:
                return False
            target_work_item = work_item_by_id.get(target_work_item_id)
            if target_work_item is None:
                return False
            if target_work_item.phase not in IN_REVIEW_PHASES:
                return False
            review_owner_seat_id = str(target_work_item.metadata.get("review_owner_seat_id", "") or "").strip()
            if review_owner_seat_id and review_owner_seat_id != str(work_item.seat_id or "").strip():
                return False
            return is_dispatchable(work_item) or phase == Phase.RUNNING
        if report_execution_work_item:
            # The report card is owned by the worker seat. Sanity-check
            # the parent worker work item is still in review (it should
            # be — the report card was spawned the moment the parent
            # transitioned to AWAITING_MANAGER_REVIEW). If the parent
            # has somehow already moved past review, the report card is
            # obsolete; let it sit (cleanup elsewhere).
            target_work_item_id = str(metadata.get("report_target_work_item_id", "") or "").strip()
            if target_work_item_id:
                target_work_item = work_item_by_id.get(target_work_item_id)
                if target_work_item is not None and target_work_item.phase not in IN_REVIEW_PHASES:
                    return False
            return is_dispatchable(work_item) or phase == Phase.RUNNING
        # Worker can resume from in_progress sub-states (paused, needs_attention,
        # waiting_for_children) once the awaited event arrives, or from any
        # in-flight phase whose claim was cleared by the stale-claim sweeper
        # (Bug C — restart recovery).
        runnable_in_progress = {Phase.PAUSED, Phase.NEEDS_ATTENTION, Phase.WAITING_FOR_CHILDREN}
        if not (is_runnable(phase) or phase in runnable_in_progress or is_orphaned(work_item)):
            return False
        if str(metadata.get("runtime_model", "") or "").strip() == "multi_team_org":
            followup_task = (
                task_by_work_item_id.get(str(getattr(work_item, "work_item_id", "") or "").strip())
                if task_by_work_item_id is not None
                else None
            )
            followup_task_pending = (
                followup_task is not None
                and followup_task.status == TaskStatus.PENDING
                and bool((followup_task.metadata or {}).get("followup_routed_to_final_decider", False))
                and str((followup_task.metadata or {}).get("current_turn_mode", "") or "").strip() == "dispatch_required"
            )
            if (
                bool(metadata.get("followup_routed_to_final_decider", False))
                and str(metadata.get("current_turn_mode", "") or "").strip() == "dispatch_required"
                and (
                    followup_task is None
                    or followup_task_pending
                    or followup_task.status == TaskStatus.PENDING
                )
            ):
                return True
            dependency_ids = [
                str(item).strip()
                for item in list(metadata.get("dependency_work_item_ids", []) or [])
                if str(item).strip()
            ]
            dependency_ids, _pruned_dependency_ids = normalize_dependency_work_item_ids(
                dependency_ids,
                work_item_by_id,
                owner_work_item_id=str(getattr(work_item, "work_item_id", "") or "").strip(),
            )
            dependency_classes = dict(metadata.get("dependency_classes", {}) or {})
            settled_failure_ids = settled_failure_dependency_ids(metadata)
            for dep_id in dependency_ids:
                dependency = work_item_by_id.get(dep_id)
                if dependency is None:
                    continue
                dep_phase = dependency.phase
                dep_class = str(
                    dependency_classes.get(dep_id, DEPENDENCY_CLASS_DEFAULT)
                    or DEPENDENCY_CLASS_DEFAULT
                ).strip().lower()
                if dep_class == "info":
                    continue
                if dep_class == "soft":
                    if dep_phase not in DONE_PHASES and dep_phase not in IN_PROGRESS_PHASES:
                        if dep_id in settled_failure_ids:
                            continue
                        return False
                    continue
                if dep_phase != Phase.APPROVED:
                    # Failure-triage release: the frontier pass stamped this
                    # card's dependency_settlement over the dep (terminal
                    # failure, or stuck behind one), so it is settled
                    # context here, not a blocker.
                    if dep_id in settled_failure_ids:
                        continue
                    return False
            return True
        adaptive = cls._normalize_adaptive_metadata(metadata.get("adaptive", {}))
        dep_classes_map = dict(metadata.get("dependency_classes", {}) or {})
        all_dep_ids = list(dict.fromkeys([
            *[
                str(item).strip()
                for item in list(metadata.get("dependency_work_item_ids", []) or [])
                if str(item).strip()
            ],
            *list(adaptive.get("hard_dependency_work_item_ids", []) or []),
        ]))
        all_dep_ids, _pruned_dependency_ids = normalize_dependency_work_item_ids(
            all_dep_ids,
            work_item_by_id,
            owner_work_item_id=str(getattr(work_item, "work_item_id", "") or "").strip(),
        )
        settled_failure_ids = settled_failure_dependency_ids(metadata)
        for dep_id in all_dep_ids:
            # Default must match _dependency_release_state and the doomed
            # computation (both DEPENDENCY_CLASS_DEFAULT="hard"): a "soft"
            # default here let a card count as runnable while the frontier
            # counted it doomed — divergent verdicts on the same dep.
            dep_class = dep_classes_map.get(dep_id, DEPENDENCY_CLASS_DEFAULT)
            dependency = work_item_by_id.get(dep_id)
            if dependency is None:
                continue
            dep_phase = dependency.phase
            if dep_class == "hard" and dep_phase != Phase.APPROVED:
                if dep_id in settled_failure_ids:
                    continue
                return False
            if dep_class == "soft" and dep_phase not in DONE_PHASES and dep_phase not in IN_PROGRESS_PHASES:
                if dep_id in settled_failure_ids:
                    continue
                return False
        if str(adaptive.get("normalized_state", "") or "").strip().lower() == "invalidated":
            return False
        if list(adaptive.get("missing_decisions", []) or []):
            return False
        if not cls._required_signals_satisfied(adaptive):
            return False
        task = None
        if task_by_work_item_id is not None:
            task = task_by_work_item_id.get(str(work_item.work_item_id or "").strip())
        if not cls._required_artifacts_present(adaptive, task):
            return False
        turn_kind = cls._adaptive_turn_kind(adaptive, fallback=str(metadata.get("work_kind", "") or work_item.kind or "execute").strip().lower() or "execute")
        strict_gate_turn_kinds = cls._strict_gate_turn_kinds_for_metadata(metadata)
        if turn_kind in strict_gate_turn_kinds:
            return True
        mixed_gate_turn_kinds = cls._mixed_gate_turn_kinds_for_metadata(metadata)
        if turn_kind in mixed_gate_turn_kinds:
            return True
        return True


    def _task_effective_projection_spec(self, task: Task) -> WorkItemProjectionSpec:
        projection = self._projection_spec_for_task(task)
        if projection is not None:
            return projection
        return WorkItemProjectionSpec(
            projection_id=self._projection_id_for_task(task),
            turn_type=self._turn_type_for_task(task, fallback="execute"),
            title=str(task.title or "Runtime Work Item").strip() or "Runtime Work Item",
            summary=str(task.description or "").strip(),
            role_id=str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "executor").strip() or "executor",
            dependency_projection_ids=[],
            execution_strategy=WorkItemExecutionStrategy.AUTO.value,
            metadata=dict(task.metadata.get("work_item_metadata", {}) or {}),
        )


    @staticmethod
    def _work_item_effective_projection_spec(work_item: DelegationWorkItem) -> WorkItemProjectionSpec:
        return WorkItemProjectionSpec(
            projection_id=projection_id_for_work_item(work_item),
            turn_type=turn_type_for_work_item(work_item),
            title=str(work_item.title or work_item.projection_id or "Runtime Work Item").strip(),
            summary=str(work_item.summary or "").strip(),
            role_id=str(work_item.role_id or "executor").strip() or "executor",
            team_id=str(work_item.team_id or "").strip(),
            seat_id=str(work_item.seat_id or "").strip(),
            manager_role_id=str(work_item.manager_role_id or "").strip(),
            manager_seat_id=str(work_item.manager_seat_id or "").strip(),
            metadata=dict(work_item.metadata or {}),
        )


    def _build_runtime_coordination_spec(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        task: Task | None,
        work_item: DelegationWorkItem | None,
        plan: CompanyWorkItemRuntimePlan | None,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
        handoff_records: list[Any],
        work_item_decisions: list[Any],
    ) -> dict[str, Any]:
        helper = self.work_item_helper
        base_assignment = {
            "inputs": list((task.metadata.get("work_item_assignment", {}) or {}).get("inputs", []) if task else []),
            "deliverables": list((task.metadata.get("work_item_assignment", {}) or {}).get("deliverables", []) if task else []),
        }
        base_spec = helper._coordination_spec_dict(
            helper._build_coordination_spec(
                projection_spec=projection_spec,
                assignment=base_assignment,
                work_item_turn_type=self._infer_work_item_turn_type_for_task(task, projection_spec) if task is not None else helper._infer_work_item_turn_type(projection_spec),
                runtime_policy=dict((task.metadata or {}).get("policy") or (task.metadata or {}).get("runtime_policy", {}) if task is not None else (plan.metadata.get("policy") or plan.metadata.get("runtime_policy", {}) if plan is not None else {})),
                employee_assignment=dict((task.metadata or {}).get("employee_assignment", {}) if task is not None else {}),
            )
        )
        adaptive = self._normalize_adaptive_metadata(base_spec)
        turn_kind = self._adaptive_turn_kind(adaptive)
        work_item_by_id = {item.work_item_id: item for item in work_items}
        def _work_item_is_approved(candidate: DelegationWorkItem | None) -> bool:
            return candidate is not None and getattr(candidate, "phase", None) == Phase.APPROVED

        current_work_item_id = str(getattr(work_item, "work_item_id", "") or linked_work_item_id_for_task(task) or "").strip()
        emitted_signal_sources: dict[str, list[str]] = {signal: [] for signal in _CANONICAL_COORDINATION_SIGNALS}
        turn_kind_by_work_item: dict[str, str] = {}
        for candidate in work_items:
            candidate_task = next(
                (
                    item for item in tasks
                    if linked_work_item_id_for_task(item) == str(candidate.work_item_id or "").strip()
                ),
                None,
            )
            candidate_projection = self._task_effective_projection_spec(candidate_task) if candidate_task is not None else self._work_item_effective_projection_spec(candidate)
            candidate_adaptive = self._normalize_adaptive_metadata(
                dict((candidate_task.metadata or {}).get("adaptive", {}) if candidate_task is not None else (candidate.metadata or {}).get("adaptive", {}))
            )
            if not candidate_adaptive:
                candidate_adaptive = self._normalize_adaptive_metadata(
                    helper._coordination_spec_dict(
                        helper._build_coordination_spec(
                            projection_spec=candidate_projection,
                            assignment={"inputs": [], "deliverables": []},
                            work_item_turn_type=self._infer_work_item_turn_type_for_task(candidate_task, candidate_projection) if candidate_task is not None else helper._infer_work_item_turn_type(candidate_projection),
                            runtime_policy=dict((candidate_task.metadata or {}).get("policy") or (candidate_task.metadata or {}).get("runtime_policy", {}) if candidate_task is not None else (plan.metadata.get("policy") or plan.metadata.get("runtime_policy", {}) if plan is not None else {})),
                            employee_assignment=dict((candidate_task.metadata or {}).get("employee_assignment", {}) if candidate_task is not None else {}),
                        )
                    )
            )
            candidate_turn_kind = self._adaptive_turn_kind(candidate_adaptive, fallback=str((candidate.metadata or {}).get("work_kind", "") or candidate.kind or "execute").strip().lower() or "execute")
            turn_kind_by_work_item[candidate.work_item_id] = candidate_turn_kind
            if not _work_item_is_approved(candidate):
                continue
            for emitted in list(candidate_adaptive.get("emitted_signals", []) or []):
                signal_name = str(emitted).strip()
                if signal_name:
                    emitted_signal_sources.setdefault(signal_name, []).append(candidate.work_item_id)
        hard_dependency_ids = [
            str(item).strip()
            for item in list((work_item.metadata or {}).get("dependency_work_item_ids", []) if work_item is not None else [])
            if str(item).strip()
        ]
        # Cell-scoped inferred dependencies: only add hard deps within the same
        # cell (or when no cell_id is set).  Cross-cell work items are treated as
        # soft / info by the flexible dependency system and should not create
        # implicit hard blockers.
        current_cell = str((work_item.metadata or {}).get("cell_id", "") or (work_item.metadata or {}).get("delegation_cell_id", "") or "").strip() if work_item is not None else ""

        def _same_cell(candidate: "DelegationWorkItem") -> bool:
            if not current_cell:
                return True
            candidate_cell = str((candidate.metadata or {}).get("cell_id", "") or (candidate.metadata or {}).get("delegation_cell_id", "") or "").strip()
            return not candidate_cell or candidate_cell == current_cell

        if turn_kind == "execute":
            hard_dependency_ids.extend(
                item.work_item_id
                for item in work_items
                if item.work_item_id != current_work_item_id
                and turn_kind_by_work_item.get(item.work_item_id) in {"setup", "acquire"}
                and _same_cell(item)
            )
        elif turn_kind == "verify":
            hard_dependency_ids.extend(
                item.work_item_id
                for item in work_items
                if item.work_item_id != current_work_item_id
                and turn_kind_by_work_item.get(item.work_item_id) in {"setup", "acquire", "execute"}
                and _same_cell(item)
            )
        elif turn_kind == "deliver":
            hard_dependency_ids.extend(
                item.work_item_id
                for item in work_items
                if item.work_item_id != current_work_item_id
                and turn_kind_by_work_item.get(item.work_item_id) != "deliver"
                and _same_cell(item)
            )
        hard_dependency_ids = list(dict.fromkeys(item for item in hard_dependency_ids if item))
        required_decisions: list[str] = []
        missing_decisions: list[str] = []
        manager_release_satisfied = False
        mixed_gate_turn_kinds = self._mixed_gate_turn_kinds_for_metadata(
            task.metadata if task is not None else (work_item.metadata if work_item is not None else {})
        )
        decision_gate_requested = bool(
            (
                (work_item.metadata or {}).get("needs_manager_attention", False)
                or work_item.phase == Phase.NEEDS_ATTENTION
            )
            if work_item is not None
            else False
        )
        if turn_kind in mixed_gate_turn_kinds and decision_gate_requested:
            requirement = f"manager_release:{projection_spec.projection_id}"
            required_decisions.append(requirement)
            matching_decisions = []
            for record in work_item_decisions:
                record_projection_id = str(getattr(record, "projection_id", "") or "").strip()
                details = dict(getattr(record, "details", {}) or {})
                if record_projection_id == str(projection_spec.projection_id or "").strip():
                    matching_decisions.append(record)
                    continue
                target_projection_id = str(
                    details.get("target_projection_id")
                    or ""
                ).strip()
                if target_projection_id == str(projection_spec.projection_id or "").strip():
                    matching_decisions.append(record)
                    continue
                if current_work_item_id and str(details.get("target_work_item_id", "") or "").strip() == current_work_item_id:
                    matching_decisions.append(record)
            manager_release_satisfied = bool(matching_decisions)
            if not manager_release_satisfied:
                missing_decisions.append(requirement)
        normalized_signals: list[dict[str, Any]] = []
        has_setup_work_item = any(kind == "setup" for kind in turn_kind_by_work_item.values())
        has_acquire_work_item = any(kind == "acquire" for kind in turn_kind_by_work_item.values())
        has_verify_work_item = any(kind == "verify" for kind in turn_kind_by_work_item.values())
        non_provider_execute = [
            work_item_id
            for work_item_id, candidate_turn_kind in turn_kind_by_work_item.items()
            if candidate_turn_kind == "execute"
        ]
        for signal in list(adaptive.get("signals", []) or []):
            signal_name = str(signal.get("name", "") or "").strip()
            evidence: list[str] = []
            satisfied = False
            if signal_name == "scope_locked":
                satisfied = all(
                    (dependency := work_item_by_id.get(dep_id)) is not None
                    and _work_item_is_approved(dependency)
                    for dep_id in [
                        str(item).strip()
                        for item in list((work_item.metadata or {}).get("dependency_work_item_ids", []) if work_item is not None else [])
                        if str(item).strip()
                    ]
                )
                if not work_item or not list((work_item.metadata or {}).get("dependency_work_item_ids", []) or []):
                    satisfied = True
                if satisfied:
                    evidence.append("manager_scope_ready")
            elif signal_name == "env_ready":
                satisfied = True if not has_setup_work_item else bool(emitted_signal_sources.get("env_ready"))
                evidence = list(emitted_signal_sources.get("env_ready", []) or [])
            elif signal_name == "inputs_ready":
                satisfied = True if not has_acquire_work_item else bool(emitted_signal_sources.get("inputs_ready"))
                evidence = list(emitted_signal_sources.get("inputs_ready", []) or [])
            elif signal_name == "implementation_ready":
                satisfied = True if not non_provider_execute else all(
                    _work_item_is_approved(work_item_by_id.get(dep_id))
                    for dep_id in non_provider_execute
                    if dep_id != current_work_item_id
                )
                evidence = [dep_id for dep_id in non_provider_execute if dep_id != current_work_item_id]
            elif signal_name == "qa_ready":
                verify_items = [dep_id for dep_id, candidate_turn_kind in turn_kind_by_work_item.items() if candidate_turn_kind == "verify" and dep_id != current_work_item_id]
                satisfied = True if not has_verify_work_item else all(
                    _work_item_is_approved(work_item_by_id.get(dep_id))
                    for dep_id in verify_items
                )
                evidence = verify_items
            elif signal_name == "delivery_ready":
                remaining = [
                    item.work_item_id
                    for item in work_items
                    if item.work_item_id != current_work_item_id
                    and turn_kind_by_work_item.get(item.work_item_id) != "deliver"
                ]
                satisfied = all(
                    _work_item_is_approved(work_item_by_id.get(dep_id))
                    for dep_id in remaining
                )
                evidence = remaining
            else:
                signal_sources = list(emitted_signal_sources.get(signal_name, []) or [])
                satisfied = bool(signal_sources)
                evidence = signal_sources
            normalized_signals.append(
                {
                    **dict(signal),
                    "name": signal_name,
                    "satisfied": satisfied,
                    "evidence": evidence,
                }
            )
        adaptive["signals"] = normalized_signals
        adaptive["hard_dependency_work_item_ids"] = hard_dependency_ids
        adaptive["soft_dependency_work_item_ids"] = []
        adaptive["required_decisions"] = required_decisions
        adaptive["missing_decisions"] = missing_decisions
        adaptive["manager_release_satisfied"] = manager_release_satisfied
        adaptive["required_artifacts"] = list(adaptive.get("required_artifacts", []) or [])
        adaptive["evidence"] = list(dict.fromkeys([
            *list(adaptive.get("evidence", []) or []),
            f"work_item_decisions:{len(work_item_decisions)}",
            f"work_item_release_decisions:{int(manager_release_satisfied)}/{len(required_decisions)}",
        ]))
        missing_dependencies = [
            dep_id
            for dep_id in hard_dependency_ids
            if not _work_item_is_approved(work_item_by_id.get(dep_id))
        ]
        missing_signals = [
            str(item.get("name", "") or "").strip()
            for item in normalized_signals
            if bool(item.get("required", True)) and not bool(item.get("satisfied", False))
        ]
        manager_attention_pending = (
            decision_gate_requested
            and bool(required_decisions)
            and not manager_release_satisfied
            and not missing_dependencies
            and not missing_signals
        )
        if str((task.metadata or {}).get("upstream_ceo_rework_source_projection_id", "") if task is not None else "").strip():
            adaptive["normalized_state"] = "invalidated"
        elif missing_dependencies:
            adaptive["normalized_state"] = "waiting_for_deps"
        elif manager_attention_pending:
            adaptive["normalized_state"] = "needs_manager_attention"
        elif missing_decisions or missing_signals:
            adaptive["normalized_state"] = "waiting_for_gate" if turn_kind in self._strict_gate_turn_kinds_for_metadata(task.metadata if task is not None else (work_item.metadata if work_item is not None else {})) else "waiting_for_deps"
        else:
            phase_value = (
                work_item.phase.value
                if work_item is not None
                else (task.status.value if task is not None else "planned")
            )
            state_map = {
                Phase.RUNNING.value: "running",
                Phase.APPROVED.value: "done",
                Phase.FAILED.value: "failed",
                Phase.CANCELLED.value: "cancelled",
                Phase.WAITING_FOR_PEER.value: "awaiting_peer",
                Phase.WAITING_FOR_CHILDREN.value: "blocked",
                Phase.AWAITING_MANAGER_REVIEW.value: "awaiting_manager_review",
                Phase.AWAITING_HUMAN.value: "awaiting_human",
                Phase.PAUSED.value: "blocked",
                Phase.NEEDS_ATTENTION.value: "needs_manager_attention",
                Phase.WAITING_DEPENDENCIES.value: "waiting_for_deps",
                Phase.QUEUED.value: "ready",
                Phase.READY.value: "ready",
                Phase.READY_FOR_REWORK.value: "ready",
            }
            adaptive["normalized_state"] = state_map.get(phase_value, "planned")
        adaptive["blocked_reason"] = ""
        if missing_dependencies:
            adaptive["blocked_reason"] = f"Waiting for hard dependencies: {', '.join(missing_dependencies)}"
        elif missing_decisions:
            adaptive["blocked_reason"] = f"Waiting for runtime decision: {', '.join(missing_decisions)}"
        elif missing_signals:
            adaptive["blocked_reason"] = f"Waiting for required signals: {', '.join(missing_signals)}"
        elif adaptive["normalized_state"] == "invalidated":
            adaptive["blocked_reason"] = "Upstream gated work item re-entered rework; this work item was invalidated."
        return adaptive


    async def _refresh_adaptive_coordination(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> tuple[list[Task], list[DelegationWorkItem]]:
        if not tasks:
            return tasks, work_items
        handoff_records: list[Any] = []
        work_item_decisions: list[Any] = []
        if self.store and hasattr(self.store, "get_handoff_records"):
            try:
                handoff_records = await self.store.get_handoff_records(project_id=tasks[0].project_id, limit=50)
            except Exception:
                handoff_records = []
        if self.store and hasattr(self.store, "get_work_item_decisions"):
            try:
                work_item_decisions = await self.store.get_work_item_decisions(project_id=tasks[0].project_id, limit=50)
            except Exception:
                work_item_decisions = []
        task_by_work_item_id = {
            work_item_id: task
            for work_item_id, task in (await self._task_by_work_item_id(tasks)).items()
            if not bool((task.metadata or {}).get("synthetic_inbox_turn", False))
        }
        changed_work_items: list[DelegationWorkItem] = []
        for task in tasks:
            projection_spec = self._task_effective_projection_spec(task)
            task.metadata = dict(task.metadata)
            task.metadata["adaptive"] = self._build_runtime_coordination_spec(
                projection_spec=projection_spec,
                task=task,
                work_item=None,
                plan=self._active_plan,
                tasks=tasks,
                work_items=work_items,
                handoff_records=handoff_records,
                work_item_decisions=work_item_decisions,
            )
        for work_item in work_items:
            task = task_by_work_item_id.get(str(work_item.work_item_id or "").strip())
            projection_spec = self._task_effective_projection_spec(task) if task is not None else self._work_item_effective_projection_spec(work_item)
            adaptive = self._build_runtime_coordination_spec(
                projection_spec=projection_spec,
                task=task,
                work_item=work_item,
                plan=self._active_plan,
                tasks=tasks,
                work_items=work_items,
                handoff_records=handoff_records,
                work_item_decisions=work_item_decisions,
            )
            metadata = dict(work_item.metadata or {})
            if bool(adaptive.get("manager_release_satisfied", False)):
                metadata["needs_manager_attention"] = False
                if work_item.phase == Phase.NEEDS_ATTENTION and self.store:
                    await self.store.update_delegation_work_item(
                        work_item.work_item_id,
                        phase=Phase.RUNNING,
                    )
                    refreshed = await self.store.get_delegation_work_item(work_item.work_item_id)
                    if refreshed is not None:
                        work_item.phase = refreshed.phase
            if metadata.get("adaptive") != adaptive:
                metadata["adaptive"] = adaptive
                metadata["needs_manager_attention"] = bool(metadata.get("needs_manager_attention", False))
                work_item.metadata = metadata
                work_item.blocked_reason = str(adaptive.get("blocked_reason", "") or "").strip()
                changed_work_items.append(work_item)
            elif work_item.blocked_reason != str(adaptive.get("blocked_reason", "") or "").strip():
                work_item.blocked_reason = str(adaptive.get("blocked_reason", "") or "").strip()
                changed_work_items.append(work_item)
        for work_item in changed_work_items:
            if self.store:
                await self.store.save_delegation_work_item(work_item)
        run_id = str((work_items[0].run_id if work_items else "") or (tasks[0].metadata.get("delegation_run_id", "") if tasks else "") or "").strip()
        if run_id and self.store and hasattr(self.store, "get_delegation_run") and hasattr(self.store, "save_delegation_run"):
            run = await self.store.get_delegation_run(run_id)
            if run is not None:
                run_metadata = dict(run.metadata or {})
                compiled_coordination_spec = {
                    "version": 1,
                    "company_profile": str(getattr(run, "company_profile", "") or ""),
                    "execution_model": str(getattr(run, "execution_model", "") or ""),
                    "tasks": {
                        self._projection_id_for_task(task): dict(
                            self._normalize_adaptive_metadata((task.metadata or {}).get("adaptive", {}))
                        )
                        for task in tasks
                        if self._projection_id_for_task(task)
                    },
                    "work_items": {
                        str(item.work_item_id or "").strip(): dict(
                            self._normalize_adaptive_metadata((item.metadata or {}).get("adaptive", {}))
                        )
                        for item in work_items
                        if str(item.work_item_id or "").strip()
                    },
                }
                if run_metadata.get("coordination_spec") != compiled_coordination_spec:
                    run_metadata["coordination_spec"] = compiled_coordination_spec
                    run.metadata = run_metadata
                    await self.store.save_delegation_run(run)
        return tasks, work_items


    @staticmethod
    def _normalize_follow_up_actions(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        allowed_actions = {"delegate_rereview", "delegate_rework", "delegate_followup"}
        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action", "") or "").strip().lower()
            target_role_id = str(item.get("target_role_id", "") or "").strip()
            if action not in allowed_actions or not target_role_id:
                continue
            normalized.append(
                {
                    "action": action,
                    "target_role_id": target_role_id,
                    "title": str(item.get("title", "") or "").strip(),
                    "summary": str(item.get("summary", "") or "").strip(),
                    "reason": str(item.get("reason", "") or "").strip(),
                    "scope_key": str(item.get("scope_key", "") or "").strip(),
                    "dedupe_key": str(item.get("dedupe_key", "") or "").strip(),
                    "depends_on_work_item_ids": [
                        str(dep).strip()
                        for dep in list(item.get("depends_on_work_item_ids", []) or [])
                        if str(dep).strip()
                    ],
                }
            )
        return normalized


    def _apply_work_item_projection_to_task(
        self,
        task: Task,
        work_item: DelegationWorkItem,
    ) -> bool:
        """Project the WorkItem source-of-truth envelope onto its runtime Task.

        Existing runtime Tasks can outlive manager mutations. When the final decider or a
        manager calls ``modify_work_item`` after Stop, the WorkItem title,
        summary, prompt contract, and mutation metadata are updated immediately,
        while the Task row may still contain the pre-Stop prompt. Refresh the
        execution-copy fields before the Task is claimed again so resumed
        external agents receive the revised contract.
        """
        changed = False

        projected_status = task_status_for_phase(work_item.phase)
        if task.status != projected_status:
            task.status = projected_status
            changed = True

        title = str(getattr(work_item, "title", "") or "").strip()
        if title and task.title != title:
            task.title = title
            changed = True

        summary = str(getattr(work_item, "summary", "") or "").strip()
        if summary and task.description != summary:
            task.description = summary
            changed = True

        before_metadata = dict(task.metadata or {})
        task.metadata = dict(before_metadata)
        work_item_metadata = dict(work_item.metadata or {})
        work_kind = str(
            work_item_metadata.get("work_kind")
            or work_item_metadata.get("delegation_turn_kind")
            or work_item.kind
            or ""
        ).strip().lower()
        if work_kind:
            canonical_turn_type = self._runtime_work_kind_to_work_item_turn_type(work_kind)
            task.metadata["work_kind"] = work_kind
            task.metadata["delegation_turn_kind"] = work_kind
            task.metadata = mark_projected_work_item_task(
                task.metadata,
                projection_id=self._projection_id_for_task(task),
                turn_type=canonical_turn_type,
            )
            task.metadata[WORK_ITEM_TURN_TYPE_KEY] = canonical_turn_type

        execution_metadata = copy_work_item_execution_metadata(work_item)
        for key in _STALE_REWORK_TASK_METADATA_KEYS:
            if key not in execution_metadata:
                task.metadata.pop(key, None)
        task.metadata.update(execution_metadata)

        dispatch_hold = str(work_item_metadata.get("dispatch_hold", "") or "").strip()
        if dispatch_hold:
            task.metadata["dispatch_hold"] = dispatch_hold
        else:
            for key in _COMPANY_RUNTIME_CONTROL_TASK_METADATA_KEYS:
                task.metadata.pop(key, None)

        runtime_plan = dict(task.metadata.get("work_item_runtime_plan", {}) or {})
        if runtime_plan:
            projection_id = projection_id_for_work_item(work_item)
            if projection_id:
                runtime_plan["projection_id"] = projection_id
            if work_kind:
                runtime_plan["turn_type"] = self._runtime_work_kind_to_work_item_turn_type(work_kind)
            if summary:
                runtime_plan["summary"] = summary
            task.metadata["work_item_runtime_plan"] = runtime_plan

        task.metadata["derived_work_item_projection"] = {
            "work_item_id": work_item.work_item_id,
            "projection_id": projection_id_for_work_item(work_item),
            **work_item_identity_payload(
                projection_id=projection_id_for_work_item(work_item),
                turn_type="",
            ),
            "kind": work_item.kind,
            "phase": work_item.phase.value,
            "kanban_column": kanban_column(work_item.phase),
            "cell_id": work_item.cell_id,
            "parent_work_item_id": work_item.parent_work_item_id,
        }
        if task.metadata != before_metadata:
            changed = True
        return changed


    async def _sync_task_projection_from_work_items(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> None:
        task_by_work_item_id = {
            work_item_id: task
            for work_item_id, task in task_by_linked_work_item_id(tasks).items()
            if not bool((task.metadata or {}).get("synthetic_inbox_turn", False))
        }
        for work_item in work_items:
            task = task_by_work_item_id.get(str(work_item.work_item_id or "").strip())
            if task is None:
                continue
            changed = self._apply_work_item_projection_to_task(task, work_item)
            if changed and self.store and hasattr(self.store, "save_task"):
                try:
                    await self.store.save_task(task)
                except Exception:
                    logger.opt(exception=True).debug("Best-effort runtime Task projection sync failed")


    async def _refresh_ready_work_items(
        self,
        work_items: list[DelegationWorkItem],
        *,
        tasks: list[Task] | None = None,
    ) -> list[DelegationWorkItem]:
        if not self.store or not work_items:
            return work_items
        work_items = await self._reconcile_missing_review_chain(work_items)
        work_item_by_id = {item.work_item_id: item for item in work_items}
        changed = False
        for work_item in work_items:
            metadata = dict(work_item.metadata or {})
            release_policy = str(metadata.get("release_policy", "auto") or "auto").strip().lower()
            dependency_state = self._dependency_release_state(work_item, work_item_by_id)
            # Auto-release: QUEUED + release_policy=auto → READY (or
            # WAITING_DEPENDENCIES when upstream isn't done).
            if work_item.phase == Phase.QUEUED and release_policy == "auto":
                target = (
                    Phase.WAITING_DEPENDENCIES
                    if dependency_state["dependency_ids"] and not dependency_state["satisfied"]
                    else Phase.READY
                )
                await self.store.update_delegation_work_item(
                    work_item.work_item_id,
                    phase=target,
                    blocked_reason="" if target == Phase.READY else None,
                    metadata_updates=dependency_state["metadata_updates"] or None,
                )
                changed = True
                continue
            # WAITING_DEPENDENCIES → READY when all upstream is approved.
            if work_item.phase == Phase.WAITING_DEPENDENCIES and dependency_state["satisfied"]:
                target_phase = (
                    Phase.READY_FOR_REWORK
                    if str(metadata.get("rework_feedback", "") or "").strip()
                    else Phase.READY
                )
                await self.store.update_delegation_work_item(
                    work_item.work_item_id,
                    phase=target_phase,
                    blocked_reason="",
                    metadata_updates=dependency_state["metadata_updates"] or None,
                )
                changed = True
            elif work_item.phase == Phase.WAITING_DEPENDENCIES and dependency_state["metadata_updates"]:
                await self.store.update_delegation_work_item(
                    work_item.work_item_id,
                    metadata_updates=dependency_state["metadata_updates"],
                )
                changed = True
        # Failure frontier for late-created cards: a delivery/aggregate card
        # created AFTER its dependency already failed never sees a failure
        # transition hook, and the per-item pass above only releases on
        # all-approved. Detection is cheap and idempotent — released cards
        # (stamp present, phase moved) stop matching.
        if has_pending_settlement_release(work_item_by_id):
            run_id = str(work_items[0].run_id or "").strip()
            if run_id:
                try:
                    if await refresh_dependents_for_run(self.store, run_id=run_id):
                        changed = True
                except Exception:
                    logger.opt(exception=True).debug(
                        "Best-effort settlement frontier refresh failed for run "
                        f"{run_id}"
                    )
        if not changed:
            return work_items
        try:
            self._signal_dispatcher_wake()
        except Exception:
            logger.opt(exception=True).debug("Best-effort dispatcher wake after dependency release failed")
        try:
            await self._notify_kanban_changed()
        except Exception:
            logger.opt(exception=True).debug("Best-effort kanban notify after dependency release failed")
        run_id = str(work_items[0].run_id or "").strip()
        return await self.store.list_delegation_work_items(run_id)


    @staticmethod
    def _dependency_release_state(
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem],
    ) -> dict[str, Any]:
        metadata = dict(getattr(work_item, "metadata", {}) or {})
        raw_dependency_ids = [
            str(item).strip()
            for item in list(metadata.get("dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        dependency_ids, pruned_dependency_ids = normalize_dependency_work_item_ids(
            raw_dependency_ids,
            work_item_by_id,
            owner_work_item_id=str(getattr(work_item, "work_item_id", "") or "").strip(),
        )
        metadata_updates: dict[str, Any] = {}
        if dependency_ids != raw_dependency_ids:
            metadata_updates["dependency_work_item_ids"] = list(dependency_ids)
            metadata_updates["dependency_pruned_at"] = datetime.now().isoformat()
        if pruned_dependency_ids:
            previous_pruned = [
                str(item).strip()
                for item in list(metadata.get("pruned_dependency_work_item_ids", []) or [])
                if str(item).strip()
            ]
            metadata_updates["pruned_dependency_work_item_ids"] = list(
                dict.fromkeys([*previous_pruned, *pruned_dependency_ids])
            )

        dependency_classes = dict(metadata.get("dependency_classes", {}) or {})
        waiting_on: list[str] = []
        for dep_id in dependency_ids:
            dependency = work_item_by_id.get(dep_id)
            dep_phase = getattr(dependency, "phase", None) if dependency is not None else None
            if not isinstance(dep_phase, Phase):
                try:
                    dep_phase = Phase(str(dep_phase or ""))
                except Exception:
                    dep_phase = None
            dep_class = str(dependency_classes.get(dep_id, "hard") or "hard").strip().lower()
            if dep_class == "info":
                continue
            if dep_class == "soft":
                if dep_phase not in DONE_PHASES and dep_phase not in IN_PROGRESS_PHASES:
                    waiting_on.append(dep_id)
                continue
            if dep_phase != Phase.APPROVED:
                waiting_on.append(dep_id)

        if waiting_on:
            if list(metadata.get("waiting_on_work_item_ids", []) or []) != waiting_on:
                metadata_updates["waiting_on_work_item_ids"] = waiting_on
        elif list(metadata.get("waiting_on_work_item_ids", []) or []):
            metadata_updates["waiting_on_work_item_ids"] = []

        return {
            "dependency_ids": dependency_ids,
            "metadata_updates": metadata_updates,
            "satisfied": not waiting_on,
            "waiting_on": waiting_on,
        }


    async def _reconcile_missing_review_chain(
        self,
        work_items: list[DelegationWorkItem],
    ) -> list[DelegationWorkItem]:
        """Converge every passive review parent to one report/review chain.

        ``Phase.AWAITING_MANAGER_REVIEW`` is the sole scheduling predicate.
        Auxiliary WorkItems are the durable journal: an active card is
        reused; a completed report whose review write was interrupted is
        consumed directly; otherwise a fresh report attempt is created.
        Runtime Tasks and parent attempt counters are never required.
        """
        if not self.store or not work_items or not hasattr(self.store, "save_delegation_work_item"):
            return work_items
        waiting = [
            item for item in work_items
            if item.phase == Phase.AWAITING_MANAGER_REVIEW
        ]
        if not waiting:
            return work_items
        repaired_ids: list[str] = []
        for parent in waiting:
            target_id = parent.work_item_id
            active_report = self._active_auxiliary_item(
                work_items,
                target_id,
                kind="report",
            )
            if active_report is not None:
                continue

            reports = self._targeting_auxiliary_items(
                work_items,
                target_id,
                kind="report",
            )
            reviews = self._targeting_auxiliary_items(
                work_items,
                target_id,
                kind="review",
            )
            applied_reports = [
                report
                for report in reports
                if report.phase in DONE_PHASES
                and str((report.metadata or {}).get("report_card_outcome", "") or "").strip()
                == "applied"
            ]
            source_report = applied_reports[-1] if applied_reports else None
            linked_reviews = []
            if source_report is not None:
                linked_reviews = [
                    review
                    for review in reviews
                    if str(
                        (review.metadata or {}).get(
                            "review_source_report_work_item_id", ""
                        )
                        or ""
                    ).strip()
                    == source_report.work_item_id
                ]
            latest_linked_review = linked_reviews[-1] if linked_reviews else None
            resolution = dict(
                (getattr(latest_linked_review, "metadata", {}) or {}).get(
                    "review_resolution", {}
                )
                or {}
            )
            resolution_applied_id = str(
                (parent.metadata or {}).get(
                    "review_resolution_applied_work_item_id", ""
                )
                or ""
            ).strip()
            resolution_state = str(
                (getattr(latest_linked_review, "metadata", {}) or {}).get(
                    "review_resolution_state", ""
                )
                or ""
            ).strip()
            if (
                latest_linked_review is not None
                and resolution
                and resolution_state != "stale"
                and resolution_applied_id != latest_linked_review.work_item_id
            ):
                try:
                    applied = await self._apply_review_resolution(
                        latest_linked_review,
                        parent,
                    )
                except Exception:
                    logger.opt(exception=True).warning(
                        "Failed to project durable review resolution for "
                        f"work_item_id={target_id}"
                    )
                    applied = None
                if applied is not None:
                    repaired_ids.append(target_id)
                # The terminal verdict is authoritative. Do not start a new
                # handoff chain until that exact resolution is projected.
                continue

            active_review = self._active_auxiliary_item(
                work_items,
                target_id,
                kind="review",
            )
            active_review_source_id = str(
                (getattr(active_review, "metadata", {}) or {}).get(
                    "review_source_report_work_item_id", ""
                )
                or ""
            ).strip()
            if active_review is not None and (
                source_report is None
                or active_review_source_id == source_report.work_item_id
            ):
                continue
            linked_outcome = str(
                (getattr(latest_linked_review, "metadata", {}) or {}).get(
                    "review_work_item_outcome", ""
                )
                or ""
            ).strip()
            source_needs_review = source_report is not None and (
                latest_linked_review is None
                or linked_outcome == "verdict_parse_failed"
            )
            try:
                if source_needs_review and source_report is not None:
                    source_metadata = dict(source_report.metadata or {})
                    completion_report = str(
                        source_metadata.get("completion_report", "") or ""
                    ).strip()
                    parent_updates = {
                        "completion_report": completion_report,
                        "review_evidence": copy.deepcopy(
                            dict(source_metadata.get("review_evidence", {}) or {})
                        ),
                        "report_completion_raw": str(
                            source_metadata.get("report_completion_raw", "") or ""
                        ),
                    }
                    # Repair the parent projection before making review
                    # runnable. If this write fails, the terminal report
                    # remains the retryable durability point.
                    await self.store.update_delegation_work_item(
                        target_id,
                        metadata_updates=parent_updates,
                    )
                    retry_updates: dict[str, Any] = {}
                    if linked_outcome == "verdict_parse_failed":
                        retry_updates = {
                            "review_retry_hint": _REVIEW_VERDICT_PARSE_RETRY_HINT,
                            "review_retry_reason": "verdict_parse_failed",
                            "review_retry_of_attempt": self._auxiliary_attempt_number(
                                latest_linked_review,
                                kind="review",
                            ),
                        }
                    spawned = await self._ensure_review_work_item_for_work_item(
                        target_id,
                        completion_report=completion_report,
                        metadata_updates=retry_updates,
                        source_report_item=source_report,
                        run_items=work_items,
                    )
                else:
                    # No unconsumed durable report exists. This is either the
                    # first handoff for the phase or a later rework cycle.
                    spawned = await self._ensure_report_work_item_for_work_item(
                        target_id,
                        run_items=work_items,
                    )
            except Exception:
                logger.opt(exception=True).warning(
                    "Failed to reconcile report/review chain for "
                    f"work_item_id={target_id}"
                )
                continue
            if spawned is not None:
                repaired_ids.append(target_id)
        if not repaired_ids:
            return work_items
        logger.info(
            "Reconciled report/review chains for work items: "
            + ", ".join(repaired_ids)
        )
        run_id = str(work_items[0].run_id or "").strip()
        if run_id and hasattr(self.store, "list_delegation_work_items"):
            return await self.store.list_delegation_work_items(run_id)
        return work_items


    async def _reconcile_role_serial_queues(
        self,
        work_items: list[DelegationWorkItem],
    ) -> list[DelegationWorkItem]:
        if not self.store or not work_items:
            return work_items
        run_id = str(work_items[0].run_id or "").strip()
        if not run_id:
            return work_items
        result = await reconcile_role_serial_queues(self.store, run_id)
        if (
            result.get("cleared_markers")
            or result.get("pruned_pending_ids")
            or result.get("promoted_work_item_ids")
            or result.get("cleared_focus_session_ids")
        ):
            return await self.store.list_delegation_work_items(run_id)
        return work_items


    async def _promote_manager_work_items_from_inbox(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> list[DelegationWorkItem]:
        if not self.store or not work_items:
            return work_items
        session_by_key: dict[str, CompanyMemberSession] = {}
        for session in self.runtime.member_sessions.values():
            seat_id = str(session.seat_id or (session.metadata or {}).get("seat_id", "") or "").strip()
            if seat_id:
                session_by_key[seat_id] = session
            if str(session.role_id or "").strip():
                session_by_key.setdefault(str(session.role_id).strip(), session)
        changed = False
        task_by_work_item_id = await self._task_by_work_item_id(tasks)
        for work_item in work_items:
            metadata = dict(work_item.metadata or {})
            phase = work_item.phase
            manager_session = (
                session_by_key.get(str(work_item.manager_seat_id or "").strip())
                or session_by_key.get(str(work_item.manager_role_id or "").strip())
            )
            if manager_session is not None and phase == Phase.QUEUED:
                matched, matched_msg_id = self._mailbox_release_matched(manager_session, work_item)
                if matched:
                    metadata["mailbox_release_satisfied"] = True
                    metadata["mailbox_release_message_id"] = matched_msg_id
                    metadata["mailbox_release_checked_at"] = datetime.now().isoformat()
                    await self.store.update_delegation_work_item(
                        work_item.work_item_id,
                        phase=Phase.READY,
                        metadata_updates=metadata,
                    )
                    work_item = await self.store.get_delegation_work_item(work_item.work_item_id) or work_item
                    metadata = dict(work_item.metadata or {})
                    phase = work_item.phase
                    changed = True
                    if hasattr(self.store, "save_delegation_event"):
                        try:
                            await self.store.save_delegation_event(
                                DelegationEvent(
                                    run_id=str(work_item.run_id or "").strip(),
                                    work_item_id=work_item.work_item_id,
                                    cell_id=work_item.cell_id,
                                    role_id=work_item.role_id,
                                    event_type="manager_work_item_released_from_mailbox",
                                    payload={
                                        "manager_seat_id": str(work_item.manager_seat_id or "").strip(),
                                        "message_id": matched_msg_id,
                                        "release_policy": str(metadata.get("release_policy", "") or "").strip(),
                                    },
                                )
                            )
                        except Exception:
                            logger.debug("Best-effort mailbox release event persistence failed")
            if phase in DONE_PHASES:
                continue
            work_kind = str(
                metadata.get("work_kind")
                or metadata.get("delegation_turn_kind")
                or work_item.kind
                or ""
            ).strip().lower()
            if work_kind not in {"aggregate", "deliver", "synthesize", "review"}:
                continue
            session = session_by_key.get(str(work_item.seat_id or "").strip()) or session_by_key.get(str(work_item.role_id or "").strip())
            if session is None:
                continue
            fingerprint = self._manager_inbox_fingerprint(session)
            if not fingerprint:
                continue
            last_fingerprint = str(metadata.get("last_ready_from_inbox_fingerprint", "") or "").strip()
            if fingerprint == last_fingerprint:
                continue
            metadata["last_ready_from_inbox_fingerprint"] = fingerprint
            metadata["needs_manager_attention"] = True
            task = task_by_work_item_id.get(str(work_item.work_item_id or "").strip())
            if task is not None:
                metadata["last_ready_from_checkpoint_basis_hash"] = self._checkpoint_basis_hash(task)
            target_phase = phase
            if phase == Phase.RUNNING:
                target_phase = Phase.NEEDS_ATTENTION
            await self.store.update_delegation_work_item(
                work_item.work_item_id,
                phase=target_phase if target_phase != phase else None,
                metadata_updates=metadata,
            )
            if task is not None and phase in IN_REVIEW_PHASES:
                supersede = getattr(self.store, "supersede_pending_checkpoints", None)
                if callable(supersede):
                    await supersede(
                        project_id=task.project_id or "default",
                        task_id=task.id,
                        checkpoint_types=["company_work_item_gate"],
                    )
            session_status = normalize_role_runtime_status(
                session.status,
                session.focused_work_item_id,
            )
            if session_status != "running":
                session.status = "idle"
                session.resident_status = "idle"
                session.focused_work_item_id = ""
                role_session = self.runtime._role_session_for_member_session(session)
                if role_session is not None:
                    role_session.status = "idle"
                    role_session.focused_work_item_id = ""
                    role_session.updated_at = datetime.now()
                    if hasattr(self.store, "save_delegation_role_session"):
                        await self.store.save_delegation_role_session(role_session)
                await self.runtime._persist_session(session, task=task)
            changed = True
            if hasattr(self.store, "save_delegation_event"):
                try:
                    await self.store.save_delegation_event(
                        DelegationEvent(
                            run_id=str(work_item.run_id or "").strip(),
                            work_item_id=work_item.work_item_id,
                            cell_id=work_item.cell_id,
                            role_id=work_item.role_id,
                            event_type="manager_work_item_promoted",
                            payload={
                                "seat_id": work_item.seat_id,
                                "fingerprint": fingerprint,
                                "work_kind": work_kind,
                                "previous_status": str(phase),
                                "needs_manager_attention": True,
                            },
                        )
                    )
                except Exception:
                    logger.debug("Best-effort manager promotion event persistence failed")
        if not changed:
            return work_items
        run_id = str(work_items[0].run_id or "").strip()
        return await self.store.list_delegation_work_items(run_id)


    async def _task_by_work_item_id(self, tasks: list[Task]) -> dict[str, Task]:
        if self.store and hasattr(self.store, "hydrate_task_work_item_links"):
            try:
                await self.store.hydrate_task_work_item_links(tasks)
            except Exception:
                logger.opt(exception=True).debug("_task_by_work_item_id: link hydration failed")
        return task_by_linked_work_item_id(tasks)


    async def _work_item_id_for_task(self, task: Task | None) -> str:
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id or task is None or not self.store:
            return work_item_id
        get_work_item_for_task = getattr(self.store, "get_work_item_for_runtime_task", None)
        if not callable(get_work_item_for_task):
            return ""
        try:
            item = await get_work_item_for_task(task.id)
        except Exception:
            logger.opt(exception=True).debug("_work_item_id_for_task: link lookup failed")
            return ""
        work_item_id = str(getattr(item, "work_item_id", "") or "").strip()
        if work_item_id:
            set_linked_work_item_id(task, work_item_id)
        return work_item_id


    @staticmethod
    def _fatal_runtime_projection_issues(
        task: Task,
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem] | None = None,
    ) -> list[WorkItemRuntimeInvariantIssue]:
        return [
            issue
            for issue in validate_work_item_runtime_projection(
                task,
                work_item,
                work_item_by_id=work_item_by_id,
            )
            if issue.severity == "error"
        ]


    @classmethod
    def _raise_for_runtime_projection_issues(
        cls,
        task: Task,
        work_item: DelegationWorkItem,
        work_item_by_id: dict[str, DelegationWorkItem] | None = None,
    ) -> None:
        issues = cls._fatal_runtime_projection_issues(task, work_item, work_item_by_id)
        if not issues:
            return
        raise RuntimeError(
            "work-item runtime invariant failed before dispatch: "
            + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
        )


    async def _diagnose_work_item_runtime_projection_issues(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> list[WorkItemRuntimeInvariantIssue]:
        issues = diagnose_work_item_runtime_projections(tasks, work_items)
        if not issues:
            return []
        work_item_by_id = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        for issue in issues:
            key = issue.fingerprint()
            if key in self._runtime_invariant_issue_keys:
                continue
            self._runtime_invariant_issue_keys.add(key)
            logger.warning(
                "work-item runtime invariant violation: "
                f"code={issue.code} severity={issue.severity} "
                f"run_id={issue.run_id} work_item={issue.work_item_id} "
                f"task={issue.runtime_task_id} projection={issue.projection_id} "
                f"message={issue.message}"
            )
            if not self.store or not hasattr(self.store, "save_delegation_event"):
                continue
            work_item = work_item_by_id.get(issue.work_item_id)
            try:
                await self.store.save_delegation_event(
                    DelegationEvent(
                        run_id=issue.run_id or str(getattr(work_item, "run_id", "") or "").strip(),
                        work_item_id=issue.work_item_id or None,
                        cell_id=str(getattr(work_item, "cell_id", "") or "").strip() or None,
                        role_id=str(getattr(work_item, "role_id", "") or "").strip() or None,
                        event_type=WORK_ITEM_RUNTIME_INVARIANT_EVENT_TYPE,
                        payload=issue.to_event_payload(),
                    )
                )
            except Exception:
                logger.opt(exception=True).debug("Best-effort runtime invariant event persistence failed")
        return issues


    async def _record_work_item_runtime_diagnostic(
        self,
        *,
        code: str,
        severity: str,
        work_item: DelegationWorkItem | None = None,
        task: Task | None = None,
        message: str,
        details: dict[str, Any] | None = None,
        warn: bool = True,
    ) -> None:
        issue = WorkItemRuntimeInvariantIssue(
            code=code,
            severity=severity,
            run_id=str(getattr(work_item, "run_id", "") or (getattr(task, "metadata", {}) or {}).get("delegation_run_id", "") or "").strip(),
            work_item_id=str(getattr(work_item, "work_item_id", "") or linked_work_item_id_for_task(task) or "").strip(),
            runtime_task_id=str(getattr(task, "id", "") or "").strip(),
            projection_id=(
                projection_id_for_work_item(work_item)
                if work_item is not None
                else projection_id_for_task(task)
            ),
            message=message,
            details=dict(details or {}),
        )
        key = issue.fingerprint()
        if key in self._runtime_invariant_issue_keys:
            return
        self._runtime_invariant_issue_keys.add(key)
        if warn:
            logger.warning(
                "work-item runtime diagnostic: "
                f"code={issue.code} severity={issue.severity} "
                f"run_id={issue.run_id} work_item={issue.work_item_id} "
                f"task={issue.runtime_task_id} projection={issue.projection_id} "
                f"message={issue.message}"
            )
        if not self.store or not hasattr(self.store, "save_delegation_event"):
            return
        try:
            await self.store.save_delegation_event(
                DelegationEvent(
                    run_id=issue.run_id,
                    work_item_id=issue.work_item_id or None,
                    cell_id=str(getattr(work_item, "cell_id", "") or "").strip() or None,
                    role_id=str(getattr(work_item, "role_id", "") or getattr(task, "assigned_to", "") or "").strip() or None,
                    event_type=WORK_ITEM_RUNTIME_INVARIANT_EVENT_TYPE,
                    payload=issue.to_event_payload(),
                )
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort runtime diagnostic event persistence failed")


    async def _materialize_work_item_tasks(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
    ) -> list[Task]:
        """Materialize company WorkItems into runtime Task envelopes.

        In company mode, DelegationWorkItem is the business unit. The Task
        records created here are execution projections for existing
        agent/tool/session APIs. The WorkItem -> Task relation is owned by
        the structured runtime link table and should not be treated as a
        second company-mode business identity.
        """
        if not self.store or not work_items:
            return tasks
        existing_tasks = list(tasks)
        if hasattr(self.store, "hydrate_task_work_item_links"):
            await self.store.hydrate_task_work_item_links(existing_tasks)
        existing_task_ids = {str(task.id or "").strip() for task in existing_tasks if str(task.id or "").strip()}
        existing_work_item_ids = set(task_by_linked_work_item_id(existing_tasks))
        root_task = sorted(existing_tasks, key=lambda item: (item.created_at, item.id))[0]
        runtime_topology = dict((root_task.metadata or {}).get("runtime_topology", {}) or {})
        root_parent_session_id = str(
            root_task.parent_session_id
            or root_task.session_id
            or (root_task.metadata or {}).get("parent_session_id", "")
            or "company-session"
        ).strip() or "company-session"
        target_output_dir = str((root_task.metadata or {}).get("target_output_dir", "") or "").strip()
        work_item_by_id = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        newly_materialized_tasks: list[Task] = []
        for work_item in work_items:
            work_item_id = str(getattr(work_item, "work_item_id", "") or "").strip()
            if not work_item_id or work_item_id in existing_work_item_ids:
                continue
            phase = work_item.phase
            metadata = dict(work_item.metadata or {})
            review_execution_work_item = is_review_execution_work_item_metadata(metadata)
            report_execution_work_item = is_report_execution_work_item_metadata(metadata)
            if (
                should_hide_work_item_from_company_kanban(metadata)
                and not review_execution_work_item
                and not report_execution_work_item
            ):
                continue
            if phase in DONE_PHASES:
                continue
            persisted = None
            get_runtime_task = getattr(self.store, "get_runtime_task_for_work_item", None)
            if callable(get_runtime_task):
                persisted = await get_runtime_task(work_item_id)
            if persisted is not None:
                set_linked_work_item_id(persisted, work_item_id)
                self._raise_for_runtime_projection_issues(persisted, work_item, work_item_by_id)
                if persisted.id not in existing_task_ids:
                    existing_tasks.append(persisted)
                    existing_task_ids.add(persisted.id)
                    existing_work_item_ids.add(work_item_id)
                continue
            seat_id = str(getattr(work_item, "seat_id", "") or dict(getattr(work_item, "metadata", {}) or {}).get("seat_id", "") or "").strip()
            topology_seat = next(
                (
                    dict(seat)
                    for seat in list(runtime_topology.get("seats", []) or [])
                    if str(seat.get("seat_id", "") or "").strip() == seat_id
                ),
                {},
            )
            work_kind = str(
                dict(getattr(work_item, "metadata", {}) or {}).get("work_kind", "")
                or getattr(work_item, "kind", "")
                or "execute"
            ).strip().lower() or "execute"
            projection_id = projection_id_for_work_item(work_item)
            session_id = f"{root_parent_session_id}:{work_item_id}"
            employee_assignment = dict(topology_seat.get("employee_assignment", {}) or {})
            preferred_external_agent = str(topology_seat.get("preferred_external_agent", "") or "").strip() or None
            selected_execution_agent, assigned_external_agent, role_force_native_execution = (
                resolve_effective_execution_agent(
                    topology_seat.get("selected_execution_agent"),
                    preferred_external_agent,
                    force_native_execution=bool(topology_seat.get("force_native_execution", False)),
                )
            )
            resolved_force_native_execution = bool((root_task.metadata or {}).get("force_native_execution", False) or role_force_native_execution)
            if resolved_force_native_execution:
                selected_execution_agent = "native"
                assigned_external_agent = None
            preferred_external_agent = assigned_external_agent
            turn_type = self._runtime_work_kind_to_work_item_turn_type(work_kind)
            current_turn_mode = self._initial_current_turn_mode_for_work_item(
                turn_type,
                topology_seat,
                review_execution_work_item=review_execution_work_item,
                report_execution_work_item=report_execution_work_item,
            )
            work_item.metadata = mark_work_item_projection(
                dict(work_item.metadata or {}),
                projection_id=projection_id,
                turn_type=turn_type,
            )
            if current_turn_mode and not str(work_item.metadata.get("current_turn_mode", "") or "").strip():
                work_item.metadata["current_turn_mode"] = current_turn_mode
            self._ensure_prompt_contract_on_work_item(
                work_item,
                task_metadata=dict(root_task.metadata or {}),
                task_description=str(getattr(work_item, "summary", "") or root_task.metadata.get("original_message", "") or "").strip(),
            )
            if employee_assignment:
                work_item.metadata["employee_assignment"] = copy.deepcopy(employee_assignment)
            prompt_ctx = str((employee_assignment or {}).get("prompt_context", "") or "").strip()
            if prompt_ctx:
                work_item.metadata["employee_prompt_context"] = prompt_ctx
            delta_ctx = str((employee_assignment or {}).get("delta_context", "") or "").strip()
            if delta_ctx:
                work_item.metadata["employee_delta_context"] = delta_ctx
            owner_execution_copy = build_work_item_owner_execution_copy(work_item)
            owner_execution_copy.setdefault(
                "delegation_role_session_id",
                canonical_role_session_id(
                    run_id=str(getattr(work_item, "run_id", "") or "").strip(),
                    role_id=str(getattr(work_item, "role_id", "") or "").strip(),
                    team_instance_id=str(getattr(work_item, "team_instance_id", "") or "").strip(),
                ),
            )
            owner_execution_copy["work_kind"] = turn_type
            task_metadata = mark_work_item_projection(mark_work_item_runtime({
                "mode": "company",
                "execution_mode": str((root_task.metadata or {}).get("execution_mode", "") or "company_mode").strip(),
                "execution_model": str((root_task.metadata or {}).get("execution_model", "") or "multi_team_org").strip(),
                "runtime_model": str((root_task.metadata or {}).get("runtime_model", "") or "multi_team_org").strip(),
                "original_message": str((root_task.metadata or {}).get("original_message", "") or "").strip(),
                "company_profile": str((root_task.metadata or {}).get("company_profile", "") or "").strip(),
                "delegation_playbook": dict((root_task.metadata or {}).get("delegation_playbook", {}) or {}),
                "runtime_topology": copy.deepcopy(runtime_topology),
                **owner_execution_copy,
                "seat_manager_role_id": str(topology_seat.get("manager_role_id", "") or getattr(work_item, "manager_role_id", "") or "").strip(),
                "manager_role_id": str(topology_seat.get("manager_role_id", "") or getattr(work_item, "manager_role_id", "") or "").strip(),
                "manager_seat_id": str(topology_seat.get("manager_seat_id", "") or getattr(work_item, "manager_seat_id", "") or "").strip(),
                "managed_team_id": str(topology_seat.get("managed_team_id", "") or "").strip(),
                "seat_contact_role_ids": list(topology_seat.get("contact_role_ids", []) or []),
                "allowed_delegate_role_ids": list(topology_seat.get("allowed_delegate_role_ids", []) or []),
                "force_native_execution": resolved_force_native_execution,
                "preferred_external_agent": preferred_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_agent_locked": bool(topology_seat.get("execution_agent_locked", False)),
                "selected_execution_agent_source": (
                    str(topology_seat.get("selected_execution_agent_source", "") or "").strip()
                    or (
                        "recruitment_user_override"
                        if bool(topology_seat.get("execution_agent_locked", False))
                        else ""
                    )
                ),
                "work_item_execution_strategy": (
                    WorkItemExecutionStrategy.NATIVE.value
                    if resolved_force_native_execution
                    else WorkItemExecutionStrategy.EXTERNAL.value
                    if assigned_external_agent
                    else WorkItemExecutionStrategy.AUTO.value
                ),
                "adaptive": copy.deepcopy(dict((getattr(work_item, "metadata", {}) or {}).get("adaptive", {}) or {})),
                "execution_task_ids": [work_item_id],
                "parent_session_id": root_parent_session_id,
                "work_item_batch_id": str(getattr(work_item, "batch_id", "") or "").strip(),
                "target_output_dir": target_output_dir,
                "output_root": target_output_dir,
                "workspace_root": str((root_task.metadata or {}).get("workspace_root", "") or "").strip(),
                "comms_workspace_root": str((root_task.metadata or {}).get("comms_workspace_root", "") or "").strip(),
                "comms_root": str((root_task.metadata or {}).get("comms_root", "") or "").strip(),
                "user_visible": bool(dict(getattr(work_item, "metadata", {}) or {}).get("user_visible", False)),
                "authoritative_output": bool(dict(getattr(work_item, "metadata", {}) or {}).get("authoritative_output", False)),
                "review_task": review_execution_work_item,
                "review_execution_work_item": review_execution_work_item,
                "report_execution_work_item": report_execution_work_item,
                "skip_work_item_sync": review_execution_work_item or report_execution_work_item,
            }, version=work_item_runtime_version(root_task.metadata)),
                projection_id=projection_id,
                turn_type=turn_type,
            )
            task_metadata.update(copy_work_item_execution_metadata(work_item))
            task_metadata.update(owner_execution_copy)
            task_metadata[WORK_ITEM_TURN_TYPE_KEY] = turn_type
            temp_task = Task(
                id=str(uuid.uuid4()),
                title=str(getattr(work_item, "title", "") or projection_id or "Runtime Work Item").strip(),
                description=str(getattr(work_item, "summary", "") or root_task.metadata.get("original_message", "") or "").strip(),
                assigned_to=str(getattr(work_item, "role_id", "") or "").strip(),
                status=task_status_for_phase(phase),
                project_id=root_task.project_id,
                session_id=session_id,
                parent_session_id=root_parent_session_id,
                assigned_external_agent=assigned_external_agent,
                metadata=task_metadata,
            )
            dependency_projection_ids: list[str] = []
            projection_spec = self._projection_spec_for_task(temp_task)
            if projection_spec is not None:
                dependency_projection_ids = [
                    str(item).strip()
                    for item in list(projection_spec.dependency_projection_ids or [])
                    if str(item).strip()
                ]
            if not dependency_projection_ids:
                dependency_projection_ids = [
                    str(projection_id_for_work_item(work_item_by_id.get(dep_id)) if work_item_by_id.get(dep_id) is not None else dep_id).strip()
                    for dep_id in [
                        str(item).strip()
                        for item in list(dict(getattr(work_item, "metadata", {}) or {}).get("dependency_work_item_ids", []) or [])
                        if str(item).strip()
                    ]
                    if str(projection_id_for_work_item(work_item_by_id.get(dep_id)) if work_item_by_id.get(dep_id) is not None else dep_id).strip()
                ]
            task = Task(
                id=temp_task.id,
                title=temp_task.title,
                description=temp_task.description,
                assigned_to=temp_task.assigned_to,
                status=temp_task.status,
                project_id=temp_task.project_id,
                session_id=temp_task.session_id,
                parent_session_id=temp_task.parent_session_id,
                assigned_external_agent=temp_task.assigned_external_agent,
                dependencies=dependency_projection_ids,
                metadata=task_metadata,
            )
            set_linked_work_item_id(task, work_item_id)
            await self.store.save_delegation_work_item(work_item)
            ensure_runtime_task = getattr(self.store, "ensure_runtime_task_for_work_item", None)
            if callable(ensure_runtime_task):
                task = await ensure_runtime_task(work_item, lambda task=task: task)
            else:
                await self.store.save_task(task)
                link_runtime_task = getattr(self.store, "link_work_item_runtime_task", None)
                if callable(link_runtime_task):
                    linked = await link_runtime_task(work_item_id, task.id)
                    if not linked:
                        raise RuntimeError(
                            "failed to link new runtime Task "
                            f"{task.id} for WorkItem {work_item_id}"
                        )
            set_linked_work_item_id(task, work_item_id)
            self._raise_for_runtime_projection_issues(task, work_item, work_item_by_id)
            if self.memory is not None and task.session_id:
                await self.memory.ensure_session(
                    task.session_id,
                    project_id=task.project_id,
                    title=task.title,
                    mode="child",
                    parent_session_id=task.parent_session_id,
                    metadata={
                        "task_id": task.id,
                        "work_item_id": work_item_id,
                        "role_id": str(getattr(work_item, "role_id", "") or "").strip(),
                        "seat_id": seat_id,
                        "origin_session_id": task.parent_session_id,
                    },
                )
            if task.id not in existing_task_ids:
                existing_tasks.append(task)
                existing_task_ids.add(task.id)
                newly_materialized_tasks.append(task)
            else:
                for existing_task in existing_tasks:
                    if str(getattr(existing_task, "id", "") or "").strip() == task.id:
                        set_linked_work_item_id(existing_task, work_item_id)
                        break
            existing_work_item_ids.add(work_item_id)
        if newly_materialized_tasks:
            task_by_projection_id: dict[str, Task] = {}
            for task in existing_tasks:
                task_by_projection_id[task.id] = task
                task_by_projection_id[self._projection_id_for_task(task)] = task
            for task in newly_materialized_tasks:
                if not list(task.dependencies or []):
                    continue
                await self._record_handoffs(task, task_by_projection_id)
                await self.save_task(task)
        return existing_tasks


    @staticmethod
    def _runtime_work_kind_to_work_item_turn_type(work_kind: str) -> str:
        return canonical_work_item_turn_type_for_kind(work_kind)


    @staticmethod
    def _initial_current_turn_mode_for_work_item(
        turn_type: str,
        topology_seat: dict[str, Any] | None,
        *,
        review_execution_work_item: bool = False,
        report_execution_work_item: bool = False,
    ) -> str:
        normalized_turn = canonical_work_item_turn_type_for_kind(turn_type)
        if normalized_turn == "deliver":
            return "deliver_required"
        if normalized_turn == "aggregate":
            return "synthesize_required"
        if report_execution_work_item or normalized_turn == "report":
            return "report_required"
        if review_execution_work_item or normalized_turn == "review":
            return "review_execute"
        seat = dict(topology_seat or {})
        direct_reports = list(seat.get("direct_report_seat_ids", []) or [])
        allowed_delegates = list(seat.get("allowed_delegate_role_ids", []) or [])
        managed_team_id = str(seat.get("managed_team_id", "") or "").strip()
        if direct_reports or allowed_delegates or managed_team_id:
            return "dispatch_required"
        return "worker_execute"


    @staticmethod
    def _driver_ownership_task(
        tasks: list[Task],
        *,
        preferred_task_ids: set[str] | None = None,
    ) -> Task | None:
        preferred = {
            str(task_id or "").strip()
            for task_id in set(preferred_task_ids or set())
            if str(task_id or "").strip()
        }
        candidates = [
            task
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
            and (not preferred or task.id in preferred)
        ]
        for task in candidates:
            if linked_work_item_id_for_task(task):
                return task
        for task in candidates:
            if is_work_item_runtime_metadata(dict(task.metadata or {})):
                return task
        return candidates[0] if candidates else None


    def acquire_driver_ownership(
        self,
        tasks: list[Task],
        *,
        preferred_task_ids: set[str] | None = None,
    ) -> CompanyExecutorDriverOwnership | None:
        registry = self.active_task_run_registry
        task = self._driver_ownership_task(
            tasks,
            preferred_task_ids=preferred_task_ids,
        )
        if registry is None or task is None:
            return None
        project_id = str(task.project_id or "default").strip() or "default"
        try:
            attempt_token = registry.register(project_id, task.id)
        except ActiveTaskRunAdmissionClosed as exc:
            raise asyncio.CancelledError(str(exc)) from exc
        return CompanyExecutorDriverOwnership(
            registry=registry,
            project_id=project_id,
            task_id=task.id,
            attempt_token=attempt_token,
        )


    async def execute(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> str:
        ownership = self.acquire_driver_ownership(tasks)
        try:
            plan = _coerce_company_work_item_runtime_plan(plan) or CompanyWorkItemRuntimePlan()
            plan.metadata = {
                **dict(plan.metadata or {}),
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
            }
            if ownership is None:
                return await self._execute_multi_team_org(plan, tasks)
            with ownership.bind():
                return await self._execute_multi_team_org(plan, tasks)
        finally:
            if ownership is not None:
                ownership.release()


    async def _execute_multi_team_org(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> str:
        run_token = self._use_run_state(
            CompanyExecutorRunState(active_plan=plan, active_tasks=list(tasks))
        )
        runtime_token = self.runtime.use_state(self.runtime.create_state())
        try:
            return await self._execute_multi_team_org_scoped(plan, tasks)
        finally:
            self.runtime.reset_state(runtime_token)
            self._reset_run_state(run_token)


    async def _execute_multi_team_org_scoped(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> str:
        self._active_plan = plan
        self._active_tasks = tasks
        await self.runtime.bootstrap(tasks)
        self._stall_counter = 0
        _wall_clock_start = time.monotonic()
        # Continuous-dispatch loop (kanban-push).  Previously this method
        # ran claim → asyncio.gather(ALL work items) → iterate, which meant
        # children created mid-turn by a leader could not be picked up
        # until every sibling in the current batch also finished — often
        # 30-60s of dead time.  We now keep work items running as
        # ``asyncio.Task`` instances in ``active_work_item_tasks`` and wake
        # the loop on whichever happens first: a work item completing, a
        # delegation tool firing ``_dispatcher_wake``, or a short polling
        # tick for DB-driven state (external agents, scheduled timers).
        active_work_item_tasks: dict[
            asyncio.Task[TaskResult | None],
            tuple[CompanyMemberSession, Task],
        ] = {}
        # Start with the wake event cleared so the first iteration always
        # performs a full load/claim pass.
        self._dispatcher_wake.clear()
        poll_timeout_sec = 0.5
        active_work_poll_timeout_sec = 5.0
        wall_clock_timeout = float(
            getattr(self, "multi_team_org_wall_clock_timeout", _MULTI_TEAM_ORG_WALL_CLOCK_TIMEOUT_SEC)
        )
        try:
            while True:
                # Global wall-clock timeout guard — prevent infinite spin.
                if time.monotonic() - _wall_clock_start > wall_clock_timeout:
                    logger.warning(
                        "_execute_multi_team_org: wall-clock timeout ({:.0f}s) exceeded; "
                        "returning degraded summary",
                        wall_clock_timeout,
                    )
                    await self._emit_progress(
                        "[Company] runtime turn timed out after "
                        f"{wall_clock_timeout:.0f}s; returning partial results."
                    )
                    return self._summarize_multi_team_org_results(tasks)
                if self.store:
                    project_id = str(tasks[0].project_id or "default").strip() if tasks else "default"
                    parent_session_id = str(
                        getattr(tasks[0], "parent_session_id", "") or (tasks[0].metadata or {}).get("parent_session_id", "") or ""
                    ).strip() if tasks else ""
                    all_tasks = await self.store.get_tasks(project_id=project_id)
                    tasks = [
                        task
                        for task in all_tasks
                        if str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
                        == str((self._active_tasks[0].metadata or {}).get("delegation_run_id", "") or "").strip()
                        and (
                            str(getattr(task, "parent_session_id", "") or "").strip() == parent_session_id
                            or str(getattr(task, "session_id", "") or "").strip() == parent_session_id
                        )
                    ] or list(self._active_tasks)
                self._active_tasks = tasks
                # Consumer half of `_park_for_blocking_comms`: blocking
                # replies arrive as durable inbox files, so each tick checks
                # parked tasks and releases the ones whose replies are all
                # present. In-flight tasks are skipped — their coroutine
                # still owns the Task object and a late save_task would
                # clobber the transition.
                in_flight_task_ids = {
                    claimed.id for _member, claimed in active_work_item_tasks.values()
                }
                for parked in tasks:
                    if (
                        parked.status == TaskStatus.AWAITING_PEER
                        and parked.id not in in_flight_task_ids
                    ):
                        await self._try_unpark_blocking_comms(parked)
                await self.runtime.refresh_inbox_state(tasks)
                work_items = await self._load_delegation_work_items(tasks)
                work_items = await self._refresh_ready_work_items(work_items, tasks=tasks)
                tasks = await self._materialize_work_item_tasks(tasks, work_items)
                self._active_tasks = tasks
                work_items = await self._load_delegation_work_items(tasks)
                tasks, work_items = await self._queue_multi_team_response_tasks(tasks, work_items)
                work_items = await self._refresh_ready_work_items(work_items, tasks=tasks)
                work_items = await self._reconcile_role_serial_queues(work_items)
                tasks = await self._materialize_work_item_tasks(tasks, work_items)
                sync_result = self._sync_task_projection_from_work_items(tasks, work_items)
                if inspect.isawaitable(sync_result):
                    await sync_result
                await self._diagnose_work_item_runtime_projection_issues(tasks, work_items)
                work_item_by_id = {item.work_item_id: item for item in work_items}
                task_by_work_item_id = await self._task_by_work_item_id(tasks)
                runnable_work_items = [
                    item for item in work_items
                    if self._work_item_is_runnable(item, work_item_by_id, task_by_work_item_id)
                ]
                self.runtime.enqueue_runnable_work_items(
                    runnable_work_items,
                    task_by_work_item_id=task_by_work_item_id,
                )
                plain_runnable_tasks = [
                    task
                    for task in tasks
                    if (
                        not linked_work_item_id_for_task(task)
                        and task.status == TaskStatus.PENDING
                    )
                ]
                self.runtime.enqueue_runnable_tasks(plain_runnable_tasks)
                runnable_work_item_ids = {item.work_item_id for item in runnable_work_items}
                runnable = [
                    task for task in tasks
                    if (
                        linked_work_item_id_for_task(task) in runnable_work_item_ids
                        or (
                            not linked_work_item_id_for_task(task)
                            and task.status == TaskStatus.PENDING
                        )
                    )
                ]
                active_tasks = [
                    task for task in tasks
                    if task.status in {TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.AWAITING_PEER, TaskStatus.AWAITING_MANAGER_REVIEW, TaskStatus.AWAITING_HUMAN, TaskStatus.AWAITING_REVIEW}
                ]
                # Phase B rehydrate: unpark any in-memory CompanyMemberSession
                # whose focused work_item has been made dispatchable by a
                # prior write (children-done wake, rework bounce, etc.).
                # Replaces sync_member_session_hook + reenqueue callbacks
                # with a single idempotent in-memory pass driven by DB truth.
                self._rehydrate_parked_member_sessions(work_items)
                # Claim whatever is immediately claimable and spawn each
                # work item as an independent asyncio.Task so the loop no
                # longer blocks on the slowest sibling.
                claims = await self._claim_and_create_work_item_tasks(
                    tasks,
                    work_items,
                    active_work_item_tasks,
                )
                # Termination: only when nothing is in-flight AND nothing
                # else is runnable.  If work items are still running, even an
                # "empty runnable" snapshot may become non-empty within
                # milliseconds (leader delegating children), so we defer
                # the break until the live set is drained.
                if not active_work_item_tasks:
                    if not active_tasks:
                        break
                    if not runnable and not claims:
                        human_waiting = [
                            t for t in active_tasks
                            if t.status in {TaskStatus.AWAITING_HUMAN, TaskStatus.AWAITING_MANAGER_REVIEW, TaskStatus.AWAITING_REVIEW}
                        ]
                        if human_waiting:
                            # Convergent exit: nothing is in flight, nothing is
                            # claimable, and every remaining active task waits on
                            # a human.  The wait is resolved through a separate
                            # engine turn (checkpoint reply → phase ready →
                            # re-dispatch), never inside this loop, so polling
                            # here can only spin forever while the caller's turn
                            # hangs and its claims block the resuming turn.
                            pending_task_ids = await self._pending_checkpoint_task_ids(
                                str(human_waiting[0].project_id or "default")
                            )
                            unparked = [t for t in human_waiting if t.id not in pending_task_ids]
                            self._stall_counter += 1
                            if not unparked or self._stall_counter >= _HUMAN_WAIT_MAX_STALL_TICKS:
                                if unparked:
                                    logger.warning(
                                        "_execute_multi_team_org: exiting after {} stalled ticks with {} "
                                        "human-waiting task(s) lacking a pending checkpoint: {}",
                                        self._stall_counter,
                                        len(unparked),
                                        [t.id for t in unparked],
                                    )
                                summary = self._summarize_human_parked_exit(tasks, human_waiting)
                                await self._emit_progress(
                                    "[Company] runtime turn parked: "
                                    f"{len(human_waiting)} work item(s) awaiting human input; "
                                    "answer the pending approval/review card(s) to continue."
                                )
                                return summary
                            await asyncio.sleep(5)
                            continue
                        for task in active_tasks:
                            if task.status == TaskStatus.AWAITING_PEER:
                                await self._save_peer_checkpoint(task)
                        break
                    self._stall_counter = 0
                else:
                    self._stall_counter = 0
                # Wait on: (a) any active work item completing, (b) a
                # dispatcher wake signaled by a delegation tool, or (c) a
                # short poll tick for external/DB-driven state changes.
                wake_waiter: asyncio.Task[bool] | None = None
                wait_futures: set[asyncio.Future[Any]] = set(active_work_item_tasks.keys())
                if not self._dispatcher_wake.is_set():
                    wake_waiter = asyncio.create_task(self._dispatcher_wake.wait())
                    wait_futures.add(wake_waiter)
                if wait_futures:
                    wait_timeout = (
                        active_work_poll_timeout_sec
                        if active_work_item_tasks
                        else poll_timeout_sec
                    )
                    try:
                        await asyncio.wait(
                            wait_futures,
                            return_when=asyncio.FIRST_COMPLETED,
                            timeout=wait_timeout,
                        )
                    except Exception:
                        logger.opt(exception=True).warning(
                            "_execute_multi_team_org: asyncio.wait raised; continuing"
                        )
                # Consume the wake signal (if any) — setting it again
                # during the next iteration is harmless and idempotent.
                self._dispatcher_wake.clear()
                if wake_waiter is not None and not wake_waiter.done():
                    wake_waiter.cancel()
                    try:
                        await wake_waiter
                    except (asyncio.CancelledError, Exception):
                        pass
                # Harvest any work items that finished during this tick.
                for completed in [t for t in list(active_work_item_tasks.keys()) if t.done()]:
                    session_task = active_work_item_tasks.pop(completed, None)
                    if session_task is None:
                        continue
                    claimed_member_session, claimed_task = session_task
                    exc = completed.exception()
                    if exc is not None:
                        await self._handle_claimed_work_item_exception(
                            claimed_member_session,
                            claimed_task,
                            exc,
                        )
                # Debounced UI push — fire-and-forget so the hot path
                # never awaits `build_collab_sync`.
                self._schedule_kanban_notification()
        except asyncio.CancelledError:
            claimed_pairs = list(active_work_item_tasks.values())
            for work_item_task in list(active_work_item_tasks.keys()):
                if not work_item_task.done():
                    work_item_task.cancel()
            if active_work_item_tasks:
                await asyncio.gather(
                    *list(active_work_item_tasks.keys()),
                    return_exceptions=True,
                )
                active_work_item_tasks.clear()
            store_ready = self._store_is_ready(self.store)
            update_role_session = getattr(self.store, "update_delegation_role_session", None)
            for member_session, claimed_task in claimed_pairs:
                try:
                    self.runtime._claimed_task_ids.discard(claimed_task.id)
                    work_item_id = linked_work_item_id_for_task(claimed_task)
                    if work_item_id:
                        self.runtime._claimed_work_item_ids.discard(work_item_id)
                    member_session.status = "idle"
                    member_session.resident_status = "idle"
                    member_session.current_task_id = ""
                    member_session.focused_work_item_id = ""
                    member_session.current_work_item = {}
                    member_session.current_assignment = {}
                    member_session.updated_at = datetime.now()
                    role_session = self.runtime._role_session_for_member_session(member_session)
                    if role_session is not None:
                        role_session.status = "idle"
                        role_session.focused_work_item_id = ""
                        role_session.current_work_item = {}
                        role_session.updated_at = datetime.now()
                    role_session_id = str(
                        getattr(role_session, "role_session_id", "")
                        or (claimed_task.metadata or {}).get("delegation_role_session_id", "")
                        or ""
                    ).strip()
                    if role_session_id and callable(update_role_session) and store_ready:
                        await update_role_session(
                            role_session_id,
                            focused_work_item_id="",
                            current_work_item={},
                            status="idle",
                            metadata_updates={
                                "last_suspend_memory_reset_at": datetime.now().isoformat(),
                                "last_suspend_task_id": claimed_task.id,
                            },
                        )
                except Exception:
                    logger.opt(exception=True).debug("company runtime cancellation: failed session idle reset")
            raise
        finally:
            # Drain any work items still running (shouldn't happen given the
            # termination invariants above, but keep the runtime honest
            # if an unexpected break-point is hit).
            if active_work_item_tasks:
                drain_results = await asyncio.gather(
                    *list(active_work_item_tasks.keys()),
                    return_exceptions=True,
                )
                for (completed_task, (drained_session, drained_task)), res in zip(
                    list(active_work_item_tasks.items()), drain_results
                ):
                    if isinstance(res, Exception):
                        await self._handle_claimed_work_item_exception(
                            drained_session,
                            drained_task,
                            res,
                        )
                active_work_item_tasks.clear()
        return self._summarize_multi_team_org_results(tasks)


    @staticmethod
    def _runtime_scope_for_tasks(tasks: list[Task]) -> tuple[str, str]:
        project_id = "default"
        runtime_session_id = ""
        for task in tasks:
            project_id = str(task.project_id or project_id).strip() or "default"
            metadata = dict(task.metadata or {})
            runtime_session_id = str(
                getattr(task, "parent_session_id", "")
                or metadata.get("company_runtime_root_session_id")
                or metadata.get("parent_session_id")
                or ""
            ).strip()
            if runtime_session_id:
                return project_id, runtime_session_id
        for task in tasks:
            runtime_session_id = str(getattr(task, "session_id", "") or "").strip()
            if runtime_session_id:
                return project_id, runtime_session_id
        return project_id, ""


    async def _claim_and_create_work_item_tasks(
        self,
        tasks: list[Task],
        work_items: list[DelegationWorkItem],
        active_work_item_tasks: dict[
            asyncio.Task[TaskResult | None],
            tuple[CompanyMemberSession, Task],
        ],
    ) -> list[tuple[CompanyMemberSession, Task]]:
        """Keep durable claim and coroutine ownership in one scope boundary."""

        async def claim_and_create() -> list[tuple[CompanyMemberSession, Task]]:
            claims = await self.runtime.claim_runnable_tasks(
                tasks,
                work_items=work_items,
            )
            for member_session, claimed_task in claims:
                work_item_task = self._create_claimed_work_item_task(
                    member_session,
                    claimed_task,
                    {},
                )
                active_work_item_tasks[work_item_task] = (
                    member_session,
                    claimed_task,
                )
            return claims

        registry = self.active_task_run_registry
        project_id, runtime_session_id = self._runtime_scope_for_tasks(tasks)
        if registry is None or not runtime_session_id:
            return await claim_and_create()
        async with registry.scope_lock(project_id, runtime_session_id):
            return await claim_and_create()


    def _create_claimed_work_item_task(
        self,
        member_session: CompanyMemberSession,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> asyncio.Task[TaskResult | None]:
        """Register ownership before scheduling the full claimed-item coroutine."""

        registry = self.active_task_run_registry
        project_id = str(task.project_id or "default").strip() or "default"
        attempt_token = ""
        if registry is not None:
            try:
                attempt_token = registry.register(project_id, task.id)
            except ActiveTaskRunAdmissionClosed as exc:
                raise asyncio.CancelledError(str(exc)) from exc

        async def run_owned() -> TaskResult | None:
            try:
                return await self._run_claimed_work_item(
                    member_session,
                    task,
                    task_by_projection_id,
                )
            finally:
                if registry is not None and attempt_token:
                    registry.unregister(project_id, task.id, attempt_token)

        try:
            return asyncio.create_task(run_owned())
        except BaseException:
            if registry is not None and attempt_token:
                registry.unregister(project_id, task.id, attempt_token)
            raise


    async def _run_claimed_work_item(
        self,
        member_session: CompanyMemberSession,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> TaskResult | None:
        result = await self._run_work_item(task, task_by_projection_id, member_session=member_session)
        # Process coordinator spawn requests after work-item completion
        spawn_requests = list((task.metadata or {}).get("coordinator_spawn_requests", []))
        if spawn_requests:
            import uuid

            for req in spawn_requests:
                spawn_projection_id = f"coord-spawn-{uuid.uuid4().hex[:8]}"
                spawn_task = Task(
                    id=str(uuid.uuid4()),
                    title=f"Coordinator-routed work for {req.get('target_role', 'unknown')}",
                    description=str(req.get("prompt", "")),
                    assigned_to=str(req.get("target_role", "")),
                    status=TaskStatus.PENDING,
                    metadata=mark_work_item_projection({
                        "coordinator_spawned": True,
                        "spawned_by": task.assigned_to,
                    }, projection_id=spawn_projection_id, turn_type="execute"),
                    project_id=task.project_id,
                )
                await self.save_task(spawn_task)
                await self._emit_progress(
                    f"[Company] coordinator spawned task for {req.get('target_role', '?')}: {spawn_task.title}",
                    task_id=spawn_task.id,
                )
        await self.runtime.complete_claim(member_session, task, result=result)
        # Phase A Step 7: tail-end reverse-projection sync removed. Each
        # intermediate path (DONE via _apply_done_transition, FAILED via
        # transition_work_item_from_task, BLOCKED / PENDING / etc.) is now
        # responsible for its own phase write + side effects. The review-
        # verdict branch below is still explicit since review work items
        # have a specialized finalizer.
        if bool((task.metadata or {}).get("review_execution_work_item", False)):
            # Review work-item completion: runtime reads the structured
            # verdict produced by the review agent and auto-applies it
            # to the child work item (approve → done, rework → todo).
            # Then closes the hidden review work item and refreshes
            # downstream dependents.
            await self._finalize_review_work_item(task)
        await self.save_task(task)
        # Notify the manager/coordinator role that this work item completed.
        if task.status == TaskStatus.DONE and not bool((task.metadata or {}).get("synthetic_inbox_turn", False)):
            await self._notify_manager_of_completion(task, result)
        return result


    def _claimed_work_item_needs_cleanup(
        self,
        member_session: CompanyMemberSession,
        task: Task,
    ) -> bool:
        work_item_id = linked_work_item_id_for_task(task)
        claimed_task_ids = set(getattr(self.runtime, "_claimed_task_ids", set()) or set())
        claimed_work_item_ids = set(getattr(self.runtime, "_claimed_work_item_ids", set()) or set())
        if task.id in claimed_task_ids:
            return True
        if work_item_id and work_item_id in claimed_work_item_ids:
            return True
        return (
            str(getattr(member_session, "current_task_id", "") or "").strip() == task.id
            and normalize_role_runtime_status(
                getattr(member_session, "status", ""),
                getattr(member_session, "focused_work_item_id", ""),
            ) == "running"
        )


    async def _handle_claimed_work_item_exception(
        self,
        member_session: CompanyMemberSession,
        task: Task,
        exc: Exception,
    ) -> None:
        projection_id = self._projection_id_for_task(task)
        work_item_id = linked_work_item_id_for_task(task)
        summary = (
            f"[Company:{projection_id}] claimed work item crashed but was isolated from the rest "
            f"of the session ({type(exc).__name__}: {exc})"
        )
        logger.opt(exception=exc).error(summary)
        exception_record = {
            "type": type(exc).__name__,
            "message": str(exc),
            **work_item_identity_payload(projection_id=projection_id, turn_type=""),
            "task_id": task.id,
            "work_item_id": work_item_id,
            "recorded_at": datetime.now().isoformat(),
        }
        task.metadata = dict(task.metadata or {})
        claim_active = self._claimed_work_item_needs_cleanup(member_session, task)
        if claim_active:
            task.metadata["claimed_work_item_exception"] = dict(exception_record)
            failure_result = TaskResult(
                status=TaskStatus.FAILED,
                content=summary,
                artifacts={"runtime_exception": dict(exception_record)},
            )
            task.result = {
                "content": failure_result.content,
                "artifacts": dict(failure_result.artifacts or {}),
            }
            # Phase A: phase write first → hook projects task.status=FAILED
            # onto the DB row and syncs our local task.status too.
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.FAILED,
                reason="claimed_work_item_exception",
                summary=summary or None,
            )
            try:
                await self.runtime.complete_claim(member_session, task, result=failure_result)
            except Exception as cleanup_exc:
                logger.opt(exception=cleanup_exc).error(
                    f"[Company:{projection_id}] failed to release crashed claimed work item"
                )
            # Phase A Step 7: the preceding transition_work_item_from_task
            # (Phase.FAILED, Step 3 migration) already drove the phase write.
            # Review-path finalizer stays explicit for review work items.
            if bool((task.metadata or {}).get("review_execution_work_item", False)):
                try:
                    await self._finalize_review_work_item(task)
                except Exception as sync_exc:
                    logger.opt(exception=sync_exc).error(
                        f"[Company:{projection_id}] failed to finalize crashed review work item"
                    )
        else:
            post_claim = list(task.metadata.get("post_claim_runtime_exceptions", []) or [])
            post_claim.append(dict(exception_record))
            task.metadata["post_claim_runtime_exceptions"] = post_claim[-4:]
        try:
            await self.save_task(task)
        except Exception as save_exc:
            logger.opt(exception=save_exc).error(
                f"[Company:{projection_id}] failed to persist isolated work item exception"
            )
        await self._notify_kanban_changed()
        try:
            await self._emit_progress(summary, task_id=task.id)
        except Exception as progress_exc:
            logger.opt(exception=progress_exc).error(
                f"[Company:{projection_id}] failed to emit isolated work item exception progress"
            )


    async def _notify_manager_of_completion(self, task: Task, result: TaskResult | None) -> None:
        """Send a structured completion notification to this task's manager role."""
        if str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org":
            return
        manager_role = str((task.metadata or {}).get("manager_role_id", "") or "").strip()
        if not manager_role or not self.communication:
            return
        from opc.core.models import AgentMessage, CommsSemanticType, MessageUrgency
        summary_parts = [f"Work item **{task.title}** completed by {task.assigned_to or 'unknown'}."]
        if result and hasattr(result, "output") and result.output:
            output_clip = clip_text(str(result.output), limit=500, marker="completion output preview truncated")
            output_preview = output_clip.text
            summary_parts.append(f"\n**Output preview:**\n{output_preview}")
        try:
            notification = AgentMessage(
                msg_type="inform",
                from_agent=task.assigned_to or task.metadata.get("work_item_role_id", "system"),
                to_agents=[manager_role],
                subject=f"[COMPLETED] {task.title}",
                body="\n".join(summary_parts),
                urgency=MessageUrgency.HIGH,
                semantic_type=CommsSemanticType.WORK_ITEM_RESULT,
                metadata={
                    "completion_task_id": task.id,
                    "auto_notification": True,
                    "output_preview_truncated": output_clip.truncated if result and getattr(result, "output", None) else False,
                    "output_preview_omitted_chars": output_clip.omitted_chars if result and getattr(result, "output", None) else 0,
                    "linked_work_item_id": linked_work_item_id_for_task(task),
                },
                task_id=task.id,
            )
            await self.communication.send_dm(notification, task=task)
        except Exception:
            pass  # Best-effort notification; don't fail the work item


    @staticmethod
    def _work_item_revision_value(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0


    async def _stale_work_item_revision_result_record(self, task: Task) -> dict[str, Any] | None:
        if not self.store:
            return None
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id or not hasattr(self.store, "get_delegation_work_item"):
            return None
        try:
            work_item = await self.store.get_delegation_work_item(work_item_id)
        except Exception:
            logger.opt(exception=True).debug("work-item revision stale guard: failed to load work item")
            return None
        if work_item is None:
            return None
        work_item_metadata = dict(getattr(work_item, "metadata", {}) or {})
        current_revision = self._work_item_revision_value(work_item_metadata.get("manager_mutation_revision"))
        if current_revision <= 0:
            return None
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        started_revision = self._work_item_revision_value(
            task_metadata.get("started_work_item_revision")
            or task_metadata.get("claimed_work_item_revision")
        )
        if current_revision <= started_revision:
            return None
        return {
            "work_item_id": work_item_id,
            "task_id": task.id,
            "manager_mutation_id": str(work_item_metadata.get("manager_mutation_id", "") or "").strip(),
            "manager_mutation_action": str(work_item_metadata.get("manager_mutation_action", "") or "").strip(),
            "manager_mutation_revision": current_revision,
            "started_work_item_revision": started_revision,
            "manager_mutation_reason": str(work_item_metadata.get("manager_mutation_reason", "") or "").strip(),
            "recorded_at": datetime.now().isoformat(),
        }


    async def _reject_stale_work_item_revision_result(
        self,
        task: Task,
        result: TaskResult,
    ) -> TaskResult | None:
        stale_record = await self._stale_work_item_revision_result_record(task)
        if stale_record is None:
            return None
        stale_record["content"] = str(result.content or "")
        stale_record["artifacts"] = dict(result.artifacts or {})
        task.metadata = dict(task.metadata or {})
        stale_history = list(task.metadata.get("stale_work_item_revision_results", []) or [])
        stale_history.append(dict(stale_record))
        task.metadata["stale_work_item_revision_results"] = stale_history[-5:]
        task.metadata["latest_stale_work_item_revision_result"] = dict(stale_record)
        task.result = {
            "content": result.content,
            "artifacts": dict(result.artifacts or {}),
            "stale_work_item_revision_result": dict(stale_record),
        }
        await self._append_progress(
            task,
            "Ignored stale work-item result because a manager changed this WorkItem before the turn completed.",
        )
        if self.save_task:
            await self.save_task(task)
        await self._emit_progress(
            f"[Company:{self._projection_id_for_task(task)}] stale result ignored after work-item mutation",
            task_id=task.id,
        )
        return TaskResult(
            status=TaskStatus.CANCELLED,
            content="Stale work-item result ignored because a manager changed this WorkItem before the turn completed.",
            artifacts={
                "stale_work_item_revision_result": dict(stale_record),
            },
        )


    @staticmethod
    def _is_self_evolution_work_item(task: Task) -> bool:
        metadata = dict(task.metadata or {})
        turn_kind = str(
            metadata.get("work_item_turn_type")
            or metadata.get("work_kind")
            or metadata.get("delegation_turn_kind")
            or ""
        ).strip().lower()
        return turn_kind == "self_evolution" or bool(metadata.get("self_evolution_work_item", False))


    @staticmethod
    def _parse_self_evolution_patch_json(raw: str | None) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            if text.lower().startswith("json\n"):
                text = text.split("\n", 1)[1].strip()
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None


    @staticmethod
    def _self_evolution_patch_validation_error(patches: list[Any], employee_id: str) -> str:
        if not patches:
            return ""
        if not employee_id:
            return "This self-evolution work item has no assigned employee; return `{ \"patches\": [] }`."
        for index, patch in enumerate(patches):
            if not isinstance(patch, dict):
                return f"Patch at index {index} must be a JSON object."
            patch_employee_id = str(patch.get("employee_id", "") or "").strip()
            if not patch_employee_id:
                return f"Patch at index {index} must include employee_id `{employee_id}`."
            if patch_employee_id != employee_id:
                return (
                    f"Patch at index {index} targets employee_id `{patch_employee_id}`, "
                    f"but this work item may only update `{employee_id}`."
                )
        return ""


    async def _retry_or_fail_self_evolution_output(
        self,
        task: Task,
        result: TaskResult,
        *,
        retry_count: int,
        max_retries: int,
        feedback: str,
    ) -> TaskResult:
        task.metadata = dict(task.metadata or {})
        task.context_snapshot = dict(task.context_snapshot or {})
        task.metadata["self_evolution_patch_retry_feedback"] = feedback
        task.context_snapshot["self_evolution_patch_retry_feedback"] = feedback
        task.metadata["self_evolution_patch_retry_count"] = retry_count + 1
        if retry_count + 1 < max_retries:
            await self.save_task(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] retrying self-evolution JSON "
                f"({retry_count + 1}/{max_retries})",
                task_id=task.id,
            )
            return TaskResult(status=TaskStatus.PENDING, content=feedback, artifacts=dict(result.artifacts or {}))
        error_record = {
            "error": "invalid_self_evolution_json",
            "message": feedback,
            "attempts": retry_count + 1,
        }
        task.metadata["self_evolution_error"] = error_record
        task.result = {"content": feedback, "artifacts": {"self_evolution_error": error_record}}
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id:
            await update_work_item_owned_metadata(self.store, work_item_id, {
                "self_evolution_error": error_record,
                "self_evolution_recorded": [],
            })
        await transition_work_item_from_task(
            self.store,
            task,
            target_status_or_phase=Phase.FAILED,
            reason="invalid_self_evolution_json",
            summary=feedback,
        )
        await self.save_task(task)
        return TaskResult(status=TaskStatus.FAILED, content=feedback, artifacts={"self_evolution_error": error_record})


    async def _finalize_self_evolution_work_item(
        self,
        task: Task,
        result: TaskResult,
    ) -> TaskResult | None:
        content = str(result.content or "").strip()
        data = self._parse_self_evolution_patch_json(content)
        patches = data.get("patches") if isinstance(data, dict) else None
        retry_count = int((task.metadata or {}).get("self_evolution_patch_retry_count", 0) or 0)
        max_retries = int((task.metadata or {}).get("self_evolution_patch_max_retries", 3) or 3)
        if data is None or not isinstance(patches, list):
            feedback = (
                "Self-evolution output must be strict JSON with a top-level `patches` list. "
                "Do not include prose, markdown, or delivery content."
            )
            return await self._retry_or_fail_self_evolution_output(
                task,
                result,
                retry_count=retry_count,
                max_retries=max_retries,
                feedback=feedback,
            )

        assignment = dict((task.metadata or {}).get("employee_assignment", {}) or {})
        employee_id = str(assignment.get("employee_id", "") or "").strip()
        patch_error = self._self_evolution_patch_validation_error(patches, employee_id)
        if patch_error:
            return await self._retry_or_fail_self_evolution_output(
                task,
                result,
                retry_count=retry_count,
                max_retries=max_retries,
                feedback=patch_error,
            )
        organization_id = str(
            getattr(task, "org_id", "")
            or (task.metadata or {}).get("organization_id", "")
            or (task.metadata or {}).get("org_id", "")
            or DEFAULT_ORGANIZATION_ID
        ).strip() or DEFAULT_ORGANIZATION_ID
        evolution = getattr(getattr(self, "memory", None), "employee_evolution", None)
        recorded: list[dict[str, Any]] = []
        if callable(getattr(evolution, "apply_employee_evolution_patch", None)):
            source = {
                "checkpoint_id": str((task.metadata or {}).get("self_evolution_checkpoint_id", "") or "").strip(),
                "checkpoint_type": "company_delivery_feedback",
                "human_action": str((task.metadata or {}).get("self_evolution_human_action", "") or "").strip(),
                "human_feedback": str((task.metadata or {}).get("self_evolution_human_feedback", "") or "").strip(),
                "project_id": str(task.project_id or "").strip(),
                "delivery_task_id": str((task.metadata or {}).get("self_evolution_delivery_task_id", "") or "").strip(),
                "delivery_projection_id": str((task.metadata or {}).get("self_evolution_delivery_projection_id", "") or "").strip(),
                "source_work_item_id": linked_work_item_id_for_task(task),
                "source_role_id": str(task.assigned_to or (task.metadata or {}).get("work_item_role_id", "") or "").strip(),
                "recorded_at": datetime.now().isoformat(),
            }
            recorded = evolution.apply_employee_evolution_patch(
                organization_id=organization_id,
                patch={"patches": patches},
                source=source,
                allowed_employee_ids={employee_id} if employee_id else set(),
            )

        task.metadata = dict(task.metadata or {})
        task.context_snapshot = dict(task.context_snapshot or {})
        task.metadata.pop("self_evolution_patch_retry_feedback", None)
        task.context_snapshot.pop("self_evolution_patch_retry_feedback", None)
        task.metadata["self_evolution_patch_retry_count"] = retry_count
        task.metadata["self_evolution_recorded"] = list(recorded)
        task.metadata["self_evolution_patch"] = {"patches": patches}
        task.metadata["self_evolution_completed_at"] = datetime.now().isoformat()
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id:
            await update_work_item_owned_metadata(self.store, work_item_id, {
                "self_evolution_recorded": list(recorded),
                "self_evolution_patch": {"patches": patches},
                "self_evolution_completed_at": task.metadata["self_evolution_completed_at"],
            })
        result.artifacts = {
            **dict(result.artifacts or {}),
            "self_evolution_recorded": list(recorded),
            "self_evolution_patch_count": len(patches),
        }
        return None


    async def _run_work_item(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
        *,
        member_session: CompanyMemberSession | None = None,
    ) -> TaskResult | None:
        multi_team_org = str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org"
        projection_id = self._projection_id_for_task(task)
        # Phase A Step 7: pre-work-item RUNNING marker removed. The pre-claim
        # transition_work_item_from_task(Phase.RUNNING) at the start of the
        # while-loop (Step 4 migration) and the per-iteration turn_start
        # transition inside the loop cover this responsibility correctly.
        await self._emit_progress(f"[Company:{projection_id}] starting {task.title}", task_id=task.id)
        if member_session:
            self.runtime.prepare_task_for_session(member_session, task)
            await self.runtime._sync_current_turn_mode_to_work_item(task, member_session.current_turn_mode)
        role = self.org_engine.get_role_for_work_item(task.assigned_to or task.metadata.get("work_item_role_id", ""), task.tags)
        task.assigned_to = role.role_id
        self._apply_role_defaults(task, role)
        if self.seat_executor is not None:
            await self.seat_executor.prepare_seat(
                task,
                member_session=member_session,
                role=role,
            )
        await self._prepare_setup_workspace(task)
        # Snapshot of which messages were unread when this turn started.
        # On successful completion we move only those to seen/, so any
        # mail that arrives mid-turn correctly stays as "new" for the
        # next turn (which may trigger a follow-up reactivation).
        self._snapshot_inbox_for_turn(task)
        self._inject_inbox_into_context(task, member_session)
        await self._inject_manager_board_into_context(task, member_session)
        self._inject_scratchpad_into_context(task)
        if not multi_team_org:
            await self._record_handoffs(task, task_by_projection_id)
            self._inject_parallel_peers_metadata(task, task_by_projection_id)
            self._inject_work_item_role_map(task)
            self._resolve_work_item_assignment_before_execution(task, task_by_projection_id)
            self._inherit_environment_manifest(task, task_by_projection_id)
            self._inherit_workspace_manifest(task, task_by_projection_id)
            self._inherit_data_acquisition_report(task, task_by_projection_id)
        lint_issues = [str(item).strip() for item in list(task.metadata.get("work_item_assignment_lint", []) or []) if str(item).strip()]
        if lint_issues and not multi_team_org:
            await self._append_progress(task, "Work-item assignment lint failed before execution.")
            await self._append_progress(task, "\n".join(f"- {issue}" for issue in lint_issues))
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.FAILED,
                reason="work_item_assignment_lint",
            )
            await self.save_task(task)
            await self._emit_progress(
                f"[Company:{projection_id}] assignment lint failed",
                task_id=task.id,
            )
            return TaskResult(status=TaskStatus.FAILED, content="\n".join(lint_issues))
        # Phase A: mark the work_item RUNNING via the phase channel. The
        # forward hook syncs task.status=RUNNING both on the DB row and
        # locally. Idempotent if the dispatcher already moved the phase.
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=Phase.RUNNING,
            reason="pre_execution_claim",
        )
        if self.agent_selector:
            # The selector also owns checkpoint attempt pins.  Forced-native
            # work must pass through it so a resumed native pin is validated
            # and consumed instead of leaking into a later dispatch.
            await self.agent_selector(task, role)
        elif task.metadata.get("force_native_execution"):
            task.assigned_external_agent = None
        else:
            if not task.assigned_external_agent:
                strategy = task.metadata.get("work_item_execution_strategy", "auto")
                orchestration_profile = str(task.metadata.get("work_item_orchestration_profile", "") or "").strip()
                if strategy == WorkItemExecutionStrategy.EXTERNAL.value:
                    task.assigned_external_agent = role.preferred_external_agent
                elif strategy == WorkItemExecutionStrategy.NATIVE.value:
                    task.assigned_external_agent = None
                elif orchestration_profile == "company_execute_native_first":
                    task.assigned_external_agent = None
                elif role.preferred_external_agent and strategy in {
                    WorkItemExecutionStrategy.AUTO.value,
                    WorkItemExecutionStrategy.MIXED.value,
                }:
                    task.assigned_external_agent = role.preferred_external_agent
        self._configure_external_timeouts(task)

        while True:
            task.metadata.pop("_retry_contract_enforcement", None)
            # The dispatch outcome is attempt-scoped.  Reset every transient
            # producer/escape marker together so a retry, rework, or follow-up
            # cannot inherit an earlier turn's board mutation or justification.
            task.metadata = reset_manager_dispatch_turn_metadata(task.metadata)
            manager_dispatch_retry_count = int(
                task.metadata.get("_manager_dispatch_retry_count", 0) or 0
            )
            dispatch_guard_before = await self._snapshot_manager_dispatch_state(
                task,
                member_session=member_session,
            )
            # Phase A: mark work_item RUNNING via phase channel. On retries
            # within this while-loop the phase may have regressed (e.g. to
            # READY_FOR_REWORK) so the explicit transition is still meaningful.
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.RUNNING,
                reason="turn_start",
            )
            await self.save_task(task)

            try:
                result = await asyncio.wait_for(
                    (
                        self.seat_executor.run_turn(task, member_session=member_session)
                        if self.seat_executor is not None
                        else self.execute_task(task)
                    ),
                    timeout=self.work_item_timeout,
                )
            except asyncio.CancelledError:
                # Suspension is a checkpoint transition owned by OPCEngine.
                # This task object may be stale by the time cancellation is
                # observed, so persisting it here can erase the canonical
                # checkpoint type, stop intent, or WorkItem hold.
                raise
            except asyncio.TimeoutError:
                logger.error(f"Company work item {projection_id} timed out after {self.work_item_timeout}s")
                await self._emit_progress(
                    f"[Company:{projection_id}] timed out after {self.work_item_timeout}s",
                    task_id=task.id,
                )
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.FAILED,
                    reason="work_item_timeout",
                )
                await self.save_task(task)
                return TaskResult(status=TaskStatus.FAILED, content=f"Work item timed out after {self.work_item_timeout}s.")
            stale_result = await self._reject_stale_work_item_revision_result(task, result)
            if stale_result is not None:
                return stale_result
            if result.status != TaskStatus.DONE:
                if result.status in _REVIEW_WAITING_STATUSES:
                    await transition_work_item_from_task(
                        self.store,
                        task,
                        target_status_or_phase=phase_for_task_status(result.status),
                        reason="work_item_awaiting_review",
                    )
                    review_level = "manager" if result.status == TaskStatus.AWAITING_MANAGER_REVIEW else "human"
                    await self._append_progress(task, f"Work item paused awaiting {review_level} review.")
                    await self._emit_progress(
                        f"[Company:{projection_id}] awaiting {review_level} review",
                        task_id=task.id,
                    )
                    await self.save_task(task)
                elif result.status == TaskStatus.AWAITING_PEER:
                    await transition_work_item_from_task(
                        self.store,
                        task,
                        target_status_or_phase=Phase.WAITING_FOR_PEER,
                        reason="work_item_awaiting_peer",
                    )
                    await self._append_progress(task, "Work item paused awaiting peer coordination.")
                    await self._save_peer_checkpoint(task)
                    await self._emit_progress(f"[Company:{projection_id}] awaiting peer", task_id=task.id)
                    await self.save_task(task)
                else:
                    # All execution agents (external + native) failed.
                    # Transition to NEEDS_ATTENTION (maps to TaskStatus.BLOCKED)
                    # so the work item does not stay stuck in RUNNING.
                    failure_reasons: list[str] = []
                    external_attempts = list(
                        (result.artifacts or {}).get("external_attempts", []) or []
                    )
                    for attempt in external_attempts:
                        agent_name = str(attempt.get("agent", "unknown"))
                        reason = str(attempt.get("failure_reason", "") or "").strip()
                        failure_reasons.append(
                            f"external agent `{agent_name}`: {reason or 'unknown error'}"
                        )
                    native_content = str(result.content or "").strip()
                    if native_content:
                        failure_reasons.append(f"native agent: {native_content}")
                    elif not external_attempts:
                        failure_reasons.append("native agent: execution failed with no output")

                    task.metadata = dict(task.metadata or {})
                    task.metadata["delegation_failure_reasons"] = failure_reasons
                    task.metadata["delegation_all_agents_failed"] = True
                    task.metadata["delegation_failure_at"] = datetime.now().isoformat()

                    await transition_work_item_from_task(
                        self.store, task,
                        target_status_or_phase=Phase.NEEDS_ATTENTION,
                        reason="all_execution_agents_failed",
                    )
                    await self._append_progress(
                        task,
                        "All execution agents failed. Work item blocked.\n"
                        + "\n".join(f"- {r}" for r in failure_reasons),
                    )
                    diagnostic_lines = [
                        f"[Company:{projection_id}] all execution agents failed — work item blocked.",
                        "",
                        "Failure details:",
                        *[f"  - {r}" for r in failure_reasons],
                        "",
                        "Suggested fixes:",
                        "  1. Check external agent connectivity and credentials.",
                        "  2. Verify the native LLM provider is reachable and has valid API keys.",
                        "  3. Review the task description for ambiguity that may confuse agents.",
                        "",
                        "To reset this stuck task and retry:",
                        f"  python scripts/reset_stuck_task.py --project <project> --session <session_id> --apply",
                    ]
                    await self._emit_progress(
                        "\n".join(diagnostic_lines),
                        task_id=task.id,
                    )
                    await self.save_task(task)
                return result

            # Comms park check: if the agent sent any blocking messages
            # this turn whose replies have not yet arrived in its own
            # inbox, park this work item in AWAITING_PEER. The receiver will
            # be reactivated automatically by the inbound-mail rule on
            # its own work item; once it writes a reply with `reply_to`
            # matching, the scheduler tick (or the next direct call to
            # `_try_unpark_blocking_comms`) will resume this task.
            if await self._park_for_blocking_comms(task):
                await self._emit_progress(
                    f"[Company:{projection_id}] parked awaiting blocking reply",
                    task_id=task.id,
                )
                return TaskResult(
                    status=TaskStatus.AWAITING_PEER,
                    content="Work item parked awaiting blocking comms reply.",
                    artifacts=dict(result.artifacts or {}),
                )

            created_follow_up_work_item_ids = await self._materialize_follow_up_work_items(task, result)
            dispatch_guard_issues = await self._enforce_manager_dispatch_guard(
                task,
                result,
                before_state=dispatch_guard_before,
                created_follow_up_work_item_ids=created_follow_up_work_item_ids,
                member_session=member_session,
            )
            if dispatch_guard_issues:
                await self._append_progress(task, "Manager dispatch guard rejected the turn.")
                await self._append_progress(task, "\n".join(f"- {issue}" for issue in dispatch_guard_issues))
                max_dispatch_retries = int(
                    task.metadata.get("manager_dispatch_guard_max_retries", 2) or 2
                )
                task.context_snapshot = dict(task.context_snapshot or {})
                # Build feedback that escalates on each retry: first attempt
                # restates the rule; later attempts add the counter so the
                # agent sees "this is strike N of M" and knows the next
                # non-delegating turn is terminal.
                violation_text = "\n".join(dispatch_guard_issues)
                if manager_dispatch_retry_count:
                    violation_text = (
                        f"(Retry {manager_dispatch_retry_count}/{max_dispatch_retries}) "
                        + violation_text
                    )
                task.context_snapshot["manager_dispatch_guard_violation"] = violation_text
                if manager_dispatch_retry_count < max_dispatch_retries:
                    task.metadata = dict(task.metadata or {})
                    task.metadata["_manager_dispatch_retry_count"] = (
                        manager_dispatch_retry_count + 1
                    )
                    await self.save_task(task)
                    await self._emit_progress(
                        f"[Company:{projection_id}] "
                        f"retrying manager dispatch turn "
                        f"({manager_dispatch_retry_count + 1}/{max_dispatch_retries})",
                        task_id=task.id,
                    )
                    continue
                # Retries exhausted. Dispatch is a soft constraint: the org
                # chart fixes who *can* delegate, but not every task needs
                # every seat, so accept the turn output as normal completion
                # instead of failing the work item. Record the unresolved
                # guard note so reviewers and the delivery report can weigh
                # the output accordingly.
                task.metadata = dict(task.metadata or {})
                task.metadata["manager_dispatch_guard_unresolved"] = violation_text
                await self._append_progress(
                    task,
                    "Dispatch guard reminders exhausted; accepting the turn output "
                    "without delegation (noted for review).",
                )
                await self._emit_progress(
                    f"[Company:{projection_id}] accepted manager turn without delegation "
                    f"after dispatch guard reminders were exhausted",
                    task_id=task.id,
                )
            task.metadata.pop("_manager_dispatch_retry_count", None)
            task.metadata.pop("manager_dispatch_guard_terminal_violation", None)
            task.context_snapshot = dict(task.context_snapshot or {})
            task.context_snapshot.pop("manager_dispatch_guard_violation", None)
            output_bundle = self._capture_work_item_outputs(task, result)
            await self._persist_work_item_owned_output_metadata(task, output_bundle)
            if await self._park_for_delegated_children(task):
                await self._emit_progress(
                    f"[Company:{projection_id}] parked awaiting delegated child work",
                    task_id=task.id,
                )
                return TaskResult(
                    status=TaskStatus.BLOCKED,
                    content="Work item delegated downstream work and is waiting for child work items to complete.",
                    artifacts=dict(result.artifacts or {}),
                )
            if await self._block_completion_for_unread_inbox(task):
                await self._emit_progress(
                    f"[Company:{projection_id}] inbox gate pending",
                    task_id=task.id,
                )
                return TaskResult(
                    status=TaskStatus.PENDING,
                    content="Work item paused by inbox completion gate; handle pending mailbox messages before finishing.",
                    artifacts=dict(result.artifacts or {}),
                )
            if self._is_self_evolution_work_item(task):
                self_evolution_result = await self._finalize_self_evolution_work_item(task, result)
                if self_evolution_result is not None:
                    if self_evolution_result.status == TaskStatus.PENDING:
                        continue
                    return self_evolution_result
            if multi_team_org:
                # Review verdicts are applied mechanically by
                # ``_finalize_review_work_item`` (runtime reads the
                # structured verdict emitted by the review agent and
                # updates the child work item directly).  No retry loop
                # is needed: one review turn produces one verdict.
                await self._append_progress(task, f"Team-runtime turn completed by role {task.assigned_to}.")
                await self._apply_done_transition(task, result=result)
                if self._is_authoritative_delivery_work_item(task) or self._requires_user_feedback(task):
                    await self._finalize_completed_work_item(task)
                    return result
                if not self._is_self_evolution_work_item(task):
                    self._append_to_scratchpad(task, result)
                self._archive_consumed_inbox_snapshot(task)
                if await self._reactivate_for_unread_mail(task):
                    await self._emit_progress(
                        f"[Company:{projection_id}] reactivated by inbound comms",
                        task_id=task.id,
                    )
                else:
                    await self._emit_progress(
                        f"[Company:{projection_id}] completed",
                        task_id=task.id,
                    )
                return result
            contract_issues = await self._enforce_work_item_contracts(task, result)
            if contract_issues:
                if bool(task.metadata.pop("_retry_contract_enforcement", False)):
                    continue
                return TaskResult(
                    status=task.status,
                    content="\n".join(contract_issues),
                    artifacts=dict(result.artifacts or {}),
                )
            gate = self._gate_from_metadata(task.metadata.get("work_item_gate"))
            if gate and self._work_item_gate_enforcement_enabled(task):
                await self._apply_gate(task, gate, task_by_projection_id)
            else:
                if gate:
                    await self._append_progress(task, f"Work-item gate `{gate.gate_type}` skipped by runtime policy.")
                await self._append_progress(task, f"Work item completed by role {task.assigned_to}.")
                await self._apply_done_transition(task, result=result)
                completion_action = await self._finalize_work_item_with_gate_harness(task, task_by_projection_id)
                if task.status == TaskStatus.DONE:
                    # Append completion summary to shared scratchpad
                    self._append_to_scratchpad(task, result)
                    # Archive whatever was unread at TURN START — those
                    # are the messages this turn had a chance to read.
                    # Anything that arrived during the turn stays as
                    # `new` and will trigger reactivation below.
                    self._archive_consumed_inbox_snapshot(task)
                    # Comms reactivation hook: if the agent has any unread
                    # mail when its turn ends, the work item is not actually
                    # finished — there is information addressed to this
                    # role that it has not consumed. Re-open the task as
                    # PENDING so the scheduler claims it for another turn.
                    # Convergence is enforced by the prompt rules ("only
                    # send when you need confirmation/changes; silence is
                    # ack"), not by a hard counter — we just record the
                    # reactivation depth for telemetry / anomaly detection.
                    if await self._reactivate_for_unread_mail(task):
                        await self._emit_progress(
                            f"[Company:{projection_id}] reactivated by inbound comms",
                            task_id=task.id,
                        )
                    else:
                        await self._emit_progress(
                            f"[Company:{projection_id}] completed",
                            task_id=task.id,
                        )
                elif task.status in _REVIEW_WAITING_STATUSES:
                    review_label = "manager review" if task.status == TaskStatus.AWAITING_MANAGER_REVIEW else "human review"
                    await self._emit_progress(
                        f"[Company:{projection_id}] awaiting {review_label}",
                        task_id=task.id,
                    )
            return result

    # Turn types whose completion is review-exempt only when THIS attempt
    # actually changed the delegated business board.  Historical children do
    # not describe what the current turn produced.
    _DELEGATION_OUTPUT_TURN_TYPES = frozenset({"dispatch", "intake", "plan"})


    def _has_agent_manager_above(self, task: Task, linked_work_item: Any) -> bool:
        """True when the card's manager is a real agent role that can run a
        review turn. Top seats report to the human ``owner`` and intentionally
        auto-approve their own zero-delegation output; subsequent refinement
        happens through the existing owner/final-decider conversation."""
        manager_role_id = (
            str((task.metadata or {}).get("manager_role_id", "") or "").strip()
            or str(getattr(linked_work_item, "manager_role_id", "") or "").strip()
        )
        if not manager_role_id or manager_role_id == "owner":
            return False
        get_agent = getattr(getattr(self, "org_engine", None), "get_agent", None)
        if callable(get_agent):
            try:
                return get_agent(manager_role_id) is not None
            except Exception:
                return True
        return True


    async def _apply_done_transition(
        self,
        task: Task,
        *,
        result: TaskResult | None = None,
    ) -> Phase | None:
        """Canonical 'worker work item completed' transition for company-mode.

        **Contract (Phase A Step 7, root-fixed post-new20)**: this helper
        handles **WORKER** tasks only. Review execution work_items
        (``metadata['review_execution_work_item'] = True``) are routed
        elsewhere — do NOT process them here, even partially. Calling this
        helper with a review card is a no-op by design; see below.

        Routing (worker tasks):
        - dispatch / intake / plan with a current-turn business-board mutation
          → Phase.APPROVED; without one → manager review when an agent
          manager exists, otherwise top-seat auto-approval
        - aggregate/synthesis → Phase.APPROVED
        - final user-visible delivery cards → Phase.AWAITING_HUMAN
          (user reviews final delivery only)
        - non-final delivery/attention cards → Phase.APPROVED
        - otherwise → Phase.AWAITING_MANAGER_REVIEW + spawn hidden manager
          review work_item in the manager seat's queue (kanban-push core)

        Side effects (only for worker tasks):
        - On AWAITING_MANAGER_REVIEW: spawn the worker report WorkItem; its
          terminal payload then spawns the manager review WorkItem.
        - On APPROVED: rely on refresh_dependents_hook (Step 1 fix) to
          cascade parent frontier refresh; also save delegation audit event.
        - Kanban UI notify via _notify_kanban_changed.

        **Why the review no-op**: the pre-Step-7 code enforced this split
        at ``_run_claimed_work_item`` tail via ``skip_work_item_sync`` + an
        ``elif review_execution_work_item: _finalize_review_work_item(...)``
        branch. Step 7 distributed the worker transition to 7 inline sites
        but the review path stayed at the tail. If this helper processed
        review cards (spawning a review-of-review manager review card, OR
        calling _finalize_review_work_item eagerly), it would conflict with
        the tail's finalize call — either infinite review-of-review
        recursion (reproduced as final decider stuck on
        ``review::review::review::...::v1`` with 15+ nesting in new20),
        or double verdict application.

        **The invariant**: exactly one finalization per review card, at
        the tail. This helper enforces that by early-returning for review
        cards. _finalize_review_work_item stays the SOLE review closer.

        Returns the resolved Phase for worker transitions, or ``None`` when
        (a) the task is a review card (no-op by contract), (b) there is no
        linked work_item (task-mode fallback).
        """
        if bool((task.metadata or {}).get("review_execution_work_item", False)):
            # CONTRACT: this helper is worker-only. Review cards are
            # finalized by _run_claimed_work_item's tail via
            # _finalize_review_work_item. Any side effect here (spawn or
            # finalize) would double-fire with the tail. See docstring.
            return None
        if bool((task.metadata or {}).get("report_execution_work_item", False)):
            # Two-turn worker→review flow: this is the hidden report card
            # finishing. Take the report turn's output as the parent's
            # canonical completion_report, refresh review_evidence, then
            # spawn the actual review card. Finally close the report card
            # itself as APPROVED (it served its purpose).
            return await self._apply_report_done_transition(task, result=result)
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id:
            # Task-mode leakage: fall back to local DONE sync via the helper's
            # built-in fallback behaviour.
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=TaskStatus.DONE,
                reason="apply_done_task_mode_fallback",
            )
            return None
        linked_work_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                linked_work_item = await self.store.get_delegation_work_item(work_item_id)
            except Exception:
                linked_work_item = None
        linked_work_item_metadata = dict(getattr(linked_work_item, "metadata", {}) or {})
        task.metadata = dict(task.metadata or {})
        for key in (
            "user_visible",
            "authoritative_output",
            "review_owner_kind",
            "requires_user_feedback",
            "feedback_scope",
        ):
            if key not in task.metadata and key in linked_work_item_metadata:
                task.metadata[key] = copy.deepcopy(linked_work_item_metadata[key])

        # Determine summary from result (preferred) or task.result (legacy).
        summary = ""
        if result is not None:
            summary = str(result.content or "").strip()
        elif task.result and isinstance(task.result, dict):
            summary = str(task.result.get("content", "") or "").strip()

        # Route DONE to one of {APPROVED, AWAITING_HUMAN, AWAITING_MANAGER_REVIEW}.
        raw_work_kind = self._turn_type_for_task(
            task,
            fallback=str(task.metadata.get("work_kind", "") or "execute"),
        )
        work_kind = canonical_work_item_turn_type_for_kind(raw_work_kind, fallback="")
        linked_attention_id = str((task.metadata or {}).get("attention_work_item_id", "") or "").strip()
        is_attention_work_item = (
            bool((task.metadata or {}).get("attention_work_item", False))
            or bool(linked_work_item_metadata.get("attention_work_item", False))
            or (bool(linked_attention_id) and linked_attention_id == work_item_id)
        )
        manager_reviewable = is_manager_reviewable_turn(work_kind) if work_kind else True
        is_delivery_card = (
            is_delivery_turn(task.metadata)
            or str(task.metadata.get("review_owner_kind", "") or "").strip().lower() == "human"
        )
        manager_turn_context: dict[str, str] = {}
        if (
            not manager_reviewable
            and not is_attention_work_item
            and not is_delivery_card
            and work_kind in self._DELEGATION_OUTPUT_TURN_TYPES
        ):
            board_mutated = bool(
                (task.metadata or {}).get("manager_board_mutation_performed", False)
            )
            justification = str(
                (task.metadata or {}).get("manager_no_delegation_justification", "") or ""
            ).strip()
            unresolved = str(
                (task.metadata or {}).get("manager_dispatch_guard_unresolved", "") or ""
            ).strip()
            manager_turn_context = {
                "outcome": "delegated" if board_mutated else "self_produced",
                "source": (
                    "board_mutation"
                    if board_mutated
                    else "justified"
                    if justification
                    else "dispatch_guard_exhausted"
                    if unresolved
                    else "no_board_mutation"
                ),
            }
            note = justification or unresolved
            if note:
                manager_turn_context["note"] = note
            if not board_mutated and self._has_agent_manager_above(
                task, linked_work_item,
            ):
                manager_reviewable = True
        if is_attention_work_item:
            # Attention work items are wake-up wrappers that let a parked
            # manager consume inbox/board state and call orchestration tools.
            # They are not business deliverables, so completing one must not
            # spawn a report/review chain for the wrapper itself.
            target_phase = Phase.APPROVED
        elif is_delivery_card:
            target_phase = (
                Phase.AWAITING_HUMAN
                if self._requires_user_feedback(task)
                else Phase.APPROVED
            )
        elif not manager_reviewable:
            # Dispatch cards deliver the child work-item set, while aggregate /
            # synthesize cards roll approved child results up to the parent.
            # These turn types are explicitly non-reviewable; routing them to
            # AWAITING_MANAGER_REVIEW leaves no review card able to consume them.
            target_phase = Phase.APPROVED
        else:
            target_phase = Phase.AWAITING_MANAGER_REVIEW

        # Persist the evidence and reviewer identity on the authoritative
        # WorkItem when it enters a passive review phase.
        metadata_updates: dict[str, Any] = {
            **work_item_identity_payload_for_task(task),
            "adaptive": dict(task.metadata.get("adaptive", {}) or {}),
        }
        if is_attention_work_item:
            metadata_updates["attention_work_item_outcome"] = "completed"
        if target_phase in {Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN}:
            review_owner_role_id = str(task.metadata.get("manager_role_id", "") or "").strip()
            review_owner_seat_id = str(task.metadata.get("manager_seat_id", "") or "").strip()
            if not review_owner_role_id or not review_owner_seat_id:
                if linked_work_item is not None:
                    if not review_owner_role_id:
                        review_owner_role_id = str(getattr(linked_work_item, "manager_role_id", "") or "").strip()
                    if not review_owner_seat_id:
                        review_owner_seat_id = str(getattr(linked_work_item, "manager_seat_id", "") or "").strip()
            if target_phase == Phase.AWAITING_MANAGER_REVIEW and not review_owner_role_id:
                logger.warning(
                    "_apply_done_transition auto-approved manager-reviewable work item "
                    "because no manager reviewer role was available; non-final work items "
                    f"must not enter human review. task_id={task.id} work_item_id={work_item_id}"
                )
                target_phase = Phase.APPROVED
            metadata_updates["review_owner_role_id"] = review_owner_role_id
            metadata_updates["review_owner_seat_id"] = review_owner_seat_id
            if summary:
                metadata_updates["completion_report"] = summary
            review_evidence = self._build_review_evidence(task, summary)
            if manager_turn_context:
                review_evidence = dict(review_evidence or {})
                review_evidence["manager_dispatch"] = dict(manager_turn_context)
            if review_evidence:
                metadata_updates["review_evidence"] = review_evidence

        # Phase write + local status sync via the canonical helper. Returns
        # False only if wid disappeared between our lookup and the call —
        # treat that as "someone else handled it" and skip side effects.
        wrote = await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=target_phase,
            reason="apply_done_transition",
            summary=summary or None,
            metadata_updates=metadata_updates,
        )
        if not wrote:
            return None

        # Re-read persisted phase to check for silent-degrade case (the
        # helper preserves the persisted phase if our target isn't in
        # ALLOWED_TRANSITIONS from the persisted state — e.g. a reviewer
        # already flipped the card while we were finishing). We only fire
        # spawn / refresh side effects if the transition actually landed
        # at our requested target.
        persisted_phase: Phase | None = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                refreshed_item = await self.store.get_delegation_work_item(work_item_id)
                if refreshed_item is not None:
                    persisted_phase = getattr(refreshed_item, "phase", None)
            except Exception:
                persisted_phase = None

        if persisted_phase == Phase.AWAITING_MANAGER_REVIEW:
            # Two-turn worker→review handoff: spawn a hidden report card
            # first (NOT the review card directly). The same worker session
            # resumes under a report-generation prompt to produce a clean
            # structured handoff. Only after the report card completes
            # (handled in _apply_report_done_transition) do we spawn the
            # actual review card. The completion_report we just stamped
            # onto the parent metadata is the worker's last execute-turn
            # prose — used as fallback if the report turn never produces
            # output; it will be overwritten by the report turn's content
            # when that turn finishes.
            await self._ensure_report_work_item_for_work_item(
                work_item_id,
                worker_task=task,
            )

        # Delegation audit event. Best-effort — never let persistence
        # failure propagate into the state machine.
        if hasattr(self.store, "save_delegation_event"):
            try:
                await self.store.save_delegation_event(
                    DelegationEvent(
                        run_id=str(task.metadata.get("delegation_run_id", "") or "").strip(),
                        work_item_id=work_item_id,
                        cell_id=str(task.metadata.get("delegation_cell_id", "") or "").strip() or None,
                        role_id=str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip() or None,
                        event_type="work_item_status_updated",
                        payload={
                            "task_id": task.id,
                            "task_status": task.status.value,
                            **work_item_identity_payload_for_task(task),
                            "summary": clip_text(summary, limit=500, marker="event summary truncated").text if summary else "",
                            **(
                                {"manager_dispatch": dict(manager_turn_context)}
                                if manager_turn_context
                                else {}
                            ),
                        },
                    )
                )
            except Exception:
                logger.debug("Best-effort delegation event persistence failed")

        # Push the transition to the kanban UI so the card moves columns
        # immediately. Uses _schedule_kanban_notification's debounce.
        await self._notify_kanban_changed()
        return persisted_phase


    async def _apply_report_done_transition(
        self,
        task: Task,
        *,
        result: TaskResult | None = None,
    ) -> Phase | None:
        """Close a hidden report card and spawn the actual review card.

        Two-turn worker→review handoff: the worker's execute turn DONE
        spawned a hidden report card; this is that report card finishing.
        The report turn's ``result.content`` is the canonical handoff
        text. The runtime first closes this report card with the full payload
        as one durable write, then projects that payload to the parent, then
        spawns review. Restart reconciliation can resume after either later
        write without asking the worker to report twice.

        The report card itself transitions to APPROVED — it served its
        purpose; nothing reviews it. The parent worker work_item stays in
        AWAITING_MANAGER_REVIEW (it was already there when the execute
        turn finished). What changes for the parent is the metadata
        payload that the upcoming review turn consumes.
        """
        meta = dict(task.metadata or {})
        report_card_id = linked_work_item_id_for_task(task)
        parent_work_item_id = str(meta.get("report_target_work_item_id", "") or "").strip()
        if not parent_work_item_id:
            # Defensive: report card with no parent pointer is corrupt;
            # close it and bail. Won't lose data — a future worker DONE
            # would re-spawn a new report card.
            if report_card_id and hasattr(self.store, "update_delegation_work_item"):
                try:
                    await self.store.update_delegation_work_item(
                        report_card_id,
                        phase=Phase.APPROVED,
                        claimed_by_role_runtime_session_id="",
                        claimed_by_seat_id="",
                        metadata_updates={
                            "claimed_by_role_session_id": "",
                            "claimed_task_id": "",
                            "report_card_outcome": "no_parent",
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("Best-effort close of orphan report card failed")
            return None

        # The report turn's prose IS the handoff. Try a structured parse
        # for downstream consumers, but pass the raw prose through
        # regardless so reviewers see what the worker actually wrote.
        report_raw = ""
        if result is not None:
            report_raw = str(result.content or "").strip()
        elif task.result and isinstance(task.result, dict):
            report_raw = str(task.result.get("content", "") or "").strip()
        parsed_report = self._parse_worker_report(report_raw)

        parent_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                parent_item = await self.store.get_delegation_work_item(parent_work_item_id)
            except Exception:
                parent_item = None
        if parent_item is None:
            # Parent disappeared — nothing we can do; close the report card.
            if report_card_id and hasattr(self.store, "update_delegation_work_item"):
                try:
                    await self.store.update_delegation_work_item(
                        report_card_id,
                        phase=Phase.APPROVED,
                        claimed_by_role_runtime_session_id="",
                        claimed_by_seat_id="",
                        metadata_updates={
                            "claimed_by_role_session_id": "",
                            "claimed_task_id": "",
                            "report_card_outcome": "parent_missing",
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("Best-effort close of orphan report card failed")
            return None

        parent_metadata = dict(getattr(parent_item, "metadata", {}) or {})
        if getattr(parent_item, "phase", None) != Phase.AWAITING_MANAGER_REVIEW:
            if report_card_id and hasattr(self.store, "update_delegation_work_item"):
                try:
                    await self.store.update_delegation_work_item(
                        report_card_id,
                        phase=Phase.APPROVED,
                        claimed_by_role_runtime_session_id="",
                        claimed_by_seat_id="",
                        metadata_updates={
                            "claimed_by_role_session_id": "",
                            "claimed_task_id": "",
                            "report_card_outcome": "parent_not_awaiting_review",
                            "report_parent_phase": str(
                                getattr(getattr(parent_item, "phase", None), "value", "") or ""
                            ),
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("Best-effort close of non-reviewable report card failed")
            await self._record_work_item_runtime_diagnostic(
                code="report_parent_not_awaiting_review",
                severity="info",
                work_item=parent_item,
                task=task,
                message="Report card target is no longer awaiting manager review; review card was not spawned.",
                details={
                    "parent_phase": str(
                        getattr(getattr(parent_item, "phase", None), "value", "") or ""
                    ),
                    "report_card_id": report_card_id,
                },
                warn=False,
            )
            return None
        completion_report = report_raw or str(parent_metadata.get("completion_report", "") or "")
        review_evidence = self._review_evidence_from_work_item(
            parent_item,
            completion_report,
        )
        if isinstance(parsed_report, dict) and parsed_report:
            review_evidence = dict(review_evidence or {})
            review_evidence.setdefault("worker_report", {})
            review_evidence["worker_report"].update(parsed_report)

        parent_metadata_updates = {
            "completion_report": completion_report,
            "review_evidence": review_evidence,
            "report_completion_raw": report_raw,
        }

        # Durability point: close the report and persist its complete payload
        # in one WorkItem write *before* projecting it to the parent or
        # creating review.  A crash after this write can be repaired using
        # the terminal report alone.
        persisted_report: DelegationWorkItem | None = None
        if report_card_id and hasattr(self.store, "update_delegation_work_item"):
            try:
                persisted_report = await self.store.update_delegation_work_item(
                    report_card_id,
                    phase=Phase.APPROVED,
                    claimed_by_role_runtime_session_id="",
                    claimed_by_seat_id="",
                    metadata_updates={
                        "claimed_by_role_session_id": "",
                        "claimed_task_id": "",
                        "report_card_outcome": "applied",
                        "completion_report": completion_report,
                        "review_evidence": review_evidence,
                        "report_completion_raw": report_raw,
                        "last_report_turn_finished_at": datetime.now().isoformat(),
                    },
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "report_done: failed to persist terminal report payload"
                )
        if persisted_report is None:
            # Do not create a review from volatile data. The report card stays
            # active. Release its persisted claim so the live dispatcher can
            # retry immediately; restart recovery is not required.
            await self._release_auxiliary_claim_for_retry(report_card_id)
            return None

        try:
            await self.store.update_delegation_work_item(
                parent_work_item_id,
                metadata_updates=parent_metadata_updates,
            )
        except Exception:
            logger.opt(exception=True).warning(
                "report_done: failed to update parent metadata with report payload"
            )
            await self._notify_kanban_changed()
            return Phase.APPROVED

        review_owner_role_id = str(
            parent_metadata.get("review_owner_role_id", "")
            or parent_item.manager_role_id
            or ""
        ).strip()
        review_owner_seat_id = str(
            parent_metadata.get("review_owner_seat_id", "")
            or parent_item.manager_seat_id
            or ""
        ).strip()
        if not review_owner_role_id or not review_owner_seat_id:
            await self._record_work_item_runtime_diagnostic(
                code="report_parent_missing_review_owner",
                severity="warning",
                work_item=parent_item,
                task=task,
                message="Reviewable report target has no review owner; review card was not spawned.",
                details={"parent_work_item_id": parent_work_item_id, "report_card_id": report_card_id},
            )
        else:
            await self._ensure_review_work_item_for_work_item(
                parent_work_item_id,
                completion_report=completion_report,
                metadata_updates={
                    "review_owner_role_id": review_owner_role_id,
                    "review_owner_seat_id": review_owner_seat_id,
                },
                source_report_item=persisted_report,
            )
        await self._notify_kanban_changed()
        return Phase.APPROVED


    async def _notify_kanban_changed(self) -> None:
        """Best-effort UI push.  Callers must NEVER let a UI-side failure
        propagate into the company-mode state machine.

        Per-transition call-sites (for example attention-card upserts) can
        fire many times in rapid
        succession when several work items flip status in the same tick.  We
        route them through ``_schedule_kanban_notification`` so the heavy
        ``build_collab_sync`` pass runs once per debounce window instead of
        once per transition — a strict responsiveness win, since none of
        these callers require the broadcast to have landed before they
        return (they were already wrapped in ``try/except: pass``).
        """
        if self.on_kanban_changed is None:
            return
        self._schedule_kanban_notification()


    def _signal_dispatcher_wake(self) -> None:
        """Synchronous wake-signal called by collaboration tools after
        persisting new TODO work items.  Safe to call from any coroutine;
        setting an already-set Event is a no-op.  The main loop in
        ``_execute_multi_team_org`` awaits this event so newly-delegated
        children are claimed+spawned immediately instead of waiting for
        the parent turn's gather batch to drain."""
        try:
            self._dispatcher_wake.set()
        except Exception:
            pass


    def _rehydrate_parked_member_sessions(self, work_items: list[Any]) -> None:
        """Per-tick dispatcher convergence: unpark in-memory member
        sessions whose focused work-item has been freed by a wake write.

        Phase B replaces the old per-transition
        ``_reconcile_member_session_after_phase`` / reenqueue hooks with
        this idempotent refresh. On every iteration of the dispatcher
        loop, we read the current work-items from the DB, find parked
        sessions whose focused card is now dispatchable (e.g. all
        children approved, or a reviewer returned a rework verdict),
        and flip their status back to ``idle`` so the next
        ``claim_runnable_tasks`` pass will pick them up.

        Pure in-memory + synchronous. No I/O.
        """
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return
        work_item_by_id: dict[str, Any] = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        now = datetime.now()
        for session in runtime.member_sessions.values():
            current = normalize_role_runtime_status(
                getattr(session, "status", ""),
                getattr(session, "focused_work_item_id", ""),
            )
            session.status = current
            session.resident_status = current
            if current != "blocked":
                continue
            focused_id = str(getattr(session, "focused_work_item_id", "") or "").strip()
            if not focused_id:
                # Nothing holding it parked; flip to idle.
                session.status = "idle"
                session.resident_status = "idle"
                session.updated_at = now
                continue
            work_item = work_item_by_id.get(focused_id)
            if work_item is None:
                # Focused card vanished (approved + archived).
                session.status = "idle"
                session.resident_status = "idle"
                session.focused_work_item_id = ""
                session.updated_at = now
                continue
            if is_dispatchable(work_item):
                session.status = "idle"
                session.resident_status = "idle"
                session.focused_work_item_id = ""
                session.updated_at = now
                # Also refresh the in-memory DelegationRoleSession mirror
                # so downstream reads (e.g. in claim_runnable_tasks) agree.
                role_session = runtime.role_sessions.get(
                    str(getattr(session, "role_session_id", "") or "").strip()
                )
                if role_session is not None and normalize_role_runtime_status(
                    getattr(role_session, "status", ""),
                    getattr(role_session, "focused_work_item_id", ""),
                ) == "blocked":
                    role_session.status = "idle"
                    role_session.focused_work_item_id = ""
                    role_session.updated_at = now


    def _schedule_kanban_notification(self) -> None:
        """Debounced, fire-and-forget UI push (Fix C).

        Marks the board dirty and ensures exactly one broadcaster
        coroutine is running; it coalesces rapid-fire changes into a
        single `build_collab_sync` + websocket broadcast after
        ``_kanban_debounce_sec`` of quiet.  Dispatch-loop iterations
        therefore never block on snapshot construction.
        """
        if self.on_kanban_changed is None:
            return
        self._kanban_dirty = True
        task = self._kanban_broadcast_task
        if task is not None and not task.done():
            return
        try:
            self._kanban_broadcast_task = asyncio.create_task(
                self._run_kanban_broadcaster()
            )
        except RuntimeError:
            # No running event loop (e.g. during teardown) — skip.
            self._kanban_dirty = False


    async def _run_kanban_broadcaster(self) -> None:
        try:
            while self._kanban_dirty:
                self._kanban_dirty = False
                try:
                    await asyncio.sleep(self._kanban_debounce_sec)
                except asyncio.CancelledError:
                    return
                # If more dirt accumulated during the debounce window,
                # clear the flag again before broadcasting so a change
                # arriving mid-snapshot still triggers a follow-up pass.
                self._kanban_dirty = False
                if self.on_kanban_changed is None:
                    return
                try:
                    await self.on_kanban_changed()
                except Exception:
                    # The hook logs its own traceback; swallow here.
                    pass
        finally:
            self._kanban_broadcast_task = None


    @staticmethod
    def _manager_dispatch_turn_mode(
        task: Task,
        member_session: CompanyMemberSession | None = None,
    ) -> str:
        return str(
            (task.metadata or {}).get("current_turn_mode", "")
            or (task.context_snapshot or {}).get("current_turn_mode", "")
            or getattr(member_session, "current_turn_mode", "")
            or ""
        ).strip().lower()

    # Turn kinds where a manager turn completes *without* dispatching any
    # child work by design: delivery/synthesize/aggregate roll sub-team
    # results up to the parent, and review evaluates a peer's output.
    # Firing the dispatch guard on these marks a legitimate terminal turn
    # as failed (new16/app13 reproduced this: final delivery produced
    # substantive output, guard rejected "no delegate_work call" → task
    # status FAILED despite disk artifacts being complete).
    _NON_DISPATCH_TURN_KINDS: frozenset[str] = frozenset({
        "deliver", "delivery",
        "synthesize", "synthesis",
        "aggregate",
        "review",
        "monitor",
        "self_evolution",
    })


    @classmethod
    def _task_turn_kind(cls, task: Task) -> str:
        """Best-effort turn-kind inference for guard-filtering. Checks the
        three metadata fields that callers stamp in different code paths —
        ``work_kind`` is the modern work-item runtime field, the other two are
        legacy signals from the work-item planner and gate policy."""
        meta = task.metadata or {}
        for key in ("work_kind", "delegation_turn_kind", "work_item_turn_type"):
            value = str(meta.get(key, "") or "").strip().lower()
            if value:
                return value
        return ""


    @classmethod
    def _requires_manager_dispatch_guard(
        cls,
        task: Task,
        member_session: CompanyMemberSession | None = None,
    ) -> bool:
        if str((task.metadata or {}).get("runtime_model", "") or "").strip() != "multi_team_org":
            return False
        # Fix 3 (follow-up): skip the guard on work items where "no delegate_work
        # call" is the expected shape, regardless of what current_turn_mode
        # resolved to. See ``_NON_DISPATCH_TURN_KINDS``. Without this, the
        # Final-delivery work item in new16/app13 got marked failed even
        # though the artifacts were written and the subteam work approved.
        turn_kind = cls._task_turn_kind(task)
        if turn_kind in cls._NON_DISPATCH_TURN_KINDS:
            return False
        if cls._manager_dispatch_turn_mode(task, member_session=member_session) != "dispatch_required":
            return False
        direct_report_seat_ids = [
            str(item).strip()
            for item in list(
                (task.metadata or {}).get("direct_report_seat_ids", [])
                or dict(getattr(member_session, "metadata", {}) or {}).get("direct_report_seat_ids", [])
                or []
            )
            if str(item).strip()
        ]
        allowed_delegate_role_ids = [
            str(item).strip()
            for item in list(
                (task.metadata or {}).get("allowed_delegate_role_ids", [])
                or dict(getattr(member_session, "metadata", {}) or {}).get("allowed_delegate_role_ids", [])
                or []
            )
            if str(item).strip()
        ]
        managed_team_id = str(
            (task.metadata or {}).get("managed_team_id", "")
            or dict(getattr(member_session, "metadata", {}) or {}).get("managed_team_id", "")
            or ""
        ).strip()
        return bool(direct_report_seat_ids or allowed_delegate_role_ids or managed_team_id)


    async def _snapshot_manager_dispatch_state(
        self,
        task: Task,
        *,
        member_session: CompanyMemberSession | None = None,
    ) -> dict[str, Any] | None:
        if not self._requires_manager_dispatch_guard(task, member_session=member_session):
            return None
        if not self.store or not hasattr(self.store, "list_delegation_work_items"):
            return None
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        parent_work_item_id = linked_work_item_id_for_task(task)
        if not run_id or not parent_work_item_id:
            return None
        work_items = await self.store.list_delegation_work_items(run_id)
        child_mutation_state: dict[str, dict[str, Any]] = {}
        child_work_item_ids = {
            str(getattr(item, "work_item_id", "") or "").strip()
            for item in work_items
            if str(getattr(item, "parent_work_item_id", "") or "").strip() == parent_work_item_id
            and not is_runtime_auxiliary_work_item(item)
            and str(getattr(item, "work_item_id", "") or "").strip()
        }
        for item in work_items:
            item_id = str(getattr(item, "work_item_id", "") or "").strip()
            if not item_id or item_id not in child_work_item_ids:
                continue
            metadata = dict(getattr(item, "metadata", {}) or {})
            try:
                mutation_revision = int(metadata.get("manager_mutation_revision", 0) or 0)
            except (TypeError, ValueError):
                mutation_revision = 0
            child_mutation_state[item_id] = {
                "manager_mutation_revision": mutation_revision,
                "manager_mutation_action": str(metadata.get("manager_mutation_action", "") or "").strip(),
                "deleted_by_manager_tool": bool(metadata.get("deleted_by_manager_tool", False)),
                "hidden_from_company_kanban": bool(metadata.get("hidden_from_company_kanban", False)),
                "upstream_visibility": str(metadata.get("upstream_visibility", "") or "").strip().lower(),
            }
        dependency_work_item_ids = {
            str(item).strip()
            for item in list((task.metadata or {}).get("delegation_wait_for_work_item_ids", []) or [])
            if str(item).strip()
        }
        parent = await self.store.get_delegation_work_item(parent_work_item_id) if hasattr(self.store, "get_delegation_work_item") else None
        if parent is not None:
            dependency_work_item_ids.update(
                str(item).strip()
                for item in list((getattr(parent, "metadata", {}) or {}).get("dependency_work_item_ids", []) or [])
                if str(item).strip()
            )
        work_item_by_id = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        normalized_dependency_ids, _pruned_dependency_ids = normalize_dependency_work_item_ids(
            list(dependency_work_item_ids),
            work_item_by_id,
            owner_work_item_id=parent_work_item_id,
        )
        return {
            "run_id": run_id,
            "parent_work_item_id": parent_work_item_id,
            "child_work_item_ids": child_work_item_ids,
            "dependency_work_item_ids": set(normalized_dependency_ids),
            "child_mutation_state": child_mutation_state,
        }


    @staticmethod
    def _genuine_no_delegation_justification(text: str) -> str:
        """Cleaned justification text, or "" for empty input or an echo of
        the instruction template's `<specific reason>` placeholder — with
        any markdown decoration, quoting, or trailing punctuation around
        the placeholder stripped before the check."""
        reason = re.sub(r"[\s*_`~\"']+$", "", str(text or "")).strip()
        if not reason:
            return ""
        core = reason
        previous = None
        while previous != core:
            previous = core
            core = core.strip().strip("*_~`\"'")
            core = re.sub(r"[\s.。!！,，;；:：]+$", "", core)
        if re.fullmatch(r"<[^<>]*>", core):
            return ""
        return reason


    @staticmethod
    def _extract_no_delegation_justification(task: Task, result: TaskResult | None) -> str:
        artifact_candidates = []
        if result and getattr(result, "artifacts", None):
            artifacts = dict(result.artifacts or {})
            artifact_candidates.extend(
                [
                    str(artifacts.get("manager_no_delegation_justification", "") or "").strip(),
                    str(artifacts.get("no_delegation_justification", "") or "").strip(),
                    str(artifacts.get("manager_no_delegation_reason", "") or "").strip(),
                    str(artifacts.get("no_delegation_reason", "") or "").strip(),
                ]
            )
        artifact_candidates.extend(
            [
                str((task.metadata or {}).get("manager_no_delegation_justification", "") or "").strip(),
                str((task.metadata or {}).get("no_delegation_justification", "") or "").strip(),
            ]
        )
        for candidate in artifact_candidates:
            cleaned = CompanyExecutorDispatchMixin._genuine_no_delegation_justification(candidate)
            if cleaned:
                return cleaned
        content = str(getattr(result, "content", "") or "").strip()
        for line in content.splitlines():
            match = _NO_DELEGATION_JUSTIFICATION_LINE.match(str(line))
            if not match:
                continue
            reason = CompanyExecutorDispatchMixin._genuine_no_delegation_justification(
                match.group("reason")
            )
            if reason:
                return reason
        return ""


    @staticmethod
    def _no_delegation_justification_is_infra_failure(
        justification: str,
        result: TaskResult | None,
    ) -> bool:
        artifacts = dict(getattr(result, "artifacts", {}) or {}) if result is not None else {}
        failure = artifacts.get("collaboration_infrastructure_failure")
        if isinstance(failure, dict) and str(failure.get("error_type", "") or "").strip() == "infrastructure":
            return True
        text = str(justification or "").strip().lower()
        if not text:
            return False
        markers = (
            "disk i/o error",
            "database is locked",
            "readonly database",
            "unable to open database file",
            "collaboration broker rpc",
            "broker rpc failed",
            "sqlite3.operationalerror",
            "sqlite operationalerror",
        )
        return any(marker in text for marker in markers)


    async def _enforce_manager_dispatch_guard(
        self,
        task: Task,
        result: TaskResult | None,
        *,
        before_state: dict[str, Any] | None,
        created_follow_up_work_item_ids: list[str] | None = None,
        member_session: CompanyMemberSession | None = None,
    ) -> list[str]:
        if before_state is None:
            return []
        after_state = await self._snapshot_manager_dispatch_state(task, member_session=member_session)
        if after_state is None:
            return []
        before_child_ids = set(before_state.get("child_work_item_ids", set()) or set())
        after_child_ids = set(after_state.get("child_work_item_ids", set()) or set())
        before_dependency_ids = set(before_state.get("dependency_work_item_ids", set()) or set())
        after_dependency_ids = set(after_state.get("dependency_work_item_ids", set()) or set())
        before_child_mutation_state = dict(before_state.get("child_mutation_state", {}) or {})
        after_child_mutation_state = dict(after_state.get("child_mutation_state", {}) or {})

        def _is_manager_mutation_marker(state: dict[str, Any]) -> bool:
            try:
                mutation_revision = int(state.get("manager_mutation_revision", 0) or 0)
            except (TypeError, ValueError):
                mutation_revision = 0
            return (
                mutation_revision > 0
                or bool(state.get("deleted_by_manager_tool", False))
                or bool(state.get("hidden_from_company_kanban", False))
                or str(state.get("upstream_visibility", "") or "") == "hidden"
            )

        manager_mutated_existing_child_ids = {
            item_id
            for item_id in before_child_ids & after_child_ids
            if (after_marker := dict(after_child_mutation_state.get(item_id, {}) or {}))
            != dict(before_child_mutation_state.get(item_id, {}) or {})
            and _is_manager_mutation_marker(after_marker)
        }
        created_follow_up_ids = {
            str(item).strip()
            for item in list(created_follow_up_work_item_ids or [])
            if str(item).strip()
        }
        if (
            after_child_ids - before_child_ids
            or before_child_ids - after_child_ids
            or after_dependency_ids - before_dependency_ids
            or manager_mutated_existing_child_ids
            or created_follow_up_ids
            or bool((task.metadata or {}).get("manager_board_mutation_performed", False))
        ):
            task.metadata = dict(task.metadata or {})
            task.metadata["manager_board_mutation_performed"] = True
            task.metadata.pop("manager_no_delegation_justification", None)
            task.metadata.pop("manager_dispatch_guard_unresolved", None)
            return []
        justification = self._extract_no_delegation_justification(task, result)
        if justification:
            if self._no_delegation_justification_is_infra_failure(justification, result):
                return [
                    "Dispatch-required manager turn hit a collaboration infrastructure failure "
                    "while trying to inspect or mutate the work-item board. Retry the collaboration "
                    "tool path instead of accepting `NO_DELEGATION_JUSTIFICATION` as normal completion."
                ]
            task.metadata = dict(task.metadata or {})
            task.metadata["manager_no_delegation_justification"] = justification
            task.metadata.pop("manager_dispatch_guard_unresolved", None)
            return []
        direct_reports = [
            str(item).strip()
            for item in list((task.metadata or {}).get("direct_report_role_ids", []) or [])
            if str(item).strip()
        ]
        direct_report_hint = f" Direct reports in scope: {', '.join(direct_reports[:6])}." if direct_reports else ""
        return [
            "Dispatch-required manager turn finished without creating child work. "
            "Use `delegate_work(...)` for new child work, or `modify_work_item(...)` / `delete_work_item(...)` "
            "when revising an existing board, "
            "or finish with `NO_DELEGATION_JUSTIFICATION: <specific reason>` when no downstream seat is a fit."
            + direct_report_hint
        ]


    async def _park_for_delegated_children(self, task: Task) -> bool:
        if not self.store or not hasattr(self.store, "get_delegation_work_item"):
            return False
        parent_work_item_id = linked_work_item_id_for_task(task)
        if not parent_work_item_id:
            return False
        dependency_ids = [
            str(item).strip()
            for item in list(task.metadata.get("delegation_wait_for_work_item_ids", []) or [])
            if str(item).strip()
        ]
        parent_work_item = await self.store.get_delegation_work_item(parent_work_item_id)
        if parent_work_item is not None:
            dependency_ids = list(
                dict.fromkeys(
                    [
                        *dependency_ids,
                        *[
                            str(item).strip()
                            for item in list((parent_work_item.metadata or {}).get("dependency_work_item_ids", []) or [])
                            if str(item).strip()
                        ],
                    ]
                )
            )
        if dependency_ids and parent_work_item is not None and hasattr(self.store, "list_delegation_work_items"):
            try:
                run_items_for_deps = await self.store.list_delegation_work_items(parent_work_item.run_id)
            except Exception:
                run_items_for_deps = []
            work_item_by_id = {
                str(getattr(item, "work_item_id", "") or "").strip(): item
                for item in run_items_for_deps
                if str(getattr(item, "work_item_id", "") or "").strip()
            }
            dependency_ids, pruned_dependency_ids = normalize_dependency_work_item_ids(
                dependency_ids,
                work_item_by_id,
                owner_work_item_id=parent_work_item_id,
            )
            if pruned_dependency_ids and hasattr(self.store, "update_delegation_work_item"):
                try:
                    await self.store.update_delegation_work_item(
                        parent_work_item_id,
                        metadata_updates={
                            "dependency_work_item_ids": dependency_ids,
                            "pruned_dependency_work_item_ids": pruned_dependency_ids,
                            "dependency_pruned_at": datetime.now().isoformat(),
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to persist pruned delegated-child dependencies for %s",
                        parent_work_item_id,
                    )
        # Belt-and-suspenders: if neither task.metadata nor parent.metadata
        # carry the dependency ids but the parent actually has children
        # filed against it (parent_work_item_id pointer), derive the deps
        # from the live work-item dependency topology. Without this, any future code
        # path that creates children without stamping the dependency ids
        # would silently bypass parking and send the manager up for review
        # while children are still running.
        if not dependency_ids and parent_work_item is not None and hasattr(self.store, "list_delegation_work_items"):
            try:
                run_items = await self.store.list_delegation_work_items(parent_work_item.run_id)
            except Exception:
                run_items = []
            dependency_ids = [
                str(getattr(child, "work_item_id", "") or "").strip()
                for child in run_items
                if str(getattr(child, "parent_work_item_id", "") or "").strip() == parent_work_item_id
                and not is_runtime_auxiliary_work_item(child)
                and not bool((getattr(child, "metadata", {}) or {}).get("deleted_by_manager_tool", False))
                and str(getattr(child, "work_item_id", "") or "").strip()
            ]
        if not dependency_ids:
            return False
        # Intake special-case: the top-level "receive user request + dispatch"
        # card's deliverable IS the delegation; there is nothing further
        # for it to integrate once children return. Instead of parking it
        # in WAITING_FOR_CHILDREN (which forces a wake and an empty "turn 2"
        # where the agent has nothing to produce), we:
        #   1. Approve the intake directly — its job is done.
        #   2. Materialize a separate delivery card, dependent on the
        #      same children, whose job is to synthesise and hand the
        #      final result to the user.
        # The delivery card is reviewed by the human user (not an upper
        # agent), so its review phase resolves to AWAITING_HUMAN.
        if (
            parent_work_item is not None
            and str(getattr(parent_work_item, "kind", "") or "").strip().lower() == "intake"
            and not bool((parent_work_item.metadata or {}).get("intake_delivery_spawned", False))
        ):
            try:
                await self._spawn_delivery_card_after_intake(
                    task=task,
                    intake_work_item=parent_work_item,
                    dependency_ids=dependency_ids,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "failed to spawn delivery card for intake %s — falling back to normal parking",
                    parent_work_item_id,
                )
            else:
                task.metadata = dict(task.metadata)
                task.metadata.pop("delegation_pending_work_item_ids", None)
                task.metadata.pop("delegated_children_pending", None)
                task.metadata.pop("delegation_wait_for_work_item_ids", None)
                await self._append_progress(
                    task,
                    "Intake dispatched; delivery card spawned, intake approved.",
                )
                # task.status will be synchronised to DONE by the phase hook
                # once we flip the intake work item to APPROVED below.
                if hasattr(self.store, "update_delegation_work_item"):
                    refreshed_intake = None
                    if hasattr(self.store, "get_delegation_work_item"):
                        try:
                            refreshed_intake = await self.store.get_delegation_work_item(parent_work_item_id)
                        except Exception:
                            refreshed_intake = None
                    current_phase = getattr(refreshed_intake or parent_work_item, "phase", None)
                    if current_phase == Phase.WAITING_FOR_CHILDREN:
                        await self.store.update_delegation_work_item(
                            parent_work_item_id,
                            phase=Phase.RUNNING,
                            blocked_reason="",
                            metadata_updates={
                                "frontier": "intake_delivery_spawned",
                            },
                        )
                    await self.store.update_delegation_work_item(
                        parent_work_item_id,
                        phase=Phase.APPROVED,
                        metadata_updates={
                            "dependency_work_item_ids": dependency_ids,
                            "waiting_on_work_item_ids": [],
                            "delegated_children_pending": False,
                            "intake_delivery_spawned": True,
                        },
                        claimed_by_role_runtime_session_id="",
                        claimed_by_seat_id="",
                    )
                await self.save_task(task)
                return False  # intake does not park; it closes out
        # A dependency only counts as pending while it can still move on its
        # own: terminal deps (APPROVED/FAILED/CANCELLED) and doomed deps
        # (transitively blocked by a terminal failure) are settled. Without
        # this, a triage turn that accepts partial results re-parks forever
        # on the already-FAILED child it just triaged — the settlement
        # release and this gate must agree on what "settled" means.
        park_doomed_ids: set[str] = set()
        park_items_by_id: dict[str, Any] = {}
        if parent_work_item is not None and hasattr(self.store, "list_delegation_work_items"):
            try:
                park_run_items = await self.store.list_delegation_work_items(parent_work_item.run_id)
            except Exception:
                park_run_items = []
            park_items_by_id = {
                str(getattr(item, "work_item_id", "") or "").strip(): item
                for item in park_run_items
                if str(getattr(item, "work_item_id", "") or "").strip()
            }
            park_doomed_ids = compute_doomed_work_item_ids(park_items_by_id)
        pending_dependency_ids: list[str] = []
        settled_failures_present = False
        for dep_id in dependency_ids:
            dependency = park_items_by_id.get(dep_id)
            if dependency is None:
                dependency = await self.store.get_delegation_work_item(dep_id)
            if dependency is None:
                pending_dependency_ids.append(dep_id)
                continue
            if dependency.phase == Phase.APPROVED:
                continue
            if dependency.phase in DONE_PHASES or dep_id in park_doomed_ids:
                settled_failures_present = True
                continue
            pending_dependency_ids.append(dep_id)
        task.metadata = dict(task.metadata)
        task.metadata["delegation_wait_for_work_item_ids"] = dependency_ids
        if not pending_dependency_ids:
            task.metadata.pop("delegation_pending_work_item_ids", None)
            parent_meta_for_stamp = (
                dict(getattr(parent_work_item, "metadata", {}) or {})
                if parent_work_item is not None
                else {}
            )
            has_settlement_stamp = bool(
                dict(parent_meta_for_stamp.get("dependency_settlement", {}) or {})
            )
            if settled_failures_present and not has_settlement_stamp:
                # Race: the children settled with failures while this turn
                # was still running, so the failure's transition hook fired
                # before this card could park — no triage release has been
                # scheduled (and the concurrent refresh may already have
                # regressed our phase to WAITING_FOR_CHILDREN without a
                # stamp). Park now and run the frontier immediately: the
                # settlement release re-arms the triage turn.
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.WAITING_FOR_CHILDREN,
                    reason="park_for_settled_failures",
                    metadata_updates={
                        "dependency_work_item_ids": dependency_ids,
                        "waiting_on_work_item_ids": [],
                        "delegated_children_pending": True,
                    },
                )
                await self.save_task(task)
                try:
                    await refresh_dependents_for_run(
                        self.store,
                        run_id=str(getattr(parent_work_item, "run_id", "") or "").strip(),
                        source_work_item_id=parent_work_item_id,
                        source_task_id=task.id,
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        "park_for_settled_failures: frontier refresh failed for "
                        f"{parent_work_item_id}"
                    )
                return True
            return False
        task.metadata["delegation_pending_work_item_ids"] = pending_dependency_ids
        await self._append_progress(
            task,
            "Waiting on delegated child work items: "
            + ", ".join(pending_dependency_ids[:8])
            + (" ..." if len(pending_dependency_ids) > 8 else ""),
        )
        park_metadata_updates: dict[str, Any] = {
            "dependency_work_item_ids": dependency_ids,
            "waiting_on_work_item_ids": pending_dependency_ids,
            "delegated_children_pending": True,
        }
        parent_meta_now = (
            dict(getattr(parent_work_item, "metadata", {}) or {})
            if parent_work_item is not None
            else {}
        )
        if bool(parent_meta_now.get("synthesis_turn_started")) or parent_meta_now.get(
            "dependency_settlement"
        ):
            # Re-parking after a synthesis / failure-triage turn rebuilt the
            # board: reset the one-shot synthesis marker and the stale
            # settlement stamp so the next wake runs a clean synthesis pass
            # over the new children (and the old failed-dep release cannot
            # leak into the rebuilt card's runnability check).
            park_metadata_updates["synthesis_turn_started"] = False
            park_metadata_updates["dependency_settlement"] = {}
            pre_kind = str(parent_meta_now.get("pre_synthesis_work_kind", "") or "").strip()
            if pre_kind and str(parent_meta_now.get("work_kind", "") or "").strip().lower() in {
                "synthesis",
                "synthesize",
            }:
                park_metadata_updates["work_kind"] = pre_kind
                park_metadata_updates["delegation_turn_kind"] = pre_kind
        # Phase A: single phase write, hook projects task.status=BLOCKED and
        # syncs local. Replaces the old "write task.status BLOCKED, save,
        # then separately write work_item.phase=WAITING_FOR_CHILDREN" double-pass.
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=Phase.WAITING_FOR_CHILDREN,
            reason="park_for_delegated_children",
            metadata_updates=park_metadata_updates,
        )
        await self.save_task(task)
        return True


    async def _spawn_delivery_card_after_intake(
        self,
        *,
        task: Task,
        intake_work_item: Any,
        dependency_ids: list[str],
    ) -> None:
        """Create the user-facing delivery work item once intake dispatches.

        Runs once per intake (idempotent via ``intake_delivery_spawned``
        flag set by the caller). The delivery card:

            - Inherits the intake's seat/team/role identity (same top-level
              agent owns it)
            - Depends on every child the intake just delegated, so it
              auto-advances WAITING_DEPENDENCIES → READY once they all
              approve (``_refresh_delegation_dependents`` already handles
              that edge)
            - Carries ``review_owner_kind="human"`` so the final review
              goes to AWAITING_HUMAN instead of bouncing to an
              upper-level agent (there is none above the root).
        """
        if not self.store or not hasattr(self.store, "save_delegation_work_item"):
            return
        run_id = str(getattr(intake_work_item, "run_id", "") or "").strip()
        if not run_id:
            return
        intake_meta = dict(getattr(intake_work_item, "metadata", {}) or {})
        role_id = str(getattr(intake_work_item, "role_id", "") or "").strip()
        delivery_projection_id = f"{role_id or 'root'}::delivery::{uuid.uuid4().hex[:8]}"
        intake_title = str(getattr(intake_work_item, "title", "") or "").strip()
        delivery_title = (
            f"Deliver final result to user: {intake_title}"
            if intake_title
            else "Deliver final result to user"
        )[:240]
        original_message = str(intake_meta.get("original_message", "") or "").strip()
        delivery_policy = {
            "user_visible": True,
            "authoritative_output": True,
            "requires_user_feedback": True,
        }
        plan_data = (
            serialized_company_plan_from_metadata(intake_meta)
            or serialized_company_plan_from_metadata(dict(intake_meta.get("delegation_playbook", {}) or {}))
        )
        if isinstance(plan_data, dict) and plan_data:
            try:
                plan = deserialize_company_work_item_runtime_plan(plan_data)
                root_projection_id = str(plan.root_projection_id or "").strip()
                raw_projection_policies: dict[str, dict[str, Any]] = {}
                for raw_projection in list(plan_data.get("projections", []) or []):
                    if not isinstance(raw_projection, dict):
                        continue
                    raw_projection_id = str(raw_projection.get("projection_id", "") or "").strip()
                    raw_policy = raw_projection.get("delivery_policy")
                    if raw_projection_id and isinstance(raw_policy, dict):
                        raw_projection_policies[raw_projection_id] = dict(raw_policy)
                for projection in plan.projections:
                    projection_id = str(projection.projection_id or "").strip()
                    if (
                        str(projection.projection_id or "").strip() == root_projection_id
                        or (
                            not root_projection_id
                            and str(projection.role_id or "").strip() == role_id
                            and str(projection.turn_type or "").strip() == "intake"
                        )
                    ):
                        raw_policy = raw_projection_policies.get(projection_id)
                        if raw_policy:
                            for key in ("user_visible", "authoritative_output"):
                                if key in raw_policy:
                                    delivery_policy[key] = bool(raw_policy.get(key))
                            if bool(raw_policy.get("requires_user_feedback", False)):
                                delivery_policy["requires_user_feedback"] = True
                        break
            except Exception:
                logger.opt(exception=True).debug("Failed to read delivery policy from intake work-item plan")
        # Owner-facing synthetic delivery cards are the stable handoff point
        # for follow-up directives. A projection-level false must not suppress
        # the human review card; review closure is an explicit runtime tool.
        delivery_policy["requires_user_feedback"] = True
        delivery_metadata = mark_work_item_projection(mark_work_item_runtime({
            "runtime_model": str(intake_meta.get("runtime_model", "") or "multi_team_org").strip(),
            "session_scope_id": task_session_scope_id(task) or str(intake_meta.get("session_scope_id", "") or "").strip(),
            "delegation_turn_kind": "delivery",
            "team_id": str(getattr(intake_work_item, "team_id", "") or intake_meta.get("team_id", "") or "").strip(),
            "team_instance_id": str(getattr(intake_work_item, "team_instance_id", "") or "").strip(),
            "seat_id": str(getattr(intake_work_item, "seat_id", "") or intake_meta.get("seat_id", "") or "").strip(),
            "seat_state_id": str(getattr(intake_work_item, "seat_state_id", "") or intake_meta.get("seat_state_id", "") or "").strip(),
            "batch_id": str(getattr(intake_work_item, "batch_id", "") or "").strip(),
            "work_kind": "delivery",
            "manager_role_id": str(getattr(intake_work_item, "manager_role_id", "") or "").strip(),
            "manager_seat_id": str(getattr(intake_work_item, "manager_seat_id", "") or "").strip(),
            "dependency_work_item_ids": list(dependency_ids),
            "waiting_on_work_item_ids": list(dependency_ids),
            "assigned_role_runtime_id": str(getattr(intake_work_item, "role_runtime_session_id", "") or intake_meta.get("assigned_role_runtime_id", "") or "").strip(),
            "contact_role_ids": list(intake_meta.get("contact_role_ids", []) or []),
            "allowed_delegate_role_ids": list(intake_meta.get("allowed_delegate_role_ids", []) or []),
            "delegation_playbook": dict(intake_meta.get("delegation_playbook", {}) or {}),
            "comms_workspace_root": str(intake_meta.get("comms_workspace_root", "") or "").strip(),
            "target_output_dir": str(intake_meta.get("target_output_dir", "") or "").strip(),
            "review_owner_kind": "human",
            "original_message": original_message,
            "intake_work_item_id": str(getattr(intake_work_item, "work_item_id", "") or "").strip(),
            "user_visible": bool(delivery_policy.get("user_visible", True)),
            "authoritative_output": bool(delivery_policy.get("authoritative_output", True)),
            "requires_user_feedback": bool(delivery_policy.get("requires_user_feedback", True)),
            "feedback_scope": "final",
        }, version=work_item_runtime_version(intake_meta)),
            projection_id=delivery_projection_id,
            turn_type="deliver",
        )
        delivery_work_item = DelegationWorkItem(
            run_id=run_id,
            cell_id=str(getattr(intake_work_item, "cell_id", "") or "").strip() or role_id,
            team_instance_id=delivery_metadata["team_instance_id"],
            team_id=delivery_metadata["team_id"],
            role_id=role_id,
            seat_id=delivery_metadata["seat_id"],
            seat_state_id=delivery_metadata["seat_state_id"],
            role_runtime_session_id=delivery_metadata["assigned_role_runtime_id"],
            parent_work_item_id=str(getattr(intake_work_item, "work_item_id", "") or "").strip(),
            source_role_id=role_id or None,
            source_seat_id=delivery_metadata["seat_id"] or None,
            title=delivery_title,
            summary=(
                "Synthesise all sub-team approved outputs and hand a final, "
                "user-facing result back to the requester. Do not re-delegate unless "
                "a critical gap is discovered — the team's work is done."
            ),
            kind="delivery",
            projection_id=delivery_projection_id,
            phase=Phase.WAITING_DEPENDENCIES,
            batch_id=delivery_metadata["batch_id"],
            batch_index=int(getattr(intake_work_item, "batch_index", 0) or 0) + 1,
            continuation_source=str(getattr(intake_work_item, "work_item_id", "") or "").strip(),
            manager_role_id=delivery_metadata["manager_role_id"],
            manager_seat_id=delivery_metadata["manager_seat_id"],
            metadata=delivery_metadata,
        )
        await self.store.save_delegation_work_item(delivery_work_item)
        if hasattr(self.store, "save_delegation_event"):
            try:
                await self.store.save_delegation_event(
                    DelegationEvent(
                        run_id=run_id,
                        work_item_id=delivery_work_item.work_item_id,
                        cell_id=delivery_work_item.cell_id,
                        role_id=delivery_work_item.role_id,
                        event_type="delivery_work_item_created",
                        payload={
                            "intake_work_item_id": delivery_metadata["intake_work_item_id"],
                            "dependency_work_item_ids": list(dependency_ids),
                        },
                    )
                )
            except Exception:
                logger.debug("Best-effort delivery work-item event persistence failed")


    async def _record_handoffs(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        """Propagate upstream collaboration warnings to the current task.

        The legacy "send a handoff message per cross-role dependency" path
        (StructuredHandoff + send_handoff + file-system ``handoffs/`` mirror)
        was removed as dead code — it was gated by ``not multi_team_org`` and
        never fired in the multi-team runtime, and the filesystem
        ``handoffs/`` tree was empty across every new02+ project session.
        Downstream tasks now receive upstream context via the normal prompt-
        building path (``task.metadata['work_item_summary_for_downstream']``, set when a
        work item completes in ``_ingest_work_item_result``) plus any collaboration
        warnings that upstream roles recorded — which is all this method
        still does.
        """
        collab_warnings: list[str] = []
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if not dep_task:
                continue
            warning = str(dep_task.metadata.get("_collaboration_awareness_warning", "") or "").strip()
            if warning:
                collab_warnings.append(warning)
        if collab_warnings:
            task.metadata = dict(task.metadata)
            task.metadata["_upstream_collaboration_warnings"] = collab_warnings
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["upstream_collaboration_warnings"] = collab_warnings


    async def _mirror_fields_to_work_item(self, task: Task, keys: list[str]) -> None:
        """Persist selected WorkItem-owned task metadata to WorkItem.

        This helper is retained as the migration bridge for old call sites:
        only keys declared WorkItem-owned in metadata_ownership are persisted.
        Runtime Task-owned fields are ignored.
        """
        if not self.store:
            return
        wid = linked_work_item_id_for_task(task)
        if not wid:
            return
        if not keys:
            return
        source = task.metadata or {}
        updates: dict[str, Any] = {k: source.get(k) for k in keys if k in source}
        if not updates:
            return
        try:
            await update_work_item_owned_metadata(self.store, wid, updates)
        except Exception:
            logger.opt(exception=True).debug(
                "WorkItem-owned metadata write failed keys=%s task=%s", keys, task.id
            )


    async def _append_progress(self, task: Task, message: str) -> None:
        wid = linked_work_item_id_for_task(task)
        task.metadata = dict(task.metadata or {})
        if wid and self.store:
            task.metadata.pop("progress_log", None)
            await append_work_item_progress(self.store, wid, message)
        else:
            progress = list(task.metadata.get("progress_log", []))
            progress.append(message)
            task.metadata["progress_log"] = progress
        working_memory = list(task.metadata.get("working_memory", []))
        working_memory.append(message)
        task.metadata["working_memory"] = working_memory[-12:]


    def _apply_role_defaults(self, task: Task, role: Any) -> None:
        if not task.metadata.get("handoff_template_ref") and getattr(role, "handoff_template_ref", None):
            task.metadata["handoff_template_ref"] = role.handoff_template_ref
        if not task.metadata.get("memory_policy_ref") and getattr(role, "memory_policy_ref", None):
            task.metadata["memory_policy_ref"] = role.memory_policy_ref
        if not task.metadata.get("artifact_contract_ref") and getattr(role, "artifact_contract_ref", None):
            task.metadata["artifact_contract_ref"] = role.artifact_contract_ref


    def _downstream_assignment_for_projection(self, dep_task: Task, next_task: Task) -> dict[str, Any] | None:
        target_projection_id = self._projection_id_for_task(next_task)
        dep_output_metadata = self._work_item_output_metadata_for_task(dep_task)
        for item in list(dep_output_metadata.get("downstream_assignments", []) or dep_task.metadata.get("downstream_assignments", []) or []):
            if not isinstance(item, dict):
                continue
            item_projection_id = str(
                item.get("work_item_projection_id")
                or item.get("target_projection_id")
                or item.get("projection_id")
                or ""
            ).strip()
            if item_projection_id != target_projection_id:
                continue
            return dict(item)
        return None


    def _capture_work_item_outputs(self, task: Task, result: TaskResult) -> WorkItemOutputBundle:
        summary = (result.content or "").strip()
        runtime_state = self._extract_runtime_state(result)
        structured_payload = self._extract_structured_work_item_payload(summary, result.artifacts)
        existing_artifacts = list(task.metadata.get("artifacts", []) or [])
        task.metadata = dict(task.metadata)
        for key in (
            "work_item_summary",
            "work_item_summary_for_downstream",
            "work_item_artifact_index",
            "verification_status",
            "verification_evidence",
            "verification",
            "structured_review_verdict",
            "delivery_package",
            "follow_up_actions",
            "downstream_assignments",
        ):
            task.metadata.pop(key, None)
        work_item_updates: dict[str, Any] = {}
        runtime_audit_updates: dict[str, Any] = {}
        if runtime_state:
            runtime_audit_updates["runtime_v2"] = runtime_state
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["runtime_v2"] = runtime_state
        if summary:
            work_item_updates["work_item_summary"] = summary
            work_item_updates["work_item_summary_for_downstream"] = summary
        artifacts = self._merge_unique_items(
            existing_artifacts,
            self._collect_artifact_refs(result.artifacts),
        )
        if artifacts:
            task.metadata["artifacts"] = artifacts
        if structured_payload.get("runtime_plan"):
            task.metadata["work_item_runtime_plan"] = structured_payload["runtime_plan"]
        artifact_index = self._normalize_work_item_artifact_index(structured_payload.get("artifact_index"))
        if not artifact_index:
            artifact_index = self._build_work_item_artifact_index(
                result.artifacts,
                list(task.metadata.get("artifacts", [])),
            )
        if artifact_index:
            work_item_updates["work_item_artifact_index"] = artifact_index
        delivery_package = self._normalize_delivery_package(structured_payload.get("delivery_package"))
        if delivery_package:
            work_item_updates["delivery_package"] = delivery_package
        follow_up_actions = self._normalize_follow_up_actions(
            structured_payload.get("follow_up_actions")
            or (result.artifacts.get("follow_up_actions") if result.artifacts else [])
        )
        if follow_up_actions:
            work_item_updates["follow_up_actions"] = follow_up_actions
        downstream_assignments = self._normalize_downstream_assignments(
            task,
            result.artifacts.get("downstream_assignments", []) if result.artifacts else [],
        )
        if downstream_assignments:
            work_item_updates["downstream_assignments"] = downstream_assignments
        review_verdict = self._normalize_review_verdict(structured_payload.get("review_verdict"))
        if review_verdict:
            work_item_updates["structured_review_verdict"] = review_verdict
        verification = result.artifacts.get("verification", []) if result.artifacts else []
        verification_evidence = dict(result.artifacts.get("verification_evidence", {}) if result.artifacts else {})
        if verification_evidence:
            work_item_updates["verification_evidence"] = verification_evidence
            runtime_audit_updates["runtime_verification_evidence"] = verification_evidence
        if verification:
            work_item_updates["verification"] = verification
            runtime_audit_updates["runtime_verification"] = verification
            verification_notes = [
                f"verification {item.get('verifier', '')}: {item.get('status', '')} - {item.get('summary', '')}".strip()
                for item in verification
                if isinstance(item, dict)
            ]
            risks = self._merge_unique_items(
                list(work_item_updates.get("risks", [])),
                [note for note in verification_notes if "failed" in note or "inconclusive" in note],
            )
            if risks:
                work_item_updates["risks"] = risks
        task.metadata["acceptance_criteria"] = list(task.metadata.get("acceptance_criteria", []))
        verification_status = self._build_verification_status(task, result, review_verdict=review_verdict)
        if verification_status:
            work_item_updates["verification_status"] = verification_status
            runtime_audit_updates["runtime_verification_status"] = verification_status
        if result.artifacts:
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["latest_artifacts"] = dict(result.artifacts)
        if work_item_updates:
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["work_item_owned_outputs"] = copy.deepcopy(work_item_updates)
        update_runtime_task_owned_metadata(task, runtime_audit_updates)
        if linked_work_item_id_for_task(task):
            strip_disallowed_work_item_metadata_from_runtime_task(task)
        else:
            task.metadata.update(copy.deepcopy(work_item_updates))

        turn_type = self._turn_type_for_task(task)
        projection_id = self._projection_id_for_task(task)
        if turn_type == "setup":
            self._capture_environment_manifest(task, result)
        if projection_id == _WORKSPACE_BOOTSTRAP_PROJECTION_ID:
            self._capture_workspace_manifest(task, result)
        if projection_id == _DATA_ACQUISITION_PROJECTION_ID:
            self._capture_data_acquisition_log(task, result)
            self._capture_data_acquisition_report(task, result)
            self._synthesize_data_acquisition_execution_record(task)
        return WorkItemOutputBundle(
            work_item_updates=work_item_updates,
            runtime_audit_updates=runtime_audit_updates,
            summary=summary,
        )


    async def _persist_work_item_owned_output_metadata(
        self,
        task: Task,
        bundle: WorkItemOutputBundle | None = None,
    ) -> None:
        """Persist business output metadata to the linked WorkItem.

        Task keeps result/runtime audit for execution replay, but the
        collaboration/board summary belongs to DelegationWorkItem.
        """
        if not self.store:
            return
        wid = linked_work_item_id_for_task(task)
        if not wid:
            return
        source = dict(bundle.work_item_updates if bundle is not None else task.metadata or {})
        business_keys = (
            "work_item_summary",
            "work_item_summary_for_downstream",
            "work_item_artifact_index",
            "verification_status",
            "verification_evidence",
            "verification",
            "structured_review_verdict",
            "delivery_package",
            "follow_up_actions",
            "downstream_assignments",
            "open_questions",
            "assumptions",
            "decisions",
            "risks",
            "completion_report",
            "handoff_context",
            "context_preview",
        )
        updates = {
            key: copy.deepcopy(source.get(key))
            for key in business_keys
            if source.get(key) not in (None, "", [], {})
        }
        try:
            summary = str(source.get("work_item_summary") or source.get("work_item_summary_for_downstream") or "").strip()
            if summary:
                updates.setdefault("deliverable_summary", summary)
            await update_work_item_owned_metadata(self.store, wid, updates)
            if summary and hasattr(self.store, "update_delegation_work_item"):
                await self.store.update_delegation_work_item(
                    wid,
                    deliverable_summary=summary,
                )
        except Exception:
            logger.opt(exception=True).debug(
                "WorkItem-owned output metadata write failed task=%s", task.id
            )


    async def _materialize_follow_up_work_items(
        self,
        task: Task,
        result: TaskResult,
    ) -> list[str]:
        if not self.store or not is_work_item_runtime_metadata(task.metadata):
            return []
        parent_work_item_id = linked_work_item_id_for_task(task)
        if not parent_work_item_id:
            return []
        output_metadata = self._work_item_output_metadata_for_task(task)
        actions = self._normalize_follow_up_actions(
            list(output_metadata.get("follow_up_actions", []) or task.metadata.get("follow_up_actions", []) or [])
            or (result.artifacts.get("follow_up_actions", []) if result.artifacts else [])
        )
        if not actions:
            return []
        parent_work_item = await self.store.get_delegation_work_item(parent_work_item_id)
        if parent_work_item is None:
            return []
        root_task = sorted(self._active_tasks, key=lambda item: (item.created_at, item.id))[0] if self._active_tasks else task
        runtime_topology = dict((root_task.metadata or {}).get("runtime_topology", {}) or {})
        seats = [dict(item) for item in list(runtime_topology.get("seats", []) or []) if isinstance(item, dict)]
        run_id = str(parent_work_item.run_id or task.metadata.get("delegation_run_id", "") or "").strip()
        if not run_id:
            return []
        existing_work_items = await self.store.list_delegation_work_items(run_id)
        follow_up_dependency_ids: list[str] = []
        created_work_item_ids: list[str] = []
        parent_metadata = dict(parent_work_item.metadata or {})
        parent_dependency_ids = [
            str(item).strip()
            for item in list(parent_metadata.get("dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        for action in actions:
            target_role_id = str(action.get("target_role_id", "") or "").strip()
            topology_seat = next(
                (
                    seat
                    for seat in seats
                    if str(seat.get("role_id", "") or "").strip() == target_role_id
                ),
                {},
            )
            seat_id = str(topology_seat.get("seat_id", "") or "").strip()
            if not seat_id:
                continue
            dedupe_key = str(action.get("dedupe_key", "") or "").strip() or (
                f"{str(task.metadata.get('delegation_seat_id', '') or '').strip()}::{target_role_id}::{action['action']}::{str(action.get('title', '') or '').strip()}"
            )
            duplicate = next(
                (
                    item
                    for item in existing_work_items
                    if str(item.manager_seat_id or "").strip() == str(task.metadata.get("delegation_seat_id", "") or "").strip()
                    and str((item.metadata or {}).get("follow_up_dedupe_key", "") or "").strip() == dedupe_key
                    and item.phase not in DONE_PHASES
                ),
                None,
            )
            if duplicate is not None:
                follow_up_dependency_ids.append(str(duplicate.work_item_id))
                continue
            dependency_work_item_ids = [
                str(dep).strip()
                for dep in list(action.get("depends_on_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            work_kind = "review" if action["action"] == "delegate_rereview" else "execute"
            turn_type = self._runtime_work_kind_to_work_item_turn_type(work_kind)
            follow_up_projection_id = f"followup::{target_role_id}::{uuid.uuid4().hex[:8]}"
            follow_up_work_item = DelegationWorkItem(
                run_id=run_id,
                cell_id=str(topology_seat.get("team_id", "") or target_role_id).strip(),
                team_instance_id=str(topology_seat.get("team_instance_id", "") or "").strip(),
                team_id=str(topology_seat.get("team_id", "") or "").strip(),
                role_id=target_role_id,
                seat_id=seat_id,
                seat_state_id=str(topology_seat.get("seat_state_id", "") or f"seat-state::{run_id}::{seat_id}").strip(),
                # Fix 2: canonical fallback when topology lacks the ID.
                role_runtime_session_id=(
                    str(topology_seat.get("role_runtime_session_id", "") or "").strip()
                    or canonical_role_session_id(
                        run_id=run_id,
                        role_id=target_role_id,
                        team_instance_id=str(topology_seat.get("team_instance_id", "") or "").strip(),
                    )
                ),
                parent_work_item_id=parent_work_item_id,
                source_role_id=self._role_id_for_task(task) or None,
                source_seat_id=str(task.metadata.get("delegation_seat_id", "") or "").strip() or None,
                title=str(action.get("title", "") or action["action"].replace("_", " ").title()).strip(),
                summary=str(action.get("summary", "") or action.get("reason", "") or result.content or "").strip(),
                kind=work_kind,
                projection_id=follow_up_projection_id,
                phase=Phase.WAITING_DEPENDENCIES if dependency_work_item_ids else Phase.READY,
                batch_id=str(parent_work_item.batch_id or f"batch::{run_id}::followup").strip(),
                batch_index=int(parent_work_item.batch_index or 0) + 1,
                continuation_source=str(parent_work_item.work_item_id or "").strip(),
                manager_role_id=self._role_id_for_task(task),
                manager_seat_id=str(task.metadata.get("delegation_seat_id", "") or "").strip(),
                metadata=mark_work_item_projection(mark_work_item_runtime({
                    "runtime_model": str(task.metadata.get("runtime_model", "") or "multi_team_org").strip(),
                    "session_scope_id": task_session_scope_id(task),
                    "delegation_turn_kind": work_kind,
                    "team_id": str(topology_seat.get("team_id", "") or "").strip(),
                    "seat_id": seat_id,
                    "seat_state_id": str(topology_seat.get("seat_state_id", "") or f"seat-state::{run_id}::{seat_id}").strip(),
                    "batch_id": str(parent_work_item.batch_id or f"batch::{run_id}::followup").strip(),
                    "work_kind": work_kind,
                    "manager_role_id": self._role_id_for_task(task),
                    "dependency_work_item_ids": dependency_work_item_ids,
                    "scope_key": str(action.get("scope_key", "") or dedupe_key).strip(),
                    # Fix 2: canonical fallback (same resolution as above).
                    "assigned_role_runtime_id": (
                        str(topology_seat.get("role_runtime_session_id", "") or "").strip()
                        or canonical_role_session_id(
                            run_id=run_id,
                            role_id=target_role_id,
                            team_instance_id=str(topology_seat.get("team_instance_id", "") or "").strip(),
                        )
                    ),
                    "contact_role_ids": list(topology_seat.get("contact_role_ids", []) or []),
                    "allowed_delegate_role_ids": list(topology_seat.get("allowed_delegate_role_ids", []) or []),
                    "delegation_playbook": dict(task.metadata.get("delegation_playbook", {}) or {}),
                    "comms_workspace_root": str(task.metadata.get("comms_workspace_root", "") or "").strip(),
                    "target_output_dir": str(task.metadata.get("target_output_dir", "") or "").strip(),
                    "user_visible": False,
                    "authoritative_output": False,
                    "follow_up_dedupe_key": dedupe_key,
                    "follow_up_action": action["action"],
                    "follow_up_reason": str(action.get("reason", "") or "").strip(),
                    "created_from_task_id": task.id,
                    "created_from_work_item_id": parent_work_item_id,
                }, version=work_item_runtime_version(task.metadata)),
                    projection_id=follow_up_projection_id,
                    turn_type=turn_type,
                ),
            )
            await self.store.save_delegation_work_item(follow_up_work_item)
            existing_work_items.append(follow_up_work_item)
            follow_up_dependency_ids.append(follow_up_work_item.work_item_id)
            created_work_item_ids.append(follow_up_work_item.work_item_id)
            if hasattr(self.store, "save_delegation_event"):
                try:
                    await self.store.save_delegation_event(
                        DelegationEvent(
                            run_id=run_id,
                            work_item_id=follow_up_work_item.work_item_id,
                            cell_id=follow_up_work_item.cell_id,
                            role_id=follow_up_work_item.role_id,
                            event_type="follow_up_work_item_created",
                            payload={
                                "parent_work_item_id": parent_work_item_id,
                                "action": action["action"],
                                "dedupe_key": dedupe_key,
                                "target_role_id": target_role_id,
                            },
                        )
                    )
                except Exception:
                    logger.debug("Best-effort follow-up delegation event persistence failed")
        if not follow_up_dependency_ids:
            return []
        work_item_by_id = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in existing_work_items
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        merged_dependency_ids, pruned_dependency_ids = normalize_dependency_work_item_ids(
            list(dict.fromkeys([*parent_dependency_ids, *follow_up_dependency_ids])),
            work_item_by_id,
            owner_work_item_id=parent_work_item_id,
        )
        parent_work_item.metadata = {
            **parent_metadata,
            "dependency_work_item_ids": merged_dependency_ids,
            "follow_up_actions": copy.deepcopy(actions),
        }
        if pruned_dependency_ids:
            parent_work_item.metadata["pruned_dependency_work_item_ids"] = list(
                dict.fromkeys(
                    [
                        *list(parent_metadata.get("pruned_dependency_work_item_ids", []) or []),
                        *pruned_dependency_ids,
                    ]
                )
            )
            parent_work_item.metadata["dependency_pruned_at"] = datetime.now().isoformat()
        await self.store.save_delegation_work_item(parent_work_item)
        supersede = getattr(self.store, "supersede_pending_checkpoints", None)
        if callable(supersede):
            await supersede(
                project_id=task.project_id or "default",
                task_id=task.id,
                checkpoint_types=["company_work_item_gate", "company_delivery_feedback"],
            )
        task.metadata = dict(task.metadata)
        task.metadata["delegation_wait_for_work_item_ids"] = merged_dependency_ids
        if linked_work_item_id_for_task(task):
            self._set_work_item_output_context(task, {"follow_up_actions": actions})
            task.metadata.pop("follow_up_actions", None)
        else:
            task.metadata["follow_up_actions"] = actions
        try:
            frontier_changed = await refresh_dependents_for_run(
                self.store,
                run_id=run_id,
                source_work_item_id=parent_work_item_id,
                source_task_id=task.id,
                source_role_id=self._role_id_for_task(task),
                source_cell_id=str(getattr(parent_work_item, "cell_id", "") or "").strip() or None,
            )
            if frontier_changed:
                self._signal_dispatcher_wake()
                await self._notify_kanban_changed()
        except Exception:
            logger.opt(exception=True).debug("Best-effort follow-up dependency frontier refresh failed")
        return created_work_item_ids


    def _capture_environment_manifest(self, task: Task, result: TaskResult) -> None:
        """Extract environment manifest from a setup work item's output."""
        manifest_data: dict[str, Any] = {}
        if result.artifacts and isinstance(result.artifacts, dict):
            manifest_data = dict(result.artifacts.get("environment_manifest", {}) or {})
        if not manifest_data:
            summary = str(result.content or "").strip()
            manifest_data = self._parse_env_manifest_from_text(summary)
        if not manifest_data:
            return
        import sys as _sys
        detected_platform = "windows" if _sys.platform.startswith("win") else ("macos" if _sys.platform == "darwin" else "linux")
        manifest = EnvironmentManifest(
            platform=str(manifest_data.get("platform", "") or detected_platform),
            tools_installed=list(manifest_data.get("tools_installed", []) or []),
            env_vars=dict(manifest_data.get("env_vars", {}) or {}),
            runtime_type=str(manifest_data.get("runtime_type", "native") or "native"),
            runtime_path=str(manifest_data.get("runtime_path", "") or ""),
            activate_command=str(manifest_data.get("activate_command", "") or ""),
            shell_prefix=str(manifest_data.get("shell_prefix", "") or ""),
            shell_prefix_win=str(manifest_data.get("shell_prefix_win", "") or ""),
            gpu_available=bool(manifest_data.get("gpu_available", False)),
            gpu_info=str(manifest_data.get("gpu_info", "") or ""),
            verification_checks=list(manifest_data.get("verification_checks", []) or []),
            verification_checks_win=list(manifest_data.get("verification_checks_win", []) or []),
            notes=str(manifest_data.get("notes", "") or ""),
        )
        task.metadata["environment_manifest"] = manifest.__dict__


    def _capture_workspace_manifest(self, task: Task, result: TaskResult) -> None:
        manifest_data = dict(task.metadata.get("workspace_manifest", {}) or {})
        if result.artifacts and isinstance(result.artifacts, dict):
            manifest_data = {**manifest_data, **dict(result.artifacts.get("workspace_manifest", {}) or {})}
        if not manifest_data:
            manifest_data = self._parse_workspace_manifest_from_text(str(result.content or "").strip())
        if not manifest_data:
            return
        reserved_paths = {
            str(key).strip(): str(value).strip()
            for key, value in dict(manifest_data.get("reserved_paths", {}) or {}).items()
            if str(key).strip() and str(value).strip()
        }
        notes = [str(item).strip() for item in list(manifest_data.get("notes", []) or []) if str(item).strip()]
        manifest = WorkspaceManifest(
            root_path=str(manifest_data.get("root_path", "") or task.metadata.get("target_output_dir", "") or "").strip(),
            manifest_path=str(manifest_data.get("manifest_path", "") or "").strip(),
            reserved_paths=reserved_paths,
            status=str(manifest_data.get("status", "ready") or "ready").strip(),
            notes=notes,
        )
        task.metadata["workspace_manifest"] = manifest.__dict__


    def _capture_data_acquisition_log(self, task: Task, result: TaskResult) -> None:
        log_data: dict[str, Any] = {}
        if result.artifacts and isinstance(result.artifacts, dict):
            log_data = dict(result.artifacts.get("data_acquisition_log", {}) or {})
        if not log_data:
            log_data = self._load_data_acquisition_artifact_file(task, artifact_kind="log")
        if not log_data:
            return
        attempted_sources = self._normalize_data_acquisition_items(log_data.get("attempted_sources", []))
        attempted_tools = self._normalize_data_acquisition_items(log_data.get("attempted_tools", []))
        prepared_assets = self._normalize_data_acquisition_items(log_data.get("prepared_assets", []))
        blocked_reasons = self._normalize_data_acquisition_items(log_data.get("blocked_reasons", []))
        notes = self._normalize_data_acquisition_items(log_data.get("notes", []))
        acquisition_attempted = self._infer_data_acquisition_attempted(
            log_data,
            attempted_sources=attempted_sources,
            prepared_assets=prepared_assets,
            blocked_reasons=blocked_reasons,
        )
        normalized = dict(log_data)
        normalized["attempted_sources"] = attempted_sources
        normalized["attempted_tools"] = attempted_tools
        normalized["prepared_assets"] = prepared_assets
        normalized["blocked_reasons"] = blocked_reasons
        normalized["notes"] = notes
        normalized["acquisition_attempted"] = acquisition_attempted
        normalized["log_path"] = self._data_acquisition_standard_path(task, artifact_kind="log")
        normalized["source_candidates_path"] = str(
            log_data.get("source_candidates_path", "") or default_source_candidates_path(task)
        ).strip()
        normalized["download_manifest_path"] = str(
            log_data.get("download_manifest_path", "") or default_download_manifest_path(task)
        ).strip()
        task.metadata["data_acquisition_log"] = normalized


    def _capture_data_acquisition_report(self, task: Task, result: TaskResult) -> None:
        report_data: dict[str, Any] = {}
        if result.artifacts and isinstance(result.artifacts, dict):
            report_data = dict(result.artifacts.get("data_acquisition_report", {}) or {})
        if not report_data:
            report_data = self._parse_data_acquisition_report_from_text(str(result.content or "").strip())
        if not report_data:
            report_data = self._load_data_acquisition_artifact_file(task, artifact_kind="report")
        if not report_data:
            return
        log_data = dict(task.metadata.get("data_acquisition_log", {}) or {})
        designated_input_dir = str(report_data.get("designated_input_dir", "") or "").strip()
        if not designated_input_dir:
            designated_input_dir = str(
                dict(task.metadata.get("workspace_manifest", {}) or {}).get("reserved_paths", {}).get("inputs", "")
                or ""
            ).strip()
        required_inputs = self._normalize_data_acquisition_items(report_data.get("required_inputs", []))
        present_inputs = self._normalize_data_acquisition_items(report_data.get("present_inputs", []))
        missing_inputs = self._normalize_data_acquisition_items(report_data.get("missing_inputs", []))
        attempted_sources = self._normalize_data_acquisition_items(
            report_data.get("attempted_sources", log_data.get("attempted_sources", []))
        )
        attempted_tools = self._merge_unique_items(
            self._normalize_data_acquisition_items(report_data.get("attempted_tools", [])),
            self._normalize_data_acquisition_items(log_data.get("attempted_tools", [])),
        )
        prepared_assets = self._normalize_data_acquisition_items(
            report_data.get("prepared_assets", log_data.get("prepared_assets", []))
        )
        blocked_reasons = self._normalize_data_acquisition_items(
            report_data.get("blocked_reasons", log_data.get("blocked_reasons", []))
        )
        notes = self._merge_unique_items(
            self._normalize_data_acquisition_items(report_data.get("notes", [])),
            self._normalize_data_acquisition_items(log_data.get("notes", [])),
        )
        acquisition_attempted = self._infer_data_acquisition_attempted(
            report_data,
            log_data=log_data,
            attempted_sources=attempted_sources,
            prepared_assets=prepared_assets,
            blocked_reasons=blocked_reasons,
        )
        report = DataAcquisitionReport(
            status=str(report_data.get("status", "missing_critical") or "missing_critical").strip(),
            designated_input_dir=designated_input_dir,
            required_inputs=required_inputs,
            present_inputs=present_inputs,
            missing_inputs=missing_inputs,
            attempted_sources=attempted_sources,
            attempted_tools=attempted_tools,
            prepared_assets=prepared_assets,
            blocked_reasons=blocked_reasons,
            acquisition_attempted=acquisition_attempted,
            report_path=self._data_acquisition_standard_path(task, artifact_kind="report"),
            log_path=str(log_data.get("log_path", "") or self._data_acquisition_standard_path(task, artifact_kind="log")).strip(),
            source_candidates_path=str(
                report_data.get("source_candidates_path", "")
                or log_data.get("source_candidates_path", "")
                or default_source_candidates_path(task)
            ).strip(),
            download_manifest_path=str(
                report_data.get("download_manifest_path", "")
                or log_data.get("download_manifest_path", "")
                or default_download_manifest_path(task)
            ).strip(),
            provenance_summary=self._normalize_data_acquisition_summary(report_data.get("provenance_summary", "")),
            notes=notes,
        )
        task.metadata["data_acquisition_report"] = report.__dict__


    def _synthesize_data_acquisition_execution_record(self, task: Task) -> None:
        report = dict(task.metadata.get("data_acquisition_report", {}) or {})
        log_data = dict(task.metadata.get("data_acquisition_log", {}) or {})
        if not report and not log_data:
            return
        output_path = Path(default_execution_record_path(task))
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        workspace_root = str(
            dict(task.metadata.get("workspace_manifest", {}) or {}).get("root_path", "")
            or task.metadata.get("target_output_dir", "")
            or ""
        ).strip()
        attempted_tools = self._merge_unique_items(
            self._normalize_data_acquisition_items(log_data.get("attempted_tools", [])),
            self._normalize_data_acquisition_items(report.get("attempted_tools", [])),
        )
        attempted_sources = self._merge_unique_items(
            self._normalize_data_acquisition_items(log_data.get("attempted_sources", [])),
            self._normalize_data_acquisition_items(report.get("attempted_sources", [])),
        )
        prepared_assets = self._merge_unique_items(
            self._normalize_data_acquisition_items(log_data.get("prepared_assets", [])),
            self._normalize_data_acquisition_items(report.get("prepared_assets", [])),
        )
        blocked_reasons = self._merge_unique_items(
            self._normalize_data_acquisition_items(log_data.get("blocked_reasons", [])),
            self._normalize_data_acquisition_items(report.get("blocked_reasons", [])),
        )
        lines = [
            "# Data Acquisition Execution Record",
            "",
            "## Scope",
            "Record the discovered sources, prepared assets, download attempts, and final readiness outcome for this data acquisition run.",
            "",
            "## Workspace",
            f"`{workspace_root}`" if workspace_root else "(unknown)",
            "",
            "## Execution Sequence",
            "1. Discover candidate sources.",
            "2. Verify candidate provenance.",
            "3. Prepare files or manifests inside the workspace.",
            "4. Publish readiness artifacts.",
            "",
            "## Attempted Tools",
        ]
        if attempted_tools:
            lines.extend(f"- `{item}`" for item in attempted_tools)
        else:
            lines.append("- (none recorded)")
        lines.extend([
            "",
            "## Structured Artifacts",
            f"- Source candidates: `{str(report.get('source_candidates_path', '') or log_data.get('source_candidates_path', '') or default_source_candidates_path(task)).strip()}`",
            f"- Download manifest: `{str(report.get('download_manifest_path', '') or log_data.get('download_manifest_path', '') or default_download_manifest_path(task)).strip()}`",
            f"- Readiness report: `{str(report.get('report_path', '') or self._data_acquisition_standard_path(task, artifact_kind='report')).strip()}`",
            f"- Acquisition log: `{str(report.get('log_path', '') or log_data.get('log_path', '') or self._data_acquisition_standard_path(task, artifact_kind='log')).strip()}`",
            "",
            "## Attempted Sources",
        ])
        if attempted_sources:
            lines.extend(f"- {item}" for item in attempted_sources)
        else:
            lines.append("- (none recorded)")
        lines.extend([
            "",
            "## Prepared Assets",
        ])
        if prepared_assets:
            lines.extend(f"- {item}" for item in prepared_assets)
        else:
            lines.append("- (none recorded)")
        lines.extend([
            "",
            "## Final Self-Audit",
            f"- Status: `{str(report.get('status', '') or 'missing_critical').strip()}`",
            f"- Acquisition attempted: `{bool(report.get('acquisition_attempted', False) or log_data.get('acquisition_attempted', False))}`",
        ])
        provenance_summary = str(report.get("provenance_summary", "") or "").strip()
        if provenance_summary:
            lines.append(f"- Provenance summary: {provenance_summary}")
        if blocked_reasons:
            lines.append("- Blockers:")
            lines.extend(f"  - {item}" for item in blocked_reasons[:10])
        try:
            output_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        except OSError:
            return
        artifacts = list(task.metadata.get("artifacts", []) or [])
        execution_record = str(output_path.resolve())
        if execution_record not in artifacts:
            task.metadata["artifacts"] = [*artifacts, execution_record]


    @staticmethod
    def _data_acquisition_candidate_paths(text: str) -> list[str]:
        candidates: list[str] = []
        stripped = text.strip()
        if not stripped:
            return candidates
        candidates.append(stripped)
        if stripped.startswith("```"):
            segments = stripped.split("```")
            for segment in segments:
                candidate = segment.strip()
                if not candidate:
                    continue
                if candidate.startswith("json"):
                    candidate = candidate[4:].strip()
                if candidate.startswith("{") or candidate.startswith("["):
                    candidates.append(candidate)
        return candidates


    def _data_acquisition_standard_path(self, task: Task, *, artifact_kind: str) -> str:
        work_item_gate = dict(task.metadata.get("work_item_gate", {}) or {})
        gate_metadata = dict(work_item_gate.get("metadata", {}) or {})
        key = "standard_log_path" if artifact_kind == "log" else "standard_report_path"
        default_value = (
            _DEFAULT_DATA_ACQUISITION_LOG_PATH
            if artifact_kind == "log"
            else _DEFAULT_DATA_ACQUISITION_REPORT_PATH
        )
        relative_path = str(gate_metadata.get(key, "") or default_value).strip()
        if not relative_path:
            return ""
        if Path(relative_path).is_absolute():
            return relative_path
        workspace_manifest = dict(task.metadata.get("workspace_manifest", {}) or {})
        reserved_paths = dict(workspace_manifest.get("reserved_paths", {}) or {})
        if relative_path.startswith("deliverables/") and reserved_paths.get("deliverables"):
            suffix = Path(relative_path).parts[1:]
            return str(Path(str(reserved_paths["deliverables"])).joinpath(*suffix))
        root_path = str(workspace_manifest.get("root_path", "") or task.metadata.get("target_output_dir", "") or "").strip()
        if root_path:
            return str(Path(root_path) / relative_path)
        return relative_path


    def _load_data_acquisition_artifact_file(self, task: Task, *, artifact_kind: str) -> dict[str, Any]:
        artifact_path = self._data_acquisition_standard_path(task, artifact_kind=artifact_kind)
        if not artifact_path:
            return {}
        path = Path(artifact_path)
        if not path.is_file():
            return {}
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        if artifact_kind == "report" and isinstance(parsed.get("data_acquisition_report"), dict):
            return dict(parsed.get("data_acquisition_report", {}) or {})
        if artifact_kind == "log" and isinstance(parsed.get("data_acquisition_log"), dict):
            return dict(parsed.get("data_acquisition_log", {}) or {})
        return parsed


    @staticmethod
    def _stringify_data_acquisition_item(value: Any) -> str:
        if isinstance(value, dict):
            label = str(
                value.get("name", "")
                or value.get("path", "")
                or value.get("source", "")
                or value.get("url", "")
                or value.get("title", "")
                or value.get("id", "")
                or ""
            ).strip()
            if not label:
                try:
                    label = json.dumps(value, ensure_ascii=False, sort_keys=True)
                except TypeError:
                    label = str(value).strip()
            qualifiers: list[str] = []
            status = str(value.get("status", "") or "").strip()
            if status:
                qualifiers.append(status)
            if bool(value.get("critical", False)):
                qualifiers.append("critical")
            return f"{label} ({', '.join(qualifiers)})" if qualifiers else label
        if isinstance(value, (list, tuple, set)):
            joined = ", ".join(
                text
                for text in (
                    CompanyExecutorDispatchMixin._stringify_data_acquisition_item(item)
                    for item in value
                )
                if text
            )
            return joined.strip()
        return str(value).strip()


    def _normalize_data_acquisition_items(self, value: Any) -> list[str]:
        if isinstance(value, list):
            raw_items = value
        elif value in (None, "", [], {}):
            raw_items = []
        else:
            raw_items = [value]
        normalized: list[str] = []
        for item in raw_items:
            text = self._stringify_data_acquisition_item(item)
            if text and text not in normalized:
                normalized.append(text)
        return normalized


    def _normalize_data_acquisition_summary(self, value: Any) -> str:
        if isinstance(value, dict):
            parts = []
            for key, item in value.items():
                key_text = str(key).strip()
                item_text = self._stringify_data_acquisition_item(item)
                if key_text and item_text:
                    parts.append(f"{key_text}: {item_text}")
            return "; ".join(parts).strip()
        if isinstance(value, list):
            return "; ".join(self._normalize_data_acquisition_items(value)).strip()
        return str(value or "").strip()


    def _infer_data_acquisition_attempted(
        self,
        report_data: dict[str, Any],
        *,
        log_data: dict[str, Any] | None = None,
        attempted_sources: list[str] | None = None,
        prepared_assets: list[str] | None = None,
        blocked_reasons: list[str] | None = None,
    ) -> bool:
        explicit = report_data.get("acquisition_attempted")
        if isinstance(explicit, bool):
            return explicit
        log_payload = dict(log_data or {})
        if isinstance(log_payload.get("acquisition_attempted"), bool):
            return bool(log_payload.get("acquisition_attempted"))
        attempted_items = list(attempted_sources or self._normalize_data_acquisition_items(report_data.get("attempted_sources", [])))
        if not attempted_items:
            attempted_items = self._normalize_data_acquisition_items(log_payload.get("attempted_sources", []))
        prepared_items = list(prepared_assets or self._normalize_data_acquisition_items(report_data.get("prepared_assets", [])))
        if not prepared_items:
            prepared_items = self._normalize_data_acquisition_items(log_payload.get("prepared_assets", []))
        blocked_items = list(blocked_reasons or self._normalize_data_acquisition_items(report_data.get("blocked_reasons", [])))
        if not blocked_items:
            blocked_items = self._normalize_data_acquisition_items(log_payload.get("blocked_reasons", []))
        acquisition_actions = self._normalize_data_acquisition_items(report_data.get("acquisition_actions", []))
        acquisition_actions = self._merge_unique_items(
            acquisition_actions,
            self._normalize_data_acquisition_items(log_payload.get("acquisition_actions", [])),
        )
        attempted_tools = self._normalize_data_acquisition_items(report_data.get("attempted_tools", []))
        attempted_tools = self._merge_unique_items(
            attempted_tools,
            self._normalize_data_acquisition_items(log_payload.get("attempted_tools", [])),
        )
        return bool(attempted_items or prepared_items or blocked_items or acquisition_actions or attempted_tools)


    @staticmethod
    def _parse_env_manifest_from_text(text: str) -> dict[str, Any]:
        """Best-effort extraction of env manifest from free-form text."""
        import json as _json
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("{") and "environment_manifest" in stripped:
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, dict) and "environment_manifest" in parsed:
                        return dict(parsed["environment_manifest"])
                except _json.JSONDecodeError:
                    pass
            if stripped.startswith("{") and "tools_installed" in stripped:
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, dict):
                        return parsed
                except _json.JSONDecodeError:
                    pass
        return {}


    @staticmethod
    def _parse_workspace_manifest_from_text(text: str) -> dict[str, Any]:
        import json as _json
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                parsed = _json.loads(stripped)
            except _json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "workspace_manifest" in parsed and isinstance(parsed["workspace_manifest"], dict):
                return dict(parsed["workspace_manifest"])
            if isinstance(parsed, dict) and "reserved_paths" in parsed:
                return parsed
        return {}


    @staticmethod
    def _parse_data_acquisition_report_from_text(text: str) -> dict[str, Any]:
        import json as _json
        for candidate in CompanyExecutorDispatchMixin._data_acquisition_candidate_paths(text):
            try:
                parsed = _json.loads(candidate)
            except _json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "data_acquisition_report" in parsed and isinstance(parsed["data_acquisition_report"], dict):
                return dict(parsed["data_acquisition_report"])
            if isinstance(parsed, dict) and str(parsed.get("status", "")).strip():
                return parsed
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                parsed = _json.loads(stripped)
            except _json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "data_acquisition_report" in parsed and isinstance(parsed["data_acquisition_report"], dict):
                return dict(parsed["data_acquisition_report"])
            if isinstance(parsed, dict) and str(parsed.get("status", "")).strip():
                return parsed
        return {}


    def _inherit_environment_manifest(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        """Propagate environment manifests from upstream setup work items."""
        if task.metadata.get("environment_manifest"):
            return
        merged_env_vars: dict[str, str] = {}
        merged_tools: list[dict[str, Any]] = []
        shell_prefix_parts: list[str] = []
        shell_prefix_win_parts: list[str] = []
        has_manifest = False

        def _collect_from_manifest(manifest: dict[str, Any]) -> None:
            nonlocal has_manifest
            if not manifest:
                return
            has_manifest = True
            merged_env_vars.update(dict(manifest.get("env_vars", {}) or {}))
            merged_tools.extend(list(manifest.get("tools_installed", []) or []))
            prefix = str(manifest.get("shell_prefix", "") or "").strip()
            if prefix and prefix not in shell_prefix_parts:
                shell_prefix_parts.append(prefix)
            prefix_win = str(manifest.get("shell_prefix_win", "") or "").strip()
            if prefix_win and prefix_win not in shell_prefix_win_parts:
                shell_prefix_win_parts.append(prefix_win)

        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if dep_task:
                _collect_from_manifest(dict(dep_task.metadata.get("environment_manifest", {}) or {}))
        if not has_manifest:
            for dependency_projection_id in task.dependencies:
                dep_task = task_by_projection_id.get(dependency_projection_id)
                if not dep_task:
                    continue
                for grand_dep_id in dep_task.dependencies:
                    grand_dep = task_by_projection_id.get(grand_dep_id)
                    if grand_dep:
                        _collect_from_manifest(dict(grand_dep.metadata.get("environment_manifest", {}) or {}))
        if has_manifest:
            task.metadata["inherited_environment"] = {
                "env_vars": merged_env_vars,
                "tools_available": merged_tools,
                "shell_prefix": " && ".join(shell_prefix_parts) if shell_prefix_parts else "",
                "shell_prefix_win": " ; ".join(shell_prefix_win_parts) if shell_prefix_win_parts else "",
            }


    def _inherit_workspace_manifest(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        if task.metadata.get("workspace_manifest"):
            return
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if dep_task and dep_task.metadata.get("workspace_manifest"):
                task.metadata["workspace_manifest"] = dict(dep_task.metadata.get("workspace_manifest", {}) or {})
                return
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if not dep_task:
                continue
            for grand_dep_id in dep_task.dependencies:
                grand_dep = task_by_projection_id.get(grand_dep_id)
                if grand_dep and grand_dep.metadata.get("workspace_manifest"):
                    task.metadata["workspace_manifest"] = dict(grand_dep.metadata.get("workspace_manifest", {}) or {})
                    return


    def _inherit_data_acquisition_report(self, task: Task, task_by_projection_id: dict[str, Task]) -> None:
        if task.metadata.get("data_acquisition_report"):
            return
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if dep_task and dep_task.metadata.get("data_acquisition_report"):
                task.metadata["data_acquisition_report"] = dict(dep_task.metadata.get("data_acquisition_report", {}) or {})
                return
        for dependency_projection_id in task.dependencies:
            dep_task = task_by_projection_id.get(dependency_projection_id)
            if not dep_task:
                continue
            for grand_dep_id in dep_task.dependencies:
                grand_dep = task_by_projection_id.get(grand_dep_id)
                if grand_dep and grand_dep.metadata.get("data_acquisition_report"):
                    task.metadata["data_acquisition_report"] = dict(grand_dep.metadata.get("data_acquisition_report", {}) or {})
                    return


    def _normalize_downstream_assignments(
        self,
        task: Task,
        value: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []

        source_projection_id = self._projection_id_for_task(task)
        plan = self._plan_view_for_task(task)
        if plan is None:
            return []
        allowed_projection_ids = set(plan.dependent_projection_ids(source_projection_id))
        if not allowed_projection_ids:
            return []

        helper = self.work_item_helper
        projection_lookup = plan.projection_by_id()
        global_intent_summary = str(task.metadata.get("global_intent_summary", "") or "").strip()
        if not global_intent_summary:
            global_intent_summary = helper._fallback_global_intent_summary(
                str(task.metadata.get("original_message", "") or "")
            )

        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            target_projection_id = str(
                item.get("work_item_projection_id")
                or item.get("projection_id")
                or ""
            ).strip()
            if not target_projection_id or target_projection_id not in allowed_projection_ids:
                continue
            projection = projection_lookup.get(target_projection_id)
            if projection is None:
                continue
            item_global_intent = str(item.get("global_intent_summary", "") or global_intent_summary).strip() or global_intent_summary
            normalized.append(
                helper._coerce_projection_assignment(
                    item,
                    projection=projection,
                    global_intent_summary=item_global_intent,
                )
            )
        return normalized


    @staticmethod
    def _extract_runtime_state(result: TaskResult | None) -> dict[str, Any]:
        artifacts = dict((result.artifacts if result else None) or {})
        runtime_session_id = str(artifacts.get("runtime_session_id", "") or "").strip()
        if not runtime_session_id:
            return {}
        return {
            "runtime_session_id": runtime_session_id,
            "active_subagents": list(artifacts.get("active_subagents", []) or []),
            "permission_requests": list(artifacts.get("permission_requests", []) or []),
            "compaction_boundaries": list(artifacts.get("compaction_boundaries", []) or []),
            "compaction_records": list(artifacts.get("compaction_records", artifacts.get("compaction_boundaries", [])) or []),
            "resume_cursor": artifacts.get("resume_cursor"),
            "worktree_path": str(artifacts.get("worktree_path", "") or "").strip(),
            "task_ledger": list(artifacts.get("task_ledger", []) or []),
            "prefetch_hits": list(artifacts.get("prefetch_hits", []) or []),
            "verification": dict(artifacts.get("verification", {}) or {}),
            "verification_evidence": dict(artifacts.get("verification_evidence", {}) or {}),
            "verification_verdict": str(artifacts.get("verification_verdict", "") or "").strip(),
            "artifact_manifest": list(artifacts.get("artifact_manifest", []) or []),
            "resume_state": dict(artifacts.get("resume_state", {}) or {}),
        }


    def _runtime_checkpoint_payload(self, task: Task) -> dict[str, Any]:
        runtime_state = dict(task.metadata.get("runtime_v2", {}) or {})
        if not runtime_state:
            runtime_state = self._extract_runtime_state(
                TaskResult(
                    status=TaskStatus.DONE,
                    artifacts=dict(task.result.get("artifacts", {}) if isinstance(task.result, dict) else {}),
                )
            )
        if not runtime_state:
            return {}
        return {
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
            "resume_state": dict(runtime_state.get("resume_state", {}) or {}),
        }


    def _collect_artifact_refs(self, artifacts: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        for key, value in (artifacts or {}).items():
            if isinstance(value, str) and value.strip():
                refs.append(f"{key}: {value.strip()}")
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        refs.append(f"{key}: {item.strip()}")
            elif isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, str) and nested_value.strip():
                        refs.append(f"{key}.{nested_key}: {nested_value.strip()}")
        unique_refs: list[str] = []
        for ref in refs:
            if ref not in unique_refs:
                unique_refs.append(ref)
        return unique_refs[:10]


    def _work_item_checkpoint_payload(self, task: Task) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        context_outputs = dict((task.context_snapshot or {}).get("work_item_owned_outputs", {}) or {})
        for key in (
            "work_item_artifact_index",
            "work_item_summary",
            "work_item_summary_for_downstream",
            "structured_review_verdict",
            "verification_status",
            "verification_evidence",
            "delivery_package",
        ):
            value = context_outputs.get(key)
            if value not in (None, "", [], {}):
                payload[key] = copy.deepcopy(value)
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
            "workspace_manifest",
            "data_acquisition_log",
            "data_acquisition_report",
            "gate_harness_status",
            "gate_harness_constraints",
            "gate_harness_pending_decision",
            "gate_harness_decision",
            "gate_harness_evidence",
        ):
            if key not in task.metadata:
                continue
            value = task.metadata.get(key)
            if value in (None, "", [], {}):
                continue
            payload[key] = value
        return payload


    def _extract_structured_work_item_payload(
        self,
        content: str,
        artifacts: dict[str, Any] | None,
    ) -> dict[str, Any]:
        artifact_payload = dict(artifacts or {})
        payload: dict[str, Any] = {}
        for key in (
            "runtime_plan",
            "work_item_runtime_plan",
            "artifact_index",
            "work_item_artifact_index",
            "review_verdict",
            "structured_review_verdict",
            "delivery_package",
            "final_delivery_package",
            "follow_up_actions",
        ):
            if key in artifact_payload:
                payload[key] = artifact_payload[key]
        decoder = json.JSONDecoder()
        search = str(content or "").strip()
        start = search.find("{")
        while start != -1:
            try:
                data, consumed = decoder.raw_decode(search[start:])
            except json.JSONDecodeError:
                start = search.find("{", start + 1)
                continue
            if isinstance(data, dict):
                for key in (
                    "runtime_plan",
                    "work_item_runtime_plan",
                    "artifact_index",
                    "work_item_artifact_index",
                    "review_verdict",
                    "structured_review_verdict",
                    "delivery_package",
                    "final_delivery_package",
                    "follow_up_actions",
                ):
                    if key in data and key not in payload:
                        payload[key] = data[key]
                if "review_verdict" not in payload and any(key in data for key in ("verdict", "decision", "status")):
                    payload["review_verdict"] = data
            start = search.find("{", start + consumed)
        if "runtime_plan" not in payload and "work_item_runtime_plan" in payload:
            payload["runtime_plan"] = payload["work_item_runtime_plan"]
        if "artifact_index" not in payload and "work_item_artifact_index" in payload:
            payload["artifact_index"] = payload["work_item_artifact_index"]
        if "review_verdict" not in payload and "structured_review_verdict" in payload:
            payload["review_verdict"] = payload["structured_review_verdict"]
        if "delivery_package" not in payload and "final_delivery_package" in payload:
            payload["delivery_package"] = payload["final_delivery_package"]
        return payload


    def _normalize_delivery_package(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        package = {str(key): val for key, val in value.items()}
        for list_key in (
            "delivered_items",
            "artifact_manifest",
            "constraints",
            "risks",
            "open_issues",
            "next_steps",
            "source_projection_refs",
        ):
            items = package.get(list_key, [])
            if not isinstance(items, list):
                package[list_key] = []
                continue
            normalized_items: list[Any] = []
            for item in items:
                if isinstance(item, dict):
                    normalized_items.append(dict(item))
                elif isinstance(item, str) and item.strip():
                    normalized_items.append(item.strip())
            package[list_key] = normalized_items
        summary = str(package.get("executive_summary", "") or package.get("summary", "") or "").strip()
        if summary:
            package["executive_summary"] = summary
        return package


    def _build_work_item_artifact_index(
        self,
        artifacts: dict[str, Any] | None,
        fallback_refs: list[str],
    ) -> list[dict[str, str]]:
        index: list[dict[str, str]] = []
        for key, value in dict(artifacts or {}).items():
            if isinstance(value, str) and value.strip():
                index.append({"kind": str(key), "label": str(key), "value": value.strip()})
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        index.append({"kind": str(key), "label": str(key), "value": item.strip()})
            elif isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    if isinstance(nested_value, str) and nested_value.strip():
                        index.append({
                            "kind": str(key),
                            "label": f"{key}.{nested_key}",
                            "value": nested_value.strip(),
                        })
        for ref in fallback_refs:
            if isinstance(ref, str) and ref.strip():
                index.append({"kind": "artifact_ref", "label": "artifact_ref", "value": ref.strip()})
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in index:
            fingerprint = (item.get("kind", ""), item.get("label", ""), item.get("value", ""))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(item)
        return deduped[:12]


    def _normalize_work_item_artifact_index(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, dict):
                rendered = {
                    "kind": str(item.get("kind", "") or "artifact").strip() or "artifact",
                    "label": str(item.get("label", "") or item.get("name", "") or "artifact").strip() or "artifact",
                    "value": str(item.get("value", "") or item.get("location", "") or item.get("path", "") or "").strip(),
                }
                if rendered["value"]:
                    normalized.append(rendered)
            elif isinstance(item, str) and item.strip():
                normalized.append({"kind": "artifact", "label": "artifact", "value": item.strip()})
        deduped: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in normalized:
            fingerprint = (item["kind"], item["label"], item["value"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(item)
        return deduped[:12]


    def _parse_worker_report(self, raw_content: str) -> dict[str, Any] | None:
        """Best-effort parse of a worker handoff report.

        The report-generation prompt suggests (but does not strictly
        require) a JSON object on the last line with shape::

            {
              "summary": str,
              "deliverables": [{"name", "path", "status"}],
              "acceptance_status": [{"criterion", "met", "evidence"}],
              "risks": [str],
              "next_actions": [str]
            }

        Per design: when parsing fails we DO NOT re-prompt the worker —
        we just hand the raw prose to the reviewer. So this helper
        returns ``None`` on failure and the caller falls back to prose.
        """
        text = str(raw_content or "").strip()
        if not text:
            return None
        # Try to find a JSON object in the tail of the prose.
        candidates: list[str] = []
        # 1. Try fenced ```json blocks anywhere.
        import re as _re
        for match in _re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=_re.DOTALL):
            candidates.append(match.group(1))
        # 2. Try the last balanced { ... } in the text.
        depth = 0
        start = -1
        last_balanced: str | None = None
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        last_balanced = text[start : i + 1]
        if last_balanced:
            candidates.append(last_balanced)
        for blob in reversed(candidates):
            try:
                parsed = json.loads(blob)
            except (ValueError, TypeError):
                continue
            if not isinstance(parsed, dict):
                continue
            # Accept anything dict-shaped — the schema is suggestive,
            # not strict. Keep only the recognized top-level keys so
            # downstream consumers see a clean payload.
            allowed_keys = {
                "summary",
                "deliverables",
                "acceptance_status",
                "risks",
                "next_actions",
            }
            cleaned: dict[str, Any] = {}
            for key in allowed_keys:
                if key in parsed:
                    cleaned[key] = parsed[key]
            # If none of the recognized keys were present, treat the
            # blob as non-report JSON (e.g. a tool call payload that
            # happens to trail the prose) and skip it.
            if not cleaned:
                continue
            return cleaned
        return None


    @staticmethod
    def _projection_id_for_task(task: Task) -> str:
        return projection_id_for_task(task)


    @staticmethod
    def _turn_type_for_task(task: Task, *, fallback: str = "") -> str:
        return turn_type_for_task(task, fallback=fallback)


    @staticmethod
    def _role_id_for_task(task: Task) -> str:
        return str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()


    def _role_name_for_task(self, task: Task) -> str:
        role_id = self._role_id_for_task(task)
        if not role_id:
            return ""
        # 優先使用 persona_name（人格化名字）
        persona = str(task.metadata.get("persona_name", "") or "").strip()
        if persona:
            return persona
        agent = self.org_engine.get_agent(role_id) if self.org_engine else None
        return str(getattr(agent, "name", "") or task.metadata.get("work_item_role_name", "") or role_id).strip()


    @staticmethod
    def _task_summary_for_map(task: Task) -> str:
        from opc.layer2_organization._company_executor_review import CompanyExecutorReviewMixin
        output_metadata = CompanyExecutorReviewMixin._work_item_output_metadata_for_task(task)
        summary = str(
            output_metadata.get("work_item_summary", "")
            or output_metadata.get("work_item_summary_for_downstream", "")
            or task.metadata.get("work_item_summary", "")
            or task.metadata.get("work_item_summary_for_downstream", "")
            or ""
        ).strip()
        if summary:
            return summary
        if isinstance(task.result, dict) and task.result.get("content"):
            return str(task.result.get("content", "")).strip()
        return ""


    def _task_open_issues(self, task: Task) -> list[str]:
        issues: list[str] = []
        output_metadata = self._work_item_output_metadata_for_task(task)
        review_verdict = self._normalize_review_verdict(
            output_metadata.get("structured_review_verdict")
            or task.metadata.get("structured_review_verdict")
        )
        if review_verdict.get("label") == "reject":
            summary = str(review_verdict.get("summary", "") or "review rejected").strip()
            issues.append(f"review rejected: {summary}")
        if task.status in {
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.AWAITING_PEER,
            *list(_REVIEW_WAITING_STATUSES),
        }:
            issues.append(f"status: {task.status.value}")
        for metadata_key, label in (
            ("gate_review_feedback", "gate rework"),
            ("contract_rework_feedback", "contract rework"),
            ("ceo_rework_feedback", "executive rework"),
            ("gate_harness_rework_feedback", "gate harness rework"),
        ):
            text = str(task.metadata.get(metadata_key, "") or "").strip()
            if text:
                issues.append(f"{label}: {text}")
        pending_decision = dict(task.metadata.get("gate_harness_pending_decision", {}) or {})
        if pending_decision:
            issues.append(f"gate harness pending: {str(pending_decision.get('summary', '') or '').strip()}")
        deduped: list[str] = []
        for issue in issues:
            if issue and issue not in deduped:
                deduped.append(issue)
        return deduped[:8]


    def _build_role_task_map(self, tasks: list[Task]) -> dict[str, dict[str, Any]]:
        role_task_map: dict[str, dict[str, Any]] = {}
        for task in tasks:
            role_id = self._role_id_for_task(task)
            if not role_id:
                continue
            entry = role_task_map.setdefault(
                role_id,
                {
                    "role_id": role_id,
                    "role_name": self._role_name_for_task(task),
                    "responsibility": str(
                        getattr(self.org_engine.get_agent(role_id) if self.org_engine else None, "responsibility", "") or ""
                    ).strip(),
                    "employees": [],
                    "work_items": [],
                },
            )
            employee_assignment = dict(task.metadata.get("employee_assignment", {}) or {})
            employee_payload = {
                "employee_id": str(employee_assignment.get("employee_id", "") or "").strip(),
                "employee_name": str(employee_assignment.get("name", "") or "").strip(),
                "role_id": str(employee_assignment.get("role_id", "") or role_id).strip(),
            }
            if employee_payload["employee_id"] and employee_payload not in entry["employees"]:
                entry["employees"].append(employee_payload)
            entry["work_items"].append(
                {
                    "projection_id": self._projection_id_for_task(task),
                    **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                    "title": task.title,
                    "status": getattr(task.status, "value", str(task.status)),
                    "summary": self._task_summary_for_map(task),
                    "open_issues": self._task_open_issues(task),
                    "assigned_to": role_id,
                    "role_name": self._role_name_for_task(task),
                    "employee_assignment": employee_assignment,
                    "work_item_assignment": dict(task.metadata.get("work_item_assignment", {}) or {}),
                }
            )
        return role_task_map


    def _inject_work_item_role_map(self, task: Task) -> None:
        turn_type = turn_type_for_task(task, fallback="")
        if turn_type not in {"intake", "plan", "dispatch", "aggregate", "deliver", "review"} and not bool(
            task.metadata.get("authoritative_output", False)
        ):
            return
        role_task_map = self._build_role_task_map(list(self._active_tasks))
        if not role_task_map:
            return
        task.metadata = dict(task.metadata)
        task.context_snapshot = dict(task.context_snapshot)
        task.metadata["work_item_role_task_map"] = role_task_map
        task.context_snapshot["work_item_role_task_map"] = role_task_map


    @staticmethod
    def _metadata_flag_true(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)


    def _collect_downstream_projection_ids(self, projection_id: str) -> list[str]:
        plan = self._active_plan
        if plan is None or not projection_id:
            return []
        dependents: dict[str, list[str]] = {}
        for projection in plan.projections:
            for dep in projection.dependency_projection_ids:
                dependents.setdefault(str(dep).strip(), []).append(str(projection.projection_id).strip())
        ordered: list[str] = []
        queue = list(dependents.get(projection_id, []))
        seen: set[str] = set()
        while queue:
            current = queue.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)
            ordered.append(current)
            queue.extend(dependents.get(current, []))
        return ordered


    async def _mark_run_awaiting_owner_from_delivery(
        self,
        task: Task,
        *,
        summary: str = "",
    ) -> None:
        if not self.store or not hasattr(self.store, "get_delegation_run") or not hasattr(self.store, "save_delegation_run"):
            return
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        if not run_id:
            return
        try:
            run = await self.store.get_delegation_run(run_id)
        except Exception:
            logger.opt(exception=True).debug("Failed to load delegation run for owner review lifecycle update")
            return
        if run is None:
            return
        run.lifecycle_status = "awaiting_owner"
        run.status = "running"
        if summary:
            run.latest_deliverable_summary = str(summary or "").strip()
        run.metadata = {
            **dict(run.metadata or {}),
            "awaiting_owner_review": True,
            "awaiting_owner_review_task_id": task.id,
            "awaiting_owner_review_at": datetime.now().isoformat(),
        }
        try:
            await self.store.save_delegation_run(run)
        except Exception:
            logger.opt(exception=True).debug("Failed to save delegation run owner review lifecycle update")


    async def _finalize_completed_work_item(self, task: Task) -> None:
        if self._is_authoritative_delivery_work_item(task):
            plan = self._active_plan or CompanyWorkItemRuntimePlan(
                profile=str(task.metadata.get("company_profile", "") or "company"),
            )
            tasks = list(self._active_tasks) or [task]
            package = self._build_authoritative_delivery_package(plan, tasks, task)
            task.metadata = dict(task.metadata)
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["delivery_package"] = package
            self._set_work_item_output_context(task, {"delivery_package": package})
            linked_work_item_id = linked_work_item_id_for_task(task)
            if linked_work_item_id:
                await update_work_item_owned_metadata(self.store, linked_work_item_id, {"delivery_package": package})
                task.metadata.pop("delivery_package", None)
            else:
                task.metadata["delivery_package"] = package
            assessment = await self._ceo_pre_delivery_assessment(task, plan, tasks, package)
            task.metadata["ceo_pre_delivery_assessment"] = dict(assessment)
            if bool(assessment.get("awaiting_human")):
                task.metadata["pre_delivery_assessment_status"] = str(
                    assessment.get("assessment_status", "awaiting_human") or "awaiting_human"
                )
                task.metadata["pre_delivery_assessment_failure_kind"] = str(
                    assessment.get("assessment_failure_kind", "") or ""
                )
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.AWAITING_HUMAN,
                    reason="pre_delivery_assessment_unavailable",
                )
                await self._append_progress(
                    task,
                    str(assessment.get("summary", "") or "Final delivery is awaiting human review."),
                )
                await self._mark_run_awaiting_owner_from_delivery(
                    task,
                    summary=str(assessment.get("summary", "") or "").strip(),
                )
                await self.save_task(task)
                await self._save_feedback_checkpoint(task)
                await self._emit_progress(
                    f"[Company:{self._projection_id_for_task(task)}] final delivery awaiting human review",
                    task_id=task.id,
                )
                return
            if not bool(assessment.get("deliverable", True)):
                task_by_projection_id: dict[str, Task] = {}
                fallback_projection_ids = [
                    self._projection_id_for_task(candidate)
                    for candidate in tasks
                    if candidate.id != task.id and self._task_open_issues(candidate)
                ]
                for work_item_task in tasks:
                    task_by_projection_id[work_item_task.id] = work_item_task
                    task_by_projection_id[self._projection_id_for_task(work_item_task)] = work_item_task
                rework_targets = self._resolve_ceo_rework_targets(
                    assessment.get("rework_targets", []),
                    task_by_projection_id,
                    fallback_projection_ids=fallback_projection_ids,
                    default_feedback=str(assessment.get("summary", "") or "").strip(),
                )
                if rework_targets:
                    try:
                        prior_pre_delivery_reworks = int(task.metadata.get("pre_delivery_rework_count", 0) or 0)
                    except (TypeError, ValueError):
                        prior_pre_delivery_reworks = 0
                    max_pre_delivery_reworks = self._resolve_max_pre_delivery_reworks(task)
                    if prior_pre_delivery_reworks >= max_pre_delivery_reworks:
                        task.metadata["pre_delivery_rework_cap_reached"] = True
                        task.metadata["pre_delivery_rework_cap"] = max_pre_delivery_reworks
                        await transition_work_item_from_task(
                            self.store, task,
                            target_status_or_phase=Phase.AWAITING_HUMAN,
                            reason="pre_delivery_rework_cap_reached",
                        )
                        await self._append_progress(
                            task,
                            (
                                "Final delivery reached the pre-delivery rework cap "
                                f"({max_pre_delivery_reworks}); awaiting human review."
                            ),
                        )
                        await self._mark_run_awaiting_owner_from_delivery(
                            task,
                            summary="Final delivery reached the pre-delivery rework cap; awaiting human review.",
                        )
                        await self.save_task(task)
                        await self._save_feedback_checkpoint(task)
                        await self._emit_progress(
                            f"[Company:{self._projection_id_for_task(task)}] pre-delivery rework cap reached",
                            task_id=task.id,
                        )
                        return
                    task.metadata["pre_delivery_rework_count"] = prior_pre_delivery_reworks + 1
                    for item in rework_targets:
                        target_projection_id = str(
                            item.get("target_projection_id")
                            or item.get("work_item_projection_id")
                            or ""
                        ).strip()
                        if not target_projection_id:
                            continue
                        await self._ceo_initiate_rework(
                            target_projection_id,
                            item.get("feedback", "") or str(assessment.get("summary", "") or ""),
                            task_by_projection_id,
                            source_task=task,
                            source="pre_delivery",
                        )
                    if task.status != TaskStatus.PENDING:
                        task.result = None
                        self._reset_work_item_outputs_for_rework(task)
                        await transition_work_item_from_task(
                            self.store, task,
                            target_status_or_phase=TaskStatus.PENDING,
                            reason="pre_delivery_rework_withheld",
                        )
                    await self._append_progress(task, "Final delivery withheld pending executive-directed rework.")
                    await self.save_task(task)
                    await self._emit_progress(
                        f"[Company:{self._projection_id_for_task(task)}] executive withheld delivery for rework",
                        task_id=task.id,
                    )
                    return
        if self._requires_user_feedback(task):
            await self._append_progress(task, "Awaiting user feedback before learning from this delivery.")
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.AWAITING_HUMAN,
                reason="awaiting_user_feedback_on_delivery",
            )
            await self._mark_run_awaiting_owner_from_delivery(
                task,
                summary=str(task.result.get("content", "") if isinstance(task.result, dict) else task.result or "").strip(),
            )
            await self.save_task(task)
            await self._save_feedback_checkpoint(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] awaiting user feedback",
                task_id=task.id,
            )
            return
        await self.save_task(task)


    def _requires_user_feedback(self, task: Task) -> bool:
        if getattr(task, "metadata", {}).get("execution_mode") != "company_mode":
            return False
        return (
            self._metadata_flag_true(getattr(task, "metadata", {}).get("requires_user_feedback", False))
            and self._is_final_human_acceptance_task(task)
        )


    def _deps_done(self, task: Task, tasks: list[Task]) -> bool:
        task_by_id = {t.id: t for t in tasks}
        missing = [dep for dep in task.dependencies if dep not in task_by_id]
        if missing:
            raise ValueError(f"Task `{task.title}` has unresolved dependencies: {', '.join(missing)}")
        return all(task_by_id[dep].status == TaskStatus.DONE for dep in task.dependencies)


    def _deps_satisfied(self, task: Task, tasks: list[Task]) -> bool:
        """Flexible dependency check supporting hard/soft/info classification.

        - hard: dep must be DONE (used for synthesize/deliver waiting on children)
        - soft: dep must be at least RUNNING or DONE (default — maximises parallelism)
        - info: never blocks (awareness only, e.g. parallel siblings)
        """
        task_by_id = {t.id: t for t in tasks}
        missing = [dep for dep in task.dependencies if dep not in task_by_id]
        if missing:
            raise ValueError(f"Task `{task.title}` has unresolved dependencies: {', '.join(missing)}")
        dep_classes = task.metadata.get("dependency_classes") or {}
        for dep_id in task.dependencies:
            dep_task = task_by_id[dep_id]
            dep_class = dep_classes.get(dep_id) or self._infer_dependency_class(task, dep_task)
            if dep_class == "hard" and dep_task.status != TaskStatus.DONE:
                return False
            if dep_class == "soft" and dep_task.status not in {TaskStatus.DONE, TaskStatus.RUNNING}:
                return False
            # "info" deps never block
        return True


    @staticmethod
    def _infer_dependency_class(task: Task, dep_task: Task) -> str:
        """Infer dependency class from work-item metadata when not explicitly annotated.

        Returns "hard", "soft", or "info".
        """
        task_meta = task.metadata or {}
        dep_meta = dep_task.metadata or {}
        # Siblings in the same parallel group → info (awareness, not blocking)
        task_pg = str(task_meta.get("work_item_parallel_group", "") or "").strip()
        dep_pg = str(dep_meta.get("work_item_parallel_group", "") or "").strip()
        if task_pg and task_pg == dep_pg:
            return "info"
        # Synthesize/deliver work items hard-depend on their direct children
        task_kind = work_item_turn_type_from_metadata(task_meta, fallback="")
        if task_kind in {"synthesize", "deliver"}:
            dep_manager = str(dep_meta.get("manager_role_id", "") or "").strip()
            task_role = str(task.assigned_to or task_meta.get("work_item_role_id", "") or "").strip()
            if dep_manager == task_role:
                return "hard"
        # Default: soft — allow tasks to proceed with partial upstream info
        return "soft"


    def _select_authoritative_delivery_task(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> Task | None:
        projection_order = plan.projection_order_map()
        candidates = [
            task
            for task in tasks
            if task.status == TaskStatus.DONE
            and (
                bool(task.metadata.get("authoritative_output", False))
                or bool(task.metadata.get("user_visible", False))
                or self._turn_type_for_task(task) in {"deliver", "aggregate"}
            )
        ]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda task: (
                projection_order.get(self._projection_id_for_task(task), len(projection_order)),
                task.created_at,
                task.id,
            ),
        )[-1]


    def _build_authoritative_delivery_package(
        self,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        delivery_task: Task,
    ) -> dict[str, Any]:
        delivery_outputs = self._work_item_output_metadata_for_task(delivery_task)
        package = self._normalize_delivery_package(
            delivery_outputs.get("delivery_package")
            or (delivery_task.context_snapshot or {}).get("delivery_package")
            or delivery_task.metadata.get("delivery_package")
        )
        if not package:
            package = {
                "executive_summary": str(
                    (delivery_task.result or {}).get("content")
                    or delivery_outputs.get("work_item_summary", "")
                    or delivery_outputs.get("work_item_summary_for_downstream", "")
                    or delivery_task.metadata.get("work_item_summary", "")
                    or delivery_task.title
                ).strip(),
                "delivered_items": [],
                "artifact_manifest": [],
                "constraints": [],
                "risks": [],
                "open_issues": [],
                "next_steps": [],
                "source_projection_refs": [],
            }
        package.setdefault("delivered_items", [])
        package.setdefault("artifact_manifest", [])
        package.setdefault("constraints", [])
        package.setdefault("risks", [])
        package.setdefault("open_issues", [])
        package.setdefault("next_steps", [])
        package.setdefault("source_projection_refs", [])
        package["role_task_map"] = self._build_role_task_map(tasks)

        for task in tasks:
            projection_id = self._projection_id_for_task(task)
            output_metadata = self._work_item_output_metadata_for_task(task)
            package["source_projection_refs"].append(
                {
                    "projection_id": projection_id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                    "title": task.title,
                    "status": task.status.value,
                    "assigned_to": self._role_id_for_task(task),
                    "role_name": self._role_name_for_task(task),
                    "employee_assignment": dict(task.metadata.get("employee_assignment", {}) or {}),
                    "summary": self._task_summary_for_map(task),
                    "open_issues": self._task_open_issues(task),
                    "gate_harness_status": str(task.metadata.get("gate_harness_status", "") or "").strip(),
                    "constraints": list(task.metadata.get("gate_harness_constraints", []) or []),
                }
            )
            if task.id == delivery_task.id:
                continue
            summary = str(
                output_metadata.get("work_item_summary", "")
                or output_metadata.get("work_item_summary_for_downstream", "")
                or ""
            ).strip()
            if summary and task.status == TaskStatus.DONE:
                package["delivered_items"].append({
                    "work_item_title": task.title,
                    "status": task.status.value,
                    "summary": summary,
                })
            for item in list(output_metadata.get("work_item_artifact_index", []) or []):
                if not isinstance(item, dict):
                    continue
                entry = dict(item)
                entry["work_item_title"] = task.title
                package["artifact_manifest"].append(entry)
            for risk in list(output_metadata.get("risks", []) or []):
                text = str(risk or "").strip()
                if text:
                    package["risks"].append(text)
            for constraint in list(task.metadata.get("gate_harness_constraints", []) or []):
                text = str(constraint or "").strip()
                if text:
                    package["constraints"].append(f"{task.title}: {text}")
            review_verdict = self._normalize_review_verdict(
                output_metadata.get("structured_review_verdict")
                or task.metadata.get("structured_review_verdict")
            )
            if review_verdict.get("label") == "reject":
                package["open_issues"].append(
                    f"{task.title}: {str(review_verdict.get('summary', '') or 'review rejected').strip()}"
                )
            if task.status in {
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
                TaskStatus.AWAITING_PEER,
                *list(_REVIEW_WAITING_STATUSES),
            }:
                package["open_issues"].append(f"{task.title}: {task.status.value}")

        if not str(package.get("executive_summary", "") or "").strip():
            package["executive_summary"] = delivery_task.title
        return package


    def _render_delivery_package(self, plan: CompanyWorkItemRuntimePlan, package: dict[str, Any]) -> str:
        parts = [f"## Company Work-Item Runtime: {plan.profile}", "## Final Delivery"]
        summary = str(package.get("executive_summary", "") or "").strip()
        if summary:
            parts.append(summary)

        delivered_items = list(package.get("delivered_items", []) or [])
        if delivered_items:
            lines = []
            for item in delivered_items:
                if isinstance(item, dict):
                    work_item_title = str(item.get("work_item_title", "") or item.get("title", "") or "Work item").strip()
                    summary_text = str(item.get("summary", "") or "").strip()
                    if summary_text:
                        lines.append(f"- {work_item_title}: {summary_text}")
                else:
                    text = str(item).strip()
                    if text:
                        lines.append(f"- {text}")
            if lines:
                parts.append("### Delivered Items")
                parts.append("\n".join(lines[:8]))

        artifact_manifest = list(package.get("artifact_manifest", []) or [])
        if artifact_manifest:
            lines = []
            for item in artifact_manifest:
                if not isinstance(item, dict):
                    continue
                work_item_title = str(item.get("work_item_title", "") or "").strip()
                label = str(item.get("label", "") or item.get("kind", "") or "artifact").strip()
                value = str(item.get("value", "") or "").strip()
                text = f"- {work_item_title}: {label}" if work_item_title else f"- {label}"
                if value:
                    text = f"{text} -> {value}"
                lines.append(text)
            if lines:
                parts.append("### Evidence")
                parts.append("\n".join(lines[:12]))

        constraints = [str(item).strip() for item in list(package.get("constraints", []) or []) if str(item).strip()]
        if constraints:
            parts.append("### Constraints")
            parts.append("\n".join(f"- {item}" for item in constraints[:8]))

        open_issues = [str(item).strip() for item in list(package.get("open_issues", []) or []) if str(item).strip()]
        if open_issues:
            parts.append("### Open Issues")
            parts.append("\n".join(f"- {item}" for item in open_issues[:8]))

        risks = [str(item).strip() for item in list(package.get("risks", []) or []) if str(item).strip()]
        if risks:
            parts.append("### Risks")
            parts.append("\n".join(f"- {item}" for item in risks[:8]))
        return "\n\n".join(parts)


    def _summarize_results(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
        if self._uses_multi_team_org_runtime(tasks, plan):
            return self._summarize_multi_team_org_results(tasks)
        delivery_task = self._select_authoritative_delivery_task(plan, tasks)
        if delivery_task is not None:
            package = self._build_authoritative_delivery_package(plan, tasks, delivery_task)
            return self._render_delivery_package(plan, package)
        parts = [f"## Company Work-Item Runtime: {plan.profile}"]
        for task in tasks:
            status = task.status.value
            content = ""
            if task.result and task.result.get("content"):
                content = task.result["content"].strip()
            parts.append(f"### {task.title} [{status}]")
            if content:
                parts.append(content)
        return "\n\n".join(parts)


    @staticmethod
    def _task_has_delegated_downstream_work(task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        if bool(metadata.get("manager_board_mutation_performed", False)):
            return True
        if bool(metadata.get("delegated_children_pending", False)):
            return True
        for key in (
            "delegation_wait_for_work_item_ids",
            "delegation_pending_work_item_ids",
            "manager_board_modified_work_item_ids",
            "manager_board_deleted_work_item_ids",
        ):
            if [
                str(item).strip()
                for item in list(metadata.get(key, []) or [])
                if str(item).strip()
            ]:
                return True
        return False


    async def _pending_checkpoint_task_ids(self, project_id: str) -> set[str]:
        """Task ids referenced by pending execution checkpoints for the project."""
        get_pending = getattr(self.store, "get_pending_checkpoints", None)
        if not callable(get_pending) or not self._store_is_ready(self.store):
            return set()
        try:
            rows = await get_pending(project_id=project_id)
        except Exception:
            logger.opt(exception=True).debug(
                "_pending_checkpoint_task_ids: pending checkpoint load failed"
            )
            return set()
        task_ids: set[str] = set()
        for row in rows or []:
            payload = dict(getattr(row, "payload", {}) or {})
            task_id = str(
                getattr(row, "task_id", "")
                or payload.get("waiting_task_id", "")
                or payload.get("task_id", "")
                or ""
            ).strip()
            if task_id:
                task_ids.add(task_id)
        return task_ids


    def _summarize_human_parked_exit(self, tasks: list[Task], human_waiting: list[Task]) -> str:
        lines = [
            "## Organization Runtime Parked",
            "All remaining work items are waiting on human input. "
            "Answer the pending approval/review card(s) and the run will continue from where it stopped.",
            "",
        ]
        for task in sorted(human_waiting, key=lambda item: (item.created_at, item.id)):
            status = str(task.status.value if isinstance(task.status, TaskStatus) else task.status)
            lines.append(f"- {task.title}: {status}")
        return "\n".join(lines)


    def _summarize_multi_team_org_results(self, tasks: list[Task]) -> str:
        if not tasks:
            return "No organization runtime tasks were found."
        ordered = sorted(tasks, key=lambda item: (item.created_at, item.id))
        def _task_content(task: Task) -> str:
            return str(((task.result or {}).get("content", "") if isinstance(task.result, dict) else "") or "").strip()

        final_delivery_task = next(
            (
                task for task in sorted(ordered, key=lambda item: (item.created_at, item.id), reverse=True)
                if task.status == TaskStatus.DONE
                and _task_content(task)
                and str((task.metadata or {}).get("feedback_scope", "") or "").strip().lower() == "final"
                and turn_type_for_task(task, fallback="") == "deliver"
                and bool((task.metadata or {}).get("authoritative_output", False))
            ),
            None,
        )
        if final_delivery_task is not None:
            return _task_content(final_delivery_task)

        root_task = next(
            (
                task for task in ordered
                if bool((task.metadata or {}).get("authoritative_output", False))
                and str((task.metadata or {}).get("execution_model", "") or "").strip() == "multi_team_org"
                and not self._task_has_delegated_downstream_work(task)
            ),
            ordered[0],
        )
        root_content = _task_content(root_task)
        if root_task.status == TaskStatus.DONE and root_content:
            return root_content
        lines = ["## Organization Runtime Snapshot"]
        for task in ordered:
            status = str(task.status.value if isinstance(task.status, TaskStatus) else task.status)
            lines.append(f"- {task.title}: {status} [{status}]")
        if root_content:
            lines.append("")
            lines.append("## Latest Root Summary")
            lines.append(root_content)
        return "\n".join(lines)


    def _progress_identity_for_task_id(self, task_id: str | None) -> tuple[str, str]:
        tid = str(task_id or "").strip()
        if not tid:
            return "", ""
        for task in list(self._active_tasks or []):
            if str(getattr(task, "id", "") or "").strip() != tid:
                continue
            role_id = self._role_id_for_task(task)
            role_name = self._role_name_for_task(task)
            return role_id, role_name
        return "", ""


    async def _emit_progress(self, message: str, *, task_id: str | None = None) -> None:
        logger.info(message)
        if self.progress_callback:
            kwargs: dict[str, Any] = {"task_id": task_id}
            role_id, role_name = self._progress_identity_for_task_id(task_id)
            if role_id:
                kwargs["agent_role_id"] = role_id
            if role_name:
                kwargs["agent_name"] = role_name
            try:
                await self.progress_callback(message, **kwargs)
            except TypeError as exc:
                if "unexpected keyword argument" not in str(exc):
                    raise
                try:
                    await self.progress_callback(message, task_id=task_id)
                except TypeError as fallback_exc:
                    if "unexpected keyword argument" not in str(fallback_exc):
                        raise
                    await self.progress_callback(message)


    def _validate_dependencies(self, tasks: list[Task]) -> None:
        valid_ids = {task.id for task in tasks}
        for task in tasks:
            missing = [dep for dep in task.dependencies if dep not in valid_ids]
            if missing:
                raise ValueError(f"Task `{task.title}` references unknown dependencies: {', '.join(missing)}")


    def _extract_bullets(self, content: str, prefixes: tuple[str, ...]) -> list[str]:
        items: list[str] = []
        for raw_line in content.splitlines():
            line = raw_line.strip().lstrip("-").strip()
            lower = line.lower()
            if any(lower.startswith(prefix) for prefix in prefixes):
                items.append(line)
        return items


    def _merge_unique_items(self, existing: list[str], new_items: list[str]) -> list[str]:
        merged = list(existing)
        for item in new_items:
            value = item.strip()
            if value and value not in merged:
                merged.append(value)
        return merged[:12]


