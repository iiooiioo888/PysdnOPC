"""角色三庫架構管理器。

每個 AI 角色配備三個獨立管理庫：
- 知識庫（knowledge/）：存儲角色專業領域的知識和經驗
- 技能庫（skills/）：管理角色具備的能力和工具訪問權限
- 文件庫（files/）：保存角色相關的文檔和資源文件

目錄結構：
    .opc/agent_homes/{role_id}/
    ├── knowledge/
    │   ├── index.json        # 知識條目索引
    │   └── entries/          # 知識條目文件
    ├── skills/
    │   ├── registry.yaml     # 技能註冊表 + 工具權限
    │   └── {skill-name}/SKILL.md
    └── files/
        ├── index.json        # 文件索引
        └── blobs/            # 實際文件
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

__all__ = [
    "RoleRepositoryPaths",
    "RoleRepositoryManager",
    "SkillRegistryEntry",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_role_dirname(role_id: str) -> str:
    """將 role_id 轉為檔案系統安全的目錄名。"""
    import re
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(role_id or "").strip()).strip("-")
    return safe or "unknown_role"


@dataclass(frozen=True)
class RoleRepositoryPaths:
    """角色三庫的目錄路徑集合。"""

    role_id: str
    root: Path
    knowledge_dir: Path
    skills_dir: Path
    files_dir: Path

    @property
    def knowledge_index(self) -> Path:
        return self.knowledge_dir / "index.json"

    @property
    def knowledge_entries_dir(self) -> Path:
        return self.knowledge_dir / "entries"

    @property
    def skill_registry(self) -> Path:
        return self.skills_dir / "registry.yaml"

    @property
    def files_index(self) -> Path:
        return self.files_dir / "index.json"

    @property
    def files_blobs_dir(self) -> Path:
        return self.files_dir / "blobs"


@dataclass
class SkillRegistryEntry:
    """技能註冊表中的單個技能條目。"""

    skill_id: str
    name: str = ""
    tool_access: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"skill_id": self.skill_id}
        if self.name:
            result["name"] = self.name
        if self.tool_access:
            result["tool_access"] = list(self.tool_access)
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SkillRegistryEntry":
        return cls(
            skill_id=str(data.get("skill_id", "") or "").strip(),
            name=str(data.get("name", "") or "").strip(),
            tool_access=[str(t).strip() for t in list(data.get("tool_access", []) or []) if str(t).strip()],
            metadata=dict(data.get("metadata", {}) or {}),
        )


class RoleRepositoryManager:
    """角色三庫架構管理器。

    負責為每個 AI 角色創建和維護獨立的知识庫、技能庫、文件庫。
    """

    def __init__(self, opc_home: Path) -> None:
        self.opc_home = Path(opc_home)
        self.agent_homes_dir = self.opc_home / "agent_homes"

    # ── 目錄管理 ──────────────────────────────────────────────────────────

    def role_paths(self, role_id: str) -> RoleRepositoryPaths:
        """取得角色的三庫路徑（不創建目錄）。"""
        safe_name = _safe_role_dirname(role_id)
        root = self.agent_homes_dir / safe_name
        return RoleRepositoryPaths(
            role_id=role_id,
            root=root,
            knowledge_dir=root / "knowledge",
            skills_dir=root / "skills",
            files_dir=root / "files",
        )

    def ensure_role_repos(self, role_id: str) -> RoleRepositoryPaths:
        """確保角色三庫目錄結構存在，返回路徑集合。"""
        paths = self.role_paths(role_id)
        paths.knowledge_entries_dir.mkdir(parents=True, exist_ok=True)
        paths.skills_dir.mkdir(parents=True, exist_ok=True)
        paths.files_blobs_dir.mkdir(parents=True, exist_ok=True)
        # 初始化索引文件
        if not paths.knowledge_index.exists():
            paths.knowledge_index.write_text("[]\n", encoding="utf-8")
        if not paths.files_index.exists():
            paths.files_index.write_text("[]\n", encoding="utf-8")
        if not paths.skill_registry.exists():
            initial_registry = {"role_id": role_id, "skills": []}
            paths.skill_registry.write_text(
                yaml.dump(initial_registry, default_flow_style=False, allow_unicode=True),
                encoding="utf-8",
            )
        return paths

    def role_exists(self, role_id: str) -> bool:
        """檢查角色三庫是否已初始化。"""
        paths = self.role_paths(role_id)
        return paths.root.exists() and paths.skills_dir.exists()

    # ── 知識庫 ──────────────────────────────────────────────────────────

    def add_knowledge(self, role_id: str, entry: dict[str, Any]) -> str:
        """向角色知識庫添加一條知識條目，返回條目 ID。"""
        paths = self.ensure_role_repos(role_id)
        entry_id = str(entry.get("id", "") or "").strip() or str(uuid.uuid4())[:8]
        record = {
            "id": entry_id,
            "title": str(entry.get("title", "") or "").strip(),
            "content": str(entry.get("content", "") or "").strip(),
            "domain": str(entry.get("domain", "") or "").strip(),
            "tags": [str(t).strip() for t in list(entry.get("tags", []) or []) if str(t).strip()],
            "created_at": str(entry.get("created_at", "") or _utc_now()),
            "metadata": dict(entry.get("metadata", {}) or {}),
        }
        # 寫入條目文件
        entry_path = paths.knowledge_entries_dir / f"{entry_id}.json"
        entry_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        # 更新索引
        index = self._load_json_list(paths.knowledge_index)
        index.append({"id": entry_id, "title": record["title"], "domain": record["domain"]})
        self._save_json_list(paths.knowledge_index, index)
        return entry_id

    def list_knowledge(self, role_id: str) -> list[dict[str, Any]]:
        """列出角色知識庫的所有條目（完整內容）。"""
        paths = self.role_paths(role_id)
        if not paths.knowledge_entries_dir.exists():
            return []
        entries: list[dict[str, Any]] = []
        for file in sorted(paths.knowledge_entries_dir.glob("*.json")):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    entries.append(data)
            except Exception:
                continue
        return entries

    def get_knowledge(self, role_id: str, entry_id: str) -> dict[str, Any] | None:
        """取得單條知識條目。"""
        paths = self.role_paths(role_id)
        entry_path = paths.knowledge_entries_dir / f"{entry_id}.json"
        if not entry_path.exists():
            return None
        try:
            data = json.loads(entry_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def remove_knowledge(self, role_id: str, entry_id: str) -> bool:
        """移除一條知識條目。"""
        paths = self.role_paths(role_id)
        entry_path = paths.knowledge_entries_dir / f"{entry_id}.json"
        if not entry_path.exists():
            return False
        entry_path.unlink()
        # 更新索引
        index = self._load_json_list(paths.knowledge_index)
        index = [item for item in index if str(item.get("id", "")) != entry_id]
        self._save_json_list(paths.knowledge_index, index)
        return True

    # ── 技能庫 ──────────────────────────────────────────────────────────

    def load_skill_registry(self, role_id: str) -> list[SkillRegistryEntry]:
        """載入角色技能註冊表。"""
        paths = self.role_paths(role_id)
        if not paths.skill_registry.exists():
            return []
        try:
            data = yaml.safe_load(paths.skill_registry.read_text(encoding="utf-8")) or {}
            raw_skills = list(data.get("skills", []) or [])
            return [
                SkillRegistryEntry.from_dict(item)
                for item in raw_skills
                if isinstance(item, dict) and str(item.get("skill_id", "")).strip()
            ]
        except Exception:
            return []

    def save_skill_registry(self, role_id: str, skills: list[SkillRegistryEntry]) -> None:
        """儲存角色技能註冊表。"""
        paths = self.ensure_role_repos(role_id)
        registry = {
            "role_id": role_id,
            "skills": [skill.to_dict() for skill in skills],
        }
        paths.skill_registry.write_text(
            yaml.dump(registry, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )

    def add_skill(self, role_id: str, skill: SkillRegistryEntry) -> None:
        """向角色技能庫添加一個技能。"""
        skills = self.load_skill_registry(role_id)
        # 去重
        existing_ids = {s.skill_id for s in skills}
        if skill.skill_id not in existing_ids:
            skills.append(skill)
            self.save_skill_registry(role_id, skills)

    def remove_skill(self, role_id: str, skill_id: str) -> bool:
        """從角色技能庫移除一個技能。"""
        skills = self.load_skill_registry(role_id)
        new_skills = [s for s in skills if s.skill_id != skill_id]
        if len(new_skills) == len(skills):
            return False
        self.save_skill_registry(role_id, new_skills)
        return True

    def list_role_skills(self, role_id: str, extra_skill_refs: list[str] | None = None) -> list[str]:
        """列出角色的所有技能 ID（技能庫註冊表 + 可選的 EmployeeConfig.skill_refs）。"""
        skills = [s.skill_id for s in self.load_skill_registry(role_id)]
        # 合併 EmployeeConfig.skill_refs（去重）
        for ref in list(extra_skill_refs or []):
            ref_str = str(ref or "").strip()
            if ref_str and ref_str not in skills:
                skills.append(ref_str)
        return skills

    def get_skill_set(self, role_id: str, extra_skill_refs: list[str] | None = None) -> set[str]:
        """取得角色的技能集合（用於技能缺口分析）。"""
        return set(self.list_role_skills(role_id, extra_skill_refs=extra_skill_refs))

    # ── 文件庫 ──────────────────────────────────────────────────────────

    def add_file(self, role_id: str, source_path: Path, metadata: dict[str, Any] | None = None) -> str:
        """將文件複製到角色文件庫，返回文件 ID。"""
        paths = self.ensure_role_repos(role_id)
        file_id = str(uuid.uuid4())[:8]
        source = Path(source_path)
        if not source.exists():
            raise FileNotFoundError(f"source file not found: {source_path}")
        # 複製到 blobs
        dest = paths.files_blobs_dir / f"{file_id}_{source.name}"
        shutil.copy2(source, dest)
        # 建立索引記錄
        record = {
            "id": file_id,
            "original_name": source.name,
            "stored_path": str(dest),
            "size_bytes": dest.stat().st_size,
            "added_at": _utc_now(),
            "metadata": dict(metadata or {}),
        }
        index = self._load_json_list(paths.files_index)
        index.append(record)
        self._save_json_list(paths.files_index, index)
        return file_id

    def list_files(self, role_id: str) -> list[dict[str, Any]]:
        """列出角色文件庫的所有文件記錄。"""
        paths = self.role_paths(role_id)
        return self._load_json_list(paths.files_index)

    def remove_file(self, role_id: str, file_id: str) -> bool:
        """從角色文件庫移除一個文件。"""
        paths = self.role_paths(role_id)
        index = self._load_json_list(paths.files_index)
        target = next((item for item in index if str(item.get("id", "")) == file_id), None)
        if not target:
            return False
        # 刪除實際文件
        stored_path = str(target.get("stored_path", "") or "")
        if stored_path:
            blob = Path(stored_path)
            if blob.exists():
                blob.unlink()
        # 更新索引
        index = [item for item in index if str(item.get("id", "")) != file_id]
        self._save_json_list(paths.files_index, index)
        return True

    # ── 內部工具 ──────────────────────────────────────────────────────────

    @staticmethod
    def _load_json_list(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    @staticmethod
    def _save_json_list(path: Path, data: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
