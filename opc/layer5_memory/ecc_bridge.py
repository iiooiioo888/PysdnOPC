"""ECC 整合橋接器 — 從 ECC 倉庫按需篩選並匯入技能、代理、規則到 OpenOPC。

職責說明：
    掃描 ECC (github.com/affaan-m/ECC) 倉庫的 skills/、agents/、rules/ 目錄，
    將 ECC 格式轉換為 OpenOPC 標準格式後寫入系統技能庫、人才模板庫。

關聯關係：
    - 被 opc/cli/app.py 的 skills-ecc-* 命令呼叫
    - 寫入結果由 opc/layer5_memory/skill_library.py 的 SkillLibrary 載入
    - 轉換邏輯參考 opc/layer5_memory/skill_importer.py 的標準化規則

使用範例：
    bridge = EccSkillBridge(opc_home)
    await bridge.prepare_source()
    available = bridge.list_available(pattern="python*")
    bridge.import_skills(["python-patterns", "python-testing"])

    agent_bridge = EccAgentBridge(opc_home, ecc_repo_path=ecc_repo)
    agents = agent_bridge.list_available()
    agent_bridge.import_agents(["code-reviewer", "planner"])

    rules_bridge = EccRulesBridge(opc_home, ecc_repo_path=ecc_repo)
    rules = rules_bridge.list_available(languages=["common", "python"])
    rules_bridge.import_rules(languages=["common", "python"])
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

        # Build normalized-name -> source directory map. ECC 部分技能的
        # frontmatter ``name`` 與其目錄名不一致（例如目錄 scientific-db-pubmed-database
        # 的 name 為 pubmed-database），因此必須以 frontmatter 正規化名定位來源，
        # 與 list_available() 的命名邏輯保持一致。
        name_to_dir: dict[str, Path] = {}
        if skills_dir.is_dir():
            for child in skills_dir.iterdir():
                if not child.is_dir():
                    continue
                skill_md = child / "SKILL.md"
                if not skill_md.exists():
                    continue
                fm, _ = self._parse_skill_md(skill_md)
                key = self.normalize_skill_name(
                    str(fm.get("name", child.name)).strip() or child.name
                )
                if key:
                    name_to_dir.setdefault(key, child)

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

            # Locate source skill directory by normalized frontmatter name,
            # falling back to a raw directory-name match.
            source_skill_dir = name_to_dir.get(normalized)
            if source_skill_dir is None:
                candidate = skills_dir / raw_name
                if candidate.is_dir():
                    source_skill_dir = candidate
            source_skill_md = (
                source_skill_dir / "SKILL.md" if source_skill_dir else None
            )
            if source_skill_md is None or not source_skill_md.exists():
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


# ======================================================================
# ECC Agent Bridge
# ======================================================================


@dataclass
class EccAgentInfo:
    """ECC 倉庫中一個代理的摘要資訊。"""

    name: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    model: str = ""
    source_path: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass
class EccAgentImportResult:
    """單個代理匯入的結果。"""

    agent_name: str
    prompt_path: str = ""
    success: bool = True
    message: str = ""
    skipped: bool = False
    employee_entry: dict[str, Any] = field(default_factory=dict)


class EccAgentBridge:
    """從 ECC agents/ 目錄掃描、轉換並匯入代理定義為 OpenOPC 人才模板。

    ECC agent 格式 (.md with YAML frontmatter):
        ---
        name: code-reviewer
        description: Reviews code for quality, security, and maintainability
        tools: Read, Grep, Glob, Bash
        model: opus
        ---
        You are a senior code reviewer...

    轉換為 OpenOPC:
        - .opc/prompts/talent/ecc-{name}.md（prompt 內容）
        - company_corporate_config.yaml 員工條目
    """

    def __init__(self, opc_home: Path, ecc_repo_path: Path | None = None) -> None:
        self.opc_home = Path(opc_home)
        self.prompts_dir = self.opc_home / "prompts" / "talent"
        self._ecc_repo_path = ecc_repo_path
        self._source_root: Path | None = ecc_repo_path

    def _require_source(self) -> Path:
        if self._source_root and self._source_root.exists():
            return self._source_root
        raise EccBridgeError(
            "ECC source not prepared. Provide ecc_repo_path or call prepare_source()."
        )

    async def prepare_source(self, repo_url: str = _ECC_DEFAULT_REPO) -> Path:
        """確保 ECC 來源可用（復用 EccSkillBridge 的 clone 邏輯）。"""
        if self._source_root and self._source_root.exists():
            agents_dir = self._source_root / "agents"
            if agents_dir.is_dir():
                return self._source_root
            raise EccBridgeError(
                f"Local ECC path does not contain an agents/ directory: {self._source_root}"
            )
        # Delegate to a temporary EccSkillBridge for cloning
        skill_bridge = EccSkillBridge(self.opc_home, ecc_repo_path=None)
        root = await skill_bridge.prepare_source(repo_url)
        self._source_root = root
        return root

    def list_available(self, pattern: str = "") -> list[EccAgentInfo]:
        """掃描 ECC agents/ 目錄，返回可用代理清單。"""
        source = self._require_source()
        agents_dir = source / "agents"
        if not agents_dir.is_dir():
            return []
        results: list[EccAgentInfo] = []

        for md_file in sorted(agents_dir.glob("*.md")):
            frontmatter, body = EccSkillBridge._parse_skill_md(md_file)
            name = str(frontmatter.get("name", md_file.stem)).strip() or md_file.stem
            normalized = EccSkillBridge.normalize_skill_name(name)
            description = str(frontmatter.get("description", "")).strip()
            tools_raw = frontmatter.get("tools", "")
            if isinstance(tools_raw, str):
                tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
            elif isinstance(tools_raw, list):
                tools = [str(t) for t in tools_raw]
            else:
                tools = []
            model = str(frontmatter.get("model", "")).strip()

            if pattern and not fnmatch.fnmatch(normalized, pattern):
                continue

            results.append(EccAgentInfo(
                name=normalized,
                description=description,
                tools=tools,
                model=model,
                source_path=str(md_file),
                frontmatter=frontmatter,
            ))

        return results

    def import_agents(
        self,
        names: list[str],
        *,
        overwrite: bool = False,
    ) -> list[EccAgentImportResult]:
        """批次匯入指定代理為 OpenOPC 人才模板。

        產出：
            - prompts/talent/ecc-{name}.md — 系統提示詞
            - 返回 employee_entry dict 供 company_corporate_config.yaml 使用
        """
        source = self._require_source()
        agents_dir = source / "agents"
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        results: list[EccAgentImportResult] = []

        # Build name -> file map
        name_to_file: dict[str, Path] = {}
        if agents_dir.is_dir():
            for md_file in agents_dir.glob("*.md"):
                fm, _ = EccSkillBridge._parse_skill_md(md_file)
                key = EccSkillBridge.normalize_skill_name(
                    str(fm.get("name", md_file.stem)).strip() or md_file.stem
                )
                if key:
                    name_to_file.setdefault(key, md_file)

        for raw_name in names:
            normalized = EccSkillBridge.normalize_skill_name(raw_name)
            if not normalized:
                results.append(EccAgentImportResult(
                    agent_name=raw_name, success=False,
                    message=f"Invalid agent name: {raw_name}",
                ))
                continue

            source_file = name_to_file.get(normalized)
            if source_file is None:
                # Fallback: try direct filename
                candidate = agents_dir / f"{raw_name}.md"
                if candidate.exists():
                    source_file = candidate
            if source_file is None or not source_file.exists():
                results.append(EccAgentImportResult(
                    agent_name=normalized, success=False,
                    message=f"Agent file not found in ECC source for '{raw_name}'",
                ))
                continue

            target_path = self.prompts_dir / f"ecc-{normalized}.md"
            if target_path.exists() and not overwrite:
                results.append(EccAgentImportResult(
                    agent_name=normalized,
                    prompt_path=str(target_path),
                    success=True,
                    message="Already exists, skipped (use overwrite=True to replace)",
                    skipped=True,
                ))
                continue

            try:
                frontmatter, body = EccSkillBridge._parse_skill_md(source_file)
                description = str(frontmatter.get("description", "")).strip()
                tools_raw = frontmatter.get("tools", "")
                if isinstance(tools_raw, str):
                    tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
                elif isinstance(tools_raw, list):
                    tools = [str(t) for t in tools_raw]
                else:
                    tools = []
                model = str(frontmatter.get("model", "")).strip()

                # Write prompt file
                prompt_content = self._build_prompt(normalized, description, body)
                target_path.write_text(prompt_content, encoding="utf-8")

                # Build employee entry for company config
                employee_entry = self._build_employee_entry(
                    normalized, description, tools, model
                )

                results.append(EccAgentImportResult(
                    agent_name=normalized,
                    prompt_path=str(target_path),
                    success=True,
                    message="Imported successfully",
                    employee_entry=employee_entry,
                ))
                logger.info(f"ECC agent imported: {normalized} -> {target_path}")
            except Exception as exc:
                results.append(EccAgentImportResult(
                    agent_name=normalized, success=False,
                    message=f"Import failed: {exc}",
                ))

        return results

    def _build_prompt(self, name: str, description: str, body: str) -> str:
        """建構人才模板 prompt 文件內容。"""
        header = f"# ECC Agent: {name}\n\n"
        if description:
            header += f"> {description}\n\n"
        header += "---\n\n"
        content = body.strip() if body.strip() else f"You are a specialized {name} agent."
        return header + content + "\n"

    def _build_employee_entry(
        self, name: str, description: str, tools: list[str], model: str
    ) -> dict[str, Any]:
        """建構 company_corporate_config.yaml 的員工條目。"""
        # Infer category from agent name
        category = "engineering"
        if any(kw in name for kw in ("review", "security", "test")):
            category = "quality"
        elif any(kw in name for kw in ("doc", "write", "content")):
            category = "content"
        elif any(kw in name for kw in ("plan", "architect")):
            category = "management"

        return {
            "employee_id": f"ecc-{name}",
            "name": name.replace("-", " ").title(),
            "description": description or f"ECC agent: {name}",
            "category": category,
            "role_id": f"ecc_{name.replace('-', '_')}",
            "seniority": "senior",
            "status": "active",
            "tags": ["ecc", "imported"] + tools[:3],
            "prompt_refs": [f"prompts/talent/ecc-{name}.md"],
            "skill_refs": [],
            "preferred_external_agent": None,
            "metadata": {
                "source": "ecc",
                "repo": "https://github.com/affaan-m/ECC",
                "ecc_model": model,
                "ecc_tools": tools,
                "imported_at": datetime.now(timezone.utc).isoformat(),
            },
        }


# ======================================================================
# ECC Rules Bridge
# ======================================================================


@dataclass
class EccRuleInfo:
    """ECC 倉庫中一個規則文件的摘要資訊。"""

    name: str
    language: str
    description: str = ""
    source_path: str = ""


@dataclass
class EccRuleImportResult:
    """單個規則匯入的結果。"""

    rule_name: str
    skill_path: str = ""
    success: bool = True
    message: str = ""
    skipped: bool = False


# ECC rules/ 目錄中已知的語言子目錄
ECC_RULE_LANGUAGES = frozenset({
    "common", "typescript", "python", "golang", "swift", "php", "arkts",
})


class EccRulesBridge:
    """從 ECC rules/ 目錄匯入編碼準則為 OpenOPC always-on 技能。

    ECC rules 結構:
        rules/
          common/         # 通用準則
            coding-style.md
            git-workflow.md
            testing.md
          python/         # Python 專用
            ...
          typescript/     # TypeScript 專用
            ...

    轉換為:
        .opc/skills/ecc-rule-{lang}-{name}/SKILL.md (always: true)
    """

    def __init__(self, opc_home: Path, ecc_repo_path: Path | None = None) -> None:
        self.opc_home = Path(opc_home)
        self.system_skills_dir = self.opc_home / "skills"
        self._ecc_repo_path = ecc_repo_path
        self._source_root: Path | None = ecc_repo_path

    def _require_source(self) -> Path:
        if self._source_root and self._source_root.exists():
            return self._source_root
        raise EccBridgeError(
            "ECC source not prepared. Provide ecc_repo_path or call prepare_source()."
        )

    async def prepare_source(self, repo_url: str = _ECC_DEFAULT_REPO) -> Path:
        """確保 ECC 來源可用。"""
        if self._source_root and self._source_root.exists():
            rules_dir = self._source_root / "rules"
            if rules_dir.is_dir():
                return self._source_root
            raise EccBridgeError(
                f"Local ECC path does not contain a rules/ directory: {self._source_root}"
            )
        skill_bridge = EccSkillBridge(self.opc_home, ecc_repo_path=None)
        root = await skill_bridge.prepare_source(repo_url)
        self._source_root = root
        return root

    def list_available(
        self, languages: list[str] | None = None
    ) -> list[EccRuleInfo]:
        """掃描 ECC rules/ 目錄，返回可用規則清單。

        Args:
            languages: 篩選語言（如 ["common", "python"]）。None 表示全部。
        """
        source = self._require_source()
        rules_dir = source / "rules"
        if not rules_dir.is_dir():
            return []

        lang_filter = set(languages) if languages else None
        results: list[EccRuleInfo] = []

        for lang_dir in sorted(rules_dir.iterdir()):
            if not lang_dir.is_dir():
                continue
            lang = lang_dir.name
            if lang_filter and lang not in lang_filter:
                continue

            for md_file in sorted(lang_dir.glob("*.md")):
                if md_file.name.upper() == "README.MD":
                    continue
                name = EccSkillBridge.normalize_skill_name(md_file.stem)
                # Try to extract first heading as description
                description = self._extract_description(md_file)
                results.append(EccRuleInfo(
                    name=f"{lang}-{name}",
                    language=lang,
                    description=description,
                    source_path=str(md_file),
                ))

        return results

    def import_rules(
        self,
        languages: list[str] | None = None,
        *,
        overwrite: bool = False,
    ) -> list[EccRuleImportResult]:
        """批次匯入規則為 always-on 技能。

        Args:
            languages: 要匯入的語言列表。None 表示全部。
            overwrite: 若目標已存在是否覆蓋。
        """
        available = self.list_available(languages=languages)
        self.system_skills_dir.mkdir(parents=True, exist_ok=True)
        results: list[EccRuleImportResult] = []

        for rule_info in available:
            skill_name = f"ecc-rule-{rule_info.name}"
            target_dir = self.system_skills_dir / skill_name
            target_md = target_dir / "SKILL.md"

            if target_md.exists() and not overwrite:
                results.append(EccRuleImportResult(
                    rule_name=skill_name,
                    skill_path=str(target_md),
                    success=True,
                    message="Already exists, skipped",
                    skipped=True,
                ))
                continue

            try:
                body = Path(rule_info.source_path).read_text(encoding="utf-8")
                frontmatter: dict[str, Any] = {
                    "name": skill_name,
                    "description": rule_info.description or f"ECC rule: {rule_info.name}",
                    "always": True,
                    "metadata": {
                        "imported_from": {
                            "source": "ecc-rules",
                            "repo": "https://github.com/affaan-m/ECC",
                            "language": rule_info.language,
                            "original_file": Path(rule_info.source_path).name,
                            "imported_at": datetime.now(timezone.utc).isoformat(),
                        },
                    },
                }

                target_dir.mkdir(parents=True, exist_ok=True)
                target_md.write_text(
                    _render_skill_document(frontmatter, body),
                    encoding="utf-8",
                )

                results.append(EccRuleImportResult(
                    rule_name=skill_name,
                    skill_path=str(target_md),
                    success=True,
                    message="Imported successfully",
                ))
                logger.info(f"ECC rule imported: {skill_name} -> {target_md}")
            except Exception as exc:
                results.append(EccRuleImportResult(
                    rule_name=skill_name,
                    success=False,
                    message=f"Import failed: {exc}",
                ))

        return results

    @staticmethod
    def _extract_description(md_file: Path) -> str:
        """從 Markdown 文件提取第一個標題作為描述。"""
        try:
            text = md_file.read_text(encoding="utf-8")
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("# "):
                    return stripped[2:].strip()
            # Fallback: first non-empty line
            for line in text.split("\n"):
                if line.strip():
                    return line.strip()[:120]
        except OSError:
            pass
        return ""
