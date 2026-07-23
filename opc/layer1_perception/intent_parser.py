"""自然語言意圖解析模組。

職責說明：
    從用戶的自然語言輸入中解析出任務配置，實現零配置啟動。
    自動推斷：領域、任務類型、複雜度、所需角色、工具、模型策略。

使用範例：
    from opc.layer1_perception.intent_parser import IntentParser
    parser = IntentParser(llm_provider)
    config = await parser.parse("幫我做一份新能源汽車行業投資分析報告")
    print(config.domain)        # → "finance"
    print(config.task_type)     # → "research_report"
    print(config.estimated_roles)  # → ["manager", "researcher", "analyst", "writer"]
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from loguru import logger


@dataclass
class TaskIntent:
    """解析後的任務意圖。"""
    raw_input: str                          # 原始用戶輸入
    domain: str = "general"                 # 領域 (finance, dev, content, ...)
    task_type: str = "general"              # 任務類型 (research_report, app_dev, ...)
    complexity: str = "medium"              # 複雜度 (low, medium, high)
    estimated_duration: str = "10-20min"    # 預估耗時
    estimated_roles: list[str] = field(default_factory=list)     # 推薦角色
    required_tools: list[str] = field(default_factory=list)      # 所需工具
    model_strategy: str = "balanced"        # 模型策略 (best, balanced, cheapest)
    output_formats: list[str] = field(default_factory=list)      # 輸出格式
    org_template: str = ""                  # 推薦的組織模板
    confidence: float = 0.0                 # 解析信心度 (0-1)


# 領域關鍵詞映射
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "finance": [
        "投資", "股票", "基金", "金融", "財務", "估值", "風控", "審計",
        "IPO", "並購", "盡調", "行研", "市場分析", "投資報告",
        "investment", "stock", "fund", "finance", "valuation", "audit",
        "due diligence", "market analysis", "equity",
    ],
    "dev": [
        "開發", "編程", "代碼", "程序", "軟件", "網站", "APP", "應用",
        "API", "數據庫", "前端", "後端", "全棧", "小程序", "遊戲",
        "develop", "code", "programming", "software", "website", "app",
        "API", "database", "frontend", "backend", "fullstack",
    ],
    "content": [
        "文章", "視頻", "腳本", "文案", "內容", "自媒體", "公眾號",
        "抖音", "小紅書", "B站", "YouTube", "劇本", "分鏡",
        "article", "video", "script", "content", "blog", "copywriting",
    ],
    "data": [
        "數據", "分析", "統計", "報表", "可視化", "圖表", "Excel",
        "大數據", "BI", "ETL",
        "data", "analysis", "statistics", "report", "visualization",
    ],
    "design": [
        "設計", "UI", "UX", "原型", "交互", "界面", "logo", "海報",
        "design", "prototype", "wireframe", "mockup",
    ],
    "marketing": [
        "營銷", "推廣", "品牌", "廣告", "SEO", "SEM", "社交媒體",
        "增長", "用戶", "轉化",
        "marketing", "branding", "advertising", "growth", "conversion",
    ],
    "education": [
        "教育", "課程", "培訓", "教案", "學習", "考試", "試題",
        "education", "course", "training", "curriculum", "learning",
    ],
    "legal": [
        "法律", "合同", "法規", "合規", "訴訟", "專利", "商標",
        "legal", "contract", "compliance", "patent", "litigation",
    ],
}

# 任務類型關鍵詞映射
_TASK_TYPE_KEYWORDS: dict[str, list[str]] = {
    "research_report": ["研究報告", "分析報告", "行業報告", "調研", "research report", "analysis report"],
    "investment_memo": ["投資備忘錄", "投資建議", "investment memo"],
    "due_diligence": ["盡職調查", "盡調", "due diligence"],
    "app_dev": ["開發應用", "開發APP", "做一個應用", "build app", "develop app"],
    "website": ["做網站", "建站", "website", "build website"],
    "api_service": ["API服務", "後端服務", "API service", "backend service"],
    "article": ["寫文章", "撰文", "write article", "blog post"],
    "video_script": ["視頻腳本", "劇本", "video script", "screenplay"],
    "data_report": ["數據報告", "報表", "data report"],
    "presentation": ["PPT", "演示", "匯報", "presentation", "slides"],
    "code_review": ["代碼審查", "code review"],
    "refactor": ["重構", "refactor", "重構代碼"],
    "testing": ["測試", "test", "QA"],
    "translation": ["翻譯", "translate"],
}

# 複雜度指標
_COMPLEXITY_SIGNALS = {
    "high": [
        "複雜", "深入", "全面", "詳細", "完整", "系統性", "多維度",
        "comprehensive", "detailed", "in-depth", "complex", "thorough",
        "多個", "多種", "各個方面", "全流程",
    ],
    "low": [
        "簡單", "快速", "簡要", "大致", "概覽", "摘要",
        "simple", "quick", "brief", "summary", "overview",
    ],
}

# 角色推薦映射
_DOMAIN_ROLES: dict[str, list[str]] = {
    "finance":    ["manager", "researcher", "analyst", "writer"],
    "dev":        ["architect", "developer", "reviewer", "tester"],
    "content":    ["manager", "researcher", "writer", "designer"],
    "data":       ["analyst", "data_analyst", "writer"],
    "design":     ["manager", "designer", "developer"],
    "marketing":  ["manager", "researcher", "writer", "analyst"],
    "education":  ["manager", "researcher", "writer"],
    "legal":      ["researcher", "analyst", "writer"],
    "general":    ["manager", "researcher", "writer"],
}

# 工具推薦映射
_DOMAIN_TOOLS: dict[str, list[str]] = {
    "finance":    ["web_search", "python_exec", "file_ops", "document_generator"],
    "dev":        ["shell", "file_ops", "git_ops", "python_exec"],
    "content":    ["web_search", "file_ops", "document_generator"],
    "data":       ["python_exec", "web_search", "file_ops"],
    "design":     ["file_ops", "browser"],
    "marketing":  ["web_search", "file_ops", "document_generator"],
    "education":  ["web_search", "file_ops", "document_generator"],
    "legal":      ["web_search", "file_ops", "document_generator"],
    "general":    ["web_search", "file_ops"],
}

# 輸出格式映射
_TASK_OUTPUT_FORMATS: dict[str, list[str]] = {
    "research_report":  ["docx", "pdf"],
    "investment_memo":  ["docx", "pdf"],
    "due_diligence":    ["docx", "pdf", "xlsx"],
    "app_dev":          ["code"],
    "website":          ["code"],
    "api_service":      ["code"],
    "article":          ["docx", "md"],
    "video_script":     ["docx"],
    "data_report":      ["xlsx", "pdf"],
    "presentation":     ["pptx"],
    "code_review":      ["md"],
    "translation":      ["docx"],
}


class IntentParser:
    """自然語言意圖解析器。

    兩階段解析：
    1. 關鍵詞快速匹配（零成本，低延遲）
    2. LLM 深度解析（可選，更準確但有成本）
    """

    def __init__(self, llm_provider: Any = None) -> None:
        self.llm_provider = llm_provider

    async def parse(self, user_input: str, *, use_llm: bool = False) -> TaskIntent:
        """解析用戶輸入的任務意圖。

        參數：
            user_input: 用戶的自然語言輸入
            use_llm: 是否使用 LLM 做深度解析（更準確但有成本）

        返回：
            TaskIntent — 解析後的任務意圖
        """
        intent = TaskIntent(raw_input=user_input)

        # Phase 1: 關鍵詞快速匹配
        self._match_domain(intent)
        self._match_task_type(intent)
        self._match_complexity(intent)
        self._infer_roles(intent)
        self._infer_tools(intent)
        self._infer_output_formats(intent)
        self._match_org_template(intent)
        self._calculate_confidence(intent)

        # Phase 2: LLM 深度解析（可選）
        if use_llm and self.llm_provider and intent.confidence < 0.7:
            await self._llm_refine(intent)

        return intent

    # --- 關鍵詞匹配 ---

    def _match_domain(self, intent: TaskIntent) -> None:
        """匹配領域。"""
        text = intent.raw_input.lower()
        scores: dict[str, int] = {}

        for domain, keywords in _DOMAIN_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw.lower() in text)
            if score > 0:
                scores[domain] = score

        if scores:
            intent.domain = max(scores, key=scores.get)  # type: ignore[arg-type]

    def _match_task_type(self, intent: TaskIntent) -> None:
        """匹配任務類型。"""
        text = intent.raw_input.lower()
        scores: dict[str, int] = {}

        for task_type, keywords in _TASK_TYPE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw.lower() in text)
            if score > 0:
                scores[task_type] = score

        if scores:
            intent.task_type = max(scores, key=scores.get)  # type: ignore[arg-type]

    def _match_complexity(self, intent: TaskIntent) -> None:
        """匹配複雜度。"""
        text = intent.raw_input.lower()

        high_score = sum(1 for kw in _COMPLEXITY_SIGNALS["high"] if kw.lower() in text)
        low_score = sum(1 for kw in _COMPLEXITY_SIGNALS["low"] if kw.lower() in text)

        if high_score > low_score:
            intent.complexity = "high"
            intent.estimated_duration = "15-30min"
        elif low_score > high_score:
            intent.complexity = "low"
            intent.estimated_duration = "3-8min"
        else:
            # 根據輸入長度推斷
            if len(intent.raw_input) > 100:
                intent.complexity = "high"
                intent.estimated_duration = "15-30min"
            elif len(intent.raw_input) < 20:
                intent.complexity = "low"
                intent.estimated_duration = "3-8min"

    def _infer_roles(self, intent: TaskIntent) -> None:
        """推斷所需角色。"""
        intent.estimated_roles = list(_DOMAIN_ROLES.get(intent.domain, _DOMAIN_ROLES["general"]))

        # 根據複雜度調整
        if intent.complexity == "high" and "reviewer" not in intent.estimated_roles:
            intent.estimated_roles.append("reviewer")
        if intent.complexity == "low" and len(intent.estimated_roles) > 2:
            intent.estimated_roles = intent.estimated_roles[:2]

    def _infer_tools(self, intent: TaskIntent) -> None:
        """推斷所需工具。"""
        intent.required_tools = list(_DOMAIN_TOOLS.get(intent.domain, _DOMAIN_TOOLS["general"]))

    def _infer_output_formats(self, intent: TaskIntent) -> None:
        """推斷輸出格式。"""
        intent.output_formats = list(
            _TASK_OUTPUT_FORMATS.get(intent.task_type, ["docx"])
        )

    def _match_org_template(self, intent: TaskIntent) -> None:
        """匹配組織模板。"""
        template_map = {
            "finance": {
                "research_report": "finance/research_report",
                "investment_memo": "finance/research_report",
                "due_diligence": "finance/due_diligence",
            },
            "dev": {
                "app_dev": "dev/fullstack_app",
                "website": "dev/fullstack_app",
                "api_service": "dev/api_service",
                "code_review": "dev/code_review",
            },
            "content": {
                "article": "content/article_series",
                "video_script": "content/video_production",
            },
        }

        domain_templates = template_map.get(intent.domain, {})
        intent.org_template = domain_templates.get(intent.task_type, "general/quick_task")

    def _calculate_confidence(self, intent: TaskIntent) -> None:
        """計算解析信心度。"""
        score = 0.0
        max_score = 4.0

        # 領域匹配
        if intent.domain != "general":
            score += 1.0

        # 任務類型匹配
        if intent.task_type != "general":
            score += 1.0

        # 複雜度信號
        if intent.complexity != "medium":
            score += 0.5

        # 輸入長度（太短信心低）
        if len(intent.raw_input) > 20:
            score += 0.5
        if len(intent.raw_input) > 50:
            score += 0.5

        # 關鍵詞密度
        text = intent.raw_input.lower()
        all_keywords = []
        for keywords in _DOMAIN_KEYWORDS.values():
            all_keywords.extend(keywords)
        matched = sum(1 for kw in all_keywords if kw.lower() in text)
        if matched >= 3:
            score += 0.5

        intent.confidence = min(1.0, score / max_score)

    async def _llm_refine(self, intent: TaskIntent) -> None:
        """使用 LLM 深度解析意圖（可選）。"""
        if not self.llm_provider:
            return

        prompt = f"""分析以下用戶任務描述，返回 JSON 格式的結構化信息：

