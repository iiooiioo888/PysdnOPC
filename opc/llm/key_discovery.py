"""API Key 自動發現模組。

職責說明：
    自動掃描環境變數、配置文件、系統鑰匙鏈，發現可用的 LLM API Key。
    用於零配置啟動場景：用戶無需手動編輯 YAML 即可開始使用。

使用範例：
    from opc.llm.key_discovery import KeyDiscovery
    discovery = KeyDiscovery()
    providers = discovery.discover()
    for p in providers:
        print(f"{p.provider}: {p.model_recommendation}")
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


@dataclass
class ProviderInfo:
    """已發現的 Provider 資訊。"""
    provider: str                    # 提供者名稱 (openai, anthropic, deepseek, ...)
    key: str                         # API Key (已驗證格式)
    source: str                      # 來源 (env, file, keychain)
    api_base: str = ""               # 自定義 API Base URL
    models: list[str] = field(default_factory=list)  # 推薦模型列表
    cost_tier: str = "medium"        # 成本等級 (cheap, medium, expensive)
    strength: str = "general"        # 擅長領域 (code, reasoning, chinese, general)
    recommended: bool = False        # 是否為推薦選項


# Provider 定義：環境變數、模型、成本、特長
_PROVIDER_DEFS: list[dict[str, Any]] = [
    {
        "name": "deepseek",
        "env_keys": ["DEEPSEEK_API_KEY"],
        "api_base": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "cost_tier": "cheap",
        "strength": "chinese",
        "key_prefix": "sk-",
        "min_length": 20,
    },
    {
        "name": "openai",
        "env_keys": ["OPENAI_API_KEY"],
        "api_base": "",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
        "cost_tier": "expensive",
        "strength": "general",
        "key_prefix": "sk-",
        "min_length": 20,
    },
    {
        "name": "anthropic",
        "env_keys": ["ANTHROPIC_API_KEY"],
        "api_base": "",
        "models": ["claude-sonnet-4-20250514", "claude-3-5-haiku-20241022"],
        "cost_tier": "expensive",
        "strength": "reasoning",
        "key_prefix": "sk-ant-",
        "min_length": 20,
    },
    {
        "name": "google",
        "env_keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "api_base": "",
        "models": ["gemini-2.0-flash", "gemini-2.5-pro"],
        "cost_tier": "medium",
        "strength": "general",
        "key_prefix": "AI",
        "min_length": 20,
    },
    {
        "name": "openrouter",
        "env_keys": ["OPENROUTER_API_KEY"],
        "api_base": "https://openrouter.ai/api/v1",
        "models": ["anthropic/claude-sonnet-4", "deepseek/deepseek-chat-v3-0324"],
        "cost_tier": "medium",
        "strength": "general",
        "key_prefix": "sk-or-",
        "min_length": 20,
    },
    {
        "name": "mistral",
        "env_keys": ["MISTRAL_API_KEY"],
        "api_base": "",
        "models": ["mistral-large-latest", "mistral-small-latest"],
        "cost_tier": "medium",
        "strength": "general",
        "key_prefix": "",
        "min_length": 10,
    },
    {
        "name": "groq",
        "env_keys": ["GROQ_API_KEY"],
        "api_base": "https://api.groq.com/openai/v1",
        "models": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
        "cost_tier": "cheap",
        "strength": "general",
        "key_prefix": "gsk_",
        "min_length": 20,
    },
    {
        "name": "together",
        "env_keys": ["TOGETHERAI_API_KEY"],
        "api_base": "https://api.together.xyz/v1",
        "models": ["meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo"],
        "cost_tier": "cheap",
        "strength": "general",
        "key_prefix": "",
        "min_length": 20,
    },
    {
        "name": "volcengine",
        "env_keys": ["ARK_API_KEY"],
        "api_base": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-pro-32k", "doubao-lite-32k"],
        "cost_tier": "cheap",
        "strength": "chinese",
        "key_prefix": "",
        "min_length": 10,
    },
    {
        "name": "dashscope",
        "env_keys": ["DASHSCOPE_API_KEY", "QWEN_API_KEY"],
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-coder-plus"],
        "cost_tier": "cheap",
        "strength": "chinese",
        "key_prefix": "sk-",
        "min_length": 20,
    },
]

# 成本等級排序（便宜優先）
_COST_TIER_ORDER = {"cheap": 0, "medium": 1, "expensive": 2}


class KeyDiscovery:
    """API Key 自動發現器。

    掃描策略（按優先級）：
    1. 環境變數（最常見，最安全）
    2. ~/.opc/keys.yaml（用戶配置文件）
    3. 項目級 .opc/config/llm_config.yaml（已有配置）
    """

    def __init__(self, opc_home: Path | None = None) -> None:
        self.opc_home = opc_home or Path.home() / ".opc"

    def discover(self) -> list[ProviderInfo]:
        """掃描所有來源，返回已發現且格式合法的 Provider 列表。"""
        found: dict[str, ProviderInfo] = {}

        # 1. 掃描環境變數
        for defn in _PROVIDER_DEFS:
            for env_key in defn["env_keys"]:
                key = os.environ.get(env_key, "").strip()
                if key and self._validate_key_format(key, defn):
                    provider = defn["name"]
                    if provider not in found:
                        info = ProviderInfo(
                            provider=provider,
                            key=key,
                            source=f"env:{env_key}",
                            api_base=defn.get("api_base", ""),
                            models=list(defn.get("models", [])),
                            cost_tier=defn.get("cost_tier", "medium"),
                            strength=defn.get("strength", "general"),
                        )
                        found[provider] = info
                        logger.debug(f"Discovered key for {provider} from {env_key}")
                    break  # 找到一個即可

        # 2. 掃描 ~/.opc/keys.yaml
        keys_file = self.opc_home / "keys.yaml"
        if keys_file.exists():
            try:
                with open(keys_file, encoding="utf-8") as f:
                    keys_data = yaml.safe_load(f) or {}
                if isinstance(keys_data, dict):
                    for provider_name, key_value in keys_data.items():
                        if provider_name in found:
                            continue
                        key = str(key_value or "").strip()
                        defn = self._find_provider_def(provider_name)
                        if defn and key and self._validate_key_format(key, defn):
                            found[provider_name] = ProviderInfo(
                                provider=provider_name,
                                key=key,
                                source="file:keys.yaml",
                                api_base=defn.get("api_base", ""),
                                models=list(defn.get("models", [])),
                                cost_tier=defn.get("cost_tier", "medium"),
                                strength=defn.get("strength", "general"),
                            )
            except Exception as e:
                logger.warning(f"Failed to read {keys_file}: {e}")

        # 3. 掃描項目級 llm_config.yaml 的 api_key
        for config_path in self._candidate_config_paths():
            if config_path.exists():
                try:
                    with open(config_path, encoding="utf-8") as f:
                        config_data = yaml.safe_load(f) or {}
                    if isinstance(config_data, dict):
                        llm = config_data.get("llm", config_data)
                        api_key = str(llm.get("api_key", "") or "").strip()
                        api_base = str(llm.get("api_base", "") or "").strip()
                        default_model = str(llm.get("default_model", "") or "").strip()
                        if api_key:
                            provider = self._infer_provider(default_model, api_base)
                            if provider and provider not in found:
                                defn = self._find_provider_def(provider)
                                if defn:
                                    found[provider] = ProviderInfo(
                                        provider=provider,
                                        key=api_key,
                                        source=f"file:{config_path.name}",
                                        api_base=api_base or defn.get("api_base", ""),
                                        models=[default_model] if default_model else list(defn.get("models", [])),
                                        cost_tier=defn.get("cost_tier", "medium"),
                                        strength=defn.get("strength", "general"),
                                    )
                except Exception:
                    pass

        # 標記推薦選項
        result = sorted(found.values(), key=lambda p: _COST_TIER_ORDER.get(p.cost_tier, 99))
        if result:
            self._mark_recommendation(result)

        return result

    def discover_best(self) -> ProviderInfo | None:
        """返回最佳推薦 Provider，無可用時返回 None。"""
        providers = self.discover()
        if not providers:
            return None
        # 優先返回標記為推薦的
        for p in providers:
            if p.recommended:
                return p
        return providers[0]

    def save_key(self, provider: str, key: str) -> Path:
        """保存 API Key 到 ~/.opc/keys.yaml。"""
        keys_file = self.opc_home / "keys.yaml"
        self.opc_home.mkdir(parents=True, exist_ok=True)

        existing: dict[str, Any] = {}
        if keys_file.exists():
            try:
                with open(keys_file, encoding="utf-8") as f:
                    existing = yaml.safe_load(f) or {}
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}

        existing[provider] = key

        import tempfile
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=keys_file.parent,
                prefix=".keys.", suffix=".tmp", delete=False,
            ) as f:
                tmp_path = Path(f.name)
                yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, keys_file)
        except Exception:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)
            raise

        logger.info(f"Saved API key for {provider} to {keys_file}")
        return keys_file

    # --- 內部方法 ---

    def _validate_key_format(self, key: str, defn: dict[str, Any]) -> bool:
        """驗證 Key 格式是否合理（不實際調用 API）。"""
        prefix = defn.get("key_prefix", "")
        min_len = defn.get("min_length", 10)
        if len(key) < min_len:
            return False
        if prefix and not key.startswith(prefix):
            # 有些 provider 的 key 格式不固定，不強制前綴
            # 但對已知前綴的做基本檢查
            if defn["name"] in ("openai", "anthropic", "openrouter", "deepseek"):
                return False
        return True

    def _find_provider_def(self, name: str) -> dict[str, Any] | None:
        """查找 Provider 定義。"""
        for defn in _PROVIDER_DEFS:
            if defn["name"] == name:
                return defn
        return None

    def _infer_provider(self, model: str, api_base: str) -> str | None:
        """從模型名稱和 API Base 推斷 Provider。"""
        model_lower = model.lower()
        base_lower = api_base.lower()

        if "deepseek" in model_lower or "deepseek" in base_lower:
            return "deepseek"
        if "claude" in model_lower or "anthropic" in model_lower:
            return "anthropic"
        if "gpt" in model_lower or "o1" in model_lower or "o3" in model_lower:
            return "openai"
        if "gemini" in model_lower:
            return "google"
        if "mistral" in model_lower:
            return "mistral"
        if "groq" in base_lower:
            return "groq"
        if "together" in base_lower:
            return "together"
        if "doubao" in model_lower or "ark" in base_lower:
            return "volcengine"
        if "openrouter" in base_lower:
            return "openrouter"
        return None

    def _candidate_config_paths(self) -> list[Path]:
        """列舉可能的配置文件路徑。"""
        paths = []
        # 項目級
        from opc.core.config import get_opc_home
        opc_home = get_opc_home()
        paths.append(opc_home / "config" / "llm_config.yaml")
        # 全局級
        paths.append(Path.home() / ".opc" / "config" / "llm_config.yaml")
        return paths

    def _mark_recommendation(self, providers: list[ProviderInfo]) -> None:
        """標記推薦 Provider。

        策略：
        - 中文環境優先推薦 DeepSeek（便宜且中文強）
        - 否則推薦性價比最高的
        - 如果只有高級選項，推薦 Claude（推理能力強）
        """
        if not providers:
            return

        # 檢測是否為中文環境
        import locale
        is_chinese = False
        try:
            lang = locale.getdefaultlocale()[0] or ""
            is_chinese = "zh" in lang.lower()
        except Exception:
            pass

        # 優先推薦邏輯（國內模型優先）
        if is_chinese:
            for p in providers:
                if p.provider == "deepseek":
                    p.recommended = True
                    return
            for p in providers:
                if p.provider == "dashscope":
                    p.recommended = True
                    return

        # 性價比優先：便宜 > 中等 > 貴
        for p in providers:
            if p.cost_tier == "cheap":
                p.recommended = True
                return

        for p in providers:
            if p.cost_tier == "medium":
                p.recommended = True
                return

        if providers:
            providers[0].recommended = True


def format_discovery_result(providers: list[ProviderInfo]) -> str:
    """格式化發現結果為人類可讀文本（供 CLI 引導使用）。"""
    if not providers:
        return (
            "❌ 未找到任何可用的 API Key。\n"
            "請設置環境變數或運行 `opc config set llm.api_key` 配置。"
        )

    lines = ["✅ 發現以下可用的 AI Provider：\n"]
    for i, p in enumerate(providers, 1):
        rec = " ⭐ 推薦" if p.recommended else ""
        cost_label = {"cheap": "💰 超值", "medium": "💰💰 適中", "expensive": "💰💰💰 高級"}
        strength_label = {
            "chinese": "中文能力強",
            "code": "代碼能力強",
            "reasoning": "推理能力強",
            "general": "綜合能力強",
        }
        lines.append(
            f"  {i}. {p.provider}{rec}\n"
            f"     模型: {', '.join(p.models[:3])}\n"
            f"     {cost_label.get(p.cost_tier, '')} | {strength_label.get(p.strength, '')}\n"
            f"     來源: {p.source}"
        )
    return "\n".join(lines)
