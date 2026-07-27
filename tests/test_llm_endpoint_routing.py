"""具名端點路由測試 — 依模型名稱將調用路由到不同 API 端點（多供應商混用）。"""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import MagicMock, patch

from opc.core.config import LLMConfig, LLMEndpointConfig
from opc.llm.provider import LLMProvider


def _qwen_mixed_config(**overrides) -> LLMConfig:
    """MiMo 全域端點 + 千問 Token Plan 具名端點的混用配置。"""
    params = dict(
        default_model="openai/mimo-v2.5-pro",
        api_base="https://token-plan-sgp.xiaomimimo.com/v1",
        api_key="sk-mimo",
        endpoints={
            "qwen_token_plan": LLMEndpointConfig(
                api_base="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                api_key="sk-qwen",
                models=[
                    "qwen3.8-max-preview",
                    "qwen3.7-plus",
                    "qwen3.7-max",
                    "qwen3.6-flash",
                    "glm-5.2",
                    "deepseek-v4-pro",
                ],
            ),
        },
    )
    params.update(overrides)
    return LLMConfig(**params)


class TestEndpointForModel(unittest.TestCase):
    def test_qwen_model_resolves_to_qwen_endpoint(self) -> None:
        provider = LLMProvider(_qwen_mixed_config())
        api_base, api_key = provider._endpoint_for_model("openai/qwen3.7-plus")
        self.assertEqual(
            api_base,
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(api_key, "sk-qwen")

    def test_mimo_model_falls_back_to_global_endpoint(self) -> None:
        provider = LLMProvider(_qwen_mixed_config())
        api_base, api_key = provider._endpoint_for_model("openai/mimo-v2.5-pro")
        self.assertEqual(api_base, "https://token-plan-sgp.xiaomimimo.com/v1")
        self.assertEqual(api_key, "sk-mimo")

    def test_prefix_match_covers_dated_model_variants(self) -> None:
        provider = LLMProvider(_qwen_mixed_config())
        api_base, _ = provider._endpoint_for_model("openai/qwen3.6-flash-2026-07-01")
        self.assertEqual(
            api_base,
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        )

    def test_match_is_provider_prefix_agnostic(self) -> None:
        """deepseek/deepseek-v4-pro 與 openai/deepseek-v4-pro 都命中千問端點。"""
        provider = LLMProvider(_qwen_mixed_config())
        for model in ("deepseek/deepseek-v4-pro", "openai/deepseek-v4-pro"):
            api_base, _ = provider._endpoint_for_model(model)
            self.assertEqual(
                api_base,
                "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                model,
            )

    def test_endpoint_api_key_env_resolution(self) -> None:
        config = _qwen_mixed_config()
        config.endpoints["qwen_token_plan"].api_key = ""
        config.endpoints["qwen_token_plan"].api_key_env = "DASHSCOPE_API_KEY"
        provider = LLMProvider(config)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-env-qwen"}):
            _, api_key = provider._endpoint_for_model("openai/qwen3.7-max")
        self.assertEqual(api_key, "sk-env-qwen")

    def test_no_endpoints_uses_global(self) -> None:
        provider = LLMProvider(_qwen_mixed_config(endpoints={}))
        api_base, api_key = provider._endpoint_for_model("openai/qwen3.7-plus")
        self.assertEqual(api_base, "https://token-plan-sgp.xiaomimimo.com/v1")
        self.assertEqual(api_key, "sk-mimo")


class TestHasCredentialsWithEndpoints(unittest.TestCase):
    def test_endpoint_env_key_counts_as_credentials(self) -> None:
        config = LLMConfig(
            default_model="openai/qwen3.7-plus",
            endpoints={
                "qwen_token_plan": LLMEndpointConfig(
                    api_base="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                    api_key_env="MY_QWEN_PLAN_KEY",
                    models=["qwen3.7-plus"],
                ),
            },
        )
        with patch.dict(os.environ, {"MY_QWEN_PLAN_KEY": "sk-x"}, clear=True):
            provider = LLMProvider(config)
            self.assertTrue(provider.has_credentials())

    def test_endpoint_direct_key_counts_as_credentials(self) -> None:
        config = LLMConfig(
            default_model="openai/qwen3.7-plus",
            endpoints={
                "qwen_token_plan": LLMEndpointConfig(api_key="sk-direct", models=["qwen3.7-plus"]),
            },
        )
        with patch.dict(os.environ, {}, clear=True):
            provider = LLMProvider(config)
            self.assertTrue(provider.has_credentials())


class TestChatUsesEndpointCredentials(unittest.TestCase):
    def _run_chat(self, config: LLMConfig, task_type: str | None = None) -> dict:
        provider = LLMProvider(config)
        captured: dict = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = "ok"
            mock_resp.choices[0].message.tool_calls = None
            mock_resp.choices[0].finish_reason = "stop"
            mock_resp.usage = None
            return mock_resp

        with patch("opc.llm.provider.litellm.acompletion", side_effect=fake_acompletion):
            with patch("opc.llm.provider.litellm.get_model_info", side_effect=Exception("not mapped")):
                asyncio.run(provider.chat(
                    [{"role": "user", "content": "hi"}],
                    task_type=task_type,
                    use_cache=False,
                ))
        return captured

    def test_routing_task_type_sends_qwen_endpoint(self) -> None:
        config = _qwen_mixed_config(routing={"quick_tasks": "openai/qwen3.7-plus"})
        captured = self._run_chat(config, task_type="quick_tasks")
        self.assertEqual(captured["model"], "openai/qwen3.7-plus")
        self.assertEqual(
            captured["api_base"],
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(captured["api_key"], "sk-qwen")

    def test_default_model_sends_global_endpoint(self) -> None:
        captured = self._run_chat(_qwen_mixed_config())
        self.assertEqual(captured["model"], "openai/mimo-v2.5-pro")
        self.assertEqual(captured["api_base"], "https://token-plan-sgp.xiaomimimo.com/v1")
        self.assertEqual(captured["api_key"], "sk-mimo")


class TestChatStreamUsesEndpointCredentials(unittest.TestCase):
    def test_stream_routing_sends_qwen_endpoint(self) -> None:
        config = _qwen_mixed_config(routing={"simple_qa": "openai/qwen3.6-flash"})
        provider = LLMProvider(config)
        captured: dict = {}

        async def fake_acompletion(**kwargs):
            captured.update(kwargs)
            mock_resp = MagicMock(spec=["choices", "usage"])
            mock_choice = MagicMock()
            mock_choice.message.content = "hello"
            mock_choice.message.tool_calls = None
            mock_choice.finish_reason = "stop"
            mock_resp.choices = [mock_choice]
            mock_resp.usage = None
            return mock_resp

        with patch("opc.llm.provider.litellm.acompletion", side_effect=fake_acompletion):
            with patch("opc.llm.provider.litellm.get_model_info", side_effect=Exception("not mapped")):
                async def collect():
                    async for _ in provider.chat_stream(
                        [{"role": "user", "content": "hi"}],
                        task_type="simple_qa",
                    ):
                        pass
                asyncio.run(collect())

        self.assertEqual(captured["model"], "openai/qwen3.6-flash")
        self.assertEqual(
            captured["api_base"],
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(captured["api_key"], "sk-qwen")


if __name__ == "__main__":
    unittest.main()
