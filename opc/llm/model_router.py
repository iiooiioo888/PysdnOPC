"""預算感知模型路由模組。

職責說明：
    根據任務類型、預算狀態、用戶偏好，自動選擇最優模型。
    支援三級模型策略（HEAVY/MEDIUM/LIGHT）、預算降級、
    事前成本估算。

核心概念：
    - ModelTier: 模型能力等級（heavy/medium/light）
    - TaskModelMap: 任務類型 → 模型等級映射
    - BudgetGuard: 預算守衛，超預算時自動降級

使用範例：
    from opc.llm.model_router import ModelRouter, ModelTier
    router = ModelRouter(budget_total=3.0)
    model = router.route(task_type="planning")  # → "gpt-4o"
    model = router.route(task_type="format")    # → "gpt-4o-mini"
    model = router.route(task_type="planning", budget_remaining=0.1)  # → "gpt-4o-mini" (降級)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger


class ModelTier(Enum):
    """模型能力等級。"""
    HEAVY = "heavy"      # GPT-4o, Claude Opus — 推理、決策、評審
    MEDIUM = "medium"    # GPT-4o-mini, Claude Sonnet — 主要執行
    LIGHT = "light"      # Haiku, DeepSeek — 格式化、搜索、搬運


# 等級降級鏈
_DOWNGRADE_CHAIN = {
    ModelTier.HEAVY: ModelTier.MEDIUM,
    ModelTier.MEDIUM: ModelTier.LIGHT,
    ModelTier.LIGHT: ModelTier.LIGHT,  # 已是最低，不降
}

_UPGRADE_CHAIN = {
    ModelTier.LIGHT: ModelTier.MEDIUM,
    ModelTier.MEDIUM: ModelTier.HEAVY,
    ModelTier.HEAVY: ModelTier.HEAVY,  # 已是最高，不升
}

# 任務類型 → 模型等級映射
TASK_MODEL_MAP: dict[str, ModelTier] = {
    # --- 高推理需求（HEAVY）---
    "planning":           ModelTier.HEAVY,
    "architecture":       ModelTier.HEAVY,
    "review":             ModelTier.HEAVY,
    "decision":           ModelTier.HEAVY,
    "complex_analysis":   ModelTier.HEAVY,
    "code_review":        ModelTier.HEAVY,
    "strategy":           ModelTier.HEAVY,

    # --- 中等需求（MEDIUM）---
    "code_write":         ModelTier.MEDIUM,
    "research":           ModelTier.MEDIUM,
    "analysis":           ModelTier.MEDIUM,
    "writing":            ModelTier.MEDIUM,
    "report":             ModelTier.MEDIUM,
    "data_analysis":      ModelTier.MEDIUM,
    "document_write":     ModelTier.MEDIUM,
    "creative":           ModelTier.MEDIUM,
    "translate":          ModelTier.MEDIUM,

    # --- 低推理需求（LIGHT）---
    "format":             ModelTier.LIGHT,
    "search":             ModelTier.LIGHT,
    "summarize":          ModelTier.LIGHT,
    "extract":            ModelTier.LIGHT,
    "classify":           ModelTier.LIGHT,
    "convert":            ModelTier.LIGHT,
    "template_fill":      ModelTier.LIGHT,
    "list_items":         ModelTier.LIGHT,
    "simple_qa":          ModelTier.LIGHT,
}


@dataclass
class ModelConfig:
    """路由結果：選定的模型配置。"""
    model: str                         # litellm 格式的模型名稱
    tier: ModelTier                    # 模型等級
    task_type: str                     # 原始任務類型
    downgraded: bool = False           # 是否因預算降級
    upgrade_reason: str = ""           # 升級原因（如有）
    estimated_cost_per_1k_tokens: float = 0.0  # 每 1k tokens 預估成本


@dataclass
class CostEstimate:
    """任務成本估算結果。"""
    role: str
    task_description: str
    model: str
    tier: ModelTier
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost: float
    confidence: str = "low"  # low, medium, high


@dataclass
class RunEstimate:
    """整個運行的成本估算。"""
    role_estimates: list[CostEstimate] = field(default_factory=list)
    total_estimated_cost: float = 0.0
    budget_limit: float = 0.0
    budget_sufficient: bool = True
    recommendations: list[str] = field(default_factory=list)


# 每 1k tokens 的預估成本（USD），基於公開定價
_MODEL_COST_PER_1K: dict[str, tuple[float, float]] = {
    # model: (input_cost, output_cost)
    # OpenAI
    "gpt-4o":                     (0.0025, 0.010),
    "gpt-4o-mini":                (0.00015, 0.0006),
    "gpt-4.1":                    (0.002, 0.008),
    "gpt-4.1-mini":               (0.0004, 0.0016),
    "gpt-4.1-nano":               (0.0001, 0.0004),
    "o3":                         (0.010, 0.040),
    "o4-mini":                    (0.0011, 0.0044),
    # Anthropic
    "claude-sonnet-4-20250514":   (0.003, 0.015),
    "claude-3-5-haiku-20241022":  (0.001, 0.005),
    "claude-opus-4-20250514":     (0.015, 0.075),
    # DeepSeek
    "deepseek-chat":              (0.00014, 0.00028),
    "deepseek-reasoner":          (0.00055, 0.00219),
    # Google
    "gemini-2.0-flash":           (0.0001, 0.0004),
    "gemini-2.5-pro":             (0.00125, 0.010),
    # Groq
    "llama-3.3-70b-versatile":    (0.00059, 0.00079),
    # Mistral
    "mistral-large-latest":       (0.002, 0.006),
    "mistral-small-latest":       (0.001, 0.003),
}

# 等級預設模型（用戶未配置 routing 時使用）
_TIER_DEFAULT_MODELS: dict[ModelTier, str] = {
    ModelTier.HEAVY:  "anthropic/claude-sonnet-4-20250514",
    ModelTier.MEDIUM: "openai/gpt-4o-mini",
    ModelTier.LIGHT:  "deepseek/deepseek-chat",
}

# 角色 → 任務類型映射（用於自動推斷）
ROLE_TASK_TYPE_MAP: dict[str, str] = {
    "manager":        "planning",
    "pm":             "planning",
    "lead":           "planning",
    "architect":      "architecture",
    "reviewer":       "review",
    "analyst":        "analysis",
    "researcher":     "research",
    "writer":         "writing",
    "developer":      "code_write",
    "coder":          "code_write",
    "engineer":       "code_write",
    "designer":       "creative",
    "translator":     "translate",
    "formatter":      "format",
    "searcher":       "search",
    "data_analyst":   "data_analysis",
}


class ModelRouter:
    """預算感知模型路由器。

    功能：
    1. 根據任務類型自動選擇模型等級
    2. 預算不足時自動降級
    3. 提供成本估算
    4. 支援用戶品質偏好（best/balanced/cheapest）
    """

    def __init__(
        self,
        default_model: str = "",
        routing: dict[str, str] | None = None,
        budget_total: float = 0.0,
        quality_hint: str = "balanced",
    ) -> None:
        self.default_model = default_model or "anthropic/claude-sonnet-4-20250514"
        self.routing = routing or {}
        self.budget_total = budget_total
        self.budget_spent = 0.0
        self.quality_hint = quality_hint  # best, balanced, cheapest

        # 可選：按等級覆蓋模型
        self._tier_overrides: dict[ModelTier, str] = {}

    def set_tier_model(self, tier: ModelTier, model: str) -> None:
        """設置指定等級使用的模型。"""
        self._tier_overrides[tier] = model

    def record_cost(self, cost: float) -> None:
        """記錄已花費成本。"""
        self.budget_spent += cost

    @property
    def budget_remaining(self) -> float:
        """剩餘預算。無預算限制時返回 inf。"""
        if self.budget_total <= 0:
            return float("inf")
        return max(0.0, self.budget_total - self.budget_spent)

    def route(
        self,
        task_type: str | None = None,
        role: str | None = None,
        budget_override: float | None = None,
        quality_hint: str | None = None,
    ) -> ModelConfig:
        """路由到最優模型。

        參數：
            task_type: 任務類型（如 planning, code_write, format）
            role: 角色名稱（用於推斷 task_type）
            budget_override: 臨時預算覆蓋
            quality_hint: 臨時品質偏好覆蓋

        返回：
            ModelConfig — 選定的模型配置
        """
        # 推斷任務類型
        effective_task_type = task_type or self._infer_task_type(role) or "analysis"
        effective_quality = quality_hint or self.quality_hint

        # 確定目標等級
        target_tier = TASK_MODEL_MAP.get(effective_task_type, ModelTier.MEDIUM)

        # 用戶品質偏好調整
        if effective_quality == "best":
            target_tier = _UPGRADE_CHAIN.get(target_tier, target_tier)
        elif effective_quality == "cheapest":
            target_tier = _DOWNGRADE_CHAIN.get(target_tier, target_tier)

        # 預算檢查與降級
        budget = budget_override if budget_override is not None else self.budget_remaining
        downgraded = False
        original_tier = target_tier

        if budget < float("inf") and budget > 0:
            target_tier, downgraded = self._budget_downgrade(target_tier, budget)

        # 選擇具體模型
        model = self._resolve_model(target_tier, effective_task_type)

        # 計算預估成本
        cost_per_1k = self._estimate_cost_per_1k(model)

        if downgraded:
            logger.info(
                f"Budget downgrade: {original_tier.value} → {target_tier.value} "
                f"for task '{effective_task_type}' (budget remaining: ${budget:.2f})"
            )

        return ModelConfig(
            model=model,
            tier=target_tier,
            task_type=effective_task_type,
            downgraded=downgraded,
            estimated_cost_per_1k_tokens=cost_per_1k,
        )

    def estimate_run_cost(
        self,
        roles: list[dict[str, Any]],
        budget_limit: float = 0.0,
    ) -> RunEstimate:
        """估算整個運行的成本。

        參數：
            roles: 角色列表，每個包含 name, task_description, estimated_complexity
            budget_limit: 預算上限

        返回：
            RunEstimate — 包含每個角色的成本估算和總計
        """
        estimates = []
        total = 0.0

        for role_info in roles:
            role_name = role_info.get("name", "unknown")
            task_desc = role_info.get("task_description", "")
            complexity = role_info.get("estimated_complexity", "medium")

            # 路由到模型
            config = self.route(role=role_name)

            # 估算 tokens
            input_tokens, output_tokens = self._estimate_tokens(task_desc, complexity)

            # 計算成本
            cost = self._compute_cost(config.model, input_tokens, output_tokens)

            estimates.append(CostEstimate(
                role=role_name,
                task_description=task_desc[:80],
                model=config.model,
                tier=config.tier,
                estimated_input_tokens=input_tokens,
                estimated_output_tokens=output_tokens,
                estimated_cost=cost,
                confidence="medium" if complexity != "unknown" else "low",
            ))
            total += cost

        # 生成建議
        recommendations = []
        if budget_limit > 0 and total > budget_limit:
            recommendations.append(
                f"⚠️ 預估費用 ${total:.2f} 超出預算 ${budget_limit:.2f}，"
                f"部分角色將使用更經濟的模型"
            )
        if budget_limit > 0 and total < budget_limit * 0.5:
            recommendations.append(
                f"💡 預估費用 ${total:.2f} 遠低於預算 ${budget_limit:.2f}，"
                f"可考慮升級模型以獲得更好效果"
            )

        return RunEstimate(
            role_estimates=estimates,
            total_estimated_cost=total,
            budget_limit=budget_limit,
            budget_sufficient=(budget_limit <= 0 or total <= budget_limit),
            recommendations=recommendations,
        )

    # --- 內部方法 ---

    def _infer_task_type(self, role: str | None) -> str | None:
        """從角色名稱推斷任務類型。"""
        if not role:
            return None
        role_lower = role.lower().strip()
        # 直接匹配
        if role_lower in ROLE_TASK_TYPE_MAP:
            return ROLE_TASK_TYPE_MAP[role_lower]
        # 模糊匹配
        for keyword, task_type in ROLE_TASK_TYPE_MAP.items():
            if keyword in role_lower:
                return task_type
        return None

    def _budget_downgrade(self, tier: ModelTier, budget: float) -> tuple[ModelTier, bool]:
        """根據預算降級模型。返回 (最終等級, 是否降級)。"""
        # 估算每個等級的成本（假設平均 5k tokens/調用）
        tier_costs = {
            ModelTier.HEAVY:  self._estimate_tier_cost(ModelTier.HEAVY, 5000),
            ModelTier.MEDIUM: self._estimate_tier_cost(ModelTier.MEDIUM, 5000),
            ModelTier.LIGHT:  self._estimate_tier_cost(ModelTier.LIGHT, 5000),
        }

        current = tier
        downgraded = False

        # 如果當前等級的單次調用成本超過剩餘預算的 20%，降級
        while current != ModelTier.LIGHT:
            single_cost = tier_costs.get(current, 0.01)
            if single_cost < budget * 0.2:
                break
            current = _DOWNGRADE_CHAIN[current]
            downgraded = True

        return current, downgraded

    def _resolve_model(self, tier: ModelTier, task_type: str) -> str:
        """解析具體模型名稱。"""
        # 1. 檢查用戶 routing 配置
        if task_type in self.routing:
            return self.routing[task_type]

        # 2. 檢查等級覆蓋
        if tier in self._tier_overrides:
            return self._tier_overrides[tier]

        # 3. 使用預設
        return _TIER_DEFAULT_MODELS.get(tier, self.default_model)

    def _estimate_cost_per_1k(self, model: str) -> float:
        """估算每 1k tokens 的平均成本。"""
        model_normalized = model.split("/")[-1].lower() if "/" in model else model.lower()
        for known_model, (input_cost, output_cost) in _MODEL_COST_PER_1K.items():
            if known_model in model_normalized:
                return (input_cost + output_cost) / 2
        return 0.002  # 默認估計

    def _estimate_tier_cost(self, tier: ModelTier, tokens: int) -> float:
        """估算指定等級處理指定 tokens 的成本。"""
        model = _TIER_DEFAULT_MODELS.get(tier, self.default_model)
        per_1k = self._estimate_cost_per_1k(model)
        return per_1k * (tokens / 1000)

    def _estimate_tokens(self, task_desc: str, complexity: str) -> tuple[int, int]:
        """估算輸入/輸出 tokens。"""
        # 基於複雜度的粗略估算
        complexity_multipliers = {
            "low":    (2000, 1000),
            "medium": (4000, 3000),
            "high":   (8000, 6000),
        }
        return complexity_multipliers.get(complexity, (4000, 3000))

    def _compute_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """計算實際成本。"""
        model_normalized = model.split("/")[-1].lower() if "/" in model else model.lower()
        input_rate = 0.002
        output_rate = 0.006

        for known_model, (i_cost, o_cost) in _MODEL_COST_PER_1K.items():
            if known_model in model_normalized:
                input_rate = i_cost
                output_rate = o_cost
                break

        return (input_rate * input_tokens / 1000) + (output_rate * output_tokens / 1000)


def format_run_estimate(estimate: RunEstimate) -> str:
    """格式化運行估算為人類可讀文本。"""
    lines = ["📊 任務預估\n"]

    for e in estimate.role_estimates:
        tier_emoji = {"heavy": "🔴", "medium": "🟡", "light": "🟢"}
        emoji = tier_emoji.get(e.tier.value, "⚪")
        lines.append(
            f"  {emoji} {e.role:<16} {e.model:<35} ~${e.estimated_cost:.2f}"
        )

    lines.append(f"\n  預估總費用：${estimate.total_estimated_cost:.2f}")

    if estimate.budget_limit > 0:
        pct = (estimate.total_estimated_cost / estimate.budget_limit * 100) if estimate.budget_limit else 0
        lines.append(f"  預算上限：${estimate.budget_limit:.2f} ({pct:.0f}%)")

    if estimate.recommendations:
        lines.append("")
        for rec in estimate.recommendations:
            lines.append(f"  {rec}")

    return "\n".join(lines)
