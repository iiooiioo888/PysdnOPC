"""TaskModeMixin — 任務模式路由和執行相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opc.engine._core import OPCEngine


class TaskModeMixin:
    """Mixin providing 任務模式路由和執行相關方法 for OPCEngine."""

    @staticmethod
    def _strip_task_mode_work_item_identity(metadata: dict[str, Any]) -> dict[str, Any]:
        """Task mode uses Task as a runtime container, not a company work item."""
        cleaned = dict(metadata or {})
        for key in (
            "work_item_projection_id",
            "work_item_turn_type",
            "work_item_projection_title",
            "work_item_metadata",
            "work_item_gate",
            "employee_assignment",
            "employee_prompt_context",
            "employee_delta_context",
        ):
            cleaned.pop(key, None)
        return cleaned

    @staticmethod
    def _company_reply_text_looks_internal_dispatch(text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        internal_markers = (
            "已创建下游 WorkItem",
            "已创建两个子 WorkItem",
            "已完成派发",
            "Work item delegated downstream work",
            "delegated downstream work and is waiting for child work items",
        )
        return any(marker in normalized for marker in internal_markers)

    @staticmethod
    def _company_task_is_owner_facing_delivery(task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        return (
            str(metadata.get("feedback_scope", "") or "").strip().lower() == "final"
            and turn_type_for_task(task, fallback="") == "deliver"
            and bool(metadata.get("authoritative_output", False))
        )

    @staticmethod
    def _company_task_is_runtime_result(task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        if execution_mode == ExecutionMode.COMPANY_MODE.value:
            return True
        if str(metadata.get("execution_model", "") or "").strip() == "multi_team_org":
            return True
        if str(metadata.get("work_item_projection_id", "") or "").strip():
            return True
        if str(metadata.get("company_profile", "") or "").strip():
            return True
        return bool(metadata.get("work_item_runtime"))

    @staticmethod
    def _company_task_is_internal_dispatch_result(task: Task, content: str) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        turn_type = turn_type_for_task(task, fallback="")
        if turn_type not in {"intake", "dispatch", "plan", "aggregate"}:
            return False
        if bool(metadata.get("manager_board_mutation_performed", False)):
            return True
        if bool(metadata.get("delegated_children_pending", False)):
            return True
        if [
            str(item).strip()
            for item in list(metadata.get("delegation_wait_for_work_item_ids", []) or [])
            if str(item).strip()
        ]:
            return True
        return OPCEngine._company_reply_text_looks_internal_dispatch(content)

    async def _company_reply_is_internal_runtime_result(
        self,
        session_id: str,
        assistant_text: str,
        *,
        allow_marker_fallback: bool = False,
    ) -> bool:
        text = str(assistant_text or "").strip()
        if not text:
            return False
        marker_fallback = (
            self._company_reply_text_looks_internal_dispatch(text)
            if allow_marker_fallback
            else False
        )
        if not self.store:
            return marker_fallback
        try:
            tasks = await self.store.get_tasks(project_id=self.project_id or "default")
        except Exception:
            logger.opt(exception=True).debug("failed to inspect company tasks before recording top-level reply")
            return marker_fallback
        session_key = str(session_id or "").strip()
        for task in tasks:
            if session_key and session_key not in {
                str(getattr(task, "session_id", "") or "").strip(),
                str(getattr(task, "parent_session_id", "") or "").strip(),
            }:
                continue
            result = getattr(task, "result", None)
            content = str((result or {}).get("content", "") if isinstance(result, dict) else "").strip()
            if content != text:
                continue
            if self._company_task_is_owner_facing_delivery(task):
                return True
            if self._company_task_is_internal_dispatch_result(task, content):
                return True
            if self._company_task_is_runtime_result(task):
                return True
        return False

    @staticmethod
    def _primary_reply_match_text(value: Any) -> str:
        text = str(value or "").replace("\r\n", "\n").strip()
        paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
        if len(paragraphs) > 1 and re.match(r"^Verification:\s", paragraphs[-1], flags=re.IGNORECASE):
            text = "\n\n".join(paragraphs[:-1]).strip()
        return text

    @staticmethod
    def _normalized_execution_agent(value: Any) -> str:
        return str(value or "").strip().lower().replace("-", "_")

    @classmethod
    def _task_mode_task_uses_external_agent(cls, task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        for value in (
            getattr(task, "assigned_external_agent", None),
            metadata.get("assigned_external_agent"),
            metadata.get("preferred_external_agent"),
            metadata.get("selected_execution_agent"),
        ):
            normalized = cls._normalized_execution_agent(value)
            if normalized and normalized not in {"native", "task_generalist", "opc"}:
                return True
        return False

    @staticmethod
    def _task_is_task_mode_runtime(task: Task) -> bool:
        metadata = dict(getattr(task, "metadata", {}) or {})
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        if execution_mode == ExecutionMode.COMPANY_MODE.value:
            return False
        if execution_mode in {ExecutionMode.TASK_MODE.value, "task", "project_mode", "project"}:
            return True
        projection_id = str(metadata.get("work_item_projection_id", "") or "").strip()
        if projection_id and projection_id != "task_mode_execution":
            return False
        return (
            str(metadata.get("mode", "") or "").strip().lower() == "task"
            or str(metadata.get("task_mode_contract", "") or "").strip() == "single_full_capability_main_agent"
            or str(metadata.get("runtime_kind", "") or "").strip() == "task_mode_agent_turn"
            or projection_id == "task_mode_execution"
        )

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _apply_task_mode_external_timeout_defaults(self, task: Task) -> None:
        if not self._task_is_task_mode_runtime(task):
            return
        timeout = self._coerce_positive_int(self.config.system.task_mode.sub_agent_timeout_sec)
        if timeout is None:
            return
        metadata = dict(getattr(task, "metadata", {}) or {})
        changed = False
        if self._coerce_positive_int(metadata.get("external_hard_timeout_seconds")) is None:
            metadata["external_hard_timeout_seconds"] = timeout
            changed = True
        if self._coerce_positive_int(metadata.get("external_idle_timeout_seconds")) is None:
            metadata["external_idle_timeout_seconds"] = timeout
            changed = True
        if changed:
            task.metadata = metadata

    @staticmethod
    def _session_transcript_item_text(item: Any) -> str:
        parts = item.get("parts", []) if isinstance(item, dict) else []
        texts: list[str] = []
        for part in parts:
            payload = part.get("payload", {}) if isinstance(part, dict) else getattr(part, "payload", {})
            if isinstance(payload, dict):
                texts.append(str(payload.get("text", "") or ""))
        return "\n".join(texts).strip()

    async def _task_mode_external_top_level_reply_exists(
        self,
        session_id: str,
        assistant_text: str,
        *,
        task_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        if not self.store:
            return False
        transcript_loader = getattr(self.store, "get_session_transcript", None)
        if not callable(transcript_loader):
            return False
        text = self._primary_reply_match_text(assistant_text)
        if not text:
            return False
        try:
            transcript = await transcript_loader(session_id)
        except Exception:
            return False
        expected_task_id = str(task_id or "").strip()
        expected_turn_id = str(turn_id or "").strip()
        for item in transcript:
            message = item.get("message") if isinstance(item, dict) else None
            metadata = dict(getattr(message, "metadata", {}) or {}) if message is not None else {}
            kind = str(metadata.get("kind", "") or metadata.get("source_kind", "") or "").strip()
            if kind != "top_level_reply" or not metadata.get("task_mode_external_result"):
                continue
            if self._primary_reply_match_text(self._session_transcript_item_text(item)) != text:
                continue
            recorded_task_id = str(getattr(message, "task_id", "") or metadata.get("task_id", "") or "").strip()
            recorded_turn_id = str(
                metadata.get("conversation_turn_id", "")
                or metadata.get("canonical_turn_id", "")
                or metadata.get("turn_id", "")
                or ""
            ).strip()
            if expected_task_id and recorded_task_id and expected_task_id != recorded_task_id:
                continue
            if expected_turn_id and recorded_turn_id and expected_turn_id != recorded_turn_id:
                continue
            return True
        return False

    async def _record_task_mode_external_result_reply(self, task: Task, result_content: str) -> None:
        if not self.memory or not task.session_id:
            return
        if not self._task_is_task_mode_runtime(task):
            return
        if not self._task_mode_task_uses_external_agent(task):
            return
        content = str(result_content or "").strip()
        if not content:
            return
        metadata = dict(getattr(task, "metadata", {}) or {})
        turn_id = str(
            metadata.get("conversation_turn_id", "")
            or metadata.get("canonical_turn_id", "")
            or metadata.get("turn_id", "")
            or ""
        ).strip()
        if await self._task_mode_external_top_level_reply_exists(
            str(task.session_id),
            content,
            task_id=str(task.id or "").strip(),
            turn_id=turn_id,
        ):
            return

        reply_metadata: dict[str, Any] = {
            "kind": "top_level_reply",
            "task_mode_external_result": True,
            "task_id": str(task.id or "").strip(),
            "assigned_external_agent": str(getattr(task, "assigned_external_agent", "") or "").strip(),
        }
        selected_agent = str(metadata.get("selected_execution_agent", "") or "").strip()
        if selected_agent:
            reply_metadata["selected_execution_agent"] = selected_agent
        if turn_id:
            reply_metadata["conversation_turn_id"] = turn_id
            reply_metadata["canonical_turn_id"] = turn_id
            reply_metadata["turn_id"] = turn_id
            reply_metadata["ui_message_id"] = f"task-mode-external-reply:{turn_id}"

        await self.memory.record_assistant_turn(
            session_id=str(task.session_id),
            content=content,
            project_id=task.project_id or self.project_id or "default",
            task_id=task.id,
            metadata=reply_metadata,
        )

    async def _task_mode_reply_uses_native_runtime_transcript(
        self,
        session_id: str,
        assistant_text: str,
        *,
        origin_task_id: str | None = None,
        preferred_agent: str | None = None,
    ) -> bool:
        normalized_agent = self._normalized_execution_agent(preferred_agent)
        if normalized_agent and normalized_agent not in {"native", "task_generalist", "opc"}:
            return False

        text = self._primary_reply_match_text(assistant_text)
        if not text:
            return False

        if self.store:
            task_getter = getattr(self.store, "get_tasks", None)
            if callable(task_getter):
                try:
                    tasks = await task_getter(project_id=self.project_id or "default")
                except Exception:
                    tasks = []
                session_key = str(session_id or "").strip()
                origin_key = str(origin_task_id or "").strip()
                for task in tasks:
                    if not self._task_is_task_mode_runtime(task):
                        continue
                    metadata = dict(getattr(task, "metadata", {}) or {})
                    task_ids = {
                        str(getattr(task, "id", "") or "").strip(),
                        str(getattr(task, "session_id", "") or "").strip(),
                        str(metadata.get("origin_task_id", "") or "").strip(),
                    }
                    if session_key and session_key not in task_ids:
                        continue
                    if origin_key and origin_key not in task_ids:
                        continue
                    result = getattr(task, "result", None)
                    content = str((result or {}).get("content", "") if isinstance(result, dict) else "").strip()
                    if self._primary_reply_match_text(content) != text:
                        continue
                    return not self._task_mode_task_uses_external_agent(task)

            transcript_loader = getattr(self.store, "get_session_transcript", None)
            if callable(transcript_loader):
                try:
                    transcript = await transcript_loader(session_id)
                except Exception:
                    transcript = []
                for item in transcript:
                    message = item.get("message") if isinstance(item, dict) else None
                    metadata = dict(getattr(message, "metadata", {}) or {}) if message is not None else {}
                    kind = str(metadata.get("kind", "") or metadata.get("source_kind", "") or "").strip()
                    if kind != "runtime_v2_assistant":
                        continue
                    content = self._session_transcript_item_text(item)
                    if self._primary_reply_match_text(content) == text:
                        return True

        return normalized_agent in {"native", "task_generalist", "opc"}

    def _build_company_runtime_confirmation_message(self, decision: ModeSelection, context: Any) -> str:
        profile = decision.company_profile or getattr(context, "company_profile", "corporate")
        available = ", ".join(getattr(context, "company_profiles", ["corporate", "custom"]))
        return (
            "Company mode needs a runtime profile before execution.\n\n"
            f"Recommended profile: `{profile}`\n\n"
            f"Available profiles: {available}\n"
            "Reply with `use corporate`, `use custom`, or directly describe your own company mode in natural language."
        )

    def _serialize_router_decision(self, decision: ModeSelection) -> dict[str, Any]:
        return {
            "mode": decision.mode.value,
            "preferred_agent": decision.preferred_agent,
            "domains": list(decision.domains),
            "company_profile": decision.company_profile,
            "sub_tasks": copy.deepcopy(list(getattr(decision, "sub_tasks", []) or [])),
            "org_id": getattr(decision, "org_id", None),
            "metadata": dict(getattr(decision, "metadata", {})),
        }

    def _deserialize_router_decision(self, data: dict[str, Any]) -> ModeSelection:
        return ModeSelection(
            mode=ExecutionMode(data.get("mode", ExecutionMode.TASK_MODE.value)),
            preferred_agent=data.get("preferred_agent"),
            domains=list(data.get("domains", [])),
            company_profile=data.get("company_profile"),
            sub_tasks=copy.deepcopy(list(data.get("sub_tasks", []) or [])),
            org_id=data.get("org_id"),
            metadata=dict(data.get("metadata", {})),
        )

    def _normalize_sub_tasks(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("name") or "").strip()
                description = str(item.get("description") or title).strip()
                dependencies = item.get("dependencies", [])
                if not isinstance(dependencies, list):
                    dependencies = []
                if not title and not description:
                    continue
                normalized.append(
                    {
                        **item,
                        "title": title or description[:80] or "Sub-task",
                        "description": description or title,
                        "dependencies": dependencies,
                    }
                )
                continue
            text = str(item).strip()
            if not text:
                continue
            normalized.append(
                {
                    "title": text[:80],
                    "description": text,
                    "dependencies": [],
                }
            )
        return normalized

    async def _continue_task_mode_execution(
        self,
        decision: ModeSelection,
        original_message: str,
        work_item_plan: CompanyWorkItemRuntimePlan | None = None,
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
        conversation_turn_id: str | None = None,
    ) -> str:
        assert self.task_scheduler and self.store and self.org_engine
        project_id = self.project_id or "default"
        attachment_refs = self._normalize_attachment_refs(attachment_refs)
        attachment_context = self._build_attachment_context(attachment_refs)
        workspace_contract = await self._resolve_workspace_contract(original_message, session_id)
        target_output_dir = str(workspace_contract.get("output_root") or "").strip() or None
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
        explicit_agent_choice = normalize_recruitment_agent_choice(decision.preferred_agent)
        explicit_external_agent = (
            explicit_agent_choice
            if explicit_agent_choice and explicit_agent_choice != "native"
            else None
        )
        include_project_knowledge = self._requests_explicit_project_knowledge(original_message)
        secretary_context = ""

        self.org_engine.configure_task_mode_tools(self._task_mode_tool_names())
        execution_role = self.org_engine.get_task_mode_role()
        role_id = execution_role.role_id
        reusable_task = await self._find_reusable_task_mode_task(
            session_id=session_id,
            project_id=project_id,
            origin_task_id=origin_task_id,
        )
        _ = (staffing_overrides, staffing_experience_modes, fallback_role_ids)
        selected_role_agent = normalize_recruitment_agent_choice(
            (role_agent_overrides or {}).get(role_id)
        )
        preferred_external_agent = explicit_external_agent
        if not explicit_agent_choice and selected_role_agent:
            preferred_external_agent = None if selected_role_agent == "native" else selected_role_agent
        # Task mode is a single full-capability main agent by default.
        # Only an explicit user override should move execution to an external agent.
        force_native_execution = explicit_agent_choice == "native" or preferred_external_agent is None
        selected_execution_agent = (
            explicit_agent_choice
            or selected_role_agent
            or ("native" if not preferred_external_agent else preferred_external_agent)
        )
        execution_agent_locked = bool(explicit_agent_choice or selected_role_agent)
        current_turn_id = str(conversation_turn_id or "").strip()
        work_item_execution_strategy = (
            WorkItemExecutionStrategy.EXTERNAL.value
            if preferred_external_agent
            else WorkItemExecutionStrategy.NATIVE.value
        )
        if reusable_task:
            task = reusable_task
            task.title = original_message[:120].rstrip()
            task.description = original_message
            task.assigned_to = role_id
            task.priority = 5
            task.tags = []
            task.dependencies = []
            task.project_id = project_id
            task.assigned_external_agent = preferred_external_agent
            task.retry_count = 0
            task.metadata = self._strip_task_mode_work_item_identity(dict(task.metadata))
            task.context_snapshot = dict(task.context_snapshot)
            context_snapshot = task.context_snapshot if isinstance(task.context_snapshot, dict) else {}
            raw_runtime_resume = context_snapshot.get("runtime_resume", {})
            runtime_resume = dict(raw_runtime_resume) if isinstance(raw_runtime_resume, dict) else {}
            runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
            if runtime_meta:
                task.context_snapshot["runtime_resume"] = {
                    **runtime_resume,
                    **runtime_meta,
                    "restored_from_same_session": True,
                    "restored_at": datetime.now().isoformat(),
                }
            task.metadata.pop("_runtime_v2_user_seeded", None)
            task.metadata.update({
                "mode": "task",
                "original_message": original_message,
                "workspace_root": workspace_contract.get("workspace_root"),
                "output_root": target_output_dir,
                "target_output_dir": target_output_dir,
                "comms_workspace_root": workspace_contract.get("comms_workspace_root"),
                "comms_root": workspace_contract.get("comms_root"),
                "include_project_knowledge": include_project_knowledge,
                "secretary_context": secretary_context,
                "origin_channel": origin_channel,
                "origin_chat_id": origin_chat_id,
                "origin_thread_id": origin_thread_id,
                "origin_task_id": origin_task_id or task.id,
                "attachment_refs": attachment_refs,
                "attachment_context": attachment_context,
                "runtime_kind": "task_mode_agent_turn",
                "work_item_role_id": role_id,
                "work_item_execution_strategy": work_item_execution_strategy,
                "preferred_external_agent": preferred_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_agent_locked": execution_agent_locked,
                "force_native_execution": force_native_execution,
                "task_mode_contract": "single_full_capability_main_agent",
                "router_preferred_agent": decision.preferred_agent,
                "execution_mode": decision.mode.value,
                "execution_task_ids": [task.id],
                "parent_session_id": session_id,
                "org_version": self.org_engine.current_org_version(),
                "runtime_topology_version": self.org_engine.current_runtime_topology_version(),
                "reorg_proposal_id": str(task.metadata.get("reorg_proposal_id", "") or ""),
                "migration_status": str(task.metadata.get("migration_status", "") or ""),
                "superseded_by_reorg": str(task.metadata.get("superseded_by_reorg", "") or ""),
            })
            if current_turn_id:
                runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
                runtime_meta["current_turn_id"] = current_turn_id
                task.metadata["runtime_v2"] = runtime_meta
                task.metadata["conversation_turn_id"] = current_turn_id
                task.metadata["current_turn_id"] = current_turn_id
                task.metadata["runtime_v2_current_turn_id"] = current_turn_id
            task.org_id = getattr(decision, "org_id", None)
            if work_item_plan:
                task.metadata["company_work_item_plan"] = serialize_company_work_item_runtime_plan(work_item_plan)
            if self.memory and task.session_id:
                await self.memory.ensure_session(
                    task.session_id,
                    project_id=task.project_id,
                    title=task.title,
                    mode="primary",
                    parent_session_id=task.parent_session_id,
                    metadata={
                        "task_id": task.id,
                        "execution_mode": decision.mode.value,
                        "origin_task_id": task.metadata.get("origin_task_id") or task.id,
                        "runtime_kind": "task_mode_agent_turn",
                        "selected_execution_agent": task.metadata.get("selected_execution_agent"),
                        **({"conversation_turn_id": current_turn_id} if current_turn_id else {}),
                    },
                )
            await self.store.save_task(task)
            return await self._execute_single_agent([task], preferred_external_agent)

        task_dicts = [{
            "title": original_message[:120].rstrip(),
            "description": original_message,
            "assigned_to": role_id,
            "dependencies": [],
            "tags": [],
            "priority": 5,
            "session_id": session_id,
            "project_id": project_id,
            "assigned_external_agent": preferred_external_agent,
            "metadata": {
                "mode": "task",
                "original_message": original_message,
                "workspace_root": workspace_contract.get("workspace_root"),
                "output_root": target_output_dir,
                "target_output_dir": target_output_dir,
                "comms_workspace_root": workspace_contract.get("comms_workspace_root"),
                "comms_root": workspace_contract.get("comms_root"),
                "include_project_knowledge": include_project_knowledge,
                "secretary_context": secretary_context,
                "origin_channel": origin_channel,
                "origin_chat_id": origin_chat_id,
                "origin_thread_id": origin_thread_id,
                "origin_task_id": origin_task_id,
                "attachment_refs": attachment_refs,
                "attachment_context": attachment_context,
                "runtime_kind": "task_mode_agent_turn",
                "work_item_role_id": role_id,
                "work_item_execution_strategy": work_item_execution_strategy,
                "preferred_external_agent": preferred_external_agent,
                "selected_execution_agent": selected_execution_agent,
                "execution_agent_locked": execution_agent_locked,
                "force_native_execution": force_native_execution,
                "task_mode_contract": "single_full_capability_main_agent",
                **({
                    "conversation_turn_id": current_turn_id,
                    "current_turn_id": current_turn_id,
                    "runtime_v2_current_turn_id": current_turn_id,
                    "runtime_v2": {"current_turn_id": current_turn_id},
                } if current_turn_id else {}),
            },
        }]

        tasks = await self.task_scheduler.create_tasks(task_dicts)
        task_ids = [task.id for task in tasks]
        serialized_plan = serialize_company_work_item_runtime_plan(work_item_plan) if work_item_plan else None
        for task in tasks:
            task.metadata = self._strip_task_mode_work_item_identity(dict(task.metadata))
            task.metadata["router_preferred_agent"] = decision.preferred_agent
            task.metadata["secretary_context"] = secretary_context
            task.metadata["execution_mode"] = decision.mode.value
            task.metadata["execution_task_ids"] = task_ids
            task.metadata["origin_task_id"] = origin_task_id or task.id
            task.metadata["runtime_kind"] = "task_mode_agent_turn"
            task.metadata["parent_session_id"] = session_id
            if current_turn_id:
                runtime_meta = dict(task.metadata.get("runtime_v2", {}) or {})
                runtime_meta["current_turn_id"] = current_turn_id
                task.metadata["runtime_v2"] = runtime_meta
                task.metadata["conversation_turn_id"] = current_turn_id
                task.metadata["current_turn_id"] = current_turn_id
                task.metadata["runtime_v2_current_turn_id"] = current_turn_id
            task.metadata["org_version"] = self.org_engine.current_org_version()
            task.metadata["runtime_topology_version"] = self.org_engine.current_runtime_topology_version()
            task.metadata.setdefault("reorg_proposal_id", "")
            task.metadata.setdefault("migration_status", "")
            task.metadata.setdefault("superseded_by_reorg", "")
            task.org_id = getattr(decision, "org_id", None)
            if serialized_plan:
                task.metadata["company_work_item_plan"] = serialized_plan
            if self.memory and task.session_id:
                await self.memory.ensure_session(
                    task.session_id,
                    project_id=task.project_id,
                    title=task.title,
                    mode="primary",
                    parent_session_id=task.parent_session_id,
                    metadata={
                        "task_id": task.id,
                        "execution_mode": decision.mode.value,
                        "origin_task_id": task.metadata.get("origin_task_id") or task.id,
                        "runtime_kind": "task_mode_agent_turn",
                        "selected_execution_agent": task.metadata.get("selected_execution_agent"),
                        **({"conversation_turn_id": current_turn_id} if current_turn_id else {}),
                    },
                )
            await self.store.save_task(task)

        return await self._execute_single_agent(tasks, preferred_external_agent)

    @staticmethod
    def _is_task_mode_primary_task(task: Task, *, session_id: str, project_id: str) -> bool:
        if str(getattr(task, "project_id", "") or "").strip() != project_id:
            return False
        if str(getattr(task, "session_id", "") or "").strip() != session_id:
            return False
        if getattr(task, "parent_id", None):
            return False
        mode = str(task.metadata.get("mode", "") or "").strip().lower()
        task_mode_contract = str(task.metadata.get("task_mode_contract", "") or "").strip()
        if mode == "task" or task_mode_contract == "single_full_capability_main_agent":
            return True
        exec_mode = str(task.metadata.get("exec_mode", "") or "").strip().lower()
        execution_mode = str(task.metadata.get("execution_mode", "") or "").strip().lower()
        return (
            exec_mode in {"task", "project", "single"}
            and execution_mode in {"", "task", "task_mode", "project"}
        )

    async def _find_reusable_task_mode_task(
        self,
        *,
        session_id: str,
        project_id: str,
        origin_task_id: str | None = None,
    ) -> Task | None:
        if not self.store or not session_id:
            return None
        getter = getattr(self.store, "get_task", None)
        if origin_task_id and callable(getter):
            origin_task = await self.store.get_task(origin_task_id)
            if origin_task and self._is_task_mode_primary_task(origin_task, session_id=session_id, project_id=project_id):
                return origin_task
        lister = getattr(self.store, "get_tasks", None)
        if not callable(lister):
            return None
        tasks = await lister(project_id=project_id)
        candidates = [
            task for task in tasks
            if self._is_task_mode_primary_task(task, session_id=session_id, project_id=project_id)
        ]
        if not candidates:
            return None
        non_terminal = [
            task for task in candidates
            if task.status not in {TaskStatus.DONE, TaskStatus.CANCELLED}
        ]
        pool = non_terminal or candidates
        pool.sort(
            key=lambda task: (
                bool(dict(task.metadata.get("runtime_v2", {}) or {}).get("runtime_session_id")),
                task.created_at,
            ),
            reverse=True,
        )
        return pool[0]

    def _detect_explicit_mode_override(self, message: str) -> str | None:
        text = message.casefold()
        single_agent_markers = (
            "single agent",
            "single-agent",
            "单agent",
            "单 agent",
            "单代理",
            "单智能体",
            "原生agent",
            "native agent",
            "native 模式",
        )
        company_mode_markers = (
            "company mode",
            "company-mode",
            "company模式",
            "公司模式",
            "团队模式",
            "多人模式",
        )
        if any(marker in text for marker in single_agent_markers):
            return ExecutionMode.SINGLE_AGENT.value
        if any(marker in text for marker in company_mode_markers):
            return ExecutionMode.COMPANY_MODE.value
        return None

    def _looks_like_followup_request(self, message: str) -> bool:
        patterns = (
            r"继续",
            r"后续",
            r"再[来做改加补修优]",
            r"新增",
            r"添加",
            r"增加",
            r"补充",
            r"修改",
            r"改一下",
            r"完善",
            r"优化",
            r"修复",
            r"接着",
            r"顺便",
            r"follow[- ]?up",
            r"continue",
            r"also",
            r"another",
            r"add ",
            r"update",
            r"modify",
            r"change",
            r"improve",
            r"fix",
            r"tweak",
        )
        return any(re.search(pattern, message, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _strip_json_fences(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()
        return cleaned
