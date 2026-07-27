"""Tests for opc.layer5_memory.ecc_bridge — ECC skill/agent/rules bridge."""

from __future__ import annotations

import textwrap
import unittest
from pathlib import Path

import pytest
import yaml

from opc.layer5_memory.ecc_bridge import (
    EccAgentBridge,
    EccAgentImportResult,
    EccAgentInfo,
    EccBridgeError,
    EccImportResult,
    EccRuleImportResult,
    EccRuleInfo,
    EccRulesBridge,
    EccSkillBridge,
    EccSkillInfo,
    _render_skill_document,
)
from opc.layer5_memory.skill_library import SkillLibrary


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ecc_skill(skills_dir: Path, name: str, description: str = "", extra_fm: dict | None = None, body: str = "") -> Path:
    """Create a fake ECC skill directory with SKILL.md."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm: dict = {"name": name, "description": description or f"ECC skill: {name}"}
    if extra_fm:
        fm.update(extra_fm)
    if not body:
        body = f"# {name}\n\nWorkflow guidance for {name}.\n"
    content = f"---\n{yaml.dump(fm, default_flow_style=False)}---\n\n{body}"
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    return skill_dir


@pytest.fixture()
def ecc_repo(tmp_path: Path) -> Path:
    """Create a mock ECC repository structure."""
    repo = tmp_path / "ecc-repo"
    skills_dir = repo / "skills"
    skills_dir.mkdir(parents=True)

    _make_ecc_skill(skills_dir, "python-patterns", "Pythonic idioms and best practices")
    _make_ecc_skill(skills_dir, "python-testing", "Python testing with pytest")
    _make_ecc_skill(skills_dir, "tdd-workflow", "Test-driven development workflow")
    _make_ecc_skill(
        skills_dir,
        "security-review",
        "Security checklist and review",
        extra_fm={"argument-hint": "<path>", "metadata": {"origin": "ECC"}},
    )
    _make_ecc_skill(skills_dir, "docker-patterns", "Docker Compose and networking")

    return repo


@pytest.fixture()
def opc_home(tmp_path: Path) -> Path:
    """Create a mock OPC home directory."""
    home = tmp_path / "opc-home"
    home.mkdir(parents=True)
    (home / "skills").mkdir()
    return home


@pytest.fixture()
def bridge(opc_home: Path, ecc_repo: Path) -> EccSkillBridge:
    """Create an EccSkillBridge with local source."""
    return EccSkillBridge(opc_home, ecc_repo_path=ecc_repo)


# ---------------------------------------------------------------------------
# normalize_skill_name
# ---------------------------------------------------------------------------


class TestNormalizeSkillName:
    def test_basic(self):
        assert EccSkillBridge.normalize_skill_name("python-patterns") == "python-patterns"

    def test_uppercase(self):
        assert EccSkillBridge.normalize_skill_name("Python Patterns") == "python-patterns"

    def test_special_chars(self):
        assert EccSkillBridge.normalize_skill_name("my_skill@v2!") == "my-skill-v2"

    def test_multiple_hyphens(self):
        assert EccSkillBridge.normalize_skill_name("a--b---c") == "a-b-c"

    def test_truncation(self):
        long_name = "a" * 100
        result = EccSkillBridge.normalize_skill_name(long_name)
        assert len(result) <= 64


# ---------------------------------------------------------------------------
# list_available
# ---------------------------------------------------------------------------


class TestListAvailable:
    def test_list_all(self, bridge: EccSkillBridge):
        available = bridge.list_available()
        names = [s.name for s in available]
        assert "python-patterns" in names
        assert "tdd-workflow" in names
        assert "security-review" in names
        assert len(available) == 5

    def test_filter_glob(self, bridge: EccSkillBridge):
        available = bridge.list_available(pattern="python*")
        names = [s.name for s in available]
        assert names == ["python-patterns", "python-testing"]

    def test_filter_glob_suffix(self, bridge: EccSkillBridge):
        available = bridge.list_available(pattern="*-workflow")
        names = [s.name for s in available]
        assert names == ["tdd-workflow"]

    def test_filter_category_keyword(self, bridge: EccSkillBridge):
        available = bridge.list_available(category="security")
        names = [s.name for s in available]
        assert "security-review" in names
        assert "python-patterns" not in names

    def test_filter_no_match(self, bridge: EccSkillBridge):
        available = bridge.list_available(pattern="nonexistent*")
        assert available == []

    def test_no_source_raises(self, opc_home: Path):
        bridge = EccSkillBridge(opc_home)
        with pytest.raises(EccBridgeError, match="not prepared"):
            bridge.list_available()


# ---------------------------------------------------------------------------
# _convert_skill (frontmatter conversion)
# ---------------------------------------------------------------------------


class TestConvertSkill:
    def test_basic_conversion(self, bridge: EccSkillBridge, ecc_repo: Path):
        skill_md = ecc_repo / "skills" / "python-patterns" / "SKILL.md"
        fm, body = bridge._convert_skill(skill_md, always=False)

        assert fm["name"] == "python-patterns"
        assert "Pythonic idioms" in fm["description"]
        assert "always" not in fm
        assert fm["metadata"]["imported_from"]["source"] == "ecc"
        assert "# python-patterns" in body

    def test_always_flag(self, bridge: EccSkillBridge, ecc_repo: Path):
        skill_md = ecc_repo / "skills" / "tdd-workflow" / "SKILL.md"
        fm, _body = bridge._convert_skill(skill_md, always=True)
        assert fm["always"] is True

    def test_extra_frontmatter_moved_to_metadata(self, bridge: EccSkillBridge, ecc_repo: Path):
        skill_md = ecc_repo / "skills" / "security-review" / "SKILL.md"
        fm, _body = bridge._convert_skill(skill_md, always=False)

        # argument-hint is not an allowed key, should be in metadata.imported_frontmatter
        assert "argument-hint" not in fm
        assert fm["metadata"]["imported_frontmatter"]["argument-hint"] == "<path>"
        # metadata.origin should be preserved
        assert fm["metadata"]["origin"] == "ECC"

    def test_description_truncation(self, bridge: EccSkillBridge, ecc_repo: Path):
        # Create a skill with very long description
        skills_dir = ecc_repo / "skills"
        long_desc = "x" * 2000
        _make_ecc_skill(skills_dir, "long-desc", long_desc)
        skill_md = skills_dir / "long-desc" / "SKILL.md"
        fm, _body = bridge._convert_skill(skill_md, always=False)
        assert len(fm["description"]) <= 1024


# ---------------------------------------------------------------------------
# import_skills
# ---------------------------------------------------------------------------


class TestImportSkills:
    def test_import_single(self, bridge: EccSkillBridge, opc_home: Path):
        results = bridge.import_skills(["python-patterns"])
        assert len(results) == 1
        assert results[0].success
        assert not results[0].skipped

        # Verify file exists
        target = opc_home / "skills" / "python-patterns" / "SKILL.md"
        assert target.exists()

        # Verify content is valid OpenOPC format
        text = target.read_text(encoding="utf-8")
        assert text.startswith("---")
        fm_match = text.split("---")
        fm = yaml.safe_load(fm_match[1])
        assert fm["name"] == "python-patterns"
        assert "description" in fm

    def test_import_multiple(self, bridge: EccSkillBridge, opc_home: Path):
        results = bridge.import_skills(["python-patterns", "tdd-workflow", "docker-patterns"])
        assert all(r.success for r in results)
        assert (opc_home / "skills" / "python-patterns" / "SKILL.md").exists()
        assert (opc_home / "skills" / "tdd-workflow" / "SKILL.md").exists()
        assert (opc_home / "skills" / "docker-patterns" / "SKILL.md").exists()

    def test_import_nonexistent(self, bridge: EccSkillBridge):
        results = bridge.import_skills(["does-not-exist"])
        assert len(results) == 1
        assert not results[0].success
        assert "not found" in results[0].message.lower() or "SKILL.md" in results[0].message

    def test_skip_existing(self, bridge: EccSkillBridge, opc_home: Path):
        # First import
        bridge.import_skills(["python-patterns"])
        # Second import should skip
        results = bridge.import_skills(["python-patterns"])
        assert results[0].skipped

    def test_overwrite_existing(self, bridge: EccSkillBridge, opc_home: Path):
        bridge.import_skills(["python-patterns"])
        results = bridge.import_skills(["python-patterns"], overwrite=True)
        assert results[0].success
        assert not results[0].skipped

    def test_import_with_always(self, bridge: EccSkillBridge, opc_home: Path):
        bridge.import_skills(["tdd-workflow"], always=True)
        target = opc_home / "skills" / "tdd-workflow" / "SKILL.md"
        text = target.read_text(encoding="utf-8")
        fm = yaml.safe_load(text.split("---")[1])
        assert fm["always"] is True

    def test_import_with_resource_dirs(self, bridge: EccSkillBridge, opc_home: Path, ecc_repo: Path):
        # Add a resource dir to the source skill
        assets_dir = ecc_repo / "skills" / "python-patterns" / "assets"
        assets_dir.mkdir(exist_ok=True)
        (assets_dir / "example.txt").write_text("example", encoding="utf-8")

        bridge.import_skills(["python-patterns"])
        target_assets = opc_home / "skills" / "python-patterns" / "assets"
        assert target_assets.exists()
        assert (target_assets / "example.txt").exists()


# ---------------------------------------------------------------------------
# SkillLibrary integration
# ---------------------------------------------------------------------------


class TestSkillLibraryIntegration:
    def test_imported_skill_loads_in_library(self, bridge: EccSkillBridge, opc_home: Path):
        bridge.import_skills(["python-patterns", "tdd-workflow"])

        lib = SkillLibrary(opc_home)
        lib.load_all()

        skill = lib.get("python-patterns")
        assert skill is not None
        assert "Pythonic" in skill.description
        assert skill.level == "system"

        tdd = lib.get("tdd-workflow")
        assert tdd is not None

    def test_imported_always_skill_in_summary(self, bridge: EccSkillBridge, opc_home: Path):
        bridge.import_skills(["tdd-workflow"], always=True)

        lib = SkillLibrary(opc_home)
        lib.load_all()

        summary = lib.build_skills_summary()
        assert "tdd-workflow" in summary


# ---------------------------------------------------------------------------
# _render_skill_document
# ---------------------------------------------------------------------------


class TestRenderSkillDocument:
    def test_roundtrip(self):
        fm = {"name": "test-skill", "description": "A test skill"}
        body = "# Test\n\nHello world.\n"
        rendered = _render_skill_document(fm, body)
        assert rendered.startswith("---\n")
        assert "name: test-skill" in rendered
        assert "# Test" in rendered

    def test_unicode(self):
        fm = {"name": "uni-skill", "description": "技能描述"}
        body = "# 標題\n\n中文內容\n"
        rendered = _render_skill_document(fm, body)
        assert "技能描述" in rendered
        assert "中文內容" in rendered


# ---------------------------------------------------------------------------
# prepare_source (local path validation)
# ---------------------------------------------------------------------------


class TestPrepareSource(unittest.IsolatedAsyncioTestCase):
    async def test_local_path_valid(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            opc_home = tmp_path / "opc-home"
            opc_home.mkdir(parents=True)
            (opc_home / "skills").mkdir()
            ecc_repo = tmp_path / "ecc-repo"
            skills_dir = ecc_repo / "skills"
            skills_dir.mkdir(parents=True)
            _make_ecc_skill(skills_dir, "test-skill", "Test skill")
            bridge = EccSkillBridge(opc_home, ecc_repo_path=ecc_repo)
            result = await bridge.prepare_source()
            assert result == ecc_repo

    async def test_local_path_no_skills_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            opc_home = tmp_path / "opc-home"
            opc_home.mkdir(parents=True)
            (opc_home / "skills").mkdir()
            bad_path = tmp_path / "no-skills"
            bad_path.mkdir()
            bridge = EccSkillBridge(opc_home, ecc_repo_path=bad_path)
            with self.assertRaises(EccBridgeError):
                await bridge.prepare_source()


# ---------------------------------------------------------------------------
# EccAgentBridge fixtures and tests
# ---------------------------------------------------------------------------


def _make_ecc_agent(agents_dir: Path, name: str, description: str = "", tools: str = "", model: str = "", body: str = "") -> Path:
    """Create a fake ECC agent .md file."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    fm: dict = {"name": name, "description": description or f"ECC agent: {name}"}
    if tools:
        fm["tools"] = tools
    if model:
        fm["model"] = model
    if not body:
        body = f"You are a specialized {name} agent.\n"
    content = f"---\n{yaml.dump(fm, default_flow_style=False)}---\n\n{body}"
    path = agents_dir / f"{name}.md"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def ecc_repo_with_agents(tmp_path: Path) -> Path:
    """Create a mock ECC repository with agents/ and rules/ directories."""
    repo = tmp_path / "ecc-repo-full"
    # skills dir (required for source validation)
    (repo / "skills").mkdir(parents=True)
    # agents
    agents_dir = repo / "agents"
    _make_ecc_agent(agents_dir, "code-reviewer", "Reviews code for quality", "Read, Grep, Bash", "opus")
    _make_ecc_agent(agents_dir, "planner", "Feature implementation planning", "Read, Grep", "sonnet")
    _make_ecc_agent(agents_dir, "security-reviewer", "Vulnerability analysis", "Read, Grep, Glob", "opus")
    _make_ecc_agent(agents_dir, "doc-updater", "Documentation sync", "Read, Write", "")
    # rules
    rules_dir = repo / "rules"
    common_dir = rules_dir / "common"
    common_dir.mkdir(parents=True)
    (common_dir / "coding-style.md").write_text("# Coding Style\n\nUse immutability.\n", encoding="utf-8")
    (common_dir / "testing.md").write_text("# Testing Rules\n\nTDD always.\n", encoding="utf-8")
    (common_dir / "README.md").write_text("# Rules README\n", encoding="utf-8")
    python_dir = rules_dir / "python"
    python_dir.mkdir(parents=True)
    (python_dir / "patterns.md").write_text("# Python Patterns\n\nUse dataclasses.\n", encoding="utf-8")
    ts_dir = rules_dir / "typescript"
    ts_dir.mkdir(parents=True)
    (ts_dir / "strict-mode.md").write_text("# Strict Mode\n\nAlways strict.\n", encoding="utf-8")
    return repo


