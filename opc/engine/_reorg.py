"""ReorgMixin — 組織重組相關方法。

從 opc/engine.py 提取的組織重組（Reorg）功能：
- 重組提案的建立、批准、套用
- 重組檢查點的保存和恢復
- /reorg 命令的解析和處理
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from opc.core.models import (
    ExecutionCheckpoint,
    ReorgChangeSet,
    ReorgProposal,
    TaskStatus,
)

if TYPE_CHECKING:
    from opc.engine._core import OPCEngine


class ReorgMixin:
    """Mixin providing organization reorg methods for OPCEngine."""

    # Type hints for attributes defined in OPCEngine core
    if TYPE_CHECKING:
        store: Any
        reorg_manager: Any
        org_engine: Any
        company_executor: Any
        company_recruiter: Any
        project_id: str | None

    async def _resume_reorg_checkpoint(self: "OPCEngine", checkpoint: ExecutionCheckpoint, user_reply: str) -> str:
        """恢復組織重組檢查點 — 處理使用者對重組提案的批准/拒絕。"""
        assert self.store and self.reorg_manager
        payload = checkpoint.payload
        proposal_id = payload.get("proposal_id", "")
        if not proposal_id:
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="invalid")
            return "Could not resume the pending reorg because the proposal reference is missing."
        reply = user_reply.strip().lower()
        approved_tokens = {"y", "yes", "ok", "okay", "approve", "approved", "confirm", "continue", "proceed", "go"}
        denied_tokens = {"n", "no", "deny", "denied", "reject", "rejected", "stop", "cancel", "abort"}
        waiting_task_id = str(payload.get("waiting_task_id", "") or "").strip()
        waiting_task = await self.store.get_task(waiting_task_id) if waiting_task_id else None
        parent_session_id = str(payload.get("parent_session_id", "") or "").strip()
        if not parent_session_id and waiting_task is not None:
            parent_session_id = str(getattr(waiting_task, "parent_session_id", "") or "").strip()
        plan_data = payload.get("company_work_item_plan") or payload.get("work_item_runtime_plan") or {}
        if plan_data:
            from opc.layer2_organization.company_mode import deserialize_company_work_item_runtime_plan

            base_plan = deserialize_company_work_item_runtime_plan(plan_data)
        else:
            from opc.layer2_organization.org_work_item_planner import CompanyWorkItemRuntimePlan

            profile = self.org_engine.get_company_profile() if self.org_engine else "corporate"
            if self.org_engine:
                try:
                    base_plan = self.org_engine.build_company_work_item_runtime_plan(
                        profile=profile,
                        runtime_topology=self.org_engine.build_runtime_delegation_topology(),
                        original_request=str(payload.get("original_message", "") or ""),
                    )
                except ValueError:
                    base_plan = CompanyWorkItemRuntimePlan(
                        profile=profile,
                        metadata={"execution_model": "multi_team_org", "work_item_driven": True},
                    )
            else:
                base_plan = CompanyWorkItemRuntimePlan(
                    profile=profile,
                    metadata={"execution_model": "multi_team_org", "work_item_driven": True},
                )
        if reply in approved_tokens:
            await self.reorg_manager.set_reorg_approval(proposal_id, approved=True, notes=user_reply.strip())
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            result = await self.reorg_manager.apply_reorg(proposal_id)
            if waiting_task is not None:
                waiting_task = await self.store.get_task(waiting_task.id) or waiting_task
                self._clear_pending_reorg_marker(waiting_task)
                if waiting_task.status not in {TaskStatus.CANCELLED, TaskStatus.DONE}:
                    waiting_task.status = TaskStatus.PENDING
                    waiting_task.result = None
                waiting_task.metadata = dict(waiting_task.metadata)
                progress = list(waiting_task.metadata.get("progress_log", []))
                progress.append(f"Approved runtime replan `{proposal_id}` and refreshed the runtime.")
                waiting_task.metadata["progress_log"] = progress
                await self.store.save_task(waiting_task)
            if parent_session_id and self.company_executor:
                profile = self.org_engine.get_company_profile() if self.org_engine else base_plan.profile
                if self.org_engine:
                    try:
                        current_plan = self.org_engine.build_company_work_item_runtime_plan(
                            profile=profile,
                            runtime_topology=self.org_engine.build_runtime_delegation_topology(),
                            original_request=str(payload.get("original_message", "") or ""),
                        )
                    except ValueError:
                        current_plan = base_plan
                else:
                    current_plan = base_plan
                reconciled = await self._reconcile_company_work_item_plan_state(
                    parent_session_id,
                    current_plan,
                )
                if reconciled:
                    plan, tasks = reconciled
                    resumed = await self.company_executor.execute(plan, tasks)
                    return (
                        f"Reorg `{proposal_id}` approved and applied.\n"
                        f"Migrated tasks: {len(result.get('migration_summary', {}).get('migrated_task_ids', []))}\n"
                        f"Migrated checkpoints: {len(result.get('migration_summary', {}).get('migrated_checkpoint_ids', []))}\n\n"
                        f"{resumed}"
                    ).strip()
            return (
                f"Reorg `{proposal_id}` approved and applied.\n"
                f"Migrated tasks: {len(result.get('migration_summary', {}).get('migrated_task_ids', []))}\n"
                f"Migrated checkpoints: {len(result.get('migration_summary', {}).get('migrated_checkpoint_ids', []))}"
            )
        if reply in denied_tokens:
            await self.reorg_manager.set_reorg_approval(proposal_id, approved=False, notes=user_reply.strip())
            await self.store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
            if waiting_task is not None:
                self._clear_pending_reorg_marker(waiting_task)
                waiting_task.result = {
                    "content": f"Runtime replan `{proposal_id}` was denied.",
                    "artifacts": {},
                }
                waiting_task.metadata = dict(waiting_task.metadata)
                progress = list(waiting_task.metadata.get("progress_log", []))
                progress.append(f"Denied runtime replan `{proposal_id}`.")
                waiting_task.metadata["progress_log"] = progress
                await self._fail_task_via_phase(
                    waiting_task,
                    reason=f"reorg_denied:{proposal_id}",
                )
                return f"Reorg `{proposal_id}` was denied. The proposing work item was halted and the current runtime remains unchanged."
            return f"Reorg `{proposal_id}` was denied. The current company architecture remains unchanged."
        return (
            "There is a pending company reorg waiting for confirmation. "
            "Reply with `approve` / `continue` to apply it, or `deny` / `stop` to reject it."
        )

    async def _maybe_handle_reorg_message(self: "OPCEngine", content: str, session_id: str | None) -> str | None:
        """嘗試將使用者訊息解析為組織重組命令（/reorg 開頭）。"""
        assert self.reorg_manager and self.store
        stripped = content.strip()
        if not stripped.lower().startswith("reorg "):
            return None
        match = re.match(r"^reorg\s+(propose|approve|deny|apply|show|adjust)\b(.*)$", stripped, re.IGNORECASE | re.DOTALL)
        if not match:
            return "Unsupported reorg command. Use `reorg propose|approve|deny|apply|show|adjust`."
        action = match.group(1).lower()
        remainder = match.group(2).strip()
        project_id = self.project_id or "default"

        if action == "show":
            proposal = await self.store.get_reorg_proposal(remainder)
            if not proposal:
                return f"Unknown reorg proposal `{remainder}`."
            return self._format_reorg_summary(proposal)
        if action in {"approve", "deny"}:
            proposal = await self.approve_company_reorg(
                proposal_id=remainder,
                approved=(action == "approve"),
                notes=f"Explicit {action} via process_message.",
            )
            if action == "approve":
                result = await self.apply_company_reorg(remainder)
                return (
                    f"{self._format_reorg_summary(proposal)}\n\n"
                    f"Applied with {len(result.get('migration_summary', {}).get('migrated_task_ids', []))} migrated tasks."
                )
            return self._format_reorg_summary(proposal)
        if action == "apply":
            result = await self.apply_company_reorg(remainder)
            return (
                f"Applied reorg `{remainder}`.\n"
                f"Migrated tasks: {len(result.get('migration_summary', {}).get('migrated_task_ids', []))}\n"
                f"Migrated checkpoints: {len(result.get('migration_summary', {}).get('migrated_checkpoint_ids', []))}"
            )

        parsed = self._parse_reorg_payload(remainder)
        if parsed is None:
            return "Reorg payload must be valid JSON after the command."
        if action == "propose":
            proposal = await self.propose_company_reorg(
                summary=str(parsed.get("summary", "Runtime company reorg")),
                rationale=str(parsed.get("rationale", parsed.get("summary", ""))),
                title=str(parsed.get("title", "")),
                changeset=parsed.get("changeset", {}),
                session_id=session_id,
                task_id=parsed.get("task_id"),
                initiated_by=str(parsed.get("initiated_by", "owner")),
                source_role_id=str(parsed.get("source_role_id", "")),
                metadata={"source": "process_message"},
            )
            if proposal.user_confirmation_required:
                await self._save_reorg_checkpoint(proposal)
                return (
                    f"{self._format_reorg_summary(proposal)}\n\n"
                    "This reorg changes the company architecture and requires user confirmation. "
                    "Reply `approve` or `deny`, or use `reorg approve <proposal_id>`."
                )
            return self._format_reorg_summary(proposal)
        if action == "adjust":
            result = await self.suggest_task_adjustment(
                summary=str(parsed.get("summary", "Task adjustment")),
                source_role_id=str(parsed.get("source_role_id", "coordinator")),
                changeset=parsed.get("changeset", {}),
                session_id=session_id,
                task_id=parsed.get("task_id"),
            )
            proposal = result["proposal"]
            return (
                f"{self._format_reorg_summary(proposal)}\n\n"
                f"Auto applied: {'yes' if result.get('auto_applied') else 'no'}"
            )
        return None

    def _parse_reorg_payload(self, payload: str) -> dict[str, Any] | None:
        """解析重組命令的 JSON payload。"""
        try:
            data = json.loads(payload)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    async def _save_reorg_checkpoint(self: "OPCEngine", proposal: ReorgProposal) -> None:
        """儲存組織重組提案檢查點（等待使用者批准）。"""
        await self._save_execution_checkpoint(
            {
                "project_id": proposal.project_id,
                "session_id": proposal.session_id,
                "checkpoint_type": "company_reorg_pending",
                "task_id": proposal.task_id,
                "payload": {
                    "proposal_id": proposal.proposal_id,
                    "org_version": proposal.old_org_version,
                    "runtime_topology_version": proposal.old_runtime_topology_version,
                },
            }
        )

    def _format_reorg_summary(self, proposal: ReorgProposal) -> str:
        """格式化組織重組提案摘要供使用者檢視。"""
        return (
            f"Reorg proposal `{proposal.proposal_id}`\n"
            f"Status: {proposal.status.value}\n"
            f"Scope: {proposal.scope.value}\n"
            f"Risk: {proposal.risk_level.value}\n"
            f"Summary: {proposal.summary}\n"
            f"Needs user confirmation: {'yes' if proposal.user_confirmation_required else 'no'}"
        )

    async def propose_company_reorg(
        self: "OPCEngine",
        *,
        summary: str,
        changeset: ReorgChangeSet | dict[str, Any],
        rationale: str = "",
        title: str = "",
        session_id: str | None = None,
        task_id: str | None = None,
        initiated_by: str = "owner",
        source_role_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ReorgProposal:
        """提出公司組織重組提案。"""
        assert self.reorg_manager
        proposal = await self.reorg_manager.propose_reorg(
            project_id=self.project_id or "default",
            summary=summary,
            rationale=rationale,
            title=title,
            initiated_by=initiated_by,
            source_role_id=source_role_id,
            changeset=changeset,
            session_id=session_id,
            task_id=task_id,
            metadata=metadata,
        )
        return proposal

    async def approve_company_reorg(
        self: "OPCEngine",
        proposal_id: str,
        *,
        approved: bool,
        notes: str = "",
    ) -> ReorgProposal:
        """批准或拒絕公司組織重組提案。"""
        assert self.reorg_manager
        return await self.reorg_manager.set_reorg_approval(proposal_id, approved=approved, notes=notes)

    async def apply_company_reorg(self: "OPCEngine", proposal_id: str) -> dict[str, Any]:
        """套用已批准的公司組織重組提案。"""
        assert self.reorg_manager
        return await self.reorg_manager.apply_reorg(proposal_id)

    async def show_company_reorg(self: "OPCEngine", proposal_id: str) -> ReorgProposal | None:
        """查詢指定 ID 的組織重組提案。"""
        assert self.store
        return await self.store.get_reorg_proposal(proposal_id)

    async def suggest_task_adjustment(
        self: "OPCEngine",
        *,
        summary: str,
        source_role_id: str,
        changeset: ReorgChangeSet | dict[str, Any],
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """建議任務調整（由角色發起的輕量重組）。"""
        assert self.reorg_manager
        return await self.reorg_manager.suggest_task_adjustment(
            project_id=self.project_id or "default",
            source_role_id=source_role_id,
            summary=summary,
            changeset=changeset,
            session_id=session_id,
            task_id=task_id,
        )
