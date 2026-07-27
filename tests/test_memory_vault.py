"""Tests for opc.layer5_memory.memory_vault — ECC-compatible Memory Vault."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from opc.layer5_memory.memory_vault import (
    MEMORY_TYPE,
    MemoryEntry,
    MemoryVault,
    VaultHealthReport,
)


@pytest.fixture()
def vault(tmp_path: Path) -> MemoryVault:
    opc_home = tmp_path / "opc-home"
    opc_home.mkdir(parents=True)
    return MemoryVault(opc_home)


class TestMemoryVaultSave:
    def test_save_returns_id(self, vault: MemoryVault):
        mem_id = vault.save("Test title", "Test body")
        assert mem_id.startswith("mem-")
        assert len(mem_id) == 16  # "mem-" + 12 hex chars

    def test_save_creates_file(self, vault: MemoryVault):
        mem_id = vault.save("Hello", "World")
        path = vault.vault_dir / f"{mem_id}.md"
        assert path.exists()

    def test_save_file_format(self, vault: MemoryVault):
        mem_id = vault.save("Auth migration", "Continue the auth work", tags=["auth"])
        path = vault.vault_dir / f"{mem_id}.md"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        fm = yaml.safe_load(text.split("---")[1])
        assert fm["id"] == mem_id
        assert fm["type"] == MEMORY_TYPE
        assert fm["title"] == "Auth migration"
        assert fm["source_harness"] == "openopc"
        assert fm["status"] == "active"
        assert "auth" in fm["tags"]
        assert "Continue the auth work" in text

    def test_save_with_target_harness(self, vault: MemoryVault):
        mem_id = vault.save("Handoff", "body", target_harness="codex")
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.target_harness == "codex"

    def test_save_with_metadata(self, vault: MemoryVault):
        mem_id = vault.save("Meta", "body", metadata={"key": "value"})
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.metadata["key"] == "value"


class TestMemoryVaultRead:
    def test_read_existing(self, vault: MemoryVault):
        mem_id = vault.save("Read test", "Some content")
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.id == mem_id
        assert entry.title == "Read test"
        assert entry.body == "Some content"

    def test_read_nonexistent(self, vault: MemoryVault):
        assert vault.read("mem-nonexistent") is None

    def test_read_preserves_tags(self, vault: MemoryVault):
        mem_id = vault.save("Tagged", "body", tags=["a", "b", "c"])
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.tags == ["a", "b", "c"]


class TestMemoryVaultSearch:
    def test_search_by_title(self, vault: MemoryVault):
        vault.save("Authentication flow", "details here")
        vault.save("Database schema", "other stuff")
        results = vault.search("authentication")
        assert len(results) == 1
        assert results[0].title == "Authentication flow"

    def test_search_by_body(self, vault: MemoryVault):
        vault.save("Title A", "contains kubernetes deployment info")
        vault.save("Title B", "just regular stuff")
        results = vault.search("kubernetes")
        assert len(results) == 1
        assert results[0].title == "Title A"

    def test_search_by_tags(self, vault: MemoryVault):
        vault.save("Tagged entry", "body", tags=["deployment", "ci"])
        results = vault.search("deployment")
        assert len(results) == 1

    def test_search_no_match(self, vault: MemoryVault):
        vault.save("Something", "else")
        results = vault.search("nonexistent_keyword_xyz")
        assert results == []

    def test_search_filter_by_status(self, vault: MemoryVault):
        mem_id = vault.save("Active entry", "body")
        vault.update(mem_id, status="archived")
        vault.save("Another active", "body2")
        results = vault.search("active", status="active")
        assert len(results) == 1
        assert results[0].title == "Another active"

    def test_search_filter_by_target_harness(self, vault: MemoryVault):
        vault.save("For codex", "body", target_harness="codex")
        vault.save("For claude", "body", target_harness="claude")
        results = vault.search("for", target_harness="codex")
        assert len(results) == 1
        assert results[0].title == "For codex"


class TestMemoryVaultUpdate:
    def test_update_title(self, vault: MemoryVault):
        mem_id = vault.save("Old title", "body")
        assert vault.update(mem_id, title="New title")
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.title == "New title"

    def test_update_body(self, vault: MemoryVault):
        mem_id = vault.save("Title", "old body")
        assert vault.update(mem_id, body="new body")
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.body == "new body"

    def test_update_status(self, vault: MemoryVault):
        mem_id = vault.save("Title", "body")
        assert vault.update(mem_id, status="archived")
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.status == "archived"

    def test_update_invalid_status_rejected(self, vault: MemoryVault):
        mem_id = vault.save("Title", "body")
        vault.update(mem_id, status="invalid_status")
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.status == "active"  # unchanged

    def test_update_nonexistent(self, vault: MemoryVault):
        assert not vault.update("mem-nonexistent", title="X")


class TestMemoryVaultHandoff:
    def test_handoff_creates_entry(self, vault: MemoryVault):
        mem_id = vault.handoff(
            "Continue auth",
            "Migration details...",
            from_harness="openopc",
            target_harness="codex",
        )
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.source_harness == "openopc"
        assert entry.target_harness == "codex"
        assert "handoff" in entry.tags

    def test_handoff_metadata(self, vault: MemoryVault):
        mem_id = vault.handoff(
            "Task", "body", from_harness="claude", target_harness="cursor"
        )
        entry = vault.read(mem_id)
        assert entry is not None
        assert entry.metadata["handoff"] is True
        assert entry.metadata["from"] == "claude"
        assert entry.metadata["to"] == "cursor"


class TestMemoryVaultDoctor:
    def test_healthy_vault(self, vault: MemoryVault):
        vault.save("Entry 1", "body1")
        vault.save("Entry 2", "body2")
        report = vault.doctor()
        assert report.healthy
        assert report.total_entries == 2
        assert report.active_entries == 2
        assert report.orphaned_files == 0

    def test_empty_vault(self, vault: MemoryVault):
        report = vault.doctor()
        assert report.healthy
        assert report.total_entries == 0

    def test_invalid_file_detected(self, vault: MemoryVault):
        vault.save("Good entry", "body")
        # Write an invalid file
        (vault.vault_dir / "mem-broken.md").write_text("no frontmatter", encoding="utf-8")
        report = vault.doctor()
        assert not report.healthy
        assert report.orphaned_files == 1
        assert "mem-broken.md" in report.invalid_entries


class TestMemoryVaultListAll:
    def test_list_all(self, vault: MemoryVault):
        vault.save("A", "body")
        vault.save("B", "body")
        entries = vault.list_all()
        assert len(entries) == 2

    def test_list_all_filter_status(self, vault: MemoryVault):
        mem_id = vault.save("Active", "body")
        vault.save("Also active", "body")
        vault.update(mem_id, status="archived")
        active = vault.list_all(status="active")
        assert len(active) == 1
        assert active[0].title == "Also active"
