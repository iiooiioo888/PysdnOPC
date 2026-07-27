"""公司工作項目規劃與執行運行時。"""

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
from typing import Any, Awaitable, Callable

from loguru import logger

from opc.core.active_task_runs import (
    ActiveTaskRunAdmissionClosed,
    ActiveTaskRunRegistry,
)
from typing import Mapping
from opc.core.config import DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS, DEFAULT_ORGANIZATION_ID
from opc.core.models import (
    AdaptiveRoleProfile,
    AdaptiveSignalSpec,
    AdaptiveWorkItemProfile,
    ApprovalAction,
    ArtifactContract,
    CompanyMemberSession,
    CoordinationSpec,
    DataAcquisitionReport,
    DelegationEvent,
    DelegationWorkItem,
    EnvironmentManifest,
    Phase,
    RouterDecision,
    StructuredReviewVerdict,
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
    TODO_PHASES,
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
# Import for side effect: registering the phase-transition hooks. The
# serial queue reconciler is also called explicitly from the dispatcher
# tick before runnable work is claimed.
from opc.layer2_organization import phase_hooks  # noqa: F401
from opc.layer2_organization.collaboration_service import CollaborationContext
from opc.layer2_organization.phase_hooks import reconcile_role_serial_queues
from opc.layer2_organization.session_scoping import task_session_scope_id
from opc.layer2_organization.turn_mode import reset_manager_dispatch_turn_metadata
from opc.layer2_organization.data_acquisition_policy import (
    DEFAULT_ACQUISITION_EXECUTION_RECORD_RELATIVE_PATH,
    default_download_manifest_path,
    default_execution_record_path,
    default_source_candidates_path,
    has_downloaded_binary_asset,
    requires_binary_asset_acquisition,
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
from opc.layer2_organization.prompt_contract import (
    has_prompt_contract,
    make_prompt_contract,
    prompt_contract_from_work_item,
)
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemGatePolicy,
    WorkItemProjectionSpec,
    deserialize_company_work_item_plan,
    serialize_company_work_item_plan,
)
from opc.layer2_organization.recruiter import (
    normalize_recruitment_agent_choice,
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
    gate_rework_payload,
    is_delivery_turn,
    is_manager_reviewable_turn,
    mark_projected_work_item_task,
    mark_gate_rework_projection,
    mark_work_item_projection,
    projection_id_for_task,
    projection_id_for_work_item,
    rework_projection_id_for_gate,
    target_projection_id_for_decision,
    target_projection_ids_for_decision,
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

from opc.layer2_organization._company_work_item_helper import (  # noqa: E402
    CompanyRuntimeSpec,
    CompanyRuntimeSpecBuilder,
    CompanyRuntimeWorkItemHelper,
    deserialize_company_runtime_spec,
    serialize_company_runtime_spec,
    _DATA_ACQUISITION_PROJECTION_ID,
    _WORKSPACE_BOOTSTRAP_PROJECTION_ID,
)
from opc.layer2_organization._company_executor_communication import (  # noqa: E402
    CompanyExecutorCommunicationMixin,
)
from opc.layer2_organization._company_executor_dispatch import (  # noqa: E402
    CompanyExecutorDispatchMixin,
)
from opc.layer2_organization._company_executor_review import (  # noqa: E402
    CompanyExecutorReviewMixin,
)
from opc.layer2_organization._company_mode_shared import (  # noqa: E402,F401
    CEO_PRE_DELIVERY_ASSESSMENT_PROMPT,
    CompanyExecutorDriverOwnership,
    CompanyExecutorRunState,
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
    WorkItemOutputBundle,
)


class CompanyWorkItemExecutor(
    CompanyExecutorDispatchMixin,
    CompanyExecutorReviewMixin,
    CompanyExecutorCommunicationMixin,
):
    """Dispatches company work-item runtime turns through projected tasks.

    This is the formal executor name for company mode. Projected task
    records use work-item projection identity for UI, resume, and
    checkpoint payloads.
    """

    def __init__(
        self,
        org_engine: OrgEngine,
        communication: Any,
        approval_engine: Any,
        memory: Any | None,
        execute_task: Callable[[Task], Awaitable[TaskResult]],
        save_task: Callable[[Task], Awaitable[None]],
        seat_executor: SeatExecutor | None = None,
        save_runtime_session: Callable[..., Awaitable[None]] | None = None,
        progress_callback: Callable[..., Awaitable[None]] | None = None,
        checkpoint_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        agent_selector: Callable[[Task, Any | None], Awaitable[str | None]] | None = None,
        emit_runtime_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_kanban_changed: Callable[[], Awaitable[None]] | None = None,
        work_item_timeout: int = 600,
        multi_team_org_wall_clock_timeout: float = 30.0,
        store: Any | None = None,
        llm: Any | None = None,
        role_prompt_runner: Callable[[Task, str, dict[str, Any], str, bool], Awaitable[str | None]] | None = None,
        active_task_run_registry: ActiveTaskRunRegistry | None = None,
    ) -> None:
        self.org_engine = org_engine
        self.communication = communication
        self.approval_engine = approval_engine
        self.memory = memory
        self.llm = llm
        self.work_item_helper = CompanyRuntimeWorkItemHelper(org_engine, llm=llm)
        self.store = store
        self.execute_task = execute_task
        self.seat_executor = seat_executor
        self.save_task = save_task
        self.save_runtime_session = save_runtime_session
        self.progress_callback = progress_callback
        self.checkpoint_callback = checkpoint_callback
        self.agent_selector = agent_selector
        self.emit_runtime_event = emit_runtime_event
        self.on_kanban_changed = on_kanban_changed
        self.work_item_timeout = work_item_timeout
        self.multi_team_org_wall_clock_timeout = multi_team_org_wall_clock_timeout
        self.role_prompt_runner = role_prompt_runner
        self.active_task_run_registry = active_task_run_registry
        self._default_run_state = CompanyExecutorRunState()
        self._run_state_var: ContextVar[CompanyExecutorRunState | None] = ContextVar(
            f"company-executor-run-state:{id(self)}",
            default=None,
        )

        self._active_plan = None
        self._active_tasks = []
        # Kanban-push: runtime state transitions route through this hook
        # so the UI sees fresh snapshots mid-turn. Routed through
        # _notify_kanban_changed so the hook is still best-effort.
        if communication is not None and getattr(communication, "on_kanban_changed", None) is None:
            communication.on_kanban_changed = self._notify_kanban_changed
        # Dispatcher wake: signaled by `delegate_work` and by the runtime
        # after applying a `rework` verdict — any time a new TODO work
        # item becomes ready. The main loop in _execute_multi_team_org
        # waits on this Event so children are claimed+spawned without
        # waiting for the parent turn's gather batch to drain.
        self._dispatcher_wake = asyncio.Event()
        if communication is not None and getattr(communication, "on_work_items_created", None) is None:
            communication.on_work_items_created = self._signal_dispatcher_wake
        # D2: register the wake callback with the phase-transition hook
        # registry so signal_dispatcher_hook can ping us whenever a phase
        # change opens new dispatchable work — without this, the hook can
        # update task/session state but the dispatcher loop sleeps on its
        # asyncio.Event until the next periodic tick.
        from opc.layer2_organization.phase_hooks import register_dispatcher_wake
        register_dispatcher_wake(self._signal_dispatcher_wake)
        # Phase B: the old runtime reconciler / reenqueue hooks that
        # reached into in-memory runtime state on every phase transition
        # have been removed. The dispatcher's per-tick rehydrate pass
        # (see _execute_multi_team_org) is now the single convergence
        # point: on every iteration it unparks stale member sessions
        # and re-enqueues runnable work items read fresh from the DB.
        # Debounced kanban broadcaster (Fix C): per-batch push was
        # synchronous on the dispatch hot path; now we mark a dirty flag
        # and a single background coroutine coalesces + broadcasts.
        self._kanban_dirty = False
        self._kanban_broadcast_task = None
        # Trailing debounce: the broadcaster always fires once more after the
        # last dirty mark, so the final board state is never lost. Each fire
        # runs a full build_collab_sync (whole-project snapshot), which is
        # expensive on large projects — keep the window generous. UI-only;
        # the state machine never waits on this broadcast.
        self._kanban_debounce_sec: float = 1.5
        self._runtime_invariant_issue_keys = set()
        self.runtime = CompanyRuntime(
            org_engine=org_engine,
            communication=communication,
            store=store,
            save_runtime_session=save_runtime_session,
            emit_runtime_event=emit_runtime_event,
        )

    def _ensure_prompt_contract_on_work_item(
        self,
        work_item: DelegationWorkItem,
        *,
        task_metadata: dict[str, Any] | None = None,
        task_description: str = "",
    ) -> dict[str, Any]:
        metadata = dict(getattr(work_item, "metadata", {}) or {})
        if has_prompt_contract(metadata.get("prompt_contract")):
            return dict(metadata.get("prompt_contract", {}) or {})
        contract = prompt_contract_from_work_item(
            work_item,
            task_metadata=task_metadata,
            task_description=task_description,
        )
        work_item.metadata = {**metadata, "prompt_contract": contract}
        if str(contract.get("source", {}).get("kind", "") or "") == "prompt_contract_blocker":
            work_item.metadata["prompt_contract_blocker"] = True
        return contract






























































































































































































































































































