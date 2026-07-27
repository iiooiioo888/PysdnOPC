"""本能式持續學習引擎 — 從任務執行中自動提取模式並進化為技能。

職責說明：
    基於 ECC Continuous Learning v2 的概念，從完成的 session 中提取
    可複用的模式（instinct），通過信心評分累積強化，最終聚類蒸餾為
    正式的 OpenOPC 技能。

關聯關係：
    - 被 opc/layer5_memory/employee_evolution.py 在反思時觸發
    - 被 opc/layer5_memory/history_compactor.py 在壓縮時觸發
    - 蒸餾產出寫入 .opc/skills/ 由 SkillLibrary 載入
    - 被 opc/cli/app.py 的 instincts 命令組呼叫

使用範例：
    engine = InstinctEngine(opc_home)
    new_instincts = await engine.extract_from_session("sess-1", messages)
    engine.reinforce("inst-xxx", "session-2 also used this pattern")
    skill_name = engine.evolve_to_skill(["inst-xxx", "inst-yyy"])
    engine.prune_expired(max_age_days=90)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

# 本能信心閾值：超過此值才可被蒸餾為技能
EVOLVE_CONFIDENCE_THRESHOLD = 0.8

# 信心強化增量
REINFORCE_INCREMENT = 0.1

# 信心衰減：每次提取時未匹配到的本能微幅衰減
DECAY_FACTOR = 0.02

# 本能類別
INSTINCT_CATEGORIES = frozenset({
    "coding", "testing", "deployment", "communication",
    "debugging", "architecture", "workflow", "security",
})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_instinct_id() -> str:
    return f"inst-{uuid.uuid4().hex[:10]}"


@dataclass
class Instinct:
    """一個從任務執行中提取的可複用模式。"""

    id: str
    pattern: str
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.3
    category: str = "coding"
    created_at: str = ""
    last_reinforced: str = ""
    reinforcement_count: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionResult:
    """一次 session 模式提取的結果。"""

    session_id: str
    new_instincts: list[Instinct] = field(default_factory=list)
    reinforced_ids: list[str] = field(default_factory=list)
    total_patterns_found: int = 0


class InstinctEngine:
    """從任務執行中自動提取模式，累積信心，進化為可复用技能。

    儲存結構：
        {opc_home}/memory/instincts.json — 所有本能的 JSON 儲存

    生命週期：
        1. extract_from_session() — 從 session 提取新模式或強化已有模式
        2. reinforce() — 手動強化特定本能
        3. evolve_to_skill() — 將高信心本能聚類蒸餾為正式技能
        4. prune_expired() — 清除長期未強化的過期本能
    """

    def __init__(self, opc_home: Path, llm: Any | None = None) -> None:
        """初始化本能引擎。

        Args:
            opc_home: OPC 主目錄路徑。
            llm: 可選的 LLM 提供者（用於智慧提取）。若為 None 則使用規則式提取。
        """
        self.opc_home = Path(opc_home)
        self._llm = llm
        self._store_path = self.opc_home / "memory" / "instincts.json"
        self._skills_dir = self.opc_home / "skills"
        self._instincts: dict[str, Instinct] = {}
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract_from_session(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> ExtractionResult:
        """從完成的 session 中提取模式。

        使用 LLM（若可用）或規則式方法分析 session 訊息，
        識別可複用的模式並建立新本能或強化已有本能。

        Args:
            session_id: 工作階段 ID。
            messages: session 訊息列表（每條含 role, content 欄位）。

        Returns:
            ExtractionResult — 提取結果摘要。
        """
        result = ExtractionResult(session_id=session_id)

        if not messages:
            return result

        # Extract patterns (rule-based fallback when no LLM)
        patterns = self._extract_patterns(messages)
        result.total_patterns_found = len(patterns)

        for pattern_text, category in patterns:
            # Check if similar instinct already exists
            existing = self._find_similar(pattern_text)
            if existing:
                self.reinforce(existing.id, f"session:{session_id}")
                result.reinforced_ids.append(existing.id)
            else:
                instinct = Instinct(
                    id=_generate_instinct_id(),
                    pattern=pattern_text,
                    evidence=[f"session:{session_id}"],
                    confidence=0.3,
                    category=category,
                    created_at=_utc_now_iso(),
                    last_reinforced=_utc_now_iso(),
                )
                self._instincts[instinct.id] = instinct
                result.new_instincts.append(instinct)

        self._save()
        if result.new_instincts or result.reinforced_ids:
            logger.info(
                f"InstinctEngine: session {session_id} → "
                f"{len(result.new_instincts)} new, {len(result.reinforced_ids)} reinforced"
            )
        return result

    def reinforce(self, instinct_id: str, evidence: str) -> bool:
        """強化已有本能的信心分數。

        Args:
            instinct_id: 本能 ID。
            evidence: 新證據描述（如 session ID 或任務描述）。

        Returns:
            bool — 是否強化成功。
        """
        instinct = self._instincts.get(instinct_id)
        if instinct is None:
            return False

        instinct.confidence = min(1.0, instinct.confidence + REINFORCE_INCREMENT)
        instinct.reinforcement_count += 1
        instinct.last_reinforced = _utc_now_iso()
        if evidence not in instinct.evidence:
            instinct.evidence.append(evidence)
        self._save()
        return True

    def evolve_to_skill(self, instinct_ids: list[str]) -> str:
        """將相關本能聚類並蒸餾為正式技能。

        僅接受信心分數 >= EVOLVE_CONFIDENCE_THRESHOLD 的本能。
        產出寫入 .opc/skills/learned-{slug}/SKILL.md。

        Args:
            instinct_ids: 要蒸餾的本能 ID 列表。

        Returns:
            str — 生成的技能名稱（空字串表示失敗）。
        """
        eligible: list[Instinct] = []
        for iid in instinct_ids:
            inst = self._instincts.get(iid)
            if inst and inst.confidence >= EVOLVE_CONFIDENCE_THRESHOLD:
                eligible.append(inst)

        if not eligible:
            logger.warning("InstinctEngine: no eligible instincts for evolution")
            return ""

        # Generate skill name from categories
        categories = sorted({inst.category for inst in eligible})
        slug = "-".join(categories[:2]) or "general"
        skill_name = f"learned-{slug}-{uuid.uuid4().hex[:6]}"

        # Build skill content
        patterns_text = "\n".join(
            f"- {inst.pattern} (confidence: {inst.confidence:.2f}, "
            f"reinforced {inst.reinforcement_count}x)"
            for inst in eligible
        )
        body = (
            f"# Learned Skill: {slug}\n\n"
            f"Auto-evolved from {len(eligible)} instincts.\n\n"
            f"## Patterns\n\n{patterns_text}\n\n"
            f"## Evidence\n\n"
            + "\n".join(
                f"- {ev}" for inst in eligible for ev in inst.evidence[:3]
            )
            + "\n"
        )

        frontmatter = {
            "name": skill_name,
            "description": f"Learned skill evolved from {len(eligible)} instincts ({slug})",
            "always": False,
            "metadata": {
                "source": "instinct-engine",
                "evolved_from": [inst.id for inst in eligible],
                "evolved_at": _utc_now_iso(),
            },
        }

        # Write skill file
        import yaml
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        skill_dir = self._skills_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        fm_text = yaml.dump(
            frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False
        ).strip()
        (skill_dir / "SKILL.md").write_text(
            f"---\n{fm_text}\n---\n\n{body}", encoding="utf-8"
        )

        # Mark evolved instincts as superseded
        for inst in eligible:
            inst.metadata["evolved_to"] = skill_name
            inst.metadata["status"] = "evolved"

        self._save()
        logger.info(f"InstinctEngine: evolved {len(eligible)} instincts -> {skill_name}")
        return skill_name

    def prune_expired(self, max_age_days: int = 90) -> int:
        """清除過期未強化的本能。

        Args:
            max_age_days: 最大允許天數（從 last_reinforced 算起）。

        Returns:
            int — 被清除的本能數量。
        """
        now = datetime.now(timezone.utc)
        to_remove: list[str] = []

        for iid, inst in self._instincts.items():
            if inst.metadata.get("status") == "evolved":
                continue
            try:
                last = datetime.fromisoformat(inst.last_reinforced)
                age_days = (now - last).days
                if age_days > max_age_days:
                    to_remove.append(iid)
            except (ValueError, TypeError):
                to_remove.append(iid)

        for iid in to_remove:
            del self._instincts[iid]

        if to_remove:
            self._save()
            logger.info(f"InstinctEngine: pruned {len(to_remove)} expired instincts")
        return len(to_remove)

    def status(self) -> list[Instinct]:
        """列出所有本能及其信心分數（按信心降序）。"""
        instincts = list(self._instincts.values())
        instincts.sort(key=lambda i: i.confidence, reverse=True)
        return instincts

    def get(self, instinct_id: str) -> Instinct | None:
        """按 ID 取得單個本能。"""
        return self._instincts.get(instinct_id)

    def export_instincts(self) -> list[dict[str, Any]]:
        """匯出所有本能為可序列化的 dict 列表。"""
        return [asdict(inst) for inst in self._instincts.values()]

    def import_instincts(self, data: list[dict[str, Any]]) -> int:
        """從外部匯入本能列表。

        Args:
            data: 本能 dict 列表（需含 id, pattern 欄位）。

        Returns:
            int — 成功匯入的數量。
        """
        count = 0
        for item in data:
            iid = item.get("id", "")
            if not iid or iid in self._instincts:
                continue
            try:
                inst = Instinct(
                    id=iid,
                    pattern=str(item.get("pattern", "")),
                    evidence=item.get("evidence", []),
                    confidence=float(item.get("confidence", 0.3)),
                    category=str(item.get("category", "coding")),
                    created_at=str(item.get("created_at", _utc_now_iso())),
                    last_reinforced=str(item.get("last_reinforced", _utc_now_iso())),
                    reinforcement_count=int(item.get("reinforcement_count", 1)),
                    metadata=item.get("metadata", {}),
                )
                self._instincts[iid] = inst
                count += 1
            except (ValueError, TypeError):
                continue
        if count:
            self._save()
        return count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """從磁碟載入本能儲存。"""
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for iid, item in data.items():
                    if isinstance(item, dict):
                        self._instincts[iid] = Instinct(
                            id=iid,
                            pattern=str(item.get("pattern", "")),
                            evidence=item.get("evidence", []),
                            confidence=float(item.get("confidence", 0.3)),
                            category=str(item.get("category", "coding")),
                            created_at=str(item.get("created_at", "")),
                            last_reinforced=str(item.get("last_reinforced", "")),
                            reinforcement_count=int(item.get("reinforcement_count", 1)),
                            metadata=item.get("metadata", {}),
                        )
        except (json.JSONDecodeError, OSError):
            logger.warning("InstinctEngine: failed to load instincts store")

    def _save(self) -> None:
        """將本能儲存寫入磁碟。"""
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        data = {iid: asdict(inst) for iid, inst in self._instincts.items()}
        self._store_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _find_similar(self, pattern_text: str) -> Instinct | None:
        """尋找與给定模式相似的已有本能（簡單字串匹配）。"""
        pattern_lower = pattern_text.lower()
        for inst in self._instincts.values():
            if inst.metadata.get("status") == "evolved":
                continue
            # Simple similarity: substring match or high word overlap
            if pattern_lower in inst.pattern.lower() or inst.pattern.lower() in pattern_lower:
                return inst
            # Word overlap check
            words_new = set(pattern_lower.split())
            words_existing = set(inst.pattern.lower().split())
            if words_new and words_existing:
                overlap = len(words_new & words_existing) / max(len(words_new), len(words_existing))
                if overlap > 0.7:
                    return inst
        return None

    def _extract_patterns(
        self, messages: list[dict[str, Any]]
    ) -> list[tuple[str, str]]:
        """從訊息中提取模式（規則式後備方案）。

        Returns:
            list of (pattern_text, category) tuples.
        """
        patterns: list[tuple[str, str]] = []

        # Heuristic: look for repeated tool usage patterns, error-fix cycles, etc.
        tool_uses: dict[str, int] = {}
        error_fix_cycles = 0
        test_mentions = 0

        for msg in messages:
            content = str(msg.get("content", "")).lower()
            role = str(msg.get("role", ""))

            # Track tool usage
            if "tool" in msg or role == "tool":
                tool_name = str(msg.get("name", msg.get("tool", "unknown")))
                tool_uses[tool_name] = tool_uses.get(tool_name, 0) + 1

            # Detect error-fix cycles
            if any(kw in content for kw in ("error", "failed", "exception", "traceback")):
                error_fix_cycles += 1
            if any(kw in content for kw in ("fixed", "resolved", "solution")):
                error_fix_cycles += 1

            # Detect testing patterns
            if any(kw in content for kw in ("test", "pytest", "assert", "coverage")):
                test_mentions += 1

        # Generate patterns from heuristics
        if error_fix_cycles >= 4:
            patterns.append((
                "Error-fix cycle detected: consider writing failing test first before fixing",
                "debugging",
            ))
        if test_mentions >= 3:
            patterns.append((
                "Testing-heavy session: TDD workflow pattern observed",
                "testing",
            ))
        for tool_name, count in tool_uses.items():
            if count >= 5:
                patterns.append((
                    f"Heavy usage of tool '{tool_name}' ({count}x): consider batching or automation",
                    "workflow",
                ))

        return patterns
