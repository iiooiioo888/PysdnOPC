"""LLMProvider 預算整合測試 — 預算攔截、降級模型切換、cost breakdown。"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from opc.core.config import BudgetConfig, LLMConfig
from opc.llm.budget_guard import (
    BudgetAction,
    BudgetDecision,
    BudgetExhaustedError,
    BudgetGuard,
)


def _run(coro):
    """同步執行異步函數的輔助方法。"""
    return asyncio.get_event_loop().run_until_complete(coro)


class LLMProviderBudgetInitTests(unittest.TestCase):
    """LLMProvider 初始化整合 BudgetGuard 測試。"""

    def test_provider_accepts_budget_guard(self):
        from opc.llm.provider import LLMProvider

        config = LLMConfig(default_model="openai/gpt-5.4-mini", api_key="sk-test")
        budget_config = BudgetConfig(task_limit_usd=1.0)
        guard = BudgetGuard(budget_config)
        provider = LLMProvider(config, budget_guard=guard)
        self.assertIsNotNone(provider._budget_guard)

    def test_provider_without_budget_guard(self):
        from opc.llm.provider import LLMProvider

        config = LLMConfig(default_model="openai/gpt-5.4-mini", api_key="sk-test")
        provider = LLMProvider(config)
        self.assertIsNone(provider._budget_guard)


class LLMProviderBudgetBlockTests(unittest.TestCase):
    """預算攔截（BLOCK）測試。"""

    def test_chat_raises_budget_exhausted_on_block(self):
        from opc.llm.provider import LLMProvider

        config = LLMConfig(default_model="openai/gpt-5.4", api_key="sk-test")
        budget_config = BudgetConfig(task_limit_usd=0.01, hard_stop=True)
        guard = BudgetGuard(budget_config)
        # 模擬已花費超過預算
        _run(guard.post_call(0.02))

        provider = LLMProvider(config, budget_guard=guard)
        messages = [{"role": "user", "content": "Hello"}]

        with self.assertRaises(BudgetExhaustedError):
            _run(provider.chat(messages, budget_tier="critical"))

    def test_stream_raises_budget_exhausted_on_block(self):
        from opc.llm.provider import LLMProvider

        config = LLMConfig(default_model="openai/gpt-5.4", api_key="sk-test")
        budget_config = BudgetConfig(task_limit_usd=0.01, hard_stop=True)
        guard = BudgetGuard(budget_config)
        _run(guard.post_call(0.02))

        provider = LLMProvider(config, budget_guard=guard)
        messages = [{"role": "user", "content": "Hello"}]

        async def _consume_stream():
            async for _ in provider.chat_stream(messages, budget_tier="critical"):
                pass

        with self.assertRaises(BudgetExhaustedError):
            _run(_consume_stream())


class LLMProviderBudgetDegradeTests(unittest.TestCase):
    """降級模型切換測試。"""

    @patch("opc.llm.provider.litellm")
    def test_chat_uses_degraded_model(self, mock_litellm):
        from opc.llm.provider import LLMProvider

        config = LLMConfig(
            default_model="openai/gpt-5.4",
            api_key="sk-test",
            tier_routing={"critical": "openai/gpt-5.4"},
            degrade_chain={"critical": "openai/gpt-5.4-mini"},
        )
        budget_config = BudgetConfig(
            task_limit_usd=1.0,
            warn_threshold=0.8,
            degrade_threshold=0.9,
        )
        guard = BudgetGuard(budget_config, llm_config=config)
        # 模擬已花費接近降級閾值（90% of $1.0 = $0.9）
        # 預估 "Hello "*500 ≈ 750 tokens → ~$0.005，所以需 spent + 0.005 > 0.9
        _run(guard.post_call(0.90))

        provider = LLMProvider(config, budget_guard=guard)

        # Mock litellm.acompletion 返回
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "response"
        mock_response.choices[0].message.tool_calls = None
        mock_response.choices[0].finish_reason = "stop"
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)
        mock_litellm.completion_cost = MagicMock(return_value=0.001)

        # 使用較長訊息確保預估成本越過閾值
        messages = [{"role": "user", "content": "Hello " * 500}]
        result = _run(provider.chat(messages, budget_tier="critical"))

        # 驗證使用了降級模型
        call_kwargs = mock_litellm.acompletion.call_args[1]
        self.assertEqual(call_kwargs["model"], "openai/gpt-5.4-mini")

    @patch("opc.llm.provider.litellm")
    def test_chat_uses_original_model_when_budget_ok(self, mock_litellm):
        from opc.llm.provider import LLMProvider

        config = LLMConfig(
            default_model="openai/gpt-5.4",
            api_key="sk-test",
        )
        budget_config = BudgetConfig(task_limit_usd=100.0)
        guard = BudgetGuard(budget_config)

        provider = LLMProvider(config, budget_guard=guard)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "response"
        mock_response.choices[0].message.tool_calls = None
        mock_response.choices[0].finish_reason = "stop"
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)
        mock_litellm.completion_cost = MagicMock(return_value=0.001)

        messages = [{"role": "user", "content": "Hello"}]
        result = _run(provider.chat(messages))

        call_kwargs = mock_litellm.acompletion.call_args[1]
        self.assertEqual(call_kwargs["model"], "openai/gpt-5.4")


class LLMProviderPostCallCostTests(unittest.TestCase):
    """呼叫後成本更新測試。"""

    @patch("opc.llm.provider.litellm")
    def test_post_call_updates_guard_spending(self, mock_litellm):
        from opc.llm.provider import LLMProvider

        config = LLMConfig(default_model="openai/gpt-5.4", api_key="sk-test")
        budget_config = BudgetConfig(task_limit_usd=10.0)
        guard = BudgetGuard(budget_config)
        provider = LLMProvider(config, budget_guard=guard)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "response"
        mock_response.choices[0].message.tool_calls = None
        mock_response.choices[0].finish_reason = "stop"
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_litellm.acompletion = AsyncMock(return_value=mock_response)
        mock_litellm.completion_cost = MagicMock(return_value=0.05)

        messages = [{"role": "user", "content": "Hello"}]
        _run(provider.chat(messages))

        # 驗證 budget guard 已更新花費
        self.assertGreater(guard.task_spent, 0.0)
        self.assertAlmostEqual(guard.task_spent, 0.05)


class CostTrackerBudgetStatusTests(unittest.TestCase):
    """CostTracker 預算狀態報告測試。"""

    def test_budget_status_dataclass(self):
        from opc.layer6_observability.cost_tracker import BudgetStatus

        status = BudgetStatus(
            task_spent=0.5,
            task_limit=2.0,
            session_spent=3.0,
            session_limit=10.0,
            monthly_spent=30.0,
            monthly_limit=100.0,
        )
        self.assertAlmostEqual(status.task_usage_pct, 25.0)
        self.assertAlmostEqual(status.session_usage_pct, 30.0)
        self.assertAlmostEqual(status.monthly_usage_pct, 30.0)

    def test_budget_status_zero_limit(self):
        from opc.layer6_observability.cost_tracker import BudgetStatus

        status = BudgetStatus(task_spent=5.0, task_limit=0.0)
        self.assertEqual(status.task_usage_pct, 0.0)

    def test_budget_status_to_dict(self):
        from opc.layer6_observability.cost_tracker import BudgetStatus

        status = BudgetStatus(
            task_spent=1.0,
            task_limit=2.0,
            session_spent=5.0,
            session_limit=10.0,
            monthly_spent=50.0,
            monthly_limit=100.0,
        )
        d = status.to_dict()
        self.assertIn("task", d)
        self.assertIn("session", d)
        self.assertIn("monthly", d)
        self.assertEqual(d["task"]["spent"], 1.0)
        self.assertEqual(d["task"]["limit"], 2.0)
        self.assertAlmostEqual(d["task"]["usage_pct"], 50.0)

    def test_budget_status_caps_at_100(self):
        from opc.layer6_observability.cost_tracker import BudgetStatus

        status = BudgetStatus(task_spent=15.0, task_limit=10.0)
        self.assertEqual(status.task_usage_pct, 100.0)


class CostTrackerGetBudgetStatusTests(unittest.TestCase):
    """CostTracker.get_budget_status 測試。"""

    def test_get_budget_status_with_config(self):
        from opc.layer6_observability.cost_tracker import CostTracker

        tracker = CostTracker.__new__(CostTracker)
        tracker._session_total = 5.0

        budget_config = BudgetConfig(
            task_limit_usd=2.0,
            session_limit_usd=10.0,
            monthly_limit_usd=100.0,
        )
        status = tracker.get_budget_status(
            task_spent=1.0,
            session_spent=5.0,
            budget_config=budget_config,
        )
        self.assertEqual(status.task_spent, 1.0)
        self.assertEqual(status.session_spent, 5.0)
        self.assertEqual(status.task_limit, 2.0)
        self.assertEqual(status.session_limit, 10.0)
        self.assertEqual(status.monthly_limit, 100.0)

    def test_get_budget_status_without_config(self):
        from opc.layer6_observability.cost_tracker import CostTracker

        tracker = CostTracker.__new__(CostTracker)
        tracker._session_total = 3.0

        status = tracker.get_budget_status(task_spent=0.5)
        self.assertEqual(status.task_spent, 0.5)
        self.assertEqual(status.task_limit, 0.0)  # 無配置時 limit=0


class EndToEndBudgetFlowTests(unittest.TestCase):
    """端到端預算流程測試。"""

    def test_full_budget_lifecycle(self):
        """模擬完整的預算生命週期：正常→預警→降級→阻止。"""
        config = BudgetConfig(
            task_limit_usd=1.0,
            session_limit_usd=100.0,
            monthly_limit_usd=1000.0,
            warn_threshold=0.8,
            degrade_threshold=0.9,
            hard_stop=True,
        )
        llm_config = LLMConfig(
            tier_routing={"critical": "openai/gpt-5.4"},
            degrade_chain={"critical": "openai/gpt-5.4-mini"},
        )
        guard = BudgetGuard(config, llm_config=llm_config)

        # 階段 1：正常允許（已花費 0，預估成本很小）
        d1 = _run(guard.pre_call("critical", 100, "openai/gpt-5.4"))
        self.assertEqual(d1.action, BudgetAction.ALLOW)
        _run(guard.post_call(0.5))

        # 階段 2：已花費 $0.5，使用大量 token 使預估成本推動越過 80% 閾值
        d2 = _run(guard.pre_call("critical", 50000, "openai/gpt-5.4"))
        self.assertIn(d2.action, (BudgetAction.WARN, BudgetAction.DEGRADE, BudgetAction.BLOCK))
        _run(guard.post_call(0.39))

        # 階段 3：已花費 $0.89，接近降級閾值
        d3 = _run(guard.pre_call("critical", 5000, "openai/gpt-5.4"))
        self.assertIn(d3.action, (BudgetAction.WARN, BudgetAction.DEGRADE, BudgetAction.BLOCK))
        _run(guard.post_call(0.09))

        # 階段 4：已花費 $0.98，超過 task limit 的 90%+，hard_stop 觸發
        d4 = _run(guard.pre_call("critical", 5000, "openai/gpt-5.4"))
        self.assertEqual(d4.action, BudgetAction.BLOCK)
        self.assertFalse(d4.should_proceed)

    def test_no_budget_config_passthrough(self):
        """無預算配置時所有呼叫直接通過。"""
        config = BudgetConfig()  # 全部為 0
        guard = BudgetGuard(config)

        for _ in range(10):
            decision = _run(guard.pre_call("critical", 100000, "openai/gpt-5.4"))
            self.assertEqual(decision.action, BudgetAction.ALLOW)
            _run(guard.post_call(10.0))

        self.assertEqual(guard.task_spent, 100.0)


if __name__ == "__main__":
    unittest.main()
