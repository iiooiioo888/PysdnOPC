from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opc.core.config import OPCConfig, RoleConfig


class UpdateRoleHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_role_persists_tools(self) -> None:
        from opc.plugins.office_ui.ws_handler import WSHandler
        from opc.plugins.office_ui.services.models import ServiceResult

        cfg = OPCConfig()
        cfg.org.roles = [
            RoleConfig(
                id="student",
                name="Student",
                responsibility="Learn.",
                reports_to="owner",
                tools=["file_read"],
            )
        ]

        # Mock the org service to simulate the update_role behavior
        mock_org_service = AsyncMock()
        mock_org_service.update_role = AsyncMock(
            return_value=ServiceResult(
                {"role": {"id": "student"}, "action": "role_updated", "role_id": "student"}
            )
        )
        mock_services = MagicMock()
        mock_services.org = mock_org_service

        handler = WSHandler.__new__(WSHandler)
        handler.engine = SimpleNamespace(config=cfg, org_engine=MagicMock())
        handler._clients = set()
        handler._shutting_down = False
        handler._ws_is_open = lambda _ws: True
        handler._config_lock = AsyncMock()
        handler._config_lock.__aenter__ = AsyncMock(return_value=None)
        handler._config_lock.__aexit__ = AsyncMock(return_value=None)
        handler._broadcast_org_info = AsyncMock()
        handler._ensure_office_services = MagicMock(return_value=mock_services)
        handler._publish_service_result = AsyncMock()
        handler._send_ack = AsyncMock()

        ws = AsyncMock()
        await handler._handle_update_role(
            ws,
            {
                "role_id": "student",
                "tools": ["file_read", " ", "web_search", ""],
            },
        )

        # Verify the org service was called with the correct data
        mock_org_service.update_role.assert_awaited_once()
        call_args = mock_org_service.update_role.call_args
        self.assertEqual(call_args.args[0], "student")
        self.assertEqual(call_args.args[1]["tools"], ["file_read", " ", "web_search", ""])
        handler._send_ack.assert_awaited()
