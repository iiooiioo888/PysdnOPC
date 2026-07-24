"""技能缺口分析與架構優先 staffing 策略測試。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from opc.layer2_organization.role_repository import RoleRepositoryManager, SkillRegistryEntry
from opc.layer2_organization.skill_gap_analyzer import (
    SkillGapReport,
    analyze_skill_gaps,
    is_structure_ready,
)


# ── Mock org_engine ──────────────────────────────────────────────────────────


@dataclass
class MockAgent:
    role_id: str = ""
    skill_refs: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)


@dataclass
class MockEmployee:
    skill_refs: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)


class MockOrgEngine:
    """模擬 org_engine 提供 list_agents / get_agent / list_employees。"""

    def __init__(
        self,
        agents: list[MockAgent] | None = None,
        employees: dict[str, list[MockEmployee]] | None = None,
    ) -> None:
        self._agents = agents or []
        self._employees = employees or {}

    def list_agents(self) -> list[MockAgent]:
        return list(self._agents)

    def get_agent(self, role_id: str) -> MockAgent | None:
        for agent in self._agents:
            if agent.role_id == role_id:
                return agent
        return None

    def list_employees(self, role_id: str = "") -> list[MockEmployee]:
        return list(self._employees.get(role_id, []))


# ── Tests ──────────────────────────────────────────────────────────────────


class TestIsStructureReady:
    """Tests for is_structure_ready()."""

    def test_no_engine(self) -> None:
        assert not is_structure_ready(None)

    def test_empty_agents(self) -> None:
        engine = MockOrgEngine(agents=[])
        assert not is_structure_ready(engine)

    def test_only_generalist(self) -> None:
        engine = MockOrgEngine(agents=[MockAgent(role_id="task_generalist")])
        assert not is_structure_ready(engine)

    def test_has_real_roles(self) -> None:
        engine = MockOrgEngine(agents=[
            MockAgent(role_id="devops_engineer"),
            MockAgent(role_id="content_specialist"),
        ])
        assert is_structure_ready(engine)

    def test_mixed_with_generalist(self) -> None:
        engine = MockOrgEngine(agents=[
            MockAgent(role_id="task_generalist"),
            MockAgent(role_id="devops_engineer"),
        ])
        assert is_structure_ready(engine)


class TestAnalyzeSkillGapsNoRequirements:
    """Tests when no specific skill requirements are given."""

    def test_no_requirements_all_satisfied(self) -> None:
        engine = MockOrgEngine(agents=[
            MockAgent(role_id="devops"),
            MockAgent(role_id="designer"),
        ])
        report = analyze_skill_gaps(engine)
        assert report.structure_ready
        assert report.all_satisfied
        assert not report.has_gaps
        assert set(report.satisfied_roles) == {"devops", "designer"}

    def test_no_structure(self) -> None:
        engine = MockOrgEngine(agents=[])
        report = analyze_skill_gaps(engine)
        assert not report.structure_ready


class TestAnalyzeSkillGapsWithRequirements:
    """Tests with specific skill requirements."""

    def test_all_skills_satisfied(self) -> None:
        engine = MockOrgEngine(
            agents=[MockAgent(role_id="devops", skill_refs=["ci", "docker"])],
        )
        report = analyze_skill_gaps(engine, {"devops": ["ci", "docker"]})
        assert report.structure_ready
        assert report.all_satisfied
        assert "devops" in report.satisfied_roles

    def test_skill_gap_detected(self) -> None:
        engine = MockOrgEngine(
            agents=[MockAgent(role_id="devops", skill_refs=["ci"])],
        )
        report = analyze_skill_gaps(engine, {"devops": ["ci", "kubernetes"]})
        assert report.structure_ready
        assert report.has_gaps
        assert "devops" in report.gap_roles
        assert "kubernetes" in report.missing_skills["devops"]

    def test_multiple_roles_mixed(self) -> None:
        engine = MockOrgEngine(agents=[
            MockAgent(role_id="devops", skill_refs=["ci", "docker"]),
            MockAgent(role_id="designer", skill_refs=["figma"]),
        ])
        report = analyze_skill_gaps(engine, {
            "devops": ["ci", "docker"],
            "designer": ["figma", "illustrator"],
        })
        assert "devops" in report.satisfied_roles
        assert "designer" in report.gap_roles
        assert "illustrator" in report.missing_skills["designer"]

    def test_role_without_requirements_satisfied(self) -> None:
        engine = MockOrgEngine(agents=[
            MockAgent(role_id="devops", skill_refs=["ci"]),
            MockAgent(role_id="qa"),
        ])
        report = analyze_skill_gaps(engine, {"devops": ["ci"]})
        assert "qa" in report.satisfied_roles

    def test_employee_skills_counted(self) -> None:
        engine = MockOrgEngine(
            agents=[MockAgent(role_id="devops")],
            employees={"devops": [MockEmployee(skill_refs=["kubernetes"])]},
        )
        report = analyze_skill_gaps(engine, {"devops": ["kubernetes"]})
        assert report.all_satisfied

    def test_domain_as_fuzzy_skill(self) -> None:
        engine = MockOrgEngine(
            agents=[MockAgent(role_id="devops", domains=["CI/CD"])],
        )
        report = analyze_skill_gaps(engine, {"devops": ["ci/cd"]}, skill_match_mode="fuzzy")
        assert report.all_satisfied


class TestFuzzyMatching:
    """Tests for fuzzy skill matching mode."""

    def test_fuzzy_partial_match(self) -> None:
        engine = MockOrgEngine(
            agents=[MockAgent(role_id="devops", skill_refs=["docker-compose"])]
        )
        report = analyze_skill_gaps(engine, {"devops": ["docker"]}, skill_match_mode="fuzzy")
        assert report.all_satisfied

    def test_strict_no_partial_match(self) -> None:
        engine = MockOrgEngine(
            agents=[MockAgent(role_id="devops", skill_refs=["docker-compose"])]
        )
        report = analyze_skill_gaps(engine, {"devops": ["docker"]}, skill_match_mode="strict")
        assert report.has_gaps
        assert "docker" in report.missing_skills["devops"]


class TestWithRepoManager:
    """Tests integrating RoleRepositoryManager."""

    def test_repo_skills_fill_gap(self, tmp_path: Path) -> None:
        engine = MockOrgEngine(agents=[MockAgent(role_id="devops", skill_refs=["ci"])])
        repo_mgr = RoleRepositoryManager(tmp_path)
        repo_mgr.add_skill("devops", SkillRegistryEntry(skill_id="kubernetes"))

        report = analyze_skill_gaps(
            engine,
            {"devops": ["ci", "kubernetes"]},
            repo_manager=repo_mgr,
        )
        assert report.all_satisfied

    def test_repo_skills_not_enough(self, tmp_path: Path) -> None:
        engine = MockOrgEngine(agents=[MockAgent(role_id="devops", skill_refs=["ci"])])
        repo_mgr = RoleRepositoryManager(tmp_path)
        repo_mgr.add_skill("devops", SkillRegistryEntry(skill_id="docker"))

        report = analyze_skill_gaps(
            engine,
            {"devops": ["ci", "kubernetes"]},
            repo_manager=repo_mgr,
        )
        assert report.has_gaps
        assert "kubernetes" in report.missing_skills["devops"]


class TestSkillGapReport:
    """Tests for SkillGapReport dataclass."""

    def test_all_satisfied_property(self) -> None:
        report = SkillGapReport(structure_ready=True, satisfied_roles=["a", "b"])
        assert report.all_satisfied

    def test_has_gaps_property(self) -> None:
        report = SkillGapReport(structure_ready=True, gap_roles=["a"])
        assert report.has_gaps
        assert not report.all_satisfied

    def test_not_ready(self) -> None:
        report = SkillGapReport(structure_ready=False)
        assert not report.all_satisfied
