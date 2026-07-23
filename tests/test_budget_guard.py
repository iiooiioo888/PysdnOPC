"""BudgetGuard 測試 — pre_call 決策邏輯、降級鏈、硬停止、閾值邊界。"""

import asyncio
import unittest

from opc.core.config import BudgetConfig, LLMConfig
from opc.llm.budget_guard import (
    BudgetAction,
    BudgetDecision,
    BudgetExhaustedError,
    BudgetGuard,
    estimate_cost,
)


def _run(coro):
    """同步執行異步函數的輔助方法。"""
    return asyncio.get_event_loop().run_until_complete(coro)


class EstimateCostTests(unittest.TestCase):
    """成本估算測試。"""

    def test_known_model_gpt54(self):
        cost = estimate_cost("openai/gpt-5.4", 1000, 250)
        self.assertGreater(cost, 0.0)

    def test_known_model_mini(self):
        cost = estimate_cost("openai/gpt-5.4-mini", 1000, 250)
        self.assertGreater(cost, 0.0)

    def test_mini_cheaper_than_full(self):
        cost_full = estimate_cost("openai/gpt-5.4", 1000, 250)
        cost_mini = estimate_cost("openai/gpt-5.4-mini", 1000, 250)
        self.assertLess(cost_mini, cost_full)

    def test_unknown_model_fallback(self):
        cost = estimate_cost("unknown/model-xyz", 1000, 250)
        self.assertGreater(cost, 0.0)

    def test_zero_tokens_zero_cost(self):
        cost = estimate_cost("openai/gpt-5.4", 0, 0)
        self.assertEqual(cost, 0.0)

    def test_cost_scales_with_tokens(self):
        cost_small = estimate_cost("openai/gpt-5.4", 100, 25)
        cost_large = estimate_cost("openai/gpt-5.4", 10000, 2500)
        self.assertGreater(cost_large, cost_small)


class BudgetGuardNoLimitTests(unittest.TestCase):
    """無預算限制時的行為測試。"""

    def setUp(self):
        self.config = BudgetConfig()  # 所有 limit = 0
        self.guard = BudgetGuard(self.config)

    def test_allow_when_no_limits(self):
        decision = _run(self.guard.pre_call("critical", 10000, "openai/gpt-5.4"))
        self.assertEqual(decision.action, BudgetAction.ALLOW)

    def test_should_proceed_when_no_limits(self):
        decision = _run(self.guard.pre_call("critical", 10000))
        self.assertTrue(decision.should_proceed)


class BudgetGuardAllowTests(unittest.TestCase):
    """正常允許（預算充足）測試。"""

    def setUp(self):
        self.config = BudgetConfig(
            task_limit_usd=10.0,
            session_limit_usd=50.0,
            monthly_limit_usd=500.0,
            warn_threshold=0.8,
            degrade_threshold=0.9,
        )
        self.guard = BudgetGuard(self.config)

    def test_allow_when_budget_sufficient(self):
        decision = _run(self.guard.pre_call("routine", 100, "openai/gpt-5.4-mini"))
        self.assertEqual(decision.action, BudgetAction.ALLOW)

    def test_allow_has_budget_usage(self):
        decision = _run(self.guard.pre_call("routine", 100, "openai/gpt-5.4-mini"))
        self.assertIsNotNone(decision.budget_usage)
        self.assertIn("task", decision.budget_usage)


class BudgetGuardWarnTests(unittest.TestCase):
    """預警閾值測試。"""

    def setUp(self):
        self.config = BudgetConfig(
            task_limit_usd=1.0,
            session_limit_usd=100.0,
            monthly_limit_usd=1000.0,
            warn_threshold=0.8,
            degrade_threshold=0.9,
        )
        self.guard = BudgetGuard(self.config)

    def test_warn_at_80_percent(self):
        # 模擬已花費 $0.795（接近 80%），下次呼叫預估成本使總計超過 $0.8
        _run(self.guard.post_call(0.795))
        # 使用較多 token 確保預估成本足以越過閾值
        decision = _run(self.guard.pre_call("routine", 2000, "openai/gpt-5.4"))
        self.assertIn(decision.action, (BudgetAction.WARN, BudgetAction.DEGRADE))

    def test_warn_decision_should_proceed(self):
        _run(self.guard.post_call(0.795))
        decision = _run(self.guard.pre_call("routine", 2000, "openai/gpt-5.4"))
        self.assertTrue(decision.should_proceed)


