"""跨 harness 可攜式記憶儲存，兼容 ECC memory vault 格式 (ecc.memory.v1)。

職責說明：
    提供統一的記憶文檔儲存，支持跨 AI 工具（harness）的上下文交接。
    格式兼容 ECC Memory Vault 標準，允許 OpenOPC 與 Claude Code、Codex、
    Cursor 等工具之間共享專案記憶。

關聯關係：
    - 被 opc/layer5_memory/memory_manager.py 的 MemoryManager 整合
    - 被 opc/cli/app.py 的 memory-vault 命令組呼叫
    - 格式參考 ECC docs/design/ecc-memory-vault.md

使用範例：
    vault = MemoryVault(opc_home)
    mem_id = vault.save("Auth migration", body_text, tags=["auth"])
    results = vault.search("authentication")
    entry = vault.read(mem_id)
    report = vault.doctor()
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# 支持的記憶文檔類型
MEMORY_TYPE = "ecc.memory.v1"

# 允許的狀態值
VALID_STATUSES = frozenset({"active", "archived", "superseded"})


@dataclass
class MemoryEntry:
    """一條記憶文檔的完整表示。"""

    id: str
    title: str
    body: str = ""
    source_harness: str = "openopc"
    target_harness: str = ""
    created_at: str = ""
    updated_at: str = ""
    tags: list[str] = field(default_factory=list)
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""


@dataclass
class VaultHealthReport:
    """Vault 健康檢查報告。"""

    total_entries: int = 0
    active_entries: int = 0
    orphaned_files: int = 0
    invalid_entries: list[str] = field(default_factory=list)
    healthy: bool = True
    messages: list[str] = field(default_factory=list)


def _generate_memory_id() -> str:
    """生成唯一的記憶 ID。"""
    return f"mem-{uuid.uuid4().hex[:12]}"


def _utc_now_iso() -> str:
    """返回當前 UTC 時間的 ISO 8601 字串。"""
    return datetime.now(timezone.utc).isoformat()


def _slugify_filename(memory_id: str) -> str:
    """將 memory ID 轉為安全的檔案名。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", memory_id)


