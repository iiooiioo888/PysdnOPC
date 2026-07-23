from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from opc.core.config import LLMConfig
from opc.llm.provider import LLMProvider, _model_supports_temperature, _sanitize_call_params


class TestLLMProviderHasCredentials(unittest.TestCase):
    def test_configured_api_key_has_credentials(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="openai/gpt-4o", api_key="sk-real"))
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(provider.has_credentials())

    def test_api_key_env_resolves_to_credentials(self) -> None:
        with patch.dict(os.environ, {"MY_KEY": "sk-env"}, clear=True):
            provider = LLMProvider(LLMConfig(default_model="openai/gpt-4o", api_key_env="MY_KEY"))
            self.assertTrue(provider.has_credentials())

    def test_no_key_anywhere_has_no_credentials(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            provider = LLMProvider(LLMConfig(default_model="openai/gpt-4o", api_key=""))
            self.assertFalse(provider.has_credentials())

    def test_well_known_env_var_counts_as_credentials(self) -> None:
        """Users who export OPENAI_API_KEY without putting it in config are not downgraded."""
        provider = LLMProvider(LLMConfig(default_model="openai/gpt-4o", api_key=""))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env"}, clear=True):
            self.assertTrue(provider.has_credentials())


class TestModelSupportsTemperature(unittest.TestCase):
    def test_o1_does_not_support_temperature(self) -> None:
        self.assertFalse(_model_supports_temperature("openai/o1"))
        self.assertFalse(_model_supports_temperature("o1"))

    def test_o3_mini_does_not_support_temperature(self) -> None:
        self.assertFalse(_model_supports_temperature("openai/o3-mini"))

    def test_o4_mini_does_not_support_temperature(self) -> None:
        self.assertFalse(_model_supports_temperature("openai/o4-mini"))

    def test_gpt4o_supports_temperature(self) -> None:
        self.assertTrue(_model_supports_temperature("openai/gpt-4o"))

    def test_claude_supports_temperature(self) -> None:
        self.assertTrue(_model_supports_temperature("anthropic/claude-sonnet-4-20250514"))


class TestSanitizeCallParams(unittest.TestCase):
    def test_removes_temperature_for_o_series(self) -> None:
        with patch("opc.llm.provider.litellm.get_model_info", side_effect=Exception("not mapped")):
            temp, max_tok = _sanitize_call_params("openai/o3-mini", 0.7, 4096)
        self.assertIsNone(temp)
        self.assertEqual(max_tok, 4096)

    def test_keeps_temperature_for_normal_model(self) -> None:
        with patch("opc.llm.provider.litellm.get_model_info", side_effect=Exception("not mapped")):
            temp, max_tok = _sanitize_call_params("openai/gpt-4o", 0.7, 4096)
        self.assertEqual(temp, 0.7)
        self.assertEqual(max_tok, 4096)

    def test_clamps_max_tokens_when_exceeds_cap(self) -> None:
        with patch(
            "opc.llm.provider.litellm.get_model_info",
            return_value={"max_output_tokens": 8192, "max_tokens": 8192},
        ):
            temp, max_tok = _sanitize_call_params("openai/gpt-4o", 0.5, 32768)
        self.assertEqual(temp, 0.5)
        self.assertEqual(max_tok, 8192)

    def test_passes_through_when_within_cap(self) -> None:
        with patch(
            "opc.llm.provider.litellm.get_model_info",
            return_value={"max_output_tokens": 16384},
        ):
            temp, max_tok = _sanitize_call_params("openai/gpt-4o", 0.5, 8000)
        self.assertEqual(max_tok, 8000)


class TestChatStreamParameterValidation(unittest.TestCase):
    def test_temperature_removed_for_o_series_in_stream(self) -> None:
        """chat_stream should not send temperature for o-series models."""
        provider = LLMProvider(LLMConfig(
            default_model="openai/o3-mini",
            api_key="sk-test",
            temperature=0.7,
            max_tokens=4096,
        ))

        captured_kwargs: dict = {}

        async def fake_acompletion(**kwargs):
            captured_kwargs.update(kwargs)
            # Return a minimal non-streaming response (spec prevents __aiter__)
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
                events = []
                async def collect():
                    async for ev in provider.chat_stream([{"role": "user", "content": "hi"}]):
                        events.append(ev)
                asyncio.run(collect())

        self.assertNotIn("temperature", captured_kwargs)

    def test_temperature_included_for_normal_model_in_stream(self) -> None:
        """chat_stream should include temperature for models that support it."""
        provider = LLMProvider(LLMConfig(
            default_model="openai/gpt-4o",
            api_key="sk-test",
            temperature=0.7,
            max_tokens=4096,
        ))

        captured_kwargs: dict = {}

        async def fake_acompletion(**kwargs):
            captured_kwargs.update(kwargs)
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
                events = []
                async def collect():
                    async for ev in provider.chat_stream([{"role": "user", "content": "hi"}]):
                        events.append(ev)
                asyncio.run(collect())

        self.assertIn("temperature", captured_kwargs)
        self.assertEqual(captured_kwargs["temperature"], 0.7)


class TestBadRequestErrorAutoRetry(unittest.TestCase):
    def test_chat_retries_without_temperature_on_bad_request(self) -> None:
        """On BadRequestError, chat should retry with temperature removed."""
        import litellm

        provider = LLMProvider(LLMConfig(
            default_model="openai/gpt-4o",
            api_key="sk-test",
            temperature=0.7,
            max_tokens=4096,
        ))

        call_count = {"n": 0}

        async def fake_acompletion(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise litellm.BadRequestError(
                    message="temperature is not supported",
                    model="gpt-4o",
                    llm_provider="openai",
                )
            # Second call succeeds
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = "success"
            mock_resp.choices[0].message.tool_calls = None
            mock_resp.choices[0].finish_reason = "stop"
            mock_resp.usage = MagicMock()
            mock_resp.usage.prompt_tokens = 10
            mock_resp.usage.completion_tokens = 5
            return mock_resp

        with patch("opc.llm.provider.litellm.acompletion", side_effect=fake_acompletion):
            with patch("opc.llm.provider.litellm.get_model_info", side_effect=Exception("not mapped")):
                with patch("opc.llm.provider.litellm.completion_cost", return_value=0.0):
                    result = asyncio.run(provider.chat([{"role": "user", "content": "hi"}]))

        self.assertEqual(result["content"], "success")
        self.assertEqual(call_count["n"], 2)

    def test_chat_stream_retries_on_bad_request(self) -> None:
        """On BadRequestError, chat_stream should retry with corrected params."""
        import litellm

        provider = LLMProvider(LLMConfig(
            default_model="openai/gpt-4o",
            api_key="sk-test",
            temperature=0.7,
            max_tokens=4096,
        ))

        call_count = {"n": 0}

        async def fake_acompletion(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise litellm.BadRequestError(
                    message="max_tokens exceeds limit",
                    model="gpt-4o",
                    llm_provider="openai",
                )
            # Second call succeeds with non-streaming response
            # Use spec=[] so hasattr(mock, '__aiter__') is False
            mock_resp = MagicMock(spec=["choices", "usage"])
            mock_choice = MagicMock()
            mock_choice.message.content = "recovered"
            mock_choice.message.tool_calls = None
            mock_choice.finish_reason = "stop"
            mock_resp.choices = [mock_choice]
            mock_resp.usage = None
            return mock_resp

        with patch("opc.llm.provider.litellm.acompletion", side_effect=fake_acompletion):
            with patch("opc.llm.provider.litellm.get_model_info", side_effect=Exception("not mapped")):
                events = []
                async def collect():
                    async for ev in provider.chat_stream([{"role": "user", "content": "hi"}]):
                        events.append(ev)
                asyncio.run(collect())

        # Should have recovered: message_start + assistant_delta + message_stop
        event_types = [e.event_type for e in events]
        self.assertIn("assistant_delta", event_types)
        self.assertIn("message_stop", event_types)
        self.assertEqual(call_count["n"], 2)


class TestValidateModelConfig(unittest.TestCase):
    def test_valid_model_returns_ok(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="openai/gpt-4o", api_key="sk-test"))
        with patch("opc.llm.provider.litellm.get_model_info", return_value={"max_input_tokens": 128000}):
            result = provider.validate_model_config()
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "openai/gpt-4o")
        self.assertEqual(result["errors"], [])

    def test_unmapped_model_returns_warning(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="openai/custom-model", api_key="sk-test"))
        with patch("opc.llm.provider.litellm.get_model_info", side_effect=Exception("not mapped")):
            result = provider.validate_model_config()
        self.assertTrue(result["ok"])  # Not fatal, just a warning
        self.assertTrue(len(result["warnings"]) > 0)
        self.assertIn("not mapped in litellm", result["warnings"][0])

    def test_o_series_model_warns_about_temperature(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="openai/o3-mini", api_key="sk-test"))
        with patch("opc.llm.provider.litellm.get_model_info", side_effect=Exception("not mapped")):
            result = provider.validate_model_config()
        self.assertTrue(result["ok"])
        temp_warnings = [w for w in result["warnings"] if "temperature" in w]
        self.assertTrue(len(temp_warnings) > 0)

    def test_max_tokens_exceeds_cap_warns(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="openai/gpt-4o",
            api_key="sk-test",
            max_tokens=32768,
        ))
        with patch(
            "opc.llm.provider.litellm.get_model_info",
            return_value={"max_output_tokens": 16384, "max_input_tokens": 128000},
        ):
            result = provider.validate_model_config()
        self.assertTrue(result["ok"])
        cap_warnings = [w for w in result["warnings"] if "max_tokens" in w]
        self.assertTrue(len(cap_warnings) > 0)


