"""角色画像更新工具 — 允許 LLM 在執行任務時自動更新角色画像數據。

職責說明：
    提供 role_profile_update 工具，LLM 可在任務執行過程中調用此工具
    來更新角色的記憶、工作記錄、取向、性格、協作、技能、產出、資源、
    任務佇列和通訊記錄等十大模塊數據。

關聯關係：
    - 被引擎工具棧註冊（create_role_profile_tools 回傳 ToolDefinition 列表）
    - 依賴 opc/database/store.py 的 OPCStore 進行持久化
    - 依賴 opc/core/models.py 的角色画像資料模型

使用範例：
    tools = create_role_profile_tools(store=engine.store)
    for tool in tools:
        registry.register(tool)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from opc.layer4_tools.registry import ToolDefinition


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_role_profile_tools(store: Any) -> list[ToolDefinition]:
    """建立角色画像更新的 Agent 工具。

    參數：
        store: OPCStore 實例（提供資料寫入）
    """

    async def role_profile_update(
        section: str,
        role_id: str,
        data: dict[str, Any],
        project_id: str = "default",
    ) -> dict[str, Any]:
        """更新角色画像數據（記憶、技能、性格、協作等）。

        在任務執行過程中調用此工具來記錄和更新角色的各項數據，
        系統會自動持久化到資料庫，供儀表板展示。

        參數：
            section: 要更新的模塊，可選值：
                - memory: 角色記憶
                - work: 工作記錄
                - orientation: 角色取向（目標/能力/價值觀）
                - personality: 角色性格（特質/交互風格）
                - collaboration: 協作網路
                - skill: 技能圖譜
                - output: 產出分析
                - resource: 資源消耗
                - task: 任務佇列
                - communication: 通訊決策
            role_id: 角色 ID
            data: 要寫入的數據（結構依 section 而異）
            project_id: 專案 ID（預設 "default"）
        """
        try:
            handler = _SECTION_HANDLERS.get(section)
            if not handler:
                return {"success": False, "error": f"未知的 section: {section}，可用值: {list(_SECTION_HANDLERS.keys())}"}

            await handler(store, project_id, role_id, data)
            return {"success": True, "section": section, "role_id": role_id, "message": f"已更新 {section} 數據"}
        except Exception as exc:
            logger.opt(exception=True).warning(f"role_profile_update failed: {exc}")
            return {"success": False, "error": str(exc)}

    tool = ToolDefinition(
        name="role_profile_update",
        description=(
            "更新角色画像數據。在任務執行過程中調用此工具來記錄角色的記憶、工作記錄、"
            "技能、性格、協作關係等數據，供角色管理儀表板展示。"
            "section 可選: memory/work/orientation/personality/collaboration/skill/output/resource/task/communication"
        ),
        parameters={
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "enum": ["memory", "work", "orientation", "personality", "collaboration", "skill", "output", "resource", "task", "communication"],
                    "description": "要更新的模塊名稱",
                },
                "role_id": {
                    "type": "string",
                    "description": "角色 ID",
                },
                "data": {
                    "type": "object",
                    "description": "要寫入的數據（結構依 section 而異）",
                },
                "project_id": {
                    "type": "string",
                    "description": "專案 ID（預設 default）",
                    "default": "default",
                },
            },
            "required": ["section", "role_id", "data"],
        },
        func=role_profile_update,
        category="role_management",
        read_only=False,
    )

    return [tool]


# ---------------------------------------------------------------------------
# Section handlers — 每個 section 對應一個寫入函數
# ---------------------------------------------------------------------------


async def _handle_memory(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RoleMemoryRecord
    record = RoleMemoryRecord(
        memory_id=data.get("memory_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        scope=data.get("scope", "project"),
        summary=data.get("summary", ""),
        details=data.get("details", {}),
    )
    await store.record_role_memory(record)


async def _handle_work(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RoleWorkRecord
    record = RoleWorkRecord(
        record_id=data.get("record_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        work_item_id=data.get("work_item_id", ""),
        title=data.get("title", ""),
        status=data.get("status", "in_progress"),
        collaborators=data.get("collaborators", []),
        started_at=_utcnow(),
        duration_seconds=float(data.get("duration_seconds", 0)),
        summary=data.get("summary", ""),
    )
    await store.record_role_work_record(record)


async def _handle_orientation(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RoleOrientation
    record = RoleOrientation(
        orientation_id=data.get("orientation_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        goals=data.get("goals", []),
        capabilities=data.get("capabilities", []),
        values=data.get("values", []),
        updated_at=_utcnow(),
    )
    await store.save_role_orientation(record)


async def _handle_personality(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RolePersonality
    record = RolePersonality(
        personality_id=data.get("personality_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        traits=data.get("traits", {}),
        interaction_style=data.get("interaction_style", ""),
        behavior_notes=data.get("behavior_notes", []),
        updated_at=_utcnow(),
    )
    await store.save_role_personality(record)


async def _handle_collaboration(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RoleCollaboration
    record = RoleCollaboration(
        collab_id=data.get("collab_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        partner_role_id=data.get("partner_role_id", ""),
        interaction_count=int(data.get("interaction_count", 1)),
        last_interaction_at=_utcnow(),
        quality_score=float(data.get("quality_score", 0.0)),
        notes=data.get("notes", ""),
    )
    await store.record_role_collaboration(record)


async def _handle_skill(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RoleSkillProficiency
    record = RoleSkillProficiency(
        skill_id=data.get("skill_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        category=data.get("category", "technical"),
        skill_name=data.get("skill_name", ""),
        level=float(data.get("level", 0.0)),
        learning_goals=data.get("learning_goals", []),
        updated_at=_utcnow(),
    )
    await store.save_role_skill(record)


async def _handle_output(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RoleOutputMetrics
    record = RoleOutputMetrics(
        metrics_id=data.get("metrics_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        week_label=data.get("week_label", ""),
        tasks_completed=int(data.get("tasks_completed", 0)),
        quality_score=float(data.get("quality_score", 0.0)),
        avg_duration=float(data.get("avg_duration", 0.0)),
        rework_count=int(data.get("rework_count", 0)),
        updated_at=_utcnow(),
    )
    await store.record_role_output_metrics(record)


async def _handle_resource(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RoleResourceUsage
    record = RoleResourceUsage(
        usage_id=data.get("usage_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        period=data.get("period", ""),
        tokens_in=int(data.get("tokens_in", 0)),
        tokens_out=int(data.get("tokens_out", 0)),
        cost_usd=float(data.get("cost_usd", 0.0)),
        duration_seconds=float(data.get("duration_seconds", 0.0)),
        model_breakdown=data.get("model_breakdown", {}),
        updated_at=_utcnow(),
    )
    await store.record_role_resource_usage(record)


async def _handle_task(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RoleTaskAssignment
    record = RoleTaskAssignment(
        assignment_id=data.get("assignment_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        work_item_id=data.get("work_item_id", ""),
        title=data.get("title", ""),
        column=data.get("column", "upcoming"),
        priority=int(data.get("priority", 0)),
        depends_on=data.get("depends_on", []),
        blocked_reason=data.get("blocked_reason", ""),
        updated_at=_utcnow(),
    )
    await store.save_role_task_assignment(record)


async def _handle_communication(store: Any, project_id: str, role_id: str, data: dict[str, Any]) -> None:
    from opc.core.models import RoleCommunicationRecord
    record = RoleCommunicationRecord(
        comm_id=data.get("comm_id", str(uuid.uuid4())),
        project_id=project_id,
        role_id=role_id,
        comm_type=data.get("comm_type", "discussion"),
        title=data.get("title", ""),
        content=data.get("content", ""),
        participants=data.get("participants", []),
        outcome=data.get("outcome", ""),
        created_at=_utcnow(),
    )
    await store.record_role_communication(record)


_SECTION_HANDLERS: dict[str, Any] = {
    "memory": _handle_memory,
    "work": _handle_work,
    "orientation": _handle_orientation,
    "personality": _handle_personality,
    "collaboration": _handle_collaboration,
    "skill": _handle_skill,
    "output": _handle_output,
    "resource": _handle_resource,
    "task": _handle_task,
    "communication": _handle_communication,
}
