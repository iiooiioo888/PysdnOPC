"""StaffingMixin — 招聘/人員配置相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from opc.engine._core import OPCEngine


class StaffingMixin:
    """Mixin providing 招聘/人員配置相關方法 for OPCEngine."""

    async def _session_has_completed_recruitment_confirmation(self, session_id: str | None) -> bool:
        if not self.store or not session_id:
            return False
        session = await self.store.get_session(session_id)
        if not session:
            return False
        metadata = dict(session.metadata or {})
        recruitment_state = metadata.get("recruitment_confirmation")
        if isinstance(recruitment_state, dict):
            return bool(recruitment_state.get("completed"))
        if metadata.get("recruitment_confirmation_completed"):
            return True
        tasks = await self.store.get_tasks(project_id=session.project_id or (self.project_id or "default"))
        for task in tasks:
            parent_session_id = str(
                getattr(task, "parent_session_id", "")
                or dict(getattr(task, "metadata", {}) or {}).get("parent_session_id", "")
                or ""
            ).strip()
            if parent_session_id == session_id:
                return True
        return False

    async def _mark_session_recruitment_confirmation_completed(
        self,
        session_id: str | None,
        *,
        source: str,
    ) -> None:
        if not self.store or not session_id:
            return
        session = await self.store.get_session(session_id)
        if not session:
            await self._ensure_primary_session(session_id)
            session = await self.store.get_session(session_id)
        if not session:
            return
        metadata = dict(session.metadata or {})
        previous = metadata.get("recruitment_confirmation")
        previous_state = dict(previous) if isinstance(previous, dict) else {}
        metadata["recruitment_confirmation"] = {
            **previous_state,
            "completed": True,
            "source": source,
            "project_id": session.project_id,
            "completed_at": datetime.now().isoformat(),
        }
        metadata["recruitment_confirmation_completed"] = True
        session.metadata = metadata
        session.updated_at = datetime.now()
        await self.store.save_session(session)

    @staticmethod
    def _is_placeholder_staffing_employee(employee: Any) -> bool:
        metadata = dict(getattr(employee, "metadata", {}) or {})
        return bool(metadata.get("is_default_employee") or metadata.get("is_fallback_employee"))

    def _active_staffing_employees_by_id(self) -> dict[str, Any]:
        employees_by_id: dict[str, Any] = {}
        for employee in (self.org_engine.list_employees() if self.org_engine else []):
            employee_id = str(getattr(employee, "employee_id", "") or "").strip()
            if not employee_id or self._is_placeholder_staffing_employee(employee):
                continue
            employees_by_id[employee_id] = employee
            legacy_ids = list(dict(getattr(employee, "metadata", {}) or {}).get("legacy_employee_ids", []) or [])
            for legacy_id in legacy_ids:
                legacy = str(legacy_id or "").strip()
                if legacy:
                    employees_by_id[legacy] = employee
        return employees_by_id

    def _canonical_staffing_employee_id(self, employee: Any, fallback: str = "") -> str:
        if employee is not None and not self._is_placeholder_staffing_employee(employee):
            template_id = str(getattr(employee, "template_id", "") or "").strip()
            if template_id:
                return template_id
            employee_id = str(getattr(employee, "employee_id", "") or "").strip()
            if employee_id:
                return employee_id
        return str(fallback or "").strip()

    def _staffing_employee_payload(self, employee: Any) -> dict[str, Any]:
        employee_id = str(getattr(employee, "employee_id", "") or "").strip()
        role_id = str(getattr(employee, "role_id", "") or "").strip()
        metadata = dict(getattr(employee, "metadata", {}) or {})
        role_ids: list[str] = []
        role_getter = getattr(self.org_engine, "employee_role_ids", None) if self.org_engine else None
        if callable(role_getter):
            try:
                role_ids = list(role_getter(employee))
            except Exception:
                role_ids = []
        if not role_ids:
            for value in [
                role_id,
                metadata.get("home_role_id"),
                *list(metadata.get("home_role_ids", []) or []),
                *list(metadata.get("staffed_role_ids", []) or []),
            ]:
                normalized_role_id = str(value or "").strip()
                if normalized_role_id and normalized_role_id not in role_ids:
                    role_ids.append(normalized_role_id)
        experience_score = 0.0
        if self.org_engine and self.org_engine.employee_evolution:
            try:
                experience_score = self.org_engine.employee_evolution.get_experience_score(
                    employee_id,
                    role_id=role_id,
                    domains=[],
                    project_id=self.project_id or "default",
                )
            except Exception:
                experience_score = 0.0
        return {
            "kind": "employee",
            "employee_id": employee_id,
            "employee_name": str(getattr(employee, "name", "") or employee_id),
            "template_id": str(getattr(employee, "template_id", "") or ""),
            "role_id": role_id,
            "role_ids": role_ids,
            "home_role_id": str(metadata.get("home_role_id") or role_id),
            "category": str(getattr(employee, "category", "") or ""),
            "domains": list(getattr(employee, "domains", []) or []),
            "tags": list(getattr(employee, "tags", []) or []),
            "description": str(getattr(employee, "description", "") or ""),
            "preferred_external_agent": getattr(employee, "preferred_external_agent", None),
            "experience_score": experience_score,
        }

    def _staffing_template_payload(self, template: Any) -> dict[str, Any]:
        template_id = str(getattr(template, "id", "") or "").strip()
        return {
            "kind": "template",
            "template_id": template_id,
            "template_name": str(getattr(template, "name", "") or template_id),
            "category": str(getattr(template, "category", "") or ""),
            "domains": list(getattr(template, "domains", []) or []),
            "tags": list(getattr(template, "tags", []) or []),
            "description": str(getattr(template, "description", "") or ""),
            "preferred_external_agent": getattr(template, "preferred_external_agent", None),
            "source_repo": str(getattr(template, "source_repo", "") or ""),
            "source_path": str(getattr(template, "source_path", "") or ""),
        }

    def _project_company_staffing_defaults_path(self, project_id: str | None = None) -> Path | None:
        project = str(project_id or self.project_id or "default").strip() or "default"
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", project):
            logger.warning(f"Skipping company staffing defaults for unsafe project_id={project!r}")
            return None
        return self.opc_home / "projects" / project / "company_staffing_defaults.json"

    def _company_staffing_scope_key(
        self,
        decision: ModeSelection | None,
        *,
        company_profile: str | None = None,
    ) -> str:
        profile = str(
            company_profile
            or getattr(decision, "company_profile", "")
            or getattr(getattr(self.config, "org", None), "company_profile", "")
            or CompanyProfile.CORPORATE.value
        ).strip().lower() or CompanyProfile.CORPORATE.value
        org_cfg = getattr(self.config, "org", None)
        org_key = str(getattr(decision, "org_id", "") or "").strip()
        if not org_key:
            org_key = str(getattr(org_cfg, "organization_id", "") or "").strip()
        if not org_key:
            org_key = str(getattr(org_cfg, "organization_config_file", "") or "").strip()
        if not org_key:
            org_key = DEFAULT_ORGANIZATION_ID
        return f"profile:{profile}|org:{org_key}"

    def _load_project_company_staffing_defaults(
        self,
        decision: ModeSelection | None,
        *,
        company_profile: str | None = None,
    ) -> dict[str, Any]:
        path = self._project_company_staffing_defaults_path()
        if path is None or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.opt(exception=True).debug("Failed to load project company staffing defaults")
            return {}
        scopes = dict(data.get("scopes", {}) or {}) if isinstance(data, dict) else {}
        return dict(scopes.get(self._company_staffing_scope_key(decision, company_profile=company_profile), {}) or {})

    def _saved_staffing_defaults_to_runtime_overrides(
        self,
        decision: ModeSelection | None,
        *,
        company_profile: str | None = None,
    ) -> tuple[dict[str, str], dict[str, str], set[str], dict[str, str]] | None:
        saved_defaults = self._load_project_company_staffing_defaults(
            decision,
            company_profile=company_profile,
        )
        saved_selections = dict(saved_defaults.get("staffing_selections", {}) or {})
        if not saved_selections:
            return None

        active_role_ids = {
            str(getattr(agent, "role_id", "") or "").strip()
            for agent in (self.org_engine.list_agents() if self.org_engine else [])
            if str(getattr(agent, "role_id", "") or "").strip()
            and str(getattr(agent, "role_id", "") or "").strip() != "task_generalist"
        }
        staffing_overrides: dict[str, str] = {}
        staffing_experience_modes: dict[str, str] = {}
        fallback_role_ids: set[str] = set()

        for raw_role_id, raw_selection in saved_selections.items():
            role_id = str(raw_role_id or "").strip()
            if not role_id or (active_role_ids and role_id not in active_role_ids):
                continue
            selection = self._normalize_staffing_selection(raw_selection)
            kind = selection.get("kind", "fallback")
            selected_id = str(selection.get("id", "") or "").strip()
            if kind == "employee" and selected_id:
                staffing_overrides[role_id] = selected_id
                raw_mode = raw_selection.get("experience_mode") if isinstance(raw_selection, dict) else ""
                staffing_experience_modes[role_id] = self._normalize_staffing_experience_mode(raw_mode)
                continue
            if kind == "template" and selected_id:
                staffing_overrides[role_id] = selected_id
                staffing_experience_modes[role_id] = "template_only"
                continue
            fallback_role_ids.add(role_id)

        role_agent_overrides: dict[str, str] = {}
        for raw_role_id, raw_agent in dict(saved_defaults.get("recruitment_role_agents", {}) or {}).items():
            role_id = str(raw_role_id or "").strip()
            if not role_id or (active_role_ids and role_id not in active_role_ids):
                continue
            agent = normalize_recruitment_agent_choice(raw_agent)
            if agent:
                role_agent_overrides[role_id] = agent

        return staffing_overrides, staffing_experience_modes, fallback_role_ids, role_agent_overrides

    def _validated_saved_staffing_selection(
        self,
        raw_selection: Any,
        *,
        employee_ids: set[str],
        template_ids: set[str],
    ) -> dict[str, str]:
        selection = self._normalize_staffing_selection(raw_selection)
        kind = selection.get("kind", "fallback")
        selected_id = str(selection.get("id", "") or "").strip()
        if kind == "employee" and selected_id in employee_ids:
            return {"kind": "employee", "id": selected_id, "employee_id": selected_id}
        if kind == "template" and selected_id in template_ids:
            return {"kind": "template", "id": selected_id, "template_id": selected_id}
        return {"kind": "fallback", "id": ""}

    def _save_project_company_staffing_defaults(
        self,
        decision: ModeSelection | None,
        *,
        company_profile: str | None = None,
        role_ids: set[str] | list[str] | tuple[str, ...],
        staffing_overrides: dict[str, str] | None,
        staffing_experience_modes: dict[str, str] | None,
        fallback_role_ids: set[str] | list[str] | tuple[str, ...],
        role_agent_overrides: dict[str, str] | None,
    ) -> None:
        path = self._project_company_staffing_defaults_path()
        if path is None:
            return
        normalized_role_ids = {
            str(role_id or "").strip()
            for role_id in list(role_ids or [])
            if str(role_id or "").strip()
        }
        if not normalized_role_ids:
            return
        fallback_set = {
            str(role_id or "").strip()
            for role_id in list(fallback_role_ids or [])
            if str(role_id or "").strip()
        }
        staffing = {
            str(role_id or "").strip(): str(employee_id or "").strip()
            for role_id, employee_id in dict(staffing_overrides or {}).items()
            if str(role_id or "").strip() and str(employee_id or "").strip()
        }
        experience_modes = {
            str(role_id or "").strip(): self._normalize_staffing_experience_mode(mode)
            for role_id, mode in dict(staffing_experience_modes or {}).items()
            if str(role_id or "").strip()
        }
        role_agents = {
            str(role_id or "").strip(): normalize_recruitment_agent_choice(agent, default="codex") or "codex"
            for role_id, agent in dict(role_agent_overrides or {}).items()
            if str(role_id or "").strip()
        }
        selections: dict[str, dict[str, str]] = {}
        agents: dict[str, str] = {}
        for role_id in sorted(normalized_role_ids):
            if role_id in staffing:
                if experience_modes.get(role_id) == "template_only":
                    selections[role_id] = {
                        "kind": "template",
                        "id": staffing[role_id],
                        "template_id": staffing[role_id],
                        "experience_mode": "template_only",
                    }
                else:
                    selections[role_id] = {
                        "kind": "employee",
                        "id": staffing[role_id],
                        "employee_id": staffing[role_id],
                        "experience_mode": "with_experience",
                    }
            else:
                selections[role_id] = {"kind": "fallback", "id": ""}
            if role_id in fallback_set:
                selections[role_id] = {"kind": "fallback", "id": ""}
            agents[role_id] = role_agents.get(role_id) or "codex"

        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        scopes = dict(data.get("scopes", {}) or {})
        scope_key = self._company_staffing_scope_key(decision, company_profile=company_profile)
        profile = str(company_profile or getattr(decision, "company_profile", "") or CompanyProfile.CORPORATE.value).strip().lower()
        scopes[scope_key] = {
            "company_profile": profile or CompanyProfile.CORPORATE.value,
            "org_id": str(getattr(decision, "org_id", "") or "").strip(),
            "updated_at": datetime.now().isoformat(),
            "staffing_selections": selections,
            "recruitment_role_agents": agents,
        }
        data.update({"version": 1, "updated_at": datetime.now().isoformat(), "scopes": scopes})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    def _build_manual_staffing_checkpoint_payload(
        self,
        decision: ModeSelection,
        original_message: str,
        runtime_spec: CompanyRuntimeSpec,
        *,
        session_id: str,
        origin_channel: str,
        origin_chat_id: str,
        origin_thread_id: str,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        force_manual_preflight: bool = False,
    ) -> dict[str, Any] | None:
        if not self.org_engine or not self.talent_market:
            return None
        employees = [
            employee
            for employee in self.org_engine.list_employees()
            if not self._is_placeholder_staffing_employee(employee)
        ]
        employee_payloads = [self._staffing_employee_payload(employee) for employee in employees]
        employee_by_role: dict[str, list[dict[str, Any]]] = {}
        for employee_payload in employee_payloads:
            role_ids = [
                str(item or "").strip()
                for item in list(employee_payload.get("role_ids", []) or [])
                if str(item or "").strip()
            ] or [str(employee_payload.get("role_id", "") or "").strip()]
            for role_id in role_ids:
                if role_id:
                    employee_by_role.setdefault(role_id, []).append(employee_payload)
        template_payloads = [
            self._staffing_template_payload(template)
            for template in self.talent_market.list_available_templates()
            if str(getattr(template, "id", "") or "").strip()
        ]
        saved_defaults = self._load_project_company_staffing_defaults(
            decision,
            company_profile=str(runtime_spec.profile or "corporate"),
        )
        saved_selections = dict(saved_defaults.get("staffing_selections", {}) or {})
        saved_agents = dict(saved_defaults.get("recruitment_role_agents", {}) or {})
        employee_ids = {
            str(item.get("employee_id", "") or "").strip()
            for item in employee_payloads
            if str(item.get("employee_id", "") or "").strip()
        }
        template_ids = {
            str(item.get("template_id", "") or "").strip()
            for item in template_payloads
            if str(item.get("template_id", "") or "").strip()
        }
        roles: list[dict[str, Any]] = []
        default_agent = "codex"
        for agent in self.org_engine.list_agents():
            role_id = str(getattr(agent, "role_id", "") or "").strip()
            if not role_id or role_id == "task_generalist":
                continue
            same_role_employees = employee_by_role.get(role_id, [])
            default_selection: dict[str, Any] = {"kind": "fallback"}
            default_source = "system"
            if role_id in saved_selections:
                default_selection = self._validated_saved_staffing_selection(
                    saved_selections.get(role_id),
                    employee_ids=employee_ids,
                    template_ids=template_ids,
                )
                if default_selection.get("kind") == "fallback" and same_role_employees:
                    default_selection = {
                        "kind": "employee",
                        "id": same_role_employees[0]["employee_id"],
                        "employee_id": same_role_employees[0]["employee_id"],
                    }
                    default_source = "org"
                else:
                    default_source = "project" if default_selection.get("kind") != "fallback" or role_id in saved_agents else "system"
            elif same_role_employees:
                default_selection = {
                    "kind": "employee",
                    "id": same_role_employees[0]["employee_id"],
                    "employee_id": same_role_employees[0]["employee_id"],
                }
                default_source = "org"
            selected_agent = normalize_recruitment_agent_choice(saved_agents.get(role_id), default=default_agent) or default_agent
            roles.append(
                {
                    "role_id": role_id,
                    "role_label": str(getattr(agent, "name", "") or role_id),
                    "role_responsibility": str(getattr(agent, "responsibility", "") or ""),
                    "default_selection": default_selection,
                    "same_role_employee_ids": [
                        str(item.get("employee_id", "") or "")
                        for item in same_role_employees
                        if str(item.get("employee_id", "") or "")
                    ],
                    "fallback_available": True,
                    "default_agent": default_agent,
                    "selected_agent": selected_agent,
                    "default_source": default_source,
                }
            )
        if not roles:
            return None
        role_agent_overrides = {
            str(role.get("role_id", "") or "").strip(): str(role.get("selected_agent", "") or "").strip()
            for role in roles
            if str(role.get("role_id", "") or "").strip() and str(role.get("selected_agent", "") or "").strip()
        }
        has_employees = bool(employee_payloads)
        has_templates = bool(template_payloads)
        if has_employees:
            staffing_strategy = "existing_staffing"
            recommended_action = "manual_approve"
            summary = "Review existing employees, optional hires, and execution agents before company runtime starts."
        elif has_templates:
            staffing_strategy = "initial_recruitment"
            recommended_action = "auto_recruit"
            summary = "No employees are staffed yet. Recruit from talent templates or choose templates manually before company runtime starts."
        else:
            staffing_strategy = "role_only_fallback"
            recommended_action = "manual_approve"
            summary = "No employees or talent templates are available. Approving will use role-only fallback execution."
        return {
            "original_message": original_message,
            "decision": self._serialize_router_decision(decision),
            "runtime_spec": serialize_company_runtime_spec(runtime_spec),
            "primary_session_id": session_id,
            "origin_channel": origin_channel,
            "origin_chat_id": origin_chat_id,
            "origin_thread_id": origin_thread_id,
            "origin_task_id": origin_task_id,
            "attachment_refs": self._normalize_attachment_refs(attachment_refs),
            "company_profile": str(runtime_spec.profile or "corporate"),
            "summary": summary,
            "staffing_strategy": staffing_strategy,
            "recommended_action": recommended_action,
            "force_manual_preflight": bool(force_manual_preflight),
            "recruitment_agent": "native",
            "staffing_defaults": {
                "source": "project" if saved_defaults else "system",
                "scope_key": self._company_staffing_scope_key(
                    decision,
                    company_profile=str(runtime_spec.profile or "corporate"),
                ),
                "updated_at": str(saved_defaults.get("updated_at", "") or ""),
            },
            "staffing_roles": roles,
            "recruitment_role_agents": role_agent_overrides,
            "staffing_pool": {
                "employees": employee_payloads,
                "templates": template_payloads,
            },
        }

    def _render_manual_staffing_summary(self, payload: dict[str, Any]) -> str:
        employees_by_id = {
            str(item.get("employee_id", "") or ""): item
            for item in list(dict(payload.get("staffing_pool", {}) or {}).get("employees", []) or [])
        }
        lines = [
            "Company mode has a pending manual staffing selection before execution.",
            "",
            f"Company profile: `{payload.get('company_profile', 'corporate')}`",
        ]
        summary = str(payload.get("summary", "") or "").strip()
        if summary:
            lines.extend(["", summary])
        recommended_action = str(payload.get("recommended_action", "") or "").strip()
        if recommended_action:
            lines.extend(["", f"Recommended action: `{recommended_action}`"])
        lines.extend(["", "Manual staffing defaults:"])
        for role in list(payload.get("staffing_roles", []) or []):
            role_id = str(role.get("role_id", "") or "").strip()
            role_label = str(role.get("role_label", "") or role_id).strip()
            selection = dict(role.get("default_selection", {}) or {})
            if selection.get("kind") == "employee":
                employee_id = str(selection.get("employee_id") or selection.get("id") or "").strip()
                employee = employees_by_id.get(employee_id, {})
                employee_name = str(employee.get("employee_name", "") or employee_id)
                lines.append(f"- Role `{role_id}` ({role_label}): `{employee_name}` ({employee_id})")
            else:
                lines.append(f"- Role `{role_id}` ({role_label}): fallback to role-only execution")
        lines.extend(
            [
                "",
                "Reply `approve` to use these defaults, or include overrides like `approve senior_engineer=tpl:engineering-frontend-developer`.",
                "Reply `auto` or `auto recruit` to run automatic recruitment.",
                "Reply `deny` / `stop` to cancel company-mode execution.",
            ]
        )
        return "\n".join(lines)

    async def _begin_company_staffing_loop(
        self,
        decision: ModeSelection,
        original_message: str,
        runtime_spec: CompanyRuntimeSpec,
        *,
        session_id: str,
        origin_channel: str,
        origin_chat_id: str,
        origin_thread_id: str,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        force_manual_preflight: bool = False,
    ) -> str:
        session_confirmed = await self._session_has_completed_recruitment_confirmation(session_id)
        if not session_confirmed:
            payload = self._build_manual_staffing_checkpoint_payload(
                decision,
                original_message,
                runtime_spec,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                force_manual_preflight=force_manual_preflight,
            )
            if payload is not None:
                await self._save_execution_checkpoint(
                    {
                        "project_id": self.project_id or "default",
                        "session_id": session_id,
                        "checkpoint_type": "company_staffing_selection",
                        "payload": payload,
                    }
                )
                return self._render_manual_staffing_summary(payload)
        else:
            saved_runtime_staffing = self._saved_staffing_defaults_to_runtime_overrides(
                decision,
                company_profile=str(runtime_spec.profile or "corporate"),
            )
            if saved_runtime_staffing is not None:
                (
                    staffing_overrides,
                    staffing_experience_modes,
                    fallback_role_ids,
                    role_agent_overrides,
                ) = saved_runtime_staffing
                return await self._continue_company_mode_execution(
                    decision,
                    original_message,
                    runtime_spec,
                    session_id=session_id,
                    origin_channel=origin_channel,
                    origin_chat_id=origin_chat_id,
                    origin_thread_id=origin_thread_id,
                    origin_task_id=origin_task_id,
                    staffing_overrides=staffing_overrides,
                    staffing_experience_modes=staffing_experience_modes,
                    fallback_role_ids=fallback_role_ids,
                    role_agent_overrides=role_agent_overrides,
                    attachment_refs=attachment_refs,
                )
        return await self._begin_company_recruitment_loop(
            decision,
            original_message,
            runtime_spec,
            session_id=session_id,
            origin_channel=origin_channel,
            origin_chat_id=origin_chat_id,
            origin_thread_id=origin_thread_id,
            origin_task_id=origin_task_id,
            attachment_refs=attachment_refs,
        )

    def _resolve_recruitment_llm(self, recruitment_agent: str | None) -> tuple[Any | None, str]:
        normalized = normalize_recruitment_agent_choice(
            recruitment_agent,
            default="native",
        ) or "native"
        if normalized == "native":
            return self.llm, normalized
        return ExternalRecruiterLLMAdapter(self, normalized), normalized

    async def _begin_company_recruitment_loop(
        self,
        decision: ModeSelection,
        original_message: str,
        runtime_spec: CompanyRuntimeSpec,
        *,
        session_id: str,
        origin_channel: str,
        origin_chat_id: str,
        origin_thread_id: str,
        origin_task_id: str | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        force_confirmation: bool = False,
        role_agent_overrides: dict[str, str] | None = None,
        recruitment_agent: str | None = None,
    ) -> str:
        assert self.company_recruiter
        recruitment_llm, selected_recruitment_agent = self._resolve_recruitment_llm(recruitment_agent)
        recruitment_plan = await self.company_recruiter.build_recruitment_plan(
            runtime_spec,
            domains=decision.domains,
            project_id=self.project_id or "default",
            recruitment_llm=recruitment_llm,
            recruitment_agent=selected_recruitment_agent,
        )
        recruitment_plan.metadata = dict(getattr(recruitment_plan, "metadata", {}) or {})
        recruitment_plan.metadata.setdefault("recruitment_revision", 1)
        recruitment_plan.metadata["recruitment_agent"] = selected_recruitment_agent
        apply_recruitment_role_agent_overrides(recruitment_plan, role_agent_overrides)
        requires_confirmation = bool(force_confirmation) or recruitment_plan_requires_confirmation(recruitment_plan)
        session_confirmed = await self._session_has_completed_recruitment_confirmation(session_id)
        role_agent_overrides = extract_recruitment_role_agent_overrides(recruitment_plan)
        fallback_role_ids = build_fallback_role_ids(recruitment_plan)
        if not requires_confirmation or session_confirmed:
            if self.talent_market:
                for proposal in recruitment_plan.proposals:
                    if proposal.status != "proposed_hire" or not proposal.candidate:
                        continue
                    employee = self.talent_market.ensure_hire_template(
                        proposal.candidate.template_id,
                        proposal.role_id,
                        employee_name=proposal.candidate.proposed_employee_name,
                        employee_id=proposal.candidate.proposed_employee_id,
                    )
                    proposal.candidate.proposed_employee_id = employee.employee_id
                    proposal.candidate.proposed_employee_name = employee.name
                self.config.save(self.opc_home / "config")
                if self.org_engine:
                    self.org_engine.reload_from_config()
            staffing_overrides = build_staffing_overrides(recruitment_plan)
            staffing_experience_modes = build_staffing_experience_modes(recruitment_plan)
            if decision.mode == ExecutionMode.COMPANY_MODE:
                return await self._continue_company_mode_execution(
                    decision,
                    original_message,
                    runtime_spec,
                    session_id=session_id,
                    origin_channel=origin_channel,
                    origin_chat_id=origin_chat_id,
                    origin_thread_id=origin_thread_id,
                    origin_task_id=origin_task_id,
                    staffing_overrides=staffing_overrides,
                    staffing_experience_modes=staffing_experience_modes,
                    fallback_role_ids=fallback_role_ids,
                    role_agent_overrides=role_agent_overrides,
                    attachment_refs=attachment_refs,
                )
            return await self._continue_task_mode_execution(
                decision,
                original_message,
                None,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                staffing_overrides=staffing_overrides,
                staffing_experience_modes=staffing_experience_modes,
                fallback_role_ids=fallback_role_ids,
                role_agent_overrides=role_agent_overrides,
                attachment_refs=attachment_refs,
            )
        await self._save_execution_checkpoint(
            {
                "project_id": self.project_id or "default",
                "session_id": session_id,
                "checkpoint_type": "company_recruitment_confirmation",
                "payload": {
                    "original_message": original_message,
                    "decision": self._serialize_router_decision(decision),
                    "runtime_spec": serialize_company_runtime_spec(runtime_spec),
                    "recruitment_plan": serialize_recruitment_plan(recruitment_plan),
                    "recruitment_revision": 1,
                    "primary_session_id": session_id,
                    "origin_channel": origin_channel,
                    "origin_chat_id": origin_chat_id,
                    "origin_thread_id": origin_thread_id,
                    "origin_task_id": origin_task_id,
                    "attachment_refs": self._normalize_attachment_refs(attachment_refs),
                    "recruitment_role_agents": role_agent_overrides,
                    "recruitment_agent": selected_recruitment_agent,
                },
            }
        )
        return recruitment_plan.summary or self.company_recruiter.render_recruitment_summary(recruitment_plan)

    def _should_auto_confirm_recruitment_plan(self, recruitment_plan: Any) -> bool:
        proposals = list(getattr(recruitment_plan, "proposals", []) or [])
        if not proposals:
            return True
        if list(getattr(recruitment_plan, "recruiter_feedback", []) or []):
            return False
        common_categories = {
            "general",
            "software-engineering",
            "quality-assurance",
            "design",
            "documentation",
            "project-management",
            "operations",
        }
        common_roles = {
            str(agent.role_id).strip()
            for agent in self.org_engine.list_agents()
            if str(agent.role_id).strip()
        }
        for proposal in proposals:
            role_id = str(getattr(proposal, "role_id", "") or "").strip()
            status = str(getattr(proposal, "status", "") or "").strip()
            metadata = dict(getattr(proposal, "metadata", {}) or {})
            candidate = getattr(proposal, "candidate", None)
            existing_ids = list(getattr(proposal, "existing_employee_ids", []) or [])
            staffing_payload = {
                "role_id": role_id,
                "status": status,
                "existing_employee_ids": existing_ids,
                "candidate_category": str(getattr(candidate, "category", "") or "").strip(),
                "candidate_domains": list(getattr(candidate, "domains", []) or []),
                "triage_action": str(metadata.get("triage_action", "") or "").strip(),
            }
            if self.secretary_policies:
                policy_hit = self.secretary_policies.evaluate_tool_policy(
                    project_id=self.project_id or "default",
                    tool_name="company_staffing",
                    arguments=staffing_payload,
                    safe_command_prefixes=[],
                )
                if policy_hit and policy_hit.get("effect") == "auto_allow":
                    continue
            if status in {"direct_role_execution", "existing_staff"}:
                continue
            candidate_category = str(getattr(candidate, "category", "") or "").strip().lower()
            candidate_domains = [
                str(item).strip().lower()
                for item in list(getattr(candidate, "domains", []) or [])
                if str(item).strip()
            ]
            if (
                status == "proposed_hire"
                and role_id in common_roles
                and candidate_category in common_categories
                and len(candidate_domains) <= 1
                and len(existing_ids) <= 1
            ):
                continue
            return False
        return True

    def _normalize_staffing_selection(self, value: Any) -> dict[str, str]:
        """標準化人員配置選擇值為 {role_id: agent_id} 字典。"""
        if isinstance(value, dict):
            kind = str(value.get("kind") or value.get("source") or "").strip().lower()
            selected_id = str(
                value.get("id")
                or value.get("employee_id")
                or value.get("template_id")
                or ""
            ).strip()
        else:
            text = str(value or "").strip()
            if ":" in text:
                kind, selected_id = text.split(":", 1)
                kind = kind.strip().lower()
                selected_id = selected_id.strip()
            else:
                kind, selected_id = text.strip().lower(), ""
        if kind in {"emp", "employee", "existing"}:
            return {"kind": "employee", "id": selected_id}
        if kind in {"tpl", "template", "talent"}:
            return {"kind": "template", "id": selected_id}
        if kind in {"fallback", "role", "role-only", "role_only", "none", ""}:
            return {"kind": "fallback", "id": ""}
        return {"kind": kind, "id": selected_id}

    @staticmethod
    def _normalize_staffing_experience_mode(value: Any) -> str:
        """標準化人員配置經驗模式字串。"""
        return "template_only" if str(value or "").strip() == "template_only" else "with_experience"

    def _parse_cli_staffing_selection_overrides(self, reply: str) -> dict[str, dict[str, str]]:
        """解析 CLI 人員配置選擇覆寫（格式：role=agent 每行一個）。"""
        overrides: dict[str, dict[str, str]] = {}
        for token in re.split(r"[\s,]+", reply.strip()):
            if "=" not in token:
                continue
            raw_role_id, raw_selection = token.split("=", 1)
            role_id = raw_role_id.strip()
            if not role_id:
                continue
            selection = self._normalize_staffing_selection(raw_selection)
            if selection.get("kind") in {"employee", "template", "fallback"}:
                overrides[role_id] = selection
        return overrides

    def _staffing_role_agent_overrides(
        self,
        payload: dict[str, Any],
        reply_metadata: dict[str, Any] | None,
    ) -> dict[str, str]:
        """從 checkpoint payload 和回覆 metadata 中提取角色→代理覆寫映射。"""
        overrides: dict[str, str] = {}
        for role in list(payload.get("staffing_roles", []) or []):
            role_id = str(role.get("role_id", "") or "").strip()
            if not role_id:
                continue
            selected = normalize_recruitment_agent_choice(
                role.get("selected_agent"),
                default=str(role.get("default_agent", "") or "codex"),
            )
            if selected:
                overrides[role_id] = selected
        raw_role_agents = dict(reply_metadata or {}).get("recruitment_role_agents")
        if isinstance(raw_role_agents, dict):
            for raw_role_id, raw_agent in raw_role_agents.items():
                role_id = str(raw_role_id or "").strip()
                agent = normalize_recruitment_agent_choice(raw_agent)
                if role_id and agent:
                    overrides[role_id] = agent
        return overrides

    async def _resume_staffing_selection_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
        *,
        reply_metadata: dict[str, Any] | None = None,
    ) -> str:
        """恢復人員配置選擇檢查點 — 處理使用者對角色代理選擇的確認。"""
        assert self.store and self.talent_market
        payload = checkpoint.payload
        original_message = str(payload.get("original_message", ""))
        if not original_message:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume staffing because the original request is missing."

        reply = user_reply.strip()
        normalized = reply.lower()
        reply_metadata = dict(reply_metadata or {})
        staffing_action = str(reply_metadata.get("staffing_action", "") or "").strip().lower()
        approved_tokens = {"1", "y", "yes", "ok", "okay", "approve", "approved", "confirm", "continue", "proceed", "go"}
        auto_tokens = {"auto", "auto_recruit", "auto recruit", "automatic", "automatic recruitment", "recruit"}
        denied_tokens = {"2", "n", "no", "deny", "denied", "reject", "rejected", "stop", "cancel", "abort"}

        decision = self._deserialize_router_decision(dict(payload.get("decision", {})))
        if payload.get("runtime_spec"):
            runtime_spec = deserialize_company_runtime_spec(dict(payload.get("runtime_spec", {})))
        else:
            runtime_spec = CompanyRuntimeSpec(
                profile=str(payload.get("company_profile", "") or getattr(decision, "company_profile", "") or CompanyProfile.CORPORATE.value),
                original_request=original_message,
                runtime_model="multi_team_org",
                work_item_driven=True,
                metadata={
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "work_item_driven": True,
                    "original_request": original_message,
                },
            )
        session_id = str(payload.get("primary_session_id") or checkpoint.session_id or str(uuid.uuid4()))
        origin_channel = str(payload.get("origin_channel", "cli"))
        origin_chat_id = str(payload.get("origin_chat_id", ""))
        origin_thread_id = str(payload.get("origin_thread_id", ""))
        origin_task_id = str(payload.get("origin_task_id", "")).strip() or None
        attachment_refs = self._normalize_attachment_refs(payload.get("attachment_refs", []))

        if staffing_action == "auto_recruit" or normalized in auto_tokens:
            role_agent_overrides = self._staffing_role_agent_overrides(payload, reply_metadata)
            recruitment_agent = normalize_recruitment_agent_choice(
                reply_metadata.get("recruitment_agent") or payload.get("recruitment_agent"),
                default="native",
            ) or "native"
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            return await self._begin_company_recruitment_loop(
                decision,
                original_message,
                runtime_spec,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                attachment_refs=attachment_refs,
                force_confirmation=True,
                role_agent_overrides=role_agent_overrides,
                recruitment_agent=recruitment_agent,
            )

        if normalized in denied_tokens or staffing_action == "deny":
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            return "Manual staffing was cancelled. Execution will not continue."

        cli_overrides = self._parse_cli_staffing_selection_overrides(reply)
        is_approve = (
            staffing_action in {"manual_approve", "approve"}
            or normalized in approved_tokens
            or normalized.startswith("approve ")
            or bool(cli_overrides)
        )
        if not is_approve:
            return self._render_manual_staffing_summary(payload)

        role_ids = {
            str(role.get("role_id", "") or "").strip()
            for role in list(payload.get("staffing_roles", []) or [])
            if str(role.get("role_id", "") or "").strip()
        }
        selections: dict[str, dict[str, str]] = {}
        for role in list(payload.get("staffing_roles", []) or []):
            role_id = str(role.get("role_id", "") or "").strip()
            if not role_id:
                continue
            selections[role_id] = self._normalize_staffing_selection(role.get("default_selection", {}))

        raw_metadata_selections = reply_metadata.get("staffing_selections")
        if isinstance(raw_metadata_selections, dict):
            for raw_role_id, raw_selection in raw_metadata_selections.items():
                role_id = str(raw_role_id or "").strip()
                if role_id in role_ids:
                    selections[role_id] = self._normalize_staffing_selection(raw_selection)
        for role_id, selection in cli_overrides.items():
            if role_id in role_ids:
                selections[role_id] = selection

        active_employees = self._active_staffing_employees_by_id()
        available_templates = {
            str(getattr(template, "id", "") or "").strip(): template
            for template in self.talent_market.list_available_templates()
            if str(getattr(template, "id", "") or "").strip()
        }

        staffing_overrides: dict[str, str] = {}
        staffing_experience_modes: dict[str, str] = {}
        fallback_role_ids: set[str] = set()
        hired_messages: list[str] = []
        errors: list[str] = []
        for role_id in sorted(role_ids):
            selection = selections.get(role_id, {"kind": "fallback", "id": ""})
            kind = selection.get("kind", "fallback")
            selected_id = str(selection.get("id", "") or "").strip()
            if kind == "employee":
                if selected_id not in active_employees:
                    errors.append(f"Role `{role_id}` selected unknown employee `{selected_id}`.")
                    continue
                staffing_overrides[role_id] = self._canonical_staffing_employee_id(active_employees[selected_id], selected_id)
                staffing_experience_modes[role_id] = "with_experience"
                continue
            if kind == "template":
                if selected_id not in available_templates:
                    errors.append(f"Role `{role_id}` selected unknown template `{selected_id}`.")
                    continue
                employee = self.talent_market.ensure_hire_template(
                    selected_id,
                    role_id,
                    employee_name=str(getattr(available_templates[selected_id], "name", "") or ""),
                )
                staffing_overrides[role_id] = employee.employee_id
                staffing_experience_modes[role_id] = "template_only"
                hired_messages.append(f"- {employee.name} ({employee.employee_id}) -> {role_id}")
                continue
            fallback_role_ids.add(role_id)

        if errors:
            return "Could not apply manual staffing:\n" + "\n".join(f"- {item}" for item in errors) + "\n\n" + self._render_manual_staffing_summary(payload)

        self.config.save(self.opc_home / "config")
        if self.org_engine:
            self.org_engine.reload_from_config()
        await self._mark_session_recruitment_confirmation_completed(
            session_id,
            source="manual_staffing_approved",
        )
        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
        role_agent_overrides = self._staffing_role_agent_overrides(payload, reply_metadata)
        self._save_project_company_staffing_defaults(
            decision,
            company_profile=str(runtime_spec.profile or payload.get("company_profile", "") or CompanyProfile.CORPORATE.value),
            role_ids=role_ids,
            staffing_overrides=staffing_overrides,
            staffing_experience_modes=staffing_experience_modes,
            fallback_role_ids=fallback_role_ids,
            role_agent_overrides=role_agent_overrides,
        )
        if decision.mode == ExecutionMode.COMPANY_MODE:
            result = await self._continue_company_mode_execution(
                decision,
                original_message,
                runtime_spec,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                staffing_overrides=staffing_overrides,
                staffing_experience_modes=staffing_experience_modes,
                fallback_role_ids=fallback_role_ids,
                role_agent_overrides=role_agent_overrides,
                attachment_refs=attachment_refs,
            )
        else:
            result = await self._continue_task_mode_execution(
                decision,
                original_message,
                None,
                session_id=session_id,
                origin_channel=origin_channel,
                origin_chat_id=origin_chat_id,
                origin_thread_id=origin_thread_id,
                origin_task_id=origin_task_id,
                staffing_overrides=staffing_overrides,
                staffing_experience_modes=staffing_experience_modes,
                fallback_role_ids=fallback_role_ids,
                role_agent_overrides=role_agent_overrides,
                attachment_refs=attachment_refs,
            )
        if not hired_messages:
            return result
        return "Approved manual staffing.\n" + "\n".join(hired_messages) + "\n\n" + result

    async def _resume_recruitment_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        user_reply: str,
        *,
        reply_metadata: dict[str, Any] | None = None,
    ) -> str:
        """恢復招聘確認檢查點 — 處理使用者對招聘計劃的批准/修改。"""
        assert self.store and self.company_recruiter and self.talent_market
        payload = checkpoint.payload
        original_message = str(payload.get("original_message", ""))
        if not original_message:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume recruitment because the original request is missing."

        reply = user_reply.strip()
        normalized = reply.lower()
        reply_metadata = dict(reply_metadata or {})
        reply_kind = str(reply_metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
        approved_tokens = {"1", "y", "yes", "ok", "okay", "approve", "approved", "confirm", "continue", "proceed", "go"}
        denied_tokens = {"2", "n", "no", "deny", "denied", "reject", "rejected", "stop", "cancel", "abort"}
        decision = self._deserialize_router_decision(dict(payload.get("decision", {})))
        if payload.get("runtime_spec"):
            runtime_spec = deserialize_company_runtime_spec(dict(payload.get("runtime_spec", {})))
        else:
            profile = str(
                payload.get("company_profile")
                or payload.get("profile")
                or getattr(decision, "company_profile", "")
                or CompanyProfile.CORPORATE.value
            ).strip() or CompanyProfile.CORPORATE.value
            runtime_spec = CompanyRuntimeSpec(
                profile=profile,
                original_request=original_message,
                runtime_model="multi_team_org",
                work_item_driven=True,
                metadata={
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "work_item_driven": True,
                    "original_request": original_message,
                },
            )
        recruitment_plan = build_recruitment_plan_from_payload(dict(payload.get("recruitment_plan", {})))
        apply_recruitment_role_agent_overrides(
            recruitment_plan,
            payload.get("recruitment_role_agents"),
        )
        apply_recruitment_role_agent_overrides(
            recruitment_plan,
            reply_metadata.get("recruitment_role_agents"),
        )
        recruitment_agent = normalize_recruitment_agent_choice(
            reply_metadata.get("recruitment_agent")
            or payload.get("recruitment_agent")
            or dict(getattr(recruitment_plan, "metadata", {}) or {}).get("recruitment_agent"),
            default="native",
        ) or "native"
        role_agent_overrides = extract_recruitment_role_agent_overrides(recruitment_plan)
        fallback_role_ids = build_fallback_role_ids(recruitment_plan)
        session_id = str(payload.get("primary_session_id") or checkpoint.session_id or str(uuid.uuid4()))
        origin_channel = str(payload.get("origin_channel", "cli"))
        origin_chat_id = str(payload.get("origin_chat_id", ""))
        origin_thread_id = str(payload.get("origin_thread_id", ""))
        origin_task_id = str(payload.get("origin_task_id", "")).strip() or None
        attachment_refs = self._normalize_attachment_refs(payload.get("attachment_refs", []))

        if reply_kind == "approve" or (reply_kind != "feedback" and normalized in approved_tokens):
            hired_messages: list[str] = []
            raw_staffing_selections = reply_metadata.get("staffing_selections")
            if isinstance(raw_staffing_selections, dict) and raw_staffing_selections:
                role_ids = {
                    str(proposal.role_id or "").strip()
                    for proposal in list(recruitment_plan.proposals or [])
                    if str(proposal.role_id or "").strip()
                }
                selections: dict[str, dict[str, str]] = {}
                proposal_by_role = {
                    str(proposal.role_id or "").strip(): proposal
                    for proposal in list(recruitment_plan.proposals or [])
                    if str(proposal.role_id or "").strip()
                }
                for role_id, proposal in proposal_by_role.items():
                    if proposal.existing_employee and proposal.existing_employee.employee_id:
                        selections[role_id] = {
                            "kind": "employee",
                            "id": proposal.existing_employee.employee_id,
                        }
                    elif proposal.candidate and proposal.candidate.template_id:
                        selections[role_id] = {
                            "kind": "template",
                            "id": proposal.candidate.template_id,
                        }
                    else:
                        selections[role_id] = {"kind": "fallback", "id": ""}
                for raw_role_id, raw_selection in raw_staffing_selections.items():
                    role_id = str(raw_role_id or "").strip()
                    if role_id in role_ids:
                        selections[role_id] = self._normalize_staffing_selection(raw_selection)

                active_employees = self._active_staffing_employees_by_id()
                available_templates = {
                    str(getattr(template, "id", "") or "").strip(): template
                    for template in self.talent_market.list_available_templates()
                    if str(getattr(template, "id", "") or "").strip()
                }
                staffing_overrides = {}
                staffing_experience_modes = {}
                fallback_role_ids = set()
                errors: list[str] = []
                for role_id in sorted(role_ids):
                    selection = selections.get(role_id, {"kind": "fallback", "id": ""})
                    kind = selection.get("kind", "fallback")
                    selected_id = str(selection.get("id", "") or "").strip()
                    if kind == "employee":
                        if selected_id not in active_employees:
                            errors.append(f"Role `{role_id}` selected unknown employee `{selected_id}`.")
                            continue
                        staffing_overrides[role_id] = self._canonical_staffing_employee_id(active_employees[selected_id], selected_id)
                        staffing_experience_modes[role_id] = "with_experience"
                        continue
                    if kind == "template":
                        if selected_id not in available_templates:
                            errors.append(f"Role `{role_id}` selected unknown template `{selected_id}`.")
                            continue
                        proposal = proposal_by_role.get(role_id)
                        use_proposed_identity = bool(
                            proposal
                            and proposal.candidate
                            and proposal.candidate.template_id == selected_id
                        )
                        employee = self.talent_market.ensure_hire_template(
                            selected_id,
                            role_id,
                            employee_name=(
                                proposal.candidate.proposed_employee_name
                                if use_proposed_identity and proposal and proposal.candidate
                                else str(getattr(available_templates[selected_id], "name", "") or "")
                            ),
                            employee_id=(
                                proposal.candidate.proposed_employee_id
                                if use_proposed_identity and proposal and proposal.candidate
                                else ""
                            ),
                        )
                        if use_proposed_identity and proposal and proposal.candidate:
                            proposal.candidate.proposed_employee_id = employee.employee_id
                            proposal.candidate.proposed_employee_name = employee.name
                        staffing_overrides[role_id] = employee.employee_id
                        staffing_experience_modes[role_id] = "template_only"
                        hired_messages.append(f"- {employee.name} ({employee.employee_id}) -> {role_id}")
                        continue
                    fallback_role_ids.add(role_id)
                if errors:
                    return (
                        "Could not apply recruitment staffing:\n"
                        + "\n".join(f"- {item}" for item in errors)
                        + "\n\n"
                        + (recruitment_plan.summary or self.company_recruiter.render_recruitment_summary(recruitment_plan))
                    )
            else:
                for proposal in recruitment_plan.proposals:
                    if proposal.status != "proposed_hire" or not proposal.candidate:
                        continue
                    employee = self.talent_market.ensure_hire_template(
                        proposal.candidate.template_id,
                        proposal.role_id,
                        employee_name=proposal.candidate.proposed_employee_name,
                        employee_id=proposal.candidate.proposed_employee_id,
                    )
                    proposal.candidate.proposed_employee_id = employee.employee_id
                    proposal.candidate.proposed_employee_name = employee.name
                    hired_messages.append(f"- {employee.name} ({employee.employee_id}) -> {employee.role_id}")
                staffing_overrides = build_staffing_overrides(recruitment_plan)
                staffing_experience_modes = build_staffing_experience_modes(recruitment_plan)
            self.config.save(self.opc_home / "config")
            if self.org_engine:
                self.org_engine.reload_from_config()
            await self._mark_session_recruitment_confirmation_completed(
                session_id,
                source="checkpoint_approved",
            )
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            self._save_project_company_staffing_defaults(
                decision,
                company_profile=str(runtime_spec.profile or payload.get("company_profile", "") or CompanyProfile.CORPORATE.value),
                role_ids={
                    *{
                        str(proposal.role_id or "").strip()
                        for proposal in list(recruitment_plan.proposals or [])
                        if str(proposal.role_id or "").strip()
                    },
                    *set(staffing_overrides),
                    *set(fallback_role_ids),
                    *set(role_agent_overrides),
                },
                staffing_overrides=staffing_overrides,
                staffing_experience_modes=staffing_experience_modes,
                fallback_role_ids=fallback_role_ids,
                role_agent_overrides=role_agent_overrides,
            )
            if decision.mode == ExecutionMode.COMPANY_MODE:
                result = await self._continue_company_mode_execution(
                    decision,
                    original_message,
                    runtime_spec,
                    session_id=session_id,
                    origin_channel=origin_channel,
                    origin_chat_id=origin_chat_id,
                    origin_thread_id=origin_thread_id,
                    origin_task_id=origin_task_id,
                    staffing_overrides=staffing_overrides,
                    staffing_experience_modes=staffing_experience_modes,
                    fallback_role_ids=fallback_role_ids,
                    role_agent_overrides=role_agent_overrides,
                    attachment_refs=attachment_refs,
                )
            else:
                result = await self._continue_task_mode_execution(
                    decision,
                    original_message,
                    None,
                    session_id=session_id,
                    origin_channel=origin_channel,
                    origin_chat_id=origin_chat_id,
                    origin_thread_id=origin_thread_id,
                    origin_task_id=origin_task_id,
                    staffing_overrides=staffing_overrides,
                    staffing_experience_modes=staffing_experience_modes,
                    fallback_role_ids=fallback_role_ids,
                    role_agent_overrides=role_agent_overrides,
                    attachment_refs=attachment_refs,
                )
            if not hired_messages:
                return result
            return "Approved recruitment plan.\n" + "\n".join(hired_messages) + "\n\n" + result

        if reply_kind == "deny" or (reply_kind != "feedback" and normalized in denied_tokens):
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            return "Recruitment was cancelled. Execution will not continue."

        control_tokens = approved_tokens | denied_tokens
        if reply_kind != "feedback" and normalized in control_tokens:
            return recruitment_plan.summary or self.company_recruiter.render_recruitment_summary(recruitment_plan)

        feedback = build_recruitment_feedback(reply)
        if not feedback:
            return recruitment_plan.summary or self.company_recruiter.render_recruitment_summary(recruitment_plan)
        feedback_history = list(recruitment_plan.recruiter_feedback)
        feedback_history.append(feedback)
        recruitment_llm, selected_recruitment_agent = self._resolve_recruitment_llm(recruitment_agent)
        revised_plan = await self.company_recruiter.build_recruitment_plan(
            runtime_spec,
            domains=decision.domains,
            project_id=self.project_id or "default",
            recruiter_feedback=feedback_history,
            recruitment_llm=recruitment_llm,
            recruitment_agent=selected_recruitment_agent,
        )
        apply_recruitment_role_agent_overrides(
            revised_plan,
            extract_recruitment_role_agent_overrides(recruitment_plan),
        )
        try:
            current_revision = int(
                payload.get("recruitment_revision")
                or dict(getattr(recruitment_plan, "metadata", {}) or {}).get("recruitment_revision")
                or 1
            )
        except (TypeError, ValueError):
            current_revision = 1
        next_revision = current_revision + 1
        revised_plan.metadata = dict(getattr(revised_plan, "metadata", {}) or {})
        revised_plan.metadata["recruitment_revision"] = next_revision
        revised_plan.metadata["previous_checkpoint_id"] = checkpoint.checkpoint_id
        revised_plan.metadata["recruitment_agent"] = selected_recruitment_agent
        raw_prior_superseded = payload.get("superseded_checkpoint_ids", [])
        prior_superseded = [
            str(item).strip()
            for item in (raw_prior_superseded if isinstance(raw_prior_superseded, list) else [])
            if str(item).strip()
        ]
        revised_payload = {
            **payload,
            "recruitment_plan": serialize_recruitment_plan(revised_plan),
            "recruiter_feedback": list(feedback_history),
            "previous_checkpoint_id": checkpoint.checkpoint_id,
            "recruitment_revision": next_revision,
            "recruitment_role_agents": extract_recruitment_role_agent_overrides(revised_plan),
            "recruitment_agent": selected_recruitment_agent,
            "superseded_checkpoint_ids": [*prior_superseded, checkpoint.checkpoint_id],
        }
        revised_payload.pop("basis_hash", None)
        await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="superseded")
        await self._save_execution_checkpoint(
            {
                "project_id": checkpoint.project_id or self.project_id or "default",
                "session_id": checkpoint.session_id or session_id,
                "checkpoint_type": "company_recruitment_confirmation",
                "task_id": checkpoint.task_id,
                "payload": revised_payload,
            }
        )
        return revised_plan.summary or self.company_recruiter.render_recruitment_summary(revised_plan)
