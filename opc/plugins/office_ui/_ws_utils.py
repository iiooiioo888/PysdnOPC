"""Shared utility functions for WebSocket handler mixins."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from opc.core.config import slugify_organization_name
from opc.core.org_config import (
    org_config_filename,
    org_configs_dir,
)


def _add_execution_turn_aliases(
    payload: dict[str, Any],
    runtime_task_id: Any | None = None,
) -> dict[str, Any]:
    """Add canonical UI aliases for runtime Task / execution turn identity."""
    task_id = str(
        runtime_task_id
        or payload.get("runtime_task_id")
        or payload.get("execution_turn_id")
        or payload.get("task_id")
        or ""
    ).strip()
    if task_id:
        payload.setdefault("runtime_task_id", task_id)
        payload.setdefault("execution_turn_id", task_id)
    return payload


def _is_cjk_title_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0x3040 <= cp <= 0x30FF
        or 0xAC00 <= cp <= 0xD7AF
    )


def _compact_session_title(content: str, *, max_units: int = 10, fallback: str = "New Chat") -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if not text:
        return fallback
    if max_units <= 0:
        return fallback

    units = 0
    index = 0
    cut_index = len(text)
    while index < len(text):
        ch = text[index]
        if ch.isspace():
            index += 1
            continue
        if _is_cjk_title_char(ch):
            units += 1
            index += 1
            if units == max_units:
                cut_index = index
                break
            continue
        if (ch.isalnum() or ch == "_") and not _is_cjk_title_char(ch):
            while (
                index < len(text)
                and (text[index].isalnum() or text[index] == "_")
                and not _is_cjk_title_char(text[index])
            ):
                index += 1
            units += 1
            if units == max_units:
                cut_index = index
                break
            continue
        index += 1

    compact = text[:cut_index].strip() or fallback
    has_more_units = any(
        _is_cjk_title_char(ch) or ((ch.isalnum() or ch == "_") and not _is_cjk_title_char(ch))
        for ch in text[cut_index:]
    )
    return f"{compact}..." if has_more_units else compact


def _normalize_escalation_key(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", value.strip()).strip("_").casefold()


def _normalize_escalation_reply(reply: str, options: list[dict[str, Any]]) -> str | None:
    raw_reply = str(reply or "").strip()
    if not raw_reply:
        return None

    normalized_map: dict[str, str] = {}
    for idx, option in enumerate(options, start=1):
        option_id = str(option.get("id", "")).strip()
        label = str(option.get("label", option_id)).strip()
        if not option_id:
            continue
        normalized_map[option_id.casefold()] = option_id
        normalized_map[_normalize_escalation_key(option_id)] = option_id
        if label:
            normalized_map[label.casefold()] = option_id
            normalized_map[_normalize_escalation_key(label)] = option_id
        normalized_map[str(idx)] = option_id

    alias_map = {
        "y": "approve_once",
        "yes": "approve_once",
        "approve": "approve_once",
        "allow": "approve_once",
        "session": "approve_session",
        "n": "deny",
        "no": "deny",
        "deny": "deny",
        "reject": "deny",
        "project": "always_project",
        "global": "always_global",
    }
    alias = alias_map.get(_normalize_escalation_key(raw_reply))
    if alias and alias in normalized_map.values():
        return alias
    return normalized_map.get(raw_reply.casefold()) or normalized_map.get(_normalize_escalation_key(raw_reply))


def _ui_message_identity_metadata(
    *,
    kind: str | None = None,
    message_id: str | None = None,
    conversation_turn_id: str | None = None,
    created_at: float | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if kind:
        metadata["kind"] = kind
    normalized_id = str(message_id or "").strip()
    if normalized_id:
        metadata["ui_message_id"] = normalized_id
    normalized_turn_id = str(conversation_turn_id or "").strip()
    if normalized_turn_id:
        metadata["conversation_turn_id"] = normalized_turn_id
        metadata["canonical_turn_id"] = normalized_turn_id
        metadata["turn_id"] = normalized_turn_id
    if created_at is not None:
        metadata["ui_created_at"] = float(created_at)
    return metadata


def _ui_conversation_turn_id(message_id: str | None) -> str:
    normalized_id = str(message_id or "").strip()
    if not normalized_id:
        return ""
    return f"ui-turn:{normalized_id}"


_GENERIC_ESCALATION_OPTIONS: list[dict[str, str]] = [
    {"id": "approve_once", "label": "Approve once"},
    {"id": "approve_session", "label": "Allow for this session"},
    {"id": "deny", "label": "Deny"},
    {"id": "always_project", "label": "Always allow for this project"},
    {"id": "always_global", "label": "Always allow globally"},
    {"id": "proceed", "label": "Proceed"},
    {"id": "abort", "label": "Abort"},
]


def _looks_like_escalation_reply(content: str) -> bool:
    return _normalize_escalation_reply(content, _GENERIC_ESCALATION_OPTIONS) is not None


_TASK_MODE_PREFERRED_AGENTS = frozenset({
    "native",
    "codex",
    "claude_code",
    "cursor",
    "opencode",
})

_PERSISTED_WORKER_NOTIFICATION_KINDS = frozenset({
    "idle",
    "task_complete",
    "blocked",
    "handoff_ready",
    "error",
    "permission_needed",
})

_RUNTIME_TASK_VISIBILITY_EVENT_TYPES = frozenset({
    "member_session_started",
    "member_claimed_work_item",
    "member_idle",
    "member_inbox_updated",
    "worker_notification",
})

_PROJECT_SCOPED_ENVELOPE_TYPES = frozenset({
    "snapshot",
    "event",
    "channel_created",
    "board_task_created",
    "board_task_moved",
    "board_task_status_changed",
    "session_runtime_control",
    "chat_new_message",
    "chat_channel_created",
    "kanban_updated",
    "kanban_board_created",
    "agent_runtime_update",
    "worker_notification",
    "execution_mode_resolved",
    "collab_sync_push",
    "project_index_push",
    "kanban_view_data",
    "session_created",
    "session_updated",
    "session_message",
    "session_title_updated",
    "session_deleted",
    "child_session_created",
    "session_progress",
    "work_item_progress",
    "project_run_updated",
    "seat_digest_updated",
    "work_item_batch_updated",
    "project_recovery_updated",
    "project_revision_created",
    "comms_state",
    "comms_message",
    "comms_state_dirty",
})

_SAVED_ORG_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_ACTIVE_SAVED_ORG_STATE_KEY = "active_saved_org"
_SAVED_ORG_NAME_LAX_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _saved_orgs_dir() -> Path:
    from opc.core.config import get_opc_home
    return org_configs_dir(get_opc_home() / "config")


def _saved_org_path(name: str, *, strict: bool = True) -> Path:
    raw = str(name or "").strip()
    pattern = _SAVED_ORG_NAME_RE if strict else _SAVED_ORG_NAME_LAX_RE
    if pattern.match(raw):
        org_id = raw.lower()
    elif not strict and raw:
        org_id = slugify_organization_name(raw)
    else:
        raise ValueError(f"Invalid saved-org name: {name!r}")
    return _saved_orgs_dir() / org_config_filename(org_id)
