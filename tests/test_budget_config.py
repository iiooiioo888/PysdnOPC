"""BudgetConfig 測試 — 三級預算聲明、默認值、YAML 加載。"""

import unittest

import yaml

from opc.core.config import BudgetConfig, LLMConfig, SystemConfig


class BudgetConfigDefaultTests(unittest.TestCase):
    """默認值測試。"""

    def test_default_task_limit_is_zero(self):
        config = BudgetConfig()
        self.assertEqual(config.task_limit_usd, 0.0)

    def test_default_session_limit_is_zero(self):
        config = BudgetConfig()
        self.assertEqual(config.session_limit_usd, 0.0)

    def test_default_monthly_limit_is_zero(self):
        config = BudgetConfig()
        self.assertEqual(config.monthly_limit_usd, 0.0)

    def test_default_warn_threshold(self):
        config = BudgetConfig()
        self.assertEqual(config.warn_threshold, 0.8)

    def test_default_degrade_threshold(self):
        config = BudgetConfig()
        self.assertEqual(config.degrade_threshold, 0.9)

    def test_default_hard_stop_is_false(self):
        config = BudgetConfig()
        self.assertFalse(config.hard_stop)


class BudgetConfigThreeLevelTests(unittest.TestCase):
    """三級預算聲明測試。"""

    def test_custom_limits(self):
        config = BudgetConfig(
            task_limit_usd=2.0,
            session_limit_usd=10.0,
            monthly_limit_usd=100.0,
        )
        self.assertEqual(config.task_limit_usd, 2.0)
        self.assertEqual(config.session_limit_usd, 10.0)
        self.assertEqual(config.monthly_limit_usd, 100.0)

    def test_get_effective_limit_task(self):
        config = BudgetConfig(task_limit_usd=5.0)
        self.assertEqual(config.get_effective_limit("task"), 5.0)

    def test_get_effective_limit_session(self):
        config = BudgetConfig(session_limit_usd=20.0)
        self.assertEqual(config.get_effective_limit("session"), 20.0)

    def test_get_effective_limit_monthly(self):
        config = BudgetConfig(monthly_limit_usd=200.0)
        self.assertEqual(config.get_effective_limit("monthly"), 200.0)

    def test_get_effective_limit_unknown_level(self):
        config = BudgetConfig(task_limit_usd=5.0)
        self.assertEqual(config.get_effective_limit("unknown"), 0.0)


class BudgetConfigThresholdTests(unittest.TestCase):
    """閾值邏輯測試。"""

    def setUp(self):
        self.config = BudgetConfig(
            task_limit_usd=10.0,
            session_limit_usd=50.0,
            monthly_limit_usd=500.0,
            warn_threshold=0.8,
            degrade_threshold=0.9,
            hard_stop=False,
        )

    def test_should_warn_below_threshold(self):
        # 7.9 < 10 * 0.8 = 8.0 → 不預警
        self.assertFalse(self.config.should_warn("task", 7.9))

    def test_should_warn_at_threshold(self):
        # 8.0 >= 10 * 0.8 = 8.0 → 預警
        self.assertTrue(self.config.should_warn("task", 8.0))

    def test_should_warn_above_threshold(self):
        self.assertTrue(self.config.should_warn("task", 9.0))

    def test_should_degrade_below_threshold(self):
        # 8.9 < 10 * 0.9 = 9.0 → 不降級
        self.assertFalse(self.config.should_degrade("task", 8.9))

    def test_should_degrade_at_threshold(self):
        # 9.0 >= 10 * 0.9 = 9.0 → 降級
        self.assertTrue(self.config.should_degrade("task", 9.0))

    def test_should_degrade_above_threshold(self):
        self.assertTrue(self.config.should_degrade("task", 9.5))

    def test_is_exceeded_below_limit(self):
        self.assertFalse(self.config.is_exceeded("task", 9.99))

    def test_is_exceeded_at_limit(self):
        self.assertTrue(self.config.is_exceeded("task", 10.0))

    def test_is_exceeded_above_limit(self):
        self.assertTrue(self.config.is_exceeded("task", 15.0))

    def test_no_limit_never_warns(self):
        """0 = 不限制，永遠不觸發預警。"""
        config = BudgetConfig(task_limit_usd=0.0)
        self.assertFalse(config.should_warn("task", 999999.0))

    def test_no_limit_never_degrades(self):
        config = BudgetConfig(session_limit_usd=0.0)
        self.assertFalse(config.should_degrade("session", 999999.0))

    def test_no_limit_never_exceeded(self):
        config = BudgetConfig(monthly_limit_usd=0.0)
        self.assertFalse(config.is_exceeded("monthly", 999999.0))

    def test_session_level_thresholds(self):
        # session_limit=50, warn at 40, degrade at 45
        self.assertFalse(self.config.should_warn("session", 39.0))
        self.assertTrue(self.config.should_warn("session", 40.0))
        self.assertTrue(self.config.should_degrade("session", 45.0))

    def test_monthly_level_thresholds(self):
        # monthly_limit=500, warn at 400, degrade at 450
        self.assertFalse(self.config.should_warn("monthly", 399.0))
        self.assertTrue(self.config.should_warn("monthly", 400.0))
        self.assertTrue(self.config.should_degrade("monthly", 450.0))


