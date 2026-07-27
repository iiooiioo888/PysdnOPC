"""Shared constants and utility functions for company mode mixins.

This module avoids circular imports between company_mode.py and the
extracted mixin modules (_company_executor_dispatch.py, etc.).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opc.core.active_task_runs import ActiveTaskRunRegistry
from opc.core.models import TaskStatus
from opc.layer2_organization.data_acquisition_policy import (
    DEFAULT_ACQUISITION_EXECUTION_RECORD_RELATIVE_PATH,
)
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemGatePolicy,
    WorkItemProjectionSpec,
    deserialize_company_work_item_plan,
    serialize_company_work_item_plan,
)

if TYPE_CHECKING:
    from opc.core.models import Task


# ---------------------------------------------------------------------------
# Shared dataclasses
# ---------------------------------------------------------------------------

@dataclass
class CompanyExecutorRunState:
    """Mutable executor state for one top-level company run."""

    active_plan: CompanyWorkItemRuntimePlan | None = None
    active_tasks: list["Task"] = field(default_factory=list)
    dispatcher_wake: asyncio.Event = field(default_factory=asyncio.Event)
    kanban_dirty: bool = False
    kanban_broadcast_task: asyncio.Task[None] | None = None
    runtime_invariant_issue_keys: set[tuple[str, str, str, str]] = field(default_factory=set)


@dataclass(frozen=True)
class WorkItemOutputBundle:
    """Separated runtime audit and WorkItem-owned output metadata."""

    work_item_updates: dict[str, Any] = field(default_factory=dict)
    runtime_audit_updates: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass(frozen=True)
class CompanyExecutorDriverOwnership:
    """One registry attempt covering a complete company scheduler run."""

    registry: ActiveTaskRunRegistry
    project_id: str
    task_id: str
    attempt_token: str

    def bind(self):
        return self.registry.bind_driver_attempt(self.attempt_token)

    def release(self) -> bool:
        return self.registry.unregister(
            self.project_id,
            self.task_id,
            self.attempt_token,
        )


# ---------------------------------------------------------------------------
# Utility helpers for defensive coding patterns
# ---------------------------------------------------------------------------

def task_meta(task: "Task", key: str, default: Any = "") -> Any:
    """Safely access task.metadata[key] with a default.

    Replaces the verbose pattern: ``(task.metadata or {}).get(key, default)``
    """
    return (task.metadata or {}).get(key, default)


def safe_str(value: Any, default: str = "") -> str:
    """Convert value to stripped string, returning default if falsy.

    Replaces the verbose pattern: ``str(value or "").strip()``
    """
    return str(value or default).strip()


def meta_str(metadata: dict[str, Any] | None, key: str, default: str = "") -> str:
    """Safely get a string value from metadata dict.

    Combines metadata access and string conversion:
    ``str((metadata or {}).get(key, "") or "").strip()``
    """
    return str((metadata or {}).get(key, default) or default).strip()


def meta_bool(metadata: dict[str, Any] | None, key: str) -> bool:
    """Safely get a boolean value from metadata dict."""
    return bool((metadata or {}).get(key, False))


def meta_int(metadata: dict[str, Any] | None, key: str, default: int = 0) -> int:
    """Safely get an integer value from metadata dict."""
    try:
        return int((metadata or {}).get(key, default) or default)
    except (TypeError, ValueError):
        return default

# Maximum consecutive idle dispatcher ticks (5s each) tolerated while every
# active task waits on a human but at least one waiter has no pending
# checkpoint on record yet (e.g. a park write racing this snapshot).  Once
# exhausted the turn exits with a parked summary instead of spinning forever.
_HUMAN_WAIT_MAX_STALL_TICKS = 24

# Global wall-clock timeout (seconds) for _execute_multi_team_org_scoped.
# If the continuous-dispatch loop exceeds this budget the turn exits with a
# degraded summary instead of spinning indefinitely.
_MULTI_TEAM_ORG_WALL_CLOCK_TIMEOUT_SEC = 30.0

# Matches the manager dispatch guard's escape line while tolerating the
# markdown decoration models routinely wrap protocol tokens in — bold
# (`**NO_DELEGATION_JUSTIFICATION**:`), headings, quotes, list markers,
# `_`/`-`/space separator variants, and fullwidth colons.
_NO_DELEGATION_JUSTIFICATION_LINE = re.compile(
    r"^\s*(?:>+\s*)*"                     # blockquote markers
    r"(?:[-*+•]\s+|\d+[.)]\s+)?"          # list markers
    r"[\s#*_`~\"']*"                       # heading/bold/quote decoration
    r"NO[_\s-]?DELEGATION[_\s-]?JUSTIFICATION"
    r"[\s*_`~\"']*"                        # decoration between token and colon
    r"[:：]\s*(?P<reason>.*?)\s*$",
    re.IGNORECASE,
)


def review_work_item_id_for_attempt(worker_work_item_id: str, attempt: int) -> str:
    """Compute a per-attempt review work-item ID for a given worker."""
    wid = str(worker_work_item_id or "").strip()
    if not wid:
        raise ValueError("worker_work_item_id is required")
    n = int(attempt)
    if n < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return f"review::{wid}::v{n}"


def report_work_item_id_for_attempt(worker_work_item_id: str, attempt: int) -> str:
    """Compute a per-attempt report work-item ID for a given worker."""
    wid = str(worker_work_item_id or "").strip()
    if not wid:
        raise ValueError("worker_work_item_id is required")
    n = int(attempt)
    if n < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return f"report::{wid}::v{n}"


# Default cap for how many times a worker can be sent back for review rework
DEFAULT_MAX_REVIEW_REWORKS = 5

# Pre-delivery rework cap
DEFAULT_MAX_PRE_DELIVERY_REWORKS = 3

# Cap on reviewer verdict parse retries
MAX_VERDICT_PARSE_RETRIES = 2

_REVIEW_VERDICT_PARSE_RETRY_HINT = (
    "\n\n[REVIEW RETRY — Your previous verdict could not be parsed. The "
    "runtime needs an explicit approve/reject decision to drive the next "
    "step. Please end your turn with EXACTLY ONE JSON object on its own "
    "line in one of these shapes:\n\n"
    "  Approve:\n"
    '    {"review_verdict":"approve","summary":"<why this meets the bar>"}\n\n'
    "  Reject:\n"
    '    {"review_verdict":"reject","summary":"<why>",\n'
    '     "blocking_issues":["<specific change needed>"],\n'
    '     "followups":["<non-blocking improvement>"]}\n\n'
    "Without a parseable label this work item cannot make forward "
    "progress. After this retry the runtime will escalate to a human "
    "reviewer if the verdict is still unparseable.]"
)


EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT = """\
你是執行最終交付就緒度檢查的頂層公司高管。

