"""ECC 技能橋接器 — 從 ECC 倉庫按需篩選並匯入技能到 OpenOPC。

職責說明：
    掃描 ECC (github.com/affaan-m/ECC) 倉庫的 skills/ 目錄，
    將 ECC 格式的 SKILL.md 轉換為 OpenOPC 標準格式後寫入系統技能庫。

關聯關係：
    - 被 opc/cli/app.py 的 skills-ecc-list / skills-ecc-import 命令呼叫
    - 寫入結果由 opc/layer5_memory/skill_library.py 的 SkillLibrary 載入
    - 轉換邏輯參考 opc/layer5_memory/skill_importer.py 的標準化規則

使用範例：
    bridge = EccSkillBridge(opc_home)
    await bridge.prepare_source()
    available = bridge.list_available(pattern="python*")
    bridge.import_skills(["python-patterns", "python-testing"])
"""

from __future__ import annotations

import asyncio
import fnmatch
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

# OpenOPC 允許的 SKILL.md frontmatter 頂層鍵
_ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "metadata",
    "always",
    "license",
    "allowed-tools",
    "homepage",
}

MAX_SKILL_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024

_ECC_DEFAULT_REPO = "https://github.com/affaan-m/ECC.git"


@dataclass
class EccSkillInfo:
    """ECC 倉庫中一個技能的摘要資訊。"""

    name: str
    description: str = ""
    source_path: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass
class EccImportResult:
    """單個技能匯入的結果。"""

    skill_name: str
    skill_path: str
    success: bool = True
    message: str = ""
    skipped: bool = False


class EccBridgeError(RuntimeError):
    """ECC 橋接操作失敗時拋出。"""