class TestLLMProviderContextWindow(unittest.TestCase):
    def test_gpt_5_4_override_applies_on_official_openai_base(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="openai/gpt-5.4"))

        with patch("opc.llm.provider.litellm.get_model_info", return_value={"max_input_tokens": 128000}):
            self.assertEqual(provider.get_context_window(), 1_050_000)

    def test_gpt_5_4_override_does_not_apply_on_proxy_base(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="openai/gpt-5.4",
            api_base="https://openrouter.ai/api/v1",
        ))

        with patch("opc.llm.provider.litellm.get_model_info", return_value={"max_input_tokens": 128000}):
            self.assertEqual(provider.get_context_window(), 128000)

    def test_poe_claude_sonnet_4_5_model_uses_local_context_window(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="claude-sonnet-4.5",
            api_base="https://api.poe.com/v1",
        ))

        with patch("opc.llm.provider.litellm.get_model_info") as get_model_info:
            self.assertEqual(provider.get_context_window(), 64_000)
            get_model_info.assert_not_called()

    def test_poe_openai_compatible_legacy_prefix_uses_same_context_window(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="openai/claude-sonnet-4.5",
            api_base="https://api.poe.com/v1",
        ))

        with patch("opc.llm.provider.litellm.get_model_info") as get_model_info:
            self.assertEqual(provider.get_context_window(), 64_000)
            get_model_info.assert_not_called()

    def test_non_overridden_model_still_uses_litellm(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="openai/gpt-4o"))

        with patch("opc.llm.provider.litellm.get_model_info", return_value={"max_input_tokens": 128000}):
            self.assertEqual(provider.get_context_window(), 128000)

    def test_context_window_uses_max_input_tokens_not_output_cap(self) -> None:
        """deepseek-style entries: max_tokens is the OUTPUT cap (8192), the
        context window is max_input_tokens (1M). The window must not be 8192."""
        provider = LLMProvider(LLMConfig(default_model="deepseek/deepseek-v4-pro"))

        with patch(
            "opc.llm.provider.litellm.get_model_info",
            return_value={"max_input_tokens": 1_000_000, "max_tokens": 8192, "max_output_tokens": 8192},
        ):
            self.assertEqual(provider.get_context_window(), 1_000_000)

    def test_config_scalar_override_supplies_window_for_unmapped_model(self) -> None:
        """Unmapped proxy models (doubao/minimax/…) get a real window from config."""
        provider = LLMProvider(LLMConfig(
            default_model="openai/doubao-seed-2.0-pro",
            api_base="https://ark.cn-beijing.volces.com/api/coding/v3",
            context_window=256000,
        ))

        with patch("opc.llm.provider.litellm.get_model_info", return_value={}) as get_model_info:
            self.assertEqual(provider.get_context_window(), 256000)
            get_model_info.assert_not_called()

    def test_unmapped_model_without_override_falls_back_to_default(self) -> None:
        """No override + litellm can't map → 128000 fallback, not None."""
        provider = LLMProvider(LLMConfig(
            default_model="openai/doubao-seed-2.0-pro",
            api_base="https://ark.cn-beijing.volces.com/api/coding/v3",
        ))

        with patch("opc.llm.provider.litellm.get_model_info", return_value={}):
            self.assertEqual(provider.get_context_window(), 128000)

    def test_unmapped_model_litellm_error_falls_back_to_default(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="deepseek/deepseek-v4-pro"))

        with patch(
            "opc.llm.provider.litellm.get_model_info",
            side_effect=Exception("Model deepseek-v4-pro isn't mapped yet."),
        ):
            self.assertEqual(provider.get_context_window(), 128000)

    def test_config_per_model_override_takes_precedence(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="openai/doubao-seed-2.0-pro",
            context_window=200000,
            context_window_overrides={"doubao-seed-2.0-pro": 262144},
        ))

        with patch("opc.llm.provider.litellm.get_model_info", return_value={}):
            self.assertEqual(provider.get_context_window(), 262144)

    def test_config_override_wins_over_litellm_for_mapped_model(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="openai/gpt-4o", context_window=50000))

        with patch("opc.llm.provider.litellm.get_model_info", return_value={"max_input_tokens": 128000}) as get_model_info:
            self.assertEqual(provider.get_context_window(), 50000)
            get_model_info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