返回嚴格 JSON：
{
  "deliverable": true,
  "summary": "簡短說明",
  "rework_targets": [
    {
      "target_projection_id": "精確的工作項目投影 id",
      "work_item_projection_id": "相同的精確工作項目投影 id",
      "role_id": "分配的角色 id",
      "feedback": "該工作項目的具體返工指示"
    }
  ]
}

規則：
- 只有在運行時確實就緒時，使用者才應收到面向業主的交付。
- 如果交付套件包含未解決的開放問題、失敗/阻斷的工作項目、被拒絕的審查或其他使工作未就緒的阻斷因素，設定 `deliverable=false`。
- 使用提供的角色/工作項目分配映射，使高管清楚知道誰負責每個部分。
- 針對應在其現有工作階段歷史中繼續工作的精確工作項目。
- 僅使用出現在提供的 work_item_tasks 數據中的投影 id。
- `summary` 和每個 `feedback` 必須簡潔且可操作。
- 僅返回 JSON。
"""

CEO_PRE_DELIVERY_ASSESSMENT_PROMPT = EXECUTIVE_PRE_DELIVERY_ASSESSMENT_PROMPT

_MAX_GATE_REVIEW_FEEDBACK_CHARS = 6000
_DEFAULT_CONTRACT_REWORK_MAX_RETRIES = 2
_WORKSPACE_BOOTSTRAP_PROJECTION_ID = "workspace_bootstrap"
_DATA_ACQUISITION_PROJECTION_ID = "data_acquisition"
_DEFAULT_WORKSPACE_LAYOUT = ("inputs", "deliverables", "work", ".openopc/manifests")
_DEFAULT_DATA_ACQUISITION_REPORT_PATH = "deliverables/data_acquisition_report.json"
_DEFAULT_DATA_ACQUISITION_LOG_PATH = "deliverables/data_acquisition_log.json"
_REVIEW_WAITING_STATUSES = {
    TaskStatus.AWAITING_MANAGER_REVIEW,
    TaskStatus.AWAITING_HUMAN,
    TaskStatus.AWAITING_REVIEW,
}

_COMPANY_RUNTIME_CONTROL_TASK_METADATA_KEYS = (
    "dispatch_hold",
    "company_runtime_stop_state",
    "company_runtime_stop_intent_id",
    "company_runtime_stop_marked_at",
    "company_runtime_suspend_checkpoint_type",
    "company_runtime_suspended_at",
)
_STALE_REWORK_TASK_METADATA_KEYS = (
    "rework_feedback",
    "review_feedback_version",
    "review_rework_count",
    "review_retry_hint",
    "review_retry_of_attempt",
    "review_retry_reason",
)
_WAITING_TASK_STATUSES = {
    *_REVIEW_WAITING_STATUSES,
    TaskStatus.AWAITING_PEER,
}
_DEFAULT_DATA_ACQUISITION_EXECUTION_RECORD_PATH = DEFAULT_ACQUISITION_EXECUTION_RECORD_RELATIVE_PATH
_CANONICAL_COORDINATION_SIGNALS = (
    "scope_locked",
    "inputs_ready",
    "env_ready",
    "implementation_ready",
    "qa_ready",
    "delivery_ready",
)


def _fallback_comms_root(target_output_dir: str | None) -> str | None:
    """Heuristic comms-root used when the engine did not pass one in."""
    if not target_output_dir:
        return None
    try:
        parent = str(Path(target_output_dir).expanduser().resolve().parent)
        if parent and parent not in {"/", "."}:
            return parent
    except Exception:
        pass
    return target_output_dir


def serialize_company_work_item_runtime_plan(plan: CompanyWorkItemRuntimePlan | None) -> dict[str, Any]:
    return serialize_company_work_item_plan(_coerce_company_work_item_runtime_plan(plan))


def deserialize_company_work_item_runtime_plan(data: dict[str, Any] | None) -> CompanyWorkItemRuntimePlan:
    return deserialize_company_work_item_plan(data)


_SERIALIZED_PLAN_MARKER_KEYS = ("projections", "seeds", "root_projection_id", "runtime_model")


def is_serialized_company_work_item_runtime_plan(data: Any) -> bool:
    """Whether ``data`` is a run-level serialized CompanyWorkItemRuntimePlan."""
    if not isinstance(data, dict) or not data:
        return False
    if "projection_id" in data:
        return False
    return any(key in data for key in _SERIALIZED_PLAN_MARKER_KEYS)


def serialized_company_plan_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the first metadata value that is a real serialized run-level plan."""
    source = dict(metadata or {})
    for key in ("company_work_item_plan", "work_item_runtime_plan"):
        candidate = source.get(key)
        if is_serialized_company_work_item_runtime_plan(candidate):
            return candidate
    return None


