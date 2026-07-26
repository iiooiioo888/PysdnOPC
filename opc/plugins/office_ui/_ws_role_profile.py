"""WsRoleProfileMixin — 角色画像數據查詢相關方法。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    pass


def _json_loads(raw: Any, default: Any = None) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw or not isinstance(raw, str):
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}


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
        """建立角色的全部 10 個 section 數據（含自動推導 fallback）。"""
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
        # Fallback: derive from delegation_work_items
        if not sections["work_records"]:
            sections["work_records"] = await self._derive_work_records(store, role_id)

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
        # Fallback: derive from delegation_work_items interactions
        if not sections["collaboration"]:
            sections["collaboration"] = await self._derive_collaborations(store, role_id)

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
        # Fallback: derive from cost_events
        if not sections["resource_usage"]:
            sections["resource_usage"] = await self._derive_resource_usage(store, role_id)

        # ⑨ 任務佇列
        tasks = await store.get_role_task_assignments(project_id, role_id)
        sections["task_assignments"] = [
            {"assignment_id": t.assignment_id, "work_item_id": t.work_item_id,
             "title": t.title, "column": t.column, "priority": t.priority,
             "depends_on": t.depends_on, "blocked_reason": t.blocked_reason,
             "updated_at": t.updated_at.isoformat()}
            for t in tasks
        ]
        # Fallback: derive from delegation_work_items
        if not sections["task_assignments"]:
            sections["task_assignments"] = await self._derive_task_assignments(store, role_id)

        # ⑩ 通訊決策
        comms = await store.get_role_communications(project_id, role_id, limit=50)
        sections["communications"] = [
            {"comm_id": c.comm_id, "comm_type": c.comm_type, "title": c.title,
             "content": c.content, "participants": c.participants,
             "outcome": c.outcome, "created_at": c.created_at.isoformat()}
            for c in comms
        ]
        # Fallback: derive from work_item_decisions
        if not sections["communications"]:
            sections["communications"] = await self._derive_communications(store, project_id, role_id)

        return sections

    async def _build_single_section(self, store: Any, project_id: str, role_id: str, section: str) -> Any:
        """建立單一 section 的數據。"""
        all_sections = await self._build_role_profile_sections(store, project_id, role_id)
        return all_sections.get(section)

    # ------------------------------------------------------------------
    # Fallback derivation helpers — 從現有數據源自動推導角色画像
    # ------------------------------------------------------------------

    async def _derive_work_records(self, store: Any, role_id: str) -> list[dict[str, Any]]:
        """從 delegation_work_items 推導工作記錄。"""
        db = getattr(store, "_db", None)
        if not db:
            return []
        try:
            async with db.execute(
                """SELECT work_item_id, title, summary, kind, phase, created_at, updated_at,
                          deliverable_summary, source_role_id, manager_role_id
                   FROM delegation_work_items
                   WHERE role_id = ?
                   ORDER BY updated_at DESC LIMIT 50""",
                (role_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                cols = [d[0] for d in cursor.description]
        except Exception:
            return []

        results = []
        for row in rows:
            d = dict(zip(cols, row))
            phase = str(d.get("phase", "") or "")
            status = self._phase_to_status(phase)
            collaborators = []
            if d.get("source_role_id"):
                collaborators.append(str(d["source_role_id"]))
            if d.get("manager_role_id"):
                collaborators.append(str(d["manager_role_id"]))
            created = str(d.get("created_at", "") or "")
            updated = str(d.get("updated_at", "") or "")
            duration = 0.0
            try:
                if created and updated:
                    dt_c = datetime.fromisoformat(created)
                    dt_u = datetime.fromisoformat(updated)
                    duration = max(0.0, (dt_u - dt_c).total_seconds())
            except (ValueError, TypeError):
                pass
            results.append({
                "record_id": str(d.get("work_item_id", "")),
                "work_item_id": str(d.get("work_item_id", "")),
                "title": str(d.get("title", "") or d.get("summary", "") or "(untitled)"),
                "status": status,
                "collaborators": list(set(collaborators)),
                "started_at": created or datetime.now(timezone.utc).isoformat(),
                "completed_at": updated if status == "completed" else None,
                "duration_seconds": duration,
                "summary": str(d.get("deliverable_summary", "") or d.get("summary", "") or ""),
            })
        return results

    async def _derive_collaborations(self, store: Any, role_id: str) -> list[dict[str, Any]]:
        """從 delegation_work_items 推導協作網路。"""
        db = getattr(store, "_db", None)
        if not db:
            return []
        try:
            # Find roles that have interacted with this role (as source or manager)
            async with db.execute(
                """SELECT source_role_id, manager_role_id, COUNT(*) as cnt, MAX(updated_at) as last_at
                   FROM delegation_work_items
                   WHERE role_id = ? AND (source_role_id != '' OR manager_role_id != '')
                   GROUP BY source_role_id, manager_role_id""",
                (role_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                cols = [d[0] for d in cursor.description]
        except Exception:
            return []

        partner_stats: dict[str, dict[str, Any]] = {}
        for row in rows:
            d = dict(zip(cols, row))
            for partner_key in ("source_role_id", "manager_role_id"):
                partner = str(d.get(partner_key, "") or "").strip()
                if not partner or partner == role_id:
                    continue
                if partner not in partner_stats:
                    partner_stats[partner] = {"count": 0, "last_at": ""}
                partner_stats[partner]["count"] += int(d.get("cnt", 0) or 0)
                last = str(d.get("last_at", "") or "")
                if last > partner_stats[partner]["last_at"]:
                    partner_stats[partner]["last_at"] = last

        results = []
        for partner, stats in partner_stats.items():
            results.append({
                "collab_id": f"derived-{role_id}-{partner}",
                "partner_role_id": partner,
                "interaction_count": stats["count"],
                "last_interaction_at": stats["last_at"] or None,
                "quality_score": 0.0,
                "notes": "",
            })
        return results

    async def _derive_resource_usage(self, store: Any, role_id: str) -> list[dict[str, Any]]:
        """從 cost_events 推導資源消耗。"""
        db = getattr(store, "_db", None)
        if not db:
            return []
        try:
            async with db.execute(
                """SELECT model, SUM(tokens_in) as tin, SUM(tokens_out) as tout,
                          SUM(cost_usd) as cost, COUNT(*) as cnt, timestamp
                   FROM cost_events
                   WHERE agent_id = ?
                   GROUP BY model
                   ORDER BY timestamp DESC LIMIT 12""",
                (role_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                cols = [d[0] for d in cursor.description]
        except Exception:
            return []

        results = []
        for row in rows:
            d = dict(zip(cols, row))
            results.append({
                "usage_id": f"derived-{role_id}-{d.get('model', 'unknown')}",
                "period": str(d.get("timestamp", "") or "")[:10] or "N/A",
                "tokens_in": int(d.get("tin", 0) or 0),
                "tokens_out": int(d.get("tout", 0) or 0),
                "cost_usd": float(d.get("cost", 0.0) or 0.0),
                "duration_seconds": 0.0,
                "model_breakdown": {str(d.get("model", "") or "unknown"): int(d.get("cnt", 0) or 0)},
                "updated_at": str(d.get("timestamp", "") or datetime.now(timezone.utc).isoformat()),
            })
        return results

    async def _derive_task_assignments(self, store: Any, role_id: str) -> list[dict[str, Any]]:
        """從 delegation_work_items 推導任務佇列。"""
        db = getattr(store, "_db", None)
        if not db:
            return []
        try:
            async with db.execute(
                """SELECT work_item_id, title, phase, kind, blocked_reason, updated_at, metadata
                   FROM delegation_work_items
                   WHERE role_id = ?
                   ORDER BY updated_at DESC LIMIT 50""",
                (role_id,),
            ) as cursor:
                rows = await cursor.fetchall()
                cols = [d[0] for d in cursor.description]
        except Exception:
            return []

        results = []
        for row in rows:
            d = dict(zip(cols, row))
            phase = str(d.get("phase", "") or "")
            column = self._phase_to_column(phase)
            meta = _json_loads(d.get("metadata"), {})
            priority = int(meta.get("priority", 0) or 0) if isinstance(meta, dict) else 0
            depends_on = meta.get("depends_on", []) if isinstance(meta, dict) else []
            if not isinstance(depends_on, list):
                depends_on = []
            results.append({
                "assignment_id": str(d.get("work_item_id", "")),
                "work_item_id": str(d.get("work_item_id", "")),
                "title": str(d.get("title", "") or "(untitled)"),
                "column": column,
                "priority": priority,
                "depends_on": [str(x) for x in depends_on],
                "blocked_reason": str(d.get("blocked_reason", "") or ""),
                "updated_at": str(d.get("updated_at", "") or datetime.now(timezone.utc).isoformat()),
            })
        return results

    async def _derive_communications(self, store: Any, project_id: str, role_id: str) -> list[dict[str, Any]]:
        """從 work_item_decisions 推導通訊決策記錄。"""
        db = getattr(store, "_db", None)
        if not db:
            return []
        try:
            async with db.execute(
                """SELECT decision_id, category, summary, details, created_at, task_id
                   FROM work_item_decisions
                   WHERE role_id = ? AND project_id = ?
                   ORDER BY created_at DESC LIMIT 50""",
                (role_id, project_id),
            ) as cursor:
                rows = await cursor.fetchall()
                cols = [d[0] for d in cursor.description]
        except Exception:
            return []

        results = []
        for row in rows:
            d = dict(zip(cols, row))
            details = _json_loads(d.get("details"), {})
            participants = details.get("participants", []) if isinstance(details, dict) else []
            if not isinstance(participants, list):
                participants = []
            results.append({
                "comm_id": str(d.get("decision_id", "")),
                "comm_type": str(d.get("category", "") or "decision"),
                "title": str(d.get("summary", "") or "(untitled)"),
                "content": str(details.get("content", "") or "") if isinstance(details, dict) else "",
                "participants": [str(p) for p in participants],
                "outcome": str(details.get("outcome", "") or "") if isinstance(details, dict) else "",
                "created_at": str(d.get("created_at", "") or datetime.now(timezone.utc).isoformat()),
            })
        return results

    # ------------------------------------------------------------------
    # Phase mapping helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _phase_to_status(phase: str) -> str:
        """將 work item phase 映射為工作記錄狀態。"""
        phase = phase.lower().strip()
        if phase in ("done", "completed", "delivered", "accepted"):
            return "completed"
        if phase in ("failed", "cancelled", "rejected"):
            return "failed"
        if phase in ("in_progress", "executing", "running", "claimed"):
            return "in_progress"
        return "pending"

    @staticmethod
    def _phase_to_column(phase: str) -> str:
        """將 work item phase 映射為看板欄位。"""
        phase = phase.lower().strip()
        if phase in ("done", "completed", "delivered", "accepted"):
            return "done"
        if phase in ("failed", "cancelled", "rejected"):
            return "blocked"
        if phase in ("in_progress", "executing", "running", "claimed", "in_review", "reviewing"):
            return "in_progress"
        return "upcoming"
