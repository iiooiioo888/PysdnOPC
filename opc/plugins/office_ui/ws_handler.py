"""WebSocket Handler — routes all messages between frontend and OPC.

Routes inbound UI messages and outbound event envelopes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from loguru import logger
from opc.core.active_task_runs import (
    ActiveTaskRunAdmissionClosed,
    ActiveTaskRunRegistry,
)
from opc.core.config import (
    OPCConfig,
    get_project_workplace,
    slugify_organization_name,
    validate_organization_id,
)
from opc.core.org_config import (
    allocate_org_config_id,
    apply_org_config_payload_to_config,
    build_org_config_payload_from_config,
    list_org_config_paths,
    load_org_config_payload,
    org_config_filename,
    org_config_path,
    org_config_relative_path,
    org_configs_dir,
    read_org_index,
    validate_runnable_org_config,
    validate_saved_org_id,
    write_org_config_payload,
    write_org_index,
)
from opc.core.models import normalize_role_runtime_status
from opc.core.transcript_visibility import rendered_transcript_metadata_visible
from opc.presentation.kanban import build_company_board_columns
from opc.layer2_organization.phase import (
    kanban_column,
    should_hide_work_item_from_company_kanban,
)
from opc.layer2_organization.company_runtime_identity import (
    ACTIVE_COMPANY_RUNTIME_CHECKPOINT_STATUSES,
    COMPANY_RUNTIME_CHECKPOINT_TYPES,
    is_company_runtime_task,
    load_company_runtime_identity_index,
)
from opc.layer2_organization.work_item_identity import (
    work_item_identity_payload,
    work_item_identity_payload_for_task,
    work_item_projection_id_from_metadata,
    work_item_turn_type_from_metadata,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer2_organization.work_item_transition import (
    apply_task_status_transition,
)
from opc.layer2_organization.org_work_item_planner import build_custom_org_work_item_blueprint
from opc.layer4_tools.output_budget import clip_text

if TYPE_CHECKING:
    import aiohttp.web
    from opc.engine import OPCEngine
    from opc.plugins.office_ui.agent_store import AgentStore
    from opc.plugins.office_ui.chat_store import ChatStore
    from opc.plugins.office_ui.event_adapter import EventAdapter

from opc.plugins.office_ui.dispatcher import Dispatcher
from opc.plugins.office_ui.services import (
    ModeState,
    OfficeServiceContext,
    OfficeServices,
    ServiceError,
    ServiceResult,
    SessionService,
)
from opc.plugins.office_ui.snapshot_builder import (
    STATUS_TO_COLUMN,
    _build_company_runtime_control_by_task,
    collapse_adjacent_transcript_duplicates,
    _build_session_context_preview,
    _extract_markdown_text,
    _sanitize_ui_message_dict,
    _normalize_transcript_detail_level,
    _task_parent_session_link,
    build_collab_sync,
    build_project_index_sync,
    build_transcript_ui_messages,
    build_snapshot,
)
from opc.plugins.office_ui.org_architecture_snapshot import (
    apply_org_architecture_snapshot,
    build_org_architecture_snapshot,
    dump_org_architecture_snapshot,
    parse_org_architecture_snapshot,
)
from opc.plugins.office_ui._ws_utils import (
    _ACTIVE_SAVED_ORG_STATE_KEY,
    _GENERIC_ESCALATION_OPTIONS,
    _PERSISTED_WORKER_NOTIFICATION_KINDS,
    _PROJECT_SCOPED_ENVELOPE_TYPES,
    _RUNTIME_TASK_VISIBILITY_EVENT_TYPES,
    _SAVED_ORG_NAME_LAX_RE,
    _SAVED_ORG_NAME_RE,
    _TASK_MODE_PREFERRED_AGENTS,
    _add_execution_turn_aliases,
    _compact_session_title,
    _is_cjk_title_char,
    _looks_like_escalation_reply,
    _normalize_escalation_key,
    _normalize_escalation_reply,
    _saved_org_path,
    _saved_orgs_dir,
    _ui_conversation_turn_id,
    _ui_message_identity_metadata,
)


_TASK_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES: frozenset[str] = frozenset({
    "message_start",
    "message_stop",
    "tool_call_delta",
    "status_snapshot",
    "context_usage",
    "cost_update",
    "task_ledger_updated",
    "prompt_prefix_state",
    "prompt_prefix_cache_fingerprint",
    "prefetch_started",
    "prefetch_completed",
    "prefetch_consumed",
    "durable_memory_extracted",
    "durable_memory_extraction_failed",
    "tool_hook",
    "turn_started",
    "turn_completed",
    "member_idle",
})


_TASK_MODE_DEBUG_ONLY_PROGRESS_TYPES: frozenset[str] = frozenset({
    "compaction_applied",
})


# Company mode shares the task-mode noise list; runtime bookkeeping events
# (turns, status snapshots, cost ticks) carry no reviewable content and drown
# out thinking/tool entries in the per-role activity feed.
_COMPANY_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES: frozenset[str] = (
    _TASK_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES | frozenset({"member_inbox_updated"})
)


# These handlers can hand an accepted UI request to engine execution.  Reserve
# at the router boundary (before their first await), because the persisted task
# mode may not be known until the handler loads the task from the project store.
_EXECUTION_HANDOFF_MESSAGE_TYPES: frozenset[str] = frozenset({
    "run_task",
    "session_send",
    "session_resume",
})


_TASK_MODE_VISIBLE_RUNTIME_PROGRESS_TYPES: frozenset[str] = frozenset({
    "thinking_delta",
    "tool_started",
    "tool_progress",
    "tool_completed",
    "permission_requested",
    "permission_resolved",
    "checkpoint_saved",
    "turn_failed",
})


class ProjectScopeError(ValueError):
    """Raised when a project-scoped WS request is missing its explicit scope."""



# --- Mixin imports ---
from opc.plugins.office_ui._ws_tasks import WsTaskMixin
from opc.plugins.office_ui._ws_company import WsCompanyMixin
from opc.plugins.office_ui._ws_chat import WsChatMixin
from opc.plugins.office_ui._ws_config import WsConfigMixin

class WSHandler(
    WsTaskMixin,
    WsCompanyMixin,
    WsChatMixin,
    WsConfigMixin,
):
    """Routes all WebSocket messages between frontend and OPC."""

    def __init__(
        self,
        engine: OPCEngine,
        agent_store: AgentStore,
        chat_store: ChatStore,
        event_adapter: EventAdapter,
    ) -> None:
        self.engine = engine
        self._root_engine = engine
        self._active_project_id = str(engine.project_id or "default").strip() or "default"
        self._project_switch_lock = asyncio.Lock()
        self.agent_store = agent_store
        self.chat_store = chat_store
        self.event_adapter = event_adapter
        self._clients: set[aiohttp.web.WebSocketResponse] = set()
        self._client_project_ids: dict[Any, str] = {}
        self._client_switch_seq: dict[Any, str] = {}
        self._client_project_index_tasks: dict[Any, asyncio.Task[Any]] = {}
        self._client_initial_state_tasks: dict[Any, asyncio.Task[Any]] = {}
        self._exec_mode: str = "task"  # restored from DB in restore_persisted_mode()
        self._company_profile: str = "corporate"
        self._task_preferred_agent: str = "native"
        self._local_talent_cache: list[Any] | None = None  # invalidated on import/hire
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._task_bg_map: dict[str, set[asyncio.Task[Any]]] = {}
        self._task_bg_context: dict[asyncio.Task[Any], dict[str, Any]] = {}
        self._handoff_route_tasks: dict[asyncio.Task[Any], str] = {}
        self._broadcast_seq: int = 0
        self._broadcast_lock = asyncio.Lock()
        self._task_locks: dict[str, asyncio.Lock] = {}
        # Tracks which asyncio.Task currently holds the per-task lock. If the
        # prior holder finishes/crashes without reaching the finally block that
        # pops the lock, the next acquirer would block forever. We detect that
        # case by checking ``holder.done()`` in ``_get_task_lock`` and replace
        # the stale lock before the new acquirer blocks.
        self._task_lock_holders: dict[str, asyncio.Task[Any]] = {}
        self._config_lock = asyncio.Lock()
        self._active_runtime_children: dict[str, str] = {}
        self._secretary_session_id: str | None = None
        self._secretary_session_ids: dict[str, str] = {}
        self._session_to_task: dict[str, str] = {}
        self._ui_task_aliases: dict[str, str] = {}
        self._pending_escalations: dict[str, dict[str, Any]] = {}
        self._pending_escalation_order: list[str] = []
        self._progress_buffer: dict[str, list[dict[str, Any]]] = {}
        self._progress_project_ids: dict[str, str] = {}
        self._assistant_delta_buffers: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._assistant_delta_flush_tasks: dict[tuple[str, str, str], asyncio.Task[None]] = {}
        self._assistant_delta_seq: int = 0
        self._ASSISTANT_DELTA_FLUSH_INTERVAL_SEC = 0.05
        self._stop_requested_task_ids: set[str] = set()
        self._company_stop_intents: dict[str, dict[str, Any]] = {}
        self._company_stop_finalize_tasks: dict[str, asyncio.Task[Any]] = {}
        self._company_suspend_reply_locks: dict[str, asyncio.Lock] = {}
        self._company_delivery_feedback_reply_locks: dict[str, asyncio.Lock] = {}
        # Buffer progress entries before UPSERTing to SQLite. Raised from 1
        # so bursts (codex streaming thinking chunks, multi-line tool output)
        # don't hammer the DB once per entry, but kept small enough that a
        # task sitting idle between work items still shows up on the Activity
        # panel without waiting for 10 entries to accumulate. A periodic
        # flush in ``_periodic_flush_loop`` catches tasks that emit sparsely
        # so at most ``_PROGRESS_FLUSH_INTERVAL_SEC`` of entries are held
        # in RAM before they're persisted and visible on page refresh.
        self._PROGRESS_FLUSH_THRESHOLD = 2
        self._PROGRESS_FLUSH_INTERVAL_SEC = 3.0
        self._progress_flush_task: asyncio.Task[None] | None = None
        self._shutting_down: bool = False
        self._active_message_tasks: set[asyncio.Task[Any]] = set()
        self.dispatcher = Dispatcher(engine, chat_store)
        self.services_context = OfficeServiceContext(
            engine=engine,
            agent_store=agent_store,
            chat_store=chat_store,
            event_adapter=event_adapter,
            mode_state=ModeState(
                exec_mode=self._exec_mode,
                company_profile=self._company_profile,
                task_preferred_agent=self._task_preferred_agent,
            ),
        )
        self.services_context.config_lock = self._config_lock
        self.services_context.background_tasks = self._background_tasks
        self.services_context.task_bg_map = self._task_bg_map
        self.services_context.task_bg_context = self._task_bg_context
        self.services_context.session_to_task = self._session_to_task
        self.services_context.active_runtime_children = self._active_runtime_children
        self.services_context.stop_requested_task_ids = self._stop_requested_task_ids
        self.services_context.task_locks = self._task_locks
        self.services_context.task_lock_holders = self._task_lock_holders
        self.services_context.wire_engine_callbacks = self._wire_engine_callbacks  # type: ignore[attr-defined]
        self.services_context.load_active_org_config = lambda org_id: self._load_active_org_config_into_engine(org_id)
        self.services_context.set_active_saved_org_name = self._service_set_active_saved_org_name
        self.services_context.get_active_saved_org_name = self._service_get_active_saved_org_name
        self.services_context.project_workplace_hook = lambda project_id: get_project_workplace(project_id)  # type: ignore[attr-defined]
        self.services_context.on_engine_activated = self._on_service_engine_activated
        self.services_context.persist_runtime_config = self._persist_runtime_config
        self.services_context.rebind_engine_config = self._rebind_engine_config
        self.services_context.sync_role_map = self._sync_role_map
        self.services_context.ensure_custom_role_agents = self._ensure_custom_role_agents
        self.services_context.broadcast_snapshot = self._broadcast_snapshot
        self.services_context.cancel_session_tasks = self._cancel_session_tasks
        self.services_context.cancel_task_tree = self._cancel_task_tree
        self.services = OfficeServices(self.services_context)
        self._wire_engine_callbacks(engine)

    def _on_service_engine_activated(self, engine: Any, project_id: str) -> None:
        self.engine = engine
        self.dispatcher = Dispatcher(engine, self.chat_store)
        self._active_project_id = self._normalize_project_id(project_id)
        self._refresh_engine_attachment_store()

    def _ensure_office_services(self) -> OfficeServices:
        """Create service wiring for tests that instantiate WSHandler via __new__."""
        if hasattr(self, "services") and hasattr(self, "services_context"):
            self.services_context.mode_state.exec_mode = getattr(self, "_exec_mode", "task")
            self.services_context.mode_state.company_profile = getattr(self, "_company_profile", "corporate")
            self.services_context.mode_state.task_preferred_agent = getattr(self, "_task_preferred_agent", "native")
            return self.services
        context = OfficeServiceContext(
            engine=self.engine,
            agent_store=getattr(self, "agent_store", None),
            chat_store=getattr(self, "chat_store", None),
            event_adapter=getattr(self, "event_adapter", None),
            mode_state=ModeState(
                exec_mode=getattr(self, "_exec_mode", "task"),
                company_profile=getattr(self, "_company_profile", "corporate"),
                task_preferred_agent=getattr(self, "_task_preferred_agent", "native"),
            ),
        )
        if hasattr(self, "_config_lock"):
            context.config_lock = self._config_lock
        for attr in ("_background_tasks", "_task_bg_map", "_task_bg_context", "_session_to_task", "_task_locks", "_task_lock_holders"):
            if hasattr(self, attr):
                setattr(context, attr.removeprefix("_"), getattr(self, attr))
        if hasattr(self, "_wire_engine_callbacks"):
            context.wire_engine_callbacks = self._wire_engine_callbacks  # type: ignore[attr-defined]
        if hasattr(self, "_load_active_org_config_into_engine"):
            context.load_active_org_config = lambda org_id: self._load_active_org_config_into_engine(org_id)
        if hasattr(self, "_service_set_active_saved_org_name"):
            context.set_active_saved_org_name = self._service_set_active_saved_org_name
        if hasattr(self, "_service_get_active_saved_org_name"):
            context.get_active_saved_org_name = self._service_get_active_saved_org_name
        if hasattr(self, "_on_service_engine_activated"):
            context.on_engine_activated = self._on_service_engine_activated
        if hasattr(self, "_persist_runtime_config"):
            context.persist_runtime_config = self._persist_runtime_config
        if hasattr(self, "_rebind_engine_config"):
            context.rebind_engine_config = self._rebind_engine_config
        if hasattr(self, "_sync_role_map"):
            context.sync_role_map = self._sync_role_map
        if hasattr(self, "_ensure_custom_role_agents"):
            context.ensure_custom_role_agents = self._ensure_custom_role_agents
        if hasattr(self, "_broadcast_snapshot"):
            context.broadcast_snapshot = self._broadcast_snapshot
        if hasattr(self, "_cancel_session_tasks"):
            context.cancel_session_tasks = self._cancel_session_tasks
        if hasattr(self, "_cancel_task_tree"):
            context.cancel_task_tree = self._cancel_task_tree
        self.services_context = context
        self.services = OfficeServices(context)
        return self.services

    async def _service_set_active_saved_org_name(self, org_id: str) -> None:
        await self._set_active_saved_org_name(org_id)

    async def _service_get_active_saved_org_name(self) -> str:
        return await self._get_active_saved_org_name()

    @staticmethod
    def _normalize_project_id(project_id: str | None) -> str:
        return str(project_id or "default").strip() or "default"

    def _active_engine_project_id(self) -> str:
        return self._normalize_project_id(getattr(self.engine, "project_id", None) or self._active_project_id)

    def _client_active_project_id(self, ws: Any | None = None) -> str:
        if ws is not None:
            project_id = str(self._client_project_ids.get(ws, "") or "").strip()
            if project_id:
                return self._normalize_project_id(project_id)
        return self._active_engine_project_id()

    def _request_project_id(self, data: dict[str, Any] | None) -> str:
        raw = (data or {}).get("project_id") or (data or {}).get("projectId")
        project_id = str(raw or "").strip()
        if not project_id:
            raise ProjectScopeError("project_id required for project-scoped request")
        return self._normalize_project_id(project_id)

    @staticmethod
    def _payload_project_id(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("project_id", "projectId", "active_project_id", "activeProjectId"):
            value = payload.get(key)
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("project_id", "projectId"):
                value = data.get(key)
                normalized = str(value or "").strip()
                if normalized:
                    return normalized
        return ""

    async def _engine_for_request(self, data: dict[str, Any] | None) -> tuple[Any, str]:
        project_id = self._request_project_id(data)
        engine = await self._engine_for_project(project_id)
        return engine, project_id

    def _progress_callback_for_engine(self, engine: Any) -> Any:
        async def _progress(text: str, **kw: Any) -> None:
            # UI progress is a best-effort display copy: a failure here (e.g.
            # a locked ui_state.db) must never crash the agent execution that
            # emitted the progress line.
            try:
                await self.on_progress(
                    text,
                    _runtime_engine=engine,
                    _project_id=self._normalize_project_id(getattr(engine, "project_id", None)),
                    **kw,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).warning(
                    "UI progress handling failed; agent execution continues"
                )

        return _progress

    def _runtime_event_callback_for_engine(self, engine: Any) -> Any:
        async def _runtime_event(event: Any) -> None:
            try:
                await self.on_opc_event(
                    event,
                    runtime_engine=engine,
                    project_id=self._normalize_project_id(getattr(engine, "project_id", None)),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).warning(
                    "UI runtime-event handling failed; agent execution continues"
                )

        setattr(_runtime_event, "_opc_ui_handler_id", id(self))
        setattr(_runtime_event, "_opc_ui_project_id", self._normalize_project_id(getattr(engine, "project_id", None)))
        return _runtime_event

    def _escalation_callback_for_engine(self, engine: Any) -> Any:
        async def _escalation(message: str, options: list[dict]) -> str | None:
            return await self._handle_ui_escalation(
                message,
                options,
                project_id=self._normalize_project_id(getattr(engine, "project_id", None)),
            )

        setattr(_escalation, "_opc_ui_handler_id", id(self))
        setattr(_escalation, "_opc_ui_project_id", self._normalize_project_id(getattr(engine, "project_id", None)))
        return _escalation

    def _kanban_callback_for_engine(self, engine: Any) -> Any:
        async def _kanban_changed() -> None:
            try:
                await self.on_kanban_changed(engine=engine)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).warning(
                    "UI kanban refresh failed; agent execution continues"
                )

        return _kanban_changed

    def _wire_engine_callbacks(self, engine: Any) -> None:
        try:
            progress_callback = self._progress_callback_for_engine(engine)
            runtime_event_callback = self._runtime_event_callback_for_engine(engine)
            escalation_callback = self._escalation_callback_for_engine(engine)
            engine.on_company_runtime_children = self._register_company_runtime_children
            engine.on_company_kanban_callback_factory = self._kanban_callback_for_engine
            engine.on_escalation = escalation_callback
            engine.on_progress = progress_callback
            engine.on_runtime_event = runtime_event_callback
            if getattr(engine, "escalation", None):
                engine.escalation.user_reply_callback = escalation_callback
            company_executor = getattr(engine, "company_executor", None)
            if company_executor is not None:
                company_executor.progress_callback = progress_callback
                company_executor.on_kanban_changed = self._kanban_callback_for_engine(engine)
            reorg_manager = getattr(engine, "reorg_manager", None)
            if reorg_manager is not None:
                reorg_manager.progress_callback = progress_callback
            event_bus = getattr(engine, "event_bus", None)
            forward_runtime_event = getattr(engine, "_forward_runtime_event", None)
            if (
                engine is not self._root_engine
                and event_bus is not None
                and callable(getattr(event_bus, "subscribe_all", None))
            ):
                # Delegates need all events, not only runtime_event. They may
                # have inherited a typed runtime forwarder during initialize();
                # remove it so runtime_event is not delivered twice.
                listeners_by_type = getattr(event_bus, "_listeners", {})
                runtime_list = listeners_by_type.get("runtime_event", []) if listeners_by_type is not None else []
                if callable(forward_runtime_event) and isinstance(runtime_list, list):
                    runtime_list[:] = [
                        listener for listener in runtime_list
                        if listener != forward_runtime_event
                    ]
                project_marker = self._normalize_project_id(getattr(engine, "project_id", None))
                global_listeners = getattr(event_bus, "_global_listeners", [])
                already_subscribed = any(
                    getattr(listener, "_opc_ui_handler_id", None) == id(self)
                    and getattr(listener, "_opc_ui_project_id", None) == project_marker
                    for listener in list(global_listeners or [])
                )
                if not already_subscribed:
                    event_bus.subscribe_all(runtime_event_callback)
        except Exception:
            logger.opt(exception=True).debug(
                f"Failed to wire UI callbacks for project engine {getattr(engine, 'project_id', None)!r}",
            )

    @staticmethod
    def _is_real_opc_engine(engine: Any) -> bool:
        try:
            from opc.engine import OPCEngine as _OPCEngine
        except Exception:
            return False
        return isinstance(engine, _OPCEngine)

    async def _engine_for_project(self, project_id: str) -> Any:
        normalized = self._normalize_project_id(project_id)
        root = self._root_engine
        current_root_project = self._normalize_project_id(getattr(root, "project_id", None))
        if normalized == current_root_project:
            engine = root
        else:
            delegate_getter = getattr(root, "_get_project_delegate", None)
            explicit_delegate = "_get_project_delegate" in getattr(root, "__dict__", {})
            if not callable(delegate_getter) or not (self._is_real_opc_engine(root) or explicit_delegate):
                raise RuntimeError(
                    "Project switching requires OPCEngine project delegates or an explicit "
                    "_get_project_delegate test double."
                )
            maybe_engine = delegate_getter(normalized)
            engine = await maybe_engine if inspect.isawaitable(maybe_engine) else maybe_engine
        # Self-heal a closed store (project deleted then re-created while this
        # engine instance stayed bound to it — e.g. the root engine, which can
        # never be evicted from its own delegate cache).
        store = getattr(engine, "store", None)
        if store is not None and not getattr(store, "is_ready", True):
            ensure_ready = getattr(store, "ensure_ready", None)
            if callable(ensure_ready):
                logger.warning(f"Reopening closed store for project '{normalized}'")
                await ensure_ready()
        self._wire_engine_callbacks(engine)
        return engine

    async def _activate_project(self, project_id: str) -> Any:
        engine = await self._engine_for_project(project_id)
        self.engine = engine
        self.dispatcher = Dispatcher(engine, self.chat_store)
        self._active_project_id = self._normalize_project_id(getattr(engine, "project_id", None) or project_id)
        self._refresh_engine_attachment_store()
        return engine

    def _refresh_engine_attachment_store(self) -> None:
        """Ensure the engine attachment store matches the active project."""
        ensure_attachment_store = getattr(self.engine, "_ensure_attachment_store", None)
        if not callable(ensure_attachment_store):
            return
        try:
            ensure_attachment_store()
        except Exception as exc:
            logger.warning(f"Failed to refresh attachment store for project {self.engine.project_id!r}: {exc}")

    def _register_company_runtime_children(self, parent_session_id: str, child_task_ids: list[str]) -> None:
        """Called by engine when company mode creates child work-item tasks.

        Maps each child task_id to the primary task_id (the one the user
        initiated) so that ``on_progress`` can dual-route runtime events.
        """
        origin_task_id = self._session_to_task.get(parent_session_id)
        if not origin_task_id:
            # Fallback: try to find by iterating known mappings
            for sid, tid in self._session_to_task.items():
                if sid == parent_session_id or tid == parent_session_id:
                    origin_task_id = tid
                    break
        if not origin_task_id:
            logger.warning(f"Cannot map runtime children: parent_session_id={parent_session_id} not in _session_to_task")
            return
        for child_id in child_task_ids:
            self._active_runtime_children[child_id] = origin_task_id
            # Also register child task_id in _session_to_task for progress routing
            self._session_to_task[child_id] = child_id

    def _get_task_lock(self, task_id: str) -> asyncio.Lock:
        """Get or create a per-task lock for serializing messages within one session.

        Self-heals stale locks: if the previous holder coroutine is ``.done()``
        but the lock was never released (process interrupt, silent cancellation,
        unhandled exception swallowed upstream), replace it with a fresh lock so
        the next acquirer can proceed instead of blocking forever. This is what
        lets Continue / new messages work after a disconnect.
        """
        prev_holder = self._task_lock_holders.get(task_id)
        if prev_holder is not None and prev_holder.done():
            logger.warning(
                f"Replacing stale task lock for {task_id} "
                f"(prior holder done: cancelled={prev_holder.cancelled()}). "
                "Lock was not released via finally — likely disconnect or crash."
            )
            self._task_locks.pop(task_id, None)
            self._task_lock_holders.pop(task_id, None)
        lock = self._task_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._task_locks[task_id] = lock
        return lock

    def _resolve_agent_for_idle(self, task_id: str, task: Any = None) -> str | None:
        """Resolve agent_id for a task, trying event_adapter map first, then task.assigned_to."""
        agent_id = self.event_adapter._resolve_agent_from_task(task_id)
        if agent_id:
            return agent_id
        # Fallback: resolve from task's assigned_to (opc_role_id → UI agent_id)
        assigned_to = str(getattr(task, "assigned_to", "") or "").strip() if task else ""
        if assigned_to:
            return self.event_adapter._resolve_role_to_agent(assigned_to)
        return None

    @staticmethod
    def _normalize_session_detail_timestamp(value: Any) -> float | None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not numeric:
            return None
        if numeric > 1_000_000_000_000:
            numeric /= 1000.0
        return numeric

    @staticmethod
    def _message_visible_in_detail_level(message: dict[str, Any], detail_level: str) -> bool:
        metadata = dict(message.get("metadata", {}) or {})
        return rendered_transcript_metadata_visible(metadata, detail_level=detail_level)

    @classmethod
    def _filter_ui_messages_for_detail_level(
        cls,
        messages: list[dict[str, Any]],
        detail_level: str,
    ) -> list[dict[str, Any]]:
        return [
            message
            for message in messages
            if cls._message_visible_in_detail_level(message, detail_level)
        ]

    async def _sync_role_map(self) -> None:
        """Sync opc_role_id → agent_id mapping to EventAdapter."""
        role_map = await self.agent_store.get_role_agent_map()
        self.event_adapter.update_role_map(role_map)

    async def restore_persisted_mode(self) -> None:
        """Restore exec_mode, company_profile, and task preferred agent from DB on startup."""
        self._exec_mode = self._normalize_session_exec_mode(
            await self.agent_store.get_server_state("exec_mode", "task")
        )
        self._company_profile = self._normalize_session_company_profile(
            await self.agent_store.get_server_state("company_profile", "corporate")
        )
        if self._exec_mode == "org":
            self._company_profile = "custom"
        self._task_preferred_agent = self._normalize_session_preferred_agent(
            await self.agent_store.get_server_state("task_preferred_agent", "native"),
        )

        # Sync in-memory org architecture to match restored exec_mode.
        # Do not save during startup restore: disk files are the source of truth.
        if self.engine.org_engine:
            async with self._config_lock:
                if self._exec_mode in {"org", "custom"}:
                    self.engine.config.org.company_profile = "custom"
                    self.engine.org_engine.config = self.engine.config
                    self.engine.org_engine.reload_from_config()
                elif self._exec_mode == "company":
                    self._restore_company_config_into_engine(self._company_profile)
                else:
                    self._restore_company_config_into_engine("")
            if self._exec_mode in {"org", "custom"}:
                await self._restore_active_saved_org_if_needed()
        if hasattr(self, "services_context"):
            self.services_context.mode_state.exec_mode = self._exec_mode
            self.services_context.mode_state.company_profile = self._company_profile
            self.services_context.mode_state.task_preferred_agent = self._task_preferred_agent

    async def _persist_mode(self) -> None:
        """Save current exec_mode, company_profile, and task preferred agent to DB."""
        await self.agent_store.set_server_state("exec_mode", self._exec_mode)
        await self.agent_store.set_server_state("company_profile", self._company_profile)
        await self.agent_store.set_server_state("task_preferred_agent", self._task_preferred_agent)

    # ══════════════════════════════════════════════════════════════════════
    # Connection lifecycle
    # ══════════════════════════════════════════════════════════════════════

    async def handle_ws(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        """Handle a WebSocket connection."""
        import aiohttp.web as web
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if bool(getattr(self, "_shutting_down", False)):
            await ws.close()
            return ws
        self._clients.add(ws)
        self._ensure_progress_flush_loop()
        logger.info(f"WS client connected ({len(self._clients)} total)")

        try:
            # Ensure role→agent mapping is current
            await self._sync_role_map()

            # Send initial snapshot
            initial_project_id = self._active_engine_project_id()
            self._client_project_ids[ws] = initial_project_id
            snapshot = await build_snapshot(
                self.engine, self.agent_store, self.chat_store, self.event_adapter
            )
            snapshot["project_id"] = initial_project_id
            snapshot["exec_mode"] = self._exec_mode
            snapshot["company_profile"] = self._company_profile
            snapshot["task_preferred_agent"] = self._task_preferred_agent
            if not await self._safe_send_json(ws, {"type": "snapshot", "payload": snapshot}):
                return ws
            self._track_client_initial_state(
                ws,
                self._send_initial_project_state_for_client(
                    ws,
                    self.engine,
                    initial_project_id,
                ),
            )

            # Process messages
            async for msg in ws:
                if msg.type == 1:  # aiohttp.WSMsgType.TEXT
                    await self._route_message(ws, msg.data)
                elif msg.type == 2:  # BINARY
                    pass
                elif msg.type == 8:  # ERROR
                    logger.warning(f"WS error: {ws.exception()}")
        except Exception as e:
            if self._is_expected_shutdown_error(e) or self._is_ws_disconnect_error(e):
                logger.debug(f"WS handler closed during disconnect/shutdown: {type(e).__name__}: {e!r}")
            else:
                logger.error(f"WS handler error: {e}")
        finally:
            self._clients.discard(ws)
            try:
                self._client_project_ids.pop(ws, None)
                self._client_switch_seq.pop(ws, None)
                index_task = self._client_project_index_tasks.pop(ws, None)
                if index_task is not None and not index_task.done():
                    index_task.cancel()
                initial_task = self._client_initial_state_tasks.pop(ws, None)
                if initial_task is not None and not initial_task.done():
                    initial_task.cancel()
            except TypeError:
                pass
            logger.info(f"WS client disconnected ({len(self._clients)} total)")

        return ws

    # ══════════════════════════════════════════════════════════════════════
    # Outbound: OPC → Frontend
    # ══════════════════════════════════════════════════════════════════════

    async def broadcast(self, envelope: dict[str, Any]) -> None:
        """Broadcast an envelope to all connected clients."""
        if not self._clients:
            return
        async with self._broadcast_lock:
            self._broadcast_seq += 1
            prepared = self._prepare_outbound_envelope(envelope)
            if prepared is None:
                return
            envelope = prepared
            envelope["_seq"] = self._broadcast_seq
            data = json.dumps(envelope, default=str)
            envelope_type = str(envelope.get("type", "") or "")
            envelope_project_id = self._payload_project_id(envelope.get("payload"))
            if not envelope_project_id:
                envelope_project_id = str(envelope.get("project_id", "") or "").strip()
            # Snapshot client set to avoid iteration-during-mutation race
            clients = set(self._clients)
            disconnected = set()
            for ws in clients:
                if (
                    envelope_type in _PROJECT_SCOPED_ENVELOPE_TYPES
                    and envelope_project_id
                    and self._client_active_project_id(ws) != self._normalize_project_id(envelope_project_id)
                ):
                    continue
                try:
                    await ws.send_str(data)
                except Exception:
                    disconnected.add(ws)
            self._clients -= disconnected

    def _prepare_outbound_envelope(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        envelope_type = str(envelope.get("type", "") or "")
        explicit_project_id = str(envelope.get("project_id", "") or "").strip()
        payload = envelope.get("payload")
        if isinstance(payload, dict):
            payload_project_id = self._payload_project_id(payload)
            final_project_id = explicit_project_id or payload_project_id
            if envelope_type in _PROJECT_SCOPED_ENVELOPE_TYPES and not final_project_id:
                logger.warning(
                    "Dropping project-scoped UI envelope without project_id: type={}",
                    envelope_type,
                )
                return None
            if final_project_id and payload_project_id != final_project_id:
                payload = {**payload, "project_id": final_project_id}
                envelope = {**envelope, "payload": payload}
        elif envelope_type in _PROJECT_SCOPED_ENVELOPE_TYPES:
            final_project_id = explicit_project_id
            if not final_project_id:
                logger.warning(
                    "Dropping project-scoped UI envelope without payload/project_id: type={}",
                    envelope_type,
                )
                return None
        if envelope.get("type") in {"session_message", "chat_new_message"}:
            payload = envelope.get("payload")
            if isinstance(payload, dict):
                envelope = {
                    **envelope,
                    "payload": _sanitize_ui_message_dict(payload),
                }
        return envelope

    async def _send_envelope_to_client(self, ws: Any, envelope: dict[str, Any]) -> bool:
        async with self._broadcast_lock:
            self._broadcast_seq += 1
            prepared = self._prepare_outbound_envelope(envelope)
            if prepared is None:
                return False
            prepared["_seq"] = self._broadcast_seq
            return await self._safe_send_json(ws, prepared)

    async def _canonicalize_runtime_visual_event(
        self,
        visual_event: dict[str, Any],
        *,
        engine: Any | None = None,
    ) -> dict[str, Any]:
        payload = dict(visual_event.get("data", {}) or {})
        raw_runtime_task_id = str(payload.get("task_id", "") or "").strip()
        mapped_task_id = await self._ui_task_id_for_runtime_task_id(raw_runtime_task_id, engine=engine)
        if mapped_task_id:
            payload["task_id"] = mapped_task_id
            if raw_runtime_task_id and mapped_task_id != raw_runtime_task_id:
                payload.setdefault("runtime_task_id", raw_runtime_task_id)
        _add_execution_turn_aliases(payload, raw_runtime_task_id or mapped_task_id)
        if not str(payload.get("turn_id", "") or "").strip():
            runtime_session_id = str(payload.get("runtime_session_id", "") or "").strip()
            iteration = payload.get("iteration")
            if runtime_session_id and iteration not in (None, "", [], {}):
                payload["turn_id"] = f"{runtime_session_id}:{iteration}"
        return {
            **visual_event,
            "data": payload,
        }

    @staticmethod
    def _assistant_delta_key(payload: dict[str, Any], *, delta_type: str = "assistant_delta") -> tuple[str, str, str] | None:
        task_id = str(payload.get("task_id", "") or "").strip()
        if not task_id:
            return None
        turn_id = str(
            payload.get("turn_id")
            or payload.get("canonical_turn_id")
            or payload.get("execution_turn_id")
            or payload.get("runtime_task_id")
            or "active"
        ).strip() or "active"
        item_id = str(
            payload.get("item_id")
            or payload.get("stream_id")
            or delta_type
        ).strip() or delta_type
        return task_id, turn_id, item_id

    async def _delayed_flush_assistant_delta(self, key: tuple[str, str, str]) -> None:
        try:
            await asyncio.sleep(self._ASSISTANT_DELTA_FLUSH_INTERVAL_SEC)
            await self._flush_assistant_delta(key)
        finally:
            current = asyncio.current_task()
            if self._assistant_delta_flush_tasks.get(key) is current:
                self._assistant_delta_flush_tasks.pop(key, None)

    async def _flush_assistant_delta(self, key: tuple[str, str, str]) -> None:
        bucket = self._assistant_delta_buffers.pop(key, None)
        pending = self._assistant_delta_flush_tasks.pop(key, None)
        current = asyncio.current_task()
        if pending is not None and pending is not current and not pending.done():
            pending.cancel()
        if not bucket:
            return
        text = str(bucket.get("text", "") or "")
        if not text:
            return
        visual_event = dict(bucket.get("event", {}) or {})
        payload = dict(visual_event.get("data", {}) or {})
        self._assistant_delta_seq += 1
        payload["text"] = text
        payload.setdefault("seq", self._assistant_delta_seq)
        visual_event["data"] = payload
        await self.broadcast({"type": "event", "payload": visual_event})

    async def _flush_assistant_delta_for_payload(self, payload: dict[str, Any]) -> None:
        task_id = str(payload.get("task_id", "") or "").strip()
        if not task_id:
            return
        turn_id = str(
            payload.get("turn_id")
            or payload.get("canonical_turn_id")
            or payload.get("execution_turn_id")
            or payload.get("runtime_task_id")
            or ""
        ).strip()
        keys = [
            key for key in list(self._assistant_delta_buffers)
            if key[0] == task_id and (not turn_id or key[1] == turn_id)
        ]
        for key in keys:
            await self._flush_assistant_delta(key)

    async def _queue_assistant_delta_visual_event(self, visual_event: dict[str, Any]) -> None:
        payload = dict(visual_event.get("data", {}) or {})
        text = str(payload.get("text", "") or "")
        if not text:
            return
        delta_type = str(visual_event.get("type", "") or "assistant_delta").strip() or "assistant_delta"
        key = self._assistant_delta_key(payload, delta_type=delta_type)
        if key is None:
            await self.broadcast({"type": "event", "payload": visual_event})
            return
        bucket = self._assistant_delta_buffers.setdefault(
            key,
            {
                "event": {
                    **visual_event,
                    "data": {
                        **payload,
                        "text": "",
                    },
                },
                "text": "",
            },
        )
        bucket["event"] = {
            **visual_event,
            "data": {
                **payload,
                "text": "",
            },
        }
        bucket["text"] = f"{bucket.get('text', '')}{text}"
        if "\n" in text:
            await self._flush_assistant_delta(key)
            return
        pending = self._assistant_delta_flush_tasks.get(key)
        if pending is None or pending.done():
            self._assistant_delta_flush_tasks[key] = asyncio.create_task(
                self._delayed_flush_assistant_delta(key),
                name=f"assistant-delta-flush:{key[0]}:{key[1]}:{key[2]}",
            )

    async def _broadcast_runtime_visual_event(self, visual_event: dict[str, Any]) -> None:
        payload = dict(visual_event.get("data", {}) or {})
        runtime_type = str(visual_event.get("type", "") or "").strip()
        if runtime_type in {"assistant_delta", "thinking_delta"}:
            await self._queue_assistant_delta_visual_event(visual_event)
            return
        if runtime_type in {"turn_completed", "turn_failed", "checkpoint_saved"}:
            await self._flush_assistant_delta_for_payload(payload)
        await self.broadcast({"type": "event", "payload": visual_event})

    async def on_opc_event(
        self,
        event: Any,
        *,
        runtime_engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        """EventBus subscriber. Translates OPC events and broadcasts."""
        engine = runtime_engine or self.engine
        pid = self._normalize_project_id(project_id or getattr(engine, "project_id", None))
        if event.event_type == "runtime_event":
            runtime_payload = dict(event.payload or {})
            runtime_type = str(runtime_payload.get("type", "") or "").strip()
            if runtime_type in _RUNTIME_TASK_VISIBILITY_EVENT_TYPES:
                await self._materialize_runtime_task_visibility(
                    runtime_payload,
                    engine=engine,
                    project_id=pid,
                )

        visual_events = self.event_adapter.translate(event)
        for ve in visual_events:
            ve = dict(ve)
            ve["project_id"] = pid
            ve_data = dict(ve.get("data", {}) or {})
            ve_data.setdefault("project_id", pid)
            ve["data"] = ve_data
            if ve.get("type") == "board_task_status_changed":
                # Kanban status update: broadcast as dedicated message type
                payload = dict(ve.get("data", {}) or {})
                raw_runtime_task_id = str(payload.get("task_id", "") or "").strip()
                mapped_task_id = await self._ui_task_id_for_runtime_task_id(raw_runtime_task_id, engine=engine)
                if mapped_task_id:
                    payload["task_id"] = mapped_task_id
                    if mapped_task_id != raw_runtime_task_id:
                        payload.setdefault("work_item_id", mapped_task_id)
                _add_execution_turn_aliases(payload, raw_runtime_task_id)
                await self.broadcast({
                    "type": "board_task_status_changed",
                    "payload": payload,
                })
            elif ve.get("type") == "execution_mode_resolved":
                await self.broadcast({
                    "type": "execution_mode_resolved",
                    "payload": ve.get("data", {}),
                })
            elif ve.get("type") == "agent_runtime_update":
                payload = dict(ve.get("data", {}) or {})
                raw_runtime_task_id = str(payload.get("task_id", "") or "").strip()
                mapped_task_id = await self._ui_task_id_for_runtime_task_id(raw_runtime_task_id, engine=engine)
                if mapped_task_id:
                    payload["task_id"] = mapped_task_id
                    if mapped_task_id != raw_runtime_task_id:
                        payload.setdefault("work_item_id", mapped_task_id)
                _add_execution_turn_aliases(payload, raw_runtime_task_id)
                await self.broadcast({
                    "type": "agent_runtime_update",
                    "payload": payload,
                })
            elif ve.get("type") == "child_session_created":
                payload = dict(ve.get("data", {}) or {})
                if event.event_type == "child_session_created" and self._store_is_ready(engine.store):
                    task_id = str(payload.get("task_id", "") or "").strip()
                    if task_id:
                        try:
                            created_task = await engine.store.get_task(task_id)
                        except Exception:
                            created_task = None
                        if created_task is not None:
                            payload.setdefault(
                                "selected_execution_agent",
                                self._resolve_task_selected_execution_agent(created_task),
                            )
                            # Enrich with role/employee metadata from task
                            meta = created_task.metadata or {}
                            identity_payload = (
                                {}
                                if self._runtime_payload_is_task_mode(dict(meta or {}))
                                else work_item_identity_payload(
                                    projection_id=str(meta.get("work_item_projection_id", "") or "").strip(),
                                    turn_type=str(meta.get("work_item_turn_type", "") or "").strip(),
                                )
                            )
                            for _key, _value in identity_payload.items():
                                if _key not in payload and _value:
                                    payload[_key] = _value
                            for _key in (
                                "work_item_role_id", "work_item_role_name",
                                "employee_assignment", "origin_task_id",
                            ):
                                if _key not in payload and meta.get(_key):
                                    payload[_key] = meta[_key]
                            role_id = str(
                                payload.get("work_item_role_id")
                                or getattr(created_task, "assigned_to", "")
                                or ""
                            ).strip()
                            if role_id and not str(payload.get("work_item_role_name", "") or "").strip():
                                payload["work_item_role_name"] = self._resolve_work_item_role_name(
                                    role_id,
                                    meta,
                                    engine=engine,
                                )
                    _add_execution_turn_aliases(payload, task_id)
                await self.broadcast({
                    "type": "child_session_created",
                    "payload": payload,
                })
            else:
                if event.event_type == "runtime_event":
                    ve = await self._canonicalize_runtime_visual_event(ve, engine=engine)
                    await self._broadcast_runtime_visual_event(ve)
                else:
                    await self.broadcast({"type": "event", "payload": ve})

        if event.event_type == "runtime_event":
            runtime_payload = dict(event.payload or {})
            if str(runtime_payload.get("type", "") or "").strip() == "worker_notification":
                await self._handle_worker_notification(runtime_payload, engine=engine, project_id=pid)
            await self._handle_runtime_event_progress(event.payload or {}, engine=engine, project_id=pid)

        # Create chat_store channel for child sessions (so messages can render)
        if event.event_type == "child_session_created":
            p = event.payload or {}
            task_id = p.get("task_id", "")
            title = p.get("title", "Sub-task")
            if task_id:
                channel_id = f"session:{task_id}"
                exec_mode = self._normalize_session_exec_mode(self._exec_mode)
                company_profile = self._normalize_session_company_profile(self._company_profile)
                org_id = ""
                preferred_agent = self._task_preferred_agent
                parent_session_id = str(p.get("parent_session_id") or "").strip()
                parent_task_id = (
                    str(self._session_to_task.get(parent_session_id) or "").strip()
                    or parent_session_id
                )
                if parent_task_id and self._store_is_ready(engine.store):
                    try:
                        parent_task = await engine.store.get_task(parent_task_id)
                    except Exception:
                        parent_task = None
                    exec_mode, company_profile = self._resolve_task_session_config(parent_task)
                    org_id = self._resolve_task_org_id(parent_task)
                    preferred_agent = self._resolve_task_preferred_agent(parent_task)
                work_item_identity: dict[str, Any] = {}
                if self._store_is_ready(engine.store):
                    try:
                        created_task = await engine.store.get_task(task_id)
                    except Exception:
                        created_task = None
                    if created_task is not None:
                        created_meta = dict(getattr(created_task, "metadata", {}) or {})
                        role_id = str(created_meta.get("work_item_role_id", "") or getattr(created_task, "assigned_to", "") or "").strip()
                        role_name = str(created_meta.get("work_item_role_name", "") or "").strip()
                        if not role_name and role_id:
                            role_name = self._resolve_work_item_role_name(
                                role_id,
                                created_meta,
                                engine=engine,
                            )
                        if self._runtime_payload_is_task_mode(created_meta):
                            work_item_identity = {
                                "employee_assignment": created_meta.get("employee_assignment"),
                                "origin_task_id": created_meta.get("origin_task_id") or parent_task_id or task_id,
                                "selected_execution_agent": self._resolve_task_selected_execution_agent(created_task),
                            }
                        else:
                            projection_id = work_item_projection_id_from_metadata(created_meta)
                            turn_type = work_item_turn_type_from_metadata(created_meta, fallback="")
                            work_item_identity = {
                                **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type),
                                "work_item_role_id": role_id,
                                "work_item_role_name": role_name,
                                "employee_assignment": created_meta.get("employee_assignment"),
                                "origin_task_id": created_meta.get("origin_task_id") or parent_task_id or task_id,
                                "selected_execution_agent": self._resolve_task_selected_execution_agent(created_task),
                            }
                        preferred_agent = self._resolve_task_preferred_agent(created_task)
                        org_id = self._resolve_task_org_id(created_task) or org_id
                await self.chat_store.create_session_channel(task_id, title, project_id=pid)
                # Display counter already incremented by task_created event — use map lookup
                display_num = self.event_adapter.get_task_display_num(task_id)
                display_id = f"OPC-{display_num}"
                execution_aliases = _add_execution_turn_aliases({}, task_id)
                # Child company runtime sessions remain in the session sidebar but do not
                # become company-mode kanban cards.
                if not parent_session_id:
                    await self.broadcast({"type": "board_task_created", "payload": {
                        "project_id": pid,
                        "task_id": task_id,
                        **execution_aliases,
                        "display_id": display_id,
                        "board_id": pid,
                        "title": title,
                        # Engine event agent_id is opc_role_id; resolve to UI agent_id
                        "assignee_ids": [self.event_adapter._resolve_role_to_agent(p["agent_id"])] if p.get("agent_id") else [],
                        **work_item_identity,
                    }})
                await self.broadcast({"type": "session_created", "payload": {
                    "project_id": pid,
                    "task_id": task_id,
                    **execution_aliases,
                    "channel_id": channel_id,
                    "session_id": p.get("session_id"),
                    "parent_session_id": p.get("parent_session_id"),
                    "origin_task_id": work_item_identity.get("origin_task_id") or parent_task_id or task_id,
                    "exec_mode": exec_mode,
                    "company_profile": company_profile,
                    "org_id": org_id,
                    "preferred_agent": preferred_agent,
                    "title": title,
                    "status": "pending",
                    "created_at": time.time(),
                    **work_item_identity,
                }})

        # Update agent_store status for persistence (match by opc_role_id)
        if event.event_type == "agent_status_changed":
            await self.agent_store.update_status_by_role(
                event.payload.get("role_id", ""),
                event.payload.get("status", ""),
            )

        # Mirror agent messages to chat
        if event.event_type == "agent_message_sent":
            await self._mirror_agent_message(event, engine=engine, project_id=pid)
            # Tell every connected client that the comms tree just changed
            # so the CommsPanel can refetch immediately instead of waiting
            # for its 5s polling tick. Cheap fire-and-forget broadcast.
            try:
                await self.broadcast({
                    "type": "comms_state_dirty",
                    "payload": {
                        "project_id": pid,
                        "from": (event.payload or {}).get("from"),
                        "to": (event.payload or {}).get("to"),
                    },
                })
            except Exception:
                logger.opt(exception=True).debug("comms_state_dirty broadcast failed")

        # Mirror escalations to chat
        if event.event_type == "escalation_created":
            await self._mirror_escalation(event, engine=engine, project_id=pid)
        if event.event_type in {"escalation_resolved", "escalation_timeout"}:
            await self._mark_escalation_event_checkpoint_terminal(event, project_id=pid)

        if event.event_type == "task_status_changed":
            payload = event.payload or {}
            task_id = str(payload.get("task_id", "") or "").strip()
            status = str(payload.get("status", "") or "").strip().lower()
            if task_id and status in {
                "done",
                "failed",
                "cancelled",
                "blocked",
                "awaiting_manager_review",
                "awaiting_human",
                "awaiting_review",
                "awaiting_peer",
            }:
                await self._sync_task_transcript_messages(task_id, engine=engine)
                if self._store_is_ready(engine.store):
                    task = await engine.store.get_task(task_id)
                    if task is not None:
                        for parent_task_id in self._related_parent_task_ids(task):
                            await self._sync_task_transcript_messages(parent_task_id, engine=engine)

    @staticmethod
    def _approval_group_key(message: str) -> str:
        raw = str(message or "").strip()
        if not raw:
            return ""
        normalized = WSHandler._semantic_permission_group_key(raw)
        if normalized:
            return normalized
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("allowlist target:"):
                return stripped.split(":", 1)[1].strip()
        match = re.search(r"Approve\s+([a-z_]+)\s+'([^']+)'\?", raw, re.IGNORECASE)
        if match:
            return f"{match.group(1).lower()}:{match.group(2).strip()}"
        return ""

    @staticmethod
    def _semantic_permission_group_key(message: str) -> str:
        raw = str(message or "")
        tool_match = re.search(r"Approve\s+tool\s+'([^']+)'\?", raw, re.IGNORECASE)
        tool_name = tool_match.group(1).strip().casefold() if tool_match else ""
        if tool_name != "shell_exec":
            return ""

        command_match = re.search(
            r"command=(.*?)(?:\nAllowlist target:|\Z)",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        command = (command_match.group(1) if command_match else raw).strip()
        command_family = ""
        if re.match(r"^(?:python|python3)\b", command, re.IGNORECASE):
            command_family = "python"
        elif re.match(r"^node\b", command, re.IGNORECASE):
            command_family = "node"
        if not command_family:
            return ""

        domains = sorted({
            match.group(1).casefold()
            for match in re.finditer(r"https?://([^/\s'\"<>]+)", command)
        })
        domain_key = ",".join(domains) if domains else "no-domain"
        return f"tool:shell_exec/{command_family}:domain:{domain_key}"

    def _resolve_related_pending_escalations(
        self,
        record: dict[str, Any],
        reply: str,
    ) -> list[str]:
        normalized_reply = str(reply or "").strip().lower()
        if normalized_reply not in {"approve_session", "always_project", "always_global"}:
            return []
        group_key = str(record.get("approval_group_key") or "").strip()
        if not group_key:
            return []
        current_escalation_id = str(record.get("escalation_id") or "").strip()
        task_id = str(record.get("task_id") or "").strip()
        project_id = str(record.get("project_id") or "").strip()
        resolved_ids: list[str] = []
        for escalation_id in list(self._pending_escalation_order):
            if escalation_id == current_escalation_id:
                continue
            candidate = self._pending_escalations.get(escalation_id)
            if not candidate:
                continue
            future = candidate.get("future")
            if future is None or future.done():
                continue
            candidate_project_id = str(candidate.get("project_id") or "").strip()
            if project_id and candidate_project_id and candidate_project_id != project_id:
                continue
            if str(candidate.get("approval_group_key") or "").strip() != group_key:
                continue
            if normalized_reply == "approve_session" and str(candidate.get("task_id") or "").strip() != task_id:
                continue
            future.set_result(normalized_reply)
            resolved_ids.append(escalation_id)
        return resolved_ids

    @staticmethod
    def _task_mode_permission_prompt(message: str, current_turn_title: str = "") -> str:
        raw = str(message or "").strip()
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        cleaned: list[str] = []
        for line in lines:
            if line.startswith("[") and "]" in line:
                line = line.split("]", 1)[1].strip()
            if line.lower().startswith("task:"):
                continue
            cleaned.append(line)
        title = "Permission required"
        if current_turn_title:
            title = f"Permission required: {current_turn_title[:80]}"
        return "\n".join([title, *cleaned]).strip()

    async def _resolve_escalation_session_task_id(
        self,
        task_id: str | None,
        *,
        engine: Any | None = None,
    ) -> str | None:
        source_task_id = str(task_id or "").strip()
        if not source_task_id:
            return None

        runtime_engine = engine or self.engine
        store = getattr(runtime_engine, "store", None)
        get_task = getattr(store, "get_task", None)
        if callable(get_task):
            try:
                task = await get_task(source_task_id)
            except Exception as e:
                logger.warning(f"Failed to resolve escalation task mapping for {source_task_id}: {e}")
                task = None
            if task is not None:
                try:
                    identity_index = await load_company_runtime_identity_index(
                        store,
                        self._normalize_project_id(
                            getattr(task, "project_id", None)
                            or getattr(runtime_engine, "project_id", None)
                        ),
                    )
                    runtime_identity = identity_index.resolve(task_id=source_task_id)
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to resolve durable company identity for escalation"
                    )
                    runtime_identity = None
                if runtime_identity is not None:
                    # Escalations are a control decision.  Route only through
                    # the durable runtime identity; process-local progress maps
                    # are not evidence that a work-item Task is the UI parent.
                    return runtime_identity.ui_anchor_task_id or None
                if is_company_runtime_task(task):
                    return None
                internal_turn_target = self._company_internal_turn_escalation_target(task)
                if internal_turn_target is not None:
                    return internal_turn_target or None
                ui_task_id = self._ui_task_id_for_task(task)
                if ui_task_id:
                    return ui_task_id
                metadata = dict(getattr(task, "metadata", {}) or {})
                origin_task_id = str(metadata.get("origin_task_id") or "").strip()
                if origin_task_id:
                    return origin_task_id

        return source_task_id

    def _company_internal_turn_escalation_target(self, task: Any | None) -> str | None:
        """Visible routing target for escalations raised by internal
        company-mode scheduling turns.

        Review/report turn work items get composite ids (``review::<wid>::vN``),
        so their runtime tasks carry session ids shaped like
        ``<root_session>:review::<wid>::vN``. The UI deliberately hides those
        session channels, so an approval card posted to the turn's own channel
        can never be seen or answered — it silently times out and the work item
        parks on AWAITING_HUMAN.

        Returns None when ``task`` is not such an internal turn (caller keeps
        its normal resolution), the durable origin task when present, or ""
        when no visible channel is known.  Company runtime control routing is
        resolved before this helper; process-local session maps are deliberately
        not consulted here.
        """
        if task is None:
            return None
        session_id = str(getattr(task, "session_id", "") or "").strip()
        root_session_id, sep, suffix = session_id.partition(":")
        if not sep or "::" not in suffix:
            return None
        metadata = dict(getattr(task, "metadata", {}) or {})
        origin_task_id = str(metadata.get("origin_task_id") or "").strip()
        task_id = str(getattr(task, "id", "") or "").strip()
        if origin_task_id and origin_task_id != task_id:
            return origin_task_id
        return ""

    @staticmethod
    def _pending_escalation_matches_task(record: dict[str, Any], task_id: str | None) -> bool:
        task_key = str(task_id or "").strip()
        if not task_key:
            return True
        record_task_id = str(record.get("task_id") or "").strip()
        source_task_id = str(record.get("source_task_id") or "").strip()
        return task_key in {record_task_id, source_task_id}

    @staticmethod
    def _pending_escalation_matches_project(record: dict[str, Any], project_id: str | None) -> bool:
        project_key = str(project_id or "").strip()
        if not project_key:
            return True
        record_project_id = str(record.get("project_id") or "").strip()
        if not record_project_id:
            return True
        return record_project_id == project_key

    def _find_pending_escalation(
        self,
        *,
        task_id: str | None = None,
        escalation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        explicit_escalation_id = str(escalation_id or "").strip()
        if explicit_escalation_id:
            record = self._pending_escalations.get(explicit_escalation_id)
            if not record:
                return None
            future = record.get("future")
            if future is None or future.done():
                return None
            if not self._pending_escalation_matches_project(record, project_id):
                return None
            if not self._pending_escalation_matches_task(record, task_id):
                return None
            return record

        for escalation_id in reversed(self._pending_escalation_order):
            record = self._pending_escalations.get(escalation_id)
            if not record:
                continue
            future = record.get("future")
            if future is None or future.done():
                continue
            if not self._pending_escalation_matches_project(record, project_id):
                continue
            if not self._pending_escalation_matches_task(record, task_id):
                continue
            return record
        return None

    async def _handle_ui_escalation(
        self,
        message: str,
        options: list[dict],
        *,
        project_id: str | None = None,
    ) -> str | None:
        project_key = self._normalize_project_id(project_id)
        option_ids = tuple(str(opt.get("id", "")).strip() for opt in options)
        record = None
        for escalation_id in reversed(self._pending_escalation_order):
            candidate = self._pending_escalations.get(escalation_id)
            if not candidate:
                continue
            future = candidate.get("future")
            if future is None or future.done():
                continue
            if not self._pending_escalation_matches_project(candidate, project_key):
                continue
            candidate_ids = tuple(str(opt.get("id", "")).strip() for opt in candidate.get("options", []))
            if candidate_ids == option_ids and str(candidate.get("message", "")) == message:
                record = candidate
                break
        if record is None:
            record = self._find_pending_escalation(project_id=project_key)
        if record is None:
            return None

        future = record["future"]
        try:
            return await future
        finally:
            escalation_id = str(record.get("escalation_id", ""))
            self._pending_escalations.pop(escalation_id, None)
            self._pending_escalation_order = [
                item for item in self._pending_escalation_order
                if item != escalation_id
            ]

    async def on_kanban_changed(self, *, engine: Any | None = None) -> None:
        """Callback fired by the company-mode lifecycle loop after each work item
        batch completes.  Broadcasts a full collab_sync so the frontend kanban
        reflects newly delegated / updated work items in real-time."""
        runtime_engine = engine or self.engine
        if (
            self._shutting_down
            or not self._store_is_ready(getattr(runtime_engine, "store", None))
            or not self._chat_store_is_ready(self.chat_store)
        ):
            return
        try:
            collab = await build_collab_sync(
                runtime_engine, self.agent_store, self.chat_store,
                self.event_adapter,
                exec_mode=self._exec_mode,
            )
            await self.broadcast({"type": "collab_sync_push", "payload": collab})
        except Exception as exc:
            if self._is_expected_shutdown_error(exc) or self._is_closed_database_error(exc):
                logger.debug(
                    "on_kanban_changed skipped during shutdown/closed store: {}: {}",
                    type(exc).__name__,
                    exc,
                )
                return
            # Surface the exception type + message inline AND force the full
            # traceback into the sink. loguru silently drops ``exc_info=True``
            # unless the sink was configured with ``backtrace=True``; using
            # ``.opt(exception=True)`` reliably prints the stack regardless
            # of sink configuration, which is what we want when diagnosing a
            # transient race between the company-mode loop and the UI push.
            logger.opt(exception=True).warning(
                "on_kanban_changed collab_sync broadcast failed: {}: {}",
                type(exc).__name__,
                exc,
            )

    async def on_progress(self, text: str, **kw: Any) -> None:
        """engine.on_progress callback. Routes progress to session channel.

        ``task_id`` is now supplied explicitly by the caller (NativeRuntimeV2,
        company_mode, or the engine scoped wrapper).  No global fallback.
        Optional ``agent_role_id`` / ``agent_name`` carry agent identity for
        dual-routed messages so the parent chat can display per-agent identity.
        """
        import time as _time

        runtime_engine = kw.pop("_runtime_engine", None) or self.engine
        pid = self._normalize_project_id(kw.pop("_project_id", None) or getattr(runtime_engine, "project_id", None))
        raw_task_id = str(kw.get("task_id", "") or "").strip() or None
        task_id = await self._ui_task_id_for_runtime_task_id(raw_task_id, engine=runtime_engine)
        task_id = task_id or None
        agent_role_id: str = kw.get("agent_role_id", "")
        agent_name: str = kw.get("agent_name", "")
        visual_events = self.event_adapter.parse_progress(
            text,
            task_id=task_id or raw_task_id,
            agent_role_id=agent_role_id,
        )
        for ve in visual_events:
            ve = dict(ve)
            ve_data = dict(ve.get("data", {}) or {})
            ve_data.setdefault("project_id", pid)
            ve["data"] = ve_data
            ve["project_id"] = pid
            await self.broadcast({"type": "event", "payload": ve})

        # Route to session channel if task_id is known, else activity
        target_channel = f"session:{task_id}" if task_id else f"activity:{pid}"

        # ── Resolve role label for cleaner display ─────────────────
        _role_label = (agent_name or agent_role_id or "").strip()
        if _role_label:
            _role_label = _role_label.replace("_", " ").title()

        # ── Broadcast progress entry for tool call history ──────────
        if task_id:
            entry = self._parse_progress_entry(text)
            if entry:
                entry["timestamp"] = _time.time()
                # Enrich with role name for frontend display
                if _role_label and not entry.get("role_name"):
                    entry["role_name"] = _role_label
                if entry.get("is_company_runtime"):
                    entry.update(
                        work_item_identity_payload(
                            projection_id=entry.get("work_item_projection_id") or "",
                            turn_type=entry.get("work_item_turn_type") or "",
                        )
                    )
                    if not entry.get("work_item_projection_title"):
                        entry["work_item_projection_title"] = _role_label or None
                _add_execution_turn_aliases(entry, raw_task_id or task_id)
                # Buffer BEFORE broadcasting: broadcast awaits can interleave a
                # session_detail read, and any entry a client has already seen
                # must be visible in buffer∪DB or the snapshot will erase it.
                self._progress_buffer.setdefault(task_id, []).append(entry)
                self._progress_project_ids[task_id] = pid
                await self.broadcast({"type": "session_progress", "payload": {
                    "project_id": pid,
                    "task_id": task_id,
                    **_add_execution_turn_aliases({}, raw_task_id or task_id),
                    "entry": entry,
                }})
                # ── Company runtime dual-route: broadcast to primary session ──
                origin = self._active_runtime_children.get(task_id)
                if entry.get("is_company_runtime") and origin:
                    await self.broadcast({"type": "work_item_progress", "payload": {
                        "project_id": pid,
                        "task_id": origin,
                        **_add_execution_turn_aliases({}, raw_task_id or task_id),
                        "entry": entry,
                    }})

                # ── Persist at threshold (re-read: a concurrent flush during the
                # broadcast awaits may have popped the buffer) ──
                if len(self._progress_buffer.get(task_id, [])) >= self._PROGRESS_FLUSH_THRESHOLD:
                    await self._flush_progress(task_id, project_id=pid)

        is_work_item_event = text.startswith("[Company:")

        # Clean up [Company:UUID] prefix for display — replace with role name
        display_text = text
        if is_work_item_event and _role_label:
            bracket_end = text.find("]")
            if bracket_end > 9:
                raw_projection = text[9:bracket_end]
                # If the projection looks like a UUID, replace it with the role label.
                if len(raw_projection) > 12 and raw_projection.replace("-", "").replace("_", "").isalnum():
                    display_text = f"[{_role_label}] {text[bracket_end + 1:].strip()}"

        if self._should_store_progress_message(text):
            msg_meta: dict[str, Any] = {}
            if task_id:
                msg_meta["task_id"] = task_id
            if is_work_item_event:
                msg_meta["is_work_item_event"] = True
            msg = await self.chat_store.insert_message(
                channel_id=target_channel,
                sender="system",
                sender_name="OPC",
                content=display_text,
                metadata=msg_meta or None,
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})

            # Dual-route: also insert into parent session channel so the
            # primary chat view shows agent output from child tasks.
            if task_id:
                parent_task_id = self._active_runtime_children.get(task_id)
                if parent_task_id and parent_task_id != task_id:
                    parent_channel = f"session:{parent_task_id}"
                    # Resolve agent identity for the forwarded message
                    fwd_sender = "system"
                    fwd_sender_name = "OPC"
                    if agent_role_id:
                        fwd_sender = self.event_adapter._resolve_role_to_agent(agent_role_id)
                        fwd_sender_name = agent_name or agent_role_id.replace("_", " ").title()
                    fwd_meta: dict[str, Any] = {
                        "task_id": task_id,
                        "forwarded_from": task_id,
                        # _should_store_progress_message already filters out noisy
                        # external streams/heartbeats. Forwarded stored messages are
                        # high-signal role launch/status records and should remain
                        # visible in the primary session summary.
                        "detail_visibility": "summary",
                    }
                    if is_work_item_event:
                        fwd_meta["is_work_item_event"] = True
                    if _role_label:
                        fwd_meta["role_name"] = _role_label
                    parent_msg = await self.chat_store.insert_message(
                        channel_id=parent_channel,
                        sender=fwd_sender,
                        sender_name=fwd_sender_name,
                        content=display_text,
                        metadata=fwd_meta,
                        project_id=pid,
                    )
                    await self.broadcast({"type": "session_message", "payload": parent_msg})

    async def _flush_progress(self, task_id: str, *, project_id: str | None = None) -> None:
        """Write buffered progress entries to ChatStore for a single task.

        Atomically pops the buffer so concurrent on_progress calls for the same
        task_id will create a fresh buffer.  Safe to call multiple times — if
        the buffer is empty (already flushed), returns immediately.
        """
        entries = self._progress_buffer.pop(task_id, [])
        if not entries:
            self._progress_project_ids.pop(task_id, None)
            return
        pid = self._normalize_project_id(project_id or self._progress_project_ids.pop(task_id, None) or self.engine.project_id)
        try:
            await self.chat_store.append_progress(task_id, entries, project_id=pid)
        except Exception:
            logger.debug(f"Failed to flush progress for task {task_id}")

    async def flush_all_progress(self) -> None:
        """Flush all buffered progress to DB. Called on graceful server shutdown."""
        task_ids = list(self._progress_buffer.keys())
        for tid in task_ids:
            await self._flush_progress(tid)

    def _ensure_progress_flush_loop(self) -> None:
        """Start the background periodic-flush coroutine if not already running.

        Called lazily on the first WS client connect so the loop exists for
        as long as the UI is in use. Flushes any task whose buffer has had
        entries sitting in RAM longer than ``_PROGRESS_FLUSH_INTERVAL_SEC``,
        so even sparsely-emitting tasks show complete Activity timelines
        without a 10-entry wait.
        """
        if self._progress_flush_task and not self._progress_flush_task.done():
            return
        self._progress_flush_task = asyncio.create_task(self._periodic_flush_loop())

    async def _periodic_flush_loop(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(self._PROGRESS_FLUSH_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            if self._shutting_down:
                break
            # Snapshot buffer keys under no-lock read; _flush_progress handles
            # the pop atomically so racing appends don't lose entries.
            pending = [tid for tid, buf in self._progress_buffer.items() if buf]
            for tid in pending:
                try:
                    await self._flush_progress(tid)
                except Exception:
                    logger.debug(
                        "Periodic progress flush error for task %s", tid,
                    )

    @staticmethod
    def _parse_progress_entry(text: str) -> dict[str, Any] | None:
        """Parse on_progress text into a ProgressEntry dict, or None to skip."""
        # [Tool: file_read] {"path": "src/api.ts"}
        if text.startswith("[Tool:"):
            bracket_end = text.find("]")
            if bracket_end > 0:
                tool_name = text[7:bracket_end].strip()
                detail = text[bracket_end + 1:].lstrip()
                return {"type": "tool_call", "summary": tool_name, "detail": detail}
            return {"type": "tool_call", "summary": text[:60]}

        # [Autonomy] tool:web_search -> auto_approve (risk=low)
        if text.startswith("[Autonomy]"):
            return {"type": "autonomy", "summary": text[11:].strip()[:100]}

        # [Delegating to claude-code] task=...
        if text.startswith("[Delegating"):
            bracket_end = text.find("]")
            target = text[15:bracket_end] if bracket_end > 15 else "agent"
            return {
                "type": "handoff",
                "summary": f"Delegating to {target}",
                "detail": text,
            }

        # [External:agent:stdout] ... / [External status] ... / failures
        if text.startswith("[External"):
            bracket_end = text.find("]")
            header = text[1:bracket_end] if bracket_end > 1 else "External"
            detail = text[bracket_end + 1:].strip() if bracket_end > 0 else text
            summary = detail[:80] if detail else header[:80]

            if header.startswith("External:"):
                parts = header.split(":")
                agent = parts[1] if len(parts) > 1 else "external"
                stream = parts[2] if len(parts) > 2 else ""
                if stream == "thinking":
                    thinking_summary = detail[:120] if detail else f"{agent} thinking"
                    if len(detail) > 120:
                        thinking_summary = thinking_summary.rstrip() + "..."
                    return {
                        "type": "thinking",
                        "summary": thinking_summary,
                        "detail": detail or None,
                    }
                if stream == "tool":
                    first_line = next((line.strip() for line in detail.splitlines() if line.strip()), "")
                    if first_line.startswith("$ "):
                        first_line = first_line[2:]
                    tool_summary = first_line[:120] if first_line else f"{agent} tool"
                    return {
                        "type": "tool_call",
                        "summary": tool_summary,
                        "detail": detail or None,
                    }
                label = f"{agent} {stream}".strip()
                if label:
                    summary = label
            elif header == "External status" and detail:
                summary = detail[:80]

            return {
                "type": "status_change",
                "summary": summary,
                "detail": detail or text,
            }

        # [CapabilityRecovery]
        if text.startswith("[CapabilityRecovery]"):
            return {"type": "status_change", "summary": "Capability recovery"}

        # [Company] (no projection) — global company runtime event (e.g. deadlock)
        if text.startswith("[Company]"):
            action = text[10:].strip()
            action_lower = action.lower()
            entry_type = "gate_result"
            if "deadlock" in action_lower:
                entry_type = "deadlock"
            elif "failed" in action_lower:
                entry_type = "work_item_failed"
            return {
                "type": entry_type,
                "summary": f"Company runtime: {action[:80]}",
                "detail": action[:200] if action else None,
                **work_item_identity_payload(projection_id="company_runtime", turn_type=""),
                "work_item_projection_title": "Company Runtime",
                "is_company_runtime": True,
            }

        # [Company:projection] ... — classify by specific action
        if text.startswith("[Company:"):
            bracket_end = text.find("]")
            projection_id = text[9:bracket_end] if bracket_end > 9 else "work_item"
            action = text[bracket_end + 2:].strip() if bracket_end > 0 else ""
            action_lower = action.lower()

            entry_type = "gate_result"
            if "starting" in action_lower or "started" in action_lower:
                entry_type = "work_item_started"
            elif "gate passed" in action_lower or "approved" in action_lower or "completed" in action_lower:
                entry_type = "gate_approved"
            elif "rejected" in action_lower or "reworking" in action_lower:
                entry_type = "gate_rejected"
            elif "awaiting manager review" in action_lower:
                entry_type = "awaiting_manager_review"
            elif "awaiting user" in action_lower or "awaiting human review" in action_lower or "awaiting review" in action_lower:
                entry_type = "awaiting_human"
            elif "awaiting peer" in action_lower:
                entry_type = "awaiting_peer"
            elif "failed" in action_lower:
                entry_type = "work_item_failed"
            elif "deadlock" in action_lower:
                entry_type = "deadlock"

            # Use projection name as title, action text as detail.
            is_uuid_like = len(projection_id) > 12 and projection_id.replace("-", "").replace("_", "").isalnum()
            projection_title = projection_id if is_uuid_like else projection_id.replace("_", " ").replace("-", " ").title()
            return {
                "type": entry_type,
                "summary": f"{projection_title}: {action[:80]}",
                "detail": action[:200] if action else None,
                **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                "work_item_projection_title": projection_title,
                "is_company_runtime": True,
            }

        return None

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                return None
            return int(numeric)
        try:
            numeric = float(str(value))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return int(numeric)

    @staticmethod
    def _context_usage_metrics(payload: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
        context_tokens = WSHandler._coerce_int(payload.get("context_tokens", payload.get("token_count")))
        context_window = WSHandler._coerce_int(payload.get("context_window"))
        remaining_pct = WSHandler._coerce_int(payload.get("context_remaining_pct"))

        if remaining_pct is not None:
            remaining_pct = max(0, min(remaining_pct, 100))

        if context_window is not None and context_window > 0:
            if context_tokens is not None:
                used_tokens = max(0, min(context_tokens, context_window))
                used_pct = int(round((used_tokens / context_window) * 100))
                return used_tokens, max(0, min(used_pct, 100)), context_window
            if remaining_pct is not None:
                used_pct = 100 - remaining_pct
                used_tokens = int(round((used_pct / 100) * context_window))
                return used_tokens, used_pct, context_window

        if remaining_pct is not None:
            return context_tokens, 100 - remaining_pct, context_window

        return context_tokens, None, context_window

    @staticmethod
    def _context_usage_compact_label(payload: dict[str, Any]) -> str | None:
        used_tokens, used_pct, context_window = WSHandler._context_usage_metrics(payload)
        if used_pct is not None:
            return f"{used_pct}% used"
        if used_tokens is not None and context_window is not None and context_window > 0:
            return f"{used_tokens}/{context_window} tokens"
        if used_tokens is not None:
            return f"{used_tokens} tokens"
        return None

    @staticmethod
    def _context_usage_detail(payload: dict[str, Any]) -> str | None:
        used_tokens, used_pct, context_window = WSHandler._context_usage_metrics(payload)
        parts: list[str] = []
        if used_tokens is not None:
            if context_window is not None and context_window > 0:
                parts.append(f"{used_tokens}/{context_window} tokens")
            else:
                parts.append(f"{used_tokens} tokens")
        if used_pct is not None:
            parts.append(f"{used_pct}% used")
        return " | ".join(parts) or None

    @staticmethod
    def _humanize_role_label(role_id: str) -> str:
        normalized = str(role_id or "").strip()
        if not normalized:
            return ""
        if "_" not in normalized and "-" not in normalized and normalized.isalpha() and len(normalized) <= 4:
            return normalized.upper()
        return normalized.replace("_", " ").replace("-", " ").title()

    def _resolve_work_item_role_name(
        self,
        role_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        engine: Any | None = None,
    ) -> str:
        explicit = str((metadata or {}).get("work_item_role_name", "") or "").strip()
        if explicit:
            return explicit

        rid = str(role_id or "").strip()
        if not rid:
            return ""

        runtime_engine = engine or self.engine
        org_engine = getattr(runtime_engine, "org_engine", None)
        get_agent = getattr(org_engine, "get_agent", None)
        if callable(get_agent):
            try:
                agent = get_agent(rid)
            except Exception:
                agent = None
            if isinstance(agent, dict):
                name = agent.get("name")
            else:
                name = getattr(agent, "name", "")
            if isinstance(name, str) and name.strip():
                return name.strip()

        return self._humanize_role_label(rid)

    def _enrich_runtime_progress_payload(self, payload: dict[str, Any], *, engine: Any | None = None) -> dict[str, Any]:
        enriched = dict(payload or {})
        if str(enriched.get("work_item_role_name", "") or "").strip():
            return enriched

        role_id = str(
            enriched.get("role_id")
            or enriched.get("agent_role_id")
            or enriched.get("work_item_role_id")
            or ""
        ).strip()
        role_name = self._resolve_work_item_role_name(role_id, engine=engine)
        if role_name:
            enriched["work_item_role_name"] = role_name
        return enriched

    @staticmethod
    def _runtime_payload_is_task_mode(payload: dict[str, Any]) -> bool:
        execution_mode = str(payload.get("execution_mode", "") or "").strip().lower()
        if execution_mode == "company_mode":
            return False
        if execution_mode in {"task_mode", "task", "project_mode", "project"}:
            return True
        projection_id = str(payload.get("work_item_projection_id", "") or "").strip()
        if projection_id and projection_id != "task_mode_execution":
            return False
        if str(payload.get("company_profile", "") or "").strip():
            return False
        mode = str(payload.get("mode", "") or "").strip().lower()
        runtime_kind = str(payload.get("runtime_kind", "") or "").strip()
        task_mode_contract = str(payload.get("task_mode_contract", "") or "").strip()
        return (
            mode == "task"
            or runtime_kind == "task_mode_agent_turn"
            or task_mode_contract == "single_full_capability_main_agent"
            or projection_id == "task_mode_execution"
        )

    @staticmethod
    def _runtime_event_to_progress_entry(payload: dict[str, Any]) -> dict[str, Any] | None:
        runtime_type = str(payload.get("type", "") or "").strip()
        if not runtime_type:
            return None
        is_task_mode = WSHandler._runtime_payload_is_task_mode(payload)
        if is_task_mode:
            if runtime_type in _TASK_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES:
                return None
            if runtime_type not in _TASK_MODE_VISIBLE_RUNTIME_PROGRESS_TYPES:
                return None
        elif runtime_type in _COMPANY_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES:
            return None

        summary = runtime_type.replace("_", " ").title()
        detail = ""
        entry_type = "status_change"

        if runtime_type == "turn_started":
            entry_type = "work_item_started"
            summary = f"Turn {payload.get('iteration', '?')} started"
        elif runtime_type == "assistant_delta":
            # Company mode only (task mode is filtered out above and already
            # streams assistant text as the draft reply): surface the role's
            # narration and final reply in its progress transcript, matching
            # what external agents get via [External:*:result] parsing.
            entry_type = "assistant"
            detail = str(payload.get("text", "") or "")
            if not detail.strip():
                return None
            preview = " ".join(detail.split())
            summary = preview[:120].rstrip() + ("..." if len(preview) > 120 else "")
        elif runtime_type == "member_idle":
            return None
        elif runtime_type == "thinking_delta":
            entry_type = "thinking"
            # Keep the raw fragment: streaming deltas are token-sized, so
            # stripping them destroys the whitespace between tokens once the
            # fragments are merged back into one entry.
            detail = str(payload.get("text", "") or "")
            if not detail.strip():
                return None
            preview = " ".join(detail.split())
            summary = preview[:120].rstrip() + ("..." if len(preview) > 120 else "")
        elif runtime_type == "member_claimed_work_item":
            entry_type = "work_item_started"
            priority = str(payload.get("message_priority", "") or "").strip().lower()
            if priority == "manager":
                summary = "Work item resumed"
                detail = "Claimed from manager queue."
            else:
                summary = "Work item started"
                if priority:
                    detail = f"Claimed from {priority.replace('_', ' ')} queue."
        elif runtime_type == "tool_started":
            entry_type = "tool_call"
            summary = str(payload.get("tool_name", "") or "tool")
            if payload.get("arguments"):
                try:
                    detail = json.dumps(payload.get("arguments", {}), ensure_ascii=False, default=str)
                except TypeError:
                    detail = str(payload.get("arguments"))
        elif runtime_type == "tool_progress":
            entry_type = "tool_call"
            summary = str(payload.get("tool_name", "") or "tool")
            detail = str(payload.get("text", "") or payload.get("message", "") or "").strip()
        elif runtime_type == "tool_completed":
            entry_type = "tool_call"
            summary = str(payload.get("tool_name", "") or "tool")
            detail = str(payload.get("result_summary", "") or payload.get("result_preview", "") or "").strip()
        elif runtime_type == "status_snapshot":
            entry_type = "status_change"
            current_tool = str(payload.get("current_tool", "") or "").strip()
            turn_cost = payload.get("turn_cost_usd")
            pieces: list[str] = []
            if current_tool:
                pieces.append(f"tool={current_tool}")
            context_label = WSHandler._context_usage_compact_label(payload)
            if context_label:
                pieces.append(f"context={context_label}")
            if turn_cost not in (None, ""):
                pieces.append(f"turn=${float(turn_cost):.4f}")
            summary = "Runtime status"
            detail = " | ".join(pieces)
        elif runtime_type in {"permission_requested", "permission_resolved"}:
            entry_type = "autonomy"
            target = str(payload.get("tool_name", "") or "tool").strip()
            resolution = str(payload.get("resolution", payload.get("predicted_permission", "")) or "").strip()
            summary = f"{target}: {resolution or runtime_type.replace('_', ' ')}".strip(": ")
            detail = str(payload.get("rationale", "") or "").strip()
        elif runtime_type == "cost_update":
            entry_type = "status_change"
            summary = "Cost update"
            detail = (
                f"turn=${float(payload.get('turn_cost_usd', 0.0) or 0.0):.4f} "
                f"session=${float(payload.get('session_cost_usd', 0.0) or 0.0):.4f}"
            )
        elif runtime_type == "context_usage":
            entry_type = "status_change"
            summary = "Context usage"
            detail = WSHandler._context_usage_detail(payload) or "Context usage updated"
        elif runtime_type == "context_warning":
            entry_type = "status_change"
            summary = "Context usage high"
            detail = WSHandler._context_usage_detail(payload) or "Context window nearly full"
        elif runtime_type in {"subagent_started", "subagent_updated", "subagent_completed"}:
            if is_task_mode and str(payload.get("profile", "") or "").strip() == "verify":
                entry_type = "verification"
                profile = str(payload.get("profile", "") or "verify").strip()
                summary = f"{profile}: {runtime_type.replace('_', ' ')}"
                detail = (
                    str(payload.get("content_preview", "") or "").strip()
                    or str(payload.get("message", "") or "").strip()
                    or str(payload.get("status", "") or "").strip()
                )
            else:
                entry_type = "handoff"
                profile = str(payload.get("profile", "") or "subagent").strip()
                summary = f"{profile}: {runtime_type.replace('_', ' ')}"
                detail = (
                    str(payload.get("content_preview", "") or "").strip()
                    or str(payload.get("message", "") or "").strip()
                    or str(payload.get("status", "") or "").strip()
                )
        elif runtime_type == "member_inbox_updated":
            entry_type = "status_change"
            summary = "Resident inbox updated"
            pieces = [
                f"chat={int(payload.get('actionable_inbox_count', 0) or 0)}",
                f"protocol={int(payload.get('protocol_backlog_count', 0) or 0)}",
                f"notifications={int(payload.get('notification_backlog_count', 0) or 0)}",
            ]
            resident_status = str(payload.get("resident_status", "") or "").strip()
            if resident_status:
                pieces.append(f"status={resident_status}")
            detail = " | ".join(pieces)
        elif runtime_type == "worker_notification":
            notification_kind = str(payload.get("notification_kind", "") or "update").strip() or "update"
            if notification_kind == "blocked":
                entry_type = "awaiting_peer"
            elif notification_kind == "error":
                entry_type = "work_item_failed"
            elif notification_kind in {"task_complete", "handoff_ready"}:
                entry_type = "handoff"
            else:
                entry_type = "status_change"
            worker_label = (
                str(payload.get("name", "") or "").strip()
                or str(payload.get("worker_type", "") or "worker").strip().replace("_", " ")
            )
            summary = f"{worker_label}: {notification_kind.replace('_', ' ')}".strip(": ")
            detail = str(payload.get("summary", "") or "").strip()
        elif runtime_type == "compaction_applied":
            if is_task_mode and runtime_type in _TASK_MODE_DEBUG_ONLY_PROGRESS_TYPES:
                return None
            entry_type = "status_change"
            summary = "Context compacted"
            detail = f"message_count={payload.get('message_count', '')}".strip()
        elif runtime_type in {"verification_started", "verification_repair_requested", "verification_completed"}:
            entry_type = "verification" if is_task_mode else "status_change"
            summary = runtime_type.replace("_", " ").title()
            detail = (
                str(payload.get("verdict", "") or "").strip()
                or str(payload.get("reason", "") or "").strip()
                or str(payload.get("profile", "") or "").strip()
            )
        elif runtime_type == "checkpoint_saved":
            if is_task_mode:
                entry_type = "needs_input"
                summary = "Needs input"
                detail = str(payload.get("checkpoint_type", "") or "").strip()
            else:
                review_level = str(payload.get("review_level", "") or "").strip().lower()
                entry_type = "awaiting_manager_review" if review_level == "manager" else "awaiting_human"
                review_target = str(payload.get("review_target_role_id", "") or "").strip()
                summary = (
                    f"Awaiting {review_target or 'manager'} review"
                    if review_level == "manager"
                    else "Awaiting human review"
                )
                detail = str(payload.get("checkpoint_type", "") or "").strip()
        elif runtime_type == "turn_completed":
            entry_type = "gate_approved"
            summary = f"Turn {payload.get('iteration', '?')} completed"
            detail = str(payload.get("content_preview", "") or "").strip()
        elif runtime_type == "turn_failed":
            entry_type = "work_item_failed"
            summary = f"Turn {payload.get('iteration', '?')} failed"
            detail = str(payload.get("message", "") or "").strip()

        entry: dict[str, Any] = {
            "type": entry_type,
            "summary": summary[:160] if summary else runtime_type,
            "detail": detail[:4000] if detail else None,
        }
        tool_call_id = str(payload.get("tool_call_id", "") or "").strip()
        if tool_call_id and entry_type in {"tool_call", "autonomy"}:
            turn_id = str(payload.get("turn_id", "") or "").strip()
            prefix = "permission" if entry_type == "autonomy" else "tool"
            entry.setdefault("item_id", f"{turn_id}:{prefix}:{tool_call_id}" if turn_id else f"{prefix}:{tool_call_id}")
            entry.setdefault("stream_id", entry["item_id"])
            entry["tool_call_id"] = tool_call_id
        permission_group_key = str(payload.get("permission_group_key", "") or "").strip()
        if permission_group_key:
            entry["permission_group_key"] = permission_group_key
        for alias_key in ("turn_id", "item_id", "stream_id", "seq", "execution_mode"):
            if alias_key in payload and payload.get(alias_key) not in (None, ""):
                entry[alias_key] = payload.get(alias_key)
        work_item_projection_id = str(payload.get("work_item_projection_id") or "").strip()
        if is_task_mode and work_item_projection_id == "task_mode_execution":
            work_item_projection_id = ""
        work_item_turn_type = str(
            payload.get("work_item_turn_type")
            or payload.get("turn_type")
            or ""
        ).strip()
        work_item_projection_title = str(payload.get("work_item_projection_title", "") or "").strip()
        work_item_role_name = str(payload.get("work_item_role_name") or payload.get("role_name") or "").strip()
        if not work_item_role_name:
            role_id = str(
                payload.get("role_id")
                or payload.get("agent_role_id")
                or payload.get("work_item_role_id")
                or ""
            ).strip()
            work_item_role_name = WSHandler._humanize_role_label(role_id)
        if not is_task_mode and (work_item_projection_id or work_item_projection_title):
            entry.update(
                work_item_identity_payload(
                    projection_id=work_item_projection_id or work_item_projection_title,
                    turn_type=work_item_turn_type,
                )
            )
            entry["work_item_projection_title"] = (
                work_item_role_name
                or work_item_projection_title
                or work_item_projection_id.replace("_", " ").title()
            )
            entry["is_company_runtime"] = True
        if work_item_role_name:
            entry["role_name"] = work_item_role_name
        return entry

    @staticmethod
    def _should_store_progress_message(text: str) -> bool:
        """Route high-signal progress into chat/activity without flooding it."""
        if len(text) <= 10:
            return False
        # Plain-text progress is used for native agent final replies; the
        # authoritative assistant message comes from transcript sync. Storing
        # it here creates a duplicate system-colored chat bubble.
        if not text.startswith("["):
            return False
        if text.startswith(("[Tool:", "[Autonomy]", "[Cost:", "[Token", "[CapabilityRecovery]")):
            return False
        if text.startswith("[External:"):
            return False
        if text.startswith("[External status]"):
            lowered = text.lower()
            return (
                "started pid=" in lowered
                or "timed out" in lowered
                or "cancelled" in lowered
            )
        return True

    def _related_parent_task_ids(self, task: Any) -> list[str]:
        task_id = str(getattr(task, "id", "") or "").strip()
        metadata = dict(getattr(task, "metadata", {}) or {})
        related: list[str] = []

        for candidate in (
            self._active_runtime_children.get(task_id),
            metadata.get("origin_task_id"),
            self._session_to_task.get(str(getattr(task, "parent_session_id", "") or "").strip()),
        ):
            resolved = str(candidate or "").strip()
            if resolved and resolved != task_id and resolved not in related:
                related.append(resolved)
        return related

    def _ensure_task_display_num(self, task_id: str) -> int:
        existing = self.event_adapter._task_display_map.get(task_id)
        if existing is not None:
            return existing
        self.event_adapter._task_display_counter += 1
        self.event_adapter._task_display_map[task_id] = self.event_adapter._task_display_counter
        return self.event_adapter._task_display_counter

    async def _materialize_runtime_task_visibility(
        self,
        payload: dict[str, Any],
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        """Ensure runtime-only company events still materialize a live UI session.

        Some company-mode tasks are first surfaced through runtime events such as
        ``member_session_started`` instead of the dedicated ``child_session_created``
        path. Without an explicit session/board broadcast, the frontend receives
        progress updates for an unknown task and cannot render the execution tree
        until a later full ``collab_sync`` rebuild.
        """
        runtime_engine = engine or self.engine
        task_id = str(payload.get("task_id", "") or "").strip()
        if not task_id or not self._store_is_ready(runtime_engine.store):
            return

        try:
            task = await runtime_engine.store.get_task(task_id)
        except Exception:
            task = None
        if task is None:
            return

        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not session_id:
            return

        metadata = dict(getattr(task, "metadata", {}) or {})
        project_id = self._normalize_project_id(
            getattr(task, "project_id", "") or project_id or getattr(runtime_engine, "project_id", None)
        )
        ui_task_id = self._ui_task_id_for_task(task) or task_id
        ui_task = task
        if ui_task_id != task_id and self._store_is_ready(runtime_engine.store):
            try:
                resolved = await runtime_engine.store.get_task(ui_task_id)
            except Exception:
                resolved = None
            if resolved is not None:
                ui_task = resolved
        title = str(getattr(ui_task, "title", "") or getattr(task, "title", "") or payload.get("title") or "Session").strip() or "Session"
        channel_id = f"session:{ui_task_id}"
        try:
            existing_channels = await self.chat_store.get_session_channels(project_id)
        except Exception:
            existing_channels = []
        channel = next(
            (
                channel
                for channel in existing_channels
                if str(channel.get("channel_id", "") or "").strip() == channel_id
            ),
            None,
        )
        channel_already_materialized = channel is not None
        if channel is None:
            channel = await self.chat_store.create_session_channel(ui_task_id, title, project_id=project_id)
        self._session_to_task[session_id] = ui_task_id
        if channel_already_materialized:
            return

        role_id = str(getattr(task, "assigned_to", "") or metadata.get("work_item_role_id", "") or "").strip()
        assignee_ids = [self.event_adapter._resolve_role_to_agent(role_id)] if role_id else []
        work_item_role_name = str(metadata.get("work_item_role_name", "") or "").strip()
        if not work_item_role_name and role_id:
            work_item_role_name = self._resolve_work_item_role_name(role_id, metadata, engine=runtime_engine)
        shared_role_session = bool(metadata.get("shared_role_session", False))

        parent_session_id = str(
            getattr(task, "parent_session_id", "")
            or metadata.get("parent_session_id", "")
            or ""
        ).strip() or None
        if shared_role_session:
            parent_session_id = None
        origin_task_id = str(
            metadata.get("origin_task_id", "")
            or self._session_to_task.get(str(parent_session_id or ""))
            or ui_task_id
        ).strip() or ui_task_id

        display_num = self._ensure_task_display_num(ui_task_id)
        created_at = channel.get("created_at") if isinstance(channel, dict) else None
        if not isinstance(created_at, (int, float)):
            raw_created_at = getattr(ui_task, "created_at", None) or getattr(task, "created_at", None)
            created_at = raw_created_at.timestamp() if hasattr(raw_created_at, "timestamp") else time.time()

        status = getattr(getattr(task, "status", None), "value", str(getattr(task, "status", "pending")))
        exec_mode, company_profile = self._resolve_task_session_config(task)
        preferred_agent = self._resolve_task_preferred_agent(task)
        selected_execution_agent = self._resolve_task_selected_execution_agent(task)
        work_item_projection_id = work_item_projection_id_from_metadata(metadata)
        work_item_turn_type = work_item_turn_type_from_metadata(metadata, fallback="")
        work_item_identity = {
            **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
            "work_item_role_id": role_id or None,
            "work_item_role_name": work_item_role_name or None,
            "employee_assignment": metadata.get("employee_assignment"),
            "origin_task_id": origin_task_id,
            "selected_execution_agent": selected_execution_agent,
        }
        execution_aliases = _add_execution_turn_aliases({}, task_id)

        if parent_session_id:
            await self.broadcast({
                "type": "child_session_created",
                "payload": {
                    "project_id": project_id,
                    "session_id": session_id,
                    "parent_session_id": parent_session_id,
                    "task_id": ui_task_id,
                    **execution_aliases,
                    "origin_task_id": origin_task_id,
                    "title": title,
                    "agent_id": assignee_ids[0] if assignee_ids else None,
                    **work_item_identity,
                },
            })

        if not parent_session_id:
            await self.broadcast({
                "type": "board_task_created",
                "payload": {
                    "project_id": project_id,
                    "task_id": ui_task_id,
                    **execution_aliases,
                    "display_id": f"OPC-{display_num}",
                    "board_id": project_id,
                    "title": title,
                    "assignee_ids": assignee_ids,
                    **work_item_identity,
                },
            })
        await self.broadcast({
            "type": "session_created",
            "payload": {
                "project_id": project_id,
                "task_id": ui_task_id,
                **execution_aliases,
                "channel_id": channel_id,
                "session_id": session_id,
                "parent_session_id": parent_session_id,
                "origin_task_id": origin_task_id,
                "exec_mode": exec_mode,
                "company_profile": company_profile,
                "preferred_agent": preferred_agent,
                "selected_execution_agent": selected_execution_agent,
                "title": title,
                "status": status,
                "created_at": created_at,
                "assignee_ids": assignee_ids,
                **work_item_identity,
            },
        })

    async def _ensure_reply_projected(
        self,
        *,
        channel_id: str,
        project_id: str,
        session_id: str | None,
        engine: Any | None = None,
    ) -> None:
        """Last-resort invariant: the session's newest persisted top-level reply
        must exist in the UI channel once the turn has unwound.

        The transcript sync is the normal projection path; when it is starved,
        cancelled, or misses the row (project 000, 2026-07-07 19:21/20:27), the
        engine has replied but the user sees an empty conversation forever.
        Detection is by the transcript message id, so an already-projected reply
        (any channel) is never duplicated.
        """
        if not session_id:
            return
        runtime_engine = engine or self.engine
        store = getattr(runtime_engine, "store", None)
        if not self._store_is_ready(store):
            return
        lister = getattr(store, "list_session_messages", None)
        parts_loader = getattr(store, "list_session_parts", None)
        if not callable(lister) or not callable(parts_loader):
            return
        try:
            records = await lister(session_id)
        except Exception:
            logger.opt(exception=True).debug("reply projection: failed to list session messages")
            return
        latest = None
        for record in reversed(records or []):
            if str(getattr(record, "role", "") or "").strip().lower() != "assistant":
                continue
            metadata = dict(getattr(record, "metadata", {}) or {})
            if str(metadata.get("kind", "") or "").strip() != "top_level_reply":
                continue
            latest = record
            break
        if latest is None:
            return
        message_id = str(getattr(latest, "message_id", "") or "").strip()
        if not message_id:
            return
        try:
            if await self.chat_store.message_scope(message_id) is not None:
                return
        except Exception:
            return
        try:
            parts = await parts_loader(session_id, message_id)
        except Exception:
            logger.opt(exception=True).debug("reply projection: failed to load reply parts")
            return
        text = "\n".join(
            chunk
            for part in parts or []
            if str(getattr(part, "part_type", "") or "") == "text"
            for chunk in [str(dict(getattr(part, "payload", {}) or {}).get("text", "") or "")]
            if chunk
        ).strip()
        if not text:
            return
        logger.warning(
            f"Top-level reply {message_id} missing from UI channel {channel_id} after "
            "transcript sync; projecting it directly"
        )
        reply_metadata = dict(getattr(latest, "metadata", {}) or {})
        reply_metadata.setdefault("kind", "top_level_reply")
        reply_metadata.setdefault("source", "engine")
        reply_metadata.setdefault("ui_message_id", message_id)
        reply_metadata["reply_projection_fallback"] = True
        msg = await self.chat_store.insert_message(
            channel_id=channel_id,
            sender="assistant",
            sender_name="OPC",
            content=text,
            project_id=project_id,
            metadata=reply_metadata,
            message_id=message_id,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

    async def _sync_task_transcript_messages(
        self,
        task_id: str,
        *,
        engine: Any | None = None,
        broadcast: bool = True,
        detail_level: str = "summary",
        latest_assistant_metadata: dict[str, Any] | None = None,
    ) -> int:
        """Backfill persisted transcript messages into chat_store and UI."""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        if not self._store_is_ready(store):
            return 0

        task = await store.get_task(task_id)
        if not task:
            return 0

        transcript_loader = getattr(store, "get_session_transcript", None)
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not callable(transcript_loader) or not session_id:
            return 0

        project_id = getattr(task, "project_id", None) or runtime_engine.project_id or "default"
        ui_task_id = self._ui_task_id_for_task(task) or task_id
        ui_task = task
        if ui_task_id != task_id:
            try:
                resolved = await store.get_task(ui_task_id)
            except Exception:
                resolved = None
            if resolved is not None:
                ui_task = resolved
        channel_id = f"session:{ui_task_id}"
        await self.chat_store.create_session_channel(
            ui_task_id,
            getattr(ui_task, "title", "") or getattr(task, "title", "") or "Session",
            project_id=project_id,
        )

        try:
            transcript = await transcript_loader(session_id)
        except Exception:
            transcript = []

        formatted_messages = build_transcript_ui_messages(
            transcript,
            channel_id=channel_id,
            task_id=ui_task_id,
            detail_level=_normalize_transcript_detail_level(detail_level),
        )

        if latest_assistant_metadata:
            latest_assistant_metadata = (
                latest_assistant_metadata
                if self._checkpoint_metadata_targets_task(latest_assistant_metadata, task)
                else None
            )

        attached_latest_metadata = False
        if latest_assistant_metadata:
            for message in reversed(formatted_messages):
                if str(message.get("sender", "") or "").strip().lower() == "user":
                    continue
                if not self._message_can_host_checkpoint_metadata(message, latest_assistant_metadata):
                    continue
                metadata = dict(message.get("metadata", {}) or {})
                metadata.update(latest_assistant_metadata)
                message["metadata"] = metadata
                attached_latest_metadata = True
                break
            if not attached_latest_metadata:
                checkpoint_id = str(latest_assistant_metadata.get("checkpoint_id", "") or "").strip()
                synthetic_message_id = f"checkpoint::{checkpoint_id}" if checkpoint_id else str(uuid.uuid4())
                formatted_messages.append({
                    "message_id": synthetic_message_id,
                    "sender": "assistant",
                    "sender_name": str(
                        latest_assistant_metadata.get("work_item_projection_title")
                        or latest_assistant_metadata.get("requesting_role_id")
                        or "Company Member"
                    ),
                    "content": str(
                        latest_assistant_metadata.get("prompt")
                        or latest_assistant_metadata.get("summary")
                        or "Human review requested."
                    ),
                    "timestamp": time.time(),
                    "reply_to_id": None,
                    "mentions": [],
                    "metadata": dict(latest_assistant_metadata),
                })

        inserted_messages = await self.chat_store.backfill_messages(
            channel_id,
            formatted_messages,
            project_id=project_id,
        )
        if broadcast and inserted_messages:
            for message in inserted_messages:
                await self.broadcast({"type": "session_message", "payload": {
                    "project_id": project_id,
                    "message_id": message["message_id"],
                    "channel_id": channel_id,
                    "sender": message["sender"],
                    "sender_name": message["sender_name"],
                    "content": message["content"],
                    "created_at": message["timestamp"],
                    "reply_to_id": message.get("reply_to_id"),
                    "mentions": message.get("mentions", []),
                    "metadata": message.get("metadata", {}),
                }})
        return len(inserted_messages)

    @staticmethod
    def _checkpoint_metadata_targets_task(metadata: dict[str, Any], task: Any) -> bool:
        if str((metadata or {}).get("checkpoint_type", "") or "").strip() != "company_delivery_feedback":
            return True
        target_task_id = str(
            (metadata or {}).get("waiting_task_id")
            or (metadata or {}).get("task_id")
            or ""
        ).strip()
        task_id = str(getattr(task, "id", "") or "").strip()
        return not target_task_id or not task_id or target_task_id == task_id

    @staticmethod
    def _message_can_host_checkpoint_metadata(
        message: dict[str, Any],
        checkpoint_metadata: dict[str, Any],
    ) -> bool:
        if str((checkpoint_metadata or {}).get("checkpoint_type", "") or "").strip() != "company_delivery_feedback":
            return True
        metadata = dict(message.get("metadata", {}) or {})
        if str(message.get("sender", "") or "").strip().lower() == "system":
            return False
        if str(metadata.get("kind", "") or "").strip() == "worker_notification":
            return False
        if str(metadata.get("transcript_kind", "") or "").strip() == "child_result":
            return False
        return True

    # ══════════════════════════════════════════════════════════════════════
    # Inbound routing
    # ══════════════════════════════════════════════════════════════════════

    async def _route_message(self, ws: Any, raw: str) -> None:
        """Parse and route an incoming WS message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        # ``json.loads`` succeeds for non-object frames (null/number/array/string);
        # ``data.get`` would then raise AttributeError, escape this method, and drop the
        # whole WS connection. Ignore anything that is not a JSON object.
        if not isinstance(data, dict):
            return

        msg_type = data.get("type", "")
        if self._shutting_down:
            logger.debug(f"Ignoring WS message during shutdown: {msg_type}")
            return
        handler_name = self._HANDLERS.get(msg_type)
        handler = getattr(self, handler_name, None) if handler_name else None
        handoff_registry = None
        handoff_token: str | None = None
        if handler and msg_type in _EXECUTION_HANDOFF_MESSAGE_TYPES:
            handoff_registry = self._controller_active_task_run_registry()
            if handoff_registry is not None:
                try:
                    handoff_token = handoff_registry.reserve_handoff()
                except ActiveTaskRunAdmissionClosed:
                    await self._send_ack(
                        ws,
                        ok=False,
                        error="service_shutting_down",
                        action=msg_type,
                    )
                    return
        if handler:
            current_task = asyncio.current_task()
            if current_task is not None:
                self._active_message_tasks.add(current_task)
                if handoff_token is not None:
                    handoff_routes = getattr(self, "_handoff_route_tasks", None)
                    if not isinstance(handoff_routes, dict):
                        handoff_routes = {}
                        self._handoff_route_tasks = handoff_routes
                    handoff_routes[current_task] = handoff_token
            try:
                if handoff_registry is not None and handoff_token is not None:
                    with handoff_registry.bind_handoff(handoff_token):
                        await handler(ws, data)
                else:
                    await handler(ws, data)
            except Exception as e:
                if self._is_ws_disconnect_error(e) or self._is_expected_shutdown_error(e):
                    logger.debug(
                        f"WS handler closed for {msg_type} during disconnect/shutdown: "
                        f"{type(e).__name__}: {e!r}"
                    )
                elif isinstance(e, ProjectScopeError):
                    logger.warning(
                        "Rejected project-scoped WS request without project_id: type={} keys={} project_id={!r} projectId={!r}",
                        msg_type,
                        sorted(str(key) for key in data.keys()),
                        data.get("project_id"),
                        data.get("projectId"),
                    )
                    try:
                        await self._send_ack(ws, ok=False, error=str(e), action=msg_type)
                    except Exception:
                        pass
                else:
                    logger.opt(exception=True).error(f"WS handler error for {msg_type}: {type(e).__name__}: {e!r}")
                    try:
                        await self._send_ack(ws, ok=False, error=str(e) or type(e).__name__, action=msg_type)
                    except Exception:
                        pass  # WS may already be closed
            finally:
                if handoff_registry is not None and handoff_token is not None:
                    handoff_registry.release_handoff(handoff_token)
                if current_task is not None:
                    self._active_message_tasks.discard(current_task)
                    handoff_routes = getattr(self, "_handoff_route_tasks", None)
                    if isinstance(handoff_routes, dict):
                        handoff_routes.pop(current_task, None)
        else:
            logger.debug(f"Unknown WS message type: {msg_type}")
            await self._send_ack(
                ws,
                ok=False,
                error="unknown_message_type",
                action=msg_type,
            )

    # ── Sync ──────────────────────────────────────────────────────────

    async def _handle_ping(self, ws: Any, data: dict) -> None:
        await ws.send_json({"type": "pong"})

    async def _send_project_index_for_client(
        self,
        ws: Any,
        engine: Any,
        project_id: str,
        *,
        switch_seq: str = "",
        view_generation: Any = None,
        include_snapshot: bool = False,
        send_error_ack: bool = True,
    ) -> None:
        try:
            index_payload = await build_project_index_sync(
                engine,
                self.agent_store,
                self.chat_store,
                self.event_adapter,
                exec_mode=self._exec_mode,
            )
            index_payload["project_id"] = project_id
            index_payload["switch_seq"] = switch_seq
            if view_generation is not None:
                index_payload["view_generation"] = view_generation

            if self._client_active_project_id(ws) != project_id:
                return
            if switch_seq and self._client_switch_seq.get(ws, "") != switch_seq:
                return
            await self._send_envelope_to_client(
                ws,
                {"type": "project_index_push", "payload": index_payload},
            )

            if not include_snapshot:
                return
            try:
                snapshot = await build_snapshot(
                    engine,
                    self.agent_store,
                    self.chat_store,
                    self.event_adapter,
                )
                snapshot["project_id"] = project_id
                snapshot["exec_mode"] = self._exec_mode
                snapshot["company_profile"] = self._company_profile
                snapshot["task_preferred_agent"] = self._task_preferred_agent
                snapshot["switch_seq"] = switch_seq
                if view_generation is not None:
                    snapshot["view_generation"] = view_generation
                if self._client_active_project_id(ws) != project_id:
                    return
                if switch_seq and self._client_switch_seq.get(ws, "") != switch_seq:
                    return
                await self._send_envelope_to_client(ws, {"type": "snapshot", "payload": snapshot})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.opt(exception=True).warning(
                    f"Project index sent, but snapshot refresh failed for {project_id}: {type(exc).__name__}: {exc!r}",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.opt(exception=True).error(
                f"Failed to build project index for {project_id}: {type(exc).__name__}: {exc!r}",
            )
            if send_error_ack and self._client_active_project_id(ws) == project_id:
                await self._send_ack(
                    ws,
                    ok=False,
                    action="project_index",
                    project_id=project_id,
                    switch_seq=switch_seq,
                    error=f"Project index failed: {exc}",
                )

    async def _send_initial_project_state_for_client(
        self,
        ws: Any,
        engine: Any,
        project_id: str,
    ) -> None:
        """Send the full reconnect baseline for a newly opened websocket."""
        await self._send_project_index_for_client(
            ws,
            engine,
            project_id,
            send_error_ack=False,
        )
        if self._client_active_project_id(ws) != project_id:
            return
        try:
            collab = await build_collab_sync(
                engine,
                self.agent_store,
                self.chat_store,
                self.event_adapter,
                exec_mode=self._exec_mode,
            )
            collab["project_id"] = project_id
            if self._client_active_project_id(ws) == project_id:
                await self._send_envelope_to_client(ws, {"type": "collab_sync_push", "payload": collab})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.opt(exception=True).warning("Initial websocket collab_sync push failed")
        try:
            org_info = await self._build_org_info_payload()
            if self._client_active_project_id(ws) == project_id:
                await self._send_envelope_to_client(ws, {"type": "org_info", "payload": org_info})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.opt(exception=True).warning("Initial websocket org_info push failed")

    async def _handle_collab_sync(self, ws: Any, data: dict) -> None:
        engine, project_id = await self._engine_for_request(data)
        result = await build_collab_sync(
            engine,
            self.agent_store,
            self.chat_store,
            self.event_adapter,
            exec_mode=self._exec_mode,
        )
        result["ok"] = True
        result["project_id"] = project_id
        if data.get("switch_seq") or data.get("switchSeq"):
            result["switch_seq"] = str(data.get("switch_seq") or data.get("switchSeq") or "")
        if data.get("view_generation") is not None:
            result["view_generation"] = data.get("view_generation")
        await ws.send_json({"type": "ack", "payload": result})

    async def _handle_project_index(self, ws: Any, data: dict) -> None:
        engine, project_id = await self._engine_for_request(data)
        switch_seq = str(data.get("switch_seq") or data.get("switchSeq") or "").strip()
        view_generation = data.get("view_generation")
        self._track_client_project_index(
            ws,
            self._send_project_index_for_client(
                ws,
                engine,
                project_id,
                switch_seq=switch_seq,
                view_generation=view_generation,
                include_snapshot=bool(data.get("include_snapshot") or data.get("includeSnapshot")),
            ),
        )
        await self._send_ack(
            ws,
            ok=True,
            action="project_index",
            project_id=project_id,
            switch_seq=switch_seq,
        )

    # ── Chat ──────────────────────────────────────────────────────────

    # ── Kanban ────────────────────────────────────────────────────────

    async def _handle_kanban_create_board(self, ws: Any, data: dict) -> None:
        # We use one board per project; accept for protocol compatibility
        _engine, project_id = await self._engine_for_request(data)
        data = {**data, "project_id": project_id}
        await self._send_ack(ws, ok=True)
        await self.broadcast({"type": "kanban_board_created", "payload": data})

    async def _handle_kanban_create_task(self, ws: Any, data: dict) -> None:
        try:
            _engine, pid = await self._engine_for_request(data)
            result = await self.services.kanban.create_task(
                project_id=pid,
                title=data.get("title", "Untitled"),
                description=data.get("description", ""),
                task_id=data.get("task_id"),
                board_id=data.get("board_id", pid),
                assignee_ids=data.get("assignee_ids", []),
            )
            self._session_to_task.update(self.services_context.session_to_task)
            await self._publish_service_result(result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_create_task")

    async def _handle_kanban_update_task(self, ws: Any, data: dict) -> None:
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.update_task(
                project_id=project_id,
                task_id=data.get("task_id", ""),
                updates=data.get("updates", {}),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_update_task")

    async def _handle_kanban_move_task(self, ws: Any, data: dict) -> None:
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.move_task(
                project_id=project_id,
                task_id=data.get("task_id", ""),
                column_id=data.get("column_id", ""),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_move_task")

    async def _handle_kanban_delete_task(self, ws: Any, data: dict) -> None:
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.delete_task(project_id=project_id, task_id=data.get("task_id", ""))
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_delete_task")

    async def _handle_kanban_delete_board(self, ws: Any, data: dict) -> None:
        # No-op for OPC's project-based boards
        _engine, project_id = await self._engine_for_request(data)
        await self._send_ack(ws, ok=True, project_id=project_id)

    async def _handle_kanban_assign(self, ws: Any, data: dict) -> None:
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.assign(
                project_id=project_id,
                task_id=data.get("task_id", ""),
                agent_id=data.get("agent_id", ""),
            )
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_assign")

    async def _handle_kanban_status(self, ws: Any, data: dict) -> None:
        # Alias: convert status to column_id and delegate
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.status(
                project_id=project_id,
                task_id=data.get("task_id", ""),
                status=data.get("status", data.get("column_id", "")),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_status")

    async def _handle_kanban_switch_view(self, ws: Any, data: dict) -> None:
        """Return filtered kanban data for global/office/agent view levels."""
        from opc.plugins.office_ui.snapshot_builder import build_company_kanban_projection, task_to_kanban
        level = data.get("level", "global")
        target_id = data.get("target_id")
        run_engine, project_id = await self._engine_for_request(data)

        tasks: list[Any] = []
        if run_engine.store:
            try:
                tasks = await run_engine.store.get_tasks(project_id=project_id)
            except Exception:
                logger.warning("Failed to load tasks for kanban_switch_view")

        company_projection_tasks: list[dict[str, Any]] = []
        company_columns: list[dict[str, Any]] = []
        company_boards: list[dict[str, Any]] = []
        if run_engine.store:
            try:
                company_projection_tasks, company_columns, company_boards, _ = await build_company_kanban_projection(
                    run_engine,
                    project_id=project_id,
                    tasks=tasks,
                    event_adapter=self.event_adapter,
                )
            except Exception:
                logger.opt(exception=True).warning("Failed to load company kanban projection for kanban_switch_view")
        if self._exec_mode in {"company", "org", "custom"} and not company_columns:
            company_boards = [{
                "board_id": project_id,
                "name": project_id if project_id != "default" else "Main Board",
                "prefix": "OPC",
                "color": "#4f46e5",
                "next_task_num": 1,
                "created_at": time.time(),
                "updated_at": time.time(),
            }]
            company_columns = build_company_board_columns(project_id)

        if company_columns:
            filtered_tasks = list(company_projection_tasks)
            if level == "agent" and target_id:
                agent = await self.agent_store._get_one(target_id)
                role_id = agent.get("opc_role_id", target_id) if agent else target_id
                filtered_tasks = [
                    task for task in filtered_tasks
                    if str(task.get("work_item_role_id", "") or "").strip() == str(role_id or "").strip()
                ]
            elif level == "office" and target_id and getattr(run_engine, "org_engine", None) is not None:
                office_role_ids = {
                    str(agent.get("opc_role_id", agent.get("agent_id", "")) or "").strip()
                    for agent in await self.agent_store.get_all()
                    if str(agent.get("office_id", "") or "").strip() == str(target_id or "").strip()
                }
                filtered_tasks = [
                    task for task in filtered_tasks
                    if str(task.get("work_item_role_id", "") or "").strip() in office_role_ids
                ]
            boards = list(company_boards)
            if boards:
                counts_by_board: dict[str, int] = {}
                for task in filtered_tasks:
                    board_id = str(task.get("board_id", "") or "").strip()
                    if not board_id:
                        continue
                    counts_by_board[board_id] = counts_by_board.get(board_id, 0) + 1
                for board in boards:
                    board_id = str(board.get("board_id", "") or "").strip()
                    board["next_task_num"] = int(counts_by_board.get(board_id, 0) or 0) + 1
            await ws.send_json({"type": "kanban_view_data", "payload": {
                "project_id": project_id,
                "boards": boards,
                "columns": company_columns,
                "tasks": filtered_tasks,
                "work_item_projections": [],
            }})
            return

        # Filter tasks by view level (task.assigned_to stores opc_role_id)
        if level == "agent" and target_id:
            agent = await self.agent_store._get_one(target_id)
            role_id = agent.get("opc_role_id", target_id) if agent else target_id
            tasks = [t for t in tasks if t.assigned_to == role_id]
        elif level == "office" and target_id:
            agents = await self.agent_store.get_all()
            office_role_ids = {a.get("opc_role_id", a["agent_id"]) for a in agents if a.get("office_id") == target_id}
            tasks = [t for t in tasks if t.assigned_to in office_role_ids]

        formatted_tasks = [task_to_kanban(t, i + 1, self.event_adapter) for i, t in enumerate(tasks)]

        # Board and columns (same structure as collab_sync)
        now = time.time()
        boards = [{
            "board_id": project_id,
            "name": project_id if project_id != "default" else "Main Board",
            "prefix": "OPC",
            "color": "#4f46e5",
            "next_task_num": len(tasks) + 1,
            "created_at": now,
            "updated_at": now,
        }]
        columns = [
            {"column_id": "todo", "board_id": project_id, "name": "Todo",
             "color": "#6b7280", "sort_order": 0, "is_terminal": False},
            {"column_id": "in-progress", "board_id": project_id, "name": "In Progress",
             "color": "#eab308", "sort_order": 1, "is_terminal": False},
            {"column_id": "done", "board_id": project_id, "name": "Done",
             "color": "#22c55e", "sort_order": 2, "is_terminal": True},
        ]
        await ws.send_json({"type": "kanban_view_data", "payload": {
            "project_id": project_id,
            "boards": boards,
            "columns": columns,
            "tasks": formatted_tasks,
            "work_item_projections": [],
        }})

    # ── Agent Management ──────────────────────────────────────────────

    async def _handle_create_agent(self, ws: Any, data: dict) -> None:
        role = data.get("role", {})
        role_id = role.get("id", "executor")
        # Resolve name: explicit name → org_engine role name → role_id
        name = role.get("name")
        if not name or name == role_id:
            if self.engine.org_engine:
                org_role = self.engine.org_engine.get_agent(role_id)
                if org_role:
                    name = org_role.name
            if not name:
                name = role_id.replace("_", " ").replace("-", " ").title()
        office_id = role.get("office_id", "office-0")
        # Collect optional custom fields from frontend
        description = role.get("description", "")
        specialties = role.get("specialties", [])
        if isinstance(specialties, str):
            specialties = [s.strip() for s in specialties.split(",") if s.strip()]
        tools = role.get("tools", [])
        system_prompt = role.get("system_prompt", "")
        appearance = role.get("appearance", {})
        palette = appearance.get("palette") if isinstance(appearance, dict) else None
        seat_zone = appearance.get("seat_zone", "workspace") if isinstance(appearance, dict) else "workspace"

        # Custom mode: create full three-layer data (RoleConfig + EmployeeConfig + Agent)
        employee_id = None
        if self._exec_mode in {"org", "custom"} and self.engine.org_engine:
            async with self._config_lock:
                from opc.core.config import RoleConfig, EmployeeConfig

                org = self.engine.org_engine
                role_created = False

                # 1. Create RoleConfig if role doesn't exist yet
                if not org.get_agent(role_id):
                    org.add_role(RoleConfig(
                        id=role_id,
                        name=name,
                        responsibility=description,
                        tools=tools or list(specialties or []),
                    ))
                    role_created = True

                # 2. Generate unique employee_id (uuid suffix prevents collisions)
                slug = f"{role_id}-{name.lower().replace(' ', '-')}"
                if any(e.employee_id == slug for e in self.engine.config.org.employees):
                    slug = f"{slug}-{uuid.uuid4().hex[:8]}"
                employee_id = slug

                # 3. Write custom prompt file if system_prompt provided
                prompt_refs: list[str] = []
                if system_prompt:
                    prompt_ref = self._write_custom_prompt(employee_id, name, system_prompt)
                    prompt_refs.append(prompt_ref)

                # 4. Create EmployeeConfig and append to config
                all_tools = tools or list(specialties or [])
                emp = EmployeeConfig(
                    employee_id=employee_id,
                    name=name,
                    role_id=role_id,
                    description=description,
                    category=description[:60] if description else "",
                    domains=all_tools,
                    tags=list(specialties or []),
                    prompt_refs=prompt_refs,
                )
                self.engine.config.org.employees = [
                    *self.engine.config.org.employees,
                    emp,
                ]

                # 5. Persist under lock to prevent concurrent save races
                try:
                    self._persist_runtime_config()
                except Exception:
                    # Rollback in-memory state
                    self.engine.config.org.employees = [
                        e for e in self.engine.config.org.employees
                        if e.employee_id != employee_id
                    ]
                    if role_created:
                        org.remove_role(role_id)
                    # Clean up prompt file
                    if prompt_refs:
                        prompt_path = Path(self.engine.opc_home) / prompt_refs[0]
                        prompt_path.unlink(missing_ok=True)
                    employee_id = None
                    logger.warning("Failed to persist config after create_agent, rolled back")

        agent = await self.agent_store.create_agent(
            name=name, opc_role_id=role_id, office_id=office_id,
            org_engine=self.engine.org_engine,
            description=description,
            specialties=specialties,
            tools=tools,
            palette=palette,
            seat_zone=seat_zone,
            employee_id=employee_id,
        )

        # Custom mode: broadcast org panel refresh to ALL clients
        if self._exec_mode in {"org", "custom"}:
            await self._broadcast_org_info()
            await self.agent_store.sync_custom_shadow()

        # Sync role map + broadcast agent_spawned event
        await self._sync_role_map()
        await self.broadcast({"type": "event", "payload": {
            "event_id": str(uuid.uuid4()),
            "type": "agent_spawned",
            "agent_id": agent["agent_id"],
            "data": {"role_name": agent["name"]},
            "timestamp": time.time(),
        }})
        agents = await self.agent_store.get_all()
        await self._send_ack(ws, ok=True, agents=agents)

    def _write_custom_prompt(self, employee_id: str, name: str, prompt_text: str) -> str:
        """Write a custom prompt file and return its relative path as a prompt_ref."""
        prompts_dir = Path(self.engine.opc_home) / "prompts" / "custom"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        # ``employee_id`` is derived from user-supplied role id/name and previously flowed
        # unchecked into the path, enabling traversal (e.g. "../../tmp/pwn"). Reduce it to
        # a single safe path component and confirm containment before writing.
        safe_id = Path(str(employee_id or "")).name.replace("..", "")
        safe_id = safe_id.replace("/", "").replace("\\", "").strip() or "agent"
        filename = f"{safe_id}.md"
        filepath = (prompts_dir / filename).resolve()
        base = prompts_dir.resolve()
        if base not in filepath.parents and filepath != base:
            raise ValueError(f"Custom prompt filename escapes prompts directory: {employee_id!r}")
        filepath.write_text(f"# {name}\n\n{prompt_text}\n", encoding="utf-8")
        return f"prompts/custom/{filename}"

    async def _build_org_info_payload(self) -> dict[str, Any]:
        """Build the full org_info payload via OrgService."""
        result = await self._ensure_office_services().org.info()
        return result.payload

    async def _broadcast_org_info(self) -> None:
        """Build org_info payload and broadcast to ALL connected clients."""
        result = await self._ensure_office_services().org.info(include_events=True)
        await self._publish_service_result(result)

    async def _broadcast_snapshot(self) -> None:
        snapshot = await build_snapshot(
            self.engine, self.agent_store, self.chat_store, self.event_adapter
        )
        snapshot["exec_mode"] = self._exec_mode
        snapshot["company_profile"] = self._company_profile
        snapshot["task_preferred_agent"] = self._task_preferred_agent
        await self.broadcast({"type": "snapshot", "payload": snapshot})

    async def _ensure_custom_role_agents(self) -> list[dict[str, Any]]:
        if self._exec_mode not in {"org", "custom"} or not self.engine.org_engine:
            return await self.agent_store.get_all()
        agents = await self.agent_store.ensure_custom_role_agents(self.engine.org_engine)
        await self._sync_role_map()
        return agents

    async def _handle_delete_agent(self, ws: Any, data: dict) -> None:
        try:
            result = await self._ensure_office_services().agent.delete(data.get("agent_id", ""))
            await self._publish_service_result(result)
            if self._exec_mode in {"org", "custom"} and data.get("agent_id"):
                if hasattr(self, "chat_store") and hasattr(self, "event_adapter"):
                    await self._broadcast_snapshot()
                await self._broadcast_org_info()
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="delete_agent")

    async def _handle_list_agents(self, ws: Any, data: dict) -> None:
        result = await self._ensure_office_services().agent.list()
        await self._send_service_ack(ws, result)

    async def _handle_move_agent(self, ws: Any, data: dict) -> None:
        try:
            await self._ensure_office_services().agent.move(
                agent_id=data.get("agent_id", ""),
                office_id=data.get("office_id", "office-0"),
                seat_zone=data.get("seat_zone"),
                desk_id=data.get("desk_id"),
            )
            await self._broadcast_snapshot()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="move_agent")

    async def _handle_get_agent_detail(self, ws: Any, data: dict) -> None:
        """Return detailed info for a single agent (agent-level kanban view)."""
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().agent.detail(
                project_id=project_id,
                agent_id=data.get("agent_id", ""),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="get_agent_detail")

    # ── Company Runtime Mode ─────────────────────────────────────────────────

    async def _handle_set_mode(self, ws: Any, data: dict) -> None:
        new_mode = data.get("mode", "task")
        new_profile = data.get("profile", "corporate")
        org_id = self._normalize_session_org_id(
            data.get("org_id") or data.get("organization_id")
        )
        new_preferred_agent = self._normalize_session_preferred_agent(
            data.get("preferred_agent", self._task_preferred_agent),
            default=self._task_preferred_agent,
        )
        ok = await self._apply_mode_switch(new_mode, new_profile, new_preferred_agent, org_id=org_id)
        if not ok:
            await self._send_ack(
                ws,
                ok=False,
                error=getattr(self, "_last_org_load_error", "") or "org_not_found",
                org_id=org_id,
            )
            return
        active_org_id = (org_id or await self._get_active_saved_org_name()) if self._exec_mode == "org" else ""
        await self._send_ack(
            ws,
            ok=True,
            mode=self._exec_mode,
            profile=self._company_profile,
            org_id=active_org_id,
            preferred_agent=self._task_preferred_agent,
        )

    async def _apply_mode_switch(
        self,
        new_mode: str,
        new_profile: str,
        new_preferred_agent: str,
        *,
        sync_config: bool = True,
        org_id: str | None = None,
    ) -> bool:
        # Mode is a default for new turns. Existing sessions carry their own
        # persisted mode/profile, so switching the toolbar must not interrupt or
        # rewrite running task state.
        previous_mode = getattr(self, "_exec_mode", "task")
        previous_profile = getattr(self, "_company_profile", "corporate")
        previous_preferred_agent = getattr(self, "_task_preferred_agent", "native")
        new_mode = self._normalize_session_exec_mode(new_mode)
        self._last_org_load_error = ""
        if sync_config and new_mode == "org" and org_id:
            config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
            if not org_config_path(config_dir, org_id).exists():
                self._last_org_load_error = "org_not_found"
                return False
        new_profile = "custom" if new_mode == "org" else self._normalize_session_company_profile(new_profile)
        self._exec_mode = new_mode
        self._company_profile = new_profile
        self._task_preferred_agent = new_preferred_agent
        if hasattr(self, "services_context"):
            self.services_context.mode_state.exec_mode = self._exec_mode
            self.services_context.mode_state.company_profile = self._company_profile
            self.services_context.mode_state.task_preferred_agent = self._task_preferred_agent
        await self._persist_mode()

        # Sync company_profile in config so _effective_roles() uses the right mode
        if sync_config and self.engine.org_engine:
            async with self._config_lock:
                if new_mode == "org":
                    loaded = self._load_active_org_config_into_engine(org_id)
                    if not loaded:
                        self._exec_mode = previous_mode
                        self._company_profile = previous_profile
                        self._task_preferred_agent = previous_preferred_agent
                        if hasattr(self, "services_context"):
                            self.services_context.mode_state.exec_mode = self._exec_mode
                            self.services_context.mode_state.company_profile = self._company_profile
                            self.services_context.mode_state.task_preferred_agent = self._task_preferred_agent
                        await self._persist_mode()
                        return False
                    elif org_id:
                        await self._set_active_saved_org_name(org_id)
                elif new_mode == "company":
                    self._restore_company_config_into_engine(new_profile)
                else:
                    self._restore_company_config_into_engine("")
                # Mode switching persists only the selected UI mode. It must not
                # rewrite company/org architecture files.

        # Reload agents for the new mode
        if self.engine.org_engine:
            preset = self._resolve_preset_name()
            await self.agent_store.load_preset(preset, self.engine.org_engine)

        await self._prune_stale_agent_store_entries()
        if self._exec_mode in {"org", "custom"}:
            await self._ensure_custom_role_agents()

        # Sync role→agent mapping for EventAdapter
        await self._sync_role_map()

        # B4: Clean orphan references — clear assigned_to on pending tasks
        # whose role no longer exists, and prune stale DM channels
        new_agents = await self.agent_store.get_all()
        valid_agent_ids = {a["agent_id"] for a in new_agents}
        valid_role_ids = {a.get("opc_role_id", a["agent_id"]) for a in new_agents}
        if self.engine.store:
            pending_tasks = await self.engine.store.get_tasks(project_id=self.engine.project_id or "default")
            for task in pending_tasks:
                if task.assigned_to and task.assigned_to not in valid_role_ids:
                    task.assigned_to = ""
                    await self.engine.store.save_task(task)
        await self.chat_store.prune_stale_channels(valid_agent_ids, project_id=self.engine.project_id or "default")
        # Ensure activity channel exists (session channels are on-demand)
        await self.chat_store.ensure_activity_channel(project_id=self.engine.project_id or "default")

        # Broadcast snapshot + full collab_sync so frontend refreshes everything
        snapshot = await build_snapshot(
            self.engine, self.agent_store, self.chat_store, self.event_adapter
        )
        snapshot["exec_mode"] = self._exec_mode
        snapshot["company_profile"] = self._company_profile
        snapshot["task_preferred_agent"] = self._task_preferred_agent
        await self.broadcast({"type": "snapshot", "payload": snapshot})

        collab = await build_collab_sync(
            self.engine,
            self.agent_store,
            self.chat_store,
            self.event_adapter,
            exec_mode=self._exec_mode,
        )
        await self.broadcast({"type": "collab_sync_push", "payload": collab})
        await self._broadcast_org_info()
        return True

    async def _prune_stale_agent_store_entries(self) -> None:
        if not self.engine.org_engine or not hasattr(self, "agent_store"):
            return

        effective_role_ids = {agent.role_id for agent in self.engine.org_engine.list_agents()}
        try:
            effective_employee_ids = {employee.employee_id for employee in self.engine.config.org.employees}
        except Exception:
            effective_employee_ids = set()

        for stale in await self.agent_store.get_all():
            emp_id = str(stale.get("employee_id") or "").strip()
            role_id = str(stale.get("opc_role_id") or stale.get("agent_id") or "").strip()
            is_stale = (
                (emp_id and emp_id not in effective_employee_ids)
                or (not emp_id and role_id and role_id not in effective_role_ids)
            )
            if is_stale:
                try:
                    await self.agent_store.remove_agent(stale["agent_id"])
                except Exception:
                    logger.debug(f"Failed to prune stale agent {stale.get('agent_id')}")

        if self._exec_mode in {"org", "custom"}:
            await self.agent_store.sync_custom_shadow()

    def _target_mode_for_profile(self, profile: str | None) -> tuple[str | None, str | None]:
        if profile == "custom":
            return "org", "custom"
        if profile == "corporate":
            return "company", "corporate"
        return None, None

    def _rebind_engine_config(self, config: Any) -> None:
        self.engine.config = config
        org_engine = getattr(self.engine, "org_engine", None)
        if org_engine is not None:
            org_engine.config = config
        talent_market = getattr(self.engine, "talent_market", None)
        if talent_market is not None:
            talent_market.config = config
        if hasattr(self.engine, "_runtime_config_signature"):
            self.engine._runtime_config_signature = None

    def _restore_company_config_into_engine(self, company_profile: str) -> None:
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        try:
            loaded_config = OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()
        except Exception as exc:
            logger.warning(f"Failed to reload company architecture after leaving org mode: {exc}")
            loaded_config = self.engine.config
        loaded_config.org.company_profile = company_profile
        self._rebind_engine_config(loaded_config)
        if self.engine.org_engine:
            self.engine.org_engine.reload_from_config()
            configure_tools = getattr(self.engine.org_engine, "configure_task_mode_tools", None)
            task_tools = getattr(self.engine, "_task_mode_tool_names", None)
            if callable(configure_tools) and callable(task_tools):
                configure_tools(task_tools())

    def _load_active_org_config_into_engine(self, organization_id: str | None = None) -> bool:
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        self._last_org_load_error = ""
        try:
            payload, source_path = load_org_config_payload(config_dir, organization_id)
            loaded_config = apply_org_config_payload_to_config(
                self.engine.config,
                payload,
                source_path=source_path,
            )
            validate_runnable_org_config(loaded_config, organization_id=organization_id or "")
        except FileNotFoundError:
            self._last_org_load_error = "org_not_found"
            return False
        except Exception as exc:
            self._last_org_load_error = str(exc)
            logger.warning(f"Failed to load org architecture for org mode: {exc}")
            return False
        self._rebind_engine_config(loaded_config)
        if self.engine.org_engine:
            self.engine.org_engine.reload_from_config()
        return True

    def _resolve_preset_name(self) -> str:
        """Map current mode to agent_store preset name.

        task    → "single" (1 executor)
        company → profile name ("corporate")
        org     → "custom" (user-managed agents)
        """
        if self._exec_mode in {"org", "custom"}:
            return "custom"
        if self._exec_mode == "company":
            return self._company_profile  # "corporate"
        return "single"  # task mode uses single-agent preset

    # ── Cross-Office ──────────────────────────────────────────────────

    # ── Workload ─────────────────────────────────────────────────────

    # ══════════════════════════════════════════════════════════════════════
    # Private helpers
    # ══════════════════════════════════════════════════════════════════════

    def _controller_active_task_run_registry(self) -> Any | None:
        root_engine = getattr(self, "_root_engine", None) or getattr(self, "engine", None)
        registry = getattr(root_engine, "_active_task_run_registry", None)
        return registry if isinstance(registry, ActiveTaskRunRegistry) else None

    def _release_current_execution_handoff(self) -> None:
        registry = self._controller_active_task_run_registry()
        release = getattr(registry, "release_current_handoff", None)
        if callable(release):
            release()

    def _track(self, coro: Any) -> asyncio.Task[Any]:
        """Create a tracked background task that auto-removes itself on completion."""
        handoff_registry = self._controller_active_task_run_registry()
        handoff_token = (
            handoff_registry.retain_current_handoff()
            if handoff_registry is not None
            else None
        )
        try:
            task = asyncio.create_task(coro)
        except BaseException:
            if handoff_registry is not None and handoff_token is not None:
                handoff_registry.release_handoff(handoff_token)
            if inspect.iscoroutine(coro):
                coro.close()
            raise
        if handoff_registry is not None and handoff_token is not None:
            def _release_execution_handoff(
                done_task: asyncio.Task[Any],
                *,
                registry: ActiveTaskRunRegistry = handoff_registry,
                token: str = handoff_token,
            ) -> None:
                registry.release_handoff(token)
                task_context = getattr(self, "_task_bg_context", None)
                if isinstance(task_context, dict):
                    context = task_context.get(done_task)
                    if isinstance(context, dict):
                        context.pop("execution_handoff_token", None)
                        if not context:
                            task_context.pop(done_task, None)

            task.add_done_callback(
                _release_execution_handoff
            )
            # This task owns the accepted engine handoff even before callers
            # such as _track_session add their richer session context.  Keep it
            # in the existing execution-owned task index so shutdown cannot
            # close the store while its cancellation cleanup is still running.
            task_context = getattr(self, "_task_bg_context", None)
            if isinstance(task_context, dict):
                context = task_context.setdefault(task, {})
                context["execution_handoff_token"] = handoff_token
        # Work scheduled after the gate closes is rejected.  A coroutine
        # retained by a pre-shutdown reservation is the one exception: it must
        # reach registry.register (or exit) before checkpoint snapshotting.
        if self._shutting_down and handoff_token is None:
            task.cancel()
        self._background_tasks.add(task)
        task.add_done_callback(self._on_bg_task_done)
        return task

    def _cancel_client_project_index(self, ws: Any) -> None:
        task = self._client_project_index_tasks.pop(ws, None)
        if task is not None and not task.done():
            task.cancel()

    def _track_client_project_index(
        self,
        ws: Any,
        coro: Any,
    ) -> asyncio.Task[Any]:
        self._cancel_client_project_index(ws)
        task = self._track(coro)
        self._client_project_index_tasks[ws] = task

        def _cleanup(done: asyncio.Task[Any]) -> None:
            if self._client_project_index_tasks.get(ws) is done:
                self._client_project_index_tasks.pop(ws, None)

        task.add_done_callback(_cleanup)
        return task

    def _track_client_initial_state(
        self,
        ws: Any,
        coro: Any,
    ) -> asyncio.Task[Any]:
        prior = self._client_initial_state_tasks.pop(ws, None)
        if prior is not None and not prior.done():
            prior.cancel()
        task = self._track(coro)
        self._client_initial_state_tasks[ws] = task

        def _cleanup(done: asyncio.Task[Any]) -> None:
            if self._client_initial_state_tasks.get(ws) is done:
                self._client_initial_state_tasks.pop(ws, None)

        task.add_done_callback(_cleanup)
        return task

    def _track_session(
        self,
        task_id: str,
        coro: Any,
        *,
        project_id: str | None = None,
        engine: Any | None = None,
    ) -> asyncio.Task[Any]:
        """Like _track but keeps all live tasks for explicit cancellation."""
        bg = self._track(coro)
        context = dict(self._task_bg_context.get(bg) or {})
        context.update({
            "task_id": task_id,
            "project_id": self._normalize_project_id(project_id or getattr(engine, "project_id", None)),
            "engine": engine,
        })
        self._task_bg_context[bg] = context
        task_group = self._task_bg_map.setdefault(task_id, set())
        task_group.add(bg)
        bg.add_done_callback(lambda t: self._discard_session_bg_task(task_id, t))
        return bg

    def _discard_session_bg_task(self, task_id: str, task: asyncio.Task[Any]) -> None:
        self._task_bg_context.pop(task, None)
        task_group = self._task_bg_map.get(task_id)
        if task_group is None:
            return
        task_group.discard(task)
        if not task_group:
            self._task_bg_map.pop(task_id, None)

    def _cancel_session_tasks(self, task_id: str) -> None:
        task_group = self._task_bg_map.pop(task_id, set())
        for bg_task in list(task_group):
            try:
                if not bg_task.done():
                    bg_task.cancel()
            except Exception:
                logger.opt(exception=True).debug(f"Failed to cancel background task for {task_id}")

    @staticmethod
    def _is_ws_disconnect_error(exc: BaseException) -> bool:
        message = str(exc or "").strip().lower()
        if type(exc).__name__ in {"ClientConnectionResetError", "ConnectionResetError"}:
            return True
        return any(
            token in message
            for token in (
                "cannot write to closing transport",
                "closing transport",
                "websocket connection is closed",
                "connection reset by peer",
                "broken pipe",
            )
        )

    @staticmethod
    def _is_closed_database_error(exc: BaseException) -> bool:
        message = str(exc or "").strip().lower()
        return any(
            token in message
            for token in (
                "cannot operate on a closed database",
                "closed database",
                "no active connection",
            )
        )

    def _is_expected_shutdown_error(self, exc: BaseException) -> bool:
        return self._shutting_down and (
            self._is_ws_disconnect_error(exc) or self._is_closed_database_error(exc)
        )

    @staticmethod
    def _ws_flag_is_set(value: Any) -> bool:
        return isinstance(value, bool) and value

    @classmethod
    def _ws_is_open(cls, ws: Any) -> bool:
        return not (
            cls._ws_flag_is_set(getattr(ws, "closed", False))
            or cls._ws_flag_is_set(getattr(ws, "closing", False))
        )

    async def _safe_send_json(self, ws: Any, payload: dict[str, Any]) -> bool:
        if not self._ws_is_open(ws):
            self._clients.discard(ws)
            return False
        try:
            result = ws.send_json(payload)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            if self._is_ws_disconnect_error(exc) or self._shutting_down:
                self._clients.discard(ws)
                logger.debug(f"Skipped WS send during disconnect/shutdown: {type(exc).__name__}: {exc!r}")
                return False
            raise

    async def shutdown(self, timeout: float = 15.0) -> None:
        """Checkpoint owned runtimes, then stop all WS work before DB shutdown."""
        import aiohttp

        self._shutting_down = True
        pending_handlers = [
            task
            for task in self._active_message_tasks
            if task is not asyncio.current_task() and not task.done()
        ]

        def execution_owned_tasks() -> set[asyncio.Task[Any]]:
            owned = set(self._task_bg_context)
            for task_group in self._task_bg_map.values():
                owned.update(task_group)
            return {
                task
                for task in owned
                if task is not asyncio.current_task() and not task.done()
            }

        # A preaccepted handler can replace its handoff reservation with the
        # real execution task while the admission barrier drains.  Union this
        # pre-barrier view with the post-checkpoint view so neither owner can
        # disappear from the cancellation set during that handoff.
        execution_tasks_before_prepare = execution_owned_tasks()

        # Atomically close execution admission, then wait only for requests
        # accepted before that close to either register their first real engine
        # coroutine or exit.  There is intentionally no timeout window here:
        # registration drains the reservation, so long execution is not part
        # of this wait.
        active_run_registry = self._controller_active_task_run_registry()
        close_and_wait = getattr(
            active_run_registry,
            "close_admission_and_wait_for_handoffs",
            None,
        )
        if callable(close_and_wait):
            # Close synchronously, then cancel accepted requests which are
            # still queued before their first engine registration.  In
            # particular, a duplicate Continue can be waiting behind the
            # per-runtime reply lock for the entire first execution; waiting
            # for that reservation here would deadlock shutdown before it can
            # checkpoint and cancel the live owner.
            active_run_registry.close_admission()
            is_handoff_pending = getattr(
                active_run_registry,
                "is_handoff_pending",
                None,
            )
            pending_handoff_tasks: set[asyncio.Task[Any]] = set()
            pending_handoff_tokens: set[str] = set()
            if callable(is_handoff_pending):
                for task, context in list(self._task_bg_context.items()):
                    token = (
                        context.get("execution_handoff_token")
                        if isinstance(context, dict)
                        else None
                    )
                    if (
                        task is not asyncio.current_task()
                        and not task.done()
                        and is_handoff_pending(token)
                    ):
                        pending_handoff_tasks.add(task)
                        pending_handoff_tokens.add(str(token))
                handoff_routes = getattr(self, "_handoff_route_tasks", {})
                if isinstance(handoff_routes, dict):
                    for task, token in list(handoff_routes.items()):
                        if (
                            task is not asyncio.current_task()
                            and not task.done()
                            and is_handoff_pending(token)
                        ):
                            pending_handoff_tasks.add(task)
                            pending_handoff_tokens.add(token)
            for task in pending_handoff_tasks:
                task.cancel()
            revoke_handoff = getattr(active_run_registry, "revoke_handoff", None)
            if callable(revoke_handoff):
                for token in pending_handoff_tokens:
                    revoke_handoff(token)
            await close_and_wait()

        # The controller registry is the only source of truth for executions
        # owned by this process.  Persist one checkpoint per active company
        # scope while every execution coroutine and its store are still alive.
        root_engine = getattr(self, "_root_engine", None) or self.engine
        try:
            await root_engine.prepare_active_company_runtimes_for_shutdown()
        except Exception:
            logger.opt(exception=True).error(
                "Refusing to cancel WS execution because company runtime checkpointing failed"
            )
            raise

        # No execution coroutine may get another scheduling turn after its
        # checkpoint/holds are durable.  Build the cancellation set and set
        # every cancellation flag synchronously, before awaiting progress
        # flushing, client close handshakes, or any other UI cleanup.
        execution_tasks = execution_tasks_before_prepare | execution_owned_tasks()
        tracked_tasks = set(self._background_tasks)
        tracked_tasks.update(execution_tasks)
        tracked_tasks.update(pending_handlers)
        tracked_tasks = {
            task
            for task in tracked_tasks
            if task is not asyncio.current_task() and not task.done()
        }
        for task in tracked_tasks:
            task.cancel()
        if self._progress_flush_task and not self._progress_flush_task.done():
            self._progress_flush_task.cancel()

        # Await execution cleanup first so broker finally blocks reap process
        # groups while the engine/store are guaranteed to remain available.
        if execution_tasks:
            _done, still_cleaning = await asyncio.wait(
                execution_tasks,
                timeout=max(0.0, float(timeout)),
            )
            if still_cleaning:
                message = (
                    "Timed out waiting for "
                    f"{len(still_cleaning)} execution task(s) to finish cancellation cleanup"
                )
                logger.error(message)
                raise RuntimeError(message)

        if self._progress_flush_task:
            try:
                await self._progress_flush_task
            except (asyncio.CancelledError, Exception):
                pass
            self._progress_flush_task = None

        clients = list(self._clients)
        for ws in clients:
            try:
                await ws.close(
                    code=aiohttp.WSCloseCode.GOING_AWAY,
                    message=b"server shutting down",
                )
            except Exception as exc:
                if not self._is_ws_disconnect_error(exc):
                    logger.debug(f"Failed to close WS client cleanly: {type(exc).__name__}: {exc!r}")

        ui_background_tasks = tracked_tasks - execution_tasks
        if ui_background_tasks:
            _done, still_running = await asyncio.wait(
                ui_background_tasks,
                timeout=max(0.0, float(timeout)),
            )
            if still_running:
                logger.warning(
                    "Timed out waiting for {} non-execution WS background task(s) to stop",
                    len(still_running),
                )

    @staticmethod
    def _store_is_ready(store: Any | None) -> bool:
        """Treat initialized stores as ready, even without an explicit flag."""
        if not store:
            return False
        ready = getattr(store, "is_ready", None)
        if callable(ready):
            return bool(ready())
        if ready is None:
            return True
        return bool(ready)

    @staticmethod
    def _chat_store_is_ready(chat_store: Any | None) -> bool:
        if chat_store is None:
            return False
        ready = getattr(chat_store, "is_ready", None)
        if callable(ready):
            try:
                if not bool(ready()):
                    return False
            except Exception:
                return False
        elif ready is not None and not bool(ready):
            return False
        db = getattr(chat_store, "_db", None)
        if db is None:
            return False
        connection = getattr(db, "_connection", True)
        return connection is not None

    @staticmethod
    def _task_has_comms_workspace(task: Any | None) -> bool:
        if task is None:
            return False
        metadata = dict(getattr(task, "metadata", {}) or {})
        return bool(
            str(metadata.get("comms_workspace_root") or "").strip()
            or str(metadata.get("target_output_dir") or "").strip()
            or str(metadata.get("setup_workspace_prepared") or "").strip()
        )

    @staticmethod
    def _shared_root_ui_task_id(task: Any | None) -> str:
        if task is None:
            return ""
        metadata = dict(getattr(task, "metadata", {}) or {})
        if not bool(metadata.get("shared_role_session", False)):
            return ""
        session_id = str(getattr(task, "session_id", "") or "").strip()
        root_session_id = str(metadata.get("company_runtime_root_session_id", "") or "").strip()
        origin_task_id = str(metadata.get("origin_task_id", "") or "").strip()
        if origin_task_id and root_session_id and session_id == root_session_id:
            return origin_task_id
        return ""

    @staticmethod
    def _task_mode_origin_ui_task_id(task: Any | None) -> str:
        if task is None:
            return ""
        metadata = dict(getattr(task, "metadata", {}) or {})
        task_id = str(getattr(task, "id", "") or "").strip()
        origin_task_id = str(metadata.get("origin_task_id", "") or "").strip()
        if not origin_task_id or origin_task_id == task_id:
            return ""
        mode = str(metadata.get("mode", "") or "").strip().lower()
        exec_mode = str(metadata.get("exec_mode", "") or "").strip().lower()
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        task_mode_contract = str(metadata.get("task_mode_contract", "") or "").strip()
        if (
            mode == "task"
            or exec_mode in {"task", "project", "single"}
            or execution_mode in {"task", "task_mode", "project"}
            or task_mode_contract == "single_full_capability_main_agent"
        ):
            return origin_task_id
        return ""

    def _ui_task_id_for_task(self, task: Any | None) -> str:
        if task is None:
            return ""
        task_id = str(getattr(task, "id", "") or "").strip()
        ui_task_id = self._shared_root_ui_task_id(task) or self._task_mode_origin_ui_task_id(task) or task_id
        if task_id and ui_task_id and ui_task_id != task_id:
            self._ui_task_aliases[task_id] = ui_task_id
        return ui_task_id

    async def _ui_task_id_for_runtime_task_id(self, task_id: str | None, *, engine: Any | None = None) -> str:
        raw_task_id = str(task_id or "").strip()
        if not raw_task_id:
            return ""
        mapped = str(self._ui_task_aliases.get(raw_task_id) or "").strip()
        if mapped:
            return mapped
        runtime_engine = engine or self.engine
        store = getattr(runtime_engine, "store", None)
        get_task = getattr(store, "get_task", None)
        if callable(get_task):
            try:
                task = await get_task(raw_task_id)
            except Exception:
                task = None
            mapped = self._ui_task_id_for_task(task)
            if mapped:
                return mapped
        return raw_task_id

    def _on_bg_task_done(self, task: asyncio.Task[Any]) -> None:
        """Callback for tracked background tasks: cleanup + log unhandled errors.

        For session tasks, also broadcasts an error message and idle status
        so the frontend doesn't stay stuck on "thinking" forever.
        """
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.opt(exception=exc).error(f"Background task failed: {exc}")
            # Notify frontend for session tasks so UI doesn't stay stuck on "thinking"
            context = self._task_bg_context.get(task) or {}
            task_id = self._find_task_id_for_bg_task(task)
            if task_id:
                asyncio.ensure_future(self._notify_session_bg_failure(
                    task_id,
                    exc,
                    project_id=str(context.get("project_id") or "").strip() or None,
                ))

    def _find_task_id_for_bg_task(self, bg_task: asyncio.Task[Any]) -> str | None:
        """Find the task_id associated with a background task."""
        context = self._task_bg_context.get(bg_task)
        if context:
            task_id = str(context.get("task_id", "") or "").strip()
            if task_id:
                return task_id
        for tid, task_group in self._task_bg_map.items():
            if bg_task in task_group:
                return tid
        return None

    async def _notify_session_bg_failure(
        self,
        task_id: str,
        exc: BaseException,
        *,
        project_id: str | None = None,
    ) -> None:
        """Broadcast error message + idle status for a failed session background task."""
        try:
            channel_id = f"session:{task_id}"
            pid = self._normalize_project_id(project_id or self.engine.project_id)
            msg = await self.chat_store.insert_message(
                channel_id=channel_id,
                sender="system",
                sender_name="OPC",
                content=f"Error: {exc}",
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})
            await self.broadcast({"type": "board_task_status_changed", "payload": {
                "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "failed",
            }})
            idle_payload: dict[str, Any] = {
                "project_id": pid,
                "task_id": task_id, "status": "idle", "current_tool": None, "iteration": 0,
            }
            resolved_agent = self._resolve_agent_for_idle(task_id)
            if resolved_agent:
                idle_payload["agent_id"] = resolved_agent
            await self.broadcast({"type": "agent_runtime_update", "payload": idle_payload})
        except Exception as notify_exc:
            logger.debug(f"Failed to notify frontend of session bg failure: {notify_exc}")

    async def _send_ack(self, ws: Any, ok: bool = True, **extra: Any) -> None:
        payload: dict[str, Any] = {"ok": ok, **extra}
        await self._safe_send_json(ws, {"type": "ack", "payload": payload})

    async def _publish_service_result(self, result: ServiceResult) -> None:
        for event in result.events:
            await self.broadcast({"type": event.type, "payload": event.payload})

    async def _send_service_ack(self, ws: Any, result: ServiceResult, **extra: Any) -> None:
        payload = dict(result.payload or {})
        payload.update(extra)
        ok = bool(payload.pop("ok", True))
        await self._send_ack(ws, ok=ok, **payload)

    async def _send_service_error(self, ws: Any, exc: ServiceError, *, action: str | None = None) -> None:
        payload = exc.to_payload()
        payload.pop("ok", None)
        if action:
            payload["action"] = action
        await self._send_ack(ws, ok=False, **payload)

    async def _refresh_runtime_control_for_client(
        self,
        ws: Any,
        *,
        engine: Any,
        project_id: str,
    ) -> None:
        """Replace optimistic Continue state with a durable snapshot."""
        try:
            collab = await build_collab_sync(
                engine,
                self.agent_store,
                self.chat_store,
                self.event_adapter,
                exec_mode=self._exec_mode,
            )
            collab["project_id"] = self._normalize_project_id(project_id)
            await self._safe_send_json(ws, {"type": "collab_sync_push", "payload": collab})
        except Exception:
            logger.opt(exception=True).debug("failed to refresh runtime control after rejected Continue")

    # UI profile name → engine CompanyProfile name
    _PROFILE_TO_ENGINE: dict[str, str] = {"classic": "corporate"}

    def _normalize_session_exec_mode(self, value: Any) -> str:
        return SessionService.normalize_exec_mode(value)

    def _normalize_session_company_profile(self, value: Any) -> str:
        return self._ensure_office_services().session.normalize_company_profile(value)

    def _normalize_session_preferred_agent(self, value: Any, default: str = "native") -> str:
        return SessionService.normalize_preferred_agent(value, default=default)

    def _normalize_session_org_id(self, value: Any) -> str:
        return SessionService.normalize_org_id(value)

    def _resolve_task_session_config(self, task: Any | None) -> tuple[str, str]:
        return self._ensure_office_services().session.resolve_task_session_config(task)

    def _resolve_task_identity(self, task: Any | None, **defaults: Any) -> Any:
        return self._ensure_office_services().session.resolve_task_identity(task, **defaults)

    def _resolve_task_org_id(self, task: Any | None) -> str:
        return self._ensure_office_services().session.resolve_task_org_id(task)

    @staticmethod
    def _is_company_session_exec_mode(exec_mode: Any) -> bool:
        return str(exec_mode or "").strip().lower() in {"company", "org", "custom"}

    def _resolve_task_preferred_agent(self, task: Any | None) -> str:
        return self._ensure_office_services().session.resolve_task_preferred_agent(task)

    def _resolve_task_selected_execution_agent(self, task: Any | None) -> str:
        return self._ensure_office_services().session.resolve_task_selected_execution_agent(task)

    async def _session_config_lock_reason(self, task: Any, project_id: str) -> str:
        """Return a reason when a session's execution config can no longer change."""
        return await self._ensure_office_services().session.session_config_lock_reason(task, project_id)

    async def _persist_session_config(
        self,
        task: Any,
        *,
        exec_mode: str,
        company_profile: str,
        preferred_agent: str,
        org_id: str = "",
        engine: Any | None = None,
    ) -> None:
        await self._ensure_office_services().session.persist_session_config(
            task,
            exec_mode=exec_mode,
            company_profile=company_profile,
            preferred_agent=preferred_agent,
            org_id=org_id,
            engine=engine,
        )
    def _resolve_engine_mode(self, mode: str | None = None, profile: str | None = None) -> tuple[str, str | None]:
        """Resolve UI execution mode into (engine_mode, company_profile).

        UI modes → engine API:
          "task"    → mode="project", company_profile=None
          "company" → mode="company", company_profile=profile
          "org"     → mode="org", company_profile="custom"
        UI profile "classic" maps to engine CompanyProfile "corporate".
        """
        mode = mode or self._exec_mode
        profile = profile or self._company_profile
        if mode == "company":
            engine_profile = self._PROFILE_TO_ENGINE.get(profile, profile) if profile else None
            return "company", engine_profile
        if mode in {"org", "custom"}:
            return "org", "custom"
        return "project", None

    # ── Session Handlers ──────────────────────────────────────────────

    # ── Checkpoint metadata extraction ──────────────────────────────────

    # ── Secretary ──────────────────────────────────────────────────────

    # ── Project Management ──────────────────────────────────────────────

    # ── Org Info handler ─────────────────────────────────────────────

    # ── Phase 4: Talent Market ──────────────────────────────────────────

    # ── Phase 4: Employee Detail ────────────────────────────────────────

    # ── Phase 4: Reorg Management ───────────────────────────────────────

    # ------------------------------------------------------------------
    # Org config import / export
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # OPC Market handlers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Org editing handlers (custom mode)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Data management handlers (file library + data export)
    # ------------------------------------------------------------------

    async def _handle_file_library_list(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.file_library.list_files(
                folder=data.get("folder"),
                tags=data.get("tags", ""),
                uploaded_by=data.get("uploaded_by", ""),
                limit=int(data.get("limit", 100)),
                offset=int(data.get("offset", 0)),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="file_library_list")

    async def _handle_file_library_folders(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.file_library.list_folders()
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="file_library_folders")

    async def _handle_file_library_upload(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.file_library.upload(
                filename=data.get("filename", ""),
                content_base64=data.get("content_base64", ""),
                folder=data.get("folder", ""),
                tags=data.get("tags", ""),
                description=data.get("description", ""),
                uploaded_by=data.get("uploaded_by", ""),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="file_library_upload")

    async def _handle_file_library_download(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.file_library.download(file_id=data.get("file_id", ""))
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="file_library_download")

    async def _handle_file_library_delete(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.file_library.delete(file_id=data.get("file_id", ""))
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="file_library_delete")

    async def _handle_file_library_search(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.file_library.search(
                query=data.get("query", ""),
                limit=int(data.get("limit", 50)),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="file_library_search")

    async def _handle_data_export_summary(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.data_export.get_summary(
                project_id=data.get("project_id", ""),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="data_export_summary")

    async def _handle_data_export_query(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.data_export.query_tasks(
                project_id=data.get("project_id", ""),
                status=data.get("status", ""),
                assigned_to=data.get("assigned_to", ""),
                limit=int(data.get("limit", 100)),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="data_export_query")

    async def _handle_data_export_snapshot(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.data_export.export_snapshot(
                project_id=data.get("project_id", ""),
                format=data.get("format", "json"),
                include_work_items=bool(data.get("include_work_items", True)),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="data_export_snapshot")

    async def _handle_data_export_list(self, ws: Any, data: dict) -> None:
        try:
            result = await self.services.data_export.list_exports(
                limit=int(data.get("limit", 50)),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="data_export_list")

    # Handler routing table
    _HANDLERS: dict[str, Any] = {
        "ping":                "_handle_ping",
        "collab_sync":         "_handle_collab_sync",
        "project_index":       "_handle_project_index",
        "kanban_create_board": "_handle_kanban_create_board",
        "kanban_create_task":  "_handle_kanban_create_task",
        "kanban_update_task":  "_handle_kanban_update_task",
        "kanban_move_task":    "_handle_kanban_move_task",
        "kanban_delete_task":  "_handle_kanban_delete_task",
        "kanban_delete_board": "_handle_kanban_delete_board",
        "kanban_assign":       "_handle_kanban_assign",
        "kanban_status":       "_handle_kanban_status",
        "create_agent":        "_handle_create_agent",
        "delete_agent":        "_handle_delete_agent",
        "list_agents":         "_handle_list_agents",
        "move_agent":          "_handle_move_agent",
        "set_execution_mode":  "_handle_set_mode",
        "run_task":            "_handle_run_task",
        "cross_office_collab": "_handle_cross_office",
        "agent_workload":      "_handle_agent_workload",
        "kanban_switch_view":  "_handle_kanban_switch_view",
        "get_agent_detail":    "_handle_get_agent_detail",
        # Session handlers
        "create_session":      "_handle_create_session",
        "session_update_config": "_handle_session_update_config",
        "session_detail":      "_handle_session_detail",
        "runtime_logs":        "_handle_runtime_logs",
        "session_send":        "_handle_session_send",
        "session_stop":        "_handle_session_stop",
        "session_resume":      "_handle_session_resume",
        "session_delete":      "_handle_session_delete",
        "session_complete":    "_handle_session_complete",
        "session_update_title": "_handle_session_update_title",
        # Secretary handler
        "secretary_send":      "_handle_secretary_send",
        # Project management
        "list_projects":       "_handle_list_projects",
        "create_project":      "_handle_create_project",
        "delete_project":      "_handle_delete_project",
        "switch_project":      "_handle_switch_project",
        # Org info
        "org_info":            "_handle_org_info",
        # Phase 4: Talent Market, Employee Detail, Reorg
        "talent_import":       "_handle_talent_import",
        "talent_list":         "_handle_talent_list",
        "talent_scan_local":   "_handle_talent_scan_local",
        "talent_import_selected": "_handle_talent_import_selected",
        "talent_hire":         "_handle_talent_hire",
        "import_employee_as_agent": "_handle_import_employee_as_agent",
        "employee_detail":     "_handle_employee_detail",
        "reorg_list":          "_handle_reorg_list",
        "reorg_decide":        "_handle_reorg_decide",
        # OPC Market
        "market_browse":       "_handle_market_browse",
        "market_preview":      "_handle_market_preview",
        "market_apply_preset": "_handle_market_apply_preset",
        "market_list_installed": "_handle_market_list_installed",
        "market_export":       "_handle_market_export",
        "market_install":      "_handle_market_install",
        "market_uninstall":    "_handle_market_uninstall",
        # Org config import/export
        "org_config_export":   "_handle_org_config_export",
        "org_config_import":   "_handle_org_config_import",
        # Saved org architectures (named snapshots)
        "org_saved_list":      "_handle_org_saved_list",
        "org_saved_save_as":   "_handle_org_saved_save_as",
        "org_saved_create":    "_handle_org_saved_create",
        "org_saved_load":      "_handle_org_saved_load",
        "org_saved_delete":    "_handle_org_saved_delete",
        # Org editing (custom mode)
        "bulk_add_roles":      "_handle_bulk_add_roles",
        "add_role":            "_handle_add_role",
        "update_role":         "_handle_update_role",
        "update_org_strategy": "_handle_update_org_strategy",
        "delete_role":         "_handle_delete_role",
        "update_runtime_policy": "_handle_update_runtime_policy",
        "reset_architecture":  "_handle_reset_architecture",
        # Data management (file library + data export)
        "file_library_list":   "_handle_file_library_list",
        "file_library_folders": "_handle_file_library_folders",
        "file_library_upload": "_handle_file_library_upload",
        "file_library_download": "_handle_file_library_download",
        "file_library_delete": "_handle_file_library_delete",
        "file_library_search": "_handle_file_library_search",
        "data_export_summary": "_handle_data_export_summary",
        "data_export_query":   "_handle_data_export_query",
        "data_export_snapshot": "_handle_data_export_snapshot",
        "data_export_list":    "_handle_data_export_list",
    }

    # Register handlers defined after _HANDLERS class-level dict
    _HANDLERS["comms_state"] = "_handle_comms_state"
    _HANDLERS["comms_read_message"] = "_handle_comms_read_message"
    _HANDLERS["llm_config_get"] = "_handle_llm_config_get"
    _HANDLERS["llm_config_set"] = "_handle_llm_config_set"
    _HANDLERS["agent_config_get"] = "_handle_agent_config_get"
    _HANDLERS["agent_config_set"] = "_handle_agent_config_set"
