"""預算守衛模組 — 多級預算控制、公司級追蹤與報告。

職責說明：
    在 LLM 調用前進行預算檢查，支援：
    - 多級限額：總預算 / 任務 / 會話 / 月度
    - 按角色預算限制
    - 公司級追蹤：按公司（org）累計花費、公司限額檢查
    - 預算告警（70%、85%、95%、100%）
    - 自動降級策略
    - 事前成本估算
    - 多級 + 公司級報告生成

使用範例：
    from opc.layer6_observability.budget_guard import BudgetGuard
    guard = BudgetGuard(total_budget=3.0, event_bus=bus)
    guard.set_context(role="researcher", task_id="t-1", company_id="org-1")
    decision = await guard.check_before_call(
        role="researcher", model="gpt-4o", estimated_tokens=5000
    )
    if decision.allowed:
        # 執行調用
        await guard.record_usage(role="researcher", cost=0.05, company_id="org-1")
    report = guard.generate_report()
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from loguru import logger

from opc.core.events import EventBus
from opc.core.models import OPCEvent

if TYPE_CHECKING:
    from opc.core.config import BudgetConfig


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
    # 多級限額追蹤
    task_budget: float = 0.0
    task_spent: float = 0.0
    session_budget: float = 0.0
    session_spent: float = 0.0
    monthly_budget: float = 0.0
    monthly_spent: float = 0.0
    # 公司級追蹤
    company_spent: dict[str, float] = field(default_factory=dict)


@dataclass
class UsageRecord:
    """使用記錄。"""
    role: str
    model: str
    cost: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    task_item: str = ""
    company_id: str = ""
    task_id: str = ""
    timestamp: float = field(default_factory=time.time)


def _current_month_key() -> str:
    """取得目前月份鍵值（UTC，格式 "YYYY-MM"）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m")