def _coerce_company_work_item_runtime_plan(plan: Any) -> CompanyWorkItemRuntimePlan | None:
    """Accept projection-plan-like test doubles without consuming obsolete plan fields."""
    if plan is None or isinstance(plan, CompanyWorkItemRuntimePlan):
        return plan
    projections: list[WorkItemProjectionSpec] = []
    for raw_projection in list(getattr(plan, "projections", []) or []):
        projection_id = str(
            getattr(raw_projection, "projection_id", "")
            or ""
        ).strip()
        if not projection_id:
            continue
        raw_gate = getattr(raw_projection, "gate_policy", None)
        gate_policy = None
        if raw_gate is not None:
            raw_gate_type = getattr(raw_gate, "gate_type", "review")
            gate_policy = WorkItemGatePolicy(
                gate_type=str(getattr(raw_gate_type, "value", raw_gate_type) or "review"),
                instructions=str(getattr(raw_gate, "instructions", "") or ""),
                reviewer_role=getattr(raw_gate, "reviewer_role", None),
                requires_human=bool(getattr(raw_gate, "requires_human", False)),
                on_reject=str(getattr(raw_gate, "on_reject", "") or "halt"),
                rework_projection_id=str(
                    getattr(raw_gate, "rework_projection_id", "")
                    or ""
                ).strip() or None,
                max_retries=int(getattr(raw_gate, "max_retries", 1) or 1),
                metadata=dict(getattr(raw_gate, "metadata", {}) or {}),
            )
        raw_strategy = getattr(raw_projection, "execution_strategy", "auto")
        projections.append(
            WorkItemProjectionSpec(
                projection_id=projection_id,
                turn_type=str(
                    getattr(raw_projection, "turn_type", "")
                    or "execute"
                ).strip().lower() or "execute",
                role_id=str(getattr(raw_projection, "role_id", "") or "").strip(),
                title=str(getattr(raw_projection, "title", "") or projection_id).strip(),
                summary=str(getattr(raw_projection, "summary", "") or "").strip(),
                dependency_projection_ids=[
                    str(item).strip()
                    for item in list(getattr(raw_projection, "dependency_projection_ids", []) or [])
                    if str(item).strip()
                ],
                execution_strategy=str(getattr(raw_strategy, "value", raw_strategy) or "auto"),
                preferred_external_agent=getattr(raw_projection, "preferred_external_agent", None),
                parallel_group=getattr(raw_projection, "parallel_group", None),
                gate_policy=gate_policy,
                metadata=dict(getattr(raw_projection, "metadata", {}) or {}),
            )
        )
    return CompanyWorkItemRuntimePlan(
        profile=str(getattr(plan, "profile", "") or "corporate").strip() or "corporate",
        root_projection_id=str(getattr(plan, "root_projection_id", "") or "").strip(),
        projections=projections,
        metadata=dict(getattr(plan, "metadata", {}) or {}),
    )
