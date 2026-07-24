"""每日任務調度模組測試。

驗證：
1. 至少一個角色擁有可讀取的每日任務列表
2. 任務列表與 work-item 狀態機兼容
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from opc.core.models import DelegationWorkItem, Phase
from opc.layer2_organization.daily_task_schedule import (
    DAILY_TASK_TEMPLATES_FILENAME,
    DailyTaskTemplate,
    RoleDailyTaskList,
    get_all_roles_with_daily_tasks,
    get_role_daily_tasks,
    instantiate_daily_work_items,
    load_daily_task_config,
    validate_daily_task_phase_compat,
)
from opc.layer2_organization.phase import ALLOWED_TRANSITIONS, TODO_PHASES
from opc.layer2_organization.work_item_identity import CANONICAL_WORK_ITEM_TURN_TYPES


SAMPLE_CONFIG = textwrap.dedent("""\
    settings:
      default_initial_phase: queued
      default_turn_type: execute
      show_on_kanban: true

    role_daily_tasks:
      devops_engineer:
        - task_id: daily-ci-check
          title: Daily CI Check
          summary: Check CI pipelines
          turn_type: monitor
          priority: high
          tags: [ci, daily]
        - task_id: daily-infra-review
          title: Infra Review
          summary: Review infrastructure
          turn_type: execute
          priority: medium
          tags: [infra, daily]
      designer:
        - task_id: daily-design-review
          title: Design Review
          summary: Review UI changes
          turn_type: review
          priority: medium
          tags: [design, daily]
""")


@pytest.fixture()
def tmp_opc_home(tmp_path: Path) -> Path:
    """Create a temporary OPC home with config directory."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / DAILY_TASK_TEMPLATES_FILENAME).write_text(SAMPLE_CONFIG, encoding="utf-8")
    return tmp_path


class TestLoadDailyTaskConfig:
    """Tests for load_daily_task_config."""

    def test_load_valid_config(self, tmp_opc_home: Path) -> None:
        settings, role_map = load_daily_task_config(tmp_opc_home)
        assert settings.default_initial_phase == "queued"
        assert settings.default_turn_type == "execute"
        assert settings.show_on_kanban is True
        assert "devops_engineer" in role_map
        assert "designer" in role_map

    def test_load_missing_config(self, tmp_path: Path) -> None:
        settings, role_map = load_daily_task_config(tmp_path)
        assert role_map == {}
        assert settings.default_initial_phase == "queued"

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True)
        (config_dir / DAILY_TASK_TEMPLATES_FILENAME).write_text("{{invalid", encoding="utf-8")
        settings, role_map = load_daily_task_config(tmp_path)
        assert role_map == {}


class TestGetRoleDailyTasks:
    """Tests for get_role_daily_tasks."""

    def test_existing_role(self, tmp_opc_home: Path) -> None:
        tasks = get_role_daily_tasks("devops_engineer", opc_home=tmp_opc_home)
        assert tasks is not None
        assert tasks.role_id == "devops_engineer"
        assert tasks.task_count == 2

    def test_nonexistent_role(self, tmp_opc_home: Path) -> None:
        tasks = get_role_daily_tasks("nonexistent", opc_home=tmp_opc_home)
        assert tasks is None

    def test_task_lookup_by_id(self, tmp_opc_home: Path) -> None:
        tasks = get_role_daily_tasks("devops_engineer", opc_home=tmp_opc_home)
        assert tasks is not None
        task = tasks.get_task("daily-ci-check")
        assert task is not None
        assert task.title == "Daily CI Check"
        assert task.turn_type == "monitor"
        assert task.priority == "high"


class TestGetAllRolesWithDailyTasks:
    """Tests for get_all_roles_with_daily_tasks."""

    def test_returns_sorted_roles(self, tmp_opc_home: Path) -> None:
        roles = get_all_roles_with_daily_tasks(opc_home=tmp_opc_home)
        assert roles == ["designer", "devops_engineer"]


class TestDailyTaskTemplate:
    """Tests for DailyTaskTemplate normalization."""

    def test_turn_type_normalization(self) -> None:
        template = DailyTaskTemplate(task_id="t1", title="Test", turn_type="follow-up")
        assert template.turn_type == "followup"

    def test_invalid_turn_type_fallback(self) -> None:
        template = DailyTaskTemplate(task_id="t1", title="Test", turn_type="invalid_type")
        assert template.turn_type == "execute"

    def test_all_turn_types_canonical(self) -> None:
        for turn_type in CANONICAL_WORK_ITEM_TURN_TYPES:
            template = DailyTaskTemplate(task_id="t1", title="Test", turn_type=turn_type)
            assert template.turn_type == turn_type


class TestPhaseCompatibility:
    """Validation: task list is compatible with work-item state machine."""

    def test_queued_phase_is_valid(self) -> None:
        assert validate_daily_task_phase_compat("queued") is True
        assert validate_daily_task_phase_compat(Phase.QUEUED) is True

    def test_all_todo_phases_valid(self) -> None:
        for phase in TODO_PHASES:
            assert validate_daily_task_phase_compat(phase) is True

    def test_all_phases_in_transition_table(self) -> None:
        for phase in Phase:
            assert validate_daily_task_phase_compat(phase) is True

    def test_invalid_phase_rejected(self) -> None:
        assert validate_daily_task_phase_compat("nonexistent_phase") is False

    def test_instantiated_items_have_valid_phase(self, tmp_opc_home: Path) -> None:
        """Every instantiated work item must have a phase in ALLOWED_TRANSITIONS."""
        items = instantiate_daily_work_items("devops_engineer", opc_home=tmp_opc_home)
        assert len(items) > 0
        for item in items:
            assert item.phase in ALLOWED_TRANSITIONS