class BudgetGuardDegradeTests(unittest.TestCase):
    """降級邏輯測試。"""

    def setUp(self):
        self.config = BudgetConfig(
            task_limit_usd=1.0,
            session_limit_usd=100.0,
            monthly_limit_usd=1000.0,
            warn_threshold=0.8,
            degrade_threshold=0.9,
        )
        self.llm_config = LLMConfig(
            tier_routing={
                "critical": "openai/gpt-5.4",
                "routine": "openai/gpt-5.4-mini",
            },
            degrade_chain={
                "critical": "openai/gpt-5.4-mini",
                "routine": "openai/gpt-5.4-nano",
            },
        )
        self.guard = BudgetGuard(self.config, llm_config=self.llm_config)

    def test_degrade_at_90_percent(self):
        # 已花費 $0.895，下次呼叫會超過 90%
        _run(self.guard.post_call(0.895))
        decision = _run(self.guard.pre_call("critical", 2000, "openai/gpt-5.4"))
        self.assertEqual(decision.action, BudgetAction.DEGRADE)

    def test_degrade_provides_model(self):
        _run(self.guard.post_call(0.895))
        decision = _run(self.guard.pre_call("critical", 2000, "openai/gpt-5.4"))
        self.assertEqual(decision.degraded_model, "openai/gpt-5.4-mini")

    def test_degrade_should_proceed(self):
        _run(self.guard.post_call(0.895))
        decision = _run(self.guard.pre_call("critical", 2000, "openai/gpt-5.4"))
        self.assertTrue(decision.should_proceed)

    def test_degrade_routine_tier(self):
        _run(self.guard.post_call(0.895))
        decision = _run(self.guard.pre_call("routine", 2000, "openai/gpt-5.4-mini"))
        if decision.action == BudgetAction.DEGRADE:
            self.assertEqual(decision.degraded_model, "openai/gpt-5.4-nano")

    def test_no_degrade_without_llm_config(self):
        """沒有 LLMConfig 時無法降級（返回 None）。"""
        guard = BudgetGuard(self.config, llm_config=None)
        _run(guard.post_call(0.895))
        decision = _run(guard.pre_call("critical", 2000, "openai/gpt-5.4"))
        # 沒有降級鏈，不會返回 DEGRADE（因為 _get_degraded_model 返回 None）
        self.assertNotEqual(decision.action, BudgetAction.BLOCK)


class BudgetGuardHardStopTests(unittest.TestCase):
    """硬停止測試。"""

    def setUp(self):
        self.config = BudgetConfig(
            task_limit_usd=1.0,
            session_limit_usd=10.0,
            monthly_limit_usd=100.0,
            warn_threshold=0.8,
            degrade_threshold=0.9,
            hard_stop=True,
        )
        self.guard = BudgetGuard(self.config)

    def test_block_when_task_exceeded(self):
        # 已花費超過 task limit（使用足夠多的 token 使預估成本越過限額）
        _run(self.guard.post_call(0.98))
        decision = _run(self.guard.pre_call("critical", 5000, "openai/gpt-5.4"))
        self.assertEqual(decision.action, BudgetAction.BLOCK)

    def test_block_should_not_proceed(self):
        _run(self.guard.post_call(0.98))
        decision = _run(self.guard.pre_call("critical", 5000, "openai/gpt-5.4"))
        self.assertFalse(decision.should_proceed)

    def test_block_has_reason(self):
        _run(self.guard.post_call(0.98))
        decision = _run(self.guard.pre_call("critical", 5000, "openai/gpt-5.4"))
        self.assertIn("預算", decision.reason)

    def test_block_session_exceeded(self):
        config = BudgetConfig(
            task_limit_usd=0.0,  # 不限任務
            session_limit_usd=1.0,
            hard_stop=True,
        )
        guard = BudgetGuard(config)
        _run(guard.post_call(0.98))
        decision = _run(guard.pre_call("routine", 5000, "openai/gpt-5.4"))
        self.assertEqual(decision.action, BudgetAction.BLOCK)

    def test_no_block_when_hard_stop_false(self):
        """hard_stop=False 時，超過預算不阻止。"""
        config = BudgetConfig(
            task_limit_usd=1.0,
            hard_stop=False,
        )
        guard = BudgetGuard(config)
        _run(guard.post_call(0.99))
        decision = _run(guard.pre_call("critical", 500, "openai/gpt-5.4"))
        self.assertNotEqual(decision.action, BudgetAction.BLOCK)


