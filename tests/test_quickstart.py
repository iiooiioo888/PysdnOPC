"""快速啟動引擎測試 — 意圖分類、配置推斷、臨時配置構建、缺失 key 檢測。"""

import os
import unittest
from unittest.mock import patch

from opc.core.quickstart import (
    QuickStartEngine,
    QuickStartResult,
    quickstart_infer,
)
from opc.core.intent_classifier import IntentDomain, ModelTier


class QuickStartInferConfigTests(unittest.TestCase):
    """配置推斷測試。"""

    def setUp(self):
        self.engine = QuickStartEngine()

    def test_infer_returns_result(self):
        result = self.engine.infer_config("寫一個 Python 爬蟲")
        self.assertIsInstance(result, QuickStartResult)

    def test_infer_detects_coding_domain(self):
        result = self.engine.infer_config("幫我寫一個 Python 程式")
        self.assertIn(IntentDomain.CODING, result.intent_profile.domains)

    def test_infer_selects_model(self):
        result = self.engine.infer_config("寫一個爬蟲")
        self.assertIn("llm", result.inferred_config)
        self.assertIn("default_model", result.inferred_config["llm"])

    def test_infer_sets_temperature_for_coding(self):
        result = self.engine.infer_config("實現一個排序演算法")
        temp = result.inferred_config.get("llm", {}).get("temperature", 1.0)
        self.assertLessEqual(temp, 0.3)

    def test_infer_sets_temperature_for_writing(self):
        result = self.engine.infer_config("寫一篇創意小說")
        temp = result.inferred_config.get("llm", {}).get("temperature", 0.0)
        self.assertGreaterEqual(temp, 0.7)

    def test_infer_complex_task_more_iterations(self):
        result = self.engine.infer_config(
            "設計完整的系統架構，端到端全流程，多步驟多階段，團隊協作"
        )
        max_iter = result.inferred_config.get("system", {}).get("max_agent_iterations", 0)
        self.assertGreaterEqual(max_iter, 50)


class QuickStartMissingKeyTests(unittest.TestCase):
    """缺失 API key 檢測測試。"""

    def setUp(self):
        self.engine = QuickStartEngine()

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_api_key_detected(self):
        # 清除所有已知 API key 環境變數
        known_vars = [
            "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
            "GROQ_API_KEY", "MISTRAL_API_KEY", "TOGETHERAI_API_KEY", "ARK_API_KEY",
        ]
        for var in known_vars:
            os.environ.pop(var, None)

        result = self.engine.infer_config("寫一個爬蟲")
        self.assertIn("api_key", result.missing_items)
        self.assertFalse(result.can_proceed)

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key-123"})
    def test_api_key_detected_from_env(self):
        result = self.engine.infer_config("寫一個爬蟲")
        self.assertNotIn("api_key", result.missing_items)
        self.assertTrue(result.can_proceed)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"})
    def test_openai_key_detected(self):
        result = self.engine.infer_config("hello")
        self.assertTrue(result.can_proceed)


class QuickStartEphemeralConfigTests(unittest.TestCase):
    """臨時配置構建測試。"""

    def setUp(self):
        self.engine = QuickStartEngine()

    def test_build_ephemeral_with_api_key(self):
        result = self.engine.infer_config("寫一個爬蟲")
        config = self.engine.build_ephemeral_config(result, api_key="sk-test")
        self.assertEqual(config["llm"]["api_key"], "sk-test")

    def test_build_ephemeral_with_api_key_env(self):
        result = self.engine.infer_config("寫一個爬蟲")
        config = self.engine.build_ephemeral_config(result, api_key_env="MY_KEY")
        self.assertEqual(config["llm"]["api_key_env"], "MY_KEY")

    def test_build_ephemeral_preserves_inferred(self):
        result = self.engine.infer_config("寫一個爬蟲")
        config = self.engine.build_ephemeral_config(result, api_key="sk-test")
        self.assertIn("default_model", config["llm"])
        self.assertIn("system", config)

    def test_to_dict_serialization(self):
        result = self.engine.infer_config("寫一個爬蟲")
        d = result.to_dict()
        self.assertIn("intent_profile", d)
        self.assertIn("missing_items", d)
        self.assertIn("can_proceed", d)


class QuickStartFromQuickstartConfigTests(unittest.TestCase):
    """OPCConfig.from_quickstart 整合測試。"""

    def test_from_quickstart_creates_config(self):
        from opc.core.config import OPCConfig

        config = OPCConfig.from_quickstart("寫一個 Python 爬蟲", api_key="sk-test")
        self.assertIsNotNone(config)
        self.assertEqual(config.llm.api_key, "sk-test")

    def test_from_quickstart_infers_model(self):
        from opc.core.config import OPCConfig

        config = OPCConfig.from_quickstart("實現一個演算法", api_key="sk-test")
        # 編碼任務應使用 critical tier 模型
        self.assertIn("gpt-5.4", config.llm.default_model)

    def test_from_quickstart_with_overrides(self):
        from opc.core.config import OPCConfig

        config = OPCConfig.from_quickstart(
            "寫一個爬蟲",
            overrides={"llm": {"temperature": 0.9}},
            api_key="sk-test",
        )
        self.assertEqual(config.llm.temperature, 0.9)

    def test_from_quickstart_low_temperature_for_coding(self):
        from opc.core.config import OPCConfig

        config = OPCConfig.from_quickstart("寫一個排序演算法", api_key="sk-test")
        self.assertLessEqual(config.llm.temperature, 0.3)


class QuickStartConvenienceTests(unittest.TestCase):
    """便捷函數測試。"""

    @patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
    def test_quickstart_infer_returns_result(self):
        result = quickstart_infer("寫一個爬蟲")
        self.assertIsInstance(result, QuickStartResult)
        self.assertTrue(result.can_proceed)


if __name__ == "__main__":
    unittest.main()
