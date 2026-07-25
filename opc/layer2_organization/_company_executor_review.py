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

from opc.core.active_task_runs import (
    ActiveTaskRunAdmissionClosed,
    ActiveTaskRunRegistry,
)
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


class CompanyExecutorReviewMixin:
    """Mixin extracted from CompanyWorkItemExecutor."""

    @staticmethod
    def _work_item_gate_enforcement_enabled(task: Task) -> bool:
        runtime_policy = dict(task.metadata.get("policy") or task.metadata.get("runtime_policy", {}) or {})
        review_policy = dict(runtime_policy.get("review", {}) or {})
        return bool(review_policy.get("enable_work_item_gates", False))


    def _gate_harness_for_task(self, task: Task) -> GateHarness:
        runtime_policy = dict(task.metadata.get("policy") or task.metadata.get("runtime_policy", {}) or {})
        judge_runner = self._gate_harness_judge_runner if self.role_prompt_runner is not None else None
        return GateHarness(
            policy=dict(runtime_policy.get("gate_harness", {}) or {}),
            llm=None if judge_runner is not None else self.llm,
            org_engine=self.org_engine,
            judge_runner=judge_runner,
        )


    async def _run_role_prompt(
        self,
        *,
        source_task: Task,
        system_prompt: str,
        payload: dict[str, Any],
        prompt_kind: str,
        force_new_session: bool = True,
    ) -> str | None:
        if self.role_prompt_runner is None:
            return None
        try:
            return await self.role_prompt_runner(
                source_task,
                system_prompt,
                payload,
                prompt_kind,
                force_new_session,
            )
        except Exception as exc:
            logger.debug(f"Role prompt runner failed for `{prompt_kind}` on task `{source_task.id}`: {exc}")
            return None


    async def _gate_harness_judge_runner(
        self,
        packet: Any,
        system_prompt: str,
        source_task: Task,
    ) -> str:
        raw = await self._run_role_prompt(
            source_task=source_task,
            system_prompt=system_prompt,
            payload=packet.to_dict(),
            prompt_kind="gate_harness_judge",
            force_new_session=True,
        )
        if raw is None:
            raise RuntimeError("Role prompt runner unavailable for gate harness")
        return raw


    @staticmethod
    def _parse_role_prompt_json(raw: str) -> dict[str, Any] | None:
        text = CompanyExecutorReviewMixin._strip_markdown_fences(str(raw or ""))
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return dict(data) if isinstance(data, dict) else None


    @staticmethod
    def _review_status_for_level(review_level: str) -> TaskStatus:
        return (
            TaskStatus.AWAITING_MANAGER_REVIEW
            if str(review_level or "").strip().lower() == "manager"
            else TaskStatus.AWAITING_HUMAN
        )


    def _review_chain_for_task(self, task: Task) -> list[str]:
        direct_manager = str(task.metadata.get("manager_role_id", "") or "").strip()
        if direct_manager:
            chain = [direct_manager]
            if self.org_engine is not None:
                current = direct_manager
                seen = {self._role_id_for_task(task), direct_manager}
                while current:
                    agent = self.org_engine.get_agent(current)
                    parent = str(getattr(agent, "reports_to", "") or "").strip()
                    if not parent or parent == "owner" or parent in seen:
                        break
                    chain.append(parent)
                    seen.add(parent)
                    current = parent
            return chain
        if self.org_engine is None:
            return []
        role_id = self._role_id_for_task(task)
        if not role_id or not hasattr(self.org_engine, "get_chain_of_command"):
            return []
        try:
            chain = list(self.org_engine.get_chain_of_command(role_id))
        except Exception:
            return []
        return [
            str(getattr(agent, "role_id", "") or "").strip()
            for agent in chain[1:]
            if str(getattr(agent, "role_id", "") or "").strip()
        ]


    @staticmethod
    def _normalize_adaptive_metadata(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        metadata = dict(value)
        metadata["work_item_profile"] = dict(metadata.get("work_item_profile", {}) or {})
        metadata["role_profile"] = dict(metadata.get("role_profile", {}) or {})
        normalized_signals: list[dict[str, Any]] = []
        for item in list(metadata.get("signals", []) or []):
            if not isinstance(item, dict):
                continue
            normalized_signals.append(
                {
                    "name": str(item.get("name", "") or "").strip(),
                    "owner_role_id": str(item.get("owner_role_id", "") or "").strip(),
                    "required": bool(item.get("required", True)),
                    "strict": bool(item.get("strict", False)),
                    "satisfied": bool(item.get("satisfied", False)),
                    "evidence": [
                        str(entry).strip()
                        for entry in list(item.get("evidence", []) or [])
                        if str(entry).strip()
                    ],
                }
            )
        metadata["signals"] = normalized_signals
        metadata["hard_dependency_work_item_ids"] = [
            str(item).strip()
            for item in list(metadata.get("hard_dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        metadata["soft_dependency_work_item_ids"] = [
            str(item).strip()
            for item in list(metadata.get("soft_dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        metadata["emitted_signals"] = [
            str(item).strip()
            for item in list(metadata.get("emitted_signals", []) or [])
            if str(item).strip()
        ]
        metadata["required_artifacts"] = [
            str(item).strip()
            for item in list(metadata.get("required_artifacts", []) or metadata.get("work_item_profile", {}).get("required_artifacts", []) or [])
            if str(item).strip()
        ]
        return metadata


    @staticmethod
    def _adaptive_turn_kind(adaptive: dict[str, Any], *, fallback: str = "execute") -> str:
        work_item_profile = dict(adaptive.get("work_item_profile", {}) or {})
        return str(work_item_profile.get("turn_kind", "") or fallback).strip().lower() or fallback


    @staticmethod
    def _coordination_policy_for_work_item(metadata: dict[str, Any]) -> dict[str, Any]:
        runtime_policy = dict(metadata.get("policy") or metadata.get("runtime_policy", {}) or {})
        return dict(runtime_policy.get("coordination", {}) or {})


    @classmethod
    def _strict_gate_turn_kinds_for_metadata(cls, metadata: dict[str, Any]) -> set[str]:
        coordination = cls._coordination_policy_for_work_item(metadata)
        configured = {
            str(item).strip().lower()
            for item in list(coordination.get("strict_gate_turn_kinds", []) or [])
            if str(item).strip()
        }
        return configured or {"verify", "deliver"}


    @classmethod
    def _mixed_gate_turn_kinds_for_metadata(cls, metadata: dict[str, Any]) -> set[str]:
        coordination = cls._coordination_policy_for_work_item(metadata)
        configured = {
            str(item).strip().lower()
            for item in list(coordination.get("mixed_gate_turn_kinds", []) or [])
            if str(item).strip()
        }
        return configured or {"synthesize", "review", "integration"}


    @classmethod
    def _required_signals_satisfied(cls, adaptive: dict[str, Any]) -> bool:
        for signal in list(adaptive.get("signals", []) or []):
            if not isinstance(signal, dict):
                continue
            if bool(signal.get("required", True)) and not bool(signal.get("satisfied", False)):
                return False
        return True


    @staticmethod
    def _required_artifacts_present(adaptive: dict[str, Any], task: Task | None = None) -> bool:
        required_artifacts = [
            str(item).strip()
            for item in list(adaptive.get("required_artifacts", []) or [])
            if str(item).strip()
        ]
        if not required_artifacts:
            return True
        if task is None:
            return False
        available = {
            str(item).strip()
            for item in list(task.metadata.get("artifacts", []) or [])
            if str(item).strip()
        }
        output_metadata = CompanyExecutorReviewMixin._work_item_output_metadata_for_task(task)
        available.update(
            str(item.get("value", "") or "").strip()
            for item in list(output_metadata.get("work_item_artifact_index", []) or task.metadata.get("work_item_artifact_index", []) or [])
            if isinstance(item, dict) and str(item.get("value", "") or "").strip()
        )
        return all(any(required in candidate for candidate in available) for required in required_artifacts)


    @staticmethod
    def _review_work_item_id_for_work_item(work_item_id: str, attempt: int) -> str:
        """Per-attempt review work-item ID.

        Each AWAITING_MANAGER_REVIEW entry creates a fresh review work
        item with a new attempt number. Old attempts are immutable
        history. This eliminates the bug class where re-using a single
        deterministic ID caused stuck states once a previous attempt
        landed in a terminal phase (CANCELLED).
        """
        return review_work_item_id_for_attempt(work_item_id, attempt)


    @staticmethod
    def _safe_positive_int(value: Any) -> int:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0


    @classmethod
    def _auxiliary_attempt_number(
        cls,
        item: DelegationWorkItem,
        *,
        kind: str,
    ) -> int:
        """Read an auxiliary attempt from durable card identity.

        Parent counters are only caches: a process can crash after the card
        write and before the counter write.  The card's metadata, batch index,
        and deterministic id therefore all participate in recovery.
        """
        metadata = dict(getattr(item, "metadata", {}) or {})
        values = [
            cls._safe_positive_int(metadata.get(f"{kind}_attempt")),
            cls._safe_positive_int(getattr(item, "batch_index", 0)),
        ]
        item_id = str(getattr(item, "work_item_id", "") or "").strip()
        match = re.search(r"::v(\d+)$", item_id)
        if match:
            values.append(cls._safe_positive_int(match.group(1)))
        return max(values or [0])


    @classmethod
    def _targeting_auxiliary_items(
        cls,
        run_items: list[DelegationWorkItem],
        target_work_item_id: str,
        *,
        kind: str,
    ) -> list[DelegationWorkItem]:
        target_key = f"{kind}_target_work_item_id"
        target = str(target_work_item_id or "").strip()
        items = [
            item
            for item in list(run_items or [])
            if str((getattr(item, "metadata", {}) or {}).get(target_key, "") or "").strip()
            == target
        ]
        return sorted(
            items,
            key=lambda item: (
                cls._auxiliary_attempt_number(item, kind=kind),
                str(getattr(item, "created_at", "") or ""),
                str(getattr(item, "work_item_id", "") or ""),
            ),
        )


    @classmethod
    def _active_auxiliary_item(
        cls,
        run_items: list[DelegationWorkItem],
        target_work_item_id: str,
        *,
        kind: str,
    ) -> DelegationWorkItem | None:
        active = [
            item
            for item in cls._targeting_auxiliary_items(
                run_items,
                target_work_item_id,
                kind=kind,
            )
            if getattr(item, "phase", None) not in DONE_PHASES
        ]
        return active[-1] if active else None


    @classmethod
    def _next_auxiliary_attempt(
        cls,
        parent_item: DelegationWorkItem,
        run_items: list[DelegationWorkItem],
        *,
        kind: str,
    ) -> int:
        metadata = dict(getattr(parent_item, "metadata", {}) or {})
        attempts = [cls._safe_positive_int(metadata.get(f"{kind}_attempt_count"))]
        attempts.extend(
            cls._auxiliary_attempt_number(item, kind=kind)
            for item in cls._targeting_auxiliary_items(
                run_items,
                parent_item.work_item_id,
                kind=kind,
            )
        )
        return max(attempts or [0]) + 1


    async def _run_items_for_parent(
        self,
        parent_item: DelegationWorkItem,
        run_items: list[DelegationWorkItem] | None = None,
    ) -> list[DelegationWorkItem]:
        if run_items is not None:
            return list(run_items)
        if not self.store or not hasattr(self.store, "list_delegation_work_items"):
            return [parent_item]
        try:
            return await self.store.list_delegation_work_items(parent_item.run_id)
        except Exception:
            logger.opt(exception=True).debug("Failed to list auxiliary work items")
            return [parent_item]


    async def _insert_auxiliary_work_item_if_absent(
        self,
        item: DelegationWorkItem,
    ) -> tuple[DelegationWorkItem | None, bool]:
        """Create a deterministic auxiliary card without overwriting a race winner.

        Production stores provide an SQLite-level insert-if-absent operation.
        The read/save fallback keeps lightweight test stores compatible; it is
        intentionally not used as the production concurrency guarantee.
        """
        if not self.store:
            return None, False
        insert_if_absent = getattr(
            self.store,
            "insert_delegation_work_item_if_absent",
            None,
        )
        if callable(insert_if_absent):
            created = bool(await insert_if_absent(item))
            if created:
                return item, True
            try:
                return await self.store.get_delegation_work_item(item.work_item_id), False
            except Exception:
                return None, False

        try:
            existing = await self.store.get_delegation_work_item(item.work_item_id)
        except Exception:
            existing = None
        if existing is not None:
            return existing, False
        await self.store.save_delegation_work_item(item)
        return item, True


    @staticmethod
    def _work_item_output_metadata_for_task(task: Task) -> dict[str, Any]:
        """Return WorkItem-owned output metadata carried as runtime context."""
        context_outputs = dict((getattr(task, "context_snapshot", {}) or {}).get("work_item_owned_outputs", {}) or {})
        metadata = dict(getattr(task, "metadata", {}) or {})
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
            "open_questions",
            "assumptions",
            "decisions",
            "risks",
            "completion_report",
            "handoff_context",
            "context_preview",
        ):
            if key not in context_outputs and metadata.get(key) not in (None, "", [], {}):
                context_outputs[key] = copy.deepcopy(metadata.get(key))
        return context_outputs


    @staticmethod
    def _set_work_item_output_context(task: Task, updates: dict[str, Any]) -> None:
        """Carry WorkItem-owned outputs on the runtime context without persisting them as Task metadata."""
        clean_updates = {
            str(key): copy.deepcopy(value)
            for key, value in dict(updates or {}).items()
            if value not in (None, "", [], {})
        }
        if not clean_updates:
            return
        task.context_snapshot = dict(getattr(task, "context_snapshot", {}) or {})
        current = dict(task.context_snapshot.get("work_item_owned_outputs", {}) or {})
        current.update(clean_updates)
        task.context_snapshot["work_item_owned_outputs"] = current


    def _build_review_evidence(self, worker_task: Task, completion_report: str) -> dict[str, Any]:
        output_metadata = self._work_item_output_metadata_for_task(worker_task)
        artifact_manifest = self._normalize_work_item_artifact_index(
            output_metadata.get("work_item_artifact_index", [])
        )[:12]
        verification_status = dict(output_metadata.get("verification_status", {}) or {})
        verification_checks: list[dict[str, str]] = []
        for item in list(worker_task.metadata.get("automated_verification_results", []) or []):
            if not isinstance(item, dict):
                continue
            verification_checks.append(
                {
                    "command": str(item.get("command", "") or "").strip(),
                    "status": str(item.get("status", "") or "").strip(),
                    "summary": str(item.get("summary", "") or "").strip(),
                }
            )
        verification_evidence = dict(output_metadata.get("verification_evidence", {}) or {})
        for item in list(verification_evidence.get("checks", []) or []):
            if not isinstance(item, dict):
                continue
            verification_checks.append(
                {
                    "command": str(item.get("command", "") or "").strip(),
                    "status": str(item.get("status", "") or "").strip(),
                    "summary": str(item.get("summary", "") or item.get("raw_output", "") or "").strip(),
                }
            )
        for item in list(output_metadata.get("verification", []) or worker_task.metadata.get("verification", []) or []):
            if not isinstance(item, dict):
                continue
            verification_checks.append(
                {
                    "command": str(item.get("command", "") or "").strip(),
                    "status": str(item.get("status", "") or "").strip(),
                    "summary": str(item.get("summary", "") or "").strip(),
                }
            )
        key_commands: list[str] = []
        for entry in verification_checks:
            command = str(entry.get("command", "") or "").strip()
            if command and command not in key_commands:
                key_commands.append(command)
        output_paths: list[str] = []
        changed_areas: list[str] = []
        for artifact in artifact_manifest:
            if not isinstance(artifact, dict):
                continue
            value = str(artifact.get("value", "") or "").strip()
            if value and value not in output_paths:
                output_paths.append(value)
            label = str(artifact.get("label", "") or artifact.get("kind", "") or "").strip()
            if label and label not in changed_areas:
                changed_areas.append(label)
            if value and value not in changed_areas:
                changed_areas.append(value)
        for ref in list(worker_task.metadata.get("artifacts", []) or []):
            item = str(ref or "").strip()
            if not item:
                continue
            if item not in changed_areas:
                changed_areas.append(item)
            if ":" in item:
                _, _, maybe_path = item.partition(":")
                maybe_path = maybe_path.strip()
                if maybe_path and maybe_path not in output_paths:
                    output_paths.append(maybe_path)
        target_output_dir = str(worker_task.metadata.get("target_output_dir", "") or "").strip()
        if target_output_dir and target_output_dir not in output_paths:
            output_paths.append(target_output_dir)
        evidence: dict[str, Any] = {
            "completion_summary": str(completion_report or "").strip(),
            "artifact_manifest": artifact_manifest,
            "changed_areas": changed_areas[:12],
            "verification_results": {
                "status": verification_status,
                "checks": verification_checks[:10],
            },
            "key_commands": key_commands[:10],
            "output_paths": output_paths[:12],
            "open_risks": [
                str(item).strip()
                for item in list(output_metadata.get("risks", []) or worker_task.metadata.get("risks", []) or [])
                if str(item).strip()
            ][:10],
        }
        prior_evidence = dict(worker_task.metadata.get("review_evidence", {}) or {})
        manager_dispatch = dict(prior_evidence.get("manager_dispatch", {}) or {})
        if manager_dispatch:
            # The report turn refreshes evidence after the worker completion.
            # Keep the attempt-scoped dispatch note that caused this parent to
            # enter review; it is audit context, not a scheduling predicate.
            evidence["manager_dispatch"] = manager_dispatch
        return evidence


    def _build_report_source_snapshot(self, worker_task: Task) -> dict[str, Any]:
        summary = self._task_summary_for_map(worker_task)
        result_content = ""
        if isinstance(worker_task.result, dict):
            result_content = str(worker_task.result.get("content", "") or "").strip()
        elif worker_task.result is not None:
            result_content = str(getattr(worker_task.result, "content", "") or "").strip()
        return {
            "summary": summary,
            "result_content": result_content,
            "evidence": self._build_review_evidence(
                worker_task,
                summary or result_content,
            ),
        }


    def _review_evidence_from_work_item(
        self,
        item: DelegationWorkItem,
        completion_report: str,
    ) -> dict[str, Any]:
        """Build recovery-safe evidence using only the durable WorkItem.

        The execute DONE path normally persisted a richer evidence snapshot
        already.  On restart that snapshot is authoritative; direct
        WorkItem-owned artifact/verification fields provide a minimal fallback
        if the process died before a runtime Task could contribute extras.
        """
        metadata = dict(getattr(item, "metadata", {}) or {})
        evidence = copy.deepcopy(dict(metadata.get("review_evidence", {}) or {}))
        evidence["completion_summary"] = str(completion_report or "").strip()
        evidence.setdefault(
            "artifact_manifest",
            self._normalize_work_item_artifact_index(
                metadata.get("work_item_artifact_index", [])
            )[:12],
        )
        evidence.setdefault("changed_areas", [])
        evidence.setdefault(
            "verification_results",
            {
                "status": dict(metadata.get("verification_status", {}) or {}),
                "checks": list(
                    dict(metadata.get("verification_evidence", {}) or {}).get(
                        "checks", []
                    )
                    or []
                )[:10],
            },
        )
        evidence.setdefault("key_commands", [])
        evidence.setdefault("output_paths", [])
        evidence.setdefault(
            "open_risks",
            [
                str(value).strip()
                for value in list(metadata.get("risks", []) or [])
                if str(value).strip()
            ][:10],
        )
        return evidence


    def _report_source_snapshot_from_work_item(
        self,
        item: DelegationWorkItem,
    ) -> dict[str, Any]:
        metadata = dict(getattr(item, "metadata", {}) or {})
        summary = str(
            metadata.get("completion_report", "")
            or metadata.get("work_item_summary_for_downstream", "")
            or getattr(item, "deliverable_summary", "")
            or getattr(item, "summary", "")
            or ""
        ).strip()
        return {
            "summary": summary,
            "result_content": str(metadata.get("report_source_result_content", "") or "").strip(),
            "evidence": self._review_evidence_from_work_item(item, summary),
        }


    @staticmethod
    def _review_approval_blocker_reason(review_metadata: dict[str, Any]) -> str:
        """Return a concrete reason to reject an internally contradictory approval.

        This intentionally only catches high-confidence contradictions: failed
        verification, blocked/partial artifact status, or an approval with no
        artifacts while the report explicitly says evidence is missing.
        """
        evidence = dict(review_metadata.get("review_evidence", {}) or {})
        artifact_manifest = [
            dict(item)
            for item in list(evidence.get("artifact_manifest", []) or [])
            if isinstance(item, dict)
        ]
        output_paths = [
            str(item).strip()
            for item in list(evidence.get("output_paths", []) or [])
            if str(item).strip()
        ]
        verification_results = dict(evidence.get("verification_results", {}) or {})
        verification_status = dict(verification_results.get("status", {}) or {})
        verification_label = str(verification_status.get("label", "") or "").strip().lower()
        verification_summary = str(verification_status.get("summary", "") or "").strip()
        if verification_label in {"failed", "fail", "blocked", "missing", "missing_evidence"}:
            return (
                "Reviewer approved the work, but verification evidence is "
                f"`{verification_label}`"
                + (f": {verification_summary}" if verification_summary else ".")
            )

        blocked_statuses = {"blocked", "partial", "failed", "missing"}
        artifact_statuses = [
            str(item.get("status", "") or "").strip().lower()
            for item in artifact_manifest
            if str(item.get("status", "") or "").strip()
        ]
        if artifact_statuses and all(status in blocked_statuses for status in artifact_statuses):
            return "Reviewer approved the work, but all known artifacts are marked blocked, partial, failed, or missing."

        report_text = str(
            review_metadata.get("review_completion_report")
            or review_metadata.get("completion_report")
            or ""
        ).strip().lower()
        missing_evidence_phrases = (
            "no evidence",
            "without evidence",
            "no artifact",
            "no artifacts",
            "not verified",
            "cannot verify",
            "unable to verify",
            "status: blocked",
            '"status":"blocked"',
            '"status": "blocked"',
        )
        if not artifact_manifest and not output_paths and any(phrase in report_text for phrase in missing_evidence_phrases):
            return "Reviewer approved the work, but the completion report says evidence or artifacts are missing."

        return ""


    async def _ensure_review_work_item_for_work_item(
        self,
        work_item_id: str,
        *,
        worker_task: Task | None = None,
        completion_report: str = "",
        metadata_updates: dict[str, Any] | None = None,
        source_report_item: DelegationWorkItem | None = None,
        run_items: list[DelegationWorkItem] | None = None,
    ) -> DelegationWorkItem | None:
        """Ensure one active review card from durable WorkItem state.

        A runtime Task may enrich the initial evidence, but it is never the
        owner of lifecycle identity.  This lets restart reconciliation repair
        the chain without manufacturing a Task or trusting a lagging attempt
        counter on the parent.
        """
        if not self.store or not hasattr(self.store, "save_delegation_work_item"):
            return None
        target_work_item_id = str(work_item_id or "").strip()
        if not target_work_item_id:
            return None
        try:
            worker_item = await self.store.get_delegation_work_item(target_work_item_id)
        except Exception:
            worker_item = None
        if worker_item is None or worker_item.phase != Phase.AWAITING_MANAGER_REVIEW:
            return None
        worker_metadata = dict(worker_item.metadata or {})
        updates = dict(metadata_updates or {})
        task_metadata = dict(getattr(worker_task, "metadata", {}) or {})
        if source_report_item is not None:
            source_report_metadata = dict(source_report_item.metadata or {})
            if (
                str(source_report_metadata.get("report_target_work_item_id", "") or "").strip()
                != target_work_item_id
                or source_report_item.phase not in DONE_PHASES
                or str(source_report_metadata.get("report_card_outcome", "") or "").strip()
                != "applied"
            ):
                return None
        manager_role_id = str(
            updates.get("review_owner_role_id", "")
            or worker_metadata.get("review_owner_role_id", "")
            or worker_item.manager_role_id
            or ""
        ).strip()
        manager_seat_id = str(
            updates.get("review_owner_seat_id", "")
            or worker_metadata.get("review_owner_seat_id", "")
            or worker_item.manager_seat_id
            or ""
        ).strip()
        if not manager_role_id or not manager_seat_id:
            return None
        run_id = str(worker_item.run_id or "").strip()
        if not run_id:
            return None
        cell_id = str(worker_item.cell_id or "").strip()
        team_instance_id = str(worker_item.team_instance_id or "").strip()
        team_id = str(worker_item.team_id or worker_metadata.get("team_id", "") or "").strip()
        worker_role_id = str(worker_item.role_id or "").strip()
        worker_seat_id = str(worker_item.seat_id or "").strip()
        target_title = str(worker_item.title or target_work_item_id).strip()
        target_description = str(worker_item.summary or "").strip()
        target_prompt_contract = self._ensure_prompt_contract_on_work_item(
            worker_item,
            task_metadata=task_metadata or worker_metadata,
            task_description=str(getattr(worker_task, "description", "") or target_description).strip(),
        )
        if not has_prompt_contract(worker_metadata.get("prompt_contract")):
            try:
                await self.store.update_delegation_work_item(
                    target_work_item_id,
                    metadata_updates={"prompt_contract": target_prompt_contract},
                )
                worker_metadata = {**worker_metadata, "prompt_contract": target_prompt_contract}
            except Exception:
                logger.opt(exception=True).debug("Best-effort target prompt_contract snapshot update failed")
        review_prompt_contract = make_prompt_contract(
            task_brief=(
                "Review the completed child deliverable and decide whether to "
                "approve it or request rework."
            ),
            target_contract=target_prompt_contract,
            source={"kind": "review_auxiliary_work_item"},
        )
        all_run_items = await self._run_items_for_parent(worker_item, run_items)
        source_metadata = dict(getattr(source_report_item, "metadata", {}) or {})
        source_report_id = str(
            getattr(source_report_item, "work_item_id", "")
            or updates.get("review_source_report_work_item_id", "")
            or ""
        ).strip()
        durable_completion = str(
            completion_report
            or source_metadata.get("completion_report", "")
            or worker_metadata.get("completion_report", "")
            or ""
        ).strip()
        if isinstance(source_metadata.get("review_evidence"), dict):
            review_evidence = copy.deepcopy(source_metadata["review_evidence"])
        elif worker_task is not None:
            review_evidence = self._build_review_evidence(worker_task, durable_completion)
        else:
            review_evidence = self._review_evidence_from_work_item(
                worker_item,
                durable_completion,
            )
        # A report turn owns the handoff while it is active. Creating review
        # in parallel would let the reviewer consume an incomplete payload.
        if self._active_auxiliary_item(
            all_run_items,
            target_work_item_id,
            kind="report",
        ) is not None:
            return None
        existing_card = self._active_auxiliary_item(
            all_run_items,
            target_work_item_id,
            kind="review",
        )
        if existing_card is not None:
            existing_source_id = str(
                (existing_card.metadata or {}).get(
                    "review_source_report_work_item_id", ""
                )
                or ""
            ).strip()
            if source_report_id and existing_source_id != source_report_id:
                closed = await self._persist_terminal_review_card(
                    existing_card.work_item_id,
                    phase=Phase.CANCELLED,
                    outcome="superseded_by_newer_report",
                )
                if closed is None:
                    return None
                existing_card = None
            else:
                try:
                    return await self.store.update_delegation_work_item(
                        existing_card.work_item_id,
                        summary=(
                            "Review the completed child deliverable and decide whether to "
                            "approve it or request rework."
                        ),
                        metadata_updates={
                            "review_completion_report": durable_completion,
                            "review_evidence": review_evidence,
                            "review_source_report_work_item_id": source_report_id,
                            "review_target_prompt_contract": target_prompt_contract,
                            "prompt_contract": review_prompt_contract,
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("Best-effort in-flight review refresh failed")
                    return existing_card

        attempt_no = self._next_auxiliary_attempt(
            worker_item,
            all_run_items,
            kind="review",
        )
        review_work_item_id = review_work_item_id_for_attempt(target_work_item_id, attempt_no)
        worker_task_id = str(
            getattr(worker_task, "id", "")
            or worker_metadata.get("worker_task_id", "")
            or worker_metadata.get("claimed_task_id", "")
            or ""
        ).strip()
        session_scope_id = str(worker_metadata.get("session_scope_id", "") or "").strip()
        if worker_task is not None:
            session_scope_id = task_session_scope_id(worker_task) or session_scope_id
        review_metadata: dict[str, Any] = mark_work_item_projection(mark_work_item_runtime({
            "runtime_model": "multi_team_org",
            "session_scope_id": session_scope_id,
            "delegation_turn_kind": "review",
            "work_kind": "review",
            "team_id": team_id,
            "seat_id": manager_seat_id,
            "review_task": True,
            "review_execution_work_item": True,
            "review_attempt": attempt_no,
            "review_owner_role_id": manager_role_id,
            "review_owner_seat_id": manager_seat_id,
            "review_target_work_item_id": target_work_item_id,
            "review_source_report_work_item_id": source_report_id,
            "review_target_worker_task_id": worker_task_id,
            "review_target_worker_role_id": worker_role_id,
            "review_target_worker_seat_id": worker_seat_id,
            "review_completion_report": durable_completion,
            "review_target_title": target_title,
            "review_target_description": target_description,
            "review_target_prompt_contract": target_prompt_contract,
            "review_evidence": review_evidence,
            "current_turn_mode": "review_execute",
            "prompt_contract": review_prompt_contract,
            "hidden_from_company_kanban": True,
            "user_visible": False,
            "authoritative_output": False,
            "skip_work_item_sync": True,
            **{
                key: copy.deepcopy(updates[key])
                for key in (
                    "review_retry_hint",
                    "review_retry_of_attempt",
                    "review_retry_reason",
                    "max_review_reworks",
                )
                if key in updates
            },
        }, version=work_item_runtime_version(worker_metadata)),
            projection_id=review_work_item_id,
            turn_type="review",
        )
        review_work_item = DelegationWorkItem(
            work_item_id=review_work_item_id,
            run_id=run_id,
            cell_id=cell_id,
            team_instance_id=team_instance_id,
            team_id=team_id,
            role_id=manager_role_id,
            seat_id=manager_seat_id,
            parent_work_item_id=target_work_item_id,
            source_role_id=worker_role_id or None,
            source_seat_id=worker_seat_id or None,
            title=f"Review #{attempt_no}: {target_title}",
            summary=(
                "Review the completed child deliverable and decide whether to "
                "approve it or request rework."
            ),
            kind="review",
            projection_id=review_work_item_id,
            phase=Phase.READY,
            batch_id=f"review::{run_id}::{target_work_item_id}",
            batch_index=attempt_no,
            handoff_status="released",
            continuation_source="review_queue",
            manager_role_id=manager_role_id,
            manager_seat_id=manager_seat_id,
            metadata=review_metadata,
        )
        try:
            persisted_review, created = await self._insert_auxiliary_work_item_if_absent(
                review_work_item
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort review work-item create failed")
            return None
        if persisted_review is None:
            return None
        if not created and persisted_review.phase in DONE_PHASES:
            # The attempt number came from a stale dispatcher snapshot.  Do
            # not overwrite immutable history; the next reconcile pass will
            # observe it and choose the following deterministic attempt.
            return None
        # Cache only. Card identity remains authoritative if this write fails.
        try:
            await self.store.update_delegation_work_item(
                target_work_item_id,
                metadata_updates={"review_attempt_count": attempt_no},
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort review_attempt_count update failed")
        return persisted_review


    async def _ensure_report_work_item_for_work_item(
        self,
        work_item_id: str,
        *,
        worker_task: Task | None = None,
        run_items: list[DelegationWorkItem] | None = None,
    ) -> DelegationWorkItem | None:
        """Upsert a hidden report-generation work item that drives the
        worker's handoff turn before the reviewer is invoked.

        Two-turn worker→review handoff: instead of treating the worker's
        last execute-turn prose as the completion report (which produced
        unstable, sometimes self-contradicting reports), the runtime
        spawns a separate hidden card that resumes the same worker
        session under a dedicated report-generation prompt. The worker
        produces a structured (or narrative) handoff, and only then does
        the runtime spawn the review card.

        Mirrors ``_ensure_review_work_item_for_work_item`` for per-attempt
        idempotent spawn / refresh semantics. The assignee is the worker
        itself (NOT the manager), because the worker is the one writing
        the report on its own session context.
        """
        if not self.store or not hasattr(self.store, "save_delegation_work_item"):
            return None
        target_work_item_id = str(work_item_id or "").strip()
        if not target_work_item_id:
            return None
        try:
            worker_item = await self.store.get_delegation_work_item(target_work_item_id)
        except Exception:
            worker_item = None
        if worker_item is None or worker_item.phase != Phase.AWAITING_MANAGER_REVIEW:
            if worker_item is not None:
                await self._record_work_item_runtime_diagnostic(
                    code="report_parent_not_awaiting_review",
                    severity="info",
                    work_item=worker_item,
                    task=worker_task,
                    message="A WorkItem outside manager-review phase does not spawn a report card.",
                    details={"parent_phase": worker_item.phase.value},
                    warn=False,
                )
            return None
        worker_metadata = dict(worker_item.metadata or {})
        task_metadata = dict(getattr(worker_task, "metadata", {}) or {})
        run_id = str(worker_item.run_id or "").strip()
        if not run_id:
            return None
        cell_id = str(worker_item.cell_id or "").strip()
        team_instance_id = str(worker_item.team_instance_id or "").strip()
        team_id = str(worker_item.team_id or worker_metadata.get("team_id", "") or "").strip()
        worker_role_id = str(worker_item.role_id or "").strip()
        worker_seat_id = str(worker_item.seat_id or "").strip()
        if not worker_role_id or not worker_seat_id:
            return None
        manager_role_id = str(
            worker_metadata.get("review_owner_role_id", "")
            or worker_item.manager_role_id
            or ""
        ).strip()
        manager_seat_id = str(
            worker_metadata.get("review_owner_seat_id", "")
            or worker_item.manager_seat_id
            or ""
        ).strip()
        target_title = str(worker_item.title or target_work_item_id).strip()
        target_description = str(worker_item.summary or "").strip()
        target_prompt_contract = self._ensure_prompt_contract_on_work_item(
            worker_item,
            task_metadata=task_metadata or worker_metadata,
            task_description=str(getattr(worker_task, "description", "") or target_description).strip(),
        )
        if not has_prompt_contract(worker_metadata.get("prompt_contract")):
            try:
                await self.store.update_delegation_work_item(
                    target_work_item_id,
                    metadata_updates={"prompt_contract": target_prompt_contract},
                )
                worker_metadata = {**worker_metadata, "prompt_contract": target_prompt_contract}
            except Exception:
                logger.opt(exception=True).debug("Best-effort target prompt_contract update before report failed")

        report_prompt_contract = make_prompt_contract(
            task_brief=(
                "Write a structured handoff report for the deliverable you just "
                "completed. Do not do new execution work."
            ),
            target_contract=target_prompt_contract,
            source={"kind": "report_auxiliary_work_item"},
        )

        all_run_items = await self._run_items_for_parent(worker_item, run_items)
        report_source = (
            self._build_report_source_snapshot(worker_task)
            if worker_task is not None
            else self._report_source_snapshot_from_work_item(worker_item)
        )
        if self._active_auxiliary_item(
            all_run_items,
            target_work_item_id,
            kind="review",
        ) is not None:
            return None
        existing_card = self._active_auxiliary_item(
            all_run_items,
            target_work_item_id,
            kind="report",
        )
        if existing_card is not None:
            try:
                await self.store.update_delegation_work_item(
                    existing_card.work_item_id,
                    metadata_updates={
                        "report_target_prompt_contract": target_prompt_contract,
                        "prompt_contract": report_prompt_contract,
                        "report_source_summary": report_source["summary"],
                        "report_source_result_content": report_source["result_content"],
                        "report_source_evidence": report_source["evidence"],
                    },
                )
            except Exception:
                logger.opt(exception=True).debug("Best-effort in-flight report refresh failed")
            return existing_card

        attempt_no = self._next_auxiliary_attempt(
            worker_item,
            all_run_items,
            kind="report",
        )
        report_id = report_work_item_id_for_attempt(target_work_item_id, attempt_no)
        worker_task_id = str(
            getattr(worker_task, "id", "")
            or worker_metadata.get("worker_task_id", "")
            or worker_metadata.get("claimed_task_id", "")
            or ""
        ).strip()
        session_scope_id = str(worker_metadata.get("session_scope_id", "") or "").strip()
        if worker_task is not None:
            session_scope_id = task_session_scope_id(worker_task) or session_scope_id
        report_metadata: dict[str, Any] = mark_work_item_projection(mark_work_item_runtime({
            "runtime_model": "multi_team_org",
            "session_scope_id": session_scope_id,
            "delegation_turn_kind": "report",
            "work_kind": "report",
            "team_id": team_id,
            "seat_id": worker_seat_id,
            "report_execution_work_item": True,
            "report_attempt": attempt_no,
            "report_target_work_item_id": target_work_item_id,
            "report_target_worker_task_id": worker_task_id,
            "report_target_worker_role_id": worker_role_id,
            "report_target_worker_seat_id": worker_seat_id,
            "report_target_title": target_title,
            "report_target_description": target_description,
            "report_target_prompt_contract": target_prompt_contract,
            "report_source_summary": report_source["summary"],
            "report_source_result_content": report_source["result_content"],
            "report_source_evidence": report_source["evidence"],
            "manager_role_id": manager_role_id,
            "manager_seat_id": manager_seat_id,
            "current_turn_mode": "report_required",
            "prompt_contract": report_prompt_contract,
            "hidden_from_company_kanban": True,
            "user_visible": False,
            "authoritative_output": False,
            "skip_work_item_sync": True,
        }, version=work_item_runtime_version(worker_metadata)),
            projection_id=report_id,
            turn_type="report",
        )
        report_work_item = DelegationWorkItem(
            work_item_id=report_id,
            run_id=run_id,
            cell_id=cell_id,
            team_instance_id=team_instance_id,
            team_id=team_id,
            role_id=worker_role_id,
            seat_id=worker_seat_id,
            parent_work_item_id=target_work_item_id,
            source_role_id=worker_role_id or None,
            source_seat_id=worker_seat_id or None,
            title=f"Report #{attempt_no}: {target_title}",
            summary=(
                "Write a structured handoff report for the deliverable you just "
                "completed. The reviewer will independently verify your claims."
            ),
            kind="report",
            projection_id=report_id,
            phase=Phase.READY,
            batch_id=f"report::{run_id}::{target_work_item_id}",
            batch_index=attempt_no,
            handoff_status="released",
            continuation_source="report_queue",
            manager_role_id=manager_role_id,
            manager_seat_id=manager_seat_id,
            metadata=report_metadata,
        )
        try:
            persisted_report, created = await self._insert_auxiliary_work_item_if_absent(
                report_work_item
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort report work-item create failed")
            return None
        if persisted_report is None:
            return None
        if not created and persisted_report.phase in DONE_PHASES:
            # A concurrent writer already completed this deterministic
            # attempt.  Preserve it and let fresh reconciliation advance.
            return None
        # Cache only. Card identity remains authoritative if this write fails.
        try:
            await self.store.update_delegation_work_item(
                target_work_item_id,
                metadata_updates={"report_attempt_count": attempt_no},
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort report_attempt_count update failed")
        return persisted_report


    async def _release_auxiliary_claim_for_retry(
        self,
        work_item_id: str,
    ) -> None:
        """Release a failed terminal-write claim so the live loop can retry.

        Startup recovery already clears stale claims.  This is the equivalent
        same-process recovery path for the narrow window where an auxiliary
        turn completed but its durable terminal journal write failed.
        """
        if not self.store or not work_item_id:
            return
        try:
            await self.store.update_delegation_work_item(
                work_item_id,
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
                metadata_updates={
                    "claimed_by_role_session_id": "",
                    "claimed_task_id": "",
                },
            )
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to release auxiliary WorkItem claim for retry: "
                f"{work_item_id}"
            )


    @staticmethod
    def _build_review_resolution(
        *,
        review_work_item_id: str,
        target_work_item_id: str,
        target_phase: Phase,
        blocked_reason: str,
        metadata_updates: dict[str, Any],
        review_outcome: str,
        source_report_work_item_id: str,
    ) -> dict[str, Any]:
        """Build the immutable verdict journal persisted on a review card."""
        child_updates = copy.deepcopy(dict(metadata_updates or {}))
        # This stamp is committed atomically with the child phase projection.
        # It prevents a completed rework cycle from replaying an old verdict
        # when the same child later re-enters AWAITING_MANAGER_REVIEW.
        child_updates["review_resolution_applied_work_item_id"] = review_work_item_id
        return {
            "target_work_item_id": target_work_item_id,
            "target_phase": target_phase.value,
            "blocked_reason": str(blocked_reason or ""),
            "metadata_updates": child_updates,
            "review_outcome": str(review_outcome or "").strip(),
            "source_report_work_item_id": str(source_report_work_item_id or "").strip(),
            "decided_at": datetime.now().isoformat(),
        }


    async def _persist_terminal_review_resolution(
        self,
        *,
        review_work_item_id: str,
        target_work_item_id: str,
        target_phase: Phase,
        blocked_reason: str,
        metadata_updates: dict[str, Any],
        review_outcome: str,
        source_report_work_item_id: str,
    ) -> DelegationWorkItem | None:
        """Commit a review verdict before projecting it to the child."""
        resolution = self._build_review_resolution(
            review_work_item_id=review_work_item_id,
            target_work_item_id=target_work_item_id,
            target_phase=target_phase,
            blocked_reason=blocked_reason,
            metadata_updates=metadata_updates,
            review_outcome=review_outcome,
            source_report_work_item_id=source_report_work_item_id,
        )
        return await self._persist_terminal_review_card(
            review_work_item_id,
            phase=Phase.APPROVED,
            outcome=review_outcome,
            resolution=resolution,
        )


    async def _persist_terminal_review_card(
        self,
        review_work_item_id: str,
        *,
        phase: Phase,
        outcome: str,
        resolution: dict[str, Any] | None = None,
    ) -> DelegationWorkItem | None:
        """Persist a terminal review card or release its claim for retry."""
        metadata_updates: dict[str, Any] = {
            "claimed_by_role_session_id": "",
            "claimed_task_id": "",
            "review_work_item_outcome": outcome,
            "last_review_turn_finished_at": datetime.now().isoformat(),
        }
        if resolution is not None:
            metadata_updates["review_resolution"] = copy.deepcopy(resolution)
            metadata_updates["review_resolution_state"] = "pending"
        try:
            persisted = await self.store.update_delegation_work_item(
                review_work_item_id,
                phase=phase,
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
                metadata_updates=metadata_updates,
            )
        except Exception:
            logger.opt(exception=True).warning(
                "review_done: failed to persist terminal review card"
            )
            persisted = None
        if persisted is None:
            await self._release_auxiliary_claim_for_retry(review_work_item_id)
        return persisted


    async def _mark_review_resolution_stale(
        self,
        review_item: DelegationWorkItem,
        *,
        reason: str,
    ) -> None:
        """Retire a late verdict without mutating its target WorkItem."""
        try:
            await self.store.update_delegation_work_item(
                review_item.work_item_id,
                metadata_updates={
                    "review_resolution_state": "stale",
                    "review_resolution_stale_reason": str(reason or "").strip(),
                    "review_resolution_stale_at": datetime.now().isoformat(),
                    "review_work_item_outcome": (
                        "target_no_longer_awaiting_manager_review"
                        if reason == "target_phase_changed"
                        else "superseded_by_newer_report"
                    ),
                },
            )
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to mark stale review resolution: "
                f"{review_item.work_item_id}"
            )


    async def _current_review_resolution_target(
        self,
        *,
        target_work_item_id: str,
        source_report_work_item_id: str,
    ) -> tuple[DelegationWorkItem | None, str]:
        """Return the authoritative target and an empty reason when current."""
        try:
            target = await self.store.get_delegation_work_item(
                target_work_item_id
            )
        except Exception:
            target = None
        if target is None or target.phase != Phase.AWAITING_MANAGER_REVIEW:
            return target, "target_phase_changed"
        run_items = await self._run_items_for_parent(target)
        applied_reports = [
            report
            for report in self._targeting_auxiliary_items(
                run_items,
                target_work_item_id,
                kind="report",
            )
            if report.phase in DONE_PHASES
            and str(
                (report.metadata or {}).get("report_card_outcome", "") or ""
            ).strip()
            == "applied"
        ]
        latest_report = applied_reports[-1] if applied_reports else None
        if latest_report is None and source_report_work_item_id:
            return target, "source_report_missing"
        if latest_report is not None and (
            source_report_work_item_id != latest_report.work_item_id
        ):
            return target, "source_report_superseded"
        return target, ""


    async def _apply_review_resolution(
        self,
        review_item: DelegationWorkItem,
        target_item: DelegationWorkItem,
    ) -> DelegationWorkItem | None:
        """Idempotently project a durable terminal review onto its child."""
        if review_item.phase not in DONE_PHASES:
            return None
        review_metadata = dict(review_item.metadata or {})
        if str(review_metadata.get("review_resolution_state", "") or "").strip() == "stale":
            return None
        resolution = dict(review_metadata.get("review_resolution", {}) or {})
        target_work_item_id = str(
            resolution.get("target_work_item_id", "") or ""
        ).strip()
        if not target_work_item_id or target_work_item_id != target_item.work_item_id:
            return None
        resolution_source_id = str(
            resolution.get("source_report_work_item_id", "") or ""
        ).strip()
        review_source_id = str(
            review_metadata.get("review_source_report_work_item_id", "") or ""
        ).strip()
        if resolution_source_id and review_source_id and (
            resolution_source_id != review_source_id
        ):
            await self._mark_review_resolution_stale(
                review_item,
                reason="source_report_superseded",
            )
            return None
        source_report_work_item_id = resolution_source_id or review_source_id
        authoritative_target, stale_reason = (
            await self._current_review_resolution_target(
                target_work_item_id=target_work_item_id,
                source_report_work_item_id=source_report_work_item_id,
            )
        )
        if stale_reason:
            await self._mark_review_resolution_stale(
                review_item,
                reason=stale_reason,
            )
            return None
        if authoritative_target is None:
            return None
        if str(
            (authoritative_target.metadata or {}).get(
                "review_resolution_applied_work_item_id", ""
            )
            or ""
        ).strip() == review_item.work_item_id:
            return authoritative_target
        try:
            target_phase = Phase(str(resolution.get("target_phase", "") or ""))
        except ValueError:
            return None
        if target_phase not in {
            Phase.APPROVED,
            Phase.READY_FOR_REWORK,
            Phase.AWAITING_HUMAN,
        }:
            return None
        metadata_updates = resolution.get("metadata_updates", {})
        if not isinstance(metadata_updates, dict):
            return None
        metadata_updates = copy.deepcopy(metadata_updates)
        metadata_updates["review_resolution_applied_work_item_id"] = (
            review_item.work_item_id
        )
        atomic_apply = getattr(
            self.store,
            "apply_delegation_review_resolution",
            None,
        )
        if callable(atomic_apply):
            applied = await atomic_apply(
                target_work_item_id,
                source_report_work_item_id=source_report_work_item_id,
                target_phase=target_phase,
                blocked_reason=str(resolution.get("blocked_reason", "") or ""),
                metadata_updates=metadata_updates,
            )
        else:
            applied = await self.store.update_delegation_work_item(
                target_work_item_id,
                phase=target_phase,
                blocked_reason=str(resolution.get("blocked_reason", "") or ""),
                metadata_updates=metadata_updates,
            )
        if applied is None:
            _target, stale_reason = await self._current_review_resolution_target(
                target_work_item_id=target_work_item_id,
                source_report_work_item_id=source_report_work_item_id,
            )
            if stale_reason:
                await self._mark_review_resolution_stale(
                    review_item,
                    reason=stale_reason,
                )
            return None
        try:
            await self.store.update_delegation_work_item(
                review_item.work_item_id,
                metadata_updates={"review_resolution_state": "applied"},
            )
        except Exception:
            logger.opt(exception=True).debug(
                "Best-effort review resolution applied marker failed"
            )
        return applied


    async def _finalize_review_work_item(self, review_task: Task) -> None:
        """Apply the review verdict to the child work item and close the
        hidden review card.

        The runtime is intentionally minimal here:

        * If the verdict has a parseable ``approve`` / ``reject`` label,
          apply it mechanically unless the approve is internally
          contradictory with explicit blocked/missing evidence. Reject
          cycles as machine-readable rework; non-final review never escalates
          to human review.
        * If the verdict cannot be parsed at all (no extractable label),
          retry the reviewer with a parse-failure hint. After
          ``MAX_VERDICT_PARSE_RETRIES``, close the review as done/approved
          with audit metadata instead of sending the worker back for rework.

        The runtime does NOT inspect issue counts, summary length, or prose
        quality, and does NOT silently flip reject to approve. It only blocks
        high-confidence contradictory approvals where evidence says blocked,
        failed, or missing.

        Durability contract: persist the terminal review card and complete
        resolution first, then project that resolution to the child. The
        parent's atomic applied stamp makes reconcile replay safe across both
        process crashes and later rework cycles.
        """
        if not self.store:
            await self._notify_kanban_changed()
            return
        review_work_item_id = linked_work_item_id_for_task(review_task)
        review_item = None
        if review_work_item_id and hasattr(self.store, "get_delegation_work_item"):
            try:
                review_item = await self.store.get_delegation_work_item(
                    review_work_item_id
                )
            except Exception:
                review_item = None
        review_metadata = {
            **dict(getattr(review_item, "metadata", {}) or {}),
            **dict(review_task.metadata or {}),
            **self._work_item_output_metadata_for_task(review_task),
        }
        target_work_item_id = str(review_metadata.get("review_target_work_item_id", "") or "").strip()
        if not review_work_item_id or not target_work_item_id:
            await self._notify_kanban_changed()
            return
        child_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                child_item = await self.store.get_delegation_work_item(target_work_item_id)
            except Exception:
                child_item = None
        child_phase = child_item.phase if child_item is not None else None
        if child_phase != Phase.AWAITING_MANAGER_REVIEW:
            # A manager verdict is valid only for the exact passive phase it
            # was created to consume. In particular, a late manager turn must
            # never jump over an AWAITING_HUMAN decision.
            await self._persist_terminal_review_card(
                review_work_item_id,
                phase=Phase.APPROVED,
                outcome="target_no_longer_awaiting_manager_review",
            )
            await self._notify_kanban_changed()
            return
        verdict = self._normalize_review_verdict(review_metadata.get("structured_review_verdict"))
        verdict_label = str(verdict.get("label", "") or "").strip().lower() if verdict else ""
        approval_blocker_reason = (
            self._review_approval_blocker_reason(review_metadata)
            if verdict_label == "approve"
            else ""
        )
        if approval_blocker_reason:
            verdict = {
                "label": "reject",
                "summary": "Approval withheld because the report or evidence is internally contradictory.",
                "blocking_issues": [approval_blocker_reason],
                "followups": [],
            }
            verdict_label = "reject"
            review_task.metadata = dict(review_task.metadata or {})
            review_task.metadata["structured_review_verdict"] = verdict
            review_metadata["structured_review_verdict"] = verdict

        # Verdict-parse retry: if the reviewer didn't emit a parseable
        # approve/reject label, tell the reviewer and give them another
        # review turn. Beyond MAX_VERDICT_PARSE_RETRIES, close the review
        # without reworking the child; parse failures are reviewer-side
        # output failures, not worker deliverable failures.
        if verdict_label not in {"approve", "reject"}:
            prior_parse_retries = await self._durable_review_parse_retry_count(
                child_item,
            )
            if prior_parse_retries < MAX_VERDICT_PARSE_RETRIES:
                retry_spawned = await self._retry_verdict_parse_failed(
                    review_task=review_task,
                    review_work_item_id=review_work_item_id,
                    target_work_item_id=target_work_item_id,
                    new_retry_count=prior_parse_retries + 1,
                )
                if retry_spawned:
                    await self._notify_kanban_changed()
                    return
                logger.warning(
                    "verdict-parse-retry spawn deferred; keeping child in review "
                    f"child={target_work_item_id}"
                )
                # The prior review card is either still retryable or already
                # a terminal parse-failure journal. Reconciliation can create
                # the next review; a transient card write must never approve
                # the worker output.
                await self._notify_kanban_changed()
                return
            # Retry budget is explicitly exhausted. Do not send the child
            # back to the worker for a reviewer formatting problem.
            auto_done_reason = (
                f"Reviewer produced an unparseable verdict {prior_parse_retries + 1} time(s); "
                "runtime is closing the review as done instead of requesting worker rework."
            )
            auto_close_verdict = {
                "label": "approve",
                "summary": "Auto-closed because reviewer verdict was unparseable after retry budget.",
                "blocking_issues": [],
                "followups": [
                    "Inspect reviewer output formatting; the worker was not reworked for this reviewer-side failure."
                ],
            }
            child_metadata_updates: dict[str, Any] = {
                "reviewed_at": datetime.now().isoformat(),
                "review_owner_role_id": str(child_item.manager_role_id or "").strip(),
                "review_owner_seat_id": str(child_item.manager_seat_id or "").strip(),
                "review_verdict_parse_retry_count": prior_parse_retries + 1,
                "review_feedback_updated_at": datetime.now().isoformat(),
                "review_verdict_parse_failed_auto_done": True,
                "review_parse_failure_feedback": auto_done_reason,
                "rework_feedback": "",
                "structured_review_verdict": auto_close_verdict,
            }
            source_report_work_item_id = str(
                dict(getattr(review_item, "metadata", {}) or {}).get(
                    "review_source_report_work_item_id", ""
                )
                or review_metadata.get("review_source_report_work_item_id", "")
                or ""
            ).strip()
            persisted_review = await self._persist_terminal_review_resolution(
                review_work_item_id=review_work_item_id,
                target_work_item_id=target_work_item_id,
                target_phase=Phase.APPROVED,
                blocked_reason="",
                metadata_updates=child_metadata_updates,
                review_outcome="verdict_parse_failed_auto_done",
                source_report_work_item_id=source_report_work_item_id,
            )
            if persisted_review is None:
                await self._notify_kanban_changed()
                return
            try:
                applied_child = await self._apply_review_resolution(
                    persisted_review,
                    child_item,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "_finalize_review_work_item: failed to project auto-close resolution"
                )
                applied_child = None
            if applied_child is None:
                # The terminal review is the recovery point; reconcile will
                # project it without rerunning the reviewer.
                await self._notify_kanban_changed()
                return
            child_item = applied_child
            child_phase = applied_child.phase
            await self._ack_lifecycle_inbox_for_review(
                review_task=review_task,
                review_work_item_id=review_work_item_id,
                target_work_item_id=target_work_item_id,
                child_item=child_item,
            )
            await self._notify_kanban_changed()
            return

        decision = "approve" if verdict_label == "approve" else "rework"
        next_phase = Phase.APPROVED if decision == "approve" else Phase.READY_FOR_REWORK
        review_outcome = decision
        persisted_review: DelegationWorkItem | None = None

        # Apply the verdict to the child work item if it is still
        # awaiting review. If the child already moved on, skip the
        # mutation but still finalize the hidden review item.
        if child_phase == Phase.AWAITING_MANAGER_REVIEW:
            feedback = self._review_feedback_with_fallback(review_task)
            prior_feedback_version = self._review_feedback_version(
                dict(getattr(child_item, "metadata", {}) or {})
            )
            child_metadata_updates = {
                "reviewed_at": datetime.now().isoformat(),
                "review_owner_role_id": str(child_item.manager_role_id or "").strip(),
                "review_owner_seat_id": str(child_item.manager_seat_id or "").strip(),
                "rework_feedback": "" if next_phase == Phase.APPROVED else feedback,
                "structured_review_verdict": verdict or {},
            }
            escalation_reason: str | None = None
            if next_phase == Phase.READY_FOR_REWORK:
                prior_rework_count = int(
                    dict(getattr(child_item, "metadata", {}) or {}).get(
                        "review_rework_count", 0
                    ) or 0
                )
                # Configurable cap on rework cycles. Default 5; either
                # the review task or the child's metadata may override.
                max_review_reworks = self._resolve_max_review_reworks(
                    review_task=review_task, child_item=child_item
                )
                if prior_rework_count >= max_review_reworks:
                    auto_done_reason = (
                        f"Rework count ({prior_rework_count}) reached the configured "
                        f"cap of {max_review_reworks}; marking the work item done instead "
                        f"of requesting another rework. Latest reviewer feedback:\n{feedback}"
                    ).strip()
                    next_phase = Phase.APPROVED
                    review_outcome = "auto_done_rework_cap"
                    child_metadata_updates["rework_feedback"] = ""
                    child_metadata_updates["review_rework_cap_reached_auto_done"] = True
                    child_metadata_updates["review_rework_cap"] = max_review_reworks
                    child_metadata_updates["review_rework_count_at_auto_done"] = prior_rework_count
                    child_metadata_updates["review_rework_cap_feedback"] = auto_done_reason
                else:
                    child_metadata_updates["review_rework_count"] = prior_rework_count + 1
                    child_metadata_updates["review_feedback_version"] = prior_feedback_version + 1
                    child_metadata_updates["review_feedback_updated_at"] = datetime.now().isoformat()
            elif next_phase == Phase.APPROVED:
                child_metadata_updates["review_rework_count"] = 0
            blocked_reason = (
                ""
                if next_phase == Phase.APPROVED
                else str(escalation_reason or "")
            )
            source_report_work_item_id = str(
                dict(getattr(review_item, "metadata", {}) or {}).get(
                    "review_source_report_work_item_id", ""
                )
                or review_metadata.get("review_source_report_work_item_id", "")
                or ""
            ).strip()
            persisted_review = await self._persist_terminal_review_resolution(
                review_work_item_id=review_work_item_id,
                target_work_item_id=target_work_item_id,
                target_phase=next_phase,
                blocked_reason=blocked_reason,
                metadata_updates=child_metadata_updates,
                review_outcome=review_outcome,
                source_report_work_item_id=source_report_work_item_id,
            )
            if persisted_review is None:
                await self._notify_kanban_changed()
                return
            try:
                applied_child = await self._apply_review_resolution(
                    persisted_review,
                    child_item,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "_finalize_review_work_item: failed to project review resolution"
                )
                applied_child = None
            if applied_child is None:
                # The verdict is already durable. A later reconcile pass will
                # replay this exact resolution onto the still-waiting child.
                await self._notify_kanban_changed()
                return
            child_item = applied_child
            child_phase = applied_child.phase
            if (
                next_phase == Phase.AWAITING_HUMAN
                and child_phase == Phase.AWAITING_HUMAN
                and feedback
            ):
                try:
                    target_task = await self._load_review_target_task(
                        review_task=review_task,
                        child_item=child_item,
                    )
                    if target_task is not None:
                        target_task = copy.deepcopy(target_task)
                        target_task.metadata = dict(target_task.metadata or {})
                        target_task.metadata.update({
                            "rework_feedback": feedback,
                            "review_owner_role_id": str(child_item.manager_role_id or "").strip(),
                            "review_owner_seat_id": str(child_item.manager_seat_id or "").strip(),
                            "review_feedback_version": int(
                                child_metadata_updates.get("review_feedback_version", prior_feedback_version) or 0
                            ),
                        })
                        if callable(getattr(self, "save_task", None)):
                            await self.save_task(target_task)
                        await self._save_review_rework_human_checkpoint(
                            target_task,
                            feedback=feedback,
                            review_owner_role_id=str(child_item.manager_role_id or "").strip(),
                            review_feedback_version=int(
                                child_metadata_updates.get("review_feedback_version", prior_feedback_version) or 0
                            ),
                            escalation_reason=escalation_reason or "",
                        )
                except Exception:
                    logger.opt(exception=True).warning(
                        "_finalize_review_work_item: failed to persist human-intervention checkpoint"
                    )
        if persisted_review is None:
            # The target moved before this reviewer finished. Close the
            # auxiliary card, but never apply a stale verdict to that newer
            # target state.
            persisted_review = await self._persist_terminal_review_card(
                review_work_item_id,
                phase=Phase.APPROVED,
                outcome="target_no_longer_awaiting_review",
            )
            if persisted_review is None:
                await self._notify_kanban_changed()
                return
        await self._ack_lifecycle_inbox_for_review(
            review_task=review_task,
            review_work_item_id=review_work_item_id,
            target_work_item_id=target_work_item_id,
            child_item=child_item,
        )
        # Dispatcher wake: a rework decision reopens the child on the
        # worker seat; signal the main loop so the rework turn starts
        # without waiting for the next gather batch.
        if next_phase == Phase.READY_FOR_REWORK:
            try:
                self._signal_dispatcher_wake()
            except Exception:
                logger.opt(exception=True).debug("_signal_dispatcher_wake failed")
        if child_phase == Phase.APPROVED:
            await self._refresh_delegation_dependents(review_task)
        await self._notify_kanban_changed()


    async def _ack_lifecycle_inbox_for_review(
        self,
        *,
        review_task: Task,
        review_work_item_id: str,
        target_work_item_id: str,
        child_item: Any | None = None,
    ) -> None:
        """Archive protocol mail that was consumed by a review-card verdict."""
        if not self.communication:
            return
        service_factory = getattr(self.communication, "_collaboration_service", None)
        if not callable(service_factory):
            return
        role_id = str(
            review_task.assigned_to
            or (review_task.metadata or {}).get("work_item_role_id", "")
            or ""
        ).strip()
        if not role_id:
            return
        review_metadata = dict(review_task.metadata or {})
        child_metadata = dict(getattr(child_item, "metadata", {}) or {})
        task_ids = {
            str(review_task.id or "").strip(),
            str(review_metadata.get("review_target_worker_task_id", "") or "").strip(),
            str(review_metadata.get("report_target_worker_task_id", "") or "").strip(),
            str(review_metadata.get("task_id", "") or "").strip(),
        }
        work_item_ids = {
            str(target_work_item_id or "").strip(),
            str(review_work_item_id or "").strip(),
            str(getattr(child_item, "work_item_id", "") or "").strip(),
            str(getattr(child_item, "parent_work_item_id", "") or "").strip(),
            str(review_metadata.get("review_target_work_item_id", "") or "").strip(),
            str(review_metadata.get("report_target_work_item_id", "") or "").strip(),
        }
        cleanup_items_by_id: dict[str, Any] = {}
        root_work_item_ids = {item for item in work_item_ids if item}
        if child_item is not None:
            child_id = str(getattr(child_item, "work_item_id", "") or "").strip()
            if child_id:
                cleanup_items_by_id[child_id] = child_item

        def _phase_value(item: Any) -> str:
            phase = getattr(item, "phase", "")
            return str(getattr(phase, "value", phase) or "").strip()

        def _cleanup_phase_eligible(item: Any) -> bool:
            eligible = {phase.value for phase in DONE_PHASES}
            eligible.add(Phase.AWAITING_HUMAN.value)
            return _phase_value(item) in eligible

        if self.store and hasattr(self.store, "list_delegation_work_items"):
            run_id = str(
                getattr(child_item, "run_id", "")
                or review_metadata.get("delegation_run_id", "")
                or review_metadata.get("run_id", "")
                or ""
            ).strip()
            if run_id:
                try:
                    all_items = await self.store.list_delegation_work_items(run_id)
                except Exception:
                    all_items = []
                by_id = {
                    str(getattr(item, "work_item_id", "") or "").strip(): item
                    for item in list(all_items or [])
                    if str(getattr(item, "work_item_id", "") or "").strip()
                }
                by_parent: dict[str, list[Any]] = {}
                for item in list(all_items or []):
                    parent_id = str(getattr(item, "parent_work_item_id", "") or "").strip()
                    if parent_id:
                        by_parent.setdefault(parent_id, []).append(item)
                for root_id in list(root_work_item_ids):
                    item = by_id.get(root_id)
                    if item is not None:
                        cleanup_items_by_id.setdefault(root_id, item)
                stack = list(root_work_item_ids)
                visited: set[str] = set()
                while stack:
                    current_id = stack.pop()
                    if not current_id or current_id in visited:
                        continue
                    visited.add(current_id)
                    for descendant in by_parent.get(current_id, []):
                        descendant_id = str(getattr(descendant, "work_item_id", "") or "").strip()
                        if not descendant_id:
                            continue
                        stack.append(descendant_id)
                        if _cleanup_phase_eligible(descendant):
                            cleanup_items_by_id.setdefault(descendant_id, descendant)

        def _safe_attempt(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        attempt_limits = [
            _safe_attempt(child_metadata.get("report_attempt_count", 0)),
            _safe_attempt(child_metadata.get("review_attempt_count", 0)),
            _safe_attempt(review_metadata.get("report_attempt", 0)),
            _safe_attempt(review_metadata.get("review_attempt", 0)),
            1,
        ]
        for cleanup_item in cleanup_items_by_id.values():
            item_id = str(getattr(cleanup_item, "work_item_id", "") or "").strip()
            if item_id:
                work_item_ids.add(item_id)
            parent_id = str(getattr(cleanup_item, "parent_work_item_id", "") or "").strip()
            if parent_id:
                work_item_ids.add(parent_id)
            item_metadata = dict(getattr(cleanup_item, "metadata", {}) or {})
            attempt_limits.extend([
                _safe_attempt(item_metadata.get("report_attempt_count", 0)),
                _safe_attempt(item_metadata.get("review_attempt_count", 0)),
                _safe_attempt(item_metadata.get("report_attempt", 0)),
                _safe_attempt(item_metadata.get("review_attempt", 0)),
            ])
            for key in (
                "claimed_task_id",
                "task_id",
                "completion_task_id",
                "review_target_worker_task_id",
                "report_target_worker_task_id",
            ):
                value = str(item_metadata.get(key, "") or "").strip()
                if value:
                    task_ids.add(value)
        for base_id in list(work_item_ids):
            if not base_id:
                continue
            max_attempt = max(attempt_limits or [1])
            for attempt in range(1, max_attempt + 1):
                work_item_ids.add(report_work_item_id_for_attempt(base_id, attempt))
                work_item_ids.add(review_work_item_id_for_attempt(base_id, attempt))
        projection_ids = {
            projection_id_for_task(review_task),
            str(review_metadata.get("work_item_projection_id", "") or "").strip(),
            str(review_metadata.get("review_target_projection_id", "") or "").strip(),
            str(getattr(child_item, "projection_id", "") or "").strip(),
            projection_id_for_work_item(child_item) if child_item is not None else "",
        }
        for cleanup_item in cleanup_items_by_id.values():
            projection_ids.add(str(getattr(cleanup_item, "projection_id", "") or "").strip())
            projection_ids.add(projection_id_for_work_item(cleanup_item))
        try:
            service = service_factory()
            ack_by_refs = getattr(service, "ack_inbox_messages_by_refs", None)
            if not callable(ack_by_refs):
                return
            await ack_by_refs(
                CollaborationContext.from_task(review_task, role_id=role_id),
                agent_id=role_id,
                work_item_ids=sorted(item for item in work_item_ids if item),
                projection_ids=sorted(item for item in projection_ids if item),
                task_ids=sorted(item for item in task_ids if item),
                semantic_types=["approval_request", "blocker", "completion", "status_digest"],
                task=review_task,
            )
        except Exception:
            logger.opt(exception=True).debug("Best-effort lifecycle inbox cleanup after review failed")


    @staticmethod
    def _resolve_max_review_reworks(
        *,
        review_task: Task,
        child_item: Any,
    ) -> int:
        """Return the configured max-rework cap for this work item.

        Resolution order:
        1. Review task metadata: ``max_review_reworks`` (per-attempt
           override, e.g. set by a recovery/escalation flow).
        2. Child work-item metadata: ``max_review_reworks`` (per-task
           override, e.g. set by a manager when delegating).
        3. Module default ``DEFAULT_MAX_REVIEW_REWORKS`` (5).
        """
        for source in (
            dict(review_task.metadata or {}),
            dict(getattr(child_item, "metadata", {}) or {}),
        ):
            raw = source.get("max_review_reworks")
            if raw is None:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return DEFAULT_MAX_REVIEW_REWORKS


    async def _retry_verdict_parse_failed(
        self,
        *,
        review_task: Task,
        review_work_item_id: str,
        target_work_item_id: str,
        new_retry_count: int,
    ) -> bool:
        """Spawn Review #N+1 because the previous reviewer turn produced a
        verdict the runtime could not parse into approve/reject.

        Distinct from worker rework: this is a reviewer-side output
        recovery, NOT a re-evaluation of the deliverable. Counts against
        ``review_verdict_parse_retry_count`` (independent budget from
        ``review_rework_count``).
        """
        if not self.store or not hasattr(self.store, "update_delegation_work_item"):
            return False
        review_metadata = dict(review_task.metadata or {})
        prior_review_item = None
        target_item = None
        if hasattr(self.store, "get_delegation_work_item"):
            try:
                prior_review_item = await self.store.get_delegation_work_item(review_work_item_id)
            except Exception:
                prior_review_item = None
            try:
                target_item = await self.store.get_delegation_work_item(target_work_item_id)
            except Exception:
                target_item = None
        prior_review_metadata = dict(getattr(prior_review_item, "metadata", {}) or {})
        target_metadata = dict(getattr(target_item, "metadata", {}) or {})
        review_owner_role_id = str(
            prior_review_metadata.get("review_owner_role_id", "")
            or review_metadata.get("review_owner_role_id", "")
            or target_metadata.get("review_owner_role_id", "")
            or getattr(target_item, "manager_role_id", "")
            or ""
        ).strip()
        review_owner_seat_id = str(
            prior_review_metadata.get("review_owner_seat_id", "")
            or review_metadata.get("review_owner_seat_id", "")
            or target_metadata.get("review_owner_seat_id", "")
            or getattr(target_item, "manager_seat_id", "")
            or ""
        ).strip()
        worker_task_id = str(
            review_metadata.get("review_target_worker_task_id", "") or ""
        ).strip()
        worker_task = None
        if worker_task_id:
            try:
                worker_task = await self.store.get_task(worker_task_id)
            except Exception:
                worker_task = None
        retry_worker_task = copy.deepcopy(worker_task) if worker_task is not None else None
        if retry_worker_task is not None:
            retry_worker_task.metadata = dict(getattr(worker_task, "metadata", {}) or {})
            if target_item is not None:
                retry_worker_task.title = str(getattr(target_item, "title", "") or retry_worker_task.title or "")
                retry_worker_task.description = str(
                    getattr(target_item, "summary", "") or retry_worker_task.description or ""
                )
                retry_worker_task.assigned_to = str(
                    getattr(target_item, "role_id", "") or retry_worker_task.assigned_to or ""
                ).strip()
                retry_worker_task.metadata.update(build_work_item_owner_execution_copy(target_item))
            if review_owner_role_id:
                retry_worker_task.metadata["manager_role_id"] = review_owner_role_id
                retry_worker_task.metadata["review_owner_role_id"] = review_owner_role_id
            if review_owner_seat_id:
                retry_worker_task.metadata["manager_seat_id"] = review_owner_seat_id
                retry_worker_task.metadata["review_owner_seat_id"] = review_owner_seat_id

        source_report_item = None
        source_report_id = str(
            prior_review_metadata.get("review_source_report_work_item_id", "") or ""
        ).strip()
        if source_report_id:
            try:
                source_report_item = await self.store.get_delegation_work_item(source_report_id)
            except Exception:
                source_report_item = None

        persisted_prior = await self._persist_terminal_review_card(
            review_work_item_id,
            phase=Phase.CANCELLED,
            outcome="verdict_parse_failed",
        )
        if persisted_prior is None:
            return False

        # Cache only. The terminal review card above is the durable retry
        # count and recovery point if this parent metadata write fails.
        try:
            await self.store.update_delegation_work_item(
                target_work_item_id,
                metadata_updates={
                    "review_verdict_parse_retry_count": new_retry_count,
                    "review_verdict_parse_retry_at": datetime.now().isoformat(),
                },
            )
        except Exception:
            logger.opt(exception=True).debug(
                "verdict-parse-retry: failed to stamp counter on child"
            )

        completion_report = str(
            review_metadata.get("review_completion_report", "") or ""
        ).strip()
        new_review_item = await self._ensure_review_work_item_for_work_item(
            target_work_item_id,
            worker_task=retry_worker_task,
            completion_report=completion_report,
            metadata_updates={
                "review_owner_role_id": review_owner_role_id,
                "review_owner_seat_id": review_owner_seat_id,
                "review_retry_hint": _REVIEW_VERDICT_PARSE_RETRY_HINT,
                "review_retry_of_attempt": int(
                    review_metadata.get("review_attempt", 0) or 0
                ),
                "review_retry_reason": "verdict_parse_failed",
            },
            source_report_item=source_report_item,
        )
        if new_review_item is None:
            return False
        try:
            base_summary = str(getattr(new_review_item, "summary", "") or "").strip() or (
                "Review the completed child deliverable and decide whether to "
                "approve it or request rework."
            )
            await self.store.update_delegation_work_item(
                getattr(new_review_item, "work_item_id", ""),
                summary=base_summary + _REVIEW_VERDICT_PARSE_RETRY_HINT,
                metadata_updates={
                    "review_retry_hint": _REVIEW_VERDICT_PARSE_RETRY_HINT,
                    "review_retry_reason": "verdict_parse_failed",
                    "review_retry_of_attempt": int(
                        review_metadata.get("review_attempt", 0) or 0
                    ),
                },
            )
        except Exception:
            logger.opt(exception=True).debug(
                "verdict-parse-retry: extending new summary failed"
            )

        try:
            self._signal_dispatcher_wake()
        except Exception:
            logger.opt(exception=True).debug("verdict-parse-retry: dispatcher wake failed")

        logger.info(
            f"verdict-parse-retry spawned: child={target_work_item_id} "
            f"retry_count={new_retry_count} "
            f"new_review={getattr(new_review_item, 'work_item_id', '?')}"
        )
        return True


    @staticmethod
    def _review_feedback_version(metadata: dict[str, Any] | None) -> int:
        payload = dict(metadata or {})
        for key in ("review_feedback_version", "review_rework_count"):
            try:
                parsed = int(payload.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
        return 0


    async def _durable_review_parse_retry_count(
        self,
        parent_item: DelegationWorkItem,
    ) -> int:
        """Count reviewer format retries from parent cache and terminal cards."""
        cached = self._safe_positive_int(
            (parent_item.metadata or {}).get("review_verdict_parse_retry_count", 0)
        )
        run_items = await self._run_items_for_parent(parent_item)
        durable = sum(
            1
            for review_item in self._targeting_auxiliary_items(
                run_items,
                parent_item.work_item_id,
                kind="review",
            )
            if str(
                (review_item.metadata or {}).get("review_work_item_outcome", "")
                or ""
            ).strip()
            == "verdict_parse_failed"
        )
        return max(cached, durable)


    async def _load_review_target_task(
        self,
        *,
        review_task: Task,
        child_item: DelegationWorkItem | None,
    ) -> Task | None:
        if not self.store or not hasattr(self.store, "get_task"):
            return None
        candidate_task_ids: list[str] = []
        if child_item is not None:
            get_runtime_task = getattr(self.store, "get_runtime_task_for_work_item", None)
            if callable(get_runtime_task):
                try:
                    linked_task = await get_runtime_task(str(getattr(child_item, "work_item_id", "") or "").strip())
                except Exception:
                    linked_task = None
                linked_task_id = str(getattr(linked_task, "id", "") or "").strip()
                if linked_task_id:
                    candidate_task_ids.append(linked_task_id)
        for raw in (
            (review_task.metadata or {}).get("review_target_worker_task_id"),
        ):
            value = str(raw or "").strip()
            if value and value not in candidate_task_ids:
                candidate_task_ids.append(value)
        for task_id in candidate_task_ids:
            try:
                target = await self.store.get_task(task_id)
            except Exception:
                target = None
            if target is not None:
                return target
        return None


    async def _save_review_rework_human_checkpoint(
        self,
        task: Task,
        *,
        feedback: str,
        review_owner_role_id: str,
        review_feedback_version: int,
        escalation_reason: str,
    ) -> None:
        if not self.checkpoint_callback:
            return

        pending_getter = getattr(self.store, "get_pending_checkpoints", None)
        if callable(pending_getter):
            try:
                pending = await pending_getter(
                    project_id=str(task.project_id or "default"),
                    session_id=str(task.session_id or "").strip() or None,
                    checkpoint_types=["task_user_input"],
                )
            except Exception:
                pending = []
            for checkpoint in pending:
                payload = dict(getattr(checkpoint, "payload", {}) or {})
                existing_task_id = str(
                    payload.get("task_id")
                    or payload.get("waiting_task_id")
                    or ""
                ).strip()
                if existing_task_id != str(task.id or "").strip():
                    continue
                if str(payload.get("manual_intervention_source", "") or "").strip() != "review_rework_escalation":
                    continue
                if self._review_feedback_version(payload) == review_feedback_version:
                    return

        runtime_payload = self._runtime_checkpoint_payload(task)
        work_item_payload = self._work_item_checkpoint_payload(task)
        summary = (
            str(escalation_reason or "").strip()
            or "Manual intervention required before this work item can continue."
        )
        prompt = "\n\n".join(
            part for part in (
                summary,
                "Please decide how this work item should continue.",
                "Reviewer feedback:",
                feedback,
            )
            if str(part).strip()
        ).strip()
        pause_request = {
            "reason": summary,
            "questions": [
                "Should this work item get another rework attempt, be approved as-is, or be redirected?"
            ],
            "required_fields": ["decision"],
            "context_note": (
                f"Reviewer: {review_owner_role_id}" if review_owner_role_id else "Reviewer feedback is attached below."
            ),
            "resume_hint": "Reply with the decision and any guidance the resumed work item should follow.",
        }
        await self.checkpoint_callback(
            {
                "checkpoint_type": "task_user_input",
                "project_id": task.project_id,
                "session_id": task.session_id,
                "task_id": task.id,
                "payload": {
                    "task_id": task.id,
                    "waiting_task_id": task.id,
                    "session_id": task.session_id,
                    "execution_mode": str(task.metadata.get("execution_mode", "company_mode") or "company_mode"),
                    "task_ids": [t.id for t in self._active_tasks] if self._active_tasks else [task.id],
                    **work_item_identity_payload_for_task(task),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "prompt": prompt,
                    "pause_request": pause_request,
                    "review_level": "human",
                    "review_target_role_id": "owner",
                    "review_chain_role_ids": [],
                    "manual_intervention_source": "review_rework_escalation",
                    "review_owner_role_id": review_owner_role_id,
                    "review_feedback_version": review_feedback_version,
                    "review_feedback": feedback,
                    **work_item_payload,
                    **runtime_payload,
                },
            }
        )


    async def _refresh_delegation_dependents(self, task: Task) -> None:
        """Propagate dependency completion/escalation into parent phases.

        Thin wrapper around
        ``opc.layer2_organization.work_item_transition.refresh_dependents_for_run``
        (the free function). The free function is also registered as a
        phase-transition hook (``refresh_dependents_hook``), so any
        terminal child transition auto-triggers the refresh — this
        explicit call remains for historical APPROVED-verdict callers
        and as a belt-and-suspenders path. Re-entrancy is guarded
        inside the free function via a ContextVar.
        """
        if not self.store:
            return
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        if not run_id:
            return
        await refresh_dependents_for_run(
            self.store,
            run_id=run_id,
            source_task_id=str(task.id or "").strip() or None,
            source_work_item_id=linked_work_item_id_for_task(task) or None,
            source_cell_id=str((task.metadata or {}).get("delegation_cell_id", "") or "").strip() or None,
            source_role_id=str(task.assigned_to or (task.metadata or {}).get("work_item_role_id", "") or "").strip() or None,
        )


    async def _enforce_work_item_contracts(self, task: Task, result: TaskResult) -> list[str]:
        issues: list[str] = []
        projection_id = self._projection_id_for_task(task)
        work_item_turn_type = self._turn_type_for_task(task)
        ownership_contract = dict(task.metadata.get("ownership_contract", {}) or {})
        verification_required = bool(task.metadata.get("work_item_verification_required", False))
        output_metadata = self._work_item_output_metadata_for_task(task)
        artifact_index = list(output_metadata.get("work_item_artifact_index", []) or [])
        work_item_summary = str(output_metadata.get("work_item_summary", "") or "").strip()
        verification_status = dict(output_metadata.get("verification_status", {}) or {})
        verification_evidence = dict(output_metadata.get("verification_evidence", {}) or {})

        if ownership_contract and work_item_turn_type == "execute":
            if not work_item_summary:
                issues.append("Ownership/artifact contract violation: missing work-item summary.")
            if not artifact_index:
                issues.append("Ownership/artifact contract violation: missing work-item artifact index.")
            if verification_required and not verification_status:
                issues.append("Verification is required for this work item but no verification_status was recorded.")
            if verification_required and not self._verification_evidence_satisfies_contract(verification_evidence):
                issues.append("Verification evidence is missing or incomplete for a verification-required execute work item.")

        # --- Collaboration awareness check for parallel work items ---
        collaboration_warning = await self._check_collaboration_awareness(task)
        if collaboration_warning:
            task.metadata = dict(task.metadata)
            task.metadata["_collaboration_awareness_warning"] = collaboration_warning

        if issues and await self._prepare_contract_rework(task, issues):
            return issues

        if issues:
            task.metadata["artifact_contract_status"] = "failed"
            contract_failure_status = (
                TaskStatus.AWAITING_MANAGER_REVIEW
                if work_item_turn_type == "execute"
                else TaskStatus.FAILED
            )
            await self._append_progress(task, "Work-item contract enforcement failed.")
            for issue in issues:
                await self._append_progress(task, issue)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=contract_failure_status,
                reason="contract_enforcement_failed",
            )
            await self.save_task(task)
            await self._emit_runtime_signal(
                "artifact_contract_failed",
                {
                    "task_id": task.id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=work_item_turn_type),
                    "member_session_id": task.metadata.get("member_session_id", ""),
                    "issues": list(issues),
                },
            )
            if verification_required and not self._verification_evidence_satisfies_contract(verification_evidence):
                await self._emit_runtime_signal(
                    "verification_required",
                    {
                        "task_id": task.id,
                        **work_item_identity_payload(projection_id=projection_id, turn_type=work_item_turn_type),
                        "reason": "missing_verification_evidence",
                    },
                )
            await self._emit_progress(
                f"[Company:{projection_id}] contract enforcement failed",
                task_id=task.id,
            )
            return issues

        if ownership_contract and work_item_turn_type == "execute":
            task.metadata["artifact_contract_status"] = "satisfied"
        else:
            task.metadata["artifact_contract_status"] = task.metadata.get("artifact_contract_status", "not_required")
        if verification_evidence:
            await self._emit_runtime_signal(
                "verification_completed",
                {
                    "task_id": task.id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=work_item_turn_type),
                    "verification_evidence": dict(verification_evidence),
                },
            )
        return []


    @staticmethod
    def _contract_rework_limit(task: Task) -> int:
        try:
            value = int(task.metadata.get("contract_rework_max_retries", _DEFAULT_CONTRACT_REWORK_MAX_RETRIES) or 0)
        except Exception:
            value = _DEFAULT_CONTRACT_REWORK_MAX_RETRIES
        return max(0, value)


    @staticmethod
    def _contract_issue_retriable(issue: str) -> bool:
        normalized = str(issue or "").strip().lower()
        if not normalized:
            return False
        non_retriable_markers = (
            "acknowledgement is still pending",
        )
        return not any(marker in normalized for marker in non_retriable_markers)


    def _build_contract_rework_record(
        self,
        *,
        task: Task,
        issues: list[str],
        rework_round: int,
        max_retries: int,
    ) -> dict[str, Any]:
        return {
            "task_id": task.id,
            **work_item_identity_payload_for_task(task),
            "work_item_title": task.title,
            "issues": [str(item).strip() for item in issues if str(item).strip()],
            "rework_round": rework_round,
            "max_retries": max_retries,
            "requested_at": datetime.now().isoformat(),
        }


    def _render_contract_rework_summary(self, rework_request: dict[str, Any]) -> str:
        work_item_title = (
            str(rework_request.get("work_item_title", "")).strip()
            or str(rework_request.get("work_item_projection_title", "")).strip()
            or "Current work item"
        )
        issues = [
            str(item).strip()
            for item in list(rework_request.get("issues", []) or [])
            if str(item).strip()
        ]
        rework_round = int(rework_request.get("rework_round", 1) or 1)
        max_retries = int(rework_request.get("max_retries", _DEFAULT_CONTRACT_REWORK_MAX_RETRIES) or _DEFAULT_CONTRACT_REWORK_MAX_RETRIES)
        lines = [
            f"Contract rework requested for {work_item_title}.",
            f"Round: {rework_round}/{max_retries}",
            "Your previous submission was incomplete. Fix every missing item below before finishing again.",
        ]
        if issues:
            lines.append("Missing required outputs:")
            lines.extend(f"- {issue}" for issue in issues)
        lines.append(
            "Do not stop at a high-level summary. Produce the missing summary, artifact index, verification evidence, and handoff details explicitly in your next completion."
        )
        return "\n".join(lines)


    @staticmethod
    def _reset_contract_outputs_for_retry(task: Task) -> None:
        for key in (
            "work_item_summary",
            "work_item_summary_for_downstream",
            "work_item_artifact_index",
            "verification_status",
            "verification_evidence",
            "verification",
            "structured_review_verdict",
            "delivery_package",
            "downstream_assignments",
            "artifacts",
            "gate_harness_status",
            "gate_harness_constraints",
            "gate_harness_pending_decision",
            "gate_harness_decision",
            "gate_harness_evidence",
        ):
            task.metadata.pop(key, None)
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot.pop("latest_artifacts", None)
        task.context_snapshot.pop("work_item_owned_outputs", None)


    async def _prepare_contract_rework(self, task: Task, issues: list[str]) -> bool:
        work_item_turn_type = self._turn_type_for_task(task)
        if work_item_turn_type != "execute":
            return False
        projection_id = self._projection_id_for_task(task)
        normalized_issues = [str(item).strip() for item in issues if str(item).strip()]
        if not normalized_issues or not all(self._contract_issue_retriable(issue) for issue in normalized_issues):
            return False
        rework_count = int(task.metadata.get("contract_rework_count", 0) or 0)
        max_retries = self._contract_rework_limit(task)
        if rework_count >= max_retries:
            return False

        rework_round = rework_count + 1
        rework_request = self._build_contract_rework_record(
            task=task,
            issues=normalized_issues,
            rework_round=rework_round,
            max_retries=max_retries,
        )
        task.metadata = dict(task.metadata)
        task.metadata["contract_rework_count"] = rework_round
        task.metadata["contract_rework_feedback"] = "\n".join(normalized_issues)
        task.metadata["contract_rework_request"] = dict(rework_request)
        task.metadata["artifact_contract_status"] = "reworking"
        task.metadata["_retry_contract_enforcement"] = True
        task.result = None
        self._reset_contract_outputs_for_retry(task)
        await transition_work_item_from_task(
            self.store, task,
            target_status_or_phase=TaskStatus.PENDING,
            reason="contract_rework_retry",
        )
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["latest_contract_rework"] = dict(rework_request)
        await self._append_progress(task, self._render_contract_rework_summary(rework_request))
        await self.save_task(task)
        await self._emit_runtime_signal(
            "artifact_contract_retry",
            {
                "task_id": task.id,
                **work_item_identity_payload(projection_id=projection_id, turn_type=work_item_turn_type),
                "rework_round": rework_round,
                "max_retries": max_retries,
                "issues": normalized_issues,
            },
        )
        await self._emit_progress(
            f"[Company:{projection_id}] reworking contract enforcement ({rework_round}/{max_retries})",
            task_id=task.id,
        )
        return True


    def _record_gate_harness_history(self, task: Task, decision: GateHarnessDecision) -> None:
        history = [
            dict(item)
            for item in list(task.metadata.get("gate_harness_history", []) or [])
            if isinstance(item, dict)
        ]
        history.append(
            {
                "action": decision.action,
                "summary": decision.summary,
                "target_projection_id": target_projection_id_for_decision(decision),
                "blocker_fingerprint": decision.blocker_fingerprint,
                "blocker_types": list(decision.blocker_types),
                "recorded_at": datetime.now().isoformat(),
            }
        )
        task.metadata["gate_harness_history"] = history[-12:]


    def _build_gate_harness_rework_record(
        self,
        *,
        source_task: Task,
        target_task: Task,
        decision: GateHarnessDecision,
        rework_round: int,
    ) -> dict[str, Any]:
        target_projection_id = self._projection_id_for_task(target_task)
        return {
            "source_projection_id": self._projection_id_for_task(source_task),
            "source_work_item_title": source_task.title,
            **gate_rework_payload(target_projection_id=target_projection_id),
            "target_work_item_title": target_task.title,
            "feedback": decision.summary,
            "blockers": list(decision.blockers),
            "blocker_types": list(decision.blocker_types),
            "constraints": list(decision.constraints),
            "rework_round": rework_round,
            "requested_at": datetime.now().isoformat(),
        }


    def _render_gate_harness_rework_summary(self, request: dict[str, Any]) -> str:
        lines = [
            f"Gate harness requested rework for {str(request.get('target_work_item_title', '') or 'current work item').strip()}.",
            f"Requested by: {str(request.get('source_work_item_title', '') or 'runtime harness').strip()}",
            f"Round: {int(request.get('rework_round', 1) or 1)}",
        ]
        feedback = str(request.get("feedback", "") or "").strip()
        if feedback:
            lines.append(f"## Gate Harness Summary\n{feedback}")
        blockers = [
            str(item).strip()
            for item in list(request.get("blockers", []) or [])
            if str(item).strip()
        ]
        if blockers:
            lines.append("## Blocking Findings\n" + "\n".join(f"- {item}" for item in blockers))
        constraints = [
            str(item).strip()
            for item in list(request.get("constraints", []) or [])
            if str(item).strip()
        ]
        if constraints:
            lines.append("## Constraints To Preserve\n" + "\n".join(f"- {item}" for item in constraints))
        lines.append("Resume the prior work-item session, fix the blocking issues above, and resubmit.")
        return "\n\n".join(lines)


    async def _gate_harness_initiate_rework(
        self,
        source_task: Task,
        decision: GateHarnessDecision,
        task_by_projection_id: dict[str, Task],
    ) -> Task | None:
        touched_task_ids: set[str] = set()
        target_projection_ids = target_projection_ids_for_decision(decision)
        if not target_projection_ids:
            target_projection_ids = [self._projection_id_for_task(source_task)]
        primary_target: Task | None = None
        for target_projection_id in target_projection_ids:
            target_task = task_by_projection_id.get(target_projection_id)
            if target_task is None:
                continue
            if primary_target is None:
                primary_target = target_task
            rework_round = int(target_task.metadata.get("gate_harness_rework_count", 0) or 0) + 1
            request = self._build_gate_harness_rework_record(
                source_task=source_task,
                target_task=target_task,
                decision=decision,
                rework_round=rework_round,
            )
            affected_projection_ids = [target_projection_id, *self._collect_downstream_projection_ids(target_projection_id)]
            for affected_projection_id in affected_projection_ids:
                affected_task = task_by_projection_id.get(affected_projection_id)
                if affected_task is None or affected_task.id in touched_task_ids:
                    continue
                touched_task_ids.add(affected_task.id)
                affected_task.metadata = dict(affected_task.metadata)
                affected_task.context_snapshot = dict(affected_task.context_snapshot)
                affected_task.result = None
                self._reset_work_item_outputs_for_rework(affected_task)
                if affected_projection_id == target_projection_id:
                    affected_task.metadata["gate_harness_rework_count"] = rework_round
                    affected_task.metadata["gate_harness_rework_feedback"] = decision.summary
                    affected_task.metadata["gate_harness_rework_request"] = dict(request)
                    history = list(affected_task.metadata.get("gate_harness_rework_requests", []) or [])
                    history.append(dict(request))
                    affected_task.metadata["gate_harness_rework_requests"] = history[-6:]
                    affected_task.context_snapshot["latest_gate_harness_rework"] = dict(request)
                    await self._append_progress(affected_task, self._render_gate_harness_rework_summary(request))
                else:
                    affected_task.metadata["upstream_gate_harness_rework_source_projection_id"] = target_projection_id
                    affected_task.context_snapshot["upstream_gate_harness_rework_source_projection_id"] = target_projection_id
                    adaptive = self._normalize_adaptive_metadata(affected_task.metadata.get("adaptive", {}))
                    adaptive["normalized_state"] = "invalidated"
                    affected_task.metadata["adaptive"] = adaptive
                    await self._append_progress(
                        affected_task,
                        f"Reset because upstream work-item projection `{target_projection_id}` entered gate-harness rework.",
                    )
                await transition_work_item_from_task(
                    self.store, affected_task,
                    target_status_or_phase=TaskStatus.PENDING,
                    reason="gate_harness_rework_reset",
                )
                await self.save_task(affected_task)
                # Emit work_item_progress event so the UI reverts the work item from
                # "done" (checkmark) back to "active" (dots) during rework.
                await self._emit_progress(
                    f"[Company:{affected_projection_id}] reworking (gate harness rework round {rework_round})",
                    task_id=affected_task.id,
                )
        return primary_target


    def _render_gate_harness_checkpoint_prompt(self, task: Task, decision: GateHarnessDecision) -> str:
        action_label = {
            "await_user_decision": "user decision",
            "escalate": "manual approval",
            "replan": "runtime replan",
        }.get(decision.action, "runtime decision")
        lines = [
            f"Gate harness recommends `{decision.action}` for work item `{task.title}`.",
            decision.summary,
        ]
        blockers = [str(item).strip() for item in list(decision.blockers or []) if str(item).strip()]
        if blockers:
            lines.append("Blocking findings:")
            lines.extend(f"- {item}" for item in blockers[:6])
        constraints = [str(item).strip() for item in list(decision.constraints or []) if str(item).strip()]
        if constraints:
            lines.append("Constraints if you continue:")
            lines.extend(f"- {item}" for item in constraints[:6])
        lines.append(
            f"Reply `approve` / `continue` to accept this {action_label} handling, or `deny` / `stop` to reject it."
        )
        return "\n".join(lines).strip()


    async def _pause_for_gate_harness_decision(
        self,
        task: Task,
        decision: GateHarnessDecision,
        *,
        review_level: str,
        review_target_role_id: str = "",
        review_chain_role_ids: list[str] | None = None,
    ) -> None:
        normalized_review_level = str(review_level or "").strip().lower() or "human"
        decision_target_projection_id = target_projection_id_for_decision(decision)
        task.status = self._review_status_for_level(normalized_review_level)
        task.metadata = dict(task.metadata)
        task.metadata["gate_harness_pending_decision"] = decision.to_dict()
        task.metadata["gate_harness_review_level"] = normalized_review_level
        task.metadata["gate_harness_review_target_role_id"] = str(review_target_role_id or "").strip()
        task.metadata["gate_harness_review_chain_role_ids"] = [
            str(item).strip()
            for item in list(review_chain_role_ids or [])
            if str(item).strip()
        ]
        await self._append_progress(
            task,
            f"Gate harness paused the work item with action `{decision.action}` for {normalized_review_level} review.",
        )
        await self._append_progress(task, decision.summary)
        await self.save_task(task)
        gate = WorkItemGatePolicy(
            gate_type="review" if normalized_review_level == "manager" else "human_confirmation",
            instructions=decision.summary,
            reviewer_role=str(review_target_role_id or "").strip() or None,
            requires_human=normalized_review_level != "manager",
            on_reject="rework" if decision_target_projection_id else "halt",
            rework_projection_id=decision_target_projection_id or None,
            max_retries=1,
            metadata={
                "source": "gate_harness",
                "recommended_action": decision.action,
                "review_level": normalized_review_level,
                "review_target_role_id": str(review_target_role_id or "").strip(),
                "review_chain_role_ids": [
                    str(item).strip()
                    for item in list(review_chain_role_ids or [])
                    if str(item).strip()
                ],
                "constraints": list(decision.constraints),
                "blockers": list(decision.blockers),
                "blocker_types": list(decision.blocker_types),
                "prompt_override": self._render_gate_harness_checkpoint_prompt(task, decision),
                **gate_rework_payload(
                    rework_projection_id=decision_target_projection_id,
                ),
            },
        )
        if decision_target_projection_id:
            mark_gate_rework_projection(gate, decision_target_projection_id)
        await self._save_checkpoint(task, gate)


    async def _apply_gate_harness(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> str:
        harness = self._gate_harness_for_task(task)
        if not harness.policy.enabled:
            return "pass"
        task.metadata = dict(task.metadata)
        packet, decision = await harness.evaluate(task, task_by_projection_id)
        task.metadata["gate_harness_evidence"] = packet.to_dict()
        task.metadata["gate_harness_decision"] = decision.to_dict()
        self._record_gate_harness_history(task, decision)

        if decision.action == "pass":
            task.metadata["gate_harness_status"] = "passed"
            task.metadata.pop("gate_harness_constraints", None)
            task.metadata.pop("gate_harness_pending_decision", None)
            return "pass"

        if decision.action == "pass_with_constraints":
            task.metadata["gate_harness_status"] = "passed_with_constraints"
            task.metadata["gate_harness_constraints"] = list(decision.constraints or decision.blockers or decision.residual_risks)
            merged_risks = self._merge_unique_items(
                list(self._work_item_output_metadata_for_task(task).get("risks", []) or task.metadata.get("risks", []) or []),
                list(task.metadata["gate_harness_constraints"]),
            )
            linked_work_item_id = linked_work_item_id_for_task(task)
            if linked_work_item_id:
                self._set_work_item_output_context(task, {"risks": merged_risks})
                await update_work_item_owned_metadata(self.store, linked_work_item_id, {"risks": merged_risks})
                task.metadata.pop("risks", None)
            else:
                task.metadata["risks"] = merged_risks
            task.metadata.pop("gate_harness_pending_decision", None)
            await self._append_progress(task, f"Gate harness allowed the work item to continue with constraints: {decision.summary}")
            await self.save_task(task)
            return "pass_with_constraints"

        if decision.action == "rerun_work_item":
            task.result = None
            task.metadata["gate_harness_status"] = "rerun_pending"
            await self._append_progress(task, decision.summary)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=TaskStatus.PENDING,
                reason="gate_harness_rerun_work_item",
            )
            await self.save_task(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] gate harness requested a rerun",
                task_id=task.id,
            )
            return "rerun_work_item"

        if decision.action == "rework_same_work_item":
            rework_task = await self._gate_harness_initiate_rework(task, decision, task_by_projection_id)
            if rework_task is None:
                failed_target = target_projection_id_for_decision(decision)
                task.metadata["gate_harness_status"] = "rework_failed"
                await self._append_progress(task, f"Gate harness could not restore rework target `{failed_target}`.")
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.FAILED,
                    reason="gate_harness_rework_failed",
                )
                await self.save_task(task)
                return "rework_failed"
            task.metadata["gate_harness_status"] = "reworking"
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] gate harness requested {decision.action}",
                task_id=task.id,
            )
            return decision.action

        if decision.action in {"await_user_decision", "replan", "escalate"}:
            task.metadata["gate_harness_status"] = "awaiting_decision"
            review_chain = self._review_chain_for_task(task)
            review_level = "human"
            review_target_role_id = ""
            if decision.action in {"replan", "escalate"} and review_chain:
                review_level = "manager"
                review_target_role_id = review_chain[0]
            await self._pause_for_gate_harness_decision(
                task,
                decision,
                review_level=review_level,
                review_target_role_id=review_target_role_id,
                review_chain_role_ids=review_chain,
            )
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] gate harness paused for {decision.action} ({review_level} review)",
                task_id=task.id,
            )
            return decision.action

        task.metadata["gate_harness_status"] = "passed"
        return "pass"


    async def _finalize_work_item_with_gate_harness(
        self,
        task: Task,
        task_by_projection_id: dict[str, Task],
    ) -> str:
        action = await self._apply_gate_harness(task, task_by_projection_id)
        if action not in {"pass", "pass_with_constraints"}:
            return action
        await self._finalize_completed_work_item(task)
        return action


    async def _apply_gate(self, task: Task, gate: WorkItemGatePolicy, task_by_projection_id: dict[str, Task]) -> None:
        if gate.gate_type == "automated_verification":
            await self._apply_automated_verification_gate(task, gate)
            return

        metadata = {
            "role_id": task.assigned_to,
            "gate_type": gate.gate_type,
            **work_item_identity_payload_for_task(task),
        }
        approved = True
        decision = None
        if gate.gate_type in {"approval", "human_confirmation"} or gate.requires_human:
            approved, decision = await self.approval_engine.authorize_work_item_action(
                task=task,
                work_item_title=task.title,
                metadata=metadata,
                on_progress=self.progress_callback,
                force_human=(gate.gate_type == "human_confirmation" or gate.requires_human),
            )
        if decision and decision.action == ApprovalAction.REQUIRE_INPUT:
            review_level = "manager" if gate.reviewer_role and not gate.requires_human else "human"
            task.status = self._review_status_for_level(review_level)
            await self._append_progress(
                task,
                "Awaiting manager review." if review_level == "manager" else "Awaiting human confirmation.",
            )
            await self.save_task(task)
            await self._save_checkpoint(task, gate)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] awaiting {'manager review' if review_level == 'manager' else 'confirmation'}",
                task_id=task.id,
            )
            return
        verdict = self._structured_or_inferred_verdict(task, gate)
        if gate.gate_type == "approval" and not approved:
            verdict = "reject"
        if gate.gate_type == "human_confirmation":
            if not approved:
                verdict = "reject"
            else:
                verdict = "approve"

        if verdict == "reject":
            reviewer_feedback = self._structured_review_feedback(task)
            if not reviewer_feedback and task.result and isinstance(task.result, dict) and task.result.get("content"):
                reviewer_feedback = str(task.result["content"]).strip()
            rework_task = await self.prepare_gate_rework(
                task,
                gate,
                task_by_projection_id,
                reviewer_feedback,
            )
            if rework_task:
                rework_projection_id = rework_projection_id_for_gate(gate)
                await self._append_progress(task, f"Gate rejected output. Reworking work item {rework_projection_id}.")
                if rework_task is not task:
                    await self.save_task(rework_task)
                await self.save_task(task)
                await self._emit_progress(
                    f"[Company:{self._projection_id_for_task(task)}] rejected; reworking {rework_projection_id}",
                    task_id=task.id,
                )
                return

            await self._append_progress(task, "Gate rejected output and no rework remained.")
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.FAILED,
                reason="gate_rejected_no_rework",
            )
            await self.save_task(task)
            await self._emit_progress(f"[Company:{self._projection_id_for_task(task)}] rejected", task_id=task.id)
            return

        if await self._block_completion_for_unread_inbox(task):
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] inbox gate pending",
                task_id=task.id,
            )
            return

        await self._append_progress(task, f"Gate {gate.gate_type} passed.")
        await self._apply_done_transition(task)
        await self.save_task(task)
        completion_action = await self._finalize_work_item_with_gate_harness(task, task_by_projection_id)
        if task.status == TaskStatus.DONE:
            await self._emit_progress(f"[Company:{self._projection_id_for_task(task)}] gate passed", task_id=task.id)
        elif task.status in _REVIEW_WAITING_STATUSES:
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] awaiting {'manager review' if task.status == TaskStatus.AWAITING_MANAGER_REVIEW else 'confirmation'}",
                task_id=task.id,
            )


    async def _apply_automated_verification_gate(self, task: Task, gate: WorkItemGatePolicy) -> None:
        """Run verification commands from the gate metadata and auto-pass/fail."""
        import sys as _sys
        is_windows = _sys.platform.startswith("win")
        readiness_artifact = str(gate.metadata.get("readiness_artifact", "") or "").strip()
        if readiness_artifact == "workspace_manifest":
            await self._apply_workspace_manifest_gate(task, gate)
            return
        if readiness_artifact == "data_acquisition_report":
            await self._apply_data_acquisition_gate(task, gate)
            return

        verification_commands = list(gate.metadata.get("verification_commands", []) or [])
        if is_windows:
            verification_commands = list(gate.metadata.get("verification_commands_win", []) or []) or verification_commands
        if not verification_commands:
            manifest = dict(task.metadata.get("environment_manifest", {}) or {})
            checks_key = "verification_checks_win" if is_windows else "verification_checks"
            fallback_key = "verification_checks"
            checks = list(manifest.get(checks_key, []) or []) or list(manifest.get(fallback_key, []) or [])
            verification_commands = [
                check.get("command", "") for check in checks
                if isinstance(check, dict) and check.get("command")
            ]
        if not verification_commands:
            await self._append_progress(task, "Automated verification gate: no commands to verify, auto-pass.")
            await self._apply_done_transition(task)
            await self.save_task(task)
            await self._finalize_completed_work_item(task)
            return

        from opc.layer4_tools.shell import bash_exec, powershell_exec
        exec_fn = powershell_exec if is_windows else bash_exec
        all_passed = True
        check_results: list[dict[str, Any]] = []
        for cmd in verification_commands:
            cmd_str = str(cmd).strip()
            if not cmd_str:
                continue
            result = await exec_fn(command=cmd_str, timeout=60)
            passed = result.get("success", False)
            check_results.append({
                "command": cmd_str,
                "passed": passed,
                "exit_code": result.get("exit_code"),
                "stdout": str(result.get("stdout", "")),
                "stderr": str(result.get("stderr", "")),
                "platform": "windows" if is_windows else ("macos" if _sys.platform == "darwin" else "linux"),
            })
            if not passed:
                all_passed = False

        task.metadata["automated_verification_results"] = check_results

        if all_passed:
            await self._append_progress(
                task,
                f"Automated verification gate passed: {len(check_results)}/{len(check_results)} checks succeeded.",
            )
            await self._apply_done_transition(task)
            await self.save_task(task)
            await self._finalize_completed_work_item(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] verification passed",
                task_id=task.id,
            )
        else:
            failed = [c for c in check_results if not c["passed"]]
            rework_task = await self.prepare_gate_rework(
                task,
                gate,
                {
                    task.id: task,
                    self._projection_id_for_task(task): task,
                },
                "",
            )
            if rework_task:
                feedback = "\n".join(
                    f"FAILED: {c['command']} (exit {c['exit_code']}): {c['stderr']}"
                    for c in failed
                )
                await self._append_progress(task, f"Automated verification gate failed:\n{feedback}")
                if rework_task is not task:
                    await self.save_task(rework_task)
                await self.save_task(task)
            else:
                await self._append_progress(
                    task,
                    f"Automated verification failed: {len(failed)} check(s) did not pass. No rework remaining.",
                )
                await transition_work_item_from_task(
                    self.store, task,
                    target_status_or_phase=Phase.FAILED,
                    reason="automated_verification_no_rework",
                )
                await self.save_task(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] verification failed",
                task_id=task.id,
            )


    async def _apply_workspace_manifest_gate(self, task: Task, gate: WorkItemGatePolicy) -> None:
        manifest = dict(task.metadata.get("workspace_manifest", {}) or {})
        root_path = str(manifest.get("root_path", "") or task.metadata.get("target_output_dir", "") or "").strip()
        required_dirs = [
            str(item).strip()
            for item in list(gate.metadata.get("required_dirs", []) or _DEFAULT_WORKSPACE_LAYOUT)
            if str(item).strip()
        ]
        check_results: list[dict[str, Any]] = []
        all_passed = bool(root_path)
        root = Path(root_path).expanduser() if root_path else None
        if root is not None:
            root_exists = root.exists()
            check_results.append({
                "command": "workspace_root_exists",
                "passed": root_exists,
                "exit_code": 0 if root_exists else 1,
                "stdout": "",
                "stderr": "" if root_exists else "Workspace root is missing.",
            })
            all_passed = all_passed and root_exists
            for relative in required_dirs:
                candidate = root / relative
                passed = candidate.exists() and candidate.is_dir()
                check_results.append({
                    "command": f"workspace_dir_exists:{relative}",
                    "passed": passed,
                    "exit_code": 0 if passed else 1,
                    "stdout": "",
                    "stderr": "" if passed else f"Missing required workspace directory `{relative}`.",
                })
                all_passed = all_passed and passed
        task.metadata["automated_verification_results"] = check_results
        if all_passed:
            await self._append_progress(task, "Workspace manifest gate passed.")
            await self._apply_done_transition(task)
            await self.save_task(task)
            await self._finalize_completed_work_item(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] verification passed",
                task_id=task.id,
            )
            return
        feedback = "\n".join(
            item["stderr"] or f"Check failed: {item['command']}"
            for item in check_results
            if not item.get("passed")
        ) or "Workspace manifest gate failed."
        rework_task = await self.prepare_gate_rework(
            task,
            gate,
            {
                task.id: task,
                self._projection_id_for_task(task): task,
            },
            feedback,
        )
        if rework_task:
            await self._append_progress(task, feedback)
            if rework_task is not task:
                await self.save_task(rework_task)
            await self.save_task(task)
        else:
            await self._append_progress(task, feedback)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.FAILED,
                reason="workspace_manifest_gate_no_rework",
            )
            await self.save_task(task)
        await self._emit_progress(
            f"[Company:{self._projection_id_for_task(task)}] verification failed",
            task_id=task.id,
        )


    async def _apply_data_acquisition_gate(self, task: Task, gate: WorkItemGatePolicy) -> None:
        report = dict(task.metadata.get("data_acquisition_report", {}) or {})
        status = str(report.get("status", "") or "").strip().lower()
        allowed = {
            str(item).strip().lower()
            for item in list(gate.metadata.get("allowed_statuses", []) or [])
            if str(item).strip()
        } or {"ready", "already_present", "not_required"}
        blocking = {
            str(item).strip().lower()
            for item in list(gate.metadata.get("blocking_statuses", []) or [])
            if str(item).strip()
        } or {"partial", "missing_critical"}
        require_attempt_evidence = bool(gate.metadata.get("require_attempt_evidence_for_blocking", False))
        valid_evidence, feedback = self._evaluate_data_acquisition_gate(
            task,
            report,
            status=status,
            allowed=allowed,
            blocking=blocking,
            require_attempt_evidence=require_attempt_evidence,
        )
        task.metadata["automated_verification_results"] = [{
            "command": "data_acquisition_status",
            "passed": status in allowed and valid_evidence,
            "exit_code": 0 if status in allowed and valid_evidence else 1,
            "stdout": "",
            "stderr": "" if status in allowed and valid_evidence else feedback,
        }]
        if status in allowed and valid_evidence:
            await self._append_progress(task, f"Data acquisition gate passed with status `{status}`.")
            await self._apply_done_transition(task)
            await self.save_task(task)
            await self._finalize_completed_work_item(task)
            await self._emit_progress(
                f"[Company:{self._projection_id_for_task(task)}] verification passed",
                task_id=task.id,
            )
            return
        rework_task = await self.prepare_gate_rework(
            task,
            gate,
            {
                task.id: task,
                self._projection_id_for_task(task): task,
            },
            feedback,
        )
        if rework_task:
            await self._append_progress(task, feedback)
            if rework_task is not task:
                await self.save_task(rework_task)
            await self.save_task(task)
        else:
            await self._append_progress(task, feedback)
            await transition_work_item_from_task(
                self.store, task,
                target_status_or_phase=Phase.FAILED,
                reason="data_acquisition_gate_no_rework",
            )
            await self.save_task(task)
        await self._emit_progress(
            f"[Company:{self._projection_id_for_task(task)}] verification failed",
            task_id=task.id,
        )


    def _evaluate_data_acquisition_gate(
        self,
        task: Task,
        report: dict[str, Any],
        *,
        status: str,
        allowed: set[str],
        blocking: set[str],
        require_attempt_evidence: bool,
    ) -> tuple[bool, str]:
        if not status:
            return False, "Data acquisition report is missing or unreadable."
        present_inputs = self._normalize_data_acquisition_items(report.get("present_inputs", []))
        prepared_assets = self._normalize_data_acquisition_items(report.get("prepared_assets", []))
        attempted_sources = self._normalize_data_acquisition_items(report.get("attempted_sources", []))
        attempted_tools = self._normalize_data_acquisition_items(report.get("attempted_tools", []))
        blocked_reasons = self._normalize_data_acquisition_items(report.get("blocked_reasons", []))
        acquisition_attempted = bool(report.get("acquisition_attempted", False))
        is_media_task = requires_binary_asset_acquisition(task, report)
        designated_input_dir = str(report.get("designated_input_dir", "") or "").strip()
        download_manifest_path = str(report.get("download_manifest_path", "") or default_download_manifest_path(task)).strip()
        if status in {"ready", "already_present"}:
            if is_media_task:
                if has_downloaded_binary_asset(
                    task=task,
                    report=report,
                    download_manifest_path=download_manifest_path,
                    designated_input_dir=designated_input_dir,
                ):
                    return True, ""
                return False, (
                    "Data acquisition report is incomplete for a media task: ready/already_present requires "
                    "a download manifest with at least one downloaded binary asset prepared inside the workspace."
                )
            if present_inputs or prepared_assets:
                return True, ""
            return False, (
                "Data acquisition report is incomplete: ready/already_present requires explicit prepared assets "
                "or present inputs."
            )
        if status == "not_required":
            return True, ""
        if status in blocking:
            if not require_attempt_evidence:
                return False, f"Data acquisition readiness is blocking downstream execution: status `{status}`."
            if acquisition_attempted or attempted_sources or attempted_tools or prepared_assets or blocked_reasons:
                return False, f"Data acquisition readiness is blocking downstream execution: status `{status}`."
            return False, (
                "Data acquisition report is incomplete: blocking statuses require evidence of acquisition attempts, "
                "prepared assets, or documented blockers before the work item may stop."
            )
        return False, f"Data acquisition readiness is blocking downstream execution: status `{status}`."


    @staticmethod
    def _normalize_gate_feedback(feedback: str, *, fallback: str) -> str:
        text = str(feedback or "").strip()
        if not text:
            text = fallback
        return clip_text(
            text,
            limit=_MAX_GATE_REVIEW_FEEDBACK_CHARS,
            marker="gate feedback preview truncated",
        ).text


    def _build_gate_rework_record(
        self,
        *,
        review_task: Task,
        gate: WorkItemGatePolicy,
        reviewer_feedback: str,
        rework_round: int,
    ) -> dict[str, Any]:
        reviewer_role = str(
            gate.reviewer_role
            or review_task.assigned_to
            or review_task.metadata.get("work_item_role_id", "")
            or ""
        ).strip()
        review_projection_id = self._projection_id_for_task(review_task)
        rework_projection_id = rework_projection_id_for_gate(gate)
        return {
            "review_task_id": review_task.id,
            **gate_rework_payload(
                review_projection_id=review_projection_id,
                target_projection_id=rework_projection_id,
            ),
            "review_work_item_title": review_task.title,
            "reviewer_role": reviewer_role,
            "feedback": reviewer_feedback,
            "gate_instructions": gate.instructions,
            "rework_round": rework_round,
            "requested_at": datetime.now().isoformat(),
        }


    def _render_gate_rework_summary(self, rework_request: dict[str, Any]) -> str:
        review_work_item_title = str(rework_request.get("review_work_item_title", "")).strip() or "Gate review"
        reviewer_feedback = str(rework_request.get("feedback", "")).strip()
        gate_instructions = str(rework_request.get("gate_instructions", "")).strip()
        rework_round = int(rework_request.get("rework_round", 1) or 1)
        lines = [f"Rework requested by {review_work_item_title}.", f"Round: {rework_round}"]
        if reviewer_feedback:
            lines.append(f"## Reviewer Feedback\n{reviewer_feedback}")
        if gate_instructions:
            lines.append(f"## Gate Criteria\n{gate_instructions}")
        lines.append("Address ALL issues listed above before resubmitting.")
        return "\n\n".join(lines)


    async def prepare_gate_rework(
        self,
        review_task: Task,
        gate: WorkItemGatePolicy,
        task_by_projection_id: dict[str, Task],
        reviewer_feedback: str,
    ) -> Task | None:
        rework_count = int(review_task.metadata.get("gate_rework_count", 0))
        rework_projection_id = rework_projection_id_for_gate(gate)
        if gate.on_reject != "rework" or not rework_projection_id or rework_count >= gate.max_retries:
            return None

        rework_task = task_by_projection_id.get(rework_projection_id)
        if rework_task is None:
            return None

        normalized_feedback = self._normalize_gate_feedback(
            reviewer_feedback,
            fallback=(
                f"{review_task.title} requested changes. "
                "Review the gate criteria, address the issues, and resubmit."
            ),
        )
        rework_round = rework_count + 1
        rework_request = self._build_gate_rework_record(
            review_task=review_task,
            gate=gate,
            reviewer_feedback=normalized_feedback,
            rework_round=rework_round,
        )

        review_task.metadata = dict(review_task.metadata)
        review_task.metadata["gate_rework_count"] = rework_round
        review_task.metadata["last_gate_review_feedback"] = normalized_feedback
        review_task.metadata["last_gate_review_feedback_full"] = str(reviewer_feedback or "").strip()
        review_task.metadata["last_gate_rework_request"] = dict(rework_request)
        review_task.result = None
        review_task.context_snapshot = dict(review_task.context_snapshot)
        review_task.context_snapshot["last_gate_rework_request"] = dict(rework_request)
        # Review task now carries last_gate_review_feedback and projects to
        # Phase.READY_FOR_REWORK through the canonical transition helper.
        await transition_work_item_from_task(
            self.store, review_task,
            target_status_or_phase=Phase.READY_FOR_REWORK,
            reason="prepare_gate_rework_review_task",
        )

        rework_task.result = None
        rework_task.metadata = dict(rework_task.metadata)
        rework_task.metadata["gate_review_feedback"] = normalized_feedback
        rework_task.metadata["gate_review_feedback_full"] = str(reviewer_feedback or "").strip()
        rework_task.metadata["gate_instructions"] = gate.instructions
        rework_task.metadata["gate_rework_round"] = rework_round
        rework_task.metadata["gate_rework_request"] = dict(rework_request)
        rework_task.context_snapshot = dict(rework_task.context_snapshot)
        rework_task.context_snapshot["latest_gate_rework"] = dict(rework_request)
        # Rework task carries gate_review_feedback → Phase.READY_FOR_REWORK
        # (same convention). Dispatched to the worker's queue as a rework
        # card with feedback.
        await transition_work_item_from_task(
            self.store, rework_task,
            target_status_or_phase=Phase.READY_FOR_REWORK,
            reason="prepare_gate_rework_rework_task",
        )
        await self._append_progress(rework_task, self._render_gate_rework_summary(rework_request))
        # Emit work-item progress events so the UI reverts the cards from
        # "done" (checkmark) back to "active" (dots) during rework.
        rework_projection_label = self._projection_id_for_task(rework_task) or rework_projection_id
        await self._emit_progress(
            f"[Company:{rework_projection_label}] reworking (gate rework round {rework_round})",
            task_id=rework_task.id,
        )
        review_projection_label = self._projection_id_for_task(review_task)
        await self._emit_progress(
            f"[Company:{review_projection_label}] reworking (gate rework round {rework_round})",
            task_id=review_task.id,
        )
        return rework_task


    async def _save_checkpoint(self, task: Task, gate: WorkItemGatePolicy) -> None:
        if not self.checkpoint_callback or not self._active_plan:
            return
        runtime_payload = self._runtime_checkpoint_payload(task)
        work_item_payload = self._work_item_checkpoint_payload(task)
        prompt_override = str(dict(gate.metadata or {}).get("prompt_override", "") or "").strip()
        gate_metadata = dict(gate.metadata or {})
        gate_rework_projection_id = rework_projection_id_for_gate(gate)
        if gate_rework_projection_id:
            gate_metadata.update(
                gate_rework_payload(
                    rework_projection_id=gate_rework_projection_id,
                )
            )
        review_level = str(
            gate_metadata.get("review_level")
            or ("manager" if gate.reviewer_role and not gate.requires_human else "human")
        ).strip().lower() or "human"
        review_target_role_id = str(
            gate_metadata.get("review_target_role_id")
            or gate.reviewer_role
            or ""
        ).strip()
        review_chain_role_ids = [
            str(item).strip()
            for item in list(gate_metadata.get("review_chain_role_ids", []) or [])
            if str(item).strip()
        ]
        await self.checkpoint_callback(
            {
                "checkpoint_type": "company_work_item_gate",
                "project_id": task.project_id,
                "session_id": task.session_id,
                "task_id": task.id,
                "payload": {
                    "waiting_task_id": task.id,
                    "session_id": task.session_id,
                    **work_item_identity_payload_for_task(task),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "task_ids": [t.id for t in self._active_tasks],
                    "gate": {
                        "type": gate.gate_type,
                        "instructions": gate.instructions,
                        "reviewer_role": gate.reviewer_role,
                        "requires_human": gate.requires_human,
                        "on_reject": gate.on_reject,
                        "rework_projection_id": gate_rework_projection_id or None,
                        "max_retries": gate.max_retries,
                        "metadata": gate_metadata,
                    },
                    "prompt": prompt_override,
                    "review_level": review_level,
                    "review_target_role_id": review_target_role_id,
                    "review_chain_role_ids": review_chain_role_ids,
                    "basis_hash": self._checkpoint_basis_hash(task),
                    "company_work_item_plan": serialize_company_work_item_runtime_plan(self._active_plan),
                    **work_item_payload,
                    **runtime_payload,
                },
            }
        )


    async def _save_feedback_checkpoint(self, task: Task) -> None:
        if not self.checkpoint_callback:
            return
        if not self._is_final_human_acceptance_task(task):
            logger.debug(
                "_save_feedback_checkpoint skipped for non-final work item "
                f"task_id={task.id} projection_id={self._projection_id_for_task(task)}"
            )
            return
        active_plan = self._active_plan or CompanyWorkItemRuntimePlan(
            profile=str(task.metadata.get("company_profile", "") or "company"),
            projections=[],
        )
        active_tasks = list(self._active_tasks) or [task]
        if self._active_plan is None:
            self._active_plan = active_plan
        if not self._active_tasks:
            self._active_tasks = list(active_tasks)
        runtime_payload = self._runtime_checkpoint_payload(task)
        work_item_payload = self._work_item_checkpoint_payload(task)
        feedback_scope = str(task.metadata.get("feedback_scope", "") or "").strip()
        if not feedback_scope and self._is_authoritative_delivery_work_item(task):
            feedback_scope = "final"
        feedback_scope = feedback_scope or "final"
        feedback_kind = "final delivery" if feedback_scope == "final" else "work item"
        followup_message = str(task.metadata.get("feedback_followup_message", "") or "").strip()
        if isinstance(task.result, dict):
            result_content = str(task.result.get("content", "") or "").strip()
        elif task.result:
            result_content = str(task.result or "").strip()
        else:
            result_content = ""
        linked_work_item_id = linked_work_item_id_for_task(task)
        delivery_revision = task.metadata.get("delivery_revision", "")
        owner_directive_revision = task.metadata.get("owner_directive_revision", "")
        if followup_message:
            prompt = (
                f"{followup_message}\n\n"
                f"The {feedback_kind} remains open for self-evolution review. Use this card only to "
                "record full agreement, ignore, or feedback that should update employee experience."
            )
        else:
            prompt = (
                f"This {feedback_kind} is ready for self-evolution review.\n"
                "Use this card only to record full agreement, ignore, or feedback that should update employee experience."
            )
        await self.checkpoint_callback(
            {
                "checkpoint_type": "company_delivery_feedback",
                "project_id": task.project_id,
                "session_id": task.session_id,
                "task_id": task.id,
                "payload": {
                    "waiting_task_id": task.id,
                    "waiting_work_item_id": linked_work_item_id,
                    "session_id": task.session_id,
                    "task_ids": [t.id for t in active_tasks],
                    **work_item_identity_payload_for_task(task),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "feedback_scope": feedback_scope,
                    "prompt": prompt,
                    "review_level": "human",
                    "review_target_role_id": "owner",
                    "review_chain_role_ids": [],
                    "delivery_revision": delivery_revision,
                    "owner_directive_revision": owner_directive_revision,
                    "latest_user_directive": str(task.metadata.get("latest_user_directive", "") or "").strip(),
                    "result_content": result_content,
                    "basis_hash": self._checkpoint_basis_hash(task),
                    "company_work_item_plan": serialize_company_work_item_runtime_plan(active_plan),
                    **work_item_payload,
                    **runtime_payload,
                },
            }
        )


    async def _save_peer_checkpoint(self, task: Task) -> None:
        if not self.checkpoint_callback or not self._active_plan:
            return
        runtime_payload = self._runtime_checkpoint_payload(task)
        work_item_payload = self._work_item_checkpoint_payload(task)
        await self.checkpoint_callback(
            {
                "checkpoint_type": "company_peer_wait",
                "project_id": task.project_id,
                "session_id": task.session_id,
                "task_id": task.id,
                "payload": {
                    "waiting_task_id": task.id,
                    "session_id": task.session_id,
                    "task_ids": [t.id for t in self._active_tasks],
                    **work_item_identity_payload_for_task(task),
                    "org_version": task.metadata.get("org_version", 1),
                    "runtime_topology_version": task.metadata.get("runtime_topology_version", 1),
                    "reorg_proposal_id": task.metadata.get("reorg_proposal_id", ""),
                    "peer_wait": dict(task.metadata.get("peer_wait", {})),
                    "company_work_item_plan": serialize_company_work_item_runtime_plan(self._active_plan),
                    **work_item_payload,
                    **runtime_payload,
                },
            }
        )


    def _normalize_review_verdict(self, value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"approve", "approved", "pass", "passed", "accept", "accepted"}:
                return {"label": "approve", "summary": value.strip()}
            if lowered in {"reject", "rejected", "fail", "failed", "rework"}:
                return {"label": "reject", "summary": value.strip()}
            return {}
        if not isinstance(value, dict):
            return {}
        # Accept both the raw agent JSON shape (review_verdict|verdict|
        # decision|status) AND the already-normalized shape (label) emitted
        # by the external broker's adapter.infer_review_verdict.
        raw = str(
            value.get("review_verdict")
            or value.get("verdict")
            or value.get("decision")
            or value.get("status")
            or value.get("label")
            or ""
        ).strip().lower()
        if raw in {"approved", "pass", "passed", "accept", "accepted"}:
            raw = "approve"
        elif raw in {"rejected", "fail", "failed", "rework"}:
            raw = "reject"
        if raw not in {"approve", "reject"}:
            return {}
        blocking = value.get("blocking_issues", [])
        followups = value.get("followups", [])
        return {
            "label": raw,
            "summary": str(value.get("summary", "") or "").strip(),
            "blocking_issues": [
                str(item).strip()
                for item in (blocking if isinstance(blocking, list) else [])
                if str(item).strip()
            ][:8],
            "followups": [
                str(item).strip()
                for item in (followups if isinstance(followups, list) else [])
                if str(item).strip()
            ][:8],
        }


    def _structured_or_inferred_verdict(self, task: Task, gate: WorkItemGatePolicy) -> str:
        output_metadata = self._work_item_output_metadata_for_task(task)
        structured = self._normalize_review_verdict(
            output_metadata.get("structured_review_verdict")
            or task.metadata.get("structured_review_verdict")
        )
        if structured.get("label") in {"approve", "reject"}:
            return str(structured["label"])
        return "reject"


    def _review_feedback_with_fallback(self, review_task: Task) -> str:
        """Return the reviewer's feedback string, with a content
        fallback when the agent did not emit a structured verdict.

        Mirrors the gate path's salvage at ``_apply_review_gate``
        (search for ``not reviewer_feedback``). Lifted out of the
        inline ``_finalize_review_work_item`` block so unit tests
        can exercise the fallback without standing up the whole
        CompanyMode + dispatcher harness.
        """
        feedback = self._structured_review_feedback(review_task)
        if feedback:
            return feedback
        review_result = getattr(review_task, "result", None)
        if isinstance(review_result, dict):
            return str(review_result.get("content", "") or "").strip()
        if review_result is not None:
            return str(getattr(review_result, "content", "") or "").strip()
        return ""


    def _structured_review_feedback(self, task: Task) -> str:
        output_metadata = self._work_item_output_metadata_for_task(task)
        structured = self._normalize_review_verdict(
            output_metadata.get("structured_review_verdict")
            or task.metadata.get("structured_review_verdict")
        )
        if not structured:
            return ""
        lines: list[str] = []
        summary = str(structured.get("summary", "") or "").strip()
        if summary:
            lines.append(summary)
        blocking = list(structured.get("blocking_issues", []) or [])
        if blocking:
            lines.append("Blocking issues:")
            lines.extend(f"- {item}" for item in blocking)
        followups = list(structured.get("followups", []) or [])
        if followups:
            lines.append("Follow-ups:")
            lines.extend(f"- {item}" for item in followups)
        return "\n".join(lines).strip()


    def _build_verification_status(
        self,
        task: Task,
        result: TaskResult,
        *,
        review_verdict: dict[str, Any],
    ) -> dict[str, Any]:
        verification_evidence = dict(result.artifacts.get("verification_evidence", {}) if result.artifacts else {})
        if verification_evidence:
            label = "verified" if str(verification_evidence.get("verdict", "")).strip().lower() == "pass" else "not_verified"
            return {
                "label": label,
                "source": "runtime_verifier_evidence",
                "summary": str(verification_evidence.get("summary", "") or verification_evidence.get("raw_output", "") or "").strip(),
            }
        verification_entries = result.artifacts.get("verification", []) if result.artifacts else []
        if isinstance(verification_entries, list) and verification_entries:
            statuses: list[str] = []
            summaries: list[str] = []
            for item in verification_entries:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "") or "").strip()
                summary = str(item.get("summary", "") or item.get("verdict", "") or "").strip()
                if status:
                    statuses.append(status)
                if summary:
                    summaries.append(summary)
            label = "verified"
            if any(status in {"issues", "failed", "inconclusive"} for status in statuses):
                label = "not_verified"
            return {
                "label": label,
                "source": "runtime_verifier",
                "summary": "; ".join(summaries[:3]).strip(),
            }
        if review_verdict.get("label"):
            return {
                "label": f"review_{review_verdict['label']}",
                "source": "review_work_item",
                "summary": str(review_verdict.get("summary", "") or "").strip(),
            }
        explicit = task.metadata.get("work_item_verification_required")
        if explicit is False:
            return {
                "label": "not_required",
                "source": "work_item_policy",
                "summary": "This work item does not require a separate verification pass.",
            }
        return {}


    @staticmethod
    def _verification_evidence_satisfies_contract(verification_evidence: dict[str, Any]) -> bool:
        status = str(verification_evidence.get("status", "") or "").strip().lower()
        return status in {"provided", "unavailable"}


    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        value = str(text or "").strip()
        if value.startswith("```"):
            value = value.split("\n", 1)[1] if "\n" in value else value[3:]
            if value.endswith("```"):
                value = value[:-3]
        return value.strip()


    def _is_final_human_acceptance_metadata(
        self,
        metadata: Mapping[str, Any] | None,
        *,
        work_item_metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """Return True only for the final user-visible delivery acceptance card."""
        meta = dict(metadata or {})
        item_meta = dict(work_item_metadata or {})
        combined = {**item_meta, **meta}
        if self._metadata_flag_true(combined.get("attention_work_item", False)):
            return False
        if not self._metadata_flag_true(combined.get("authoritative_output", False)):
            return False
        if not self._metadata_flag_true(combined.get("user_visible", False)):
            return False
        if str(combined.get("feedback_scope", "") or "").strip().lower() != "final":
            return False
        return (
            work_item_turn_type_from_metadata(combined, fallback="") == "deliver"
            or is_delivery_turn(combined)
            or str(combined.get("review_owner_kind", "") or "").strip().lower() == "human"
        )


    def _is_final_human_acceptance_task(
        self,
        task: Task,
        work_item: Any | None = None,
    ) -> bool:
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        if str(task_metadata.get("execution_mode", "") or "").strip() != "company_mode":
            return False
        work_item_metadata = dict(getattr(work_item, "metadata", {}) or {}) if work_item is not None else None
        return self._is_final_human_acceptance_metadata(
            task_metadata,
            work_item_metadata=work_item_metadata,
        )


    def _is_authoritative_delivery_work_item(self, task: Task) -> bool:
        return self._is_final_human_acceptance_task(task)


    def _build_ceo_rework_record(
        self,
        *,
        source_task: Task,
        target_task: Task,
        feedback: str,
        rework_round: int,
        source: str,
    ) -> dict[str, Any]:
        target_projection_id = self._projection_id_for_task(target_task)
        return {
            "source": source,
            "requested_by_projection_id": self._projection_id_for_task(source_task),
            "requested_by_work_item_title": source_task.title,
            "requested_by_role_id": self._role_id_for_task(source_task),
            **gate_rework_payload(target_projection_id=target_projection_id),
            "target_work_item_title": target_task.title,
            "target_role_id": self._role_id_for_task(target_task),
            "feedback": feedback,
            "rework_round": rework_round,
            "requested_at": datetime.now().isoformat(),
        }


    def _render_ceo_rework_summary(self, rework_request: dict[str, Any]) -> str:
        target_work_item = str(
            rework_request.get("target_work_item_title", "")
            or "Current work item"
        ).strip()
        requested_by = str(
            rework_request.get("requested_by_work_item_title", "")
            or "Executive review"
        ).strip()
        feedback = str(rework_request.get("feedback", "") or "").strip()
        round_no = int(rework_request.get("rework_round", 1) or 1)
        lines = [
            f"Executive rework requested for {target_work_item}.",
            f"Requested by: {requested_by}",
            f"Round: {round_no}",
        ]
        if feedback:
            lines.append(f"## Executive Feedback\n{feedback}")
        lines.append("Resume your previous work session, address the issues above, and then resubmit this work item.")
        return "\n\n".join(lines)


    @staticmethod
    def _reset_work_item_outputs_for_rework(task: Task) -> None:
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
            "downstream_assignments",
            "artifacts",
            "automated_verification_results",
            "final_feedback_evaluation",
            "feedback_followup_message",
            "gate_harness_status",
            "gate_harness_constraints",
            "gate_harness_pending_decision",
            "gate_harness_decision",
            "gate_harness_evidence",
        ):
            task.metadata.pop(key, None)
        task.context_snapshot = dict(task.context_snapshot)
        for key in (
            "latest_artifacts",
            "delivery_package",
            "work_item_owned_outputs",
            "latest_ceo_rework",
            "upstream_ceo_rework_source_projection_id",
            "latest_gate_harness_rework",
            "upstream_gate_harness_rework_source_projection_id",
        ):
            task.context_snapshot.pop(key, None)


    def _fallback_ceo_pre_delivery_assessment(
        self,
        delivery_task: Task,
        tasks: list[Task],
        package: dict[str, Any],
    ) -> dict[str, Any]:
        blocking_projection_ids: list[str] = []
        for task in tasks:
            if task.id == delivery_task.id:
                continue
            if self._task_open_issues(task):
                blocking_projection_ids.append(self._projection_id_for_task(task))
        if not blocking_projection_ids:
            return {
                "deliverable": True,
                "summary": "No unresolved blocking work-item issues were detected before delivery.",
                "rework_targets": [],
            }
        return {
            "deliverable": False,
            "summary": (
                f"Delivery is not ready because the work-item runtime still has {len(package.get('open_issues', [])) or len(blocking_projection_ids)} "
                "open issue(s)."
            ),
            "rework_targets": [
                {
                    "target_projection_id": projection_id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                    "feedback": "Resolve the outstanding issues recorded in the work-item runtime before the final delivery is sent to the user.",
                }
                for projection_id in list(dict.fromkeys(blocking_projection_ids))
            ],
        }


    @staticmethod
    def _pre_delivery_assessment_unavailable(
        fallback: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        fallback_deliverable = bool(fallback.get("deliverable", True))
        summary = str(fallback.get("summary", "") or "").strip()
        payload = {
            "deliverable": fallback_deliverable,
            "summary": summary or "Pre-delivery assessment was unavailable.",
            "rework_targets": [],
            "assessment_status": "unavailable",
            "assessment_failure_kind": reason,
            "assessment_infrastructure_failure": True,
        }
        if not fallback_deliverable:
            payload["awaiting_human"] = True
            payload["summary"] = (
                f"{payload['summary']} Pre-delivery assessment could not produce a "
                "structured decision, so automatic rework is suspended."
            )
        return payload


    @staticmethod
    def _resolve_max_pre_delivery_reworks(task: Task) -> int:
        raw = getattr(task, "metadata", {}).get("max_pre_delivery_reworks", DEFAULT_MAX_PRE_DELIVERY_REWORKS)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_MAX_PRE_DELIVERY_REWORKS
        return max(0, value)


    def _resolve_ceo_rework_targets(
        self,
        raw_targets: Any,
        task_by_projection_id: dict[str, Task],
        *,
        fallback_projection_ids: list[str] | None = None,
        default_feedback: str = "",
    ) -> list[dict[str, str]]:
        resolved: list[dict[str, str]] = []
        seen_projection_ids: set[str] = set()
        items = list(raw_targets) if isinstance(raw_targets, list) else []
        for item in items:
            projection_id = ""
            role_id = ""
            feedback = default_feedback
            if isinstance(item, str):
                token = str(item).strip()
                if token in task_by_projection_id:
                    projection_id = token
                else:
                    role_id = token
            elif isinstance(item, dict):
                projection_id = str(
                    item.get("target_projection_id")
                    or item.get("work_item_projection_id")
                    or item.get("projection_id")
                    or ""
                ).strip()
                role_id = str(item.get("role_id", "") or "").strip()
                feedback = str(item.get("feedback", "") or item.get("reason", "") or default_feedback).strip()
            if not projection_id and role_id:
                matching = [
                    task
                    for task in list(self._active_tasks)
                    if self._role_id_for_task(task) == role_id
                ]
                matching.sort(
                    key=lambda task: (
                        0 if self._task_open_issues(task) else 1,
                        0 if task.status != TaskStatus.DONE else 1,
                        self._projection_id_for_task(task),
                    )
                )
                if matching:
                    projection_id = self._projection_id_for_task(matching[0])
            target_task = task_by_projection_id.get(projection_id)
            if target_task is None or projection_id in seen_projection_ids:
                continue
            seen_projection_ids.add(projection_id)
            resolved.append(
                {
                    "target_projection_id": projection_id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                    "role_id": self._role_id_for_task(target_task),
                    "feedback": feedback,
                }
            )
        if resolved:
            return resolved
        for projection_id in list(fallback_projection_ids or []):
            target_task = task_by_projection_id.get(projection_id)
            if target_task is None or projection_id in seen_projection_ids:
                continue
            seen_projection_ids.add(projection_id)
            resolved.append(
                {
                    "target_projection_id": projection_id,
                    **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                    "role_id": self._role_id_for_task(target_task),
                    "feedback": default_feedback,
                }
            )
        return resolved


    async def _ceo_pre_delivery_assessment(
        self,
        delivery_task: Task,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
        package: dict[str, Any],
    ) -> dict[str, Any]:
        fallback = self._fallback_ceo_pre_delivery_assessment(delivery_task, tasks, package)
        work_item_tasks: list[dict[str, Any]] = []
        for task in tasks:
            output_metadata = self._work_item_output_metadata_for_task(task)
            work_item_tasks.append(
                {
                    "task_id": task.id,
                    "projection_id": self._projection_id_for_task(task),
                    **work_item_identity_payload_for_task(task, fallback_turn_type=""),
                    "title": task.title,
                    "status": getattr(task.status, "value", str(task.status)),
                    "role_id": self._role_id_for_task(task),
                    "role_name": self._role_name_for_task(task),
                    "employee_assignment": dict(task.metadata.get("employee_assignment", {}) or {}),
                    "work_item_assignment": dict(task.metadata.get("work_item_assignment", {}) or {}),
                    "summary": self._task_summary_for_map(task),
                    "open_issues": self._task_open_issues(task),
                    "risks": [str(item).strip() for item in list(output_metadata.get("risks", []) or []) if str(item).strip()],
                    "dependency_projection_ids": list(task.dependencies),
                }
            )
        prompt = {
            "project_id": delivery_task.project_id,
            "company_profile": plan.profile,
            "delivery_projection_id": self._projection_id_for_task(delivery_task),
            "delivery_projection_title": delivery_task.title,
            "delivery_role_id": self._role_id_for_task(delivery_task),
            "delivery_role_name": self._role_name_for_task(delivery_task),
            "role_task_map": self._build_role_task_map(tasks),
            "delivery_package": package,
            "work_item_tasks": work_item_tasks,
        }
        raw = await self._run_role_prompt(
            source_task=delivery_task,
            system_prompt=EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT,
            payload=prompt,
            prompt_kind="ceo_pre_delivery_assessment",
            force_new_session=True,
        )
        data = self._parse_role_prompt_json(raw) if raw is not None else None
        if data is None and self.role_prompt_runner is not None:
            reason = "role_prompt_empty_result" if raw is None else "role_prompt_non_json_output"
            logger.debug("Executive pre-delivery assessment unavailable: {}", reason)
            return self._pre_delivery_assessment_unavailable(fallback, reason=reason)
        if data is None and self.role_prompt_runner is None and self.llm is not None:
            try:
                data = await call_llm_json_with_retry(
                    self.llm,
                    system=EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT,
                    payload=prompt,
                    task_type="quick_tasks",
                    label="ceo_pre_delivery_assessment",
                )
            except LLMRetryError as exc:
                logger.debug(f"Executive pre-delivery assessment failed after retries: {exc}")
                return fallback
            except Exception as exc:
                logger.debug(f"Executive pre-delivery assessment construction failed: {exc}")
                return fallback
        if data is None:
            logger.debug("Executive pre-delivery assessment returned non-JSON output")
            return fallback
        summary = str(data.get("summary", "") or fallback.get("summary", "")).strip()
        return {
            "deliverable": bool(data.get("deliverable", fallback.get("deliverable", True))),
            "summary": summary or str(fallback.get("summary", "")).strip(),
            "rework_targets": list(data.get("rework_targets", []) or []),
        }


    async def _ceo_initiate_rework(
        self,
        target_projection_id: str,
        feedback: str,
        task_by_projection_id: dict[str, Task],
        *,
        source_task: Task,
        source: str,
    ) -> Task | None:
        target_task = task_by_projection_id.get(target_projection_id)
        if target_task is None:
            return None
        normalized_feedback = self._normalize_gate_feedback(
            feedback,
            fallback=(
                f"{source_task.title} cannot move forward yet. "
                "Review the executive feedback, address the blocking issues, and resubmit."
            ),
        )
        rework_round = int(target_task.metadata.get("ceo_rework_count", 0) or 0) + 1
        rework_request = self._build_ceo_rework_record(
            source_task=source_task,
            target_task=target_task,
            feedback=normalized_feedback,
            rework_round=rework_round,
            source=source,
        )
        affected_projection_ids = [target_projection_id, *self._collect_downstream_projection_ids(target_projection_id)]
        touched_task_ids: set[str] = set()
        for affected_projection_id in affected_projection_ids:
            affected_task = task_by_projection_id.get(affected_projection_id)
            if affected_task is None or affected_task.id in touched_task_ids:
                continue
            touched_task_ids.add(affected_task.id)
            affected_task.metadata = dict(affected_task.metadata)
            affected_task.context_snapshot = dict(affected_task.context_snapshot)
            affected_task.result = None
            self._reset_work_item_outputs_for_rework(affected_task)
            if affected_projection_id == target_projection_id:
                affected_task.metadata["ceo_rework_count"] = rework_round
                affected_task.metadata["ceo_rework_feedback"] = normalized_feedback
                affected_task.metadata["ceo_rework_feedback_full"] = str(feedback or "").strip()
                affected_task.metadata["ceo_rework_request"] = dict(rework_request)
                history = list(affected_task.metadata.get("ceo_rework_requests", []) or [])
                history.append(dict(rework_request))
                affected_task.metadata["ceo_rework_requests"] = history[-6:]
                affected_task.context_snapshot["ceo_rework_feedback"] = normalized_feedback
                affected_task.context_snapshot["latest_ceo_rework"] = dict(rework_request)
                await self._append_progress(affected_task, self._render_ceo_rework_summary(rework_request))
            else:
                affected_task.metadata["upstream_ceo_rework_source_projection_id"] = target_projection_id
                affected_task.context_snapshot["upstream_ceo_rework_source_projection_id"] = target_projection_id
                adaptive = self._normalize_adaptive_metadata(affected_task.metadata.get("adaptive", {}))
                adaptive["normalized_state"] = "invalidated"
                affected_task.metadata["adaptive"] = adaptive
                await self._append_progress(
                    affected_task,
                    f"Reset because upstream work-item projection `{target_projection_id}` entered executive-directed rework.",
                )
            await transition_work_item_from_task(
                self.store, affected_task,
                target_status_or_phase=TaskStatus.PENDING,
                reason="ceo_rework_reset",
            )
            await self.save_task(affected_task)
            # Emit work_item_progress event so the UI reverts the work item from
            # "done" (checkmark) back to "active" (dots) during rework.
            await self._emit_progress(
                f"[Company:{affected_projection_id}] reworking ({source} rework round {rework_round})",
                task_id=affected_task.id,
            )
        return target_task


    def _infer_verdict(self, content: str, gate: WorkItemGatePolicy) -> str:
        # 1) Try structured JSON verdict first
        structured = self._extract_structured_verdict(content)
        if structured:
            return structured

        # 2) Keyword matching (improved)
        lower = content.lower()
        reject_terms = [
            "reject", "rejected", "needs changes", "needs change",
            "not approved", "blocked", "fail", "not ready",
            "rework needed", "does not meet", "insufficient",
        ]
        approve_terms = [
            "approve", "approved", "accepted", "pass",
            "looks good", "ready to proceed", "meets criteria",
        ]
        strict_gate = bool(gate.metadata.get("strict_gate_inference", False)) or (
            gate.instructions and "strict" in gate.instructions.lower()
        )
        has_reject = any(term in lower for term in reject_terms)
        has_approve = any(term in lower for term in approve_terms)

        if has_reject and has_approve:
            # Ambiguous: in strict mode reject wins; otherwise last-position wins
            if strict_gate:
                return "reject"
            last_reject = max((lower.rfind(t) for t in reject_terms if t in lower), default=-1)
            last_approve = max((lower.rfind(t) for t in approve_terms if t in lower), default=-1)
            return "reject" if last_reject > last_approve else "approve"
        if has_reject:
            return "reject"
        if has_approve:
            return "approve"
        # No keywords: strict mode defaults to reject, otherwise approve
        return "reject" if strict_gate else "approve"


    @staticmethod
    def _extract_structured_verdict(content: str) -> str | None:
        """Extract verdict from a JSON object embedded in the content."""
        import json as _json

        start = content.find("{")
        while start != -1:
            end = content.find("}", start)
            if end == -1:
                break
            try:
                data = _json.loads(content[start : end + 1])
                raw = str(
                    data.get("verdict") or data.get("decision") or data.get("status") or ""
                ).lower().strip()
                if raw in ("reject", "rejected", "fail", "failed", "rework"):
                    return "reject"
                if raw in ("approve", "approved", "pass", "passed", "accept", "accepted"):
                    return "approve"
            except (ValueError, AttributeError):
                pass
            start = content.find("{", start + 1)
        return None


    def _gate_from_metadata(self, gate_data: dict[str, Any] | None) -> WorkItemGatePolicy | None:
        if not gate_data:
            return None
        gate_metadata = dict(gate_data.get("metadata", {}) or {})
        rework_projection_id = str(
            gate_data.get("rework_projection_id")
            or gate_metadata.get("rework_projection_id")
            or ""
        ).strip()
        gate = WorkItemGatePolicy(
            gate_type=str(gate_data.get("type", "review") or "review"),
            instructions=gate_data.get("instructions", ""),
            reviewer_role=gate_data.get("reviewer_role"),
            requires_human=bool(gate_data.get("requires_human", False)),
            on_reject=gate_data.get("on_reject", "halt"),
            rework_projection_id=rework_projection_id or None,
            max_retries=int(gate_data.get("max_retries", 1)),
            metadata=gate_metadata,
        )
        if rework_projection_id:
            mark_gate_rework_projection(gate, rework_projection_id)
        return gate


