"""基於規則的意圖分類器 — 零配置啟動的核心推断引擎。

不依賴 LLM 呼叫，使用關鍵詞映射和模式匹配來推斷使用者意圖，
避免雞生蛋問題（需要 LLM 才能配置 LLM）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class IntentDomain(str, Enum):
    """意圖領域分類。"""

    CODING = "coding"
    WRITING = "writing"
    RESEARCH = "research"
    AUTOMATION = "automation"
    DATA = "data"
    CHAT = "chat"


class ModelTier(str, Enum):
    """模型層級。"""

    CRITICAL = "critical"  # 關鍵決策、複雜代碼生成
    REASONING = "reasoning"  # 複雜推理、多步驟規劃
    ROUTINE = "routine"  # 日常對話、簡單任務
    SUMMARY = "summary"  # 摘要、分類、壓縮


class ExecutionModeHint(str, Enum):
    """執行模式提示。"""

    TASK = "task"  # 單代理任務模式
    COMPANY = "company"  # 多角色公司模式
    AUTO = "auto"  # 自動判斷


@dataclass
class IntentProfile:
    """意圖分析結果。"""

    domains: list[IntentDomain] = field(default_factory=lambda: [IntentDomain.CHAT])
    mode_hint: ExecutionModeHint = ExecutionModeHint.AUTO
    skills: list[str] = field(default_factory=list)
    model_tier: ModelTier = ModelTier.ROUTINE
    complexity_score: float = 0.0  # 0.0 ~ 1.0
    keywords_matched: list[str] = field(default_factory=list)
    raw_intent: str = ""

    def to_dict(self) -> dict[str, Any]:
        """轉換為字典。"""
        return {
            "domains": [d.value for d in self.domains],
            "mode_hint": self.mode_hint.value,
            "skills": self.skills,
            "model_tier": self.model_tier.value,
            "complexity_score": self.complexity_score,
            "keywords_matched": self.keywords_matched,
        }


# ─────────────────────────────────────────────────────────────────────────────
# 關鍵詞映射表（中英文）
# ─────────────────────────────────────────────────────────────────────────────

_DOMAIN_KEYWORDS: dict[IntentDomain, list[str]] = {
    IntentDomain.CODING: [
        # 中文
        "程式", "程式碼", "代碼", "編程", "編程", "開發", "腳本", "爬蟲", "抓取",
        "函數", "類", "物件", "變數", "陣列", "列表", "字典", "迴圈", "條件",
        "debug", "除錯", "重構", "優化", "演算法", "資料結構",
        "api", "介面", "資料庫", "sql", "nosql",
        "前端", "後端", "全端", "網頁", "app", "應用程式",
        "python", "javascript", "typescript", "java", "c++", "go", "rust",
        "react", "vue", "angular", "node", "django", "flask", "fastapi",
        "git", "docker", "kubernetes", "ci/cd", "部署",
        "測試", "單元測試", "整合測試", "pytest", "jest",
        # 英文
        "code", "coding", "program", "script", "develop", "build",
        "function", "class", "object", "variable", "array", "loop",
        "algorithm", "refactor", "optimize", "implement",
        "frontend", "backend", "fullstack", "web", "mobile",
        "database", "query", "migration", "schema",
        "test", "unittest", "integration", "coverage",
        "deploy", "container", "serverless", "microservice",
    ],
    IntentDomain.WRITING: [
        # 中文
        "文章", "報告", "文案", "寫作", "撰寫", "草稿", "大綱",
        "郵件", "信件", "通知", "公告", "新聞稿",
        "翻譯", "潤飾", "改寫", "摘要", "總結",
        "小說", "故事", "劇本", "詩", "散文",
        "簡報", "ppt", "投影片", "演示",
        "文件", "說明書", "手冊", "指南", "教學",
        "部落格", "貼文", "社交媒體", "推特", "臉書",
        # 英文
        "write", "writing", "article", "report", "essay", "blog",
        "email", "letter", "memo", "notice", "announcement",
        "translate", "proofread", "edit", "summarize", "rewrite",
        "story", "novel", "script", "poem", "creative",
        "presentation", "slides", "deck", "pitch",
        "document", "manual", "guide", "tutorial", "readme",
        "copy", "copywriting", "content", "post", "tweet",
    ],
    IntentDomain.RESEARCH: [
        # 中文
        "研究", "調查", "分析", "報告", "論文", "文獻",
        "搜尋", "查找", "找資料", "收集", "整理",
        "比較", "對比", "評估", "審查", "檢視",
        "市場", "競品", "趨勢", "數據", "統計",
        "問卷", "訪談", "觀察", "實驗",
        # 英文
        "research", "investigate", "analyze", "analysis", "study",
        "search", "find", "lookup", "gather", "collect",
        "compare", "evaluate", "assess", "review", "audit",
        "market", "competitor", "trend", "data", "statistics",
        "survey", "interview", "observe", "experiment",
        "paper", "thesis", "literature", "citation", "reference",
    ],
    IntentDomain.AUTOMATION: [
        # 中文
        "自動化", "排程", "定時", "週期", "批次", "批量",
        "工作流", "流程", "管線", "pipeline",
        "通知", "提醒", "警報", "監控",
        "同步", "備份", "匯入", "匯出", "轉換",
        "爬蟲", "抓取", "擷取", "下載",
        "郵件", "簡訊", "推送", "webhook",
        # 英文
        "automate", "automation", "schedule", "cron", "periodic",
        "workflow", "pipeline", "batch", "bulk",
        "notify", "alert", "monitor", "watch",
        "sync", "backup", "import", "export", "convert",
        "scrape", "crawl", "extract", "download",
        "email", "sms", "push", "webhook", "trigger",
    ],
    IntentDomain.DATA: [
        # 中文
        "資料", "數據", "表格", "excel", "csv", "json",
        "視覺化", "圖表", "儀表板", "報表",
        "清洗", "處理", "轉換", "標準化",
        "機器學習", "深度學習", "ai", "模型", "訓練",
        "預測", "分類", "聚類", "迴歸",
        # 英文
        "data", "dataset", "table", "spreadsheet", "csv", "json",
        "visualize", "chart", "graph", "dashboard", "plot",
        "clean", "process", "transform", "normalize",
        "machine learning", "deep learning", "ml", "ai", "model",
        "predict", "classify", "cluster", "regress",
        "pandas", "numpy", "scipy", "sklearn", "tensorflow", "pytorch",
    ],
}

# 複雜度指示詞（觸發多角色模式）
_COMPLEXITY_INDICATORS = [
    # 中文
    "多步驟", "多階段", "完整", "全流程", "端到端", "從頭到尾",
    "團隊", "協作", "分工", "角色", "審查", "審核", "品質",
    "架構", "設計", "規劃", "策略", "方案",
    "系統", "平台", "框架", "基礎設施",
    # 英文
    "multi-step", "multi-stage", "complete", "full", "end-to-end",
    "team", "collaborate", "delegate", "role", "review", "quality",
    "architecture", "design", "plan", "strategy",
    "system", "platform", "framework", "infrastructure",
]

# 技能映射（domain → skill 名稱）
_DOMAIN_SKILLS: dict[IntentDomain, list[str]] = {
    IntentDomain.CODING: ["coding", "deployment"],
    IntentDomain.WRITING: ["writing"],
    IntentDomain.RESEARCH: ["web_search"],
    IntentDomain.AUTOMATION: ["coding", "deployment"],
    IntentDomain.DATA: ["coding", "web_search"],
    IntentDomain.CHAT: [],
}

# 模型層級映射
_DOMAIN_TIER: dict[IntentDomain, ModelTier] = {
    IntentDomain.CODING: ModelTier.CRITICAL,
    IntentDomain.WRITING: ModelTier.ROUTINE,
    IntentDomain.RESEARCH: ModelTier.REASONING,
    IntentDomain.AUTOMATION: ModelTier.ROUTINE,
    IntentDomain.DATA: ModelTier.REASONING,
    IntentDomain.CHAT: ModelTier.ROUTINE,
}


class IntentClassifier:
    """基於規則的意圖分類器。

    使用關鍵詞匹配和模式分析來推斷使用者意圖，
    不依賴 LLM 呼叫，適用於零配置啟動場景。
    """

    def __init__(self) -> None:
        self._domain_keywords = _DOMAIN_KEYWORDS
        self._complexity_indicators = _COMPLEXITY_INDICATORS

    def classify(self, intent: str) -> IntentProfile:
        """分類使用者意圖。

        Args:
            intent: 使用者輸入的自然語言意圖描述。

        Returns:
            IntentProfile 包含推斷的領域、模式、技能和模型層級。
        """
        if not intent or not intent.strip():
            return IntentProfile(raw_intent=intent)

        text = intent.lower()
        matched_domains: list[tuple[IntentDomain, int]] = []
        all_matched_keywords: list[str] = []

        # 1. 領域關鍵詞匹配
        for domain, keywords in self._domain_keywords.items():
            count = 0
            for kw in keywords:
                if kw.lower() in text:
                    count += 1
                    all_matched_keywords.append(kw)
            if count > 0:
                matched_domains.append((domain, count))

        # 2. 如果沒有匹配到任何領域，預設為 CHAT
        if not matched_domains:
            return IntentProfile(
                domains=[IntentDomain.CHAT],
                mode_hint=ExecutionModeHint.TASK,
                model_tier=ModelTier.ROUTINE,
                complexity_score=0.1,
                raw_intent=intent,
            )

        # 3. 按匹配數量排序，取得前兩個領域
        matched_domains.sort(key=lambda x: x[1], reverse=True)
        primary_domains = [d for d, _ in matched_domains[:2]]

        # 4. 計算複雜度分數
        complexity = self._calculate_complexity(text, matched_domains)

        # 5. 推斷執行模式
        mode_hint = self._infer_mode(text, complexity, primary_domains)

        # 6. 匹配技能
        skills = self._match_skills(primary_domains)

        # 7. 選擇模型層級（使用主要領域的層級）
        model_tier = _DOMAIN_TIER.get(primary_domains[0], ModelTier.ROUTINE)
        if complexity > 0.7:
            model_tier = ModelTier.CRITICAL

        return IntentProfile(
            domains=primary_domains,
            mode_hint=mode_hint,
            skills=skills,
            model_tier=model_tier,
            complexity_score=complexity,
            keywords_matched=all_matched_keywords[:10],  # 限制數量
            raw_intent=intent,
        )

    def _calculate_complexity(
        self,
        text: str,
        matched_domains: list[tuple[IntentDomain, int]],
    ) -> float:
        """計算任務複雜度分數 (0.0 ~ 1.0)。"""
        score = 0.0

        # 基礎分數：匹配到的領域數量
        score += min(len(matched_domains) * 0.15, 0.3)

        # 複雜度指示詞
        indicator_count = sum(
            1 for ind in self._complexity_indicators if ind.lower() in text
        )
        score += min(indicator_count * 0.1, 0.4)

        # 文本長度（較長的描述通常意味著較複雜的任務）
        text_len = len(text)
        if text_len > 200:
            score += 0.15
        elif text_len > 100:
            score += 0.1
        elif text_len > 50:
            score += 0.05

        # 匹配關鍵詞數量
        total_matches = sum(count for _, count in matched_domains)
        score += min(total_matches * 0.02, 0.15)

        return min(score, 1.0)

    def _infer_mode(
        self,
        text: str,
        complexity: float,
        domains: list[IntentDomain],
    ) -> ExecutionModeHint:
        """推斷執行模式。"""
        # 高複雜度 → 公司模式
        if complexity > 0.7:
            return ExecutionModeHint.COMPANY

        # 明確的多角色指示詞
        multi_role_keywords = ["團隊", "協作", "分工", "角色", "審查", "team", "collaborate", "delegate"]
        if any(kw in text for kw in multi_role_keywords):
            return ExecutionModeHint.COMPANY

        # 低複雜度 → 任務模式
        if complexity < 0.3:
            return ExecutionModeHint.TASK

        # 中等複雜度 → 自動判斷
        return ExecutionModeHint.AUTO

    def _match_skills(self, domains: list[IntentDomain]) -> list[str]:
        """根據領域匹配技能。"""
        skills: set[str] = set()
        for domain in domains:
            domain_skills = _DOMAIN_SKILLS.get(domain, [])
            skills.update(domain_skills)
        return sorted(skills)


# 全域單例
_default_classifier: IntentClassifier | None = None


def get_classifier() -> IntentClassifier:
    """取得預設分類器實例。"""
    global _default_classifier
    if _default_classifier is None:
        _default_classifier = IntentClassifier()
    return _default_classifier


def classify_intent(intent: str) -> IntentProfile:
    """便捷函數：分類使用者意圖。"""
    return get_classifier().classify(intent)
