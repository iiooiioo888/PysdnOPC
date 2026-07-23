"""洞察引擎模組 — 從執行數據中提取可操作的洞察。

職責說明：
    分析任務執行歷史，自動發現：
    - 瓶頸角色（耗時異常）
    - 返工熱點（質量問題）
    - 成本異常（花費超預期）
    - 效率趨勢（進步/退步）
    - 優化建議（可操作的改進）

使用範例：
    from opc.layer6_observability.insight_engine import InsightEngine
    engine = InsightEngine(event_bus=bus)
    insights = engine.analyze_run(events)
    for insight in insights:
        print(insight.message)
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger


class InsightType(Enum):
    """洞察類型。"""
    BOTTLENECK = "bottleneck"        # 瓶頸
    QUALITY = "quality"              # 質量問題
    COST = "cost"                    # 成本異常
    EFFICIENCY = "efficiency"        # 效率問題
    SUGGESTION = "suggestion"        # 優化建議
    TREND = "trend"                  # 趨勢


class InsightSeverity(Enum):
    """洞察嚴重程度。"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Insight:
    """單個洞察。"""
    type: InsightType
    severity: InsightSeverity
    message: str
    role: str = ""
    metric_value: float = 0.0
    metric_unit: str = ""
    suggestion: str = ""
    confidence: float = 0.8  # 0-1


@dataclass
class RoleMetrics:
    """單個角色的執行指標。"""
    role: str
    total_duration: float = 0.0         # 總耗時（秒）
    task_count: int = 0                 # 任務數量
    rework_count: int = 0               # 返工次數
    total_cost: float = 0.0             # 總花費
    total_tokens: int = 0              # 總 tokens
    avg_duration_per_task: float = 0.0  # 平均每任務耗時
    rework_rate: float = 0.0           # 返工率
    cost_per_task: float = 0.0         # 平均每任務花費


@dataclass
class RunAnalysis:
    """一次運行的分析結果。"""
    run_id: str
    total_duration: float
    total_cost: float
    total_tasks: int
    role_metrics: list[RoleMetrics]
    insights: list[Insight]
    score: float = 0.0  # 綜合評分 0-100


