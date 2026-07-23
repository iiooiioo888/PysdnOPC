"""智能啟動模組 — 零配置啟動的入口點。

職責說明：
    整合 IntentParser、KeyDiscovery、ModelRouter、BudgetGuard，
    提供一站式智能啟動體驗。

    用戶只需說一句話，系統自動：
    1. 發現可用的 API Key
    2. 解析任務意圖
    3. 選擇最佳組織模板
    4. 配置模型路由和預算
    5. 啟動執行

使用範例：
    from opc.smart_start import SmartStarter
    starter = SmartStarter()
    config = await starter.start("幫我做一份投資分析報告")
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from opc.llm.key_discovery import KeyDiscovery, ProviderInfo, format_discovery_result
from opc.llm.model_router import ModelRouter, RunEstimate, format_run_estimate
from opc.layer1_perception.intent_parser import IntentParser, TaskIntent, format_intent
from opc.layer6_observability.budget_guard import BudgetGuard


@dataclass
class SmartStartConfig:
    """智能啟動配置。"""
    intent: TaskIntent
    provider: ProviderInfo
    model_router: ModelRouter
    budget_guard: BudgetGuard
    org_template_path: str = ""
    org_template_data: dict[str, Any] = field(default_factory=dict)
    run_estimate: RunEstimate | None = None
    warnings: list[str] = field(default_factory=list)


class SmartStarter:
    """智能啟動器。

    整合所有零配置啟動組件，提供一站式啟動體驗。
    """

    def __init__(
        self,
        opc_home: Path | None = None,
        llm_provider: Any = None,
    ) -> None:
        self.opc_home = opc_home
        self.llm_provider = llm_provider
        self.key_discovery = KeyDiscovery(opc_home)
        self.intent_parser = IntentParser(llm_provider)

    async def start(
        self,
        user_input: str,
        *,
        budget: float = 0.0,
        quality_hint: str = "balanced",
        use_llm_for_intent: bool = False,
    ) -> SmartStartConfig:
        """智能啟動。

        參數：
            user_input: 用戶的自然語言描述
            budget: 預算上限（0=無限制）
            quality_hint: 品質偏好 (best/balanced/cheapest)
            use_llm_for_intent: 是否用 LLM 做意圖解析

        返回：
            SmartStartConfig — 完整的啟動配置
        """
        warnings = []

        # Step 1: 發現 API Key
        providers = self.key_discovery.discover()
        if not providers:
            raise ValueError(
                "未找到可用的 API Key。請設置環境變數（如 OPENAI_API_KEY）"
                "或運行 `opc config set llm.api_key` 配置。"
            )
        provider = providers[0]  # 使用第一個（已按推薦排序）
        logger.info(f"Using provider: {provider.provider} (from {provider.source})")

        # Step 2: 解析任務意圖
        intent = await self.intent_parser.parse(user_input, use_llm=use_llm_for_intent)
        logger.info(f"Intent parsed: domain={intent.domain}, type={intent.task_type}, confidence={intent.confidence:.2f}")

        if intent.confidence < 0.3:
            warnings.append(
                f"⚠️ 任務解析信心度較低 ({intent.confidence:.0%})，"
                f"可能需要手動調整配置"
            )

        # Step 3: 配置模型路由
        model_router = ModelRouter(
            default_model=provider.models[0] if provider.models else "gpt-4o-mini",
            routing={},
            budget_total=budget,
            quality_hint=quality_hint,
        )

        # Step 4: 配置預算守衛
        budget_guard = BudgetGuard(
            total_budget=budget,
            event_bus=None,  # 後續注入
        )

        # Step 5: 加載組織模板
        org_template_data = self._load_org_template(intent.org_template)

        # Step 6: 生成運行估算
        run_estimate = None
        if org_template_data and budget > 0:
            roles = self._extract_roles_for_estimate(org_template_data)
            run_estimate = model_router.estimate_run_cost(roles, budget_limit=budget)

        return SmartStartConfig(
            intent=intent,
            provider=provider,
            model_router=model_router,
            budget_guard=budget_guard,
            org_template_path=intent.org_template,
            org_template_data=org_template_data,
            run_estimate=run_estimate,
            warnings=warnings,
        )

    def format_startup_info(self, config: SmartStartConfig) -> str:
        """格式化啟動信息。"""
        lines = ["=" * 60]
        lines.append("🚀 OpenOPC 智能啟動")
        lines.append("=" * 60)

        # 意圖信息
        lines.append("")
        lines.append(format_intent(config.intent))

        # Provider 信息
        lines.append(f"🤖 AI Provider: {config.provider.provider}")
        lines.append(f"   模型: {', '.join(config.provider.models[:3])}")
        lines.append(f"   來源: {config.provider.source}")

        # 組織模板
        if config.org_template_data:
            org_name = config.org_template_data.get("name", "未命名")
            roles = config.org_template_data.get("roles", [])
            lines.append(f"\n🏗️ 組織: {org_name}")
            lines.append(f"   角色: {', '.join(r.get('name', r.get('id', '')) for r in roles)}")

        # 運行估算
        if config.run_estimate:
            lines.append("")
            lines.append(format_run_estimate(config.run_estimate))

        # 警告
        if config.warnings:
            lines.append("\n⚠️ 注意事項:")
            for w in config.warnings:
                lines.append(f"  {w}")

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)

    def _load_org_template(self, template_path: str) -> dict[str, Any]:
        """加載組織模板。"""
        if not template_path:
            return {}

        # 查找模板文件
        candidates = [
            Path(__file__).parent.parent / "config" / "org_templates" / f"{template_path}.yaml",
            Path.home() / ".opc" / "org_templates" / f"{template_path}.yaml",
        ]

        # 也查找項目級 .opc 目錄
        from opc.core.config import get_opc_home
        opc_home = get_opc_home()
        candidates.append(opc_home / "org_templates" / f"{template_path}.yaml")

        for path in candidates:
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    if isinstance(data, dict):
                        logger.info(f"Loaded org template from {path}")
                        return data
                except Exception as e:
                    logger.warning(f"Failed to load template {path}: {e}")

        logger.info(f"Org template '{template_path}' not found, using defaults")
        return {}

    def _extract_roles_for_estimate(
        self, template: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """從模板中提取角色信息用於成本估算。"""
        roles = []
        for role_def in template.get("roles", []):
            roles.append({
                "name": role_def.get("id", role_def.get("name", "unknown")),
                "task_description": role_def.get("description", ""),
                "estimated_complexity": "medium",
            })
        return roles


def format_smart_start_summary(config: SmartStartConfig) -> str:
    """格式化智能啟動摘要（簡短版）。"""
    lines = [
        f"🎯 任務: {config.intent.raw_input[:60]}",
        f"📂 領域: {config.intent.domain} | 類型: {config.intent.task_type}",
        f"🤖 Provider: {config.provider.provider}",
        f"👥 角色: {', '.join(config.intent.estimated_roles)}",
    ]

    if config.run_estimate:
        lines.append(f"💰 預估費用: ${config.run_estimate.total_estimated_cost:.2f}")

    return " | ".join(lines)