@pytest.fixture()
def agent_bridge(opc_home: Path, ecc_repo_with_agents: Path) -> EccAgentBridge:
    return EccAgentBridge(opc_home, ecc_repo_path=ecc_repo_with_agents)


@pytest.fixture()
def rules_bridge(opc_home: Path, ecc_repo_with_agents: Path) -> EccRulesBridge:
    return EccRulesBridge(opc_home, ecc_repo_path=ecc_repo_with_agents)


class TestEccAgentBridge:
    def test_list_all(self, agent_bridge: EccAgentBridge):
        agents = agent_bridge.list_available()
        names = [a.name for a in agents]
        assert "code-reviewer" in names
        assert "planner" in names
        assert "security-reviewer" in names
        assert "doc-updater" in names
        assert len(agents) == 4

    def test_list_with_pattern(self, agent_bridge: EccAgentBridge):
        agents = agent_bridge.list_available(pattern="*reviewer")
        names = [a.name for a in agents]
        assert "code-reviewer" in names
        assert "security-reviewer" in names
        assert "planner" not in names

    def test_agent_info_fields(self, agent_bridge: EccAgentBridge):
        agents = agent_bridge.list_available(pattern="code-reviewer")
        assert len(agents) == 1
        agent = agents[0]
        assert agent.description == "Reviews code for quality"
        assert agent.tools == ["Read", "Grep", "Bash"]
        assert agent.model == "opus"

    def test_import_single_agent(self, agent_bridge: EccAgentBridge, opc_home: Path):
        results = agent_bridge.import_agents(["code-reviewer"])
        assert len(results) == 1
        assert results[0].success
        assert not results[0].skipped
        target = opc_home / "prompts" / "talent" / "ecc-code-reviewer.md"
        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert "code-reviewer" in content
        assert "specialized" in content

    def test_import_multiple_agents(self, agent_bridge: EccAgentBridge, opc_home: Path):
        results = agent_bridge.import_agents(["planner", "doc-updater"])
        assert all(r.success for r in results)
        assert (opc_home / "prompts" / "talent" / "ecc-planner.md").exists()
        assert (opc_home / "prompts" / "talent" / "ecc-doc-updater.md").exists()

    def test_import_nonexistent_agent(self, agent_bridge: EccAgentBridge):
        results = agent_bridge.import_agents(["does-not-exist"])
        assert len(results) == 1
        assert not results[0].success
        assert "not found" in results[0].message.lower()

    def test_skip_existing(self, agent_bridge: EccAgentBridge):
        agent_bridge.import_agents(["planner"])
        results = agent_bridge.import_agents(["planner"])
        assert results[0].skipped

    def test_overwrite_existing(self, agent_bridge: EccAgentBridge):
        agent_bridge.import_agents(["planner"])
        results = agent_bridge.import_agents(["planner"], overwrite=True)
        assert results[0].success
        assert not results[0].skipped

    def test_employee_entry_structure(self, agent_bridge: EccAgentBridge):
        results = agent_bridge.import_agents(["security-reviewer"])
        entry = results[0].employee_entry
        assert entry["employee_id"] == "ecc-security-reviewer"
        assert entry["category"] == "quality"
        assert entry["metadata"]["source"] == "ecc"
        assert "prompts/talent/ecc-security-reviewer.md" in entry["prompt_refs"]

    def test_no_source_raises(self, opc_home: Path):
        bridge = EccAgentBridge(opc_home)
        with pytest.raises(EccBridgeError, match="not prepared"):
            bridge.list_available()


