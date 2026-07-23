"""預算守衛 — LLM 呼叫前的預算攔截器。

在每次 LLM 呼叫前檢查預算餘量，根據配置決定：
- allow: 正常允許呼叫
- warn: 允許但發出預警
- degrade: 降級到較便宜的模型
- block: 阻止呼叫（預算耗盡且 hard_stop=true）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from opc.core.config import BudgetConfig, LLMConfig
    from opc.layer6_observability.cost_tracker import CostTracker


class BudgetAction(str, Enum):
    """預算決策動作。"""

    ALLOW = "allow"  # 正常允許
    WARN = "warn"  # 允許但預警
    DEGRADE = "degrade"  # 降級模型
    BLOCK = "block"  # 阻止呼叫


@dataclass
class BudgetDecision:
    """預算決策結果。"""

    action: BudgetAction
    reason: str = ""
    original_tier: str = ""
    degraded_model: str | None = None
    budget_usage: dict[str, float] | None = None  # 各層級使用率

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


class BudgetGuard:
    """LLM 呼叫前的預算攔截器。

    使用方式：
        guard = BudgetGuard(budget_config, llm_config, cost_tracker)
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
    ) -> None:
        self.config = budget_config
        self.llm_config = llm_config
        self.tracker = cost_tracker
        self._task_spent = 0.0
        self._session_spent = 0.0

    def reset_task(self) -> None:
        """重置任務級別計量（新任務開始時呼叫）。"""
        self._task_spent = 0.0

    def reset_session(self) -> None:
        """重置會話級別計量（新會話開始時呼叫）。"""
        self._session_spent = 0.0
        self._task_spent = 0.0

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

        # 取得月度花費（從 tracker）
        monthly_spent = 0.0
        if self.tracker:
            try:
                summary = await self.tracker.get_summary()
                monthly_spent = summary.get("total_cost", 0.0)
            except Exception:
                pass
        monthly_after = monthly_spent + estimated_cost

        budget_usage = {
            "task": task_after,
            "session": session_after,
            "monthly": monthly_after,
        }

        # 檢查是否超過預算（硬停止）
        if self.config.hard_stop:
            if self.config.is_exceeded("task", task_after):
                return BudgetDecision(
                    action=BudgetAction.BLOCK,
                    reason=f"任務預算已耗盡（${task_after:.4f} >= ${self.config.task_limit_usd}）",
                    original_tier=tier,
                    budget_usage=budget_usage,
                )
            if self.config.is_exceeded("session", session_after):
                return BudgetDecision(
                    action=BudgetAction.BLOCK,
                    reason=f"會話預算已耗盡（${session_after:.4f} >= ${self.config.session_limit_usd}）",
                    original_tier=tier,
                    budget_usage=budget_usage,
                )
            if self.config.is_exceeded("monthly", monthly_after):
                return BudgetDecision(
                    action=BudgetAction.BLOCK,
                    reason=f"月度預算已耗盡（${monthly_after:.4f} >= ${self.config.monthly_limit_usd}）",
                    original_tier=tier,
                    budget_usage=budget_usage,
                )

        # 檢查是否需要降級
        should_degrade = (
            self.config.should_degrade("task", task_after)
            or self.config.should_degrade("session", session_after)
            or self.config.should_degrade("monthly", monthly_after)
        )

        if should_degrade:
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
                    reason=f"預算接近上限，降級到較便宜的模型",
                    original_tier=tier,
                    degraded_model=degraded_model,
                    budget_usage=budget_usage,
                )

        # 檢查是否需要預警
        should_warn = (
            self.config.should_warn("task", task_after)
            or self.config.should_warn("session", session_after)
            or self.config.should_warn("monthly", monthly_after)
        )

        if should_warn:
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

    async def post_call(self, actual_cost: float) -> None:
        """LLM 呼叫後更新計量。

        Args:
            actual_cost: 實際花費（美元）
        """
        self._task_spent += actual_cost
        self._session_spent += actual_cost

    def _has_any_limit(self) -> bool:
        """檢查是否配置了任何預算限制。"""
        return (
            self.config.task_limit_usd > 0
            or self.config.session_limit_usd > 0
            or self.config.monthly_limit_usd > 0
        )

    def _get_degraded_model(self, tier: str) -> str | None:
        """取得降級後的模型。"""
        if self.llm_config:
            return self.llm_config.get_model_for_tier(tier, degraded=True)
        return None

    @property
    def task_spent(self) -> float:
        """目前任務已花費。"""
        return self._task_spent

    @property
    def session_spent(self) -> float:
        """目前會話已花費。"""
        return self._session_spent

    def get_status(self) -> dict[str, float]:
        """取得目前預算狀態。"""
        return {
            "task_spent": self._task_spent,
            "task_limit": self.config.task_limit_usd,
            "session_spent": self._session_spent,
            "session_limit": self.config.session_limit_usd,
        }


class BudgetExhaustedError(Exception):
    """預算耗盡異常。"""

    def __init__(self, message: str, decision: BudgetDecision | None = None):
        super().__init__(message)
        self.decision = decision
