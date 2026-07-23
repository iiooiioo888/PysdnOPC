"""預算守衛模組 — 多級預算控制與告警。

職責說明：
    在 LLM 調用前進行預算檢查，支援：
    - 全局預算上限
    - 按角色預算限制
    - 預算告警（80%、90%、100%）
    - 自動降級策略
    - 事前成本估算

使用範例：
    from opc.layer6_observability.budget_guard import BudgetGuard
    guard = BudgetGuard(total_budget=3.0, event_bus=bus)
    decision = await guard.check_before_call(
        role="researcher", model="gpt-4o", estimated_tokens=5000
    )
    if decision.allowed:
        # 執行調用
        await guard.record_usage(role="researcher", cost=0.05)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger

from opc.core.events import EventBus
from opc.core.models import OPCEvent


class BudgetDecision(Enum):
    """預算決策結果。"""
    PROCEED = "proceed"              # 正常執行
    DOWNGRADE_MODEL = "downgrade"    # 降級模型
    ASK_USER = "ask_user"           # 詢問用戶
    BLOCK = "block"                 # 阻斷執行


@dataclass
class BudgetStatus:
    """預算狀態快照。"""
    total_budget: float
    total_spent: float
    remaining: float
    usage_pct: float                 # 使用百分比 (0-100)
    role_spent: dict[str, float]     # 各角色花費
    alerts_triggered: list[str]      # 已觸發的告警
    is_blocked: bool = False


@dataclass
class UsageRecord:
    """使用記錄。"""
    role: str
    model: str
    cost: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    task_item: str = ""
    timestamp: float = field(default_factory=time.time)


class BudgetGuard:
    """預算守衛 — 多級預算控制。

    告警閾值：
    - 70%: 黃色警告（提醒用戶注意）
    - 85%: 橙色警告（建議降級）
    - 95%: 紅色警告（詢問是否繼續）
    - 100%: 阻斷（除非用戶明確覆蓋）

    降級策略：
    - 85%: 推薦降級 LIGHT 角色的模型
    - 95%: 自動降級所有非 HEAVY 角色
    - 100%: 阻斷，等待用戶干預
    """

    ALERT_THRESHOLDS = [0.70, 0.85, 0.95, 1.0]

    def __init__(
        self,
        total_budget: float = 0.0,
        per_role_limits: dict[str, float] | None = None,
        event_bus: EventBus | None = None,
        auto_downgrade: bool = True,
    ) -> None:
        self.total_budget = total_budget
        self.per_role_limits = per_role_limits or {}
        self.event_bus = event_bus
        self.auto_downgrade = auto_downgrade

        self._total_spent = 0.0
        self._role_spent: dict[str, float] = {}
        self._alerts_triggered: set[float] = set()
        self._usage_history: list[UsageRecord] = []
        self._user_override = False  # 用戶是否已覆蓋阻斷

    @property
    def budget_remaining(self) -> float:
        if self.total_budget <= 0:
            return float("inf")
        return max(0.0, self.total_budget - self._total_spent)

    @property
    def usage_pct(self) -> float:
        if self.total_budget <= 0:
            return 0.0
        return (self._total_spent / self.total_budget) * 100

    def get_status(self) -> BudgetStatus:
        """獲取當前預算狀態。"""
        return BudgetStatus(
            total_budget=self.total_budget,
            total_spent=self._total_spent,
            remaining=self.budget_remaining,
            usage_pct=self.usage_pct,
            role_spent=dict(self._role_spent),
            alerts_triggered=[str(t) for t in sorted(self._alerts_triggered)],
            is_blocked=(self.total_budget > 0 and self._total_spent >= self.total_budget),
        )

    async def check_before_call(
        self,
        role: str,
        model: str,
        estimated_tokens: int = 5000,
        tier: str = "medium",
    ) -> BudgetCheckResult:
        """在 LLM 調用前檢查預算。

        返回：
            BudgetCheckResult — 包含決策、建議模型、原因
        """
        # 無預算限制時直接通過
        if self.total_budget <= 0:
            return BudgetCheckResult(
                decision=BudgetDecision.PROCEED,
                model=model,
                reason="No budget limit set",
            )

        # 估算本次調用成本
        estimated_cost = self._estimate_cost(model, estimated_tokens)

        # 檢查全局預算
        if self._total_spent + estimated_cost > self.total_budget:
            if self._user_override:
                return BudgetCheckResult(
                    decision=BudgetDecision.PROCEED,
                    model=model,
                    reason="User override active",
                )
            return BudgetCheckResult(
                decision=BudgetDecision.BLOCK,
                model=model,
                reason=f"Budget exceeded: ${self._total_spent:.2f} + ${estimated_cost:.2f} > ${self.total_budget:.2f}",
            )

        # 檢查角色預算
        role_limit = self.per_role_limits.get(role, 0)
        if role_limit > 0:
            role_spent = self._role_spent.get(role, 0)
            if role_spent + estimated_cost > role_limit:
                return BudgetCheckResult(
                    decision=BudgetDecision.DOWNGRADE_MODEL,
                    model=model,
                    reason=f"Role '{role}' budget limit reached: ${role_spent:.2f}/${role_limit:.2f}",
                    suggested_tier="light",
                )

        # 檢查告警閾值
        new_pct = ((self._total_spent + estimated_cost) / self.total_budget) * 100

        if new_pct >= 95:
            await self._emit_alert("critical", role, new_pct)
            return BudgetCheckResult(
                decision=BudgetDecision.ASK_USER,
                model=model,
                reason=f"Budget at {new_pct:.0f}% — please confirm to continue",
                suggested_tier="light" if self.auto_downgrade else None,
            )

        if new_pct >= 85:
            await self._emit_alert("warning", role, new_pct)
            if self.auto_downgrade and tier != "light":
                return BudgetCheckResult(
                    decision=BudgetDecision.DOWNGRADE_MODEL,
                    model=model,
                    reason=f"Budget at {new_pct:.0f}% — auto-downgrading to save cost",
                    suggested_tier="light",
                )

        if new_pct >= 70:
            await self._emit_alert("info", role, new_pct)

        return BudgetCheckResult(
            decision=BudgetDecision.PROCEED,
            model=model,
            reason="Within budget",
        )

    async def record_usage(
        self,
        role: str,
        cost: float,
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        task_item: str = "",
    ) -> None:
        """記錄使用量。"""
        self._total_spent += cost
        self._role_spent[role] = self._role_spent.get(role, 0) + cost

        record = UsageRecord(
            role=role,
            model=model,
            cost=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            task_item=task_item,
        )
        self._usage_history.append(record)

        # 發佈事件
        if self.event_bus:
            await self.event_bus.publish(OPCEvent(
                event_type="budget.usage",
                payload={
                    "role": role,
                    "cost": cost,
                    "total_spent": self._total_spent,
                    "remaining": self.budget_remaining,
                    "usage_pct": self.usage_pct,
                    "model": model,
                },
            ))

    def set_user_override(self, override: bool) -> None:
        """設置用戶覆蓋（允許超預算）。"""
        self._user_override = override
        logger.info(f"Budget user override: {override}")

    def get_usage_summary(self) -> dict[str, Any]:
        """獲取使用摘要。"""
        return {
            "total_budget": self.total_budget,
            "total_spent": self._total_spent,
            "remaining": self.budget_remaining,
            "usage_pct": self.usage_pct,
            "role_breakdown": {
                role: {
                    "spent": spent,
                    "pct": (spent / self.total_budget * 100) if self.total_budget > 0 else 0,
                    "limit": self.per_role_limits.get(role, 0),
                }
                for role, spent in self._role_spent.items()
            },
            "total_calls": len(self._usage_history),
            "alerts": sorted(self._alerts_triggered),
        }

    # --- 內部方法 ---

    async def _emit_alert(self, severity: str, role: str, pct: float) -> None:
        """發送預算告警事件。"""
        threshold = round(pct / 5) * 5  # 四捨五入到 5 的倍數
        if threshold in self._alerts_triggered:
            return
        self._alerts_triggered.add(threshold)

        logger.warning(f"Budget alert [{severity}]: {pct:.0f}% used (triggered by role '{role}')")

        if self.event_bus:
            await self.event_bus.publish(OPCEvent(
                event_type="budget.alert",
                payload={
                    "severity": severity,
                    "role": role,
                    "usage_pct": pct,
                    "total_spent": self._total_spent,
                    "budget": self.total_budget,
                },
            ))

    def _estimate_cost(self, model: str, tokens: int) -> float:
        """估算調用成本。"""
        # 簡化估算：使用每 1k tokens 的平均成本
        model_lower = model.lower()
        cost_per_1k = 0.002  # 默認

        if "gpt-4o-mini" in model_lower:
            cost_per_1k = 0.0003
        elif "gpt-4o" in model_lower:
            cost_per_1k = 0.005
        elif "gpt-4.1-nano" in model_lower:
            cost_per_1k = 0.00025
        elif "gpt-4.1-mini" in model_lower:
            cost_per_1k = 0.001
        elif "gpt-4.1" in model_lower:
            cost_per_1k = 0.005
        elif "claude-sonnet" in model_lower:
            cost_per_1k = 0.009
        elif "claude-haiku" in model_lower:
            cost_per_1k = 0.003
        elif "deepseek-chat" in model_lower:
            cost_per_1k = 0.0002
        elif "deepseek" in model_lower:
            cost_per_1k = 0.001
        elif "gemini-flash" in model_lower:
            cost_per_1k = 0.00025
        elif "gemini" in model_lower:
            cost_per_1k = 0.003

        return cost_per_1k * (tokens / 1000)


@dataclass
class BudgetCheckResult:
    """預算檢查結果。"""
    decision: BudgetDecision
    model: str
    reason: str
    suggested_tier: str | None = None


def format_budget_status(status: BudgetStatus) -> str:
    """格式化預算狀態為人類可讀文本。"""
    if status.total_budget <= 0:
        return "💰 預算：無限制"

    bar_len = 30
    filled = int(bar_len * status.usage_pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    severity = ""
    if status.usage_pct >= 95:
        severity = "🔴"
    elif status.usage_pct >= 85:
        severity = "🟠"
    elif status.usage_pct >= 70:
        severity = "🟡"
    else:
        severity = "🟢"

    lines = [
        f"{severity} 預算狀態: ${status.total_spent:.2f} / ${status.total_budget:.2f} ({status.usage_pct:.0f}%)",
        f"  {bar}",
        f"  剩餘: ${status.remaining:.2f}",
    ]

    if status.role_spent:
        lines.append("\n  角色花費:")
        for role, spent in sorted(status.role_spent.items(), key=lambda x: -x[1]):
            pct = (spent / status.total_budget * 100) if status.total_budget > 0 else 0
            role_bar_len = int(15 * pct / 100)
            role_bar = "█" * role_bar_len + "░" * (15 - role_bar_len)
            lines.append(f"    {role:<16} ${spent:.2f}  {role_bar}")

    return "\n".join(lines)
