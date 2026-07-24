"""WsRoleProfileMixin — 角色画像數據查詢相關方法。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    pass


class WsRoleProfileMixin:
    """Mixin providing role profile data handlers for WSHandler."""

    async def _handle_get_role_profile(self, ws: Any, data: dict) -> None:
        """返回指定角色的全部 10 個 section 數據。"""
        role_id = str(data.get("role_id", "")).strip()
        project_id = str(data.get("project_id", "default")).strip() or "default"

        if not role_id:
            await self._send_ack(ws, ok=False, error="role_id required", action="get_role_profile")
            return

        store = self._get_store()
        if not store:
            await self._send_ack(ws, ok=False, error="store unavailable", action="get_role_profile")
            return

        try:
            sections = await self._build_role_profile_sections(store, project_id, role_id)
            await self._send_ack(
                ws, ok=True, action="get_role_profile",
                role_id=role_id, project_id=project_id, sections=sections,
            )
        except Exception as exc:
            logger.opt(exception=True).error(f"get_role_profile error: {exc}")
            await self._send_ack(ws, ok=False, error="internal_error", action="get_role_profile")

    async def _handle_get_role_profile_section(self, ws: Any, data: dict) -> None:
        """返回指定角色的單一 section 數據。"""
        role_id = str(data.get("role_id", "")).strip()
        project_id = str(data.get("project_id", "default")).strip() or "default"
        section = str(data.get("section", "")).strip()

        if not role_id:
            await self._send_ack(ws, ok=False, error="role_id required", action="get_role_profile_section")
            return
        if not section:
            await self._send_ack(ws, ok=False, error="section required", action="get_role_profile_section")
            return

        store = self._get_store()
        if not store:
            await self._send_ack(ws, ok=False, error="store unavailable", action="get_role_profile_section")
            return

        try:
            section_data = await self._build_single_section(store, project_id, role_id, section)
            await self._send_ack(
                ws, ok=True, action="get_role_profile_section",
                role_id=role_id, section=section, data=section_data,
            )
        except Exception as exc:
            logger.opt(exception=True).error(f"get_role_profile_section error: {exc}")
            await self._send_ack(ws, ok=False, error="internal_error", action="get_role_profile_section")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_store(self) -> Any:
        """取得 OPCStore 實例。"""
        engine = getattr(self, "engine", None)
        if engine and hasattr(engine, "store"):
            return engine.store
        return getattr(self, "store", None)

    async def _build_role_profile_sections(self, store: Any, project_id: str, role_id: str) -> dict[str, Any]:
        """建立角色的全部 10 個 section 數據。"""
        sections: dict[str, Any] = {}

        # ① 角色記憶
        memories = await store.get_role_memory(project_id, role_id, limit=50)
        sections["memory"] = [
            {"memory_id": m.memory_id, "scope": m.scope, "summary": m.summary,
             "details": m.details, "created_at": m.created_at.isoformat()}
            for m in memories
        ]

        # ② 工作記錄
        work_records = await store.get_role_work_records(project_id, role_id, limit=50)
        sections["work_records"] = [
            {"record_id": r.record_id, "work_item_id": r.work_item_id, "title": r.title,
             "status": r.status, "collaborators": r.collaborators,
             "started_at": r.started_at.isoformat(),
             "completed_at": r.completed_at.isoformat() if r.completed_at else None,
             "duration_seconds": r.duration_seconds, "summary": r.summary}
            for r in work_records
        ]

        # ③ 角色取向
        orientation = await store.get_role_orientation(project_id, role_id)
        sections["orientation"] = (
            {"goals": orientation.goals, "capabilities": orientation.capabilities,
             "values": orientation.values, "updated_at": orientation.updated_at.isoformat()}
            if orientation else None
        )

        # ④ 角色性格
        personality = await store.get_role_personality(project_id, role_id)
        sections["personality"] = (
            {"traits": personality.traits, "interaction_style": personality.interaction_style,
             "behavior_notes": personality.behavior_notes, "updated_at": personality.updated_at.isoformat()}
            if personality else None
        )

        # ⑤ 協作網路
        collabs = await store.get_role_collaborations(project_id, role_id)
        sections["collaboration"] = [
            {"collab_id": c.collab_id, "partner_role_id": c.partner_role_id,
             "interaction_count": c.interaction_count,
             "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
             "quality_score": c.quality_score, "notes": c.notes}
            for c in collabs
        ]

        # ⑥ 技能圖譜
        skills = await store.get_role_skills(project_id, role_id)
        sections["skills"] = [
            {"skill_id": s.skill_id, "category": s.category, "skill_name": s.skill_name,
             "level": s.level, "learning_goals": s.learning_goals,
             "updated_at": s.updated_at.isoformat()}
            for s in skills
        ]

        # ⑦ 產出分析
        metrics = await store.get_role_output_metrics(project_id, role_id, limit=12)
        sections["output_metrics"] = [
            {"metrics_id": m.metrics_id, "week_label": m.week_label,
             "tasks_completed": m.tasks_completed, "quality_score": m.quality_score,
             "avg_duration": m.avg_duration, "rework_count": m.rework_count,
             "updated_at": m.updated_at.isoformat()}
            for m in metrics
        ]

        # ⑧ 資源消耗
        usage = await store.get_role_resource_usage(project_id, role_id, limit=12)
        sections["resource_usage"] = [
            {"usage_id": u.usage_id, "period": u.period,
             "tokens_in": u.tokens_in, "tokens_out": u.tokens_out,
             "cost_usd": u.cost_usd, "duration_seconds": u.duration_seconds,
             "model_breakdown": u.model_breakdown, "updated_at": u.updated_at.isoformat()}
            for u in usage
        ]

        # ⑨ 任務佇列
        tasks = await store.get_role_task_assignments(project_id, role_id)
        sections["task_assignments"] = [
            {"assignment_id": t.assignment_id, "work_item_id": t.work_item_id,
             "title": t.title, "column": t.column, "priority": t.priority,
             "depends_on": t.depends_on, "blocked_reason": t.blocked_reason,
             "updated_at": t.updated_at.isoformat()}
            for t in tasks
        ]

        # ⑩ 通訊決策
        comms = await store.get_role_communications(project_id, role_id, limit=50)
        sections["communications"] = [
            {"comm_id": c.comm_id, "comm_type": c.comm_type, "title": c.title,
             "content": c.content, "participants": c.participants,
             "outcome": c.outcome, "created_at": c.created_at.isoformat()}
            for c in comms
        ]

        return sections

    async def _build_single_section(self, store: Any, project_id: str, role_id: str, section: str) -> Any:
        """建立單一 section 的數據。"""
        all_sections = await self._build_role_profile_sections(store, project_id, role_id)
        return all_sections.get(section)