class EccSkillBridge:
    """從 ECC 倉庫掃描、篩選、轉換並匯入技能到 OpenOPC 系統技能庫。"""

    def __init__(self, opc_home: Path, ecc_repo_path: Path | None = None) -> None:
        self.opc_home = Path(opc_home)
        self.system_skills_dir = self.opc_home / "skills"
        self._ecc_repo_path = ecc_repo_path
        self._source_root: Path | None = ecc_repo_path

    # ------------------------------------------------------------------
    # Source preparation
    # ------------------------------------------------------------------

    async def prepare_source(
        self, repo_url: str = _ECC_DEFAULT_REPO
    ) -> Path:
        """確保 ECC 來源可用。若已有本地路徑則直接使用，否則 shallow clone。"""
        if self._source_root and self._source_root.exists():
            skills_dir = self._source_root / "skills"
            if skills_dir.is_dir():
                logger.info(f"Using local ECC source: {self._source_root}")
                return self._source_root
            raise EccBridgeError(
                f"Local ECC path does not contain a skills/ directory: {self._source_root}"
            )

        tmp_dir = self.opc_home / ".tmp-ecc"
        if tmp_dir.exists():
            skills_dir = tmp_dir / "skills"
            if skills_dir.is_dir():
                logger.info(f"Reusing cached ECC clone: {tmp_dir}")
                self._source_root = tmp_dir
                return tmp_dir
            shutil.rmtree(tmp_dir, ignore_errors=True)

        tmp_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Cloning ECC repository (shallow): {repo_url}")
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", repo_url, str(tmp_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            msg = stderr.decode("utf-8", errors="replace").strip()
            raise EccBridgeError(f"git clone failed: {msg}")

        if not (tmp_dir / "skills").is_dir():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise EccBridgeError("Cloned ECC repo does not contain a skills/ directory.")

        self._source_root = tmp_dir
        return tmp_dir

    # ------------------------------------------------------------------
    # Listing / filtering
    # ------------------------------------------------------------------

    def list_available(self, pattern: str = "", category: str = "") -> list[EccSkillInfo]:
        """掃描 ECC skills/ 目錄，返回可用技能清單。

        Args:
            pattern: fnmatch glob 模式篩選技能名稱（如 "python*"、"*-tdd"）
            category: 關鍵字篩選（在 name + description 中搜尋）
        """
        source = self._require_source()
        skills_dir = source / "skills"
        results: list[EccSkillInfo] = []

        for child in sorted(skills_dir.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.exists():
                continue
            frontmatter, _body = self._parse_skill_md(skill_md)
            name = str(frontmatter.get("name", child.name)).strip() or child.name
            name = self.normalize_skill_name(name)
            description = str(frontmatter.get("description", "")).strip()

            # Apply pattern filter
            if pattern and not fnmatch.fnmatch(name, pattern):
                continue
            # Apply category/keyword filter
            if category:
                haystack = f"{name} {description}".lower()
                if category.lower() not in haystack:
                    continue

            results.append(EccSkillInfo(
                name=name,
                description=description,
                source_path=str(skill_md),
                frontmatter=frontmatter,
            ))

        return results

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def import_skills(
        self,
        names: list[str],
        *,
        always: bool = False,
        overwrite: bool = False,
    ) -> list[EccImportResult]:
        """批次匯入指定技能到 opc_home/skills/<name>/SKILL.md。

        Args:
            names: 要匯入的技能名稱列表
            always: 是否將技能標記為 always_on
            overwrite: 若目標已存在是否覆蓋（預設跳過）
        """
        source = self._require_source()
        skills_dir = source / "skills"
        self.system_skills_dir.mkdir(parents=True, exist_ok=True)
        results: list[EccImportResult] = []

        for raw_name in names:
            normalized = self.normalize_skill_name(raw_name)
            if not normalized:
                results.append(EccImportResult(
                    skill_name=raw_name,
                    skill_path="",
                    success=False,
                    message=f"Invalid skill name: {raw_name}",
                ))
                continue

            # Locate source skill directory
            source_skill_dir = skills_dir / normalized
            if not source_skill_dir.exists():
                # Try original raw name as directory
                source_skill_dir = skills_dir / raw_name
            source_skill_md = source_skill_dir / "SKILL.md"
            if not source_skill_md.exists():
                results.append(EccImportResult(
                    skill_name=normalized,
                    skill_path="",
                    success=False,
                    message=f"SKILL.md not found in ECC source for '{raw_name}'",
                ))
                continue

            target_dir = self.system_skills_dir / normalized
            if target_dir.exists() and not overwrite:
                results.append(EccImportResult(
                    skill_name=normalized,
                    skill_path=str(target_dir / "SKILL.md"),
                    success=True,
                    message="Already exists, skipped (use overwrite=True to replace)",
                    skipped=True,
                ))
                continue

            try:
                frontmatter, body = self._convert_skill(
                    source_skill_md, always=always
                )
                # Ensure name matches target directory
                frontmatter["name"] = normalized

                target_dir.mkdir(parents=True, exist_ok=True)
                target_md = target_dir / "SKILL.md"
                target_md.write_text(
                    _render_skill_document(frontmatter, body),
                    encoding="utf-8",
                )

                # Copy allowed resource directories
                for res_dir in ("scripts", "references", "assets"):
                    src_res = source_skill_dir / res_dir
                    if src_res.is_dir():
                        dst_res = target_dir / res_dir
                        if dst_res.exists():
                            shutil.rmtree(dst_res)
                        shutil.copytree(src_res, dst_res)

                results.append(EccImportResult(
                    skill_name=normalized,
                    skill_path=str(target_md),
                    success=True,
                    message="Imported successfully",
                ))
                logger.info(f"ECC skill imported: {normalized} -> {target_md}")
            except Exception as exc:
                results.append(EccImportResult(
                    skill_name=normalized,
                    skill_path="",
                    success=False,
                    message=f"Import failed: {exc}",
                ))

        return results

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def _convert_skill(self, ecc_skill_md: Path, *, always: bool) -> tuple[dict[str, Any], str]:
        """轉換 ECC SKILL.md 為 OpenOPC 標準 frontmatter + body。"""
        frontmatter, body = self._parse_skill_md(ecc_skill_md)

        name = self.normalize_skill_name(
            str(frontmatter.get("name", ecc_skill_md.parent.name))
        )
        description = str(frontmatter.get("description", "")).strip()
        if len(description) > MAX_DESCRIPTION_LENGTH:
            description = description[: MAX_DESCRIPTION_LENGTH - 3].rstrip() + "..."
        if not description:
            description = f"ECC skill: {name}"

        # Build normalized frontmatter
        normalized: dict[str, Any] = {
            "name": name,
            "description": description,
        }
        if always:
            normalized["always"] = True

        # Collect extra ECC frontmatter into metadata
        metadata: dict[str, Any] = {}
        if isinstance(frontmatter.get("metadata"), dict):
            metadata.update(frontmatter["metadata"])

        imported_extra: dict[str, Any] = {}
        for key, value in frontmatter.items():
            if key in {"name", "description", "always", "metadata"}:
                continue
            if key in {"license", "allowed-tools", "homepage"} and value not in (None, ""):
                normalized[key] = value
            else:
                imported_extra[key] = value

        metadata["imported_from"] = {
            "source": "ecc",
            "repo": "https://github.com/affaan-m/ECC",
            "original_name": str(frontmatter.get("name", ecc_skill_md.parent.name)),
            "imported_at": datetime.now(timezone.utc).isoformat(),
        }
        if imported_extra:
            metadata["imported_frontmatter"] = imported_extra
        if metadata:
            normalized["metadata"] = metadata

        # Ensure body is non-empty
        body = body.strip()
        if not body:
            body = f"# {name}\n\nImported from ECC. Add project-specific guidance as needed.\n"

        return normalized, body

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_source(self) -> Path:
        if self._source_root and self._source_root.exists():
            return self._source_root
        raise EccBridgeError(
            "ECC source not prepared. Call prepare_source() first or provide ecc_repo_path."
        )

    @staticmethod
    def _parse_skill_md(path: Path) -> tuple[dict[str, Any], str]:
        """解析 SKILL.md 的 YAML frontmatter 和 body。"""
        text = path.read_text(encoding="utf-8")
        if text.startswith("---"):
            parts = text.split("\n")
            for index in range(1, len(parts)):
                if parts[index].strip() == "---":
                    frontmatter_text = "\n".join(parts[1:index])
                    body = "\n".join(parts[index + 1:]).lstrip("\n")
                    try:
                        frontmatter = yaml.safe_load(frontmatter_text) or {}
                    except yaml.YAMLError:
                        frontmatter = {}
                    return frontmatter if isinstance(frontmatter, dict) else {}, body
        return {}, text

    @staticmethod
    def normalize_skill_name(raw: str) -> str:
        """將技能名稱標準化為 hyphen-case。"""
        normalized = raw.strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
        normalized = normalized.strip("-")
        normalized = re.sub(r"-{2,}", "-", normalized)
        return normalized[:MAX_SKILL_NAME_LENGTH]

    def cleanup_temp(self) -> None:
        """清除暫存的 ECC clone 目錄。"""
        tmp_dir = self.opc_home / ".tmp-ecc"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info("Cleaned up temporary ECC clone.")


def _render_skill_document(frontmatter: dict[str, Any], body: str) -> str:
    """渲染標準化的 SKILL.md 文件內容。"""
    fm = yaml.dump(
        frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False
    ).strip()
    return f"---\n{fm}\n---\n\n{body.rstrip()}\n"
