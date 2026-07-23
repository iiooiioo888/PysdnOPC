"""CompanyModeMixin — 公司模式委派/運行時相關方法。"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from opc.core.models import (
    DelegationCell,
    DelegationRun,
    DelegationWorkItem,
    ExecutionMode,
    ModeSelection,
    Phase,
    Task,
    TaskStatus,
    TeamInstance,
)
from opc.core.config import DEFAULT_ORGANIZATION_ID
from opc.core.models import (
    CompanyProfile,
    DelegationEvent,
    ExecutionCheckpoint,
    RoleRuntimeSession,
    SeatState,
    SessionLinkRecord,
    WorkItemExecutionStrategy,
)
from opc.engine._core import _COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES, _WAITING_TASK_STATUSES
from opc.layer2_organization.company_mode import serialized_company_plan_from_metadata
from opc.layer2_organization.company_runtime_identity import is_company_runtime_task
from opc.layer2_organization.metadata_ownership import build_work_item_owner_execution_copy
from opc.layer2_organization.prompt_contract import make_prompt_contract
from opc.layer2_organization.recruiter import (
    normalize_recruitment_agent_choice,
    resolve_effective_execution_agent,
)
from opc.layer2_organization.session_scoping import task_session_scope_id
from opc.layer2_organization.work_item_identity import (
    canonical_work_item_turn_type_for_kind,
    mark_work_item_projection,
    turn_type_for_task,
    turn_type_for_work_item,
    work_item_identity_payload,
    work_item_projection_id_from_metadata,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer2_organization.work_item_runtime import is_work_item_runtime_metadata
from opc.layer2_organization.work_item_runtime_invariants import (
    validate_work_item_runtime_projection,
)
from opc.layer2_organization.company_mode import (
    CompanyRuntimeSpec,
    CompanyWorkItemExecutor,
    deserialize_company_runtime_spec,
    deserialize_company_work_item_runtime_plan,
    serialize_company_runtime_spec,
    serialize_company_work_item_runtime_plan,
)
from opc.layer2_organization.company_runtime import canonical_role_session_id
from opc.layer2_organization.company_runtime_identity import (
    build_company_runtime_identity_index,
    load_company_runtime_identity_index,
)
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)
from opc.layer2_organization.phase import DONE_PHASES, IN_PROGRESS_PHASES, task_status_for_phase
from opc.layer2_organization.work_item_identity import (
    mark_projected_work_item_task,
    projection_id_for_task,
    projection_id_for_work_item,
)
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer2_organization.work_item_runtime import mark_work_item_runtime

if TYPE_CHECKING:
    from opc.engine._core import OPCEngine


class CompanyModeMixin:
    """Mixin providing 公司模式委派/運行時相關方法 for OPCEngine."""

    async def _continue_company_mode_execution(
        self,
        decision: ModeSelection,
        original_message: str,
        runtime_spec: CompanyRuntimeSpec,
        *,
        session_id: str,
        origin_channel: str = "cli",
        origin_chat_id: str = "",
        origin_thread_id: str = "",
        origin_task_id: str | None = None,
        staffing_overrides: dict[str, str] | None = None,
        staffing_experience_modes: dict[str, str] | None = None,
        fallback_role_ids: set[str] | None = None,
        role_agent_overrides: dict[str, str] | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
    ) -> str:
        assert self.store and self.memory
        project_id = self.project_id or "default"
        attachment_refs = self._normalize_attachment_refs(attachment_refs)
        attachment_context = self._build_attachment_context(attachment_refs)
        workspace_contract = await self._resolve_workspace_contract(original_message, session_id)
        target_output_dir = str(workspace_contract.get("output_root") or "").strip() or None
        force_native_execution = decision.preferred_agent == "native"
        await self._sync_origin_task_execution_context(
            origin_task_id,
            session_id=session_id,
            decision=decision,
            workspace_contract=workspace_contract,
            original_message=original_message,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_thread_id=origin_thread_id,
            attachment_refs=attachment_refs,
        )
        secretary_context = ""
        await self._remember_session_execution_defaults(
            session_id,
            decision,
            target_output_dir=target_output_dir,
            workspace_root=workspace_contract.get("workspace_root"),
            comms_workspace_root=workspace_contract.get("comms_workspace_root"),
            comms_root=workspace_contract.get("comms_root"),
        )
        runtime_topology = self.org_engine.build_runtime_delegation_topology() if self.org_engine else {}
        runtime_topology = self._enrich_runtime_delegation_topology(
            runtime_topology=runtime_topology,
            decision=decision,
            project_id=project_id,
            staffing_overrides=staffing_overrides,
            staffing_experience_modes=staffing_experience_modes,
            fallback_role_ids=fallback_role_ids,
            role_agent_overrides=role_agent_overrides,
        )
        company_profile = str(
            getattr(runtime_spec, "profile", "")
            or getattr(self.config.org, "company_profile", "")
            or ""
        ).strip()
        company_work_item_plan: CompanyWorkItemRuntimePlan | None = None
        if self.org_engine:
            try:
                company_work_item_plan = self.org_engine.build_company_work_item_runtime_plan(
                    company_profile or CompanyProfile.CORPORATE.value,
                    runtime_topology=runtime_topology,
                    original_request=original_message,
                )
                runtime_topology["company_work_item_plan"] = serialize_company_work_item_runtime_plan(company_work_item_plan)
                runtime_topology["runtime_blueprint_source"] = "company_work_item_runtime_plan"
            except ValueError as exc:
                return f"Cannot execute company mode: {exc}"
        final_decider_role_id = str(runtime_topology.get("final_decider_role_id", "") or "").strip()
        if not final_decider_role_id:
            return "Cannot execute company mode: no final decider role is available."
        delegation_playbook = self._build_runtime_delegation_playbook(
            runtime_spec=runtime_spec,
            decision=decision,
            original_message=original_message,
            staffing_overrides=staffing_overrides,
            staffing_experience_modes=staffing_experience_modes,
            fallback_role_ids=fallback_role_ids,
            role_agent_overrides=role_agent_overrides,
        )
        if company_work_item_plan is not None:
            delegation_playbook["company_work_item_plan"] = serialize_company_work_item_runtime_plan(company_work_item_plan)
            delegation_playbook["runtime_blueprint_source"] = "company_work_item_runtime_plan"
        seat_force_native_flags = [
            bool(seat.get("force_native_execution", False))
            for seat in list(runtime_topology.get("seats", []) or [])
            if isinstance(seat, dict)
        ]
        runtime_force_native_execution = bool(seat_force_native_flags) and all(seat_force_native_flags)
        delegation_run_id, root_work_item = await self._bootstrap_runtime_delegation_run(
            session_id=session_id,
            project_id=project_id,
            runtime_spec=runtime_spec,
            original_message=original_message,
            runtime_topology=runtime_topology,
            work_item_plan=company_work_item_plan,
            delegation_playbook=delegation_playbook,
            target_output_dir=target_output_dir,
            comms_workspace_root=str(workspace_contract.get("comms_workspace_root") or "").strip(),
            force_native_execution=runtime_force_native_execution,
        )
        root_task = await self._ensure_runtime_work_item_task(
            work_item=root_work_item,
            parent_session_id=session_id,
            original_message=original_message,
            decision=decision,
            runtime_topology=runtime_topology,
            delegation_playbook=delegation_playbook,
            secretary_context=secretary_context,
            target_output_dir=target_output_dir,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_thread_id=origin_thread_id,
            origin_task_id=origin_task_id,
            attachment_refs=attachment_refs,
            attachment_context=attachment_context,
            force_native_execution=runtime_force_native_execution,
            root_session=True,
        )
        root_task.metadata["delegation_run_id"] = delegation_run_id
        await self.store.save_task(root_task)
        if self.on_company_runtime_children:
            self.on_company_runtime_children(session_id, [root_task.id])
        return await self._execute_company_mode([root_task], runtime_spec)

    def _build_runtime_delegation_playbook(
        self,
        *,
        runtime_spec: CompanyRuntimeSpec | None,
        decision: ModeSelection,
        original_message: str,
        staffing_overrides: dict[str, str] | None = None,
        staffing_experience_modes: dict[str, str] | None = None,
        fallback_role_ids: set[str] | None = None,
        role_agent_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        playbook_cfg = getattr(getattr(self.config, "org", None), "delegation_playbook", None)
        if hasattr(playbook_cfg, "model_dump"):
            playbook: dict[str, Any] = dict(playbook_cfg.model_dump())
        elif isinstance(playbook_cfg, dict):
            playbook = dict(playbook_cfg)
        else:
            playbook = {}
        playbook.setdefault("goal", "multi_team_org")
        playbook.setdefault("runtime_model", "multi_team_org")
        playbook["work_item_driven"] = True
        playbook.setdefault("original_message", original_message)
        if runtime_spec is not None:
            playbook["company_profile"] = str(runtime_spec.profile or "corporate").strip() or "corporate"
            playbook["runtime_spec"] = serialize_company_runtime_spec(runtime_spec)
        playbook["requested_sub_tasks"] = self._normalize_sub_tasks(getattr(decision, "sub_tasks", []))
        playbook["recruitment_staffing_overrides"] = {
            str(role_id).strip(): str(employee_id).strip()
            for role_id, employee_id in dict(staffing_overrides or {}).items()
            if str(role_id).strip() and str(employee_id).strip()
        }
        playbook["recruitment_staffing_experience_modes"] = {
            str(role_id).strip(): self._normalize_staffing_experience_mode(mode)
            for role_id, mode in dict(staffing_experience_modes or {}).items()
            if str(role_id).strip()
        }
        playbook["recruitment_fallback_role_ids"] = [
            role_id
            for role_id in sorted(
                {
                    str(role_id).strip()
                    for role_id in list(fallback_role_ids or [])
                    if str(role_id).strip()
                }
            )
        ]
        playbook["recruitment_role_agent_overrides"] = {
            str(role_id).strip(): str(agent_name).strip()
            for role_id, agent_name in dict(role_agent_overrides or {}).items()
            if str(role_id).strip() and str(agent_name).strip()
        }
        return playbook

    def _build_runtime_root_description(
        self,
        *,
        original_message: str,
        decision: ModeSelection,
    ) -> str:
        sections = [
            "## Global Intent Summary",
            " ".join(str(original_message or "").split()) or "Complete the requested work.",
            "",
            "## Runtime Model",
            "Company mode is driven by work items. Create downstream work with delegate_work; completed child work returns to leaders for review and synthesis.",
        ]
        normalized_sub_tasks = self._normalize_sub_tasks(getattr(decision, "sub_tasks", []))
        if normalized_sub_tasks:
            sections.extend(
                [
                    "",
                    "## Requested Subtasks",
                    *[
                        f"{index}. {item['description']}"
                        for index, item in enumerate(normalized_sub_tasks, start=1)
                    ],
                ]
            )
        return "\n".join(sections)

    async def _prepare_project_run_context(
        self,
        *,
        project_id: str,
        session_id: str,
    ) -> tuple[DelegationRun | None, int, dict[str, Any]]:
        previous_run: DelegationRun | None = None
        if self.store is not None:
            if hasattr(self.store, "get_latest_delegation_run"):
                previous_run = await self.store.get_latest_delegation_run(
                    project_id,
                    include_session_id=session_id,
                )
            elif hasattr(self.store, "list_delegation_runs"):
                prior_runs = await self.store.list_delegation_runs(project_id=project_id)
                for candidate in prior_runs:
                    if candidate.session_id != session_id:
                        previous_run = candidate
                        break
        current_revision = 1
        if previous_run is not None:
            current_revision = max(1, int(getattr(previous_run, "current_revision", 1) or 1))
            if str(getattr(previous_run, "lifecycle_status", "") or "").strip() in {"deliverable", "delivered"}:
                current_revision += 1
        dossier: dict[str, Any] = {}
        if self.memory is not None and hasattr(self.memory, "build_project_dossier"):
            try:
                dossier = await self.memory.build_project_dossier(
                    project_id=project_id,
                    run_id=getattr(previous_run, "run_id", "") or None,
                    session_id=getattr(previous_run, "session_id", "") or None,
                )
            except Exception:
                dossier = {}
        return previous_run, current_revision, dossier

    def _enrich_runtime_delegation_topology(
        self,
        *,
        runtime_topology: dict[str, Any],
        decision: ModeSelection,
        project_id: str,
        staffing_overrides: dict[str, str] | None = None,
        staffing_experience_modes: dict[str, str] | None = None,
        fallback_role_ids: set[str] | None = None,
        role_agent_overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.org_engine:
            return runtime_topology
        enriched = copy.deepcopy(runtime_topology)
        fallback_roles = {
            str(role_id).strip()
            for role_id in list(fallback_role_ids or [])
            if str(role_id).strip()
        }
        explicit_agent_choice = normalize_recruitment_agent_choice(decision.preferred_agent)
        explicit_external_agent = (
            explicit_agent_choice
            if explicit_agent_choice and explicit_agent_choice != "native"
            else None
        )
        explicit_force_native = explicit_agent_choice == "native"
        seats: list[dict[str, Any]] = []
        for raw_seat in list(enriched.get("seats", []) or []):
            seat = dict(raw_seat or {})
            role_id = str(seat.get("role_id", "") or "").strip()
            if not role_id:
                seats.append(seat)
                continue
            employee_assignment: dict[str, Any] = {}
            preferred_employee_id = str((staffing_overrides or {}).get(role_id, "") or "").strip()
            selected_employee = None
            if preferred_employee_id:
                selected_employee = self.org_engine.get_employee(preferred_employee_id)
            if selected_employee is None and role_id in fallback_roles and not preferred_employee_id:
                selected_employee = self.org_engine.ensure_fallback_employee_for_role(role_id, persist=False)
            if selected_employee is None:
                selected_employee = self.org_engine.get_default_employee_for_role(role_id)
            if selected_employee is None:
                candidates = [
                    employee
                    for employee in self.org_engine.list_employees(role_id=role_id)
                    if not dict(employee.metadata or {}).get("is_fallback_employee")
                ]
                selected_employee = candidates[0] if candidates else None
            if selected_employee is not None and hasattr(self.org_engine, "_build_employee_assignment"):
                experience_mode = self._normalize_staffing_experience_mode(
                    dict(staffing_experience_modes or {}).get(role_id)
                )
                employee_assignment = self.org_engine._build_employee_assignment(  # type: ignore[attr-defined]
                    selected_employee,
                    role_id=role_id,
                    domains=[],
                    project_id=project_id,
                    experience_mode=experience_mode,
                )
            selected_role_agent = normalize_recruitment_agent_choice(
                (role_agent_overrides or {}).get(role_id)
            )
            preferred_external_agent = explicit_external_agent
            if selected_role_agent:
                preferred_external_agent = None if selected_role_agent == "native" else selected_role_agent
            elif explicit_agent_choice:
                pass
            elif not preferred_external_agent:
                preferred_external_agent = str(
                    getattr(self.org_engine.get_agent(role_id), "preferred_external_agent", "") or ""
                ).strip() or None
            force_native_execution = bool(
                selected_role_agent == "native"
                or (explicit_force_native and not selected_role_agent)
            )
            selected_execution_agent = (
                selected_role_agent
                or explicit_agent_choice
                or ("native" if force_native_execution or not preferred_external_agent else preferred_external_agent)
            )
            execution_agent_locked = bool(selected_role_agent or explicit_agent_choice)
            selection_source = (
                "recruitment_user_override"
                if selected_role_agent
                else "explicit_user_agent"
                if explicit_agent_choice
                else ""
            )
            seat["employee_id"] = str(employee_assignment.get("employee_id", "") or seat.get("employee_id", "") or "").strip()
            seat["employee_assignment"] = dict(employee_assignment or {})
            seat["preferred_external_agent"] = preferred_external_agent
            seat["selected_execution_agent"] = selected_execution_agent
            seat["execution_agent_locked"] = execution_agent_locked
            seat["selected_execution_agent_source"] = selection_source
            seat["force_native_execution"] = force_native_execution
            seat["metadata"] = {
                **dict(seat.get("metadata", {}) or {}),
                "employee_prompt_context": str((employee_assignment or {}).get("prompt_context", "")).strip(),
                "employee_delta_context": str((employee_assignment or {}).get("delta_context", "")).strip(),
                "preferred_external_agent": preferred_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_agent_locked": execution_agent_locked,
                "selected_execution_agent_source": selection_source,
            }
            seats.append(seat)
        enriched["seats"] = seats
        return enriched

    @staticmethod
    def _select_runtime_work_item_seat(
        runtime_topology: dict[str, Any],
        *,
        role_id: str,
        manager_role_id: str = "",
        work_item_turn_type: str = "execute",
    ) -> dict[str, Any]:
        seats = [
            dict(seat)
            for seat in list(runtime_topology.get("seats", []) or [])
            if str(seat.get("role_id", "") or "").strip() == str(role_id or "").strip()
        ]
        if not seats:
            return {}
        if work_item_turn_type in {"intake", "dispatch", "plan", "aggregate", "deliver"}:
            lead_seat = next((seat for seat in seats if bool(seat.get("is_team_lead", False))), None)
            if lead_seat is not None:
                return lead_seat
        if manager_role_id:
            manager_match = next(
                (
                    seat
                    for seat in seats
                    if str(seat.get("manager_role_id", "") or "").strip() == manager_role_id
                ),
                None,
            )
            if manager_match is not None:
                return manager_match
        lead_seat = next((seat for seat in seats if bool(seat.get("is_team_lead", False))), None)
        if lead_seat is not None:
            return lead_seat
        return seats[0]

    async def _bootstrap_runtime_delegation_run(
        self,
        *,
        session_id: str,
        project_id: str,
        runtime_spec: CompanyRuntimeSpec | None,
        original_message: str,
        runtime_topology: dict[str, Any],
        work_item_plan: CompanyWorkItemRuntimePlan | None,
        delegation_playbook: dict[str, Any],
        target_output_dir: str | None,
        comms_workspace_root: str,
        force_native_execution: bool,
    ) -> tuple[str, DelegationWorkItem]:
        assert self.store and self.org_engine
        final_decider_role_id = str(runtime_topology.get("final_decider_role_id", "") or "").strip()
        previous_run, current_revision, project_dossier = await self._prepare_project_run_context(
            project_id=project_id,
            session_id=session_id,
        )
        run = DelegationRun(
            project_id=project_id,
            session_id=session_id,
            company_profile=str(
                runtime_topology.get("company_profile", "")
                or (runtime_spec.profile if runtime_spec is not None else "")
                or getattr(self.config.org, "company_profile", "corporate")
            ),
            execution_model="multi_team_org",
            final_decider_role_id=final_decider_role_id,
            top_level_role_ids=list(runtime_topology.get("top_level_role_ids", []) or self.org_engine.get_top_level_role_ids()),
            status="running",
            lifecycle_status="active",
            current_revision=current_revision,
            latest_deliverable_summary=str(getattr(previous_run, "latest_deliverable_summary", "") or "").strip(),
            recovery_pointer={
                "status": "bootstrapping",
                "session_id": session_id,
                "project_id": project_id,
            },
            project_dossier=dict(project_dossier or {}),
            metadata=mark_work_item_runtime({
                "runtime_model": "multi_team_org",
                "source": "multi_team_org",
                "runtime_spec": serialize_company_runtime_spec(runtime_spec) if runtime_spec is not None else {},
                "delegation_playbook": dict(delegation_playbook),
                "runtime_topology": copy.deepcopy(runtime_topology),
                "company_work_item_plan": serialize_company_work_item_runtime_plan(work_item_plan),
                "org_snapshot": copy.deepcopy(runtime_topology),
                "target_output_dir": target_output_dir,
                "comms_workspace_root": comms_workspace_root,
                "force_native_execution": force_native_execution,
                "continuation_of_run_id": str(getattr(previous_run, "run_id", "") or "").strip(),
                "continuation_of_session_id": str(getattr(previous_run, "session_id", "") or "").strip(),
                "loaded_from_dossier": bool(project_dossier),
            }),
        )
        await self.store.save_delegation_run(run)
        if previous_run is not None and session_id != previous_run.session_id and hasattr(self.store, "save_session_link"):
            await self.store.save_session_link(
                SessionLinkRecord(
                    project_id=project_id,
                    session_id=session_id,
                    linked_session_id=previous_run.session_id,
                    link_type="continuation_of",
                    metadata={
                        "run_id": run.run_id,
                        "linked_run_id": previous_run.run_id,
                    },
                )
            )
            if str(getattr(previous_run, "lifecycle_status", "") or "").strip() in {"deliverable", "delivered"} or str(getattr(previous_run, "latest_deliverable_summary", "") or "").strip():
                await self.store.save_session_link(
                    SessionLinkRecord(
                        project_id=project_id,
                        session_id=session_id,
                        linked_session_id=previous_run.session_id,
                        link_type="revision_of",
                        metadata={
                            "run_id": run.run_id,
                            "linked_run_id": previous_run.run_id,
                            "revision": current_revision,
                        },
                    )
                )

        teams = [dict(item) for item in list(runtime_topology.get("teams", []) or []) if isinstance(item, dict)]
        seats = [dict(item) for item in list(runtime_topology.get("seats", []) or []) if isinstance(item, dict)]
        team_instance_ids: dict[str, str] = {}
        seats_by_id: dict[str, dict[str, Any]] = {}

        for team in teams:
            team_id = str(team.get("team_id", "") or "").strip()
            if not team_id:
                continue
            team_instance_id = str(team.get("team_instance_id", "") or f"team-instance::{run.run_id}::{team_id}").strip()
            team_instance_ids[team_id] = team_instance_id
            await self.store.save_team_instance(
                TeamInstance(
                    team_instance_id=team_instance_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_id=team_id,
                    session_id=session_id,
                    status="active",
                    seat_ids=[str(item).strip() for item in list(team.get("member_seat_ids", []) or []) if str(item).strip()],
                    role_ids=[
                        str(item).strip()
                        for item in {
                            str(team.get("lead_role_id", "") or "").strip(),
                            *[
                                str(item).strip()
                                for item in list(team.get("member_role_ids", []) or [])
                                if str(item).strip()
                            ],
                        }
                        if str(item).strip()
                    ],
                    metadata=mark_work_item_runtime({
                        "parent_team_id": str(team.get("parent_team_id", "") or "").strip(),
                        "lead_role_id": str(team.get("lead_role_id", "") or "").strip(),
                        **dict(team.get("metadata", {}) or {}),
                    }),
                )
            )
            await self.store.save_delegation_cell(
                DelegationCell(
                    cell_id=team_id,
                    run_id=run.run_id,
                    manager_role_id=str(team.get("lead_role_id", "") or "").strip(),
                    member_role_ids=[str(item).strip() for item in list(team.get("member_role_ids", []) or []) if str(item).strip()],
                    status="active",
                    metadata=mark_work_item_runtime({
                        "team_id": team_id,
                        "team_instance_id": team_instance_id,
                        "parent_team_id": str(team.get("parent_team_id", "") or "").strip(),
                        "lead_role_id": str(team.get("lead_role_id", "") or "").strip(),
                        "member_seat_ids": list(team.get("member_seat_ids", []) or []),
                        **dict(team.get("metadata", {}) or {}),
                    }),
                )
            )

        for seat in seats:
            seat_id = str(seat.get("seat_id", "") or "").strip()
            role_id = str(seat.get("role_id", "") or "").strip()
            if not seat_id or not role_id:
                continue
            team_id = str(seat.get("team_id", "") or "").strip()
            employee_id = str(seat.get("employee_id", "") or "").strip() or f"{role_id}-default-session"
            team_instance_id = team_instance_ids.get(team_id, "")
            seat_state_id = str(seat.get("seat_state_id", "") or f"seat-state::{run.run_id}::{seat_id}").strip()
            # Role-instance model: one role → one role_runtime_session_id, shared across seats.
            # Fix 2: canonical fallback includes team_instance so parallel
            # role instances in the same run don't collide on a short ID.
            role_runtime_session_id = (
                str(seat.get("role_runtime_session_id", "") or "").strip()
                or canonical_role_session_id(
                    run_id=run.run_id,
                    role_id=role_id,
                    team_instance_id=team_instance_id,
                )
            )
            seats_by_id[seat_id] = {
                **seat,
                "seat_state_id": seat_state_id,
                "role_runtime_session_id": role_runtime_session_id,
                "team_instance_id": team_instance_id,
            }
            await self.store.save_seat_state(
                SeatState(
                    seat_state_id=seat_state_id,
                    team_instance_id=team_instance_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_id=team_id,
                    seat_id=seat_id,
                    role_id=role_id,
                    employee_id=employee_id,
                    member_session_id=f"role-session::{project_id}::{role_id}",
                    role_runtime_session_id=role_runtime_session_id,
                    status="idle",
                    resident_status="idle",
                    manager_role_id=str(seat.get("manager_role_id", "") or "").strip(),
                    manager_seat_id=str(seat.get("manager_seat_id", "") or "").strip(),
                    manager_role_ids=[
                        str(item).strip()
                        for item in {
                            str(seat.get("manager_role_id", "") or "").strip(),
                            *[
                                str(item).strip()
                                for item in list(seat.get("contact_role_ids", []) or [])
                                if str(item).strip()
                            ],
                        }
                        if str(item).strip()
                    ],
                    manager_seat_ids=[
                        str(item).strip()
                        for item in [str(seat.get("manager_seat_id", "") or "").strip()]
                        if str(item).strip()
                    ],
                    metadata=mark_work_item_runtime({
                        "managed_team_id": str(seat.get("managed_team_id", "") or "").strip(),
                        "allowed_delegate_role_ids": list(seat.get("allowed_delegate_role_ids", []) or []),
                        "contact_role_ids": list(seat.get("contact_role_ids", []) or []),
                        **dict(seat.get("metadata", {}) or {}),
                    }),
                )
            )
            await self.store.save_delegation_role_session(
                RoleRuntimeSession(
                    role_session_id=role_runtime_session_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_instance_id=team_instance_id,
                    team_id=team_id,
                    role_id=role_id,
                    seat_id=seat_id,
                    seat_state_id=seat_state_id,
                    employee_id=employee_id,
                    manager_role_ids=[
                        str(item).strip()
                        for item in {
                            str(seat.get("manager_role_id", "") or "").strip(),
                            *[
                                str(item).strip()
                                for item in list(seat.get("contact_role_ids", []) or [])
                                if str(item).strip()
                            ],
                        }
                        if str(item).strip()
                    ],
                    manager_seat_ids=[
                        str(item).strip()
                        for item in [str(seat.get("manager_seat_id", "") or "").strip()]
                        if str(item).strip()
                    ],
                    seat_ids=[seat_id],
                    status="idle",
                    metadata=mark_work_item_runtime({
                        "shared_role_executor": bool(seat.get("shared_executor", True)),
                        "final_decider_role_id": final_decider_role_id,
                        "managed_team_id": str(seat.get("managed_team_id", "") or "").strip(),
                        "contact_role_ids": list(seat.get("contact_role_ids", []) or []),
                    }),
                )
            )

        final_decider_seat = next(
            (
                seat
                for seat in seats_by_id.values()
                if str(seat.get("role_id", "") or "").strip() == final_decider_role_id
            ),
            {},
        )
        root_team_id = str(final_decider_seat.get("team_id", "") or f"team::{final_decider_role_id}").strip()
        root_seat_id = str(final_decider_seat.get("seat_id", "") or f"seat::{root_team_id}::{final_decider_role_id}").strip()
        root_team_instance_id = str(final_decider_seat.get("team_instance_id", "") or team_instance_ids.get(root_team_id, f"team-instance::{run.run_id}::{root_team_id}")).strip()
        root_seat_state_id = str(final_decider_seat.get("seat_state_id", "") or f"seat-state::{run.run_id}::{root_seat_id}").strip()
        # Role-instance model: role_runtime_session_id is keyed by role, not seat.
        # Fix 2: canonical fallback with team_instance slot.
        root_role_runtime_id = (
            str(final_decider_seat.get("role_runtime_session_id", "") or "").strip()
            or canonical_role_session_id(
                run_id=run.run_id,
                role_id=final_decider_role_id,
                team_instance_id=root_team_instance_id,
            )
        )

        if root_team_id not in team_instance_ids:
            team_instance_ids[root_team_id] = root_team_instance_id
            await self.store.save_team_instance(
                TeamInstance(
                    team_instance_id=root_team_instance_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_id=root_team_id,
                    session_id=session_id,
                    status="active",
                    seat_ids=[root_seat_id],
                    role_ids=[final_decider_role_id],
                    metadata=mark_work_item_runtime(),
                )
            )
            await self.store.save_delegation_cell(
                DelegationCell(
                    cell_id=root_team_id,
                    run_id=run.run_id,
                    manager_role_id=final_decider_role_id,
                    member_role_ids=[final_decider_role_id],
                    status="active",
                    metadata=mark_work_item_runtime({"team_instance_id": root_team_instance_id}),
                )
            )
        if root_seat_id not in seats_by_id:
            await self.store.save_seat_state(
                SeatState(
                    seat_state_id=root_seat_state_id,
                    team_instance_id=root_team_instance_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_id=root_team_id,
                    seat_id=root_seat_id,
                    role_id=final_decider_role_id,
                    employee_id=f"{final_decider_role_id}-default-session",
                    member_session_id=f"role-session::{project_id}::{final_decider_role_id}",
                    role_runtime_session_id=root_role_runtime_id,
                    status="idle",
                    resident_status="idle",
                    metadata=mark_work_item_runtime(),
                )
            )
            await self.store.save_delegation_role_session(
                RoleRuntimeSession(
                    role_session_id=root_role_runtime_id,
                    run_id=run.run_id,
                    project_id=project_id,
                    team_instance_id=root_team_instance_id,
                    team_id=root_team_id,
                    role_id=final_decider_role_id,
                    seat_id=root_seat_id,
                    seat_state_id=root_seat_state_id,
                    employee_id=f"{final_decider_role_id}-default-session",
                    seat_ids=[root_seat_id],
                    status="idle",
                    metadata=mark_work_item_runtime({
                        "final_decider_role_id": final_decider_role_id,
                    }),
                )
            )

        final_decider = self.org_engine.get_agent(final_decider_role_id)
        root_title = (
            f"{getattr(final_decider, 'name', final_decider_role_id)} Intake"
            if final_decider is not None
            else "Runtime Delegation Intake"
        )
        dynamic_root_team_instance_id = f"team-instance::{run.run_id}::{root_seat_id}::root"
        plan_payload = serialize_company_work_item_runtime_plan(work_item_plan)
        root_work_item = DelegationWorkItem(
            run_id=run.run_id,
            cell_id=root_team_id,
            team_instance_id=dynamic_root_team_instance_id,
            team_id=root_team_id,
            role_id=final_decider_role_id,
            seat_id=root_seat_id,
            seat_state_id=root_seat_state_id,
            role_runtime_session_id=root_role_runtime_id,
            title=root_title,
            summary=original_message,
            kind="intake",
            phase=Phase.READY,
            batch_id=f"batch::{run.run_id}::0",
            batch_index=0,
            continuation_source=str(getattr(previous_run, "run_id", "") or "").strip(),
            metadata=mark_work_item_projection(mark_work_item_runtime({
                "runtime_model": "multi_team_org",
                "session_scope_id": session_id,
                "team_id": root_team_id,
                "team_instance_id": dynamic_root_team_instance_id,
                "seat_id": root_seat_id,
                "seat_state_id": root_seat_state_id,
                "work_kind": "intake",
                "dependency_work_item_ids": [],
                "batch_id": f"batch::{run.run_id}::0",
                "created_by_seat_id": root_seat_id,
                "assigned_role_runtime_id": root_role_runtime_id,
                "delegation_playbook": dict(delegation_playbook),
                "project_dossier": dict(project_dossier or {}),
                "contact_role_ids": list(
                    dict(final_decider_seat or {}).get("contact_role_ids", [])
                    or []
                ),
                "allowed_delegate_role_ids": list(
                    dict(final_decider_seat or {}).get("allowed_delegate_role_ids", [])
                    or []
                ),
                "target_output_dir": target_output_dir,
                "comms_workspace_root": comms_workspace_root,
                "authoritative_output": True,
                "user_visible": True,
            }), turn_type="intake"),
        )
        root_projection_id = str(
            (work_item_plan.root_projection_id if work_item_plan is not None else "")
            or plan_payload.get("root_projection_id", "")
            or ""
        ).strip()
        root_work_item.projection_id = root_projection_id or root_work_item.work_item_id
        root_work_item.metadata = mark_work_item_projection(
            root_work_item.metadata,
            projection_id=root_work_item.projection_id,
            turn_type="intake",
        )
        await self.store.save_delegation_work_item(root_work_item)
        await self.store.save_team_instance(
            TeamInstance(
                team_instance_id=dynamic_root_team_instance_id,
                run_id=run.run_id,
                project_id=project_id,
                team_id=root_team_id,
                session_id=session_id,
                status="active",
                seat_ids=[
                    str(item.get("seat_id", "") or "").strip()
                    for item in list(runtime_topology.get("seats", []) or [])
                    if str(item.get("team_id", "") or "").strip() == root_team_id and str(item.get("seat_id", "") or "").strip()
                ] or [root_seat_id],
                role_ids=[
                    str(item.get("role_id", "") or "").strip()
                    for item in list(runtime_topology.get("seats", []) or [])
                    if str(item.get("team_id", "") or "").strip() == root_team_id and str(item.get("role_id", "") or "").strip()
                ] or [final_decider_role_id],
                metadata=mark_work_item_runtime({
                    "runtime_model": "multi_team_org",
                    "manager_seat_id": root_seat_id,
                    "parent_work_item_id": root_work_item.work_item_id,
                    "root_team": True,
                }),
            )
        )
        run.metadata = dict(run.metadata or {})
        run.metadata["root_work_item_id"] = root_work_item.work_item_id
        run.metadata["root_team_instance_id"] = dynamic_root_team_instance_id
        if work_item_plan is not None:
            run.metadata["company_work_item_plan"] = serialize_company_work_item_runtime_plan(work_item_plan)
        await self.store.save_delegation_run(run)
        if hasattr(self.store, "save_delegation_event"):
            await self.store.save_delegation_event(
                DelegationEvent(
                    run_id=run.run_id,
                    work_item_id=root_work_item.work_item_id,
                    cell_id=root_work_item.cell_id,
                    role_id=root_work_item.role_id,
                    event_type="work_item_created",
                    payload={
                        "work_item_runtime": True,
                        **work_item_identity_payload(projection_id=root_work_item.projection_id, turn_type="intake"),
                        "team_id": root_team_id,
                        "seat_id": root_seat_id,
                        "batch_id": root_work_item.batch_id,
                        "work_kind": "intake",
                        "title": root_title,
                    },
                )
            )
        return run.run_id, root_work_item

    @staticmethod
    def _uses_shared_role_session(task: Task | None) -> bool:
        if task is None:
            return False
        return bool(dict(getattr(task, "metadata", {}) or {}).get("shared_role_session", False))

    @staticmethod
    def _shared_company_role_session_id(
        parent_session_id: str,
        role_id: str,
        *,
        final_decider_role_id: str = "",
        root_session: bool = False,
    ) -> str:
        parent_sid = str(parent_session_id or "").strip()
        normalized_role = str(role_id or "").strip()
        final_role = str(final_decider_role_id or "").strip()
        if root_session or (parent_sid and final_role and normalized_role == final_role):
            return parent_sid or normalized_role or str(uuid.uuid4())
        if parent_sid and normalized_role:
            return f"{parent_sid}:role:{normalized_role}"
        return parent_sid or normalized_role or str(uuid.uuid4())

    async def _ensure_runtime_work_item_task(
        self,
        *,
        work_item: DelegationWorkItem,
        parent_session_id: str,
        original_message: str,
        decision: ModeSelection,
        runtime_topology: dict[str, Any],
        delegation_playbook: dict[str, Any],
        secretary_context: str,
        target_output_dir: str | None,
        origin_channel: str,
        origin_chat_id: str,
        origin_thread_id: str,
        origin_task_id: str | None,
        attachment_refs: list[dict[str, Any]] | None,
        attachment_context: str,
        force_native_execution: bool,
        root_session: bool = False,
    ) -> Task:
        assert self.store and self.memory
        role_id = str(work_item.role_id or "").strip()
        seat_id = str((work_item.metadata or {}).get("seat_id", "") or "").strip()
        team_id = str((work_item.metadata or {}).get("team_id", "") or work_item.cell_id or "").strip()
        work_kind = str((work_item.metadata or {}).get("work_kind", "") or work_item.kind or "execute").strip().lower() or "execute"
        work_item_projection_id = projection_id_for_work_item(work_item)
        legacy_turn_type = turn_type_for_work_item(work_item, fallback="")
        mapped_turn_type = self._runtime_work_kind_to_work_item_turn_type(work_kind)
        work_item_turn_type = (
            legacy_turn_type
            if legacy_turn_type in {"intake", "dispatch", "plan", "setup", "execute", "review", "report", "aggregate", "deliver"}
            else mapped_turn_type
        )
        work_item_projection_ref = work_item_projection_id
        topology_seat = next(
            (
                dict(seat)
                for seat in list(runtime_topology.get("seats", []) or [])
                if str(seat.get("seat_id", "") or "").strip() == seat_id
            ),
            {},
        )
        # Fix 2: canonical fallback. assigned_role_runtime_id on metadata
        # is usually populated; we only land in the generator when a stale
        # work item predates that seeding.
        role_session_id = (
            str((work_item.metadata or {}).get("assigned_role_runtime_id", "") or "").strip()
            or canonical_role_session_id(
                run_id=str(work_item.run_id or "").strip(),
                role_id=role_id,
                team_instance_id=str(getattr(work_item, "team_instance_id", "") or "").strip(),
            )
        )
        final_decider_role_id = str(runtime_topology.get("final_decider_role_id", "") or "").strip()
        session_id = self._shared_company_role_session_id(
            parent_session_id,
            role_id,
            final_decider_role_id=final_decider_role_id,
            root_session=root_session,
        )
        session_title = (
            str((topology_seat.get("metadata", {}) or {}).get("role_name", "") or "").strip()
            or role_id
            or str(work_item.title or work_item_projection_ref or "Runtime Work Item").strip()
        )
        existing = None
        get_runtime_task = getattr(self.store, "get_runtime_task_for_work_item", None)
        if callable(get_runtime_task):
            existing = await get_runtime_task(work_item.work_item_id)
        if existing is not None:
            set_linked_work_item_id(existing, work_item.work_item_id)
            existing.session_id = session_id
            existing.metadata = dict(existing.metadata or {})
            existing.metadata["shared_role_session"] = True
            existing.metadata["shared_role_id"] = role_id
            existing.metadata["company_runtime_root_session_id"] = parent_session_id
            existing.metadata = mark_work_item_projection(
                existing.metadata,
                projection_id=work_item_projection_id,
                turn_type=work_item_turn_type,
            )
            issues = [
                issue for issue in validate_work_item_runtime_projection(existing, work_item)
                if issue.severity == "error"
            ]
            if issues:
                raise RuntimeError(
                    "work-item runtime invariant failed for root runtime Task "
                    f"{existing.id}: "
                    + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
                )
            await self.memory.ensure_session(
                existing.session_id,
                project_id=existing.project_id,
                title=session_title,
                mode="primary",
                parent_session_id=None,
                metadata={
                    "task_id": existing.id,
                    **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
                    "work_item_id": work_item.work_item_id,
                    "role_id": role_id,
                    "seat_id": seat_id,
                    "origin_session_id": parent_session_id,
                    "origin_channel": origin_channel,
                    "origin_chat_id": origin_chat_id,
                    "origin_thread_id": origin_thread_id,
                    "shared_role_session": True,
                    "shared_role_id": role_id,
                    "company_runtime_root_session_id": parent_session_id,
                },
            )
            await self.store.save_task(existing)
            return existing
        employee_assignment = dict(topology_seat.get("employee_assignment", {}) or {})
        if not employee_assignment and self.org_engine and role_id:
            preferred_employee_id = str(topology_seat.get("employee_id", "") or "").strip() or None
            resolved_assignment = self.org_engine.resolve_employee_for_work_item(
                role_id,
                [],
                project_id=self.project_id or "default",
                preferred_employee_id=preferred_employee_id,
            )
            employee_assignment = dict(resolved_assignment or {})
            if employee_assignment:
                topology_seat["employee_id"] = str(employee_assignment.get("employee_id", "") or "").strip()
                topology_seat["employee_assignment"] = dict(employee_assignment)
                for index, raw_seat in enumerate(list(runtime_topology.get("seats", []) or [])):
                    seat_entry = dict(raw_seat or {})
                    if str(seat_entry.get("seat_id", "") or "").strip() != seat_id:
                        continue
                    seat_entry["employee_id"] = topology_seat["employee_id"]
                    seat_entry["employee_assignment"] = dict(employee_assignment)
                    runtime_topology["seats"][index] = seat_entry
                    break
        preferred_external_agent = (
            str(topology_seat.get("preferred_external_agent", "") or "").strip()
            or str((employee_assignment or {}).get("preferred_external_agent", "") or "").strip()
            or None
        )
        selected_execution_agent, assigned_external_agent, role_force_native_execution = (
            resolve_effective_execution_agent(
                topology_seat.get("selected_execution_agent"),
                preferred_external_agent,
                force_native_execution=bool(topology_seat.get("force_native_execution", False)),
            )
        )
        resolved_force_native_execution = bool(force_native_execution or role_force_native_execution)
        if resolved_force_native_execution:
            selected_execution_agent = "native"
            assigned_external_agent = None
        preferred_external_agent = assigned_external_agent
        work_item.metadata = dict(work_item.metadata or {})
        if employee_assignment:
            work_item.metadata["employee_assignment"] = copy.deepcopy(employee_assignment)
        prompt_ctx = str((employee_assignment or {}).get("prompt_context", "") or "").strip()
        if prompt_ctx:
            work_item.metadata["employee_prompt_context"] = prompt_ctx
        delta_ctx = str((employee_assignment or {}).get("delta_context", "") or "").strip()
        if delta_ctx:
            work_item.metadata["employee_delta_context"] = delta_ctx
        owner_execution_copy = build_work_item_owner_execution_copy(work_item)
        owner_execution_copy.setdefault("delegation_role_session_id", role_session_id)
        owner_execution_copy["work_kind"] = work_item_turn_type
        task = Task(
            title=str(work_item.title or work_item_projection_ref or "Runtime Work Item").strip(),
            description=(
                self._build_runtime_root_description(original_message=original_message, decision=decision)
                if root_session
                else str(work_item.summary or original_message or "").strip()
            ),
            assigned_to=role_id,
            status=TaskStatus.PENDING,
            project_id=self.project_id or "default",
            session_id=session_id,
            parent_session_id=parent_session_id,
            assigned_external_agent=assigned_external_agent,
            metadata=mark_work_item_projection(mark_work_item_runtime({
                "mode": "company",
                "execution_mode": decision.mode.value,
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "original_message": original_message,
                "router_preferred_agent": decision.preferred_agent,
                "company_profile": decision.company_profile or getattr(self.config.org, "company_profile", "corporate"),
                "organization_id": getattr(self.config.org, "organization_id", ""),
                "organization_name": getattr(self.config.org, "organization_name", ""),
                "organization_config_file": getattr(self.config.org, "organization_config_file", ""),
                "delegation_playbook": dict(delegation_playbook),
                "recruitment_staffing_overrides": dict(
                    (delegation_playbook or {}).get("recruitment_staffing_overrides", {}) or {}
                ),
                "recruitment_fallback_role_ids": list(
                    (delegation_playbook or {}).get("recruitment_fallback_role_ids", []) or []
                ),
                "recruitment_role_agent_overrides": dict(
                    (delegation_playbook or {}).get("recruitment_role_agent_overrides", {}) or {}
                ),
                "runtime_topology": copy.deepcopy(runtime_topology),
                **owner_execution_copy,
                "work_item_projection_ref": work_item_projection_ref,
                "seat_manager_role_id": str(topology_seat.get("manager_role_id", "") or "").strip(),
                "manager_role_id": str(topology_seat.get("manager_role_id", "") or "").strip(),
                "manager_seat_id": str(topology_seat.get("manager_seat_id", "") or "").strip(),
                "managed_team_id": str(topology_seat.get("managed_team_id", "") or "").strip(),
                "seat_contact_role_ids": list(topology_seat.get("contact_role_ids", []) or []),
                "allowed_delegate_role_ids": list(topology_seat.get("allowed_delegate_role_ids", []) or []),
                "force_native_execution": resolved_force_native_execution,
                "employee_assignment": dict(employee_assignment or {}),
                "employee_prompt_context": str((employee_assignment or {}).get("prompt_context", "")).strip(),
                "employee_delta_context": str((employee_assignment or {}).get("delta_context", "")).strip(),
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
                "execution_task_ids": [work_item.work_item_id],
                "work_item_batch_id": str(getattr(work_item, "batch_id", "") or "").strip(),
                "parent_session_id": parent_session_id,
                "origin_task_id": origin_task_id,
                "origin_channel": origin_channel,
                "origin_chat_id": origin_chat_id,
                "origin_thread_id": origin_thread_id,
                "attachment_refs": list(attachment_refs or []),
                "attachment_context": attachment_context,
                "secretary_context": secretary_context,
                "include_project_knowledge": self._requests_explicit_project_knowledge(original_message),
                "target_output_dir": target_output_dir,
                "output_root": target_output_dir,
                "workspace_root": str((work_item.metadata or {}).get("comms_workspace_root", "") or "").strip(),
                "comms_workspace_root": str((work_item.metadata or {}).get("comms_workspace_root", "") or "").strip(),
                "comms_root": str((work_item.metadata or {}).get("comms_root", "") or "").strip(),
                "org_version": self.org_engine.current_org_version() if self.org_engine else 1,
                "org_runtime_version": self.org_engine.current_runtime_topology_version() if self.org_engine else 1,
                "user_visible": bool((work_item.metadata or {}).get("user_visible", False)),
                "authoritative_output": bool((work_item.metadata or {}).get("authoritative_output", False)),
                "shared_role_session": True,
                "shared_role_id": role_id,
                "company_runtime_root_session_id": parent_session_id,
            }), projection_id=work_item_projection_id, turn_type=work_item_turn_type),
        )
        set_linked_work_item_id(task, work_item.work_item_id)
        await self.store.save_delegation_work_item(work_item)
        ensure_runtime_task = getattr(self.store, "ensure_runtime_task_for_work_item", None)
        if callable(ensure_runtime_task):
            task = await ensure_runtime_task(work_item, lambda task=task: task)
        else:
            await self.store.save_task(task)
            link_runtime_task = getattr(self.store, "link_work_item_runtime_task", None)
            if callable(link_runtime_task):
                linked = await link_runtime_task(work_item.work_item_id, task.id)
                if not linked:
                    raise RuntimeError(
                        "failed to link new runtime Task "
                        f"{task.id} for WorkItem {work_item.work_item_id}"
                    )
        set_linked_work_item_id(task, work_item.work_item_id)
        issues = [
            issue for issue in validate_work_item_runtime_projection(task, work_item)
            if issue.severity == "error"
        ]
        if issues:
            raise RuntimeError(
                "work-item runtime invariant failed for root runtime Task "
                f"{task.id}: "
                + "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
            )
        await self.memory.ensure_session(
            task.session_id,
            project_id=task.project_id,
            title=session_title,
            mode="primary",
            parent_session_id=None,
            metadata={
                "task_id": task.id,
                **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
                "work_item_id": work_item.work_item_id,
                "role_id": role_id,
                "seat_id": seat_id,
                "origin_session_id": task.parent_session_id,
                "origin_channel": origin_channel,
                "origin_chat_id": origin_chat_id,
                "origin_thread_id": origin_thread_id,
                "shared_role_session": True,
                "shared_role_id": role_id,
                "company_runtime_root_session_id": parent_session_id,
            },
        )
        return task

    @staticmethod
    def _runtime_work_kind_to_work_item_turn_type(work_kind: str) -> str:
        return canonical_work_item_turn_type_for_kind(work_kind)

    def _reregister_company_runtime_children(
        self,
        tasks: list[Task],
        *,
        checkpoint_session_id: str | None = None,
    ) -> None:
        """Re-register child task IDs with WSHandler for progress dual-routing.

        During checkpoint resume / runtime re-execution, the original
        ``_active_runtimes`` mapping has been cleaned up. This helper
        re-registers all task IDs so that ``work_item_progress`` events
        emitted by ``_ceo_initiate_rework`` or the execution loop are
        correctly routed to the parent session's UI channel.

        The parent session ID is resolved from the tasks themselves
        (``parent_session_id`` field), falling back to the checkpoint's
        ``session_id``.
        """
        if not self.on_company_runtime_children or not tasks:
            return
        parent_session_id: str | None = None
        for task in tasks:
            candidate = str(getattr(task, "parent_session_id", "") or "").strip()
            if not candidate:
                candidate = str(task.metadata.get("parent_session_id", "") or "").strip()
            if candidate:
                parent_session_id = candidate
                break
        if not parent_session_id:
            parent_session_id = checkpoint_session_id
        if not parent_session_id:
            return
        self.on_company_runtime_children(parent_session_id, [t.id for t in tasks])

    async def _load_company_runtime_snapshot(
        self,
        parent_session_id: str | None,
    ) -> tuple[CompanyWorkItemRuntimePlan, list[Task]] | None:
        if not self.store or not parent_session_id:
            return None
        identity_index = await load_company_runtime_identity_index(
            self.store,
            self.project_id or "default",
        )
        identity = identity_index.resolve(runtime_session_id=parent_session_id)
        if identity is None:
            return None
        work_item_tasks = [
            task
            for task_id in identity.runtime_task_ids
            if task_id != identity.ui_anchor_task_id
            for task in [identity_index.task(task_id)]
            if task is not None
            and is_company_runtime_task(task)
            and (
                work_item_projection_id_from_metadata(getattr(task, "metadata", {}) or {})
                or is_work_item_runtime_metadata(getattr(task, "metadata", {}) or {})
                or linked_work_item_id_for_task(task)
            )
        ]
        if not work_item_tasks:
            return None

        latest_by_projection_id: dict[str, Task] = {}
        for task in sorted(work_item_tasks, key=lambda item: (item.created_at, item.id)):
            projection_id = str(
                work_item_projection_id_from_metadata(task.metadata)
                or linked_work_item_id_for_task(task)
                or ""
            ).strip()
            if projection_id:
                latest_by_projection_id[projection_id] = task
        if not latest_by_projection_id:
            return None

        plan_data = None
        plan_sources = list(latest_by_projection_id.values())
        config_source = identity_index.task(identity.config_source_task_id)
        if config_source is not None and config_source.id not in {
            task.id for task in plan_sources
        }:
            plan_sources.append(config_source)
        for task in sorted(
            plan_sources,
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        ):
            candidate = serialized_company_plan_from_metadata(task.metadata)
            if candidate:
                plan_data = candidate
                break
        sample = next(iter(latest_by_projection_id.values()))
        if plan_data and isinstance(plan_data, dict):
            plan = deserialize_company_work_item_runtime_plan(plan_data)
        else:
            plan = CompanyWorkItemRuntimePlan(
                profile=str(sample.metadata.get("company_profile", "") or getattr(self.config.org, "company_profile", "corporate")).strip() or "corporate",
                metadata={
                    "execution_model": str(sample.metadata.get("execution_model", "") or "multi_team_org").strip() or "multi_team_org",
                    "runtime_model": str(sample.metadata.get("runtime_model", "") or "").strip(),
                    "work_item_runtime": is_work_item_runtime_metadata(sample.metadata),
                },
            )
        projection_order = {spec.projection_id: idx for idx, spec in enumerate(plan.projections)}
        ordered_tasks = sorted(
            latest_by_projection_id.values(),
            key=lambda task: (
                projection_order.get(
                    projection_id_for_task(task)
                    or linked_work_item_id_for_task(task),
                    len(projection_order),
                ),
                task.created_at,
                task.id,
            ),
        )
        return plan, ordered_tasks

    @staticmethod
    def _runtime_uses_multi_team_org(plan: CompanyWorkItemRuntimePlan | None) -> bool:
        if plan is None:
            return False
        metadata = dict(getattr(plan, "metadata", {}) or {})
        return (
            str(metadata.get("execution_model", "") or "").strip() == "multi_team_org"
            or str(metadata.get("runtime_model", "") or "").strip() == "multi_team_org"
            or bool(getattr(plan, "projections", []) or [])
        )

    @staticmethod
    def _task_uses_multi_team_org(task: Task | None) -> bool:
        if task is None:
            return False
        metadata = dict(task.metadata or {})
        return (
            str(metadata.get("execution_model", "") or "").strip() == "multi_team_org"
            or str(metadata.get("runtime_model", "") or "").strip() == "multi_team_org"
            or is_work_item_runtime_metadata(metadata)
        )

    @staticmethod
    def _is_company_runtime_suspend_checkpoint(checkpoint_type: str | None) -> bool:
        return str(checkpoint_type or "").strip() in _COMPANY_RUNTIME_SUSPEND_CHECKPOINT_TYPES

    @staticmethod
    def _checkpoint_progress_tail(task: Task, *, limit: int = 20) -> list[str]:
        progress = list((task.metadata or {}).get("progress_log", []) or [])
        return [str(item) for item in progress[-limit:]]

    @staticmethod
    def _task_effective_execution_agent_identity(
        task: Task,
    ) -> tuple[str, str, str]:
        """Return the backend that this Task attempt actually executes on.

        Recruitment's ``selected_execution_agent`` is policy/default input and
        can remain unchanged after an unlocked adaptive selection.  Runtime
        assignment and its audit record are the attempt identity; recruitment
        metadata is only a fallback for tasks that have not recorded either.
        """

        metadata = dict(task.metadata or {})
        selection = dict(metadata.get("agent_selection", {}) or {})
        selection_agent = normalize_recruitment_agent_choice(
            selection.get("selected")
        )
        assigned_agent = normalize_recruitment_agent_choice(
            task.assigned_external_agent
        )
        if bool(metadata.get("force_native_execution")) or selection_agent == "native":
            selected_agent = "native"
            assigned_external_agent = ""
        elif assigned_agent and assigned_agent != "native":
            selected_agent = assigned_agent
            assigned_external_agent = assigned_agent
        elif selection_agent and selection_agent != "native":
            selected_agent = selection_agent
            assigned_external_agent = selection_agent
        else:
            selected_agent = normalize_recruitment_agent_choice(
                metadata.get("selected_execution_agent"),
                default="native",
            ) or "native"
            assigned_external_agent = (
                selected_agent if selected_agent != "native" else ""
            )
        selection_source = str(
            selection.get("selection_source")
            or metadata.get("selected_execution_agent_source")
            or ""
        ).strip()
        return selected_agent, assigned_external_agent, selection_source

    async def _external_resume_snapshot_for_task(self, task: Task) -> dict[str, Any]:
        session = (
            await self._load_best_external_resume_session_for_task(task)
            or await self._load_latest_external_session_for_task(task)
        )
        if not session:
            return {}
        metadata = dict(getattr(session, "metadata", {}) or {})
        return {
            "agent_type": str(getattr(session, "agent_type", "") or "").strip(),
            "session_id": str(getattr(session, "session_id", "") or "").strip(),
            "opc_session_id": str(getattr(session, "opc_session_id", "") or "").strip(),
            "task_id": str(getattr(session, "task_id", "") or "").strip(),
            "status": str(getattr(session, "status", "") or "").strip(),
            "workspace_path": str(getattr(session, "workspace_path", "") or "").strip(),
            "resume_session_id": str(metadata.get("resume_session_id", "") or "").strip(),
            "provider_session_id": str(metadata.get("provider_session_id", "") or "").strip(),
            "metadata": metadata,
            "updated_at": getattr(session, "updated_at", datetime.now()).isoformat(),
        }

    async def _maybe_resume_existing_company_runtime(
        self,
        user_reply: str,
        session_id: str | None = None,
        *,
        force_resume: bool = False,
    ) -> str | None:
        assert self.store
        if not self.company_executor or not session_id:
            return None
        runtime_session_id = await self._company_runtime_parent_session_for_session_id(session_id)
        runtime_session_id = runtime_session_id or session_id
        snapshot = await self._load_company_runtime_snapshot(runtime_session_id)
        if not snapshot:
            return None
        plan, tasks = snapshot
        if not tasks:
            return None
        tasks = await self._terminalize_already_closed_delivery_review_tasks(tasks)
        work_item_runtime_tasks = [task for task in tasks if is_work_item_runtime_metadata(task.metadata)]
        if work_item_runtime_tasks and self._runtime_uses_multi_team_org(plan) and all(self._task_uses_multi_team_org(task) for task in work_item_runtime_tasks):
            live_running_tasks: list[Task] = []
            for task in tasks:
                if task.status == TaskStatus.RUNNING and await self._task_runtime_is_live(task):
                    live_running_tasks.append(task)
            waiting_tasks = [task for task in tasks if task.status in _WAITING_TASK_STATUSES]
            pending_tasks = [task for task in tasks if task.status == TaskStatus.PENDING]
            failed_tasks = [task for task in tasks if task.status == TaskStatus.FAILED]
            blocked_tasks = [task for task in tasks if task.status == TaskStatus.BLOCKED]
            no_active_runtime_work = not live_running_tasks and not waiting_tasks and not pending_tasks and not failed_tasks and not blocked_tasks
            has_closed_delivery_review = any(self._is_closed_company_delivery_review_task(task) for task in tasks)
            if not force_resume:
                if not (no_active_runtime_work and has_closed_delivery_review):
                    followup_result = await self._resume_company_runtime_via_final_decider(
                        plan=plan,
                        tasks=tasks,
                        user_reply=user_reply,
                        session_id=runtime_session_id,
                    )
                    if followup_result is not None:
                        return followup_result
            if no_active_runtime_work:
                return None
            if live_running_tasks or waiting_tasks:
                snapshot_text = self._format_company_runtime_snapshot(tasks)
                active_labels = ", ".join(
                    f"`{str(task.title or projection_id_for_task(task) or task.id)}`"
                    for task in [*live_running_tasks, *waiting_tasks][:6]
                )
                return (
                    "The latest multi-team organization run is already in progress for this session. "
                    f"Active turns: {active_labels}.\n\n{snapshot_text}"
                )
            if self.on_company_runtime_children and runtime_session_id and tasks:
                self.on_company_runtime_children(runtime_session_id, [t.id for t in tasks])
            result = await self.company_executor.execute(plan, tasks)
            snapshot_text = self._format_company_runtime_snapshot(
                tasks,
                heading="## Latest Organization Snapshot (before resume)",
            )
            return f"Resuming the existing multi-team organization run.\n\n{snapshot_text}\n\n{result}".strip()
        all_terminal = all(
            t.status in {TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.FAILED}
            for t in tasks
        )
        if all_terminal:
            return None
        snapshot_text = self._format_company_runtime_snapshot(
            tasks,
            heading="## Legacy Runtime Snapshot (read-only)",
        )
        return (
            "A legacy company runtime run was found for this session. "
            "Legacy runs are read-only and cannot be resumed under the work-item runtime.\n\n"
            f"{snapshot_text}"
        )

    @classmethod
    def _is_closed_company_delivery_review_task(cls, task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        closed = (
            cls._metadata_flag_true(metadata.get("self_evolution_review_completed", False))
            or cls._metadata_flag_true(metadata.get("feedback_closed", False))
            or cls._metadata_flag_true(metadata.get("human_review_closed", False))
            or cls._metadata_flag_true(metadata.get("feedback_superseded", False))
        )
        if not closed:
            return False
        turn_type = turn_type_for_task(task, fallback="")
        feedback_scope = str(metadata.get("feedback_scope", "") or "").strip().lower()
        return (
            turn_type in {"deliver", "delivery"}
            or feedback_scope == "final"
            or cls._metadata_flag_true(metadata.get("authoritative_output", False))
        )

    async def _terminalize_already_closed_delivery_review_tasks(self, tasks: list[Task]) -> list[Task]:
        if not self.store:
            return tasks
        refreshed_tasks: list[Task] = []
        for task in tasks:
            metadata = dict(getattr(task, "metadata", {}) or {})
            if task.status in _WAITING_TASK_STATUSES and self._is_closed_company_delivery_review_task(task):
                resolution = str(
                    metadata.get("feedback_resolution")
                    or metadata.get("human_review_resolution")
                    or (
                        "self_evolution_review_completed"
                        if self._metadata_flag_true(metadata.get("self_evolution_review_completed", False))
                        else "delivery_review_closed"
                    )
                ).strip()
                await self._close_company_delivery_review_task(
                    task,
                    resolution=resolution,
                    closed_at=str(
                        metadata.get("self_evolution_review_completed_at")
                        or metadata.get("feedback_closed_at")
                        or metadata.get("human_review_closed_at")
                        or datetime.now().isoformat()
                    ),
                    checkpoint_id=str(metadata.get("human_review_checkpoint_id", "") or "").strip(),
                )
                try:
                    reloaded = await self.store.get_task(task.id)
                except Exception:
                    reloaded = None
                refreshed_tasks.append(reloaded or task)
                continue
            refreshed_tasks.append(task)
        return refreshed_tasks

    async def _execute_single_agent(self, tasks: list[Task], use_external: str | None = None) -> str:
        """串列執行任務列表 — 逐一執行並彙總結果。

        參數：
            tasks (list[Task])：待執行的任務列表。
            use_external (str | None)：強制指定的外部代理 ID。

        返回值：
            str — 所有任務結果以雙換行串接的文字。

        被誰引用：
            - _run_task_once()：單代理/任務模式執行路徑
        """
        results: list[str] = []

        for task in tasks:
            if use_external and not task.assigned_external_agent:
                task.assigned_external_agent = use_external
            task.status = TaskStatus.RUNNING
            await self.store.save_task(task)
            await self._record_lifecycle_started(task)
            result = await self._execute_task(task)
            results.append(result.content)

        return "\n\n".join(r for r in results if r)

    async def _execute_multi_agent(self, tasks: list[Task]) -> str:
        """並行執行獨立任務（已棄用）。

        .. deprecated::
            新程式碼應使用公司模式（company_profile="corporate"）。
            此方法僅為遷移前建立的檢查點保留向後相容。

        參數：
            tasks (list[Task])：待並行執行的任務列表。

        返回值：
            str — 各任務結果以標題分隔的彙總文字。

        被誰引用：
            - _run_task_once()：MULTI_AGENT 執行模式（舊檢查點恢復）
        """
        logger.warning(
            "[deprecated] _execute_multi_agent called; "
            "new requests should use company mode with parallel profile"
        )
        assert self.task_scheduler

        async def executor(task: Task) -> None:
            await self._execute_task(task)

        tasks = await self.task_scheduler.execute_graph(tasks, executor)

        results = []
        for t in tasks:
            if t.result and t.result.get("content"):
                results.append(f"### {t.title}\n{t.result['content']}")

        return "\n\n".join(results)

    async def _execute_company_mode(self, tasks: list[Task], runtime_plan: Any) -> str:
        """公司模式執行 — 透過多團隊運行時執行工作項目計劃。

        參數：
            tasks (list[Task])：觸發公司模式的任務列表。
            runtime_plan (Any)：CompanyWorkItemRuntimePlan 或 CompanyRuntimeSpec。

        返回值：
            str — 公司執行器回傳的最終彙總結果。

        被誰引用：
            - _run_task_once()：COMPANY_MODE 執行模式
        """
        assert self.company_executor
        if isinstance(runtime_plan, CompanyWorkItemRuntimePlan):
            work_item_plan = runtime_plan
        else:
            spec = runtime_plan if isinstance(runtime_plan, CompanyRuntimeSpec) else None
            task_metadata = dict((tasks[0].metadata if tasks else {}) or {})
            spec_metadata = dict(getattr(spec, "metadata", {}) or {})
            profile = str(
                getattr(spec, "profile", "")
                or task_metadata.get("company_profile", "")
                or getattr(self.config.org, "company_profile", "corporate")
            ).strip() or "corporate"
            plan_payload = serialized_company_plan_from_metadata(task_metadata) or {}
            work_item_plan = (
                deserialize_company_work_item_runtime_plan(plan_payload)
                if isinstance(plan_payload, dict) and plan_payload
                else CompanyWorkItemRuntimePlan(profile=profile)
            )
            work_item_plan.metadata = {
                **dict(work_item_plan.metadata or {}),
                    **spec_metadata,
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "work_item_driven": True,
                    "original_request": str(
                        getattr(spec, "original_request", "")
                        or task_metadata.get("original_message", "")
                        or ""
                    ).strip(),
            }
        return await self.company_executor.execute(work_item_plan, tasks)

    def _self_evolution_prompt_contract(
        self,
        *,
        role_id: str,
        source: dict[str, Any],
        tasks: list[Task],
    ) -> dict[str, Any]:
        """建構員工自我演化的 Prompt 契約（含任務摘要、組織圖、交付物規範）。"""
        feedback = str(source.get("human_feedback", "") or "").strip()
        action = str(source.get("human_action", "") or "approve").strip()
        review_text = (
            f"Human review action: {action}."
            + (f"\nHuman feedback: {feedback}" if feedback else "\nHuman fully agreed with this delivery.")
        )
        task_brief = (
            "Run employee self-evolution for this role from the completed company delivery review.\n"
            f"{review_text}\n\n"
            "Decide whether your assigned employee should update its experience. If direct reports should also learn, "
            "delegate child WorkItems with `work_kind=\"self_evolution\"`. Do not continue the original user task, "
            "do not edit files, and do not produce a user-facing report. Final response must be strict JSON only: "
            "`{\"patches\": [...]}`."
        )
        return make_prompt_contract(
            task_brief=task_brief,
            upstream_intent_summary=str(source.get("delivery_summary", "") or "").strip(),
            manager_planning_handoff=(
                "Use the human review signal, delivery summary, work item task list, and org graph to decide "
                "which direct reports need self-evolution work."
            ),
            owned_outcome_kind="self_evolution",
            scope_key=f"self_evolution::{source.get('checkpoint_id', '')}::{role_id}",
            deliverables=[
                "Strict JSON only with top-level `patches` list.",
                "Use `patches: []` if no employee experience update is needed for this role.",
                "Use `delegate_work` with `work_kind=\"self_evolution\"` for direct reports that should reflect on their own work.",
            ],
            acceptance_criteria=[
                "No prose, markdown, file edits, or user-facing delivery content.",
                "Patch employee_id must be the employee assigned to this role's self-evolution work item.",
                "Each patch may include summary, strengths, adjustments, avoid_next_time, routing_notes, evidence_task_ids, and confidence.",
            ],
            coordination_notes=json.dumps(
                {
                    "work_item_tasks": self._self_evolution_task_payloads(tasks),
                    "org_graph": self._self_evolution_org_graph(),
                },
                ensure_ascii=False,
            ),
            source={"kind": "company_delivery_feedback_self_evolution"},
        )

    async def _create_company_self_evolution_root_work_item(
        self,
        *,
        checkpoint: ExecutionCheckpoint,
        waiting_task: Task,
        tasks: list[Task],
        plan: CompanyWorkItemRuntimePlan,
        root_role_id: str,
        organization_id: str,
        source: dict[str, Any],
        assignments: dict[str, dict[str, Any]],
    ) -> DelegationWorkItem | None:
        """建立公司自我演化的根工作項目（DelegationWorkItem）。"""
        if not self.store or not hasattr(self.store, "save_delegation_work_item"):
            return None
        all_tasks = list(tasks or [])
        if waiting_task.id not in {task.id for task in all_tasks}:
            all_tasks.append(waiting_task)
        runtime_topology = self._runtime_topology_from_tasks(all_tasks, waiting_task)
        linked_delivery_work_item: DelegationWorkItem | None = None
        linked_delivery_work_item_id = str(linked_work_item_id_for_task(waiting_task) or "").strip()
        if linked_delivery_work_item_id and hasattr(self.store, "get_delegation_work_item"):
            try:
                linked_delivery_work_item = await self.store.get_delegation_work_item(linked_delivery_work_item_id)
            except Exception:
                linked_delivery_work_item = None
        run_id = str(
            self._task_runtime_value(all_tasks, "delegation_run_id")
            or runtime_topology.get("run_id", "")
            or getattr(linked_delivery_work_item, "run_id", "")
            or ""
        ).strip()
        if not runtime_topology and self.org_engine and hasattr(self.org_engine, "build_runtime_delegation_topology"):
            try:
                runtime_topology = dict(self.org_engine.build_runtime_delegation_topology() or {})
            except Exception:
                runtime_topology = {}
        if run_id and runtime_topology:
            runtime_topology.setdefault("run_id", run_id)
        root_seat = self._runtime_seat_for_role(runtime_topology, root_role_id)
        if not root_seat and linked_delivery_work_item is not None:
            linked_metadata = dict(getattr(linked_delivery_work_item, "metadata", {}) or {})
            root_assignment = dict(
                assignments.get(root_role_id, {})
                or linked_metadata.get("employee_assignment", {})
                or waiting_task.metadata.get("employee_assignment", {})
                or {}
            )
            root_seat = {
                "role_id": root_role_id,
                "cell_id": str(getattr(linked_delivery_work_item, "cell_id", "") or linked_metadata.get("delegation_cell_id", "") or "").strip(),
                "team_id": str(getattr(linked_delivery_work_item, "team_id", "") or linked_metadata.get("delegation_team_id", "") or "").strip(),
                "team_instance_id": str(getattr(linked_delivery_work_item, "team_instance_id", "") or linked_metadata.get("delegation_team_instance_id", "") or "").strip(),
                "seat_id": str(getattr(linked_delivery_work_item, "seat_id", "") or linked_metadata.get("delegation_seat_id", "") or "").strip(),
                "seat_state_id": str(getattr(linked_delivery_work_item, "seat_state_id", "") or linked_metadata.get("delegation_seat_state_id", "") or "").strip(),
                "role_runtime_session_id": str(getattr(linked_delivery_work_item, "role_runtime_session_id", "") or linked_metadata.get("delegation_role_session_id", "") or "").strip(),
                "manager_role_id": str(getattr(linked_delivery_work_item, "manager_role_id", "") or linked_metadata.get("manager_role_id", "") or "").strip(),
                "manager_seat_id": str(getattr(linked_delivery_work_item, "manager_seat_id", "") or linked_metadata.get("manager_seat_id", "") or "").strip(),
                "managed_team_id": str(linked_metadata.get("managed_team_id", "") or "").strip(),
                "direct_report_role_ids": list(linked_metadata.get("direct_report_role_ids", []) or []),
                "direct_report_seat_ids": list(linked_metadata.get("direct_report_seat_ids", []) or []),
                "allowed_delegate_role_ids": list(linked_metadata.get("allowed_delegate_role_ids", []) or []),
                "contact_role_ids": list(linked_metadata.get("contact_role_ids", []) or []),
                "employee_assignment": root_assignment,
            }
        if not run_id or not root_seat:
            return None
        team_id = str(root_seat.get("team_id", "") or self._task_runtime_value(all_tasks, "delegation_team_id") or "").strip()
        team_instance_id = str(root_seat.get("team_instance_id", "") or "").strip()
        if not team_instance_id and team_id:
            team_instance_id = f"team-instance::{run_id}::{team_id}"
        role_runtime_session_id = str(root_seat.get("role_runtime_session_id", "") or "").strip()
        if not role_runtime_session_id:
            role_runtime_session_id = canonical_role_session_id(
                run_id=run_id,
                role_id=root_role_id,
                team_instance_id=team_instance_id,
            )
        seat_id = str(root_seat.get("seat_id", "") or "").strip()
        assignment = dict(assignments.get(root_role_id, {}) or root_seat.get("employee_assignment", {}) or {})
        work_item_id = f"self-evolution::{checkpoint.checkpoint_id}"
        projection_id = f"self_evolution::{checkpoint.checkpoint_id[:8]}::{root_role_id}"
        delivery_summary = str(
            waiting_task.metadata.get("work_item_summary_for_downstream", "")
            or waiting_task.metadata.get("work_item_summary", "")
            or source.get("delivery_summary", "")
            or ""
        ).strip()
        source["delivery_summary"] = delivery_summary
        prompt_contract = self._self_evolution_prompt_contract(
            role_id=root_role_id,
            source=source,
            tasks=all_tasks,
        )
        assignment_context = dict(prompt_contract.get("assignment_context", {}) or {})
        session_scope_id = task_session_scope_id(waiting_task)
        metadata = mark_work_item_projection(mark_work_item_runtime({
            "runtime_model": "multi_team_org",
            "execution_mode": "company_mode",
            "execution_model": "multi_team_org",
            "mode": "company",
            "work_kind": "self_evolution",
            "self_evolution_work_item": True,
            "self_evolution_root": True,
            "self_evolution_checkpoint_id": checkpoint.checkpoint_id,
            "self_evolution_human_action": source.get("human_action", ""),
            "self_evolution_human_feedback": source.get("human_feedback", ""),
            "self_evolution_delivery_task_id": waiting_task.id,
            "self_evolution_delivery_projection_id": projection_id_for_task(waiting_task),
            "self_evolution_delivery_summary": delivery_summary,
            "self_evolution_patch_max_retries": 3,
            "organization_id": organization_id,
            "org_id": organization_id,
            "company_profile": str(getattr(plan, "profile", "") or waiting_task.metadata.get("company_profile", "") or "").strip(),
            "delegation_run_id": run_id,
            "delegation_cell_id": str(root_seat.get("cell_id", "") or team_id or "").strip(),
            "delegation_team_id": team_id,
            "delegation_team_instance_id": team_instance_id,
            "delegation_role_session_id": role_runtime_session_id,
            "session_scope_id": session_scope_id,
            "assigned_role_runtime_id": role_runtime_session_id,
            "manager_role_id": str(root_seat.get("manager_role_id", "") or "").strip(),
            "manager_seat_id": str(root_seat.get("manager_seat_id", "") or "").strip(),
            "managed_team_id": str(root_seat.get("managed_team_id", "") or "").strip(),
            "direct_report_role_ids": list(root_seat.get("direct_report_role_ids", []) or []),
            "direct_report_seat_ids": list(root_seat.get("direct_report_seat_ids", []) or []),
            "allowed_delegate_role_ids": list(root_seat.get("allowed_delegate_role_ids", []) or []),
            "contact_role_ids": list(root_seat.get("contact_role_ids", []) or []),
            "runtime_topology": runtime_topology,
            "employee_assignment": assignment,
            "prompt_contract": prompt_contract,
            "prompt_assignment": assignment_context,
            "brief": prompt_contract.get("task_brief", ""),
            "deliverables": list(assignment_context.get("deliverables", []) or []),
            "acceptance_criteria": list(assignment_context.get("acceptance_criteria", []) or []),
            "work_item_tasks": self._self_evolution_task_payloads(all_tasks),
            "org_graph": self._self_evolution_org_graph(),
            "workspace_root": self._task_runtime_value(all_tasks, "workspace_root"),
            "comms_workspace_root": self._task_runtime_value(all_tasks, "comms_workspace_root"),
            "comms_root": self._task_runtime_value(all_tasks, "comms_root"),
            "target_output_dir": self._task_runtime_value(all_tasks, "target_output_dir"),
            "output_root": self._task_runtime_value(all_tasks, "output_root"),
            "user_visible": False,
            "authoritative_output": False,
        }), projection_id=projection_id, turn_type="self_evolution")
        work_item = DelegationWorkItem(
            work_item_id=work_item_id,
            run_id=run_id,
            cell_id=str(root_seat.get("cell_id", "") or team_id or "").strip(),
            team_instance_id=team_instance_id,
            team_id=team_id,
            role_id=root_role_id,
            seat_id=seat_id,
            seat_state_id=str(root_seat.get("seat_state_id", "") or "").strip(),
            role_runtime_session_id=role_runtime_session_id,
            parent_work_item_id=str(linked_work_item_id_for_task(waiting_task) or "").strip() or None,
            source_role_id=str(waiting_task.assigned_to or waiting_task.metadata.get("work_item_role_id", "") or "").strip() or None,
            source_seat_id=str(waiting_task.metadata.get("delegation_seat_id", "") or "").strip() or None,
            title="Self-Evolution Review",
            summary=str(prompt_contract.get("task_brief", "") or "").strip(),
            kind="self_evolution",
            projection_id=projection_id,
            phase=Phase.READY,
            batch_id=f"self-evolution::{checkpoint.checkpoint_id}",
            manager_role_id=str(root_seat.get("manager_role_id", "") or "").strip(),
            manager_seat_id=str(root_seat.get("manager_seat_id", "") or "").strip(),
            metadata=metadata,
        )
        await self.store.save_delegation_work_item(work_item)
        return work_item

    async def _prepare_self_evolution_runtime_resume_tasks(
        self,
        *,
        tasks: list[Task],
        root_work_item: DelegationWorkItem,
    ) -> None:
        """準備自我演化運行時恢復任務 — 填充缺失的 delegation metadata。"""
        run_id = str(getattr(root_work_item, "run_id", "") or "").strip()
        if not run_id:
            return
        runtime_topology = dict((getattr(root_work_item, "metadata", {}) or {}).get("runtime_topology", {}) or {})
        for task in tasks:
            task.metadata = dict(getattr(task, "metadata", {}) or {})
            changed = False
            defaults = {
                "delegation_run_id": run_id,
                "execution_mode": "company_mode",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
            }
            if runtime_topology:
                defaults["runtime_topology"] = runtime_topology
            for key, value in defaults.items():
                if task.metadata.get(key) in (None, "", {}, []):
                    task.metadata[key] = value
                    changed = True
            if changed and self.store and hasattr(self.store, "save_task"):
                await self.store.save_task(task)

    async def _collect_company_self_evolution_result(
        self,
        *,
        checkpoint_id: str,
        run_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """收集公司自我演化結果 — 從工作項目 metadata 中提取已記錄的 patches。"""
        recorded: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        if not self.store or not run_id or not hasattr(self.store, "list_delegation_work_items"):
            return {"recorded": recorded, "errors": errors}
        try:
            work_items = await self.store.list_delegation_work_items(run_id)
        except Exception:
            return {"recorded": recorded, "errors": [{"error": "self_evolution_result_collection_failed"}]}
        for item in list(work_items or []):
            metadata = dict(getattr(item, "metadata", {}) or {})
            if str(metadata.get("self_evolution_checkpoint_id", "") or "").strip() != str(checkpoint_id or "").strip():
                continue
            for entry in list(metadata.get("self_evolution_recorded", []) or []):
                if isinstance(entry, dict):
                    recorded.append(dict(entry))
            error = metadata.get("self_evolution_error")
            if isinstance(error, dict):
                errors.append({
                    "work_item_id": str(getattr(item, "work_item_id", "") or "").strip(),
                    **dict(error),
                })
            phase = getattr(item, "phase", None)
            if phase == Phase.FAILED and not error:
                errors.append({
                    "work_item_id": str(getattr(item, "work_item_id", "") or "").strip(),
                    "error": "self_evolution_work_item_failed",
                })
        return {"recorded": recorded, "errors": errors}

    async def run_company_delivery_self_evolution_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        action: str,
        feedback: str = "",
        reply_metadata: dict[str, Any] | None = None,
    ) -> str:
        """執行公司交付自我演化檢查點 — 根據使用者動作觸發員工經驗更新。"""
        assert self.store
        if str(action or "").strip().lower() == "ignore":
            return await self.ignore_company_delivery_feedback_checkpoint(
                checkpoint,
                reply_metadata=reply_metadata,
            )
        checkpoint = await self._ensure_checkpoint_runtime_v2_payload(checkpoint)
        status = str(getattr(checkpoint, "status", "") or "").strip().lower()
        if status and status != "pending":
            return "This self-evolution review is no longer active."

        payload = dict(checkpoint.payload or {})
        waiting_task_id = str(payload.get("waiting_task_id", "") or payload.get("task_id", "") or "").strip()
        if not waiting_task_id:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because the delivery task reference is missing."
        waiting_task = await self.store.get_task(waiting_task_id)
        if not waiting_task:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because the delivery task no longer exists."

        self._restore_runtime_state_from_checkpoint(waiting_task, payload)
        task_ids = [
            str(task_id).strip()
            for task_id in list(payload.get("task_ids", []) or [waiting_task_id])
            if str(task_id).strip()
        ]
        if waiting_task_id not in task_ids:
            task_ids.append(waiting_task_id)
        tasks: list[Task] = []
        seen_task_ids: set[str] = set()
        for task_id in task_ids:
            task = await self.store.get_task(task_id)
            if task and task.id not in seen_task_ids:
                tasks.append(task)
                seen_task_ids.add(task.id)
        if not tasks:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because the runtime task set could not be restored."

        plan = deserialize_company_work_item_runtime_plan(payload.get("company_work_item_plan") or payload.get("plan", {}))
        organization_id = str(
            getattr(waiting_task, "org_id", "")
            or payload.get("organization_id")
            or getattr(getattr(self.config, "org", None), "organization_id", "")
            or DEFAULT_ORGANIZATION_ID
        ).strip() or DEFAULT_ORGANIZATION_ID
        root_role_id = str(
            getattr(plan, "final_decider_role_id", "")
            or plan.metadata.get("final_decider_role_id", "")
            or ""
        ).strip()
        if not root_role_id and self.org_engine:
            try:
                root_role_id = str(self.org_engine.get_final_decider_role_id(strict=False) or "").strip()
            except Exception:
                root_role_id = ""
        if not root_role_id:
            target = self._company_followup_target_task(plan, tasks)
            root_role_id = str(getattr(target, "assigned_to", "") or getattr(target, "metadata", {}).get("work_item_role_id", "") or "").strip()
        if not root_role_id:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because no final-decider role could be resolved."

        normalized_action = "feedback" if str(action or "").strip().lower() == "feedback" else "approve"
        feedback_text = str(feedback or "").strip()
        source = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": "company_delivery_feedback",
            "human_action": normalized_action,
            "human_feedback": feedback_text,
            "project_id": waiting_task.project_id or self.project_id or "default",
            "delivery_task_id": waiting_task.id,
            "delivery_projection_id": projection_id_for_task(waiting_task),
            "recorded_at": datetime.now().isoformat(),
        }
        assignments = self._self_evolution_assignments_by_role(tasks)
        if self.company_executor is None:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because company runtime is not available."
        root_work_item = await self._create_company_self_evolution_root_work_item(
            checkpoint=checkpoint,
            waiting_task=waiting_task,
            tasks=tasks,
            plan=plan,
            root_role_id=root_role_id,
            organization_id=organization_id,
            source=source,
            assignments=assignments,
        )
        if root_work_item is None:
            await self._mark_company_runtime_checkpoint_status(checkpoint, status="invalid")
            return "Could not run self-evolution because the company runtime work-item state could not be restored."

        await self._close_company_delivery_review_task(
            waiting_task,
            resolution="self_evolution_started",
            closed_at=datetime.now().isoformat(),
            checkpoint_id=str(getattr(checkpoint, "checkpoint_id", "") or "").strip(),
            metadata_updates={
                "self_evolution_review_started": True,
                "self_evolution_review_started_at": datetime.now().isoformat(),
                "self_evolution_root_work_item_id": root_work_item.work_item_id,
            },
        )
        if waiting_task.id not in seen_task_ids:
            tasks.append(waiting_task)
            seen_task_ids.add(waiting_task.id)
        await self._prepare_self_evolution_runtime_resume_tasks(
            tasks=tasks,
            root_work_item=root_work_item,
        )
        await self.company_executor.execute(plan, tasks)
        result = await self._collect_company_self_evolution_result(
            checkpoint_id=checkpoint.checkpoint_id,
            run_id=str(getattr(root_work_item, "run_id", "") or "").strip(),
        )

        waiting_task.metadata = dict(waiting_task.metadata or {})
        review_record = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "action": normalized_action,
            "feedback": feedback_text,
            "completed_at": datetime.now().isoformat(),
            "recorded_count": len(result.get("recorded", [])),
            "error_count": len(result.get("errors", [])),
        }
        history = list(waiting_task.metadata.get("self_evolution_reviews", []) or [])
        history.append(review_record)
        task_metadata_updates = {
            "self_evolution_review_completed": True,
            "self_evolution_review_completed_at": review_record["completed_at"],
            "latest_self_evolution_review": review_record,
            "self_evolution_reviews": history[-20:],
        }
        await self._terminalize_company_delivery_feedback_checkpoint(
            checkpoint,
            status="resolved",
            resolution="self_evolution_review_completed",
            payload_updates={
                **payload,
                "self_evolution_review": review_record,
                "self_evolution_recorded": list(result.get("recorded", [])),
                "self_evolution_errors": list(result.get("errors", [])),
            },
            task_metadata_updates=task_metadata_updates,
        )
        recorded_count = len(result.get("recorded", []))
        if recorded_count:
            return f"Self-evolution completed. Recorded {recorded_count} employee experience update(s)."
        errors = list(result.get("errors", []))
        if errors:
            return "Self-evolution finished without writing updates because the agents did not return valid evolution patches."
        return "Self-evolution completed. No employee experience updates were needed."

    def _self_evolution_assignments_by_role(self, tasks: list[Task]) -> dict[str, dict[str, Any]]:
        """按角色 ID 分組任務的員工分配資訊。"""
        assignments: dict[str, dict[str, Any]] = {}
        for task in tasks:
            assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
            employee_id = str(assignment.get("employee_id", "") or "").strip()
            role_id = str(
                assignment.get("role_id")
                or getattr(task, "assigned_to", "")
                or getattr(task, "metadata", {}).get("work_item_role_id", "")
                or ""
            ).strip()
            if not employee_id or not role_id:
                continue
            assignments.setdefault(role_id, {
                "employee_id": employee_id,
                "employee_name": assignment.get("name", ""),
                "template_id": assignment.get("template_id", ""),
                "role_id": role_id,
                "category": assignment.get("category", ""),
                "domains": list(assignment.get("domains", []) or []),
            })
        return assignments

    def _self_evolution_task_payloads(self, tasks: list[Task]) -> list[dict[str, Any]]:
        """建構自我演化用的任務摘要 payload 列表。"""
        payloads: list[dict[str, Any]] = []
        for task in tasks:
            assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
            result_content = ""
            if isinstance(task.result, dict):
                result_content = str(task.result.get("content", "") or "").strip()
            elif task.result:
                result_content = str(task.result or "").strip()
            payloads.append({
                "task_id": task.id,
                "title": task.title,
                "role_id": str(assignment.get("role_id") or task.assigned_to or "").strip(),
                "employee_id": str(assignment.get("employee_id", "") or "").strip(),
                "projection_id": projection_id_for_task(task),
                "turn_type": turn_type_for_task(task, fallback=""),
                "status": getattr(task.status, "value", str(task.status)),
                "summary": str(task.metadata.get("work_item_summary_for_downstream", "") or result_content).strip()[:2000],
            })
        return payloads

    def _self_evolution_org_graph(self) -> dict[str, list[str]]:
        """建構組織圖（角色 → 下屬角色列表）供自我演化使用。"""
        if not self.org_engine:
            return {}
        graph: dict[str, list[str]] = {}
        for agent in self.org_engine.list_agents():
            role_id = str(agent.role_id or "").strip()
            if role_id:
                graph.setdefault(role_id, [])
        for agent in self.org_engine.list_agents():
            role_id = str(agent.role_id or "").strip()
            manager = str(agent.reports_to or "").strip()
            if role_id and manager and manager in graph:
                graph.setdefault(manager, [])
                if role_id not in graph[manager]:
                    graph[manager].append(role_id)
        return graph