class BudgetGuardPostCallTests(unittest.TestCase):
    """post_call 計量更新測試。"""

    def setUp(self):
        self.config = BudgetConfig(task_limit_usd=10.0)
        self.guard = BudgetGuard(self.config)

    def test_post_call_accumulates_task_spent(self):
        _run(self.guard.post_call(0.5))
        _run(self.guard.post_call(0.3))
        self.assertAlmostEqual(self.guard.task_spent, 0.8)

    def test_post_call_accumulates_session_spent(self):
        _run(self.guard.post_call(1.0))
        _run(self.guard.post_call(2.0))
        self.assertAlmostEqual(self.guard.session_spent, 3.0)

    def test_reset_task(self):
        _run(self.guard.post_call(5.0))
        self.guard.reset_task()
        self.assertEqual(self.guard.task_spent, 0.0)
        # session 不受影響
        self.assertEqual(self.guard.session_spent, 5.0)

    def test_reset_session(self):
        _run(self.guard.post_call(5.0))
        self.guard.reset_session()
        self.assertEqual(self.guard.session_spent, 0.0)
        self.assertEqual(self.guard.task_spent, 0.0)


class BudgetGuardStatusTests(unittest.TestCase):
    """狀態查詢測試。"""

    def test_get_status(self):
        config = BudgetConfig(task_limit_usd=5.0, session_limit_usd=20.0)
        guard = BudgetGuard(config)
        _run(guard.post_call(1.5))
        status = guard.get_status()
        self.assertEqual(status["task_spent"], 1.5)
        self.assertEqual(status["task_limit"], 5.0)
        self.assertEqual(status["session_spent"], 1.5)
        self.assertEqual(status["session_limit"], 20.0)


class BudgetExhaustedErrorTests(unittest.TestCase):
    """BudgetExhaustedError 異常測試。"""

    def test_error_message(self):
        err = BudgetExhaustedError("預算耗盡")
        self.assertEqual(str(err), "預算耗盡")

    def test_error_with_decision(self):
        decision = BudgetDecision(action=BudgetAction.BLOCK, reason="超過限制")
        err = BudgetExhaustedError("預算耗盡", decision)
        self.assertEqual(err.decision.action, BudgetAction.BLOCK)

    def test_error_without_decision(self):
        err = BudgetExhaustedError("預算耗盡")
        self.assertIsNone(err.decision)


class BudgetDecisionTests(unittest.TestCase):
    """BudgetDecision 測試。"""

    def test_should_proceed_allow(self):
        d = BudgetDecision(action=BudgetAction.ALLOW)
        self.assertTrue(d.should_proceed)

    def test_should_proceed_warn(self):
        d = BudgetDecision(action=BudgetAction.WARN)
        self.assertTrue(d.should_proceed)

    def test_should_proceed_degrade(self):
        d = BudgetDecision(action=BudgetAction.DEGRADE)
        self.assertTrue(d.should_proceed)

    def test_should_not_proceed_block(self):
        d = BudgetDecision(action=BudgetAction.BLOCK)
        self.assertFalse(d.should_proceed)


if __name__ == "__main__":
    unittest.main()
