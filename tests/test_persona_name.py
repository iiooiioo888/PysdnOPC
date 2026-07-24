"""persona_name 人格化名字測試。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from opc.core.config import EmployeeConfig
from opc.core.models import DelegationWorkItem, Phase
from opc.layer2_organization.daily_task_schedule import (
    DAILY_TASK_TEMPLATES_FILENAME,
    display_name_for_employee,
    instantiate_daily_work_items,
)


SAMPLE_CONFIG = textwrap.dedent("""\
    settings:
      default_initial_phase: queued
    role_daily_tasks:
      devops_engineer:
        - task_id: daily-ci-check
          title: Daily CI Check
          summary: Check CI
          turn_type: monitor
""")


@pytest.fixture()
def tmp_opc_home(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / DAILY_TASK_TEMPLATES_FILENAME).write_text(SAMPLE_CONFIG, encoding="utf-8")
    return tmp_path


class TestEmployeeConfigPersonaName:
    """Tests for persona_name field on EmployeeConfig."""

    def test_persona_name_default_empty(self) -> None:
        emp = EmployeeConfig(employee_id="e1", name="DevOps Automator", role_id="devops")
        assert emp.persona_name == ""

    def test_persona_name_set(self) -> None:
        emp = EmployeeConfig(
            employee_id="e1",
            name="DevOps Automator",
            persona_name="小明",
            role_id="devops",
        )
        assert emp.persona_name == "小明"

    def test_serialization_roundtrip(self) -> None:
        emp = EmployeeConfig(
            employee_id="e1",
            name="Technical Writer",
            persona_name="Alice",
            role_id="content",
        )
        data = emp.model_dump()
        assert data["persona_name"] == "Alice"
        restored = EmployeeConfig.model_validate(data)
        assert restored.persona_name == "Alice"

    def test_missing_persona_name_in_payload(self) -> None:
        data = {"employee_id": "e2", "name": "Designer", "role_id": "design"}
        emp = EmployeeConfig.model_validate(data)
        assert emp.persona_name == ""


class TestDisplayNameForEmployee:
    """Tests for display_name_for_employee helper."""

    def test_prefers_persona_name(self) -> None:
        emp = EmployeeConfig(
            employee_id="e1",
            name="DevOps Automator",
            persona_name="Kenji",
            role_id="devops",
        )
        assert display_name_for_employee(emp) == "Kenji"

    def test_fallback_to_name(self) -> None:
        emp = EmployeeConfig(employee_id="e1", name="DevOps Automator", role_id="devops")
        assert display_name_for_employee(emp) == "DevOps Automator"

    def test_empty_persona_falls_back(self) -> None:
        emp = EmployeeConfig(
            employee_id="e1",
            name="Designer",
            persona_name="  ",
            role_id="design",
        )
        assert display_name_for_employee(emp) == "Designer"


class TestDailyTaskPersonaNameInjection:
    """Tests for persona_name injection into work item metadata."""

    def test_persona_name_in_metadata(self, tmp_opc_home: Path) -> None:
        items = instantiate_daily_work_items(
            "devops_engineer",
            persona_name="小明",
            opc_home=tmp_opc_home,
        )
        assert len(items) == 1
        assert items[0].metadata.get("persona_name") == "小明"

    def test_no_persona_name_when_empty(self, tmp_opc_home: Path) -> None:
        items = instantiate_daily_work_items(
            "devops_engineer",
            opc_home=tmp_opc_home,
        )
        assert len(items) == 1
        assert "persona_name" not in items[0].metadata
