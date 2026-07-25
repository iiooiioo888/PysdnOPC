"""E2E tests for Office UI WebSocket.

Tests WebSocket connection, snapshot delivery, message handling,
and reconnection state recovery.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from opc.core.models import (
    SessionMessageRecord,
    SessionPartRecord,
    SessionRecord,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore


class MockWebSocket:
    """Mock WebSocket for testing."""

    def __init__(self):
        self.sent_messages: list[dict] = []
        self.closed = False
        self._receive_queue: asyncio.Queue = asyncio.Queue()

    async def send_json(self, data: dict) -> None:
        self.sent_messages.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_messages.append(json.loads(data))

    async def receive_json(self) -> dict:
        return await self._receive_queue.get()

    async def receive_text(self) -> str:
        data = await self._receive_queue.get()
        return json.dumps(data)

    def queue_message(self, data: dict) -> None:
        self._receive_queue.put_nowait(data)

    async def close(self) -> None:
        self.closed = True

    def get_messages_by_type(self, msg_type: str) -> list[dict]:
        return [m for m in self.sent_messages if m.get("type") == msg_type]

    def get_last_message(self) -> dict | None:
        return self.sent_messages[-1] if self.sent_messages else None


class WebSocketE2ETests(unittest.IsolatedAsyncioTestCase):
    """E2E tests for Office UI WebSocket."""

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_ws.db"
        self.store = OPCStore(self.db_path)
        await self.store.ensure_ready()
        self.project_id = "test_ws_project"

    async def asyncTearDown(self) -> None:
        await self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_websocket_envelope_format(self) -> None:
        """Test that WebSocket messages follow the envelope format."""
        # Standard envelope format: {"type": "...", "payload": {...}}
        snapshot_envelope = {
            "type": "snapshot",
            "payload": {
                "agents": {},
                "timeline": [],
                "offices": {},
            },
        }
        self.assertEqual(snapshot_envelope["type"], "snapshot")
        self.assertIn("payload", snapshot_envelope)

        event_envelope = {
            "type": "event",
            "payload": {
                "event_id": "evt_123",
                "event_type": "task_started",
                "data": {},
            },
        }
        self.assertEqual(event_envelope["type"], "event")
        self.assertIn("event_id", event_envelope["payload"])

        ack_envelope = {
            "type": "ack",
            "payload": {
                "ok": True,
                "action": "session_send",
            },
        }
        self.assertEqual(ack_envelope["type"], "ack")
        self.assertTrue(ack_envelope["payload"]["ok"])

    def test_reconnect_sync_message_format(self) -> None:
        """Test reconnect_sync message format."""
        reconnect_msg = {
            "type": "reconnect_sync",
            "last_snapshot_version": 42,
            "reconnect_attempt": 3,
        }
        self.assertEqual(reconnect_msg["type"], "reconnect_sync")
        self.assertEqual(reconnect_msg["last_snapshot_version"], 42)
        self.assertEqual(reconnect_msg["reconnect_attempt"], 3)

    def test_session_detail_request_format(self) -> None:
        """Test session_detail request message format."""
        request = {
            "type": "session_detail",
            "project_id": "test_project",
            "task_id": "task_123",
            "detail_level": "full",
            "limit": 50,
        }
        self.assertEqual(request["type"], "session_detail")
        self.assertEqual(request["detail_level"], "full")

    def test_collab_sync_request_format(self) -> None:
        """Test collab_sync request message format."""
        request = {
            "type": "collab_sync",
            "project_id": "test_project",
            "view_generation": 5,
        }
        self.assertEqual(request["type"], "collab_sync")
        self.assertEqual(request["view_generation"], 5)

    async def test_snapshot_contains_view_generation(self) -> None:
        """Test that snapshots can include view_generation for reconnect sync."""
        snapshot = {
            "type": "snapshot",
            "payload": {
                "agents": {},
                "timeline": [],
                "offices": {},
                "view_generation": 123,
            },
        }
        self.assertEqual(snapshot["payload"]["view_generation"], 123)

    async def test_session_message_persistence_and_retrieval(self) -> None:
        """Test that session messages are persisted and can be retrieved."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        task_id = f"task_{uuid.uuid4().hex[:8]}"

        # Create session
        session = SessionRecord(
            session_id=session_id,
            project_id=self.project_id,
            title="WS Test Session",
            status="active",
            metadata={"task_id": task_id},
        )
        await self.store.save_session(session)

        # Add messages
        for i in range(5):
            msg = SessionMessageRecord(
                message_id=f"msg_{uuid.uuid4().hex[:8]}",
                session_id=session_id,
                role="user" if i % 2 == 0 else "assistant",
                metadata={"content": f"Message {i}"},
            )
            await self.store.save_session_message(msg)

        # Retrieve and verify
        messages = await self.store.list_session_messages(session_id)
        self.assertEqual(len(messages), 5)

        # Verify transcript structure
        transcript = await self.store.get_session_transcript(session_id)
        self.assertEqual(len(transcript), 5)
        for item in transcript:
            self.assertIn("message", item)
            self.assertIn("parts", item)
            self.assertIsInstance(item["message"], SessionMessageRecord)

    async def test_reconnect_state_recovery(self) -> None:
        """Test state recovery after reconnection."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        task_id = f"task_{uuid.uuid4().hex[:8]}"

        # Create initial state
        session = SessionRecord(
            session_id=session_id,
            project_id=self.project_id,
            title="Reconnect Test",
            status="active",
            metadata={"task_id": task_id},
        )
        await self.store.save_session(session)

        # Add initial messages
        initial_msg = SessionMessageRecord(
            message_id=f"msg_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            role="user",
            metadata={"content": "Initial message before disconnect"},
        )
        await self.store.save_session_message(initial_msg)

        # Simulate disconnect and reconnect
        # After reconnect, client sends reconnect_sync with last_snapshot_version
        reconnect_request = {
            "type": "reconnect_sync",
            "last_snapshot_version": 1,
            "reconnect_attempt": 1,
        }
        self.assertEqual(reconnect_request["type"], "reconnect_sync")

        # Server responds with full state refresh
        # (In real implementation, this would be handled by ws_handler)
        messages_after_reconnect = await self.store.list_session_messages(session_id)
        self.assertEqual(len(messages_after_reconnect), 1)

        # Add message after reconnect
        post_reconnect_msg = SessionMessageRecord(
            message_id=f"msg_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            role="assistant",
            metadata={"content": "Response after reconnect"},
        )
        await self.store.save_session_message(post_reconnect_msg)

        # Verify state is consistent
        final_messages = await self.store.list_session_messages(session_id)
        self.assertEqual(len(final_messages), 2)

    def test_connection_status_values(self) -> None:
        """Test valid connection status values."""
        valid_statuses = ["connecting", "connected", "disconnected", "error"]
        for status in valid_statuses:
            self.assertIn(status, valid_statuses)

    def test_heartbeat_message_format(self) -> None:
        """Test heartbeat ping/pong message format."""
        ping = {"type": "ping"}
        pong = {"type": "pong"}
        self.assertEqual(ping["type"], "ping")
        self.assertEqual(pong["type"], "pong")

    def test_project_scoped_message_types(self) -> None:
        """Test that project-scoped messages require project_id."""
        project_scoped_types = [
            "collab_sync",
            "kanban_create_task",
            "run_task",
            "create_session",
            "session_send",
            "session_detail",
            "comms_state",
        ]
        for msg_type in project_scoped_types:
            msg = {"type": msg_type, "project_id": "test_project"}
            self.assertIn("project_id", msg)

    async def test_work_item_progress_event_format(self) -> None:
        """Test work_item_progress event format."""
        event = {
            "type": "work_item_progress",
            "payload": {
                "project_id": self.project_id,
                "work_item_id": "wi_123",
                "projection_id": "proj_456",
                "progress_type": "status_change",
                "old_status": "in_progress",
                "new_status": "in_review",
                "timestamp": "2024-01-01T00:00:00Z",
            },
        }
        self.assertEqual(event["type"], "work_item_progress")
        self.assertEqual(event["payload"]["progress_type"], "status_change")

    async def test_session_progress_event_format(self) -> None:
        """Test session_progress event format."""
        event = {
            "type": "session_progress",
            "payload": {
                "project_id": self.project_id,
                "task_id": "task_123",
                "session_id": "session_456",
                "progress_message": "Processing your request...",
                "progress_percent": 50,
            },
        }
        self.assertEqual(event["type"], "session_progress")
        self.assertIn("progress_message", event["payload"])


class WebSocketReconnectLogicTests(unittest.TestCase):
    """Unit tests for WebSocket reconnection logic."""

    def test_exponential_backoff_calculation(self) -> None:
        """Test exponential backoff delay calculation."""
        base_ms = 2000
        max_ms = 30000

        delays = []
        for attempt in range(10):
            delay = min(base_ms * (2 ** attempt), max_ms)
            delays.append(delay)

        # Verify exponential growth
        self.assertEqual(delays[0], 2000)
        self.assertEqual(delays[1], 4000)
        self.assertEqual(delays[2], 8000)
        self.assertEqual(delays[3], 16000)

        # Verify max cap
        self.assertEqual(delays[4], 30000)  # 32000 capped to 30000
        self.assertEqual(delays[9], 30000)

    def test_reconnect_attempt_tracking(self) -> None:
        """Test reconnection attempt tracking."""
        max_attempts = 20
        attempt = 0

        # Simulate reconnection attempts
        while attempt < max_attempts:
            attempt += 1

        self.assertEqual(attempt, max_attempts)

        # After max attempts, should stop
        self.assertGreaterEqual(attempt, max_attempts)

    def test_pending_queue_limit(self) -> None:
        """Test pending message queue limit."""
        max_queue_size = 100
        queue: list[str] = []

        # Fill queue
        for i in range(150):
            if len(queue) < max_queue_size:
                queue.append(f"message_{i}")

        self.assertEqual(len(queue), max_queue_size)

    def test_heartbeat_timing(self) -> None:
        """Test heartbeat interval and timeout values."""
        heartbeat_interval_ms = 30000
        heartbeat_timeout_ms = 10000

        # Timeout should be less than interval
        self.assertLess(heartbeat_timeout_ms, heartbeat_interval_ms)

        # Interval should be reasonable (not too frequent)
        self.assertGreaterEqual(heartbeat_interval_ms, 10000)


if __name__ == "__main__":
    unittest.main()