class TestInstantiateDailyWorkItems:
    """Tests for instantiate_daily_work_items."""

    def test_instantiate_for_existing_role(self, tmp_opc_home: Path) -> None:
        items = instantiate_daily_work_items(
            "devops_engineer",
            run_id="run-1",
            team_id="team-1",
            seat_id="seat-1",
            manager_role_id="manager-1",
            batch_id="batch-daily",
            opc_home=tmp_opc_home,
        )
        assert len(items) == 2
        assert all(isinstance(item, DelegationWorkItem) for item in items)
        assert all(item.phase == Phase.QUEUED for item in items)
        assert all(item.role_id == "devops_engineer" for item in items)
        assert all(item.run_id == "run-1" for item in items)
        assert all(item.metadata.get("daily_task") is True for item in items)
        assert all(item.metadata.get("source") == "daily_task_schedule" for item in items)

    def test_instantiate_for_nonexistent_role(self, tmp_opc_home: Path) -> None:
        items = instantiate_daily_work_items("nonexistent", opc_home=tmp_opc_home)
        assert items == []

    def test_instantiate_custom_phase(self, tmp_opc_home: Path) -> None:
        items = instantiate_daily_work_items(
            "designer",
            initial_phase=Phase.READY,
            opc_home=tmp_opc_home,
        )
        assert len(items) == 1
        assert items[0].phase == Phase.READY

    def test_instantiate_invalid_phase_fallback(self, tmp_opc_home: Path) -> None:
        items = instantiate_daily_work_items(
            "designer",
            initial_phase="bogus_phase",
            opc_home=tmp_opc_home,
        )
        assert len(items) == 1
        assert items[0].phase == Phase.QUEUED

    def test_metadata_contains_turn_type(self, tmp_opc_home: Path) -> None:
        items = instantiate_daily_work_items("devops_engineer", opc_home=tmp_opc_home)
        ci_item = next(i for i in items if i.metadata.get("daily_task_id") == "daily-ci-check")
        assert ci_item.metadata["work_kind"] == "monitor"
        assert ci_item.metadata["work_item_turn_type"] == "monitor"
        assert ci_item.kind == "monitor"

    def test_batch_index_sequential(self, tmp_opc_home: Path) -> None:
        items = instantiate_daily_work_items("devops_engineer", opc_home=tmp_opc_home)
        assert [item.batch_index for item in items] == [0, 1]

    def test_kanban_hidden_when_configured(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True)
        config_content = textwrap.dedent("""\
            settings:
              show_on_kanban: false
            role_daily_tasks:
              tester:
                - task_id: t1
                  title: Test Task
        """)
        (config_dir / DAILY_TASK_TEMPLATES_FILENAME).write_text(config_content, encoding="utf-8")
        items = instantiate_daily_work_items("tester", opc_home=tmp_path)
        assert len(items) == 1
        assert items[0].metadata.get("hidden_from_company_kanban") is True


class TestValidationCriteria:
    """Acceptance validation criteria from the issue."""

    def test_at_least_one_role_has_readable_daily_task_list(self, tmp_opc_home: Path) -> None:
        """Validation: 確認至少一個角色擁有可讀取的每日任務列表。"""
        roles = get_all_roles_with_daily_tasks(opc_home=tmp_opc_home)
        assert len(roles) >= 1
        # Verify the first role's task list is readable
        first_role = roles[0]
        task_list = get_role_daily_tasks(first_role, opc_home=tmp_opc_home)
        assert task_list is not None
        assert task_list.task_count >= 1
        for task in task_list.tasks:
            assert task.task_id
            assert task.title
            assert task.turn_type in CANONICAL_WORK_ITEM_TURN_TYPES

    def test_task_list_compatible_with_state_machine(self, tmp_opc_home: Path) -> None:
        """Validation: 確認任務列表與 work-item 狀態機兼容。"""
        roles = get_all_roles_with_daily_tasks(opc_home=tmp_opc_home)
        for role_id in roles:
            items = instantiate_daily_work_items(role_id, opc_home=tmp_opc_home)
            for item in items:
                # Phase must be in the transition table
                assert item.phase in ALLOWED_TRANSITIONS
                # Phase must be a valid Phase enum
                assert isinstance(item.phase, Phase)
                # kind must be canonical
                assert item.kind in CANONICAL_WORK_ITEM_TURN_TYPES


class TestProjectConfigIntegration:
    """Test with the actual project config if available."""

    def test_project_config_loads(self) -> None:
        """The shipped .opc/config/daily_task_templates.yaml should load."""
        project_opc_home = Path(__file__).resolve().parent.parent / ".opc"
        config_path = project_opc_home / "config" / DAILY_TASK_TEMPLATES_FILENAME
        if not config_path.exists():
            pytest.skip("project config not available")
        settings, role_map = load_daily_task_config(project_opc_home)
        assert len(role_map) >= 1
        # At least one role has tasks
        for role_id, task_list in role_map.items():
            assert task_list.task_count >= 1
            for task in task_list.tasks:
                assert task.turn_type in CANONICAL_WORK_ITEM_TURN_TYPES
