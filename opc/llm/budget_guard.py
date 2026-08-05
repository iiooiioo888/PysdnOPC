"""預算守衛 — LLM 呼叫前的預算攔截器。

在每次 LLM 呼叫前檢查預算餘量，根據配置決定：
- allow: 正常允許呼叫
- warn: 允許但發出預警（預設 80% 閾值）
- degrade: 降級到較便宜的模型（預設 90% 閾值）
- ask_user: 軟性超限（hard_stop=False），詢問使用者是否繼續
- block: 阻止呼叫（預算耗盡且 hard_stop=True）

監控與告警：
    當任一层级（任務/會話/月度/角色）使用率達到 warn_threshold 或
    degrade_threshold 時，除了日誌記錄外，會透過 EventBus 發布
    "budget.alert" 事件（含 severity、level、usage_pct），供 UI 與
    頻道層消費。告警按 (level, stage) 去重，reset_task / reset_session /
    跨月滾動時重置。

追蹤與記錄：
    post_call() 累計任務/會話/月度/角色花费；set_context() 設定目前
    角色與任務上下文（切換任務時自動重置任務級計量）。get_status()
    回報各層級花費與上限的比較。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from loguru import logger

from opc.core.models import OPCEvent

if TYPE_CHECKING:
    from opc.core.config import BudgetConfig, LLMConfig
    from opc.core.events import EventBus
    from opc.layer6_observability.cost_tracker import CostTracker


class BudgetAction(str, Enum):
    """預算決策動作。"""

    ALLOW = "allow"  # 正常允許
    WARN = "warn"  # 允許但預警
    DEGRADE = "degrade"  # 降級模型
    ASK_USER = "ask_user"  # 詢問使用者（軟性超限）
    BLOCK = "block"  # 阻止呼叫


@dataclass
class BudgetDecision:
    """預算決策結果。"""

    action: BudgetAction
    reason: str = ""
    original_tier: str = ""
    degraded_model: str | None = None
    budget_usage: dict[str, float] | None = None  # 各層級使用率
    level: str = ""  # 觸發決策的層級（task/session/monthly/role）

    @property
    def should_proceed(self) -> bool:
        """是否應該繼續執行 LLM 呼叫。"""
        return self.action != BudgetAction.BLOCK


# 預設的模型價格估算（每百萬 token，美元）
# 用於在 litellm 無法提供價格時的後備估算
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    # model_prefix: (input_price_per_mtok, output_price_per_mtok)
    "gpt-5.4": (15.0, 75.0),
    "gpt-5.4-mini": (3.0, 15.0),
    "gpt-5.4-nano": (0.5, 2.5),
    "gpt-5": (10.0, 50.0),
    "gpt-5-mini": (2.0, 10.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.15, 0.6),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int = 0) -> float:
    """估算 LLM 呼叫成本。

    Args:
        model: 模型名稱（litellm 格式）
        input_tokens: 輸入 token 數
        output_tokens: 輸出 token 數（預估）

    Returns:
        預估成本（美元）
    """
    # 嘗試從 litellm 取得價格
    try:
        import litellm

        info = litellm.get_model_info(model)
        input_price = info.get("input_cost_per_token", 0) * 1_000_000
        output_price = info.get("output_cost_per_token", 0) * 1_000_000
        if input_price > 0 or output_price > 0:
            return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
    except Exception:
        pass

    # 使用預設價格表
    model_lower = model.lower()
    for prefix, (in_price, out_price) in _DEFAULT_PRICING.items():
        if prefix in model_lower:
            return (input_tokens * in_price + output_tokens * out_price) / 1_000_000

    # 完全未知模型，使用保守估算
    return (input_tokens * 3.0 + output_tokens * 15.0) / 1_000_000


def _current_month_key() -> str:
    """取得目前月份的鍵值（UTC，格式 "YYYY-MM"）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m")


