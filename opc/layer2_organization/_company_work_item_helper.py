"""Company-mode work-item helper and spec builder.

Extracted from company_mode.py to reduce file size.  Contains:
- CompanyRuntimeSpec dataclass + serialize/deserialize helpers
- CompanyRuntimeWorkItemHelper: builds runtime coordination defaults
- CompanyRuntimeSpecBuilder: builds the lightweight pre-recruitment spec
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from opc.core.models import (
    AdaptiveRoleProfile,
    AdaptiveSignalSpec,
    AdaptiveWorkItemProfile,
    ArtifactContract,
    CoordinationSpec,
    RouterDecision,
    WorkItemExecutionStrategy,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)
from opc.layer2_organization.work_item_identity import work_item_identity_payload

_WORKSPACE_BOOTSTRAP_PROJECTION_ID = "workspace_bootstrap"
_DATA_ACQUISITION_PROJECTION_ID = "data_acquisition"


@dataclass
class CompanyRuntimeSpec:
    """Lightweight pre-runtime spec for company-mode recruitment/bootstrap."""

    profile: str = "corporate"
    original_request: str = ""
    runtime_model: str = "multi_team_org"
    work_item_driven: bool = True
    staffing_overrides: dict[str, str] = field(default_factory=dict)
    role_agent_overrides: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def serialize_company_runtime_spec(spec: CompanyRuntimeSpec | None) -> dict[str, Any]:
    if spec is None:
        return {}
    return {
        "profile": spec.profile,
        "original_request": spec.original_request,
        "runtime_model": spec.runtime_model,
        "work_item_driven": bool(spec.work_item_driven),
        "staffing_overrides": dict(spec.staffing_overrides or {}),
        "role_agent_overrides": dict(spec.role_agent_overrides or {}),
        "metadata": dict(spec.metadata or {}),
    }


def deserialize_company_runtime_spec(data: dict[str, Any] | None) -> CompanyRuntimeSpec:
    payload = dict(data or {})
    metadata = dict(payload.get("metadata", {}) or {})
    original_request = str(
        payload.get("original_request")
        or metadata.get("original_request")
        or ""
    )
    runtime_model = str(payload.get("runtime_model") or metadata.get("runtime_model") or "multi_team_org")
    work_item_driven = bool(payload.get("work_item_driven", metadata.get("work_item_driven", True)))
    metadata.setdefault("original_request", original_request)
    metadata.setdefault("runtime_model", runtime_model)
    metadata.setdefault("work_item_driven", work_item_driven)
    return CompanyRuntimeSpec(
        profile=str(payload.get("profile") or metadata.get("company_profile") or "corporate"),
        original_request=original_request,
        runtime_model=runtime_model,
        work_item_driven=work_item_driven,
        staffing_overrides={
            str(role_id).strip(): str(employee_id).strip()
            for role_id, employee_id in dict(payload.get("staffing_overrides", {}) or {}).items()
            if str(role_id).strip() and str(employee_id).strip()
        },
        role_agent_overrides={
            str(role_id).strip(): str(agent_name).strip()
            for role_id, agent_name in dict(payload.get("role_agent_overrides", {}) or {}).items()
            if str(role_id).strip() and str(agent_name).strip()
        },
        metadata=metadata,
    )


class CompanyRuntimeWorkItemHelper:
    """Builds runtime coordination defaults for company work items."""

    def __init__(self, org_engine: OrgEngine, llm: Any | None = None) -> None:
        self.org_engine = org_engine
        self.llm = llm

    @staticmethod
    def _dedupe_lines(items: list[str]) -> list[str]:
        seen: set[str] = set()
        deduped: list[str] = []
        for raw in items:
            item = str(raw or "").strip()
            if not item:
                continue
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    def _coerce_projection_assignment(
        self,
        data: dict[str, Any] | None,
        *,
        projection: WorkItemProjectionSpec,
        global_intent_summary: str,
    ) -> dict[str, Any]:
        fallback = self._default_projection_assignment(projection, global_intent_summary=global_intent_summary)
        data = dict(data or {})
        your_responsibility = str(
            data.get("your_responsibility") or fallback["your_responsibility"]
        ).strip() or fallback["your_responsibility"]
        out_of_scope = self._normalize_assignment_lines(
            data.get("out_of_scope"),
            fallback["out_of_scope"],
        )
        inputs = self._normalize_assignment_lines(
            data.get("inputs"),
            fallback["inputs"],
        )
        deliverables = self._normalize_assignment_lines(
            data.get("deliverables"),
            fallback["deliverables"],
        )
        acceptance = self._normalize_assignment_lines(
            data.get("acceptance_criteria"),
            fallback["acceptance_criteria"],
        )
        return {
            "projection_id": projection.projection_id,
            **work_item_identity_payload(projection_id=projection.projection_id, turn_type=""),
            "global_intent_summary": global_intent_summary,
            "your_responsibility": your_responsibility,
            "out_of_scope": out_of_scope,
            "inputs": inputs,
            "deliverables": deliverables,
            "acceptance_criteria": acceptance,
        }

    def _default_projection_assignment(
        self,
        projection: WorkItemProjectionSpec,
        *,
        global_intent_summary: str,
    ) -> dict[str, Any]:
        dependency_labels = [
            dep.replace("_", " ").strip().title()
            for dep in projection.dependency_projection_ids
        ]
        inputs = ["Use the global intent summary as the mission baseline."]
        if dependency_labels:
            inputs.extend(
                f"Rely on the completed handoff/results from `{label}`."
                for label in dependency_labels
            )
        else:
            inputs.append("You may proceed without waiting for upstream work-item handoffs.")
        deliverables = self._infer_work_item_deliverables(projection)
        acceptance = list(projection.metadata.get("acceptance_criteria", [])) or self._infer_work_item_acceptance(projection, deliverables)
        out_of_scope = [
            "Do not redo work that belongs to upstream completed work items.",
            "Do not take ownership of deliverables assigned to other work items.",
        ]
        if projection.dependency_projection_ids:
            out_of_scope.append("Do not overwrite dependency conclusions unless a gate or handoff explicitly requests rework.")
        return {
            "projection_id": projection.projection_id,
            **work_item_identity_payload(projection_id=projection.projection_id, turn_type=""),
            "global_intent_summary": global_intent_summary,
            "your_responsibility": (
                f"Own the `{projection.title}` work item for role `{projection.role_id}`. "
                f"{projection.summary.strip() or 'Complete the work defined for this projected work item.'}"
            ),
            "out_of_scope": out_of_scope,
            "inputs": inputs,
            "deliverables": deliverables,
            "acceptance_criteria": acceptance,
        }

    def _infer_work_item_deliverables(self, projection_spec: WorkItemProjectionSpec) -> list[str]:
        lowered = f"{projection_spec.projection_id} {projection_spec.title} {projection_spec.summary}".lower()
        if _WORKSPACE_BOOTSTRAP_PROJECTION_ID in lowered or "workspace bootstrap" in lowered:
            return ["A structured workspace_manifest plus the prepared shared workspace layout."]
        if _DATA_ACQUISITION_PROJECTION_ID in lowered or "data acquisition" in lowered:
            return [
                "A data acquisition execution record showing what sources were attempted and what assets were prepared.",
                "A structured data_acquisition_report describing final self-audited input readiness for downstream execution.",
            ]
        if any(token in lowered for token in ("intake", "triage", "framing the company mission")):
            return [
                f"Write the project intake brief to `deliverables/{projection_spec.projection_id}.md` with these sections: "
                f"## Mission Summary, ## Scope, ## Out of Scope, ## Initial Risks & Unknowns, "
                f"## C-suite Routing (which executives own which slice), ## Acceptance Criteria for Final Delivery.",
                "Surface the same brief in your final task result so downstream planning work items and reviewers can read it directly.",
            ]
        if any(token in lowered for token in ("plan", "planning", "approach", "architecture")):
            return [
                f"Write the planning brief to `deliverables/{projection_spec.projection_id}.md` with these sections: "
                f"## Goals (what success looks like for this work item's slice), ## Key Decisions, "
                f"## Approach / Sequencing, ## Assumptions, ## Risks & Mitigations, "
                f"## Downstream Handoff Notes (what executors need from this plan).",
                "Surface the same plan in your final task result so downstream work items and reviewers can read it directly.",
            ]
        if any(token in lowered for token in ("review", "audit", "qa", "test", "validation")):
            return ["A review outcome with findings, validation notes, and a clear pass/fail recommendation."]
        if any(token in lowered for token in ("delivery", "aggregate", "final")):
            return ["A concise final delivery that aggregates relevant upstream results for the user."]
        if any(token in lowered for token in ("content", "documentation", "presentation", "design")):
            return ["The work-item-specific content or artifact requested by the runtime objective."]
        return ["A concrete output that fulfills this work-item objective and can be handed off downstream."]

    def _infer_work_item_acceptance(self, projection_spec: WorkItemProjectionSpec, deliverables: list[str]) -> list[str]:
        lowered = f"{projection_spec.projection_id} {projection_spec.title} {projection_spec.summary}".lower()
        if _WORKSPACE_BOOTSTRAP_PROJECTION_ID in lowered or "workspace bootstrap" in lowered:
            return [
                "The target workspace root exists and is writable for downstream work items.",
                "The reserved directories exist: inputs/, deliverables/, work/, and .openopc/manifests/.",
                "A workspace_manifest is recorded with the root path and reserved directory mapping.",
            ]
        if _DATA_ACQUISITION_PROJECTION_ID in lowered or "data acquisition" in lowered:
            return [
                "The work item attempts real acquisition or preparation of missing critical inputs before concluding they are blocked.",
                "A data_acquisition_report is recorded after the work item self-audits the prepared inputs.",
                "The report status is one of ready, already_present, not_required, partial, or missing_critical.",
                "Blocking statuses require explicit acquisition attempt evidence, prepared assets, or documented blockers.",
                "Required inputs, present inputs, missing inputs, attempted sources, prepared assets, and provenance summary are all explicit.",
            ]
        if any(token in lowered for token in (
            "intake", "triage", "framing the company mission", "plan", "planning", "approach", "architecture",
        )):
            return [
                f"The planning brief file `deliverables/{projection_spec.projection_id}.md` exists in the shared workspace and contains every required section.",
                "The brief stays within this work item's role boundary (this work item plans its own slice; it does not redo upstream work or absorb other work items' deliverables).",
                "Downstream work items can act on the brief without re-reading the original user request.",
            ]
        criteria = [
            "The output stays within this work item's responsibility boundary.",
            "The output is usable by downstream work items without re-reading the full user request.",
        ]
        if projection_spec.dependency_projection_ids:
            criteria.append("Upstream handoffs or dependency results are incorporated rather than duplicated.")
        return criteria

    def _normalize_assignment_lines(self, value: Any, fallback: list[str]) -> list[str]:
        if isinstance(value, str):
            lines = [line.strip(" -") for line in value.splitlines() if line.strip()]
        elif isinstance(value, list):
            lines = [str(item).strip() for item in value if str(item).strip()]
        else:
            lines = []
        return lines or list(fallback)

    def _fallback_global_intent_summary(self, original_message: str) -> str:
        compact = " ".join(str(original_message or "").split())
        if not compact:
            return "Complete the requested runtime while keeping each work item tightly scoped."
        # No character truncation: this fallback is the user's original
        # mission and downstream work items depend on it for routing.
        # Truncating it (the previous behavior chopped at 217 chars and
        # appended "...") silently destroyed the mission baseline for
        # any prompt that hit the fallback path.
        return compact

    def _build_work_item_description(
        self,
        assignment: dict[str, Any],
    ) -> str:
        parts = [
            f"## Global Intent Summary\n{assignment['global_intent_summary']}",
            f"## Your Responsibility\n{assignment['your_responsibility']}",
            "## Out of Scope\n" + "\n".join(f"- {item}" for item in assignment["out_of_scope"]),
            "## Inputs\n" + "\n".join(f"- {item}" for item in assignment["inputs"]),
            "## Deliverables\n" + "\n".join(f"- {item}" for item in assignment["deliverables"]),
            "## Acceptance Criteria\n" + "\n".join(f"- {item}" for item in assignment["acceptance_criteria"]),
        ]
        return "\n\n".join(parts)

    def _infer_work_item_turn_type(self, projection: WorkItemProjectionSpec) -> str:
        delegation_kind = str((projection.metadata or {}).get("delegation_turn_kind", "") or "").strip().lower()
        if delegation_kind == "intake":
            return "intake"
        if delegation_kind == "delegate":
            return "dispatch"
        if delegation_kind == "synthesize":
            return "aggregate"
        if delegation_kind == "deliver":
            return "deliver"
        if delegation_kind == "execute":
            return "execute"
        projection_id = str(projection.projection_id or "").strip().lower()
        if projection_id == _WORKSPACE_BOOTSTRAP_PROJECTION_ID:
            return "setup"
        if projection_id == _DATA_ACQUISITION_PROJECTION_ID:
            return "execute"
        lowered = " ".join(
            part for part in (
                str(projection.projection_id or "").strip().lower(),
                str(projection.title or "").strip().lower(),
                str(projection.summary or "").strip().lower(),
                str(projection.role_id or "").strip().lower(),
            )
            if part
        )
        if any(token in lowered for token in (
            "setup", "provision", "environment", "env_setup", "env_provision",
            "install dependencies", "install tools", "configure environment",
            "toolchain", "runtime setup", "dependency install",
            "workspace bootstrap", "workspace scaffold", "workspace setup", "bootstrap workspace",
        )):
            return "setup"
        if any(token in lowered for token in ("intake", "triage", "classify", "frame the company mission")):
            return "intake"
        if any(token in lowered for token in ("review", "approval", "approve", "audit", "qa", "validation", "acceptance")):
            return "review"
        if any(token in lowered for token in ("dispatch", "coordination", "coordinate", "routing", "assignment")):
            return "dispatch"
        if any(token in lowered for token in ("plan", "planning", "architecture", "approach", "execution plan")):
            return "plan"
        if any(token in lowered for token in ("execute", "execution", "implement", "implementation", "develop", "backend api", "code artifacts", "code implementation")):
            return "execute"
        if any(token in lowered for token in ("aggregate", "aggregation", "synthesize", "synthesis")):
            return "aggregate"
        if any(token in lowered for token in ("delivery", "deliver", "final return", "final delivery", "return the outcome")):
            return "deliver"
        return "execute"

    def _infer_work_item_orchestration_profile(
        self,
        projection_spec: WorkItemProjectionSpec,
        *,
        work_item_turn_type: str,
    ) -> str:
        strategy = (
            projection_spec.execution_strategy.value
            if hasattr(projection_spec.execution_strategy, "value")
            else str(projection_spec.execution_strategy or "auto")
        )
        if work_item_turn_type == "setup":
            return "company_setup_provision"
        if work_item_turn_type == "review":
            return "company_review_fresh_eyes"
        if work_item_turn_type in {"plan", "intake"}:
            return "company_plan_read_heavy"
        if work_item_turn_type in {"dispatch", "aggregate", "deliver"}:
            return f"company_{work_item_turn_type}_coordinator"
        if strategy == WorkItemExecutionStrategy.AUTO.value:
            return "company_execute_native_first"
        if strategy == WorkItemExecutionStrategy.NATIVE.value:
            return "company_execute_native"
        if strategy == WorkItemExecutionStrategy.EXTERNAL.value:
            return "company_execute_external"
        if strategy == WorkItemExecutionStrategy.MIXED.value:
            return "company_execute_mixed"
        return "company_execute_native_first"

    def _work_item_verification_required(
        self,
        projection_spec: WorkItemProjectionSpec,
        *,
        work_item_turn_type: str,
    ) -> bool:
        explicit = projection_spec.metadata.get("verification_required")
        if isinstance(explicit, bool):
            return explicit
        if work_item_turn_type not in {"execute"}:
            return False
        lowered = f"{projection_spec.title} {projection_spec.summary}".lower()
        if any(token in lowered for token in ("documentation", "presentation", "content strategy")):
            return False
        return True

    def _build_work_item_runtime_plan(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        assignment: dict[str, Any],
        work_item_turn_type: str,
        runtime_policy: dict[str, Any],
    ) -> dict[str, Any]:
        communication_policy = dict(runtime_policy.get("communication", {}))
        collaboration: list[str] = []
        if projection_spec.dependency_projection_ids:
            collaboration.append("Start from dependency handoffs instead of re-solving upstream work.")
        if work_item_turn_type == "execute":
            collaboration.append("Consume only designated workspace and input paths from upstream readiness artifacts.")
            collaboration.append("Do not fabricate critical missing inputs or downgrade missing inputs into a done state.")
            if str(projection_spec.projection_id).strip() == _DATA_ACQUISITION_PROJECTION_ID:
                collaboration.append(
                    "Use web_search/web_fetch or browser tools first to discover and verify candidate external sources before writing custom network scripts."
                )
                collaboration.append(
                    "Use shell_exec only after concrete source URLs are identified and you need deterministic preparation or downloads inside the workspace."
                )
                collaboration.append("Attempt real acquisition and preparation before declaring a blocking input status.")
                collaboration.append("Record attempted sources, prepared assets, and blockers in the final data acquisition artifacts.")
        data_acquisition_extensions: dict[str, Any] = {}
        if str(projection_spec.projection_id).strip() == _DATA_ACQUISITION_PROJECTION_ID:
            data_acquisition_extensions = {
                "execution_sequence": [
                    "Discover: use web_search/web_fetch or browser tools to identify candidate sources.",
                    "Verify: keep only sources you can justify as official or acceptable for the task.",
                    "Prepare inputs: use standard CLI tools through shell_exec to download or normalize inputs inside the workspace.",
                    "Report: publish source_candidates, download_manifest, and the final readiness report.",
                ],
                "media_mode_triggers": [
                    "Enable media mode when the request or required inputs mention video, trailer, footage, clip, 素材, 片段, audio, music, subtitle, srt, bilibili, youtube, mp4, or wav.",
                ],
                "media_mode_rules": [
                    "Search-result pages, HTML snapshots, and URL lists never count as acquired binary assets.",
                    "Binary media must be prepared inside the workspace or the status must remain partial/missing_critical.",
                    "Prefer standard CLI tools such as yt-dlp, curl, wget, aria2c, and ffmpeg.",
                    "Do not use inline Python or ad hoc urllib scripts as the primary acquisition path.",
                    "Parse raw HTML into work/source_candidates.json before inspecting it further.",
                ],
                "download_priority": [
                    "Discover/verify: web_search, web_fetch, browser_*",
                    "Download and prepare: yt-dlp, curl, wget, aria2c",
                    "Normalize/probe: ffmpeg",
                ],
            }
        default_mode = str(communication_policy.get("default_mode", "") or "").strip()
        if default_mode == "dm":
            collaboration.append("Use direct messages for targeted clarifications.")
        elif default_mode == "broadcast":
            collaboration.append("Broadcast only when an issue affects multiple downstream roles.")
        if communication_policy.get("meeting_required_for"):
            collaboration.append("Escalate to a meeting only for true cross-role decisions or conflicts.")
        if work_item_turn_type in {"execute", "review"}:
            collaboration.append(
                "You may run a work-item swarm: the durable owner stays accountable while elastic worker slots claim shared microtasks."
            )
        if not collaboration:
            collaboration.append("Prefer asynchronous handoffs and annotations over blocking coordination.")
        return {
            "projection_id": projection_spec.projection_id,
            **work_item_identity_payload(projection_id=projection_spec.projection_id, turn_type=work_item_turn_type),
            "turn_type": work_item_turn_type,
            "summary": assignment["your_responsibility"],
            "inputs": list(assignment["inputs"]),
            "deliverables": list(assignment["deliverables"]),
            "acceptance_criteria": list(assignment["acceptance_criteria"]),
            "out_of_scope": list(assignment["out_of_scope"]),
            "collaboration_expectations": collaboration,
            "verification_required": self._work_item_verification_required(
                projection_spec,
                work_item_turn_type=work_item_turn_type,
            ),
            **data_acquisition_extensions,
        }

    def _downstream_consumers(
        self,
        plan: CompanyWorkItemRuntimePlan,
        projection_spec: WorkItemProjectionSpec,
    ) -> list[str]:
        consumers: list[str] = []
        for candidate in plan.projections:
            if projection_spec.projection_id not in list(candidate.dependency_projection_ids):
                continue
            role_id = str(candidate.role_id or "").strip()
            if role_id and role_id not in consumers:
                consumers.append(role_id)
        return consumers

    @staticmethod
    def _work_item_requires_writable_scope(
        projection_spec: WorkItemProjectionSpec,
        *,
        work_item_turn_type: str,
    ) -> bool:
        projection_id = str(projection_spec.projection_id or "").strip().lower()
        if projection_id == _DATA_ACQUISITION_PROJECTION_ID:
            return True
        return work_item_turn_type in {"execute", "setup"}

    def _build_ownership_contract(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        assignment: dict[str, Any],
        work_item_turn_type: str,
        target_output_dir: str | None,
        downstream_consumers: list[str],
    ) -> ArtifactContract:
        write_scope = "read_only"
        if self._work_item_requires_writable_scope(projection_spec, work_item_turn_type=work_item_turn_type):
            write_scope = str(target_output_dir or "assigned_workspace").strip() or "assigned_workspace"
        expected_artifacts = [
            str(item).strip()
            for item in list(assignment.get("deliverables", []) or [])
            if str(item).strip()
        ]
        allowed_targets: list[str] = []
        get_allowed_contact_roles = getattr(self.org_engine, "get_allowed_contact_roles", None)
        if callable(get_allowed_contact_roles):
            allowed_targets = list(get_allowed_contact_roles(projection_spec.role_id))
        return ArtifactContract(
            summary=str(assignment.get("your_responsibility", "") or projection_spec.summary or "").strip(),
            write_scope=write_scope,
            expected_artifacts=expected_artifacts,
            downstream_consumer=list(downstream_consumers),
            allowed_collaboration_targets=allowed_targets,
        )

    @staticmethod
    def _coordination_policy(runtime_policy: dict[str, Any]) -> dict[str, Any]:
        return dict(runtime_policy.get("coordination", {}) or {})

    @staticmethod
    def _role_text(agent: Any | None, employee_assignment: dict[str, Any] | None = None) -> str:
        def _collect_values(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                text = value.strip()
                return [text] if text else []
            if isinstance(value, (int, float, bool)):
                return [str(value)]
            if isinstance(value, dict):
                values: list[str] = []
                for nested in value.values():
                    values.extend(_collect_values(nested))
                return values
            if isinstance(value, (list, tuple, set)):
                values: list[str] = []
                for nested in value:
                    values.extend(_collect_values(nested))
                return values
            text = str(value).strip()
            return [text] if text else []

        parts = [
            str(getattr(agent, "name", "") or "").strip(),
            str(getattr(agent, "responsibility", "") or "").strip(),
            " ".join(str(item).strip() for item in list(getattr(agent, "can_spawn", []) or []) if str(item).strip()),
            " ".join(_collect_values(dict(getattr(agent, "runtime_policy", {}) or {}))),
            " ".join(_collect_values(dict(employee_assignment or {}))),
        ]
        return " ".join(part for part in parts if part).lower()

    def _infer_role_profile(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        employee_assignment: dict[str, Any] | None = None,
    ) -> AdaptiveRoleProfile:
        agent = self.org_engine.get_agent(projection_spec.role_id)
        role_text = self._role_text(agent, employee_assignment)
        facets: list[str] = []
        evidence: list[str] = []
        if list(getattr(agent, "can_spawn", []) or []) or str(getattr(agent, "reports_to", "") or "").strip() == "owner":
            facets.append("coordination")
            evidence.append("role_can_spawn_or_reports_to_owner")
        if any(token in role_text for token in ("review", "qa", "quality assurance", "test", "validation", "audit", "compliance", "verification")):
            facets.append("review")
            evidence.append("review_tokens")
        if any(token in role_text for token in ("environment", "toolchain", "setup", "provision", "dependency", "runtime")):
            facets.extend(["setup", "provider"])
            evidence.append("setup_tokens")
        if any(token in role_text for token in ("acquisition", "source", "input", "preparation", "download", "provenance")):
            facets.extend(["acquisition", "provider"])
            evidence.append("acquisition_tokens")
        if any(token in role_text for token in ("engineer", "implement", "implementation", "develop", "code", "technical")):
            facets.append("technical_execution")
            evidence.append("technical_tokens")
        if any(token in role_text for token in ("design", "ux", "content", "copy", "presentation", "documentation")):
            facets.append("creative_execution")
            evidence.append("creative_tokens")
        if str(getattr(agent, "reports_to", "") or "").strip() == "owner":
            facets.append("decision_maker")
            evidence.append("top_level_role")
        if not facets:
            facets.append("generalist")
            evidence.append("fallback_generalist")
        facets = list(dict.fromkeys(facets))
        authority_scope: list[str] = []
        if "decision_maker" in facets:
            authority_scope.extend(["deliver", "approve", "direct"])
        if "coordination" in facets:
            authority_scope.extend(["delegate", "synthesize", "gate"])
        if "review" in facets:
            authority_scope.extend(["review", "verify"])
        if "provider" in facets:
            authority_scope.extend(["prepare"])
        if "technical_execution" in facets or "creative_execution" in facets or "generalist" in facets:
            authority_scope.append("execute")
        label = "generalist"
        if "decision_maker" in facets:
            label = "decision_maker"
        elif "review" in facets and "coordination" in facets:
            label = "review_coordinator"
        elif "coordination" in facets:
            label = "coordinator"
        elif "review" in facets:
            label = "reviewer"
        elif "provider" in facets:
            label = "provider"
        elif "technical_execution" in facets:
            label = "technical_executor"
        elif "creative_execution" in facets:
            label = "creative_executor"
        execution_bias = "balanced"
        if "provider" in facets or "review" in facets:
            execution_bias = "serial_preferred"
        elif "technical_execution" in facets or "creative_execution" in facets:
            execution_bias = "parallel_friendly"
        review_bias = "none"
        if "review" in facets:
            review_bias = "strict"
        elif "coordination" in facets:
            review_bias = "managerial"
        collaboration_style = "async"
        if "coordination" in facets:
            collaboration_style = "manager_driven"
        confidence = 0.55
        if "fallback_generalist" not in evidence:
            confidence = 0.72
        if "decision_maker" in facets:
            confidence = 0.85
        return AdaptiveRoleProfile(
            label=label,
            facets=facets,
            authority_scope=list(dict.fromkeys(authority_scope)),
            execution_bias=execution_bias,
            review_bias=review_bias,
            collaboration_style=collaboration_style,
            confidence=confidence,
            evidence=evidence,
        )

    def _coordination_turn_kind(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        work_item_turn_type: str,
        role_profile: AdaptiveRoleProfile,
    ) -> str:
        explicit = str((projection_spec.metadata or {}).get("turn_kind", "") or "").strip().lower()
        if explicit:
            return explicit
        projection_id = str(projection_spec.projection_id or "").strip().lower()
        if projection_id.endswith("__prepare"):
            return "prepare"
        if projection_id.endswith("__verify"):
            return "verify"
        if work_item_turn_type == "intake":
            return "plan"
        if work_item_turn_type == "dispatch":
            return "dispatch"
        if work_item_turn_type == "plan":
            return "plan"
        if work_item_turn_type == "setup":
            return "acquire" if "acquisition" in role_profile.facets else "setup"
        if work_item_turn_type == "review":
            return "verify"
        if work_item_turn_type == "aggregate":
            return "synthesize"
        if work_item_turn_type == "deliver":
            return "deliver"
        if "review" in role_profile.facets:
            return "verify"
        if "provider" in role_profile.facets:
            return "setup"
        return "execute"

    def _coordination_signal_owner(
        self,
        *,
        signal_name: str,
        projection_spec: WorkItemProjectionSpec,
    ) -> str:
        if signal_name == "delivery_ready":
            final_decider = getattr(self.org_engine, "get_final_decider_role_id", None)
            if callable(final_decider):
                return str(final_decider(strict=False) or projection_spec.role_id).strip()
            return projection_spec.role_id
        agents = list(getattr(self.org_engine, "list_agents", lambda: [])() or [])
        signal_tokens = {
            "env_ready": ("environment", "setup", "provision", "toolchain", "dependency"),
            "inputs_ready": ("acquisition", "source", "input", "download", "provenance"),
            "implementation_ready": ("technical", "engineer", "implement", "development", "architecture"),
            "qa_ready": ("review", "qa", "quality assurance", "validation", "audit", "compliance"),
        }.get(signal_name, ())
        for agent in agents:
            role_text = self._role_text(agent)
            if any(token in role_text for token in signal_tokens):
                return str(getattr(agent, "role_id", "") or "").strip()
        manager_role_id = str((projection_spec.metadata or {}).get("manager_role_id", "") or "").strip()
        return manager_role_id or projection_spec.role_id

    def _coordination_required_signals(
        self,
        *,
        turn_kind: str,
        role_profile: AdaptiveRoleProfile,
    ) -> list[str]:
        signals: list[str] = []
        if turn_kind in {"plan", "prepare", "setup", "acquire", "execute", "synthesize", "integration"}:
            signals.append("scope_locked")
        if turn_kind == "execute" and "provider" not in role_profile.facets:
            signals.extend(["env_ready", "inputs_ready"])
        if turn_kind == "verify":
            signals.extend(["implementation_ready", "env_ready", "inputs_ready"])
        if turn_kind == "deliver":
            signals.extend(["qa_ready", "delivery_ready"])
        return list(dict.fromkeys(signal for signal in signals if signal))

    @staticmethod
    def _coordination_emitted_signals(
        *,
        turn_kind: str,
        role_profile: AdaptiveRoleProfile,
    ) -> list[str]:
        if turn_kind == "setup":
            return ["env_ready"]
        if turn_kind == "acquire":
            return ["inputs_ready"]
        if turn_kind == "execute" and "provider" not in role_profile.facets and "review" not in role_profile.facets:
            return ["implementation_ready"]
        if turn_kind == "verify":
            return ["qa_ready"]
        if turn_kind == "deliver":
            return ["delivery_ready"]
        return []

    def _build_coordination_spec(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        assignment: dict[str, Any],
        work_item_turn_type: str,
        runtime_policy: dict[str, Any],
        employee_assignment: dict[str, Any] | None = None,
    ) -> CoordinationSpec:
        coordination_policy = self._coordination_policy(runtime_policy)
        role_profile = self._infer_role_profile(projection_spec=projection_spec, employee_assignment=employee_assignment)
        turn_kind = self._coordination_turn_kind(
            projection_spec=projection_spec,
            work_item_turn_type=work_item_turn_type,
            role_profile=role_profile,
        )
        strict_gate_turn_kinds = {
            str(item).strip().lower()
            for item in list(coordination_policy.get("strict_gate_turn_kinds", []) or [])
            if str(item).strip()
        }
        mixed_gate_turn_kinds = {
            str(item).strip().lower()
            for item in list(coordination_policy.get("mixed_gate_turn_kinds", []) or [])
            if str(item).strip()
        }
        gate_profile = str((projection_spec.metadata or {}).get("gate_profile", "") or "").strip().lower()
        dependency_class = "hard"
        if turn_kind in mixed_gate_turn_kinds:
            dependency_class = "soft"
        if gate_profile in {"readiness", "contract", "delivery"} or turn_kind in strict_gate_turn_kinds:
            dependency_class = "hard"
        required_artifacts = [
            item for item in list(assignment.get("inputs", []) or [])
            if any(token in str(item) for token in ("/", ".md", ".json", ".yml", ".yaml", ".txt"))
        ]
        writes: list[str] = []
        if work_item_turn_type in {"setup", "execute", "review", "deliver", "aggregate"}:
            writes.append("assigned_workspace")
        reads = [str(item).strip() for item in list(assignment.get("inputs", []) or []) if str(item).strip()]
        signals = [
            AdaptiveSignalSpec(
                name=signal_name,
                owner_role_id=self._coordination_signal_owner(signal_name=signal_name, projection_spec=projection_spec),
                required=True,
                strict=turn_kind in strict_gate_turn_kinds or gate_profile in {"readiness", "contract", "delivery"},
            )
            for signal_name in self._coordination_required_signals(turn_kind=turn_kind, role_profile=role_profile)
        ]
        work_item_profile = AdaptiveWorkItemProfile(
            turn_kind=turn_kind,
            dependency_class=dependency_class,
            blocked_by_projection_ids=list(dict.fromkeys(str(item).strip() for item in list(projection_spec.dependency_projection_ids) if str(item).strip())),
            blocked_by_signals=[signal.name for signal in signals],
            required_artifacts=list(dict.fromkeys(required_artifacts)),
            reads=list(dict.fromkeys(reads)),
            writes=list(dict.fromkeys(writes)),
            gate_owner_role_id=self._coordination_signal_owner(signal_name="qa_ready" if turn_kind == "verify" else "delivery_ready" if turn_kind == "deliver" else "scope_locked", projection_spec=projection_spec),
            soft_release_allowed=turn_kind in mixed_gate_turn_kinds and bool(coordination_policy.get("allow_manager_release_for_mixed_only", True)),
            confidence=0.8 if str((projection_spec.metadata or {}).get("turn_kind", "") or "").strip() else 0.68,
        )
        confidence = min(role_profile.confidence, work_item_profile.confidence) if role_profile.confidence and work_item_profile.confidence else max(role_profile.confidence, work_item_profile.confidence)
        return CoordinationSpec(
            version=1,
            inference_mode=str(coordination_policy.get("inference_mode", "llm_primary") or "llm_primary"),
            fallback_mode=str(coordination_policy.get("fallback_mode", "conservative") or "conservative"),
            role_profile=role_profile,
            work_item_profile=work_item_profile,
            signals=signals,
            emitted_signals=self._coordination_emitted_signals(turn_kind=turn_kind, role_profile=role_profile),
            normalized_state="planned",
            notes=[],
            confidence=confidence,
            evidence=[
                f"work_item_turn_type:{work_item_turn_type}",
                f"dependency_count:{len(projection_spec.dependency_projection_ids)}",
                *list(role_profile.evidence),
            ],
        )

    @staticmethod
    def _coordination_spec_dict(spec: CoordinationSpec) -> dict[str, Any]:
        return asdict(spec)

    def _lint_work_item_assignment(
        self,
        *,
        projection_spec: WorkItemProjectionSpec,
        assignment: dict[str, Any],
    ) -> list[str]:
        issues: list[str] = []
        responsibility = str(assignment.get("your_responsibility", "") or "").strip().lower()
        if not responsibility:
            issues.append("Missing responsibility summary for the work-item assignment.")
        out_of_scope = {
            str(item).strip().lower()
            for item in list(assignment.get("out_of_scope", []) or [])
            if str(item).strip()
        }
        deliverables = [
            str(item).strip()
            for item in list(assignment.get("deliverables", []) or [])
            if str(item).strip()
        ]
        acceptance_criteria = [
            str(item).strip()
            for item in list(assignment.get("acceptance_criteria", []) or [])
            if str(item).strip()
        ]
        inputs = [
            str(item).strip()
            for item in list(assignment.get("inputs", []) or [])
            if str(item).strip()
        ]
        if not deliverables:
            issues.append("Missing concrete deliverables for the work-item assignment.")
        if not acceptance_criteria:
            issues.append("Missing acceptance criteria for the work-item assignment.")
        if projection_spec.dependency_projection_ids and not inputs:
            issues.append("Work item depends on upstream work but does not list dependency inputs.")
        for deliverable in deliverables:
            if deliverable.lower() in out_of_scope:
                issues.append(f"Deliverable `{deliverable}` conflicts with out_of_scope.")
        if projection_spec.dependency_projection_ids and not any("handoff" in item.lower() or "dependency" in item.lower() for item in inputs):
            issues.append("Dependency inputs do not explicitly mention upstream handoffs or dependency results.")
        owner = str(projection_spec.role_id or "").strip()
        if not owner:
            issues.append("Work-item assignment is missing a role owner.")
        return issues


class CompanyRuntimeSpecBuilder(CompanyRuntimeWorkItemHelper):
    """Builds the lightweight company runtime spec used before recruitment."""

    def build_spec(self, decision: RouterDecision, *, original_message: str = "") -> CompanyRuntimeSpec:
        profile = str(
            getattr(decision, "company_profile", None)
            or self.org_engine.get_company_profile()
            or "corporate"
        ).strip() or "corporate"
        org_config = getattr(self.org_engine.config, "org", None)
        metadata: dict[str, Any] = {
            "source": "work_item_runtime",
            "execution_mode": "company_mode",
            "execution_model": "multi_team_org",
            "runtime_model": "multi_team_org",
            "work_item_driven": True,
            "company_profile": profile,
            "organization_id": str(getattr(org_config, "organization_id", "") or "").strip(),
            "organization_name": str(getattr(org_config, "organization_name", "") or "").strip(),
            "organization_config_file": str(getattr(org_config, "organization_config_file", "") or "").strip(),
            "original_request": original_message,
            "request_label": "company_runtime",
            "domains": list(getattr(decision, "domains", []) or []),
            "preferred_agent": getattr(decision, "preferred_agent", None),
            "requested_sub_tasks": list(getattr(decision, "sub_tasks", []) or []),
            "org_id": getattr(decision, "org_id", None),
        }
        return CompanyRuntimeSpec(
            profile=profile,
            original_request=original_message,
            runtime_model="multi_team_org",
            work_item_driven=True,
            metadata=metadata,
        )
