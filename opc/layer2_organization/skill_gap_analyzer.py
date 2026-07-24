"""技能缺口分析器。

實現「架構優先、按需補人」策略的核心分析邏輯：
- 檢查公司架構是否已存在（角色非空）
- 分析每個角色的技能是否滿足任務需求
- 產出缺口報告供 CompanyRecruiter 決策
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

__all__ = [
    "SkillGapReport",
    "analyze_skill_gaps",
    "is_structure_ready",
]


@dataclass
class SkillGapReport:
    """技能缺口分析報告。"""

    structure_ready: bool = False
    satisfied_roles: list[str] = field(default_factory=list)
    gap_roles: list[str] = field(default_factory=list)
    missing_skills: dict[str, list[str]] = field(default_factory=dict)
    total_roles: int = 0

    @property
    def has_gaps(self) -> bool:
        return len(self.gap_roles) > 0

    @property
    def all_satisfied(self) -> bool:
        return self.structure_ready and not self.has_gaps


def is_structure_ready(org_engine: Any) -> bool:
    """檢查公司架構是否已就緒（至少有一個非通用角色）。"""
    if not org_engine:
        return False
    try:
        agents = list(org_engine.list_agents())
    except Exception:
        return False
    role_ids = {
        str(getattr(agent, "role_id", "") or "").strip()
        for agent in agents
        if str(getattr(agent, "role_id", "") or "").strip()
        and str(getattr(agent, "role_id", "") or "").strip() != "task_generalist"
    }
    return len(role_ids) > 0


def _collect_role_skills_from_engine(org_engine: Any, role_id: str) -> set[str]:
    """從 org_engine 收集角色已有的技能（skill_refs + domains）。"""
    skills: set[str] = set()
    try:
        agent = org_engine.get_agent(role_id) if hasattr(org_engine, "get_agent") else None
        if agent:
            # 從 skill_refs 收集
            for ref in list(getattr(agent, "skill_refs", []) or []):
                ref_str = str(ref or "").strip()
                if ref_str:
                    skills.add(ref_str)
            # 從 domains 收集（作為模糊技能匹配）
            for domain in list(getattr(agent, "domains", []) or []):
                domain_str = str(domain or "").strip().lower()
                if domain_str:
                    skills.add(domain_str)
        # 從員工列表收集
        employees = org_engine.list_employees(role_id=role_id) if hasattr(org_engine, "list_employees") else []
        for emp in employees:
            for ref in list(getattr(emp, "skill_refs", []) or []):
                ref_str = str(ref or "").strip()
                if ref_str:
                    skills.add(ref_str)
            for domain in list(getattr(emp, "domains", []) or []):
                domain_str = str(domain or "").strip().lower()
                if domain_str:
                    skills.add(domain_str)
    except Exception:
        logger.debug(f"failed to collect skills for role {role_id}")
    return skills


def _fuzzy_match(required: str, available: set[str]) -> bool:
    """模糊匹配：required 技能是否在 available 中有近似匹配。"""
    required_lower = required.strip().lower()
    if required_lower in available:
        return True
    # 簡單前綴/包含匹配
    for skill in available:
        if required_lower in skill or skill in required_lower:
            return True
    return False


def analyze_skill_gaps(
    org_engine: Any,
    required_skills: dict[str, list[str]] | None = None,
    *,
    repo_manager: Any | None = None,
    skill_match_mode: str = "fuzzy",
) -> SkillGapReport:
    """分析公司架構的技能缺口。

    Args:
        org_engine: 組織引擎（提供 list_agents / get_agent / list_employees）
        required_skills: 每個角色所需的技能映射 {role_id: [skill_id, ...]}
            若為 None 或空，則僅檢查架構是否存在。
        repo_manager: RoleRepositoryManager 實例（可選，用於讀取角色技能庫）
        skill_match_mode: 匹配模式 "strict"（精確）或 "fuzzy"（模糊）

    Returns:
        SkillGapReport — 包含架構狀態和缺口詳情
    """
    # 檢查架構是否就緒
    if not is_structure_ready(org_engine):
        return SkillGapReport(structure_ready=False)

    # 收集所有角色
    try:
        agents = list(org_engine.list_agents())
    except Exception:
        return SkillGapReport(structure_ready=False)

    role_ids = [
        str(getattr(agent, "role_id", "") or "").strip()
        for agent in agents
        if str(getattr(agent, "role_id", "") or "").strip()
        and str(getattr(agent, "role_id", "") or "").strip() != "task_generalist"
    ]

    if not role_ids:
        return SkillGapReport(structure_ready=False)

    # 如果沒有指定所需技能，架構存在即視為滿足
    if not required_skills:
        return SkillGapReport(
            structure_ready=True,
            satisfied_roles=list(role_ids),
            total_roles=len(role_ids),
        )

    # 分析每個角色的技能缺口
    satisfied: list[str] = []
    gaps: list[str] = []
    missing: dict[str, list[str]] = {}
    use_fuzzy = skill_match_mode != "strict"

    for role_id in role_ids:
        required = list(required_skills.get(role_id, []) or [])
        if not required:
            # 無特定技能需求 → 視為滿足
            satisfied.append(role_id)
            continue

        # 收集角色已有技能
        available = _collect_role_skills_from_engine(org_engine, role_id)

        # 合併角色技能庫（如果 repo_manager 可用）
        if repo_manager and hasattr(repo_manager, "get_skill_set"):
            try:
                repo_skills = repo_manager.get_skill_set(role_id)
                available = available | repo_skills
            except Exception:
                pass

        # 比對
        role_missing: list[str] = []
        for skill in required:
            skill_str = str(skill or "").strip()
            if not skill_str:
                continue
            if use_fuzzy:
                if not _fuzzy_match(skill_str, available):
                    role_missing.append(skill_str)
            else:
                if skill_str not in available:
                    role_missing.append(skill_str)

        if role_missing:
            gaps.append(role_id)
            missing[role_id] = role_missing
        else:
            satisfied.append(role_id)

    return SkillGapReport(
        structure_ready=True,
        satisfied_roles=satisfied,
        gap_roles=gaps,
        missing_skills=missing,
        total_roles=len(role_ids),
    )
