"""Tests for opc.layer5_memory.ecc_bridge — ECC skill bridge."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from opc.layer5_memory.ecc_bridge import (
    EccBridgeError,
    EccImportResult,
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


class TestPrepareSource:
    @pytest.mark.asyncio
    async def test_local_path_valid(self, opc_home: Path, ecc_repo: Path):
        bridge = EccSkillBridge(opc_home, ecc_repo_path=ecc_repo)
        result = await bridge.prepare_source()
        assert result == ecc_repo

    @pytest.mark.asyncio
    async def test_local_path_no_skills_dir(self, opc_home: Path, tmp_path: Path):
        bad_path = tmp_path / "no-skills"
        bad_path.mkdir()
        bridge = EccSkillBridge(opc_home, ecc_repo_path=bad_path)
        with pytest.raises(EccBridgeError, match="skills/"):
            await bridge.prepare_source()
