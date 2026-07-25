"""E2E tests for Task Mode flow.

Tests the complete Task Mode pipeline:
  init → create task → execute → complete → verify transcript persistence

Uses mock LLM (returns fixed responses) to verify end-to-end data flow.
"""

from __future__ import annotations

import asyncio
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
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore


# TaskStatus uses PENDING, not TODO
TASK_STATUS_TODO = TaskStatus.PENDING


class MockLLM:
    """Mock LLM that returns fixed responses for testing."""

    def __init__(self, responses: list[str] | None = None):
        self.responses = responses or ["This is a mock response."]
        self.call_count = 0

    async def generate(self, messages: list[dict], **kwargs) -> str:
        response = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        return response

    async def __call__(self, messages: list[dict], **kwargs) -> str:
        return await self.generate(messages, **kwargs)


class TaskModeE2ETests(unittest.IsolatedAsyncioTestCase):
    """E2E tests for Task Mode flow."""

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_tasks.db"
        self.store = OPCStore(self.db_path)
        await self.store.ensure_ready()
        self.project_id = "test_project"
        self.task_id = f"task_{uuid.uuid4().hex[:8]}"

    async def asyncTearDown(self) -> None:
        await self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_task_creation_and_persistence(self) -> None:
        """Test that a task can be created and persisted."""
        task = Task(
            id=self.task_id,
            project_id=self.project_id,
            title="Test Task",
            description="A test task for E2E testing",
            status=TASK_STATUS_TODO,
        )
        await self.store.save_task(task)

        # Verify task was persisted
        retrieved = await self.store.get_task(self.task_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, self.task_id)
        self.assertEqual(retrieved.title, "Test Task")
        self.assertEqual(retrieved.status, TASK_STATUS_TODO)

    async def test_task_status_transitions(self) -> None:
        """Test task status transitions: PENDING → RUNNING → DONE."""
        task = Task(
            id=self.task_id,
            project_id=self.project_id,
            title="Status Transition Test",
            status=TASK_STATUS_TODO,
        )
        await self.store.save_task(task)

        # Transition to RUNNING
        task.status = TaskStatus.RUNNING
        await self.store.save_task(task)
        retrieved = await self.store.get_task(self.task_id)
        self.assertEqual(retrieved.status, TaskStatus.RUNNING)

        # Transition to DONE
        task.status = TaskStatus.DONE
        task.result = TaskResult(status=TaskStatus.DONE, content="Task completed successfully")
        await self.store.save_task(task)
        retrieved = await self.store.get_task(self.task_id)
        self.assertEqual(retrieved.status, TaskStatus.DONE)
        self.assertIsNotNone(retrieved.result)

    async def test_session_creation_for_task(self) -> None:
        """Test that a session can be created for a task."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        session = SessionRecord(
            session_id=session_id,
            project_id=self.project_id,
            title="Test Session",
            status="active",
            metadata={"task_id": self.task_id},
        )
        await self.store.save_session(session)

        # Verify session was persisted
        retrieved = await self.store.get_session(session_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.session_id, session_id)
        self.assertEqual(retrieved.metadata.get("task_id"), self.task_id)

    async def test_transcript_message_persistence(self) -> None:
        """Test that transcript messages are persisted correctly."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        session = SessionRecord(
            session_id=session_id,
            project_id=self.project_id,
            title="Transcript Test",
            status="active",
            metadata={"task_id": self.task_id},
        )
        await self.store.save_session(session)

        # Add user message
        user_msg = SessionMessageRecord(
            message_id=f"msg_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            role="user",
            metadata={"content": "Hello, this is a test message"},
        )
        await self.store.save_session_message(user_msg)

        # Add assistant message
        assistant_msg = SessionMessageRecord(
            message_id=f"msg_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            role="assistant",
            metadata={"content": "This is a mock response"},
        )
        await self.store.save_session_message(assistant_msg)

        # Verify messages were persisted
        messages = await self.store.list_session_messages(session_id)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].role, "user")
        self.assertEqual(messages[1].role, "assistant")

    async def test_transcript_parts_persistence(self) -> None:
        """Test that transcript message parts are persisted correctly."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        session = SessionRecord(
            session_id=session_id,
            project_id=self.project_id,
            title="Parts Test",
            status="active",
            metadata={"task_id": self.task_id},
        )
        await self.store.save_session(session)

        message_id = f"msg_{uuid.uuid4().hex[:8]}"
        msg = SessionMessageRecord(
            message_id=message_id,
            session_id=session_id,
            role="assistant",
            metadata={"content": "Response with parts"},
        )
        await self.store.save_session_message(msg)

        # Add text part
        text_part = SessionPartRecord(
            part_id=f"part_{uuid.uuid4().hex[:8]}",
            message_id=message_id,
            session_id=session_id,
            part_type="text",
            payload={"text": "This is the text content"},
        )
        await self.store.save_session_part(text_part)

        # Add tool_use part
        tool_part = SessionPartRecord(
            part_id=f"part_{uuid.uuid4().hex[:8]}",
            message_id=message_id,
            session_id=session_id,
            part_type="tool_use",
            payload={"name": "test_tool", "input": {"arg": "value"}},
        )
        await self.store.save_session_part(tool_part)

        # Verify parts were persisted
        parts = await self.store.list_session_parts(session_id)
        self.assertEqual(len(parts), 2)
        part_types = {p.part_type for p in parts}
        self.assertEqual(part_types, {"text", "tool_use"})

    async def test_get_session_transcript_returns_objects(self) -> None:
        """Test that get_session_transcript returns proper objects (not dicts)."""
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        session = SessionRecord(
            session_id=session_id,
            project_id=self.project_id,
            title="Transcript Objects Test",
            status="active",
            metadata={"task_id": self.task_id},
        )
        await self.store.save_session(session)

        message_id = f"msg_{uuid.uuid4().hex[:8]}"
        msg = SessionMessageRecord(
            message_id=message_id,
            session_id=session_id,
            role="user",
            metadata={"content": "Test message"},
        )
        await self.store.save_session_message(msg)

        part = SessionPartRecord(
            part_id=f"part_{uuid.uuid4().hex[:8]}",
            message_id=message_id,
            session_id=session_id,
            part_type="text",
            payload={"text": "Test content"},
        )
        await self.store.save_session_part(part)

        # Get transcript
        transcript = await self.store.get_session_transcript(session_id)
        self.assertEqual(len(transcript), 1)

        # Verify structure: each item is {"message": SessionMessageRecord, "parts": [SessionPartRecord]}
        item = transcript[0]
        self.assertIn("message", item)
        self.assertIn("parts", item)

        # Verify message is an object with attributes (not a dict)
        message = item["message"]
        self.assertIsInstance(message, SessionMessageRecord)
        self.assertEqual(message.role, "user")
        self.assertEqual(message.metadata.get("content"), "Test message")

        # Verify parts are objects
        parts = item["parts"]
        self.assertEqual(len(parts), 1)
        self.assertIsInstance(parts[0], SessionPartRecord)
        self.assertEqual(parts[0].part_type, "text")

    async def test_full_task_mode_flow(self) -> None:
        """Test complete Task Mode flow: create → execute → complete."""
        # 1. Create task
        task = Task(
            id=self.task_id,
            project_id=self.project_id,
            title="Full Flow Test",
            description="Testing the complete task mode flow",
            status=TASK_STATUS_TODO,
        )
        await self.store.save_task(task)

        # 2. Create session for the task
        session_id = f"session_{uuid.uuid4().hex[:8]}"
        session = SessionRecord(
            session_id=session_id,
            project_id=self.project_id,
            title="Full Flow Session",
            status="active",
            metadata={"task_id": self.task_id},
        )
        await self.store.save_session(session)

        # 3. Start task (transition to RUNNING)
        task.status = TaskStatus.RUNNING
        await self.store.save_task(task)

        # 4. Add user message (simulating user input)
        user_msg = SessionMessageRecord(
            message_id=f"msg_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            role="user",
            metadata={"content": "Please help me with this task"},
        )
        await self.store.save_session_message(user_msg)

        # 5. Add assistant response (simulating LLM output)
        assistant_msg = SessionMessageRecord(
            message_id=f"msg_{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            role="assistant",
            metadata={"content": "I'll help you with that. Here's my response."},
        )
        await self.store.save_session_message(assistant_msg)

        # 6. Complete task
        task.status = TaskStatus.DONE
        task.result = TaskResult(status=TaskStatus.DONE, content="Task completed successfully")
        await self.store.save_task(task)

        # 7. Verify final state
        final_task = await self.store.get_task(self.task_id)
        self.assertEqual(final_task.status, TaskStatus.DONE)

        transcript = await self.store.get_session_transcript(session_id)
        self.assertEqual(len(transcript), 2)
        self.assertEqual(transcript[0]["message"].role, "user")
        self.assertEqual(transcript[1]["message"].role, "assistant")


if __name__ == "__main__":
    unittest.main()
