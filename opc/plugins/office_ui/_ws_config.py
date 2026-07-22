"""WsConfigMixin — 配置/組織/員工/市場相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opc.plugins.office_ui.ws_handler import WSHandler


class WsConfigMixin:
    """Mixin providing 配置/組織/員工/市場相關方法 for WSHandler."""

    def _resolve_task_comms_dir(self, task: Any) -> Path | None:
        """Resolve `<workspace>/.opc-comms/<project>/<session>` for a task.

        Returns None when the task lacks enough metadata to locate its
        on-disk comms tree (e.g. workspace never resolved, no session id).
        """
        md = task.metadata or {}
        workspace_root = (
            str(md.get("comms_workspace_root") or "").strip()
            or str(md.get("target_output_dir") or "").strip()
            or str(md.get("setup_workspace_prepared") or "").strip()
        )
        comms_root = str(md.get("comms_root") or "").strip()
        session_id = (
            str(getattr(task, "parent_session_id", "") or "").strip()
            or str(getattr(task, "session_id", "") or "").strip()
        )
        project_id = (
            str(getattr(task, "project_id", "") or "").strip()
            or self.engine.project_id
            or "default"
        )
        if not session_id:
            return None
        try:
            from opc.layer2_organization import comms as _comms
            if workspace_root:
                return _comms.resolve_layout(workspace_root, project_id, session_id).root
            if comms_root:
                # comms_root is `<ws>/.opc-comms`; its parent is workspace_root.
                inferred_ws = str(Path(comms_root).parent)
                return _comms.resolve_layout(inferred_ws, project_id, session_id).root
        except Exception:
            logger.opt(exception=True).debug(f"_resolve_task_comms_dir failed for task {getattr(task, 'id', '?')}")
        return None

    async def _handle_list_projects(self, ws: Any, data: dict) -> None:
        """List available projects by scanning the projects directory."""
        result = await self.services.project.list(active_project_id=self._client_active_project_id(ws))
        await self._send_ack(ws, ok=True, **result.payload)

    async def _handle_create_project(self, ws: Any, data: dict) -> None:
        """Create a new project directory."""
        try:
            result = await self.services.project.create(
                data.get("project_id", ""),
                active_project_id=self._client_active_project_id(ws),
            )
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="create_project")

    async def _handle_delete_project(self, ws: Any, data: dict) -> None:
        """Delete a project and all its data. Switches to 'default' if active."""
        try:
            result = await self.services.project.delete(data.get("project_id", ""))
            self.engine = self.services_context.engine
            if result.payload.get("active_project_id") == "default":
                self._secretary_session_id = None
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, project_id=result.payload.get("project_id"))
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="delete_project")

    async def _handle_switch_project(self, ws: Any, data: dict) -> None:
        """Switch the active project view without rebinding in-flight runtimes."""
        new_id = data.get("project_id", "").strip()
        switch_seq = str(data.get("switch_seq") or data.get("switchSeq") or "").strip()
        try:
            await self.services.project.switch(new_id, switch_seq=switch_seq, include_snapshot=False)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="switch_project")
            return
        except Exception as exc:
            logger.opt(exception=True).error(
                f"Failed to prepare project switch for {new_id}: {type(exc).__name__}: {exc!r}",
            )
            await self._send_ack(
                ws,
                ok=False,
                project_id=new_id,
                switch_seq=switch_seq,
                error=f"Failed to switch project `{new_id}`: {exc}",
            )
            return

        project_engine = self.services_context.engine
        self._client_project_ids[ws] = new_id
        self._client_switch_seq[ws] = switch_seq
        self._secretary_session_id = None
        await self._send_envelope_to_client(
            ws,
            {"type": "project_switched", "payload": {"project_id": new_id, "switch_seq": switch_seq}},
        )
        self._track_client_project_index(
            ws,
            self._send_project_index_for_client(
                ws,
                project_engine,
                new_id,
                switch_seq=switch_seq,
                include_snapshot=True,
            ),
        )
        await self._send_ack(ws, ok=True, project_id=new_id, switch_seq=switch_seq)

    async def _handle_org_info(self, ws: Any, data: dict) -> None:
        """Return org structure, employees, runtime topology, and channel statuses."""
        result = await self._ensure_office_services().org.info()
        await ws.send_json({"type": "org_info", "payload": result.payload})

    async def _handle_talent_import(self, ws: Any, data: dict) -> None:
        """Import talent templates from a local repo directory."""
        try:
            result = await self._ensure_office_services().talent.import_repo(data.get("repo_path", ""))
            self._local_talent_cache = None
            await self._send_service_ack(ws, result)
            await self._send_talent_list(ws)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="talent_import")
        except Exception as exc:
            logger.warning(f"Failed to import talent templates: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_talent_scan_local(self, ws: Any, data: dict) -> None:
        """Scan local talent directory and return unregistered templates for selection."""
        try:
            result = await self._ensure_office_services().talent.scan()
            await ws.send_json({"type": "talent_scan_local", "payload": result.payload})
        except Exception:
            await ws.send_json({"type": "talent_scan_local", "payload": {"templates": []}})

    async def _handle_talent_import_selected(self, ws: Any, data: dict) -> None:
        """Import user-selected templates from the local talent directory."""
        try:
            result = await self._ensure_office_services().talent.import_selected(list(data.get("template_ids", []) or []))
            self._local_talent_cache = None
            await self._send_service_ack(ws, result)
            await self._send_talent_list(ws)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="talent_import_selected")
        except Exception as exc:
            logger.warning(f"Failed to import selected templates: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _send_talent_list(self, ws: Any) -> None:
        result = await self._ensure_office_services().talent.list()
        await ws.send_json({"type": "talent_list", "payload": result.payload})

    async def _handle_talent_list(self, ws: Any, data: dict) -> None:
        """List all talent templates."""
        try:
            await self._send_talent_list(ws)
        except Exception:
            logger.debug("Failed to list talent templates")
            await ws.send_json({"type": "talent_list", "payload": {"templates": []}})

    async def _handle_talent_hire(self, ws: Any, data: dict) -> None:
        """Hire a talent template into an existing role."""
        try:
            result = await self._ensure_office_services().talent.hire(
                template_id=data.get("template_id", ""),
                role_id=data.get("role_id", ""),
                employee_name=data.get("employee_name"),
                employee_id=data.get("employee_id"),
                organization_id=data.get("org_id") or data.get("organization_id"),
            )
            self._local_talent_cache = None
            await self._publish_service_result(result)
            await self._send_service_ack(ws, result)
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="talent_hire")
        except Exception as exc:
            logger.warning(f"Unexpected error hiring template: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_import_employee_as_agent(self, ws: Any, data: dict) -> None:
        """Import an existing org employee as a visual office agent."""
        try:
            result = await self._ensure_office_services().talent.import_employee_as_agent(
                employee_id=data.get("employee_id", ""),
                office_id=data.get("office_id", "office-0"),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="employee_imported", imported_employee_id=result.payload.get("imported_employee_id"))
            await self._handle_org_info(ws, {})
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="import_employee_as_agent")
        except Exception as exc:
            logger.warning(f"Failed to import employee: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_employee_detail(self, ws: Any, data: dict) -> None:
        """Return detailed evolution profile for an employee."""
        employee_id = data.get("employee_id", "")
        if not employee_id:
            await ws.send_json({"type": "employee_detail", "payload": {"employee_id": "", "error": "employee_id required"}})
            return
        try:
            result = await self._ensure_office_services().talent.employee_detail(employee_id)
            payload = dict(result.payload.get("employee", {}) or {})
            await ws.send_json({"type": "employee_detail", "payload": payload})
        except ServiceError as exc:
            await ws.send_json({"type": "employee_detail", "payload": {"employee_id": employee_id, "error": exc.message}})

    async def _handle_reorg_list(self, ws: Any, data: dict) -> None:
        """List recent reorg proposals for the current project."""
        store = self.engine.store
        if not store:
            await ws.send_json({"type": "reorg_list", "payload": {"proposals": []}})
            return
        project_id = self.engine.project_id or "default"
        try:
            proposals = await store.list_reorg_proposals(project_id, limit=20)
            result = []
            for p in proposals:
                changeset_summary: dict[str, Any] = {
                    "role_changes": [],
                    "task_adjustments_count": 0,
                }
                if p.changeset:
                    changeset_summary = {
                        "role_changes": [
                            {"action": rc.action, "role_id": rc.role_id, "reason": rc.reason}
                            for rc in (p.changeset.role_changes or [])
                        ],
                        "task_adjustments_count": len(p.changeset.task_adjustments or []),
                    }
                result.append({
                    "proposal_id": p.proposal_id,
                    "title": p.title,
                    "summary": p.summary,
                    "rationale": p.rationale,
                    "scope": p.scope.value if hasattr(p.scope, "value") else str(p.scope),
                    "risk_level": p.risk_level.value if hasattr(p.risk_level, "value") else str(p.risk_level),
                    "status": p.status.value if hasattr(p.status, "value") else str(p.status),
                    "initiated_by": p.initiated_by,
                    "changeset": changeset_summary,
                    "impact_summary": p.impact_summary or {},
                    "created_at": p.created_at.timestamp() if hasattr(p.created_at, "timestamp") else 0,
                    "updated_at": p.updated_at.timestamp() if hasattr(p.updated_at, "timestamp") else 0,
                })
            await ws.send_json({"type": "reorg_list", "payload": {"proposals": result}})
        except Exception:
            logger.debug("Failed to list reorg proposals")
            await ws.send_json({"type": "reorg_list", "payload": {"proposals": []}})

    async def _handle_reorg_decide(self, ws: Any, data: dict) -> None:
        """Approve or deny a reorg proposal from the UI."""
        proposal_id = data.get("proposal_id", "")
        approved = data.get("approved", False)
        notes = data.get("notes", "")
        rm = getattr(self.engine, "reorg_manager", None)
        if not rm or not proposal_id:
            await ws.send_json({"type": "ack", "payload": {"ok": False, "error": "reorg_manager not available or missing proposal_id"}})
            return
        try:
            await rm.set_reorg_approval(proposal_id, approved=approved, notes=notes)
            if approved:
                result = await rm.apply_reorg(proposal_id)
                await ws.send_json({"type": "ack", "payload": {"ok": True, "action": "reorg_applied", "result": result}})
                # Refresh org info for all clients
                await self._handle_org_info(ws, {})
            else:
                await ws.send_json({"type": "ack", "payload": {"ok": True, "action": "reorg_denied"}})
        except Exception as exc:
            logger.warning(f"Failed to decide reorg {proposal_id}: {exc}")
            await ws.send_json({"type": "ack", "payload": {"ok": False, "error": str(exc)}})

    async def _ack_saved_err(self, ws: Any, msg_type: str, name: str, error: str) -> None:
        """Shared error-ack helper for the saved-org handlers."""
        await ws.send_json({"type": msg_type, "payload": {
            "ok": False, "name": name, "error": error,
        }})

    async def _get_active_saved_org_name(self) -> str:
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        try:
            active_id = read_org_index(config_dir)
            if active_id and org_config_path(config_dir, active_id).exists():
                return active_id
        except Exception:
            pass
        if not hasattr(self, "agent_store"):
            return ""
        try:
            name = await self.agent_store.get_server_state(_ACTIVE_SAVED_ORG_STATE_KEY, "")
        except Exception:
            return ""
        try:
            path = _saved_org_path(name, strict=False)
        except ValueError:
            return ""
        org_id = path.stem.removeprefix("org_").removesuffix("_config")
        if path.exists():
            try:
                write_org_index(config_dir, validate_saved_org_id(org_id))
            except Exception:
                pass
            return org_id
        return ""

    async def _set_active_saved_org_name(self, name: str | None) -> None:
        value = str(name or "").strip()
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        if value:
            org_id = validate_saved_org_id(value)
            write_org_index(config_dir, org_id)
        if not hasattr(self, "agent_store"):
            return
        try:
            await self.agent_store.set_server_state(_ACTIVE_SAVED_ORG_STATE_KEY, value)
        except Exception:
            logger.debug("Failed to persist active saved org name")

    def _write_active_org_config(self, config: Any) -> None:
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        org = getattr(config, "org", None)
        raw_org_id = str(getattr(org, "organization_id", "") or "").strip()
        raw_name = str(
            getattr(org, "organization_name", "")
            or getattr(org, "company_name", "")
            or raw_org_id
            or "org"
        ).strip()
        try:
            organization_id = validate_saved_org_id(raw_org_id)
        except ValueError:
            active_id = read_org_index(config_dir)
            organization_id = active_id or allocate_org_config_id(config_dir, raw_name)
        if org is not None:
            org.organization_id = organization_id
            org.organization_name = raw_name
            org.organization_config_file = org_config_relative_path(organization_id)
            org.company_profile = "custom"
            try:
                from opc.core.employee_registry import write_employee_registry

                org.employees, _ = write_employee_registry(
                    Path(config_dir).parent,
                    organization_id,
                    list(getattr(org, "employees", []) or []),
                )
            except Exception:
                logger.opt(exception=True).debug("Failed to write employee registry for active org")
        payload = build_org_config_payload_from_config(
            config,
            organization_id=organization_id,
            organization_name=raw_name,
        )
        write_org_config_payload(config_dir, organization_id, payload)
        write_org_index(config_dir, organization_id)

    def _persist_runtime_config(self) -> None:
        if str(getattr(self, "_exec_mode", "") or "").strip().lower() in {"org", "custom"}:
            self._write_active_org_config(self.engine.config)
            return
        self.engine.config.save()

    async def _restore_active_saved_org_if_needed(self) -> None:
        """Recover org mode from the last loaded saved architecture.

        Org mode owns its active index, so startup restore must not consult or
        mutate company_index.yaml.
        """
        if str(getattr(self, "_exec_mode", "") or "").strip().lower() not in {"org", "custom"}:
            return

        name = await self._get_active_saved_org_name()
        if not name:
            return
        try:
            config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
            payload, path = load_org_config_payload(config_dir, name)
            validated_config = apply_org_config_payload_to_config(
                self.engine.config,
                payload,
                source_path=path,
            )
        except Exception as exc:
            logger.warning(f"Failed to restore saved org '{name}' during startup: {exc}")
            return

        async with self._config_lock:
            self._rebind_engine_config(validated_config)
            if self.engine.org_engine:
                self.engine.org_engine.reload_from_config()
        logger.info(f"Restored org architecture from saved org '{name}'")
        try:
            await self._broadcast_org_info()
        except Exception:
            pass

    async def _handle_org_saved_list(self, ws: Any, data: dict) -> None:
        """Enumerate saved organization configs."""
        result = await self._ensure_office_services().org.saved_list()
        await ws.send_json({"type": "org_saved_list", "payload": result.payload})

    async def _handle_org_saved_save_as(self, ws: Any, data: dict) -> None:
        """Snapshot current engine config.org as a saved organization."""
        try:
            result = await self._ensure_office_services().org.saved_save_as(
                str(data.get("organization_name") or data.get("name") or "").strip(),
                overwrite=bool(data.get("overwrite", False)),
            )
            await ws.send_json({"type": "org_saved_save_as", "payload": result.payload})
        except ServiceError as exc:
            await self._ack_saved_err(ws, "org_saved_save_as", str(data.get("name") or ""), exc.message)

    async def _handle_org_saved_create(self, ws: Any, data: dict) -> None:
        """Create, save, and activate a new custom organization."""
        name = str(data.get("organization_name") or data.get("name") or "").strip()
        try:
            members = data.get("members")
            result = await self._ensure_office_services().org.saved_create(
                organization_name=name,
                members=members if isinstance(members, list) else [],
            )
            org_id = str(result.payload.get("organization_id") or result.payload.get("name") or "").strip()
            ok = await self._apply_mode_switch(
                "org",
                "custom",
                getattr(self, "_task_preferred_agent", "native"),
                org_id=org_id,
            )
            if not ok:
                await ws.send_json({"type": "org_saved_create", "payload": {
                    "ok": False,
                    "name": org_id or name,
                    "error": getattr(self, "_last_org_load_error", "") or "org_activation_failed",
                }})
                return
            await ws.send_json({"type": "org_saved_create", "payload": result.payload})
        except ServiceError as exc:
            await self._ack_saved_err(ws, "org_saved_create", name, exc.message)

    async def _handle_org_saved_load(self, ws: Any, data: dict) -> None:
        """Activate a saved org. Uses _apply_org_config so errors surface
        correctly (the previous delegation to _handle_org_config_import
        sent a spurious org_config_import response and unconditionally
        acked ok=True even on failure)."""
        name = str(data.get("organization_id") or data.get("name") or "")
        try:
            organization_id = validate_saved_org_id(name)
            path = org_config_path(Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config", organization_id)
        except ValueError as exc:
            return await self._ack_saved_err(ws, "org_saved_load", name, str(exc))
        if not path.exists():
            return await self._ack_saved_err(ws, "org_saved_load", name, "not_found")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return await self._ack_saved_err(ws, "org_saved_load", name, f"read_failed: {exc}")
        ok, payload = await self._apply_org_config(raw, dry_run=False, allow_mode_transition=True)
        if ok:
            await self._set_active_saved_org_name(organization_id)
            await ws.send_json({"type": "org_saved_load", "payload": {
                "ok": True,
                "name": organization_id,
                "organization_id": organization_id,
                "filename": path.name,
                **payload,
            }})
        else:
            await ws.send_json({"type": "org_saved_load", "payload": {
                "ok": False, "name": name, **payload,
            }})

    async def _handle_org_saved_delete(self, ws: Any, data: dict) -> None:
        """Remove a saved-org file. Never touches the active config file."""
        name = str(data.get("organization_id") or data.get("name") or "")
        try:
            result = await self._ensure_office_services().org.saved_delete(name)
            await ws.send_json({"type": "org_saved_delete", "payload": result.payload})
        except ServiceError as exc:
            await self._ack_saved_err(ws, "org_saved_delete", name, exc.message)

    async def _handle_org_config_export(self, ws: Any, data: dict) -> None:
        """Build and return current org config as a YAML string from live engine state."""
        result = await self._ensure_office_services().org.export_config()
        await ws.send_json({"type": "org_config_export", "payload": {"yaml": result.payload.get("yaml", "")}})

    async def _apply_org_config(
        self,
        raw_yaml: str,
        *,
        dry_run: bool,
        allow_mode_transition: bool = False,
    ) -> tuple[bool, dict]:
        """Parse, validate and (optionally) apply an org architecture YAML string.

        Pure function with no WS I/O — callers format their own response.
        The apply path takes self._config_lock and broadcasts org_info on
        success; dry-run and error paths do neither.

        Returns (ok, payload_dict):
          Success: (True, {"dry_run": bool, "preview": {roles_added, roles_removed, employees_changed}})
          Error:   (False, {"error": str, "validation_errors": list[str]})
        """
        try:
            snapshot = parse_org_architecture_snapshot(raw_yaml)
            existing = self.engine.config
            validated_config = apply_org_architecture_snapshot(existing, snapshot)
            try:
                validate_saved_org_id(getattr(validated_config.org, "organization_id", ""))
            except ValueError:
                if "organization_id" in snapshot:
                    raise
                raw_name = str(
                    getattr(validated_config.org, "organization_name", "")
                    or getattr(validated_config.org, "company_name", "")
                    or "org"
                ).strip()
                config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
                organization_id = allocate_org_config_id(config_dir, raw_name)
                validated_config.org.organization_id = organization_id
                validated_config.org.organization_name = raw_name
                validated_config.org.organization_config_file = org_config_relative_path(organization_id)
            validated_config.org.company_profile = "custom"
            try:
                from opc.core.employee_registry import load_company_employees

                config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
                validated_config.org.employees = load_company_employees(
                    Path(config_dir).parent,
                    validated_config.org.organization_id,
                    list(validated_config.org.employees),
                )
            except Exception:
                logger.opt(exception=True).debug("Failed to load employee registry for applied org config")
            roles_before = {r.id for r in existing.org.roles}
            roles_after = {r.id for r in validated_config.org.roles}
            employees_before = len(existing.org.employees)
            preview = {
                "roles_added": len(roles_after - roles_before),
                "roles_removed": len(roles_before - roles_after),
                "employees_changed": abs(
                    len(validated_config.org.employees) - employees_before
                ),
            }
            if dry_run:
                return True, {"dry_run": True, "preview": preview}
            validate_runnable_org_config(validated_config)
            current_mode = getattr(self, "_exec_mode", None)
            if not allow_mode_transition and current_mode is not None and str(current_mode or "").strip().lower() not in {"org", "custom"}:
                return False, {
                    "error": "Corporate organization is read-only. Select or create a saved custom org before editing.",
                    "code": "org_read_only",
                    "validation_errors": [],
                }
            async with self._config_lock:
                self._write_active_org_config(validated_config)
                self._rebind_engine_config(validated_config)
                self.engine.org_engine.reload_from_config()

            target_mode, target_profile = self._target_mode_for_profile(
                validated_config.org.company_profile
            )
            current_mode = getattr(self, "_exec_mode", None)
            current_profile = getattr(self, "_company_profile", "corporate")
            mode_changed = target_mode is not None and (
                target_mode != current_mode
                or (target_mode == "company" and target_profile != current_profile)
            )
            runtime_ready = all(
                hasattr(self, attr)
                for attr in ("agent_store", "chat_store", "event_adapter")
            )
            if mode_changed and runtime_ready:
                await self._apply_mode_switch(
                    target_mode,
                    target_profile or current_profile,
                    getattr(self, "_task_preferred_agent", "native"),
                    sync_config=False,
                )
            else:
                if hasattr(self, "agent_store"):
                    await self._prune_stale_agent_store_entries()
                    if getattr(self, "_exec_mode", "") in {"org", "custom"}:
                        await self._ensure_custom_role_agents()
                        if runtime_ready:
                            await self._broadcast_snapshot()
            await self._broadcast_org_info()
            return True, {"dry_run": False, "preview": preview}
        except Exception as exc:
            validation_errors: list[str] = []
            try:
                from pydantic import ValidationError
                if isinstance(exc, ValidationError):
                    validation_errors = [str(e) for e in exc.errors()]
            except ImportError:
                pass
            return False, {"error": str(exc), "validation_errors": validation_errors}

    async def _handle_org_config_import(self, ws: Any, data: dict) -> None:
        """WS endpoint: user-initiated YAML import. Thin adapter around
        _apply_org_config. Saved-org Load uses _handle_org_saved_load
        which also calls _apply_org_config directly (not this handler),
        so the two flows don't cross-pollute their WS response types."""
        raw_yaml: str = data.get("yaml", "")
        dry_run: bool = data.get("dry_run", True)
        ok, payload = await self._apply_org_config(raw_yaml, dry_run=dry_run)
        await ws.send_json({"type": "org_config_import", "payload": {"ok": ok, **payload}})

    async def _handle_market_browse(self, ws: Any, data: dict) -> None:
        """Return all available architecture presets for browsing."""
        result = await self._ensure_office_services().market.browse()
        await ws.send_json({"type": "market_browse", "payload": result.payload})

    async def _handle_market_preview(self, ws: Any, data: dict) -> None:
        """Return full details of a single architecture preset."""
        try:
            result = await self._ensure_office_services().market.preview(data.get("preset_id", ""))
            await ws.send_json({"type": "market_preview", "payload": result.payload})
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_preview")

    async def _handle_market_apply_preset(self, ws: Any, data: dict) -> None:
        """Apply a built-in architecture preset to the current org."""
        try:
            result = await self._ensure_office_services().market.apply_preset(
                preset_id=data.get("preset_id", ""),
                strategy=data.get("strategy", "namespace"),
            )
            await self._publish_service_result(result)
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_apply_preset")
        except Exception as exc:
            logger.warning(f"Market apply preset failed: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_market_list_installed(self, ws: Any, data: dict) -> None:
        """Return list of installed market packages."""
        result = await self._ensure_office_services().market.list_installed()
        await ws.send_json({"type": "market_list_installed", "payload": result.payload})

    async def _handle_market_export(self, ws: Any, data: dict) -> None:
        """Export current org as an .opcpkg package."""
        try:
            result = await self._ensure_office_services().market.export(
                package_id=data.get("package_id", ""),
                name=data.get("name", ""),
                description=data.get("description", ""),
                version=data.get("version", "1.0.0"),
                output_dir=str(Path(getattr(self.engine, "opc_home", Path.cwd() / ".opc")) / "exports"),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_export")
        except Exception as exc:
            logger.warning(f"Market export failed: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_market_install(self, ws: Any, data: dict) -> None:
        """Install an .opcpkg package from a local path."""
        try:
            result = await self._ensure_office_services().market.install(
                path=data.get("path", ""),
                strategy=data.get("strategy", "namespace"),
            )
            await self._publish_service_result(result)
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_install")
        except Exception as exc:
            logger.warning(f"Market install failed: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_market_uninstall(self, ws: Any, data: dict) -> None:
        """Uninstall a market package and clean related role/agent state."""
        try:
            result = await self._ensure_office_services().market.uninstall(data.get("package_id", ""))
            await self._publish_service_result(result)
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_uninstall")
        except Exception as exc:
            logger.warning(f"Market uninstall failed: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_bulk_add_roles(self, ws: Any, data: dict) -> None:
        """Add multiple roles atomically in a single transaction."""
        try:
            roles_data = data.get("roles", [])
            if not roles_data or not isinstance(roles_data, list):
                await self._send_ack(ws, ok=False, error="roles list required")
                return
            result = await self._ensure_office_services().org.bulk_add_roles(roles_data)
            await self._publish_service_result(result)
            await self._send_ack(
                ws,
                ok=True,
                action="roles_added",
                role_ids=result.payload.get("role_ids", []),
                count=result.payload.get("count", 0),
            )
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="bulk_add_roles")
        except Exception as exc:
            logger.warning(f"Failed to bulk add roles: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_add_role(self, ws: Any, data: dict) -> None:
        """Add a new role to the organisation."""
        try:
            result = await self._ensure_office_services().org.add_role(data)
            await self._publish_service_result(result)
            role = dict(result.payload.get("role", {}) or {})
            await self._send_ack(ws, ok=True, action="role_added", role_id=role.get("id") or role.get("role_id"))
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="add_role")
        except Exception as exc:
            logger.warning(f"Failed to add role: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_update_role(self, ws: Any, data: dict) -> None:
        """Update a role's relationships and runtime fields."""
        try:
            role_id = str(data.get("role_id", "") or "").strip()
            if not role_id:
                await self._send_ack(ws, ok=False, error="role_id required")
                return
            result = await self._ensure_office_services().org.update_role(role_id, data)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="role_updated", role_id=role_id)
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="update_role")
        except Exception as exc:
            logger.warning(f"Failed to update role: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_update_org_strategy(self, ws: Any, data: dict) -> None:
        try:
            result = await self._ensure_office_services().org.update_org_strategy(
                final_decider_role_id=str(data.get("final_decider_role_id", "") or "").strip() or None,
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="org_strategy_updated")
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="update_org_strategy")
        except Exception as exc:
            logger.warning(f"Failed to update org strategy: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_delete_role(self, ws: Any, data: dict) -> None:
        """Delete a role and its employees from the organisation."""
        try:
            role_id = str(data.get("role_id", "") or "").strip()
            if not role_id:
                await self._send_ack(ws, ok=False, error="role_id required")
                return
            result = await self._ensure_office_services().org.delete_role(role_id)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="role_deleted", role_id=role_id)
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="delete_role")
        except Exception as exc:
            logger.warning(f"Failed to delete role: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_update_runtime_policy(self, ws: Any, data: dict) -> None:
        """Update the runtime policy for custom mode."""
        try:
            result = await self._ensure_office_services().org.update_runtime_policy(data.get("policy", {}) or {})
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="runtime_policy_updated")
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="update_runtime_policy")
        except Exception as exc:
            logger.warning(f"Failed to update runtime policy: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_reset_architecture(self, ws: Any, data: dict) -> None:
        """Clear all custom roles, employees, runtime, and installed packages."""
        try:
            result = await self._ensure_office_services().org.reset_architecture()
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="architecture_reset")
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="reset_architecture")
        except Exception as exc:
            logger.warning(f"Failed to reset architecture: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_comms_state(self, ws: Any, data: dict) -> None:
        """Return a snapshot of the file-based comms layout for a session."""
        if self._shutting_down:
            return
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().comms.state(
                project_id=project_id,
                task_id=str(data.get("task_id", "") or ""),
                session_id=str(data.get("session_id", "") or ""),
            )
            await ws.send_json({"type": "comms_state", "payload": result.payload})
        except ServiceError as exc:
            await ws.send_json({"type": "comms_state", "payload": {"available": False, "reason": exc.message, **exc.payload}})
        except Exception as exc:
            await ws.send_json({"type": "comms_state", "payload": {"available": False, "reason": str(exc)}})

    async def _handle_comms_read_message(self, ws: Any, data: dict) -> None:
        """Read the body of a single comms message file for the UI viewer."""
        if self._shutting_down:
            return
        try:
            result = await self._ensure_office_services().comms.read(
                project_id=self._request_project_id(data),
                task_id=str(data.get("task_id", "") or ""),
                path=str(data.get("path", "") or ""),
            )
            await ws.send_json({"type": "comms_message", "payload": result.payload})
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="comms_read_message")