class MemoryVault:
    """跨 harness 可攜式記憶儲存，兼容 ECC memory vault 格式。

    儲存結構：
        {opc_home}/memory/vault/
            mem-xxxxxxxxxxxx.md    # 每條記憶一個 Markdown 文件

    文件格式 (ecc.memory.v1):
        ---
        id: mem-xxxx
        type: ecc.memory.v1
        title: "Continue authentication migration"
        source_harness: openopc
        target_harness: codex
        created_at: 2026-07-27T...
        updated_at: 2026-07-27T...
        tags: [auth, migration]
        status: active
        ---
        Body content here...
    """

    def __init__(self, opc_home: Path, scope: str = "project") -> None:
        """初始化 Memory Vault。

        Args:
            opc_home: OPC 主目錄路徑。
            scope: 儲存範圍。"project" → .opc/memory/vault/，
                   "user" → ~/.opc/memory/vault/
        """
        self.opc_home = Path(opc_home)
        self.scope = scope
        if scope == "user":
            self._vault_dir = Path.home() / ".opc" / "memory" / "vault"
        else:
            self._vault_dir = self.opc_home / "memory" / "vault"
        self._vault_dir.mkdir(parents=True, exist_ok=True)

    @property
    def vault_dir(self) -> Path:
        """返回 vault 儲存目錄路徑。"""
        return self._vault_dir

    def save(
        self,
        title: str,
        body: str,
        *,
        source_harness: str = "openopc",
        target_harness: str = "",
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """寫入一條新記憶文檔，返回 memory-id。

        Args:
            title: 記憶標題。
            body: 記憶內容。
            source_harness: 來源工具標識。
            target_harness: 目標工具標識（空表示通用）。
            tags: 標籤列表。
            metadata: 額外元資料。

        Returns:
            str — 新記憶的 ID（如 "mem-a1b2c3d4e5f6"）。
        """
        memory_id = _generate_memory_id()
        now = _utc_now_iso()

        frontmatter: dict[str, Any] = {
            "id": memory_id,
            "type": MEMORY_TYPE,
            "title": title,
            "source_harness": source_harness,
            "created_at": now,
            "updated_at": now,
            "tags": tags or [],
            "status": "active",
        }
        if target_harness:
            frontmatter["target_harness"] = target_harness
        if metadata:
            frontmatter["metadata"] = metadata

        self._write_entry(memory_id, frontmatter, body)
        logger.info(f"Memory vault: saved '{title}' as {memory_id}")
        return memory_id

    def search(
        self,
        query: str,
        *,
        target_harness: str = "",
        status: str = "",
        tags: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """全文搜尋記憶。

        Args:
            query: 搜尋關鍵字（在 title + body + tags 中匹配）。
            target_harness: 篩選目標工具。
            status: 篩選狀態（如 "active"）。
            tags: 篩選標籤（任一匹配即返回）。

        Returns:
            匹配的記憶條目列表（按 created_at 降序）。
        """
        entries = self._load_all_entries()
        query_lower = query.lower()
        results: list[MemoryEntry] = []

        for entry in entries:
            # Status filter
            if status and entry.status != status:
                continue
            # Target harness filter
            if target_harness and entry.target_harness != target_harness:
                continue
            # Tags filter
            if tags and not any(t in entry.tags for t in tags):
                continue
            # Text match
            haystack = f"{entry.title} {entry.body} {' '.join(entry.tags)}".lower()
            if query_lower in haystack:
                results.append(entry)

        # Sort by created_at descending
        results.sort(key=lambda e: e.created_at, reverse=True)
        return results

    def read(self, memory_id: str) -> MemoryEntry | None:
        """按 ID 讀取單條記憶。

        Args:
            memory_id: 記憶 ID。

        Returns:
            MemoryEntry 或 None（若不存在）。
        """
        file_path = self._entry_path(memory_id)
        if not file_path.exists():
            return None
        return self._parse_entry_file(file_path)

    def update(
        self,
        memory_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> bool:
        """更新已有記憶的欄位。

        Args:
            memory_id: 要更新的記憶 ID。
            title: 新標題（None 表示不變）。
            body: 新內容（None 表示不變）。
            tags: 新標籤（None 表示不變）。
            status: 新狀態（None 表示不變）。

        Returns:
            bool — 是否更新成功。
        """
        entry = self.read(memory_id)
        if entry is None:
            return False

        fm = self._entry_to_frontmatter(entry)
        if title is not None:
            fm["title"] = title
        if tags is not None:
            fm["tags"] = tags
        if status is not None and status in VALID_STATUSES:
            fm["status"] = status
        fm["updated_at"] = _utc_now_iso()

        new_body = body if body is not None else entry.body
        self._write_entry(memory_id, fm, new_body)
        return True

    def handoff(
        self,
        title: str,
        body: str,
        *,
        from_harness: str,
        target_harness: str,
        tags: list[str] | None = None,
    ) -> str:
        """建立跨 harness 交接文檔。

        Args:
            title: 交接標題。
            body: 交接內容。
            from_harness: 來源工具。
            target_harness: 目標工具。
            tags: 標籤。

        Returns:
            str — 新記憶的 ID。
        """
        return self.save(
            title,
            body,
            source_harness=from_harness,
            target_harness=target_harness,
            tags=(tags or []) + ["handoff"],
            metadata={"handoff": True, "from": from_harness, "to": target_harness},
        )

    def doctor(self) -> VaultHealthReport:
        """驗證 vault 完整性。

        檢查項目：
            - 所有 .md 文件是否可正確解析
            - frontmatter 是否含必要欄位（id, type, title）
            - 是否有孤立文件（無效格式）

        Returns:
            VaultHealthReport — 健康檢查結果。
        """
        report = VaultHealthReport()
        md_files = list(self._vault_dir.glob("*.md"))
        report.total_entries = len(md_files)

        for md_file in md_files:
            entry = self._parse_entry_file(md_file)
            if entry is None:
                report.orphaned_files += 1
                report.invalid_entries.append(md_file.name)
                report.healthy = False
                report.messages.append(f"Invalid entry: {md_file.name}")
                continue
            if entry.status == "active":
                report.active_entries += 1
            # Validate required fields
            if not entry.id or not entry.title:
                report.invalid_entries.append(md_file.name)
                report.healthy = False
                report.messages.append(f"Missing required fields: {md_file.name}")

        if report.healthy:
            report.messages.append(
                f"Vault healthy: {report.total_entries} entries, "
                f"{report.active_entries} active"
            )
        return report

    def list_all(self, status: str = "") -> list[MemoryEntry]:
        """列出所有記憶條目。

        Args:
            status: 篩選狀態（空表示全部）。

        Returns:
            記憶條目列表（按 created_at 降序）。
        """
        entries = self._load_all_entries()
        if status:
            entries = [e for e in entries if e.status == status]
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _entry_path(self, memory_id: str) -> Path:
        """取得記憶條目的檔案路徑。"""
        return self._vault_dir / f"{_slugify_filename(memory_id)}.md"

    def _write_entry(self, memory_id: str, frontmatter: dict[str, Any], body: str) -> None:
        """將記憶條目寫入磁碟。"""
        fm_text = yaml.dump(
            frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False
        ).strip()
        content = f"---\n{fm_text}\n---\n\n{body.strip()}\n"
        path = self._entry_path(memory_id)
        path.write_text(content, encoding="utf-8")

    def _parse_entry_file(self, path: Path) -> MemoryEntry | None:
        """解析記憶 Markdown 文件為 MemoryEntry。"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None

        if not text.startswith("---"):
            return None

        parts = text.split("\n")
        end_idx = -1
        for i in range(1, len(parts)):
            if parts[i].strip() == "---":
                end_idx = i
                break
        if end_idx < 0:
            return None

        fm_text = "\n".join(parts[1:end_idx])
        body = "\n".join(parts[end_idx + 1:]).strip()

        try:
            fm = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError:
            return None
        if not isinstance(fm, dict):
            return None

        return MemoryEntry(
            id=str(fm.get("id", "")),
            title=str(fm.get("title", "")),
            body=body,
            source_harness=str(fm.get("source_harness", "openopc")),
            target_harness=str(fm.get("target_harness", "")),
            created_at=str(fm.get("created_at", "")),
            updated_at=str(fm.get("updated_at", "")),
            tags=fm.get("tags", []) if isinstance(fm.get("tags"), list) else [],
            status=str(fm.get("status", "active")),
            metadata=fm.get("metadata", {}) if isinstance(fm.get("metadata"), dict) else {},
            file_path=str(path),
        )

    def _load_all_entries(self) -> list[MemoryEntry]:
        """載入 vault 中所有記憶條目。"""
        entries: list[MemoryEntry] = []
        for md_file in self._vault_dir.glob("*.md"):
            entry = self._parse_entry_file(md_file)
            if entry is not None:
                entries.append(entry)
        return entries

    @staticmethod
    def _entry_to_frontmatter(entry: MemoryEntry) -> dict[str, Any]:
        """將 MemoryEntry 轉回 frontmatter dict。"""
        fm: dict[str, Any] = {
            "id": entry.id,
            "type": MEMORY_TYPE,
            "title": entry.title,
            "source_harness": entry.source_harness,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
            "tags": entry.tags,
            "status": entry.status,
        }
        if entry.target_harness:
            fm["target_harness"] = entry.target_harness
        if entry.metadata:
            fm["metadata"] = entry.metadata
        return fm
