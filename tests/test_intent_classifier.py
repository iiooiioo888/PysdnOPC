"""意圖分類器測試 — 關鍵詞映射、模式選擇、邊界情況。"""

import unittest

from opc.core.intent_classifier import (
    ExecutionModeHint,
    IntentClassifier,
    IntentDomain,
    IntentProfile,
    ModelTier,
    classify_intent,
)


class IntentClassifierDomainTests(unittest.TestCase):
    """領域關鍵詞映射測試。"""

    def setUp(self):
        self.classifier = IntentClassifier()

    def test_coding_domain_chinese(self):
        result = self.classifier.classify("幫我寫一個 Python 爬蟲")
        self.assertIn(IntentDomain.CODING, result.domains)

    def test_coding_domain_english(self):
        result = self.classifier.classify("Write a function to sort an array")
        self.assertIn(IntentDomain.CODING, result.domains)

    def test_writing_domain_chinese(self):
        result = self.classifier.classify("幫我寫一篇關於 AI 的文章")
        self.assertIn(IntentDomain.WRITING, result.domains)

    def test_writing_domain_english(self):
        result = self.classifier.classify("Write a blog post about machine learning")
        self.assertIn(IntentDomain.WRITING, result.domains)

    def test_research_domain_chinese(self):
        result = self.classifier.classify("幫我研究市場趨勢並分析競品")
        self.assertIn(IntentDomain.RESEARCH, result.domains)

    def test_research_domain_english(self):
        result = self.classifier.classify("Research and analyze competitor products")
        self.assertIn(IntentDomain.RESEARCH, result.domains)

    def test_automation_domain_chinese(self):
        result = self.classifier.classify("設定定時自動化備份資料庫")
        self.assertIn(IntentDomain.AUTOMATION, result.domains)

    def test_automation_domain_english(self):
        result = self.classifier.classify("Automate the deployment pipeline with cron")
        self.assertIn(IntentDomain.AUTOMATION, result.domains)

    def test_data_domain_chinese(self):
        result = self.classifier.classify("用 pandas 處理 CSV 數據並視覺化")
        self.assertIn(IntentDomain.DATA, result.domains)

    def test_data_domain_english(self):
        result = self.classifier.classify("Clean the dataset and create a dashboard")
        self.assertIn(IntentDomain.DATA, result.domains)

    def test_chat_fallback_for_unknown(self):
        result = self.classifier.classify("你好，今天天氣如何？")
        self.assertIn(IntentDomain.CHAT, result.domains)

    def test_multiple_domains_detected(self):
        result = self.classifier.classify("寫一個爬蟲抓取數據並生成報告文章")
        self.assertGreaterEqual(len(result.domains), 1)


class IntentClassifierModeTests(unittest.TestCase):
    """執行模式推斷測試。"""

    def setUp(self):
        self.classifier = IntentClassifier()

    def test_simple_task_mode(self):
        result = self.classifier.classify("寫一個 hello world")
        self.assertIn(result.mode_hint, (ExecutionModeHint.TASK, ExecutionModeHint.AUTO))

    def test_complex_company_mode(self):
        result = self.classifier.classify(
            "設計一個完整的微服務系統架構，需要團隊協作分工，"
            "包含前端後端資料庫部署，端到端全流程"
        )
        self.assertEqual(result.mode_hint, ExecutionModeHint.COMPANY)

    def test_multi_role_keywords_trigger_company(self):
        result = self.classifier.classify("需要團隊協作分工寫一個 Python 爬蟲專案")
        self.assertEqual(result.mode_hint, ExecutionModeHint.COMPANY)


class IntentClassifierModelTierTests(unittest.TestCase):
    """模型層級選擇測試。"""

    def setUp(self):
        self.classifier = IntentClassifier()

    def test_coding_gets_critical_tier(self):
        result = self.classifier.classify("實現一個排序演算法")
        self.assertEqual(result.model_tier, ModelTier.CRITICAL)

    def test_writing_gets_routine_tier(self):
        result = self.classifier.classify("寫一封感謝信")
        self.assertEqual(result.model_tier, ModelTier.ROUTINE)

    def test_research_gets_reasoning_tier(self):
        result = self.classifier.classify("分析這個研究論文的數據")
        self.assertEqual(result.model_tier, ModelTier.REASONING)

    def test_high_complexity_gets_critical(self):
        result = self.classifier.classify(
            "設計完整的系統架構，端到端全流程，多步驟多階段，"
            "需要團隊協作分工，從頭到尾完成整個平台基礎設施，"
            "寫一個 Python 爬蟲抓取數據並生成研究分析報告，"
            "包含前端後端資料庫部署和自動化測試審查品質策略規劃方案"
        )
        self.assertEqual(result.model_tier, ModelTier.CRITICAL)


class IntentClassifierEdgeCaseTests(unittest.TestCase):
    """邊界情況測試。"""

    def setUp(self):
        self.classifier = IntentClassifier()

    def test_empty_string(self):
        result = self.classifier.classify("")
        self.assertIn(IntentDomain.CHAT, result.domains)
        self.assertEqual(result.complexity_score, 0.0)

    def test_whitespace_only(self):
        result = self.classifier.classify("   ")
        self.assertIn(IntentDomain.CHAT, result.domains)

    def test_very_long_input(self):
        long_text = "寫一個 Python 程式 " * 100
        result = self.classifier.classify(long_text)
        self.assertIn(IntentDomain.CODING, result.domains)
        self.assertLessEqual(result.complexity_score, 1.0)

    def test_complexity_bounded_0_to_1(self):
        result = self.classifier.classify("完整的系統設計，多階段端到端全流程架構")
        self.assertGreaterEqual(result.complexity_score, 0.0)
        self.assertLessEqual(result.complexity_score, 1.0)

    def test_skills_matched_for_coding(self):
        result = self.classifier.classify("幫我寫一個 Python 腳本")
        self.assertIn("coding", result.skills)

    def test_keywords_matched_tracked(self):
        result = self.classifier.classify("寫一個 Python 爬蟲")
        self.assertGreater(len(result.keywords_matched), 0)

    def test_to_dict_serialization(self):
        result = self.classifier.classify("寫一個 Python 爬蟲")
        d = result.to_dict()
        self.assertIn("domains", d)
        self.assertIn("model_tier", d)
        self.assertIn("complexity_score", d)
        self.assertIsInstance(d["domains"], list)


class ClassifyIntentConvenienceTests(unittest.TestCase):
    """便捷函數測試。"""

    def test_classify_intent_returns_profile(self):
        result = classify_intent("寫一個爬蟲")
        self.assertIsInstance(result, IntentProfile)

    def test_classify_intent_consistent(self):
        r1 = classify_intent("write a python script")
        r2 = classify_intent("write a python script")
        self.assertEqual(r1.domains, r2.domains)
        self.assertEqual(r1.model_tier, r2.model_tier)


if __name__ == "__main__":
    unittest.main()
