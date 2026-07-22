"""Pure external provider-session identity helpers.

Monitoring rows exist before an external CLI reports its real resumable
thread/session id.  A synthetic ``agent:project:task`` id is useful for local
observability, but is never a provider resume capability.
"""

from __future__ import annotations

from typing import Any, Iterable


NON_RESUMABLE_EXTERNAL_SESSION_STATUSES: frozenset[str] = frozenset({
    "failed",
    "cancelled",
    "denied",
    "rejected",
    "hard_timeout",
    "idle_timeout",
    "startup_timeout",
})


def external_session_status_allows_resume(status: Any) -> bool:
    status = str(status or "").strip().lower()
    return status not in NON_RESUMABLE_EXTERNAL_SESSION_STATUSES


def external_session_allows_resume(session: Any | None) -> bool:
    return session is not None and external_session_status_allows_resume(
        getattr(session, "status", "")
    )


def is_provider_session_token(
    token: Any,
    *,
    agent_type: str,
    project_id: str,
) -> bool:
    value = str(token or "").strip()
    if not value:
        return False
    normalized_agent = str(agent_type or "").strip()
    normalized_project = str(project_id or "default").strip() or "default"
    if normalized_agent and value.startswith(
        f"{normalized_agent}:{normalized_project}:"
    ):
        return False
    if normalized_agent and value.startswith(f"{normalized_agent}:"):
        if len(value.split(":")) >= 3:
            return False
    return True


def provider_token_from_external_session(
    session: Any | None,
    *,
    agent_type: str,
    project_id: str,
) -> str:
    if not external_session_allows_resume(session):
        return ""
    metadata = dict(getattr(session, "metadata", {}) or {})
    for candidate in (
        metadata.get("resume_session_id"),
        metadata.get("provider_session_id"),
        getattr(session, "session_id", ""),
    ):
        token = str(candidate or "").strip()
        if is_provider_session_token(
            token,
            agent_type=agent_type,
            project_id=project_id,
        ):
            return token
    return ""


def external_session_matches_provider_token(
    session: Any | None,
    token: Any,
) -> bool:
    """Return whether a persisted row represents ``token`` regardless of status."""

    if session is None:
        return False
    expected = str(token or "").strip()
    if not expected:
        return False
    metadata = dict(getattr(session, "metadata", {}) or {})
    return expected in {
        str(candidate or "").strip()
        for candidate in (
            metadata.get("resume_session_id"),
            metadata.get("provider_session_id"),
            getattr(session, "session_id", ""),
        )
        if str(candidate or "").strip()
    }


def select_best_external_resume_session(
    sessions: Iterable[Any],
    *,
    agent_type: str,
    project_id: str,
) -> tuple[Any | None, str]:
    """Select the newest valid provider capability, ignoring placeholders."""

    valid: list[tuple[Any, str]] = []
    normalized_agent = str(agent_type or "").strip()
    for session in list(sessions or []):
        if (
            str(getattr(session, "agent_type", "") or "").strip()
            != normalized_agent
        ):
            continue
        token = provider_token_from_external_session(
            session,
            agent_type=normalized_agent,
            project_id=project_id,
        )
        if token:
            valid.append((session, token))
    if not valid:
        return None, ""

    def _sort_key(item: tuple[Any, str]) -> tuple[float, str]:
        session, token = item
        updated_at = getattr(session, "updated_at", None)
        try:
            timestamp = float(updated_at.timestamp())
        except Exception:
            timestamp = 0.0
        return timestamp, token

    return max(valid, key=_sort_key)