@dataclass
class ExecutionEvent:
    """執行事件（用於分析）。"""
    event_type: str     # task_started, task_completed, task_reworked, cost_update, ...
    role: str = ""
    task_item: str = ""
    duration: float = 0.0
    cost: float = 0.0
    tokens: int = 0
    model: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class InsightEngine:
    """洞察引擎 — 從執行數據中提取可操作的洞察。

    分析維度：
    1. 瓶頸分析：哪個角色耗時最長？
    2. 返工分析：哪個角色返工率最高？
    3. 成本分析：哪個角色花費最多？是否有異常？
    4. 效率分析：整體效率如何？有無退步？
    5. 優化建議：基於以上分析給出可操作建議
    """

    def __init__(self, event_bus: Any = None) -> None:
        self.event_bus = event_bus
        self._run_history: list[RunAnalysis] = []

    def analyze_run(self, events: list[ExecutionEvent], run_id: str = "") -> RunAnalysis:
        """分析一次運行的事件，返回洞察。

        參數：
            events: 執行事件列表
            run_id: 運行 ID

        返回：
            RunAnalysis — 包含角色指標、洞察、評分
        """
        # 1. 計算角色指標
        role_metrics = self._calc_role_metrics(events)

        # 2. 提取洞察
        insights = []
        insights.extend(self._analyze_bottlenecks(role_metrics))
        insights.extend(self._analyze_rework(role_metrics))
        insights.extend(self._analyze_cost(role_metrics))
        insights.extend(self._analyze_efficiency(role_metrics))
        insights.extend(self._generate_suggestions(role_metrics, insights))

        # 3. 計算綜合評分
        score = self._calc_score(role_metrics, insights)

        # 4. 計算總計
        total_duration = max((e.timestamp for e in events), default=0) - min((e.timestamp for e in events), default=0)
        total_cost = sum(e.cost for e in events)
        total_tasks = len(set(e.task_item for e in events if e.task_item))

        analysis = RunAnalysis(
            run_id=run_id,
            total_duration=total_duration,
            total_cost=total_cost,
            total_tasks=total_tasks,
            role_metrics=role_metrics,
            insights=insights,
            score=score,
        )

        self._run_history.append(analysis)
        return analysis

    def get_trend(self, last_n: int = 5) -> list[Insight]:
        """分析最近 N 次運行的趨勢。"""
        if len(self._run_history) < 2:
            return []

        insights = []
        recent = self._run_history[-last_n:]

        # 評分趨勢
        scores = [r.score for r in recent]
        if len(scores) >= 2:
            trend = scores[-1] - scores[0]
            if trend > 10:
                insights.append(Insight(
                    type=InsightType.TREND,
                    severity=InsightSeverity.INFO,
                    message=f"📈 質量持續提升：評分從 {scores[0]:.0f} 提升到 {scores[-1]:.0f}",
                    metric_value=trend,
                    metric_unit="score",
                ))
            elif trend < -10:
                insights.append(Insight(
                    type=InsightType.TREND,
                    severity=InsightSeverity.WARNING,
                    message=f"📉 質量下降趨勢：評分從 {scores[0]:.0f} 下降到 {scores[-1]:.0f}",
                    metric_value=trend,
                    metric_unit="score",
                    suggestion="檢查最近的配置變更或角色調整",
                ))

        # 成本趨勢
        costs = [r.total_cost for r in recent]
        if len(costs) >= 2:
            cost_trend = costs[-1] - costs[0]
            if cost_trend > 0.5:
                insights.append(Insight(
                    type=InsightType.COST,
                    severity=InsightSeverity.WARNING,
                    message=f"💰 成本上升趨勢：從 ${costs[0]:.2f} 增加到 ${costs[-1]:.2f}",
                    metric_value=cost_trend,
                    metric_unit="USD",
                    suggestion="考慮使用更經濟的模型或優化 prompt",
                ))

        return insights

    # --- 內部分析方法 ---

    def _calc_role_metrics(self, events: list[ExecutionEvent]) -> list[RoleMetrics]:
        """計算每個角色的執行指標。"""
        role_data: dict[str, dict[str, Any]] = {}

        for event in events:
            role = event.role
            if not role:
                continue

            if role not in role_data:
                role_data[role] = {
                    "total_duration": 0.0,
                    "task_count": 0,
                    "rework_count": 0,
                    "total_cost": 0.0,
                    "total_tokens": 0,
                    "durations": [],
                }

            data = role_data[role]
            data["total_cost"] += event.cost
            data["total_tokens"] += event.tokens

            if event.event_type == "task_completed":
                data["task_count"] += 1
                data["total_duration"] += event.duration
                data["durations"].append(event.duration)
            elif event.event_type == "task_reworked":
                data["rework_count"] += 1

        metrics = []
        for role, data in role_data.items():
            task_count = max(data["task_count"], 1)
            metrics.append(RoleMetrics(
                role=role,
                total_duration=data["total_duration"],
                task_count=data["task_count"],
                rework_count=data["rework_count"],
                total_cost=data["total_cost"],
                total_tokens=data["total_tokens"],
                avg_duration_per_task=data["total_duration"] / task_count,
                rework_rate=data["rework_count"] / task_count,
                cost_per_task=data["total_cost"] / task_count,
            ))

        return metrics

    def _analyze_bottlenecks(self, metrics: list[RoleMetrics]) -> list[Insight]:
        """分析瓶頸角色。"""
        insights = []
        if len(metrics) < 2:
            return insights

        durations = [m.avg_duration_per_task for m in metrics if m.avg_duration_per_task > 0]
        if not durations:
            return insights

        mean_duration = statistics.mean(durations)
        std_duration = statistics.stdev(durations) if len(durations) > 1 else 0

        for m in metrics:
            if m.avg_duration_per_task > mean_duration + 2 * std_duration:
                insights.append(Insight(
                    type=InsightType.BOTTLENECK,
                    severity=InsightSeverity.WARNING,
                    message=(
                        f"⏱️ 角色 '{m.role}' 平均耗時 {m.avg_duration_per_task:.0f}s，"
                        f"是團隊平均 ({mean_duration:.0f}s) 的 "
                        f"{m.avg_duration_per_task / mean_duration:.1f}x"
                    ),
                    role=m.role,
                    metric_value=m.avg_duration_per_task,
                    metric_unit="seconds",
                    suggestion=f"考慮為 '{m.role}' 分配更強的模型或拆分任務",
                ))

        return insights

    def _analyze_rework(self, metrics: list[RoleMetrics]) -> list[Insight]:
        """分析返工問題。"""
        insights = []

        for m in metrics:
            if m.rework_rate > 0.3 and m.task_count >= 2:
                severity = InsightSeverity.CRITICAL if m.rework_rate > 0.5 else InsightSeverity.WARNING
                insights.append(Insight(
                    type=InsightType.QUALITY,
                    severity=severity,
                    message=(
                        f"🔄 角色 '{m.role}' 返工率 {m.rework_rate:.0%} "
                        f"({m.rework_count}/{m.task_count} 任務)"
                    ),
                    role=m.role,
                    metric_value=m.rework_rate,
                    metric_unit="rate",
                    suggestion=(
                        f"建議：1) 加強 '{m.role}' 的 prompt 2) 升級模型 "
                        f"3) 在 '{m.role}' 之前增加審核角色"
                    ),
                ))

        return insights

    def _analyze_cost(self, metrics: list[RoleMetrics]) -> list[Insight]:
        """分析成本異常。"""
        insights = []
        if not metrics:
            return insights

        total_cost = sum(m.total_cost for m in metrics)
        if total_cost <= 0:
            return insights

        # 找出花費佔比最高的角色
        for m in metrics:
            pct = m.total_cost / total_cost
            if pct > 0.6 and len(metrics) > 1:
                insights.append(Insight(
                    type=InsightType.COST,
                    severity=InsightSeverity.WARNING,
                    message=(
                        f"💰 角色 '{m.role}' 佔總花費的 {pct:.0%} "
                        f"(${m.total_cost:.2f}/${total_cost:.2f})"
                    ),
                    role=m.role,
                    metric_value=m.total_cost,
                    metric_unit="USD",
                    suggestion=f"考慮為 '{m.role}' 使用更經濟的模型",
                ))

        # 檢查單任務成本異常
        costs_per_task = [m.cost_per_task for m in metrics if m.cost_per_task > 0]
        if len(costs_per_task) >= 2:
            mean_cost = statistics.mean(costs_per_task)
            for m in metrics:
                if m.cost_per_task > mean_cost * 3:
                    insights.append(Insight(
                        type=InsightType.COST,
                        severity=InsightSeverity.WARNING,
                        message=(
                            f"💸 角色 '{m.role}' 每任務成本 ${m.cost_per_task:.2f} "
                            f"異常高（團隊平均 ${mean_cost:.2f}）"
                        ),
                        role=m.role,
                        metric_value=m.cost_per_task,
                        metric_unit="USD/task",
                    ))

        return insights

    def _analyze_efficiency(self, metrics: list[RoleMetrics]) -> list[Insight]:
        """分析效率問題。"""
        insights = []
        if not metrics:
            return insights

        # 找出空閒角色（任務量極少）
        total_tasks = sum(m.task_count for m in metrics)
        if total_tasks > 0:
            for m in metrics:
                if m.task_count == 0:
                    insights.append(Insight(
                        type=InsightType.EFFICIENCY,
                        severity=InsightSeverity.INFO,
                        message=f"💤 角色 '{m.role}' 未執行任何任務，可能是冗餘角色",
                        role=m.role,
                        suggestion=f"考慮移除 '{m.role}' 或合併到其他角色",
                    ))

        return insights

    def _generate_suggestions(
        self, metrics: list[RoleMetrics], existing_insights: list[Insight]
    ) -> list[Insight]:
        """基於分析結果生成優化建議。"""
        suggestions = []

        # 如果有瓶頸和返工問題同時存在
        has_bottleneck = any(i.type == InsightType.BOTTLENECK for i in existing_insights)
        has_quality = any(i.type == InsightType.QUALITY for i in existing_insights)

        if has_bottleneck and has_quality:
            suggestions.append(Insight(
                type=InsightType.SUGGESTION,
                severity=InsightSeverity.INFO,
                message="💡 同時存在瓶頸和質量問題，建議重構工作流：將瓶頸任務拆分為並行子任務",
                suggestion="使用自適應組織架構，自動拆分瓶頸角色",
            ))

        # 如果總成本較高
        total_cost = sum(m.total_cost for m in metrics)
        if total_cost > 1.0:
            suggestions.append(Insight(
                type=InsightType.SUGGESTION,
                severity=InsightSeverity.INFO,
                message=f"💡 總成本 ${total_cost:.2f}，可通過模型路由優化節省 30-50%",
                suggestion="啟用預算感知模型路由，為不同任務自動選擇性價比最優的模型",
            ))

        return suggestions

    def _calc_score(self, metrics: list[RoleMetrics], insights: list[Insight]) -> float:
        """計算綜合評分 (0-100)。"""
        score = 100.0

        # 扣分規則
        for insight in insights:
            if insight.severity == InsightSeverity.CRITICAL:
                score -= 20
            elif insight.severity == InsightSeverity.WARNING:
                score -= 10
            elif insight.severity == InsightSeverity.INFO:
                score -= 2

        return max(0.0, min(100.0, score))


