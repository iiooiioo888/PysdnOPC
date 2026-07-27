"""Shared render helpers for the CLI board widgets."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from rich.text import Text

# ---------------------------------------------------------------------------
# Module-level display timezone management.
# Call ``set_display_timezone`` once at app startup; widgets use
# ``get_display_tz()`` to obtain the resolved tzinfo.
# ---------------------------------------------------------------------------
_display_tz: ZoneInfo | timezone = timezone.utc


def set_display_timezone(name: str) -> None:
    """Set the module-level display timezone from an IANA name (e.g. 'Asia/Taipei').

    Falls back to UTC if the name is empty or invalid.
    """
    global _display_tz
    name = (name or "").strip()
    if not name or name.upper() == "UTC":
        _display_tz = timezone.utc
        return
    try:
        _display_tz = ZoneInfo(name)
    except (KeyError, Exception):
        _display_tz = timezone.utc


def get_display_tz() -> ZoneInfo | timezone:
    """Return the currently configured display timezone."""
    return _display_tz

STATUS_STYLES = {
    "pending": "bold black on #64748b",
    "running": "bold black on #22c55e",
    "idle": "bold black on #38bdf8",
    "blocked": "bold black on #f59e0b",
    "awaiting_peer": "bold black on #f59e0b",
    "awaiting_review": "bold black on #f59e0b",
    "done": "bold black on #10b981",
    "failed": "bold white on #ef4444",
    "cancelled": "bold white on #9333ea",
    "reflecting": "bold black on #a78bfa",
    "tool_active": "bold black on #fb7185",
    "info": "bold black on #38bdf8",
    "warn": "bold black on #f59e0b",
    "error": "bold white on #ef4444",
}

PRIORITY_STYLES = {
    "urgent": "bold white on #dc2626",
    "high": "bold black on #fb7185",
    "medium": "bold black on #fbbf24",
    "low": "bold black on #60a5fa",
}

ROLE_STYLES = {
    "user": "bold #38bdf8",
    "assistant": "bold #22c55e",
    "system": "bold #f59e0b",
    "subagent": "bold #c084fc",
}


def truncate_text(value: str | None, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def humanize_age(
    timestamp: float | None,
    *,
    now: float | None = None,
    tz: ZoneInfo | timezone | None = None,
) -> str:
    """Return a compact relative-age string (e.g. '5m', '2h', '3d').

    The calculation uses UTC epoch arithmetic so it is inherently DST-safe.
    The *tz* parameter is accepted for API symmetry with :func:`format_clock`
    and does not affect the result (relative elapsed seconds are
    timezone-independent).
    """
    if not timestamp:
        return "n/a"
    if now is not None:
        current = float(now)
    else:
        # Use timezone-aware now to avoid naive-datetime pitfalls; the
        # resulting epoch is identical regardless of tz, but this keeps
        # the code path explicit and DST-safe.
        _tz = tz if tz is not None else timezone.utc
        current = datetime.now(_tz).timestamp()
    seconds = max(0, int(current - float(timestamp)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def format_clock(
    timestamp: float | None,
    *,
    tz: ZoneInfo | timezone | None = None,
) -> str:
    """Format an epoch timestamp as ``HH:MM`` in the given timezone.

    Parameters
    ----------
    timestamp:
        POSIX epoch seconds (UTC-based).
    tz:
        Target timezone for display.  Defaults to UTC when *None*.
        Accepts :class:`zoneinfo.ZoneInfo` (handles DST) or a fixed
        :class:`datetime.timezone` offset.

    The output uses the **local time of the specified timezone** and
    correctly reflects DST transitions when a :class:`ZoneInfo` instance
    is provided (e.g. ``ZoneInfo("America/New_York")``).
    """
    if not timestamp:
        return "--:--"
    _tz = tz if tz is not None else timezone.utc
    return datetime.fromtimestamp(float(timestamp), tz=_tz).strftime("%H:%M")


def badge(label: str, style: str, *, prefix: str = "") -> Text:
    text = Text()
    if prefix:
        text.append(prefix, style="dim")
    text.append(f" {label} ", style=style)
    return text


def status_style(status: str | None) -> str:
    return STATUS_STYLES.get(str(status or "").strip().lower(), "bold black on #475569")


def priority_style(priority: str | None) -> str:
    return PRIORITY_STYLES.get(str(priority or "").strip().lower(), "bold black on #475569")


def role_style(role: str | None) -> str:
    return ROLE_STYLES.get(str(role or "").strip().lower(), "bold white")


def adaptive_summary(metadata: dict[str, Any] | None) -> dict[str, Any]:
    meta = dict(metadata or {})
    adaptive = dict(meta.get("adaptive", {}) or {})
    if not adaptive:
        return {
            "state": "",
            "blocked_reason": "",
            "gate_owner": "",
            "missing_signals": [],
            "confidence_label": "",
            "invalidated": False,
        }
    work_item_profile = dict(adaptive.get("work_item_profile", {}) or {})
    missing_signals = [
        str(item.get("name", "") or "").strip()
        for item in list(adaptive.get("signals", []) or [])
        if isinstance(item, dict)
        and bool(item.get("required", True))
        and not bool(item.get("satisfied", False))
        and str(item.get("name", "") or "").strip()
    ]
    confidence = adaptive.get("confidence")
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = None
    return {
        "state": str(adaptive.get("normalized_state", "") or "").strip(),
        "blocked_reason": str(adaptive.get("blocked_reason", "") or "").strip(),
        "gate_owner": str(work_item_profile.get("gate_owner_role_id", "") or "").strip(),
        "missing_signals": missing_signals,
        "confidence_label": (
            f"{round(confidence_value * 100)}%"
            if confidence_value is not None
            else ""
        ),
        "invalidated": str(adaptive.get("normalized_state", "") or "").strip().lower() == "invalidated",
    }