用戶輸入：「{intent.raw_input}」

請返回：
{{
  "domain": "領域 (finance/dev/content/data/design/marketing/education/legal/general)",
  "task_type": "具體任務類型",
  "complexity": "low/medium/high",
  "suggested_roles": ["角色1", "角色2"],
  "suggested_tools": ["工具1", "工具2"],
  "output_formats": ["格式1"],
  "model_strategy": "best/balanced/cheapest"
}}

只返回 JSON，不要其他文字。"""

        try:
            result = await self.llm_provider.simple_chat(
                prompt=prompt,
                system="你是一個任務分析專家，擅長從用戶描述中提取結構化信息。",
                task_type="simple_qa",
            )

            # 解析 JSON
            result_text = result.strip()
            # 移除可能的 markdown 代碼塊標記
            if result_text.startswith("```"):
                result_text = result_text.split("\n", 1)[-1]
            if result_text.endswith("```"):
                result_text = result_text.rsplit("```", 1)[0]
            result_text = result_text.strip()

            parsed = json.loads(result_text)

            # 更新意圖（LLM 結果優先，但保留關鍵詞匹配的合理結果）
            if parsed.get("domain") and parsed["domain"] != "general":
                intent.domain = parsed["domain"]
            if parsed.get("task_type"):
                intent.task_type = parsed["task_type"]
            if parsed.get("complexity"):
                intent.complexity = parsed["complexity"]
            if parsed.get("suggested_roles"):
                intent.estimated_roles = parsed["suggested_roles"]
            if parsed.get("suggested_tools"):
                intent.required_tools = parsed["suggested_tools"]
            if parsed.get("output_formats"):
                intent.output_formats = parsed["output_formats"]
            if parsed.get("model_strategy"):
                intent.model_strategy = parsed["model_strategy"]

            intent.confidence = max(intent.confidence, 0.85)
            logger.info(f"LLM refined intent: domain={intent.domain}, type={intent.task_type}")

        except Exception as e:
            logger.warning(f"LLM intent refinement failed: {e}")


def format_intent(intent: TaskIntent) -> str:
    """格式化意圖解析結果為人類可讀文本。"""
    confidence_bar = "🟢" if intent.confidence >= 0.7 else "🟡" if intent.confidence >= 0.4 else "🔴"

    return f"""🎯 任務意圖解析

  {confidence_bar} 信心度: {intent.confidence:.0%}
  📂 領域: {intent.domain}
  📋 類型: {intent.task_type}
  ⚙️ 複雜度: {intent.complexity}
  ⏱️ 預估耗時: {intent.estimated_duration}

  👥 推薦角色: {', '.join(intent.estimated_roles)}
  🔧 所需工具: {', '.join(intent.required_tools)}
  📄 輸出格式: {', '.join(intent.output_formats)}
  🏗️ 組織模板: {intent.org_template}
"""
