"""零配置快速啟動引擎。

從自然語言意圖推斷最小可用配置，讓使用者在 60 秒內執行第一個任務，
無需手動執行 `opc init` 或編輯 YAML 配置檔。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from opc.core.intent_classifier import (
    ExecutionModeHint,
    IntentClassifier,
    IntentProfile,
    ModelTier,
    classify_intent,
)


@dataclass
class QuickStartResult:
    """快速啟動推断结果。"""

    intent_profile: IntentProfile
    missing_items: list[str] = field(default_factory=list)  # 需要追問的缺失項
    warnings: list[str] = field(default_factory=list)
    inferred_config: dict[str, Any] = field(default_factory=dict)
    can_proceed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """轉換為字典。"""
        return {
            "intent_profile": self.intent_profile.to_dict(),
            "missing_items": self.missing_items,
            "warnings": self.warnings,
            "inferred_config": self.inferred_config,
            "can_proceed": self.can_proceed,
        }


# 已知的 API key 環境變數
_KNOWN_API_KEY_ENVS = [
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "TOGETHERAI_API_KEY",
    "ARK_API_KEY",
]

# 模型層級到預設模型的映射
_TIER_DEFAULT_MODELS: dict[ModelTier, str] = {
    ModelTier.CRITICAL: "openai/gpt-5.4",
    ModelTier.REASONING: "openai/gpt-5.4",
    ModelTier.ROUTINE: "openai/gpt-5.4-mini",
    ModelTier.SUMMARY: "openai/gpt-5.4-nano",
}


class QuickStartEngine:
    """從自然語言意圖推斷最小可用配置。

    使用規則引擎（關鍵詞 + 模式匹配）進行推断，
    不依賴 LLM 呼叫，避免雞生蛋問題。
    """

    def __init__(self, classifier: IntentClassifier | None = None) -> None:
        self._classifier = classifier or IntentClassifier()

    def infer_config(self, intent: str) -> QuickStartResult:
        """解析使用者意圖，返回推斷的配置和缺失項。

        Args:
            intent: 使用者輸入的自然語言意圖描述。

        Returns:
            QuickStartResult 包含推斷配置、缺失項和警告。
        """
        # 1. 意圖分類
        profile = self._classifier.classify(intent)
        logger.debug(
            "QuickStart: intent='{}' -> domains={}, tier={}, complexity={:.2f}",
            intent[:50],
            [d.value for d in profile.domains],
            profile.model_tier.value,
            profile.complexity_score,
        )

        # 2. 檢測 API key
        missing_items: list[str] = []
        warnings: list[str] = []
        api_key_env = self._detect_api_key_env()

        if not api_key_env:
            missing_items.append("api_key")
            warnings.append(
                "未檢測到 API key。請設定環境變數（如 OPENROUTER_API_KEY）"
                "或在互動模式中提供。"
            )

        # 3. 推斷配置
        inferred_config = self._build_inferred_config(profile, api_key_env)

        # 4. 判斷是否可以繼續
        can_proceed = len(missing_items) == 0

        return QuickStartResult(
            intent_profile=profile,
            missing_items=missing_items,
            warnings=warnings,
            inferred_config=inferred_config,
            can_proceed=can_proceed,
        )

    def build_ephemeral_config(
        self,
        result: QuickStartResult,
        api_key: str | None = None,
        api_key_env: str | None = None,
    ) -> dict[str, Any]:
        """構建記憶體中的臨時配置（不寫磁碟）。

        Args:
            result: infer_config() 的結果。
            api_key: 直接提供的 API key。
            api_key_env: API key 環境變數名稱。

        Returns:
            可直接傳遞給 OPCConfig.model_validate() 的配置字典。
        """
        config = dict(result.inferred_config)

        # 處理 API key
        if api_key:
            config.setdefault("llm", {})["api_key"] = api_key
        elif api_key_env:
            config.setdefault("llm", {})["api_key_env"] = api_key_env

        return config

    def _detect_api_key_env(self) -> str | None:
        """檢測已設定的 API key 環境變數。"""
        for env_name in _KNOWN_API_KEY_ENVS:
            if os.environ.get(env_name):
                logger.debug("QuickStart: detected API key env: {}", env_name)
                return env_name
        return None

    def _build_inferred_config(
        self,
        profile: IntentProfile,
        api_key_env: str | None,
    ) -> dict[str, Any]:
        """根據意圖分析結果構建推斷配置。"""
        # 選擇模型
        model = _TIER_DEFAULT_MODELS.get(
            profile.model_tier,
            "openai/gpt-5.4-mini",
        )

        # 構建 LLM 配置
        llm_config: dict[str, Any] = {
            "default_model": model,
            "api_base": "https://openrouter.ai/api/v1",
            "temperature": self._infer_temperature(profile),
            "max_tokens": 32768,
        }

        if api_key_env:
            llm_config["api_key_env"] = api_key_env

        # 構建系統配置
        system_config: dict[str, Any] = {
            "log_level": "INFO",
            "default_channel": "cli",
            "max_agent_iterations": self._infer_max_iterations(profile),
            "native_runtime": {
                "enabled": True,
                "stream_llm": True,
            },
        }

        # 構建完整配置
        config: dict[str, Any] = {
            "llm": llm_config,
            "system": system_config,
        }

        # 根據執行模式添加配置
        if profile.mode_hint == ExecutionModeHint.COMPANY:
            config["system"]["task_mode"] = {
                "sub_agent_timeout_sec": 86400,
            }

        return config

    def _infer_temperature(self, profile: IntentProfile) -> float:
        """根據意圖推斷溫度參數。"""
        from opc.core.intent_classifier import IntentDomain

        # 創意寫作需要較高溫度
        if IntentDomain.WRITING in profile.domains:
            return 0.8

        # 程式碼生成需要較低溫度
        if IntentDomain.CODING in profile.domains:
            return 0.2

        # 研究和分析需要中等溫度
        if IntentDomain.RESEARCH in profile.domains:
            return 0.5

        # 預設溫度
        return 0.5

    def _infer_max_iterations(self, profile: IntentProfile) -> int:
        """根據複雜度推斷最大迭代次數。"""
        if profile.complexity_score > 0.7:
            return 100  # 複雜任務
        elif profile.complexity_score > 0.4:
            return 50  # 中等任務
        else:
            return 30  # 簡單任務


# 全域單例
_default_engine: QuickStartEngine | None = None


def get_quickstart_engine() -> QuickStartEngine:
    """取得預設快速啟動引擎實例。"""
    global _default_engine
    if _default_engine is None:
        _default_engine = QuickStartEngine()
    return _default_engine


def quickstart_infer(intent: str) -> QuickStartResult:
    """便捷函數：推斷快速啟動配置。"""
    return get_quickstart_engine().infer_config(intent)