class BudgetGuard:
    """LLM 呼叫前的預算攔截器。

    使用方式：
        guard = BudgetGuard(budget_config, llm_config, cost_tracker, event_bus)
        guard.set_context(role="researcher", task_id="task-1")
        decision = await guard.pre_call("routine", estimated_tokens=1000)
        if decision.should_proceed:
            model = decision.degraded_model or original_model
            # 執行 LLM 呼叫
            await guard.post_call(actual_cost)
    """

    def __init__(
        self,
        budget_config: BudgetConfig,
        llm_config: LLMConfig | None = None,
        cost_tracker: CostTracker | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.config = budget_config
        self.llm_config = llm_config
        self.tracker = cost_tracker
        self.event_bus = event_bus
        self._task_spent = 0.0
        self._session_spent = 0.0
        self._monthly_spent = 0.0
        self._month_key = _current_month_key()
        self._role_spent: dict[str, float] = {}
        self._role = ""
        self._task_id = ""
        # 告警去重：(level, stage)，stage ∈ {"warn", "degrade", "exceeded"}
        self._alerts_fired: set[tuple[str, str]] = set()

    # --- 上下文與生命週期 ---

    def set_context(self, role: str | None = None, task_id: str | None = None) -> None:
        """設定目前的角色/任務上下文。

        切換到不同任務時自動重置任務級計量與任務級告警，
        確保單任務預算上限以任務為週期獨立計算。
        """
        if task_id is not None and task_id != self._task_id:
            self._task_spent = 0.0
            self._alerts_fired = {key for key in self._alerts_fired if key[0] != "task"}
            self._task_id = task_id
        if role is not None:
            self._role = role

    def reset_task(self) -> None:
        """重置任務級別計量（新任務開始時呼叫）。"""
        self._task_spent = 0.0
        self._task_id = ""
        self._alerts_fired = {key for key in self._alerts_fired if key[0] != "task"}

    def reset_session(self) -> None:
        """重置會話級別計量（新會話開始時呼叫）。"""
        self._session_spent = 0.0
        self._task_spent = 0.0
        self._task_id = ""
        self._alerts_fired = {
            key for key in self._alerts_fired if key[0] not in ("task", "session")
        }

    def _maybe_roll_month(self) -> None:
        """跨月滾動：月份變更時重置月度計量與告警。"""
        key = _current_month_key()
        if key != self._month_key:
            self._month_key = key
            self._monthly_spent = 0.0
            self._alerts_fired = {
                key_ for key_ in self._alerts_fired if key_[0] != "monthly"
            }

    # --- 呼叫前決策 ---

    async def pre_call(
        self,
        tier: str,
        estimated_tokens: int,
        model: str | None = None,
    ) -> BudgetDecision:
        """LLM 呼叫前檢查。

        Args:
            tier: 模型層級（critical, reasoning, routine, summary）
            estimated_tokens: 預估輸入 token 數
            model: 原始模型名稱（用於成本估算）

        Returns:
            BudgetDecision 包含決策動作和相關資訊。
        """
        self._maybe_roll_month()

        # 如果沒有配置預算限制，直接允許
        if not self._has_any_limit():
            return BudgetDecision(action=BudgetAction.ALLOW)

        # 估算本次呼叫成本
        estimated_cost = estimate_cost(
            model or "unknown",
            estimated_tokens,
            output_tokens=estimated_tokens // 4,  # 粗略預估輸出
        )

        # 計算各層級的預期花費
        task_after = self._task_spent + estimated_cost
        session_after = self._session_spent + estimated_cost

        # 取得月度花費（內部計量與 tracker 取較大值，避免漏計）
        monthly_spent = self._monthly_spent
        if self.tracker:
            try:
                summary = await self.tracker.get_summary()
                monthly_spent = max(monthly_spent, float(summary.get("total_cost", 0.0)))
            except Exception:
                pass
        monthly_after = monthly_spent + estimated_cost

        budget_usage = {
            "task": task_after,
            "session": session_after,
            "monthly": monthly_after,
        }

        # 檢查角色預算
        role_limit = self.config.get_role_limit(self._role) if self._role else 0.0
        if role_limit > 0:
            role_after = self._role_spent.get(self._role, 0.0) + estimated_cost
            if role_after >= role_limit:
                await self._fire_alert("role", "exceeded", "critical", role_after, role_limit)
                if self.config.hard_stop:
                    return BudgetDecision(
                        action=BudgetAction.BLOCK,
                        reason=f"角色 '{self._role}' 預算已耗盡（${role_after:.4f} >= ${role_limit:.2f}）",
                        original_tier=tier,
                        budget_usage=budget_usage,
                        level="role",
                    )
                degraded_model = self._get_degraded_model(tier)
                if degraded_model:
                    return BudgetDecision(
                        action=BudgetAction.DEGRADE,
                        reason=f"角色 '{self._role}' 預算達到上限，降級到較便宜的模型",
                        original_tier=tier,
                        degraded_model=degraded_model,
                        budget_usage=budget_usage,
                        level="role",
                    )
                return BudgetDecision(
                    action=BudgetAction.ASK_USER,
                    reason=f"角色 '{self._role}' 預算已耗盡（${role_after:.4f} >= ${role_limit:.2f}）",
                    original_tier=tier,
                    budget_usage=budget_usage,
                    level="role",
                )

        # 檢查是否超過預算（硬停止）
        if self.config.hard_stop:
            for level, after in (("task", task_after), ("session", session_after), ("monthly", monthly_after)):
                if self.config.is_exceeded(level, after):
                    limit = self.config.get_effective_limit(level)
                    await self._fire_alert(level, "exceeded", "critical", after, limit)
                    label = {"task": "任務", "session": "會話", "monthly": "月度"}[level]
                    return BudgetDecision(
                        action=BudgetAction.BLOCK,
                        reason=f"{label}預算已耗盡（${after:.4f} >= ${limit:.2f}）",
                        original_tier=tier,
                        budget_usage=budget_usage,
                        level=level,
                    )

        # 檢查是否需要降級
        should_degrade = (
            self.config.should_degrade("task", task_after)
            or self.config.should_degrade("session", session_after)
            or self.config.should_degrade("monthly", monthly_after)
        )

        if should_degrade:
            await self._fire_degrade_alerts(
                (("task", task_after), ("session", session_after), ("monthly", monthly_after))
            )
            degraded_model = self._get_degraded_model(tier)
            if degraded_model:
                logger.info(
                    "BudgetGuard: 降級模型 {} -> {} (tier={})",
                    model or tier,
                    degraded_model,
                    tier,
                )
                return BudgetDecision(
                    action=BudgetAction.DEGRADE,
                    reason="預算接近上限，降級到較便宜的模型",
                    original_tier=tier,
                    degraded_model=degraded_model,
                    budget_usage=budget_usage,
                )

        # 軟性超限（hard_stop=False 且無可降級模型）：詢問使用者
        for level, after in (("task", task_after), ("session", session_after), ("monthly", monthly_after)):
            if self.config.is_exceeded(level, after):
                limit = self.config.get_effective_limit(level)
                await self._fire_alert(level, "exceeded", "critical", after, limit)
                label = {"task": "任務", "session": "會話", "monthly": "月度"}[level]
                return BudgetDecision(
                    action=BudgetAction.ASK_USER,
                    reason=f"{label}預算已超限（${after:.4f} >= ${limit:.2f}），是否繼續？",
                    original_tier=tier,
                    budget_usage=budget_usage,
                    level=level,
                )

        # 檢查是否需要預警
        should_warn = (
            self.config.should_warn("task", task_after)
            or self.config.should_warn("session", session_after)
            or self.config.should_warn("monthly", monthly_after)
        )

        if should_warn:
            await self._fire_warn_alerts(
                (("task", task_after), ("session", session_after), ("monthly", monthly_after))
            )
            logger.warning(
                "BudgetGuard: 預算預警 (task=${:.4f}, session=${:.4f}, monthly=${:.4f})",
                task_after,
                session_after,
                monthly_after,
            )
            return BudgetDecision(
                action=BudgetAction.WARN,
                reason="預算使用率超過預警閾值",
                original_tier=tier,
                budget_usage=budget_usage,
            )

        # 正常允許
        return BudgetDecision(
            action=BudgetAction.ALLOW,
            original_tier=tier,
            budget_usage=budget_usage,
        )

    async def post_call(self, actual_cost: float, role: str | None = None) -> None:
        """LLM 呼叫後更新計量。

        Args:
            actual_cost: 實際花費（美元）
            role: 角色 ID（None 則使用目前上下文角色）
        """
        self._maybe_roll_month()
        self._task_spent += actual_cost
        self._session_spent += actual_cost
        self._monthly_spent += actual_cost
        effective_role = role or self._role
        if effective_role:
            self._role_spent[effective_role] = (
                self._role_spent.get(effective_role, 0.0) + actual_cost
            )

    # --- 狀態查詢 ---

    @property
    def task_spent(self) -> float:
        """目前任務已花費。"""
        return self._task_spent

    @property
    def session_spent(self) -> float:
        """目前會話已花費。"""
        return self._session_spent

    @property
    def monthly_spent(self) -> float:
        """本月已花費（內部計量）。"""
        return self._monthly_spent

    @property
    def role_spent(self) -> dict[str, float]:
        """各角色已花費。"""
        return dict(self._role_spent)

    @property
    def has_limits(self) -> bool:
        """是否配置了任何預算限制。"""
        return self._has_any_limit()

    def get_status(self) -> dict[str, Any]:
        """取得目前預算狀態（各層級花費與上限比較）。"""

        def _pct(spent: float, limit: float) -> float:
            if limit <= 0:
                return 0.0
            return min(100.0, spent / limit * 100)

        return {
            "task_spent": self._task_spent,
            "task_limit": self.config.task_limit_usd,
            "session_spent": self._session_spent,
            "session_limit": self.config.session_limit_usd,
            "monthly_spent": self._monthly_spent,
            "monthly_limit": self.config.monthly_limit_usd,
            "role_spent": dict(self._role_spent),
            "role_limits": dict(self.config.role_limits_usd),
            "warn_threshold": self.config.warn_threshold,
            "degrade_threshold": self.config.degrade_threshold,
            "hard_stop": self.config.hard_stop,
            "levels": {
                "task": {
                    "spent": self._task_spent,
                    "limit": self.config.task_limit_usd,
                    "usage_pct": _pct(self._task_spent, self.config.task_limit_usd),
                },
                "session": {
                    "spent": self._session_spent,
                    "limit": self.config.session_limit_usd,
                    "usage_pct": _pct(self._session_spent, self.config.session_limit_usd),
                },
                "monthly": {
                    "spent": self._monthly_spent,
                    "limit": self.config.monthly_limit_usd,
                    "usage_pct": _pct(self._monthly_spent, self.config.monthly_limit_usd),
                },
            },
        }

    # --- 內部方法 ---

    def _has_any_limit(self) -> bool:
        """檢查是否配置了任何預算限制。"""
        return self.config.has_any_limit()

    def _get_degraded_model(self, tier: str) -> str | None:
        """取得降級後的模型。"""
        if self.llm_config:
            return self.llm_config.get_model_for_tier(tier, degraded=True)
        return None

    async def _fire_alert(
        self,
        level: str,
        stage: str,
        severity: str,
        spent: float,
        limit: float,
    ) -> None:
        """發布預算告警事件（按 level+stage 去重）。"""
        key = (level, stage)
        if key in self._alerts_fired:
            return
        self._alerts_fired.add(key)

        usage_pct = (spent / limit * 100) if limit > 0 else 0.0
        logger.warning(
            "BudgetGuard: 預算告警 [{}] level={} stage={} usage={:.1f}% (${:.4f}/${:.2f})",
            severity,
            level,
            stage,
            usage_pct,
            spent,
            limit,
        )

        if self.event_bus:
            await self.event_bus.publish(OPCEvent(
                event_type="budget.alert",
                payload={
                    "severity": severity,
                    "level": level,
                    "stage": stage,
                    "usage_pct": usage_pct,
                    "spent": spent,
                    "limit": limit,
                    "warn_threshold": self.config.warn_threshold,
                    "degrade_threshold": self.config.degrade_threshold,
                    "role": self._role,
                    "task_id": self._task_id,
                },
            ))

    async def _fire_warn_alerts(self, levels: tuple[tuple[str, float], ...]) -> None:
        """對達到預警閾值的層級發布 warning 告警。"""
        for level, after in levels:
            if self.config.should_warn(level, after):
                await self._fire_alert(
                    level, "warn", "warning", after, self.config.get_effective_limit(level)
                )

    async def _fire_degrade_alerts(self, levels: tuple[tuple[str, float], ...]) -> None:
        """對達到降級閾值的層級發布 critical 告警。"""
        for level, after in levels:
            if self.config.should_degrade(level, after):
                await self._fire_alert(
                    level, "degrade", "critical", after, self.config.get_effective_limit(level)
                )


class BudgetExhaustedError(Exception):
    """預算耗盡異常。"""

    def __init__(self, message: str, decision: BudgetDecision | None = None):
        super().__init__(message)
        self.decision = decision