def format_run_analysis(analysis: RunAnalysis) -> str:
    """格式化運行分析結果為人類可讀文本。"""
    lines = [f"📊 運行分析報告 (Run: {analysis.run_id or 'N/A'})\n"]

    # 摘要
    score_emoji = "🟢" if analysis.score >= 80 else "🟡" if analysis.score >= 60 else "🔴"
    lines.append(f"  {score_emoji} 綜合評分: {analysis.score:.0f}/100")
    lines.append(f"  ⏱️ 總耗時: {analysis.total_duration:.0f}s")
    lines.append(f"  💰 總花費: ${analysis.total_cost:.2f}")
    lines.append(f"  📋 任務數: {analysis.total_tasks}")

    # 角色指標
    if analysis.role_metrics:
        lines.append("\n  ── 角色表現 ──")
        for m in analysis.role_metrics:
            status = "🔄" if m.rework_rate > 0.3 else "✅"
            lines.append(
                f"  {status} {m.role:<16} "
                f"任務:{m.task_count}  "
                f"返工:{m.rework_rate:.0%}  "
                f"耗時:{m.avg_duration_per_task:.0f}s  "
                f"花費:${m.cost_per_task:.2f}"
            )

    # 洞察
    if analysis.insights:
        lines.append("\n  ── 洞察與建議 ──")
        for insight in analysis.insights:
            lines.append(f"  {insight.message}")
            if insight.suggestion:
                lines.append(f"    → {insight.suggestion}")

    return "\n".join(lines)