class TestEccRulesBridge:
    def test_list_all(self, rules_bridge: EccRulesBridge):
        rules = rules_bridge.list_available()
        names = [r.name for r in rules]
        assert "common-coding-style" in names
        assert "common-testing" in names
        assert "python-patterns" in names
        assert "typescript-strict-mode" in names
        # README.md should be excluded
        assert not any("readme" in n for n in names)

    def test_filter_by_language(self, rules_bridge: EccRulesBridge):
        rules = rules_bridge.list_available(languages=["common"])
        assert all(r.language == "common" for r in rules)
        assert len(rules) == 2

    def test_filter_multiple_languages(self, rules_bridge: EccRulesBridge):
        rules = rules_bridge.list_available(languages=["common", "python"])
        langs = {r.language for r in rules}
        assert langs == {"common", "python"}
        assert len(rules) == 3

    def test_import_rules_common(self, rules_bridge: EccRulesBridge, opc_home: Path):
        results = rules_bridge.import_rules(languages=["common"])
        assert all(r.success for r in results)
        assert len(results) == 2
        # Verify skill files created
        skill_dir = opc_home / "skills" / "ecc-rule-common-coding-style"
        assert (skill_dir / "SKILL.md").exists()
        # Verify always: true
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        fm = yaml.safe_load(text.split("---")[1])
        assert fm["always"] is True
        assert fm["metadata"]["imported_from"]["source"] == "ecc-rules"

    def test_import_all_rules(self, rules_bridge: EccRulesBridge, opc_home: Path):
        results = rules_bridge.import_rules()
        assert all(r.success for r in results)
        assert len(results) == 4  # 2 common + 1 python + 1 typescript

    def test_skip_existing(self, rules_bridge: EccRulesBridge):
        rules_bridge.import_rules(languages=["python"])
        results = rules_bridge.import_rules(languages=["python"])
        assert all(r.skipped for r in results)

    def test_overwrite(self, rules_bridge: EccRulesBridge):
        rules_bridge.import_rules(languages=["python"])
        results = rules_bridge.import_rules(languages=["python"], overwrite=True)
        assert all(r.success and not r.skipped for r in results)

    def test_imported_rule_loads_in_skill_library(self, rules_bridge: EccRulesBridge, opc_home: Path):
        rules_bridge.import_rules(languages=["common"])
        lib = SkillLibrary(opc_home)
        lib.load_all()
        skill = lib.get("ecc-rule-common-coding-style")
        assert skill is not None
        assert skill.always is True

    def test_no_source_raises(self, opc_home: Path):
        bridge = EccRulesBridge(opc_home)
        with pytest.raises(EccBridgeError, match="not prepared"):
            bridge.list_available()
