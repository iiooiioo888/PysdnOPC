"""引擎增強適配器 — 將智能模組接入 OPCEngine。

職責說明：
    作為 OPCEngine 和新增智能模組之間的橋樑，負責：
    - 將 ModelRouter 接入 LLM 選擇流程
    - 將 BudgetGuard 接入 CostTracker
    - 將 InsightEngine 接入 EventBus
    - 將 EnhancedEventBus 替換原始 EventBus
    - 管理 WebSocket 實時廣播

使用方式：
    enhancer = EngineEnhancer(engine)
    await enhancer.enhance()  # 注入所有增強功能
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.core.events import EventBus
from opc.core.events_enhanced import EnhancedEventBus
from opc.core.models import OPCEvent
from opc.layer6_observability.budget_guard import BudgetGuard, BudgetDecision, format_budget_status
from opc.layer6_observability.insight_engine import InsightEngine, ExecutionEvent, format_run_analysis
from opc.llm.model_router import ModelRouter, ModelTier
from opc.engine.auto_loop import AutoLoopManager, LoopConfig, format_loop_stats


class EngineEnhancer:
    """引擎增強器 — 將智能模組注入 OPCEngine。

    增強內容：
    1. EventBus → EnhancedEventBus（WebSocket 廣播）
    2. LLMProvider + ModelRouter（智能模型選擇）
    3. CostTracker + BudgetGuard（預算控制）
    4. InsightEngine（自動洞察）
    """

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._enhanced_bus: EnhancedEventBus | None = None
        self._model_router: ModelRouter | None = None
        self._budget_guard: BudgetGuard | None = None
        self._insight_engine: InsightEngine | None = None
        self._auto_loop: AutoLoopManager | None = None
        self._ws_clients: list[Any] = []
        self._execution_events: list[ExecutionEvent] = []
        self._current_run_id: str = ""

    @property
    def model_router(self) -> ModelRouter | None:
        return self._model_router

    @property
    def budget_guard(self) -> BudgetGuard | None:
        return self._budget_guard

    @property
    def insight_engine(self) -> InsightEngine | None:
        return self._insight_engine

    @property
    def auto_loop(self) -> AutoLoopManager | None:
        return self._auto_loop

    async def enhance(
        self,
        budget: float = 0.0,
        quality_hint: str = "balanced",
        enable_insights: bool = True,
    ) -> None:
        """注入所有增強功能到引擎。

        參數：
            budget: 預算上限（0=無限制）
            quality_hint: 品質偏好
            enable_insights: 是否啟用洞察引擎
        """
        logger.info("Enhancing engine with smart modules...")

        # 1. 升級 EventBus
        await self._upgrade_event_bus()

        # 2. 初始化 ModelRouter
        self._init_model_router(budget, quality_hint)

        # 3. 初始化 BudgetGuard
        self._init_budget_guard(budget)

        # 4. 初始化 InsightEngine
        if enable_insights:
            self._init_insight_engine()

        # 5. 初始化 AutoLoopManager
        self._init_auto_loop()

        # 6. 註冊事件監聽器
        self._register_listeners()

        # 6. 包裝 LLM 語用
        self._wrap_llm_usage()

        logger.info("Engine enhancement complete")

    async def _upgrade_event_bus(self) -> None:
        """將 EventBus 升級為 EnhancedEventBus。"""
        old_bus = self.engine.event_bus

        # 創建增強型事件匯流排
        enhanced_bus = EnhancedEventBus()

        # 遷移原有訂閱者
        enhanced_bus._listeners = old_bus._listeners
        enhanced_bus._global_listeners = old_bus._global_listeners
        enhanced_bus._history = old_bus._history

        # 替換引擎的事件匯流排
        self.engine.event_bus = enhanced_bus
        self._enhanced_bus = enhanced_bus

        logger.info("EventBus upgraded to EnhancedEventBus")

    def _init_model_router(self, budget: float, quality_hint: str) -> None:
        """初始化模型路由器。"""
        llm_config = self.engine.config.llm

        self._model_router = ModelRouter(
            default_model=llm_config.default_model,
            routing=dict(llm_config.routing) if hasattr(llm_config, 'routing') else {},
            budget_total=budget,
            quality_hint=quality_hint,
        )

        logger.info(f"ModelRouter initialized: default={llm_config.default_model}, budget=${budget:.2f}")

    def _init_budget_guard(self, budget: float) -> None:
        """初始化預算守衛。"""
        self._budget_guard = BudgetGuard(
            total_budget=budget,
            event_bus=self.engine.event_bus,
        )

        # 包裝 cost_tracker 的 record 方法
        if hasattr(self.engine, 'cost_tracker') and self.engine.cost_tracker:
            original_record = self.engine.cost_tracker.record

            async def enhanced_record(entry):
                # 先檢查預算
                if self._budget_guard and self._budget_guard.total_budget > 0:
                    check = await self._budget_guard.check_before_call(
                        role=entry.agent_id or "unknown",
                        model=entry.model,
                        estimated_tokens=entry.tokens_in + entry.tokens_out,
                    )
                    if check.decision == BudgetDecision.BLOCK:
                        logger.warning(f"Budget blocked: {check.reason}")
                        return

                # 執行原始記錄
                await original_record(entry)

                # 記錄到預算守衛
                if self._budget_guard:
                    await self._budget_guard.record_usage(
                        role=entry.agent_id or "unknown",
                        cost=entry.cost,
                        model=entry.model,
                        prompt_tokens=entry.tokens_in,
                        completion_tokens=entry.tokens_out,
                    )

            self.engine.cost_tracker.record = enhanced_record

        logger.info(f"BudgetGuard initialized: budget=${budget:.2f}")

    def _init_insight_engine(self) -> None:
        """初始化洞察引擎。"""
        self._insight_engine = InsightEngine(event_bus=self.engine.event_bus)
        logger.info("InsightEngine initialized")

    def _init_auto_loop(self) -> None:
        """初始化自動循環管理器。"""
        self._auto_loop = AutoLoopManager(
            engine=self.engine,
            event_bus=self.engine.event_bus,
        )
        logger.info("AutoLoopManager initialized")

    def _register_listeners(self) -> None:
        """註冊事件監聽器。"""
        bus = self.engine.event_bus

        # 監聽任務完成事件，收集執行數據
        async def on_task_completed(event: OPCEvent):
            data = event.payload if isinstance(event.payload, dict) else {}
            self._execution_events.append(ExecutionEvent(
                event_type="task_completed",
                role=data.get("role", data.get("agent_id", "")),
                task_item=data.get("task_id", data.get("work_item_id", "")),
                duration=data.get("duration", 0),
                cost=data.get("cost", 0),
                tokens=data.get("tokens", 0),
                model=data.get("model", ""),
            ))

        async def on_task_reworked(event: OPCEvent):
            data = event.payload if isinstance(event.payload, dict) else {}
            self._execution_events.append(ExecutionEvent(
                event_type="task_reworked",
                role=data.get("role", data.get("agent_id", "")),
                task_item=data.get("task_id", ""),
            ))

        async def on_cost_update(event: OPCEvent):
            data = event.payload if isinstance(event.payload, dict) else {}
            self._execution_events.append(ExecutionEvent(
                event_type="cost_update",
                role=data.get("agent_id", ""),
                cost=data.get("cost", 0),
                tokens=data.get("tokens", 0),
                model=data.get("model", ""),
            ))

        # 公司運行事件
        async def on_company_started(event: OPCEvent):
            self._current_run_id = event.payload.get("run_id", "") if isinstance(event.payload, dict) else ""
            self._execution_events.clear()
            logger.info(f"Company run started: {self._current_run_id}")

        async def on_company_completed(event: OPCEvent):
            if self._insight_engine and self._execution_events:
                analysis = self._insight_engine.analyze_run(
                    self._execution_events,
                    run_id=self._current_run_id,
                )
                logger.info(f"Run analysis complete: score={analysis.score:.0f}")
                # 發佈分析結果
                await bus.publish(OPCEvent(
                    event_type="insight.run_analysis",
                    payload={
                        "run_id": self._current_run_id,
                        "score": analysis.score,
                        "total_cost": analysis.total_cost,
                        "insights_count": len(analysis.insights),
                    },
                ))

        bus.subscribe("task.completed", on_task_completed)
        bus.subscribe("task.reworked", on_task_reworked)
        bus.subscribe("cost.update", on_cost_update)
        bus.subscribe("company.started", on_company_started)
        bus.subscribe("company.completed", on_company_completed)

        logger.info("Event listeners registered")

    def _wrap_llm_usage(self) -> None:
        """包裝 LLM 使用流程，注入模型路由。"""
        if not self._model_router:
            return

        llm = self.engine.llm
        if not llm:
            return

        original_select = llm._select_model

        def enhanced_select(task_type=None):
            """增強的模型選擇：使用 ModelRouter 智能路由。"""
            if self._model_router:
                config = self._model_router.route(task_type=task_type)
                return config.model
            return original_select(task_type)

        llm._select_model = enhanced_select
        logger.info("LLM model selection enhanced with ModelRouter")

    def add_ws_client(self, ws: Any) -> None:
        """添加 WebSocket 客戶端。"""
        self._ws_clients.append(ws)
        if self._enhanced_bus:
            async def ws_broadcaster(payload: dict):
                dead = []
                for client in self._ws_clients:
                    try:
                        await client.send(json.dumps(payload, default=str))
                    except Exception:
                        dead.append(client)
                for client in dead:
                    self._ws_clients.remove(client)

            self._enhanced_bus.add_ws_broadcaster(ws_broadcaster)
        logger.info(f"WebSocket client added (total: {len(self._ws_clients)})")

    def remove_ws_client(self, ws: Any) -> None:
        """移除 WebSocket 客戶端。"""
        if ws in self._ws_clients:
            self._ws_clients.remove(ws)

    def get_dashboard_data(self) -> dict[str, Any]:
        """獲取儀表盤數據。"""
        data: dict[str, Any] = {
            "timestamp": time.time(),
        }

        # 預算狀態
        if self._budget_guard:
            status = self._budget_guard.get_status()
            data["budget"] = {
                "total": status.total_budget,
                "spent": status.total_spent,
                "remaining": status.remaining,
                "pct": status.usage_pct,
                "role_breakdown": status.role_spent,
            }

        # 事件統計
        if self._enhanced_bus:
            stats = self._enhanced_bus.get_stats()
            data["events"] = {
                "total": stats.total_published,
                "by_category": dict(stats.by_category),
                "ws_broadcasts": stats.ws_broadcasts,
            }

        # 最近事件
        if self._enhanced_bus:
            data["recent_events"] = self._enhanced_bus.get_recent_events(limit=20)

        # 洞察
        if self._insight_engine and self._insight_engine._run_history:
            latest = self._insight_engine._run_history[-1]
            data["insights"] = {
                "score": latest.score,
                "insight_count": len(latest.insights),
                "insights": [
                    {
                        "type": i.type.value,
                        "severity": i.severity.value,
                        "message": i.message,
                        "suggestion": i.suggestion,
                    }
                    for i in latest.insights[:10]
                ],
            }

        # 模型路由
        if self._model_router:
            data["model_router"] = {
                "default_model": self._model_router.default_model,
                "quality_hint": self._model_router.quality_hint,
                "budget_spent": self._model_router.budget_spent,
            }

        # 自動循環
        if self._auto_loop:
            data["auto_loop"] = self._auto_loop.get_stats()
            data["active_loops"] = self._auto_loop.get_active_loops()

        return data

    def get_status_summary(self) -> str:
        """獲取增強狀態摘要。"""
        lines = ["🔧 引擎增強狀態\n"]

        lines.append(f"  📡 EnhancedEventBus: {'✅' if self._enhanced_bus else '❌'}")
        lines.append(f"  🧠 ModelRouter: {'✅' if self._model_router else '❌'}")
        lines.append(f"  🛡️ BudgetGuard: {'✅' if self._budget_guard else '❌'}")
        lines.append(f"  📊 InsightEngine: {'✅' if self._insight_engine else '❌'}")
        lines.append(f"  🔄 AutoLoopManager: {'✅' if self._auto_loop else '❌'}")
        lines.append(f"  📺 WebSocket 客戶端: {len(self._ws_clients)}")

        if self._budget_guard and self._budget_guard.total_budget > 0:
            lines.append(f"\n{format_budget_status(self._budget_guard.get_status())}")

        if self._model_router:
            lines.append(f"\n  🧠 預設模型: {self._model_router.default_model}")
            lines.append(f"  🎯 品質偏好: {self._model_router.quality_hint}")

        return "\n".join(lines)
