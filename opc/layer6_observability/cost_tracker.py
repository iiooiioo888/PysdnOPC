"""LLM API 使用成本追蹤。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from opc.database.store import OPCStore
from opc.core.events import EventBus
from opc.core.models import OPCEvent, CostEvent


@dataclass
class CostEntry:
    task_id: str | None = None
    agent_id: str | None = None
    org_id: str | None = None
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class BudgetStatus:
    """預算狀態報告。"""

    task_spent: float = 0.0
    task_limit: float = 0.0
    session_spent: float = 0.0
    session_limit: float = 0.0
    monthly_spent: float = 0.0
    monthly_limit: float = 0.0

    @property
    def task_usage_pct(self) -> float:
        """任務預算使用率。"""
        if self.task_limit <= 0:
            return 0.0
        return min(100.0, (self.task_spent / self.task_limit) * 100)

    @property
    def session_usage_pct(self) -> float:
        """會話預算使用率。"""
        if self.session_limit <= 0:
            return 0.0
        return min(100.0, (self.session_spent / self.session_limit) * 100)

    @property
    def monthly_usage_pct(self) -> float:
        """月度預算使用率。"""
        if self.monthly_limit <= 0:
            return 0.0
        return min(100.0, (self.monthly_spent / self.monthly_limit) * 100)

    def to_dict(self) -> dict[str, Any]:
        """轉換為字典。"""
        return {
            "task": {
                "spent": self.task_spent,
                "limit": self.task_limit,
                "usage_pct": self.task_usage_pct,
            },
            "session": {
                "spent": self.session_spent,
                "limit": self.session_limit,
                "usage_pct": self.session_usage_pct,
            },
            "monthly": {
                "spent": self.monthly_spent,
                "limit": self.monthly_limit,
                "usage_pct": self.monthly_usage_pct,
            },
        }


class CostTracker:
    """Tracks LLM API costs per task and agent."""

    def __init__(self, store: OPCStore, event_bus: EventBus | None = None) -> None:
        self.store = store
        self.event_bus = event_bus
        self._session_total = 0.0

    async def record(self, entry: CostEntry) -> None:
        await self.store.record_cost(
            task_id=entry.task_id,
            agent_id=entry.agent_id,
            model=entry.model,
            tokens_in=entry.tokens_in,
            tokens_out=entry.tokens_out,
            cost=entry.cost,
        )
        # Also record CostEvent for cost_events table (org-scoped tracking)
        event = CostEvent(
            org_id=entry.org_id,
            agent_id=entry.agent_id,
            task_id=entry.task_id,
            model=entry.model,
            tokens_in=entry.tokens_in,
            tokens_out=entry.tokens_out,
            cost_usd=entry.cost,
            timestamp=entry.timestamp,
        )
        await self.store.record_cost_event(event)
        self._session_total += entry.cost

        if self.event_bus:
            await self.event_bus.publish(OPCEvent(
                event_type="cost_update",
                payload={
                    "task_id": entry.task_id,
                    "cost": entry.cost,
                    "session_total": self._session_total,
                },
            ))

    async def check_budget(
        self,
        agent_id: str | None = None,
        org_id: str | None = None,
    ) -> tuple[bool, str]:
        """Check if agent/org is within budget. Returns (allowed, reason)."""
        return await check_budget(self.store, agent_id=agent_id, org_id=org_id)

    async def get_summary(self, project_id: str | None = None) -> dict[str, Any]:
        db_totals = await self.store.get_total_cost(project_id)
        return {
            **db_totals,
            "session_cost": self._session_total,
        }

    async def get_task_breakdown(self, task_id: str) -> dict[str, Any]:
        """取得任務的成本明細（按模型和代理）。

        Args:
            task_id: 任務 ID

        Returns:
            包含成本明細的字典。
        """
        # 從資料庫取得任務相關的成本記錄
        try:
            costs = await self.store.get_task_costs(task_id)
            breakdown: dict[str, Any] = {
                "task_id": task_id,
                "total_cost": 0.0,
                "by_model": {},
                "by_agent": {},
                "entries": [],
            }
            for entry in costs:
                cost = entry.get("cost", 0.0)
                model = entry.get("model", "unknown")
                agent = entry.get("agent_id", "unknown")

                breakdown["total_cost"] += cost
                breakdown["by_model"][model] = breakdown["by_model"].get(model, 0.0) + cost
                breakdown["by_agent"][agent] = breakdown["by_agent"].get(agent, 0.0) + cost
                breakdown["entries"].append(entry)

            return breakdown
        except Exception as e:
            logger.warning("Failed to get task breakdown: {}", e)
            return {"task_id": task_id, "total_cost": 0.0, "error": str(e)}

    def get_budget_status(
        self,
        task_spent: float = 0.0,
        session_spent: float | None = None,
        budget_config: Any | None = None,
    ) -> BudgetStatus:
        """取得目前預算狀態。

        Args:
            task_spent: 任務已花費
            session_spent: 會話已花費（None 則使用內部計量）
            budget_config: 預算配置（BudgetConfig）

        Returns:
            BudgetStatus 物件。
        """
        session = session_spent if session_spent is not None else self._session_total

        status = BudgetStatus(
            task_spent=task_spent,
            session_spent=session,
            monthly_spent=self._session_total,  # 簡化：使用會話總計作為月度近似
        )

        if budget_config:
            status.task_limit = budget_config.task_limit_usd
            status.session_limit = budget_config.session_limit_usd
            status.monthly_limit = budget_config.monthly_limit_usd

        return status

    @property
    def session_total(self) -> float:
        return self._session_total


async def check_budget(
    store: OPCStore,
    agent_id: str | None = None,
    org_id: str | None = None,
) -> tuple[bool, str]:
    """Check if agent/org is within budget. Returns (allowed, reason)."""
    if org_id:
        org = await store.get_organization(org_id)
        if org and org.budget_monthly_cents > 0:
            if org.spent_monthly_cents >= org.budget_monthly_cents:
                return False, f"Organization '{org.name}' has exceeded its monthly budget"
    if agent_id and org_id:
        agents = await store.list_org_agents(org_id)
        for agent in agents:
            if agent.agent_id == agent_id or agent.role_id == agent_id:
                if agent.budget_monthly_cents > 0 and agent.spent_monthly_cents >= agent.budget_monthly_cents:
                    return False, f"Agent '{agent.name}' has exceeded its monthly budget"
                break
    return True, ""
