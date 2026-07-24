"""角色三庫架構管理器測試。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from opc.layer2_organization.role_repository import (
    RoleRepositoryManager,
    RoleRepositoryPaths,
    SkillRegistryEntry,
)


@pytest.fixture()
def repo_manager(tmp_path: Path) -> RoleRepositoryManager:
    return RoleRepositoryManager(tmp_path)


class TestEnsureRoleRepos:
    """Tests for directory structure creation."""

    def test_creates_directory_structure(self, repo_manager: RoleRepositoryManager) -> None:
        paths = repo_manager.ensure_role_repos("devops_engineer")
        assert paths.root.exists()
        assert paths.knowledge_dir.exists()
        assert paths.knowledge_entries_dir.exists()
        assert paths.skills_dir.exists()
        assert paths.files_dir.exists()
        assert paths.files_blobs_dir.exists()

    def test_creates_index_files(self, repo_manager: RoleRepositoryManager) -> None:
        paths = repo_manager.ensure_role_repos("designer")
        assert paths.knowledge_index.exists()
        assert paths.files_index.exists()
        assert paths.skill_registry.exists()

    def test_idempotent(self, repo_manager: RoleRepositoryManager) -> None:
        paths1 = repo_manager.ensure_role_repos("devops")
        paths2 = repo_manager.ensure_role_repos("devops")
        assert paths1.root == paths2.root

    def test_role_exists(self, repo_manager: RoleRepositoryManager) -> None:
        assert not repo_manager.role_exists("new_role")
        repo_manager.ensure_role_repos("new_role")
        assert repo_manager.role_exists("new_role")


class TestKnowledgeCRUD:
    """Tests for knowledge repository operations."""

    def test_add_and_list(self, repo_manager: RoleRepositoryManager) -> None:
        entry_id = repo_manager.add_knowledge("devops", {
            "title": "CI Best Practices",
            "content": "Always run tests before merge.",
            "domain": "ci",
            "tags": ["ci", "testing"],
        })
        assert entry_id
        entries = repo_manager.list_knowledge("devops")
        assert len(entries) == 1
        assert entries[0]["title"] == "CI Best Practices"
        assert entries[0]["domain"] == "ci"

    def test_get_single(self, repo_manager: RoleRepositoryManager) -> None:
        entry_id = repo_manager.add_knowledge("devops", {"title": "Test", "content": "Body"})
        entry = repo_manager.get_knowledge("devops", entry_id)
        assert entry is not None
        assert entry["title"] == "Test"

    def test_get_nonexistent(self, repo_manager: RoleRepositoryManager) -> None:
        assert repo_manager.get_knowledge("devops", "nonexistent") is None

    def test_remove(self, repo_manager: RoleRepositoryManager) -> None:
        entry_id = repo_manager.add_knowledge("devops", {"title": "ToRemove"})
        assert repo_manager.remove_knowledge("devops", entry_id)
        assert repo_manager.get_knowledge("devops", entry_id) is None
        assert repo_manager.list_knowledge("devops") == []

    def test_remove_nonexistent(self, repo_manager: RoleRepositoryManager) -> None:
        assert not repo_manager.remove_knowledge("devops", "nope")

    def test_multiple_entries(self, repo_manager: RoleRepositoryManager) -> None:
        repo_manager.add_knowledge("devops", {"title": "A"})
        repo_manager.add_knowledge("devops", {"title": "B"})
        repo_manager.add_knowledge("devops", {"title": "C"})
        assert len(repo_manager.list_knowledge("devops")) == 3


class TestSkillRegistry:
    """Tests for skill repository operations."""

    def test_add_and_list_skills(self, repo_manager: RoleRepositoryManager) -> None:
        repo_manager.ensure_role_repos("devops")
        repo_manager.add_skill("devops", SkillRegistryEntry(
            skill_id="ci-monitoring",
            name="CI/CD Monitoring",
            tool_access=["shell_exec", "file_read"],
        ))
        skills = repo_manager.list_role_skills("devops")
        assert "ci-monitoring" in skills

    def test_skill_set(self, repo_manager: RoleRepositoryManager) -> None:
        repo_manager.add_skill("devops", SkillRegistryEntry(skill_id="s1"))
        repo_manager.add_skill("devops", SkillRegistryEntry(skill_id="s2"))
        assert repo_manager.get_skill_set("devops") == {"s1", "s2"}

    def test_deduplication(self, repo_manager: RoleRepositoryManager) -> None:
        repo_manager.add_skill("devops", SkillRegistryEntry(skill_id="s1"))
        repo_manager.add_skill("devops", SkillRegistryEntry(skill_id="s1"))
        assert len(repo_manager.list_role_skills("devops")) == 1

    def test_remove_skill(self, repo_manager: RoleRepositoryManager) -> None:
        repo_manager.add_skill("devops", SkillRegistryEntry(skill_id="s1"))
        repo_manager.add_skill("devops", SkillRegistryEntry(skill_id="s2"))
        assert repo_manager.remove_skill("devops", "s1")
        assert repo_manager.list_role_skills("devops") == ["s2"]

    def test_remove_nonexistent_skill(self, repo_manager: RoleRepositoryManager) -> None:
        repo_manager.ensure_role_repos("devops")
        assert not repo_manager.remove_skill("devops", "nope")

    def test_registry_yaml_format(self, repo_manager: RoleRepositoryManager) -> None:
        repo_manager.add_skill("devops", SkillRegistryEntry(
            skill_id="ci",
            name="CI",
            tool_access=["shell_exec"],
        ))
        paths = repo_manager.role_paths("devops")
        data = yaml.safe_load(paths.skill_registry.read_text(encoding="utf-8"))
        assert data["role_id"] == "devops"
        assert len(data["skills"]) == 1
        assert data["skills"][0]["skill_id"] == "ci"
        assert data["skills"][0]["tool_access"] == ["shell_exec"]

    def test_empty_role_returns_empty(self, repo_manager: RoleRepositoryManager) -> None:
        assert repo_manager.list_role_skills("nonexistent") == []
        assert repo_manager.get_skill_set("nonexistent") == set()


class TestFileRepository:
    """Tests for file repository operations."""

    def test_add_and_list_file(self, repo_manager: RoleRepositoryManager, tmp_path: Path) -> None:
        source = tmp_path / "test_doc.txt"
        source.write_text("hello world", encoding="utf-8")
        file_id = repo_manager.add_file("devops", source, {"category": "docs"})
        assert file_id
        files = repo_manager.list_files("devops")
        assert len(files) == 1
        assert files[0]["original_name"] == "test_doc.txt"
        assert files[0]["metadata"]["category"] == "docs"

    def test_file_blob_stored(self, repo_manager: RoleRepositoryManager, tmp_path: Path) -> None:
        source = tmp_path / "data.csv"
        source.write_text("a,b,c", encoding="utf-8")
        file_id = repo_manager.add_file("devops", source)
        files = repo_manager.list_files("devops")
        stored_path = Path(files[0]["stored_path"])
        assert stored_path.exists()
        assert stored_path.read_text(encoding="utf-8") == "a,b,c"

    def test_remove_file(self, repo_manager: RoleRepositoryManager, tmp_path: Path) -> None:
        source = tmp_path / "temp.txt"
        source.write_text("temp", encoding="utf-8")
        file_id = repo_manager.add_file("devops", source)
        assert repo_manager.remove_file("devops", file_id)
        assert repo_manager.list_files("devops") == []

    def test_add_nonexistent_file_raises(self, repo_manager: RoleRepositoryManager) -> None:
        with pytest.raises(FileNotFoundError):
            repo_manager.add_file("devops", Path("/nonexistent/file.txt"))


class TestRoleIsolation:
    """Tests for cross-role isolation."""

    def test_roles_are_isolated(self, repo_manager: RoleRepositoryManager) -> None:
        repo_manager.add_knowledge("role_a", {"title": "A knowledge"})
        repo_manager.add_knowledge("role_b", {"title": "B knowledge"})
        assert len(repo_manager.list_knowledge("role_a")) == 1
        assert len(repo_manager.list_knowledge("role_b")) == 1
        assert repo_manager.list_knowledge("role_a")[0]["title"] == "A knowledge"

    def test_skill_isolation(self, repo_manager: RoleRepositoryManager) -> None:
        repo_manager.add_skill("role_a", SkillRegistryEntry(skill_id="skill_a"))
        repo_manager.add_skill("role_b", SkillRegistryEntry(skill_id="skill_b"))
        assert repo_manager.get_skill_set("role_a") == {"skill_a"}
        assert repo_manager.get_skill_set("role_b") == {"skill_b"}