class BudgetConfigYAMLLoadTests(unittest.TestCase):
    """YAML 加載測試。"""

    def test_load_from_yaml_dict(self):
        yaml_str = """
task_limit_usd: 2.0
session_limit_usd: 10.0
monthly_limit_usd: 100.0
warn_threshold: 0.8
degrade_threshold: 0.9
hard_stop: false
"""
        data = yaml.safe_load(yaml_str)
        config = BudgetConfig(**data)
        self.assertEqual(config.task_limit_usd, 2.0)
        self.assertEqual(config.session_limit_usd, 10.0)
        self.assertEqual(config.monthly_limit_usd, 100.0)
        self.assertFalse(config.hard_stop)

    def test_load_partial_yaml(self):
        yaml_str = """
task_limit_usd: 0.5
hard_stop: true
"""
        data = yaml.safe_load(yaml_str)
        config = BudgetConfig(**data)
        self.assertEqual(config.task_limit_usd, 0.5)
        self.assertTrue(config.hard_stop)
        # 未指定的使用默認值
        self.assertEqual(config.session_limit_usd, 0.0)
        self.assertEqual(config.warn_threshold, 0.8)

    def test_load_empty_yaml_gives_defaults(self):
        config = BudgetConfig(**{})
        self.assertEqual(config.task_limit_usd, 0.0)
        self.assertEqual(config.warn_threshold, 0.8)


class SystemConfigBudgetIntegrationTests(unittest.TestCase):
    """SystemConfig 整合 BudgetConfig 測試。"""

    def test_system_config_has_budget_field(self):
        config = SystemConfig()
        self.assertIsInstance(config.budget, BudgetConfig)

    def test_system_config_budget_defaults(self):
        config = SystemConfig()
        self.assertEqual(config.budget.task_limit_usd, 0.0)
        self.assertFalse(config.budget.hard_stop)


class LLMConfigTierRoutingTests(unittest.TestCase):
    """LLMConfig 分層路由測試。"""

    def test_default_tier_routing_empty(self):
        config = LLMConfig()
        self.assertEqual(config.tier_routing, {})

    def test_default_degrade_chain_empty(self):
        config = LLMConfig()
        self.assertEqual(config.degrade_chain, {})

    def test_get_model_for_tier_normal(self):
        config = LLMConfig(
            tier_routing={"critical": "openai/gpt-5.4", "routine": "openai/gpt-5.4-mini"},
        )
        self.assertEqual(config.get_model_for_tier("critical"), "openai/gpt-5.4")
        self.assertEqual(config.get_model_for_tier("routine"), "openai/gpt-5.4-mini")

    def test_get_model_for_tier_degraded(self):
        config = LLMConfig(
            tier_routing={"critical": "openai/gpt-5.4"},
            degrade_chain={"critical": "openai/gpt-5.4-mini"},
        )
        self.assertEqual(config.get_model_for_tier("critical", degraded=True), "openai/gpt-5.4-mini")

    def test_get_model_for_tier_degraded_fallback_to_normal(self):
        """降級鏈中沒有該 tier 時，返回正常路由。"""
        config = LLMConfig(
            tier_routing={"routine": "openai/gpt-5.4-mini"},
            degrade_chain={},
        )
        self.assertEqual(config.get_model_for_tier("routine", degraded=True), "openai/gpt-5.4-mini")

    def test_get_model_for_tier_unknown_returns_none(self):
        config = LLMConfig()
        self.assertIsNone(config.get_model_for_tier("unknown_tier"))

    def test_tier_routing_from_yaml(self):
        yaml_str = """
default_model: "anthropic/claude-sonnet-4-20250514"
tier_routing:
  critical: "openai/gpt-5.4"
  reasoning: "openai/gpt-5.4"
  routine: "openai/gpt-5.4-mini"
  summary: "openai/gpt-5.4-nano"
degrade_chain:
  critical: "openai/gpt-5.4-mini"
  reasoning: "openai/gpt-5.4-mini"
  routine: "openai/gpt-5.4-nano"
  summary: "openai/gpt-5.4-nano"
"""
        data = yaml.safe_load(yaml_str)
        config = LLMConfig(**data)
        self.assertEqual(config.tier_routing["critical"], "openai/gpt-5.4")
        self.assertEqual(config.degrade_chain["critical"], "openai/gpt-5.4-mini")
        self.assertEqual(config.get_model_for_tier("summary", degraded=True), "openai/gpt-5.4-nano")


if __name__ == "__main__":
    unittest.main()
