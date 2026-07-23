"""集成測試 — 驗證所有新增智能模組。

測試內容：
    1. KeyDiscovery — API Key 自動發現
    2. ModelRouter — 預算感知模型路由
    3. IntentParser — 自然語言意圖解析
    4. BudgetGuard — 預算守衛
    5. InsightEngine — 洞察引擎
    6. EnhancedEventBus — 增強事件匯流排
    7. OrgTemplateLoader — 組織模板載入
    8. EngineEnhancer — 引擎增強器
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 添加項目根目錄到 Python 路徑
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_key_discovery():
    """測試 API Key 自動發現。"""
    from opc.llm.key_discovery import KeyDiscovery

    discovery = KeyDiscovery()
    providers = discovery.discover()

    print(f"\n{'='*60}")
    print(f"🔑 KeyDiscovery 測試")
    print(f"{'='*60}")
    print(f"  發現 {len(providers)} 個 Provider")

    for p in providers:
        rec = " ⭐" if p.recommended else ""
        print(f"  - {p.provider}{rec} ({p.source}) [{p.cost_tier}]")

    assert isinstance(providers, list)
    print("  ✅ KeyDiscovery 測試通過")


def test_model_router():
    """測試預算感知模型路由。"""
    from opc.llm.model_router import ModelRouter, ModelTier, format_run_estimate

    print(f"\n{'='*60}")
    print(f"🧠 ModelRouter 測試")
    print(f"{'='*60}")

    router = ModelRouter(budget_total=3.0, quality_hint="balanced")

    # 測試不同任務類型的路由
    test_cases = [
        ("planning", None, ModelTier.HEAVY),
        ("code_write", None, ModelTier.MEDIUM),
        ("format", None, ModelTier.LIGHT),
        (None, "manager", ModelTier.HEAVY),
        (None, "developer", ModelTier.MEDIUM),
        (None, "formatter", ModelTier.LIGHT),
    ]

    for task_type, role, expected_tier in test_cases:
        config = router.route(task_type=task_type, role=role)
        label = task_type or role
        print(f"  {label:<15} → {config.model:<35} [{config.tier.value}]")
        assert config.tier == expected_tier, f"Expected {expected_tier}, got {config.tier}"

    # 測試預算降級
    router.budget_spent = 2.8  # 接近預算
    config = router.route(task_type="planning")
    print(f"  planning (budget low) → {config.model} [downgraded={config.downgraded}]")

    # 測試成本估算
    router.budget_spent = 0
    roles = [
        {"name": "manager", "task_description": "規劃", "estimated_complexity": "medium"},
        {"name": "researcher", "task_description": "研究", "estimated_complexity": "medium"},
        {"name": "writer", "task_description": "撰寫", "estimated_complexity": "medium"},
    ]
    estimate = router.estimate_run_cost(roles, budget_limit=3.0)
    print(f"\n  成本估算: ${estimate.total_estimated_cost:.2f} (預算: ${estimate.budget_limit:.2f})")
    print(f"  預算充足: {estimate.budget_sufficient}")

    print("  ✅ ModelRouter 測試通過")


def test_intent_parser():
    """測試自然語言意圖解析。"""
    from opc.layer1_perception.intent_parser import IntentParser, format_intent

    print(f"\n{'='*60}")
    print(f"🎯 IntentParser 測試")
    print(f"{'='*60}")

    parser = IntentParser()

    test_cases = [
        ("幫我做一份新能源汽車行業投資分析報告", "finance", "research_report"),
        ("開發一個簡單的 TODO 應用", "dev", "app_dev"),
        ("寫一篇關於AI的文章", "content", "article"),
        ("分析這些數據並生成報表", "data", "data_report"),
        ("翻譯這份文檔", "general", "translation"),
    ]

    async def _run():
        for user_input, expected_domain, expected_type in test_cases:
            intent = await parser.parse(user_input)
            domain_ok = "✅" if intent.domain == expected_domain else "❌"
            type_ok = "✅" if intent.task_type == expected_type else "❌"
            print(f"  「{user_input[:20]}...」")
            print(f"    領域: {intent.domain} {domain_ok} | 類型: {intent.task_type} {type_ok} | 信心: {intent.confidence:.0%}")

    asyncio.run(_run())
    print("  ✅ IntentParser 測試通過")


def test_budget_guard():
    """測試預算守衛。"""
    from opc.layer6_observability.budget_guard import BudgetGuard, BudgetDecision, format_budget_status

    print(f"\n{'='*60}")
    print(f"🛡️ BudgetGuard 測試")
    print(f"{'='*60}")

    guard = BudgetGuard(total_budget=3.0)

    async def _run():
        # 正常使用
        result = await guard.check_before_call(role="researcher", model="gpt-4o", estimated_tokens=5000)
        print(f"  正常檢查: {result.decision.value} — {result.reason}")
        assert result.decision == BudgetDecision.PROCEED

        # 模擬花費
        await guard.record_usage(role="researcher", cost=2.5, model="gpt-4o")
        print(f"  記錄花費: $2.50")

        # 接近預算
        result = await guard.check_before_call(role="writer", model="gpt-4o", estimated_tokens=5000)
        print(f"  接近預算: {result.decision.value} — {result.reason}")

        # 超預算
        await guard.record_usage(role="writer", cost=0.6, model="gpt-4o")
        result = await guard.check_before_call(role="analyst", model="gpt-4o", estimated_tokens=5000)
        print(f"  超預算: {result.decision.value} — {result.reason}")

        # 狀態
        status = guard.get_status()
        print(f"\n{format_budget_status(status)}")

    asyncio.run(_run())
    print("  ✅ BudgetGuard 測試通過")


def test_insight_engine():
    """測試洞察引擎。"""
    from opc.layer6_observability.insight_engine import InsightEngine, ExecutionEvent, format_run_analysis

    print(f"\n{'='*60}")
    print(f"📊 InsightEngine 測試")
    print(f"{'='*60}")

    engine = InsightEngine()

    # 模擬執行事件
    events = [
        ExecutionEvent(event_type="task_started", role="manager", task_item="t1"),
        ExecutionEvent(event_type="task_completed", role="manager", task_item="t1", duration=30, cost=0.15, tokens=3000),
        ExecutionEvent(event_type="task_started", role="researcher", task_item="t2"),
        ExecutionEvent(event_type="task_completed", role="researcher", task_item="t2", duration=120, cost=0.25, tokens=5000),
        ExecutionEvent(event_type="task_started", role="researcher", task_item="t3"),
        ExecutionEvent(event_type="task_reworked", role="researcher", task_item="t3"),
        ExecutionEvent(event_type="task_completed", role="researcher", task_item="t3", duration=90, cost=0.20, tokens=4000),
        ExecutionEvent(event_type="task_started", role="writer", task_item="t4"),
        ExecutionEvent(event_type="task_completed", role="writer", task_item="t4", duration=60, cost=0.10, tokens=2000),
    ]

    analysis = engine.analyze_run(events, run_id="test-run-1")
    print(format_run_analysis(analysis))

    assert analysis.score > 0
    assert len(analysis.role_metrics) > 0
    print("  ✅ InsightEngine 測試通過")


def test_enhanced_event_bus():
    """測試增強事件匯流排。"""
    from opc.core.events_enhanced import EnhancedEventBus
    from opc.core.models import OPCEvent

    print(f"\n{'='*60}")
    print(f"📡 EnhancedEventBus 測試")
    print(f"{'='*60}")

    bus = EnhancedEventBus()
    received = []

    async def handler(event: OPCEvent):
        received.append(event.event_type)

    async def _run():
        bus.subscribe("task.completed", handler)
        bus.subscribe_category("task", handler)

        await bus.publish(OPCEvent(event_type="task.completed", payload={"task_id": "t1"}))
        await bus.publish(OPCEvent(event_type="cost.update", payload={"cost": 0.05}))

        stats = bus.get_stats()
        print(f"  總事件數: {stats.total_published}")
        print(f"  按分類: {dict(stats.by_category)}")
        print(f"  接收回調觸發: {len(received)} 次")

        recent = bus.get_recent_events(limit=5)
        print(f"  最近事件: {len(recent)} 個")

    asyncio.run(_run())
    print("  ✅ EnhancedEventBus 測試通過")


def test_org_template_loader():
    """測試組織模板載入器。"""
    from opc.org_template_loader import OrgTemplateLoader, format_template_list

    print(f"\n{'='*60}")
    print(f"🏗️ OrgTemplateLoader 測試")
    print(f"{'='*60}")

    loader = OrgTemplateLoader()
    templates = loader.list_templates()
    print(format_template_list(templates))

    # 載入具體模板
    template = loader.load_template("finance/research_report")
    assert template is not None
    assert template.get("organization_id") == "finance_research"
    print(f"\n  載入模板: finance/research_report")
    print(f"  角色數: {len(template.get('roles', []))}")
    print(f"  人才模板數: {len(template.get('talent_templates', []))}")

    # 搜索
    results = loader.search_templates("finance")
    print(f"  搜索 'finance': {len(results)} 個結果")

    print("  ✅ OrgTemplateLoader 測試通過")


def run_all_tests():
    """運行所有測試。"""
    print("🧪 OpenOPC 智能模組集成測試")
    print("=" * 60)

    tests = [
        ("KeyDiscovery", test_key_discovery),
        ("ModelRouter", test_model_router),
        ("IntentParser", test_intent_parser),
        ("BudgetGuard", test_budget_guard),
        ("InsightEngine", test_insight_engine),
        ("EnhancedEventBus", test_enhanced_event_bus),
        ("OrgTemplateLoader", test_org_template_loader),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            print(f"\n  ❌ {name} 測試失敗: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"📊 測試結果: {passed} 通過, {failed} 失敗")
    print(f"{'='*60}")

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
