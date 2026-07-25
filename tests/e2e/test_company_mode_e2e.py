"""E2E tests for Company Mode flow.

Tests the complete Company Mode pipeline:
  create org → dispatch work items → role execution → review → complete

Verifies work item state machine transitions and runtime session creation.
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
    DelegationWorkItem,
    SessionRecord,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization._company_work_item_helper import (
    CompanyRuntimeSpec,
    CompanyRuntimeSpecBuilder,
    CompanyRuntimeWorkItemHelper,
    deserialize_company_runtime_spec,
    serialize_company_runtime_spec,
)
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)


# TaskStatus uses PENDING, not TODO
TASK_STATUS_TODO = TaskStatus.PENDING


class DummyOrgEngine:
    """Minimal OrgEngine stub for testing."""

    def __init__(self):
        self.config = SimpleNamespace(org=SimpleNamespace(
            organization_id="test_org",
            organization_name="Test Organization",
            organization_config_file="",
        ))
        self._agents = {}

    def get_agent(self, role_id: str):
        return self._agents.get(role_id, SimpleNamespace(
            role_id=role_id,
            name=f"Agent {role_id}",
            responsibility="General responsibilities",
            can_spawn=[],
            reports_to="owner",
            runtime_policy={},
        ))

    def list_agents(self):
        return list(self._agents.values())

    def get_company_profile(self):
        return "corporate"

    def get_allowed_contact_roles(self, role_id: str):
        return list(self._agents.keys())

    def get_final_decider_role_id(self, strict: bool = True):
        return "ceo"


class CompanyModeE2ETests(unittest.IsolatedAsyncioTestCase):
    """E2E tests for Company Mode flow."""

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_company.db"
        self.store = OPCStore(self.db_path)
        await self.store.store.ensure_ready() if hasattr(self.store, 'store') else await self.store.ensure_ready()
        self.project_id = "test_company_project"
        self.org_engine = DummyOrgEngine()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_company_runtime_spec_serialization(self) -> None:
        """Test CompanyRuntimeSpec serialization/deserialization."""
        spec = CompanyRuntimeSpec(
            profile="corporate",
            original_request="Build a web application",
            runtime_model="multi_team_org",
            work_item_driven=True,
            staffing_overrides={"cto": "agent_001"},
            role_agent_overrides={"engineer": "qwen_code"},
            metadata={"source": "test"},
        )

        # Serialize
        data = serialize_company_runtime_spec(spec)
        self.assertEqual(data["profile"], "corporate")
        self.assertEqual(data["original_request"], "Build a web application")
        self.assertEqual(data["runtime_model"], "multi_team_org")
        self.assertTrue(data["work_item_driven"])

        # Deserialize
        restored = deserialize_company_runtime_spec(data)
        self.assertEqual(restored.profile, spec.profile)
        self.assertEqual(restored.original_request, spec.original_request)
        self.assertEqual(restored.runtime_model, spec.runtime_model)
        self.assertEqual(restored.staffing_overrides, spec.staffing_overrides)

    def test_company_runtime_spec_builder(self) -> None:
        """Test CompanyRuntimeSpecBuilder creates valid specs."""
        builder = CompanyRuntimeSpecBuilder(self.org_engine)

        # Create a mock RouterDecision
        decision = SimpleNamespace(
            company_profile="corporate",
            domains=["software", "web"],
            preferred_agent="native",
            sub_tasks=["task1", "task2"],
            org_id="test_org",
        )

        spec = builder.build_spec(decision, original_message="Build a web app")

        self.assertEqual(spec.profile, "corporate")
        self.assertEqual(spec.original_request, "Build a web app")
        self.assertEqual(spec.runtime_model, "multi_team_org")
        self.assertTrue(spec.work_item_driven)
        self.assertIn("source", spec.metadata)
        self.assertEqual(spec.metadata["source"], "work_item_runtime")

    def test_work_item_helper_dedupe_lines(self) -> None:
        """Test CompanyRuntimeWorkItemHelper._dedupe_lines."""
        helper = CompanyRuntimeWorkItemHelper(self.org_engine)

        lines = ["item1", "item2", "item1", "item3", "item2", ""]
        result = helper._dedupe_lines(lines)

        self.assertEqual(result, ["item1", "item2", "item3"])

    def test_work_item_helper_infer_turn_type(self) -> None:
        """Test work item turn type inference."""
        helper = CompanyRuntimeWorkItemHelper(self.org_engine)

        # Test setup projection
        setup_projection = WorkItemProjectionSpec(
            projection_id="workspace_bootstrap",
            turn_type="setup",
            role_id="cto",
            title="Workspace Bootstrap",
            summary="Set up the workspace",
            dependency_projection_ids=[],
        )
        turn_type = helper._infer_work_item_turn_type(setup_projection)
        self.assertEqual(turn_type, "setup")

        # Test review projection
        review_projection = WorkItemProjectionSpec(
            projection_id="code_review",
            turn_type="review",
            role_id="qa_lead",
            title="Code Review",
            summary="Review the implementation",
            dependency_projection_ids=["impl"],
        )
        turn_type = helper._infer_work_item_turn_type(review_projection)
        self.assertEqual(turn_type, "review")

        # Test execute projection
        exec_projection = WorkItemProjectionSpec(
            projection_id="backend_api",
            turn_type="execute",
            role_id="engineer",
            title="Backend API Implementation",
            summary="Implement the backend API",
            dependency_projection_ids=[],
        )
        turn_type = helper._infer_work_item_turn_type(exec_projection)
        self.assertEqual(turn_type, "execute")

    def test_work_item_helper_infer_deliverables(self) -> None:
        """Test work item deliverables inference."""
        helper = CompanyRuntimeWorkItemHelper(self.org_engine)

        # Test workspace bootstrap deliverables
        bootstrap_projection = WorkItemProjectionSpec(
            projection_id="workspace_bootstrap",
            turn_type="setup",
            role_id="cto",
            title="Workspace Bootstrap",
            summary="Set up the workspace",
            dependency_projection_ids=[],
        )
        deliverables = helper._infer_work_item_deliverables(bootstrap_projection)
        self.assertTrue(any("workspace" in d.lower() for d in deliverables))

        # Test review deliverables
        review_projection = WorkItemProjectionSpec(
            projection_id="qa_review",
            turn_type="review",
            role_id="qa",
            title="QA Review",
            summary="Review and validate",
            dependency_projection_ids=[],
        )
        deliverables = helper._infer_work_item_deliverables(review_projection)
        self.assertTrue(any("review" in d.lower() for d in deliverables))

    def test_work_item_helper_build_description(self) -> None:
        """Test work item description building."""
        helper = CompanyRuntimeWorkItemHelper(self.org_engine)

        assignment = {
            "global_intent_summary": "Build a web application",
            "your_responsibility": "Implement the backend API",
            "out_of_scope": ["Frontend development", "Database design"],
            "inputs": ["API specification", "Data models"],
            "deliverables": ["REST API endpoints", "API documentation"],
            "acceptance_criteria": ["All endpoints tested", "Documentation complete"],
        }

        description = helper._build_work_item_description(assignment)

        self.assertIn("## Global Intent Summary", description)
        self.assertIn("## Your Responsibility", description)
        self.assertIn("## Out of Scope", description)
        self.assertIn("## Inputs", description)
        self.assertIn("## Deliverables", description)
        self.assertIn("## Acceptance Criteria", description)
        self.assertIn("Build a web application", description)
        self.assertIn("Implement the backend API", description)

    def test_work_item_helper_lint_assignment(self) -> None:
        """Test work item assignment linting."""
        helper = CompanyRuntimeWorkItemHelper(self.org_engine)

        projection = WorkItemProjectionSpec(
            projection_id="test_item",
            turn_type="execute",
            role_id="engineer",
            title="Test Item",
            summary="Test work item",
            dependency_projection_ids=["upstream_item"],
        )

        # Valid assignment
        valid_assignment = {
            "your_responsibility": "Implement the feature",
            "out_of_scope": ["Other features"],
            "inputs": ["Upstream handoff from dependency"],
            "deliverables": ["Working feature"],
            "acceptance_criteria": ["Feature works correctly"],
        }
        issues = helper._lint_work_item_assignment(
            projection_spec=projection,
            assignment=valid_assignment,
        )
        self.assertEqual(len(issues), 0)

        # Invalid assignment (missing deliverables)
        invalid_assignment = {
            "your_responsibility": "Implement the feature",
            "out_of_scope": [],
            "inputs": ["Upstream handoff"],
            "deliverables": [],
            "acceptance_criteria": ["Feature works"],
        }
        issues = helper._lint_work_item_assignment(
            projection_spec=projection,
            assignment=invalid_assignment,
        )
        self.assertTrue(any("deliverables" in issue.lower() for issue in issues))

    async def test_work_item_state_machine_transitions(self) -> None:
        """Test work item state machine: PENDING → IN_PROGRESS → IN_REVIEW → DONE."""
        task_id = f"task_{uuid.uuid4().hex[:8]}"

        # Create work item task
        task = Task(
            id=task_id,
            project_id=self.project_id,
            title="Work Item Test",
            status=TASK_STATUS_TODO,
            metadata={
                "work_item_id": f"wi_{uuid.uuid4().hex[:8]}",
                "projection_id": "test_projection",
                "turn_type": "execute",
            },
        )
        await self.store.save_task(task)

        # Transition to IN_PROGRESS (RUNNING)
        task.status = TaskStatus.RUNNING
        await self.store.save_task(task)
        retrieved = await self.store.get_task(task_id)
        self.assertEqual(retrieved.status, TaskStatus.RUNNING)

        # Transition to IN_REVIEW (AWAITING_MANAGER_REVIEW)
        task.status = TaskStatus.AWAITING_MANAGER_REVIEW
        await self.store.save_task(task)
        retrieved = await self.store.get_task(task_id)
        self.assertEqual(retrieved.status, TaskStatus.AWAITING_MANAGER_REVIEW)

        # Transition to DONE
        task.status = TaskStatus.DONE
        task.result = TaskResult(status=TaskStatus.DONE, content="Work item completed")
        await self.store.save_task(task)
        retrieved = await self.store.get_task(task_id)
        self.assertEqual(retrieved.status, TaskStatus.DONE)

    async def test_runtime_session_creation_for_work_item(self) -> None:
        """Test that runtime sessions are created for work items."""
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        session_id = f"session_{uuid.uuid4().hex[:8]}"

        # Create work item task
        task = Task(
            id=task_id,
            project_id=self.project_id,
            title="Session Creation Test",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_id": f"wi_{uuid.uuid4().hex[:8]}",
                "projection_id": "test_projection",
            },
        )
        await self.store.save_task(task)

        # Create runtime session
        session = SessionRecord(
            session_id=session_id,
            project_id=self.project_id,
            title="Work Item Runtime Session",
            status="active",
            metadata={
                "task_id": task_id,
                "exec_mode": "company",
                "company_profile": "corporate",
            },
        )
        await self.store.save_session(session)

        # Verify session was created
        retrieved = await self.store.get_session(session_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.metadata.get("task_id"), task_id)
        self.assertEqual(retrieved.metadata.get("exec_mode"), "company")

    def test_coordination_spec_building(self) -> None:
        """Test coordination spec building for work items."""
        helper = CompanyRuntimeWorkItemHelper(self.org_engine)

        projection = WorkItemProjectionSpec(
            projection_id="backend_impl",
            turn_type="execute",
            role_id="engineer",
            title="Backend Implementation",
            summary="Implement the backend API",
            dependency_projection_ids=["workspace_bootstrap"],
        )

        assignment = {
            "your_responsibility": "Implement the backend API",
            "out_of_scope": ["Frontend"],
            "inputs": ["Workspace setup complete"],
            "deliverables": ["REST API"],
            "acceptance_criteria": ["API tested"],
        }

        runtime_policy = {
            "coordination": {
                "inference_mode": "llm_primary",
                "fallback_mode": "conservative",
            },
            "communication": {
                "default_mode": "dm",
            },
        }

        spec = helper._build_coordination_spec(
            projection_spec=projection,
            assignment=assignment,
            work_item_turn_type="execute",
            runtime_policy=runtime_policy,
        )

        self.assertEqual(spec.version, 1)
        self.assertIsNotNone(spec.role_profile)
        self.assertIsNotNone(spec.work_item_profile)
        self.assertEqual(spec.normalized_state, "planned")


if __name__ == "__main__":
    unittest.main()