class BudgetGuard:
    """預算守衛 — 多級預算控制與公司級追蹤。

    層級：
    - total：總預算上限（跨任務累計）
    - task：單任務上限（切換任務時自動重置）
    - session：單會話上限
    - monthly：月度上限（跨月自動滾動重置）
    - role：按角色上限
    - company：按公司（org）上限與追蹤

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
        task_budget: float = 0.0,
        session_budget: float = 0.0,
        monthly_budget: float = 0.0,
        company_limits: dict[str, float] | None = None,
        default_company_limit: float = 0.0,
    ) -> None:
        self.total_budget = total_budget
        self.per_role_limits = per_role_limits or {}
        self.event_bus = event_bus
        self.auto_downgrade = auto_downgrade
        self.task_budget = task_budget
        self.session_budget = session_budget
        self.monthly_budget = monthly_budget
        self.company_limits = company_limits or {}
        self.default_company_limit = default_company_limit

        self._total_spent = 0.0
        self._task_spent = 0.0
        self._session_spent = 0.0
        self._monthly_spent = 0.0
        self._month_key = _current_month_key()
        self._role_spent: dict[str, float] = {}
        self._company_spent: dict[str, float] = {}
        self._alerts_triggered: set[str] = set()
        self._usage_history: list[UsageRecord] = []
        self._user_override = False  # 用戶是否已覆蓋阻斷
        # 當前上下文
        self._role = ""
        self._task_id = ""
        self._session_id = ""
        self._company_id = ""

    # --- 工廠方法 ---

    @classmethod
    def from_budget_config(
        cls,
        budget_config: BudgetConfig,
        event_bus: EventBus | None = None,
        auto_downgrade: bool = True,
    ) -> "BudgetGuard":
        """從 BudgetConfig 建立預算守衛。

        映射規則：
        - total_budget：以月度上限作為總預算上限（0=不限制）
        - task/session/monthly：直接對應三級限額
        - per_role_limits / company_limits：對應角色與公司限額
        """
        return cls(
            total_budget=budget_config.monthly_limit_usd,
            per_role_limits=dict(budget_config.role_limits_usd),
            event_bus=event_bus,
            auto_downgrade=auto_downgrade,
            task_budget=budget_config.task_limit_usd,
            session_budget=budget_config.session_limit_usd,
            monthly_budget=budget_config.monthly_limit_usd,
            company_limits=dict(budget_config.company_limits_usd),
            default_company_limit=budget_config.company_default_limit_usd,
        )

    # --- 上下文與生命週期 ---

    def set_context(
        self,
        role: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        company_id: str | None = None,
    ) -> None:
        """設定當前角色/任務/會話/公司上下文。

        切換到不同任務時自動重置任務級計量與任務級告警，
        確保任務預算上限以任務為週期獨立計算。
        """
        if task_id is not None and task_id != self._task_id:
            self._task_spent = 0.0
            self._alerts_triggered = {
                key for key in self._alerts_triggered if not key.startswith("task:")
            }
            self._task_id = task_id
        if role is not None:
            self._role = role
        if session_id is not None:
            self._session_id = session_id
        if company_id is not None:
            self._company_id = company_id

    def reset_task(self) -> None:
        """重置任務級別計量（新任務開始時呼叫）。"""
        self._task_spent = 0.0
        self._task_id = ""
        self._alerts_triggered = {
            key for key in self._alerts_triggered if not key.startswith("task:")
        }

    def reset_session(self) -> None:
        """重置會話級別計量（新會話開始時呼叫）。"""
        self._session_spent = 0.0
        self._task_spent = 0.0
        self._task_id = ""
        self._alerts_triggered = {
            key
            for key in self._alerts_triggered
            if not key.startswith(("task:", "session:"))
        }

    def _maybe_roll_month(self) -> None:
        """跨月滾動：月份變更時重置月度計量與月度告警。"""
        key = _current_month_key()
        if key != self._month_key:
            self._month_key = key
            self._monthly_spent = 0.0
            self._alerts_triggered = {
                key_ for key_ in self._alerts_triggered if not key_.startswith("monthly:")
            }

    # --- 屬性 ---

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

    @property
    def total_spent(self) -> float:
        return self._total_spent

    @property
    def task_spent(self) -> float:
        return self._task_spent

    @property
    def session_spent(self) -> float:
        return self._session_spent

    @property
    def monthly_spent(self) -> float:
        return self._monthly_spent

    @property
    def company_spent(self) -> dict[str, float]:
        return dict(self._company_spent)

    # --- 呼叫前決策 ---

    async def check_before_call(
        self,
        role: str,
        model: str,
        estimated_tokens: int = 5000,
        tier: str = "medium",
        company_id: str | None = None,
    ) -> "BudgetCheckResult":
        """在 LLM 調用前檢查預算（多級 + 公司級）。

        返回：
            BudgetCheckResult — 包含決策、建議模型、原因
        """
        self._maybe_roll_month()

        # 估算本次調用成本
        estimated_cost = self._estimate_cost(model, estimated_tokens)
        effective_company = str(company_id or "").strip() or self._company_id

        # 檢查公司預算（硬阻斷）
        company_limit = self.get_company_limit(effective_company) if effective_company else 0.0
        if company_limit > 0:
            company_spent = self._company_spent.get(effective_company, 0.0)
            if company_spent + estimated_cost > company_limit and not self._user_override:
                await self._emit_alert(
                    "critical", role, 100.0,
                    level_key=f"company:{effective_company}",
                    extra={"company_id": effective_company},
                )
                return BudgetCheckResult(
                    decision=BudgetDecision.BLOCK,
                    model=model,
                    reason=(
                        f"Company '{effective_company}' budget exceeded: "
                        f"${company_spent:.2f} + ${estimated_cost:.2f} > ${company_limit:.2f}"
                    ),
                )

        # 檢查任務預算（硬阻斷）
        if self.task_budget > 0:
            if self._task_spent + estimated_cost > self.task_budget and not self._user_override:
                await self._emit_alert(
                    "critical", role, 100.0,
                    level_key="task",
                    extra={"task_id": self._task_id},
                )
                return BudgetCheckResult(
                    decision=BudgetDecision.BLOCK,
                    model=model,
                    reason=(
                        f"Task budget exceeded: ${self._task_spent:.2f} + "
                        f"${estimated_cost:.2f} > ${self.task_budget:.2f}"
                    ),
                )

        # 檢查會話/月度預算（硬阻斷）
        for level_name, spent, limit in (
            ("session", self._session_spent, self.session_budget),
            ("monthly", self._monthly_spent, self.monthly_budget),
        ):
            if limit > 0 and spent + estimated_cost > limit and not self._user_override:
                await self._emit_alert(
                    "critical", role, 100.0,
                    level_key=level_name,
                )
                return BudgetCheckResult(
                    decision=BudgetDecision.BLOCK,
                    model=model,
                    reason=(
                        f"{level_name.capitalize()} budget exceeded: ${spent:.2f} + "
                        f"${estimated_cost:.2f} > ${limit:.2f}"
                    ),
                )

        # 無總預算限制時通過（角色/告警檢查仍需總預算基準）
        if self.total_budget <= 0:
            return BudgetCheckResult(
                decision=BudgetDecision.PROCEED,
                model=model,
                reason="No budget limit set",
            )

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

    async def check_company_budget(
        self,
        company_id: str,
        estimated_cost: float = 0.0,
    ) -> "BudgetCheckResult":
        """執行前檢查指定公司的預算餘量（不估算調用成本）。

        返回：
            BudgetCheckResult — BLOCK 表示該公司已達限額。
        """
        company_id = str(company_id or "").strip()
        if not company_id:
            return BudgetCheckResult(
                decision=BudgetDecision.PROCEED, model="", reason="No company context",
            )
        limit = self.get_company_limit(company_id)
        if limit <= 0:
            return BudgetCheckResult(
                decision=BudgetDecision.PROCEED, model="", reason="No company limit set",
            )
        spent = self._company_spent.get(company_id, 0.0)
        if spent + max(0.0, estimated_cost) >= limit and not self._user_override:
            await self._emit_alert(
                "critical", self._role, 100.0,
                level_key=f"company:{company_id}",
                extra={"company_id": company_id},
            )
            return BudgetCheckResult(
                decision=BudgetDecision.BLOCK,
                model="",
                reason=(
                    f"Company '{company_id}' monthly budget exhausted: "
                    f"${spent:.2f} / ${limit:.2f}"
                ),
            )
        return BudgetCheckResult(
            decision=BudgetDecision.PROCEED,
            model="",
            reason="Company within budget",
        )

    # --- 使用量記錄 ---

    async def record_usage(
        self,
        role: str,
        cost: float,
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        task_item: str = "",
        company_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """記錄使用量（累計到總/任務/會話/月度/角色/公司各層級）。"""
        self._maybe_roll_month()
        cost = float(cost or 0.0)
        self._total_spent += cost
        self._task_spent += cost
        self._session_spent += cost
        self._monthly_spent += cost
        if role:
            self._role_spent[role] = self._role_spent.get(role, 0.0) + cost

        effective_company = str(company_id or "").strip() or self._company_id
        if effective_company:
            self._company_spent[effective_company] = (
                self._company_spent.get(effective_company, 0.0) + cost
            )

        record = UsageRecord(
            role=role,
            model=model,
            cost=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            task_item=task_item,
            company_id=effective_company,
            task_id=str(task_id or "").strip() or self._task_id,
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
                    "company_id": effective_company,
                    "company_spent": self._company_spent.get(effective_company, 0.0),
                    "task_spent": self._task_spent,
                },
            ))

    # --- 公司級查詢與報告 ---

    def get_company_limit(self, company_id: str) -> float:
        """取得指定公司的限額（專屬限額優先，其次預設公司限額，0=不限制）。"""
        company_id = str(company_id or "").strip()
        if not company_id:
            return 0.0
        limit = float(self.company_limits.get(company_id, 0.0) or 0.0)
        if limit <= 0:
            limit = float(self.default_company_limit or 0.0)
        return limit

    def get_company_status(self, company_id: str) -> dict[str, Any]:
        """取得單一公司的預算狀態。"""
        company_id = str(company_id or "").strip()
        spent = self._company_spent.get(company_id, 0.0)
        limit = self.get_company_limit(company_id)
        return {
            "company_id": company_id,
            "spent": spent,
            "limit": limit,
            "remaining": max(0.0, limit - spent) if limit > 0 else float("inf"),
            "usage_pct": (spent / limit * 100) if limit > 0 else 0.0,
            "is_blocked": limit > 0 and spent >= limit,
        }

    def get_company_report(self) -> dict[str, dict[str, Any]]:
        """取得所有已知公司的預算報告（含限額與使用率）。"""
        company_ids = set(self._company_spent) | set(self.company_limits)
        return {
            company_id: self.get_company_status(company_id)
            for company_id in sorted(company_ids)
        }

    def generate_report(self) -> dict[str, Any]:
        """生成多級 + 公司級預算報告。"""
        self._maybe_roll_month()

        def _level(spent: float, limit: float) -> dict[str, Any]:
            return {
                "spent": spent,
                "limit": limit,
                "usage_pct": (spent / limit * 100) if limit > 0 else 0.0,
                "is_blocked": limit > 0 and spent >= limit,
            }

        return {
            "levels": {
                "total": _level(self._total_spent, self.total_budget),
                "task": _level(self._task_spent, self.task_budget),
                "session": _level(self._session_spent, self.session_budget),
                "monthly": _level(self._monthly_spent, self.monthly_budget),
            },
            "roles": {
                role: {
                    "spent": spent,
                    "limit": float(self.per_role_limits.get(role, 0.0) or 0.0),
                    "pct": (spent / self.total_budget * 100) if self.total_budget > 0 else 0.0,
                }
                for role, spent in self._role_spent.items()
            },
            "companies": self.get_company_report(),
            "total_calls": len(self._usage_history),
            "alerts": sorted(self._alerts_triggered),
        }

    # --- 狀態查詢（向後相容） ---

    def get_status(self) -> BudgetStatus:
        """獲取當前預算狀態。"""
        self._maybe_roll_month()
        return BudgetStatus(
            total_budget=self.total_budget,
            total_spent=self._total_spent,
            remaining=self.budget_remaining,
            usage_pct=self.usage_pct,
            role_spent=dict(self._role_spent),
            alerts_triggered=[str(t) for t in sorted(self._alerts_triggered)],
            is_blocked=(self.total_budget > 0 and self._total_spent >= self.total_budget),
            task_budget=self.task_budget,
            task_spent=self._task_spent,
            session_budget=self.session_budget,
            session_spent=self._session_spent,
            monthly_budget=self.monthly_budget,
            monthly_spent=self._monthly_spent,
            company_spent=dict(self._company_spent),
        )

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
            "company_breakdown": self.get_company_report(),
            "total_calls": len(self._usage_history),
            "alerts": sorted(self._alerts_triggered),
        }

    # --- 內部方法 ---

    async def _emit_alert(
        self,
        severity: str,
        role: str,
        pct: float,
        level_key: str = "total",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """發送預算告警事件（按 level_key + 閾值去重）。"""
        threshold = round(pct / 5) * 5  # 四捨五入到 5 的倍數
        alert_key = f"{level_key}:{threshold}"
        if alert_key in self._alerts_triggered:
            return
        self._alerts_triggered.add(alert_key)

        logger.warning(
            f"Budget alert [{severity}] ({level_key}): {pct:.0f}% used (triggered by role '{role}')"
        )

        if self.event_bus:
            payload: dict[str, Any] = {
                "severity": severity,
                "role": role,
                "usage_pct": pct,
                "total_spent": self._total_spent,
                "budget": self.total_budget,
                "level": level_key,
            }
            if extra:
                payload.update(extra)
            await self.event_bus.publish(OPCEvent(
                event_type="budget.alert",
                payload=payload,
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

    @property
    def allowed(self) -> bool:
        """是否允許繼續執行（BLOCK 以外的決策）。"""
        return self.decision != BudgetDecision.BLOCK


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


def format_budget_report(report: dict[str, Any]) -> str:
    """格式化多級 + 公司級預算報告為人類可讀文本。"""
    lines = ["💰 預算報告"]

    level_labels = {
        "total": "總預算",
        "task": "任務",
        "session": "會話",
        "monthly": "月度",
    }
    lines.append("  層級使用:")
    for level_name, label in level_labels.items():
        level = (report.get("levels") or {}).get(level_name) or {}
        limit = float(level.get("limit", 0.0) or 0.0)
        spent = float(level.get("spent", 0.0) or 0.0)
        if limit <= 0:
            lines.append(f"    {label:<6} 無限制（已花費 ${spent:.2f}）")
        else:
            pct = float(level.get("usage_pct", 0.0) or 0.0)
            flag = "🔴" if level.get("is_blocked") else ("🟡" if pct >= 70 else "🟢")
            lines.append(f"    {flag} {label:<6} ${spent:.2f} / ${limit:.2f} ({pct:.0f}%)")

    roles = report.get("roles") or {}
    if roles:
        lines.append("  角色花費:")
        for role, info in sorted(roles.items(), key=lambda kv: -float(kv[1].get("spent", 0.0))):
            limit = float(info.get("limit", 0.0) or 0.0)
            limit_text = f"（限額 ${limit:.2f}）" if limit > 0 else ""
            lines.append(f"    {role:<16} ${float(info.get('spent', 0.0)):.2f}{limit_text}")

    companies = report.get("companies") or {}
    if companies:
        lines.append("  公司花費:")
        for company_id, info in companies.items():
            limit = float(info.get("limit", 0.0) or 0.0)
            spent = float(info.get("spent", 0.0) or 0.0)
            if limit <= 0:
                lines.append(f"    {company_id:<16} ${spent:.2f}（無限額）")
            else:
                pct = float(info.get("usage_pct", 0.0) or 0.0)
                flag = "🔴" if info.get("is_blocked") else ("🟡" if pct >= 70 else "🟢")
                lines.append(f"    {flag} {company_id:<16} ${spent:.2f} / ${limit:.2f} ({pct:.0f}%)")

    total_calls = int(report.get("total_calls", 0) or 0)
    lines.append(f"  累計調用: {total_calls}")
    alerts = report.get("alerts") or []
    if alerts:
        lines.append(f"  已觸發告警: {', '.join(str(a) for a in alerts)}")

    return "\n".join(lines)
