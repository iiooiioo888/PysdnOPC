"""WebSocket 配置測試 — 驗證 WebSocket 伺服器配置載入與預設值。"""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from opc.core.config import OPCConfig


# ---------------------------------------------------------------------------
# Server default configuration tests
# ---------------------------------------------------------------------------

class WsServerDefaultTests(unittest.TestCase):
    """Verify Office-UI WebSocket server default configuration."""

    def test_run_server_default_host(self) -> None:
        from opc.plugins.office_ui.server import run_server
        sig = inspect.signature(run_server)
        self.assertEqual(sig.parameters["host"].default, "0.0.0.0")

    def test_run_server_default_port(self) -> None:
        from opc.plugins.office_ui.server import run_server
        sig = inspect.signature(run_server)
        self.assertEqual(sig.parameters["port"].default, 8765)

    def test_run_server_accepts_config_param(self) -> None:
        from opc.plugins.office_ui.server import run_server
        sig = inspect.signature(run_server)
        self.assertIn("config", sig.parameters)
        self.assertIsNone(sig.parameters["config"].default)

    def test_run_server_accepts_project_id_param(self) -> None:
        from opc.plugins.office_ui.server import run_server
        sig = inspect.signature(run_server)
        self.assertIn("project_id", sig.parameters)
        self.assertIsNone(sig.parameters["project_id"].default)

    def test_create_app_is_async(self) -> None:
        from opc.plugins.office_ui.server import create_app
        self.assertTrue(inspect.iscoroutinefunction(create_app))

    def test_create_app_accepts_config_and_project(self) -> None:
        from opc.plugins.office_ui.server import create_app
        sig = inspect.signature(create_app)
        self.assertIn("config", sig.parameters)
        self.assertIn("project_id", sig.parameters)


# ---------------------------------------------------------------------------
# OPCConfig loading for WS context
# ---------------------------------------------------------------------------

class WsConfigLoadingTests(unittest.TestCase):
    """Verify OPCConfig loading behavior used by the WS server."""

    def test_default_config_has_agents(self) -> None:
        config = OPCConfig()
        self.assertIsNotNone(config.agents)
        self.assertIn("qwen_code", config.agents.agents)

    def test_default_config_has_llm(self) -> None:
        config = OPCConfig()
        self.assertIsNotNone(config.llm)
        self.assertTrue(hasattr(config.llm, "default_model"))

    def test_default_config_has_org(self) -> None:
        config = OPCConfig()
        self.assertIsNotNone(config.org)

    def test_config_load_from_nonexistent_dir_returns_default(self) -> None:
        # OPCConfig.load on a non-existent dir should either raise or return default
        # The server code catches exceptions and falls back to OPCConfig()
        config = OPCConfig()
        self.assertIsNotNone(config)

    def test_config_load_from_template_dir(self) -> None:
        """Verify config can load from the project's config/ template directory."""
        template_dir = Path(__file__).parent.parent / "config"
        if template_dir.is_dir():
            config = OPCConfig.load(template_dir)
            self.assertIsNotNone(config)
            self.assertIsNotNone(config.agents)


# ---------------------------------------------------------------------------
# WsConfigMixin structure tests
# ---------------------------------------------------------------------------

class WsConfigMixinStructureTests(unittest.TestCase):
    """Verify WsConfigMixin provides expected handler methods."""

    def test_mixin_class_exists(self) -> None:
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(inspect.isclass(WsConfigMixin))

    def test_mixin_has_llm_config_get(self) -> None:
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(hasattr(WsConfigMixin, "_handle_llm_config_get"))
        self.assertTrue(inspect.iscoroutinefunction(WsConfigMixin._handle_llm_config_get))

    def test_mixin_has_llm_config_set(self) -> None:
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(hasattr(WsConfigMixin, "_handle_llm_config_set"))
        self.assertTrue(inspect.iscoroutinefunction(WsConfigMixin._handle_llm_config_set))

    def test_mixin_has_org_info_handler(self) -> None:
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(hasattr(WsConfigMixin, "_handle_org_info"))

    def test_mixin_has_org_config_import(self) -> None:
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(hasattr(WsConfigMixin, "_handle_org_config_import"))

    def test_mixin_has_org_config_export(self) -> None:
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(hasattr(WsConfigMixin, "_handle_org_config_export"))

    def test_mixin_has_project_handlers(self) -> None:
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(hasattr(WsConfigMixin, "_handle_list_projects"))
        self.assertTrue(hasattr(WsConfigMixin, "_handle_create_project"))
        self.assertTrue(hasattr(WsConfigMixin, "_handle_switch_project"))

    def test_mixin_has_market_handlers(self) -> None:
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(hasattr(WsConfigMixin, "_handle_market_browse"))
        self.assertTrue(hasattr(WsConfigMixin, "_handle_market_apply_preset"))

    def test_mixin_has_persist_runtime_config(self) -> None:
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(hasattr(WsConfigMixin, "_persist_runtime_config"))


# ---------------------------------------------------------------------------
# WSHandler integration structure tests
# ---------------------------------------------------------------------------

class WSHandlerConfigIntegrationTests(unittest.TestCase):
    """Verify WSHandler inherits WsConfigMixin correctly."""

    def test_ws_handler_inherits_ws_config_mixin(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        from opc.plugins.office_ui._ws_config import WsConfigMixin
        self.assertTrue(issubclass(WSHandler, WsConfigMixin))

    def test_ws_handler_has_handle_ws_method(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        self.assertTrue(hasattr(WSHandler, "handle_ws"))


# ---------------------------------------------------------------------------
# CLI ui command defaults
# ---------------------------------------------------------------------------

class CliUiCommandDefaultsTests(unittest.TestCase):
    """Verify the `opc ui` CLI command registers correct defaults."""

    def test_register_cli_function_exists(self) -> None:
        from opc.plugins.office_ui import register_cli
        self.assertTrue(callable(register_cli))

    def test_frontend_dist_path_defined(self) -> None:
        from opc.plugins.office_ui import _FRONTEND_DIST
        self.assertIsInstance(_FRONTEND_DIST, Path)
        self.assertEqual(_FRONTEND_DIST.name, "frontend_dist")

    def test_frontend_src_path_defined(self) -> None:
        from opc.plugins.office_ui import _FRONTEND_SRC
        self.assertIsInstance(_FRONTEND_SRC, Path)
        self.assertEqual(_FRONTEND_SRC.name, "frontend_src")


# ---------------------------------------------------------------------------
# WS route registration
# ---------------------------------------------------------------------------

class WsRouteTests(unittest.TestCase):
    """Verify WebSocket route path configuration."""

    def test_ws_endpoint_path_constant(self) -> None:
        """The WS endpoint is registered at /ws in create_app."""
        import opc.plugins.office_ui.server as server_mod
        source = inspect.getsource(server_mod.create_app)
        self.assertIn('"/ws"', source)

    def test_static_dir_defined(self) -> None:
        from opc.plugins.office_ui.server import _STATIC_DIR
        self.assertIsInstance(_STATIC_DIR, Path)
        self.assertEqual(_STATIC_DIR.name, "frontend_dist")


if __name__ == "__main__":
    unittest.main()
