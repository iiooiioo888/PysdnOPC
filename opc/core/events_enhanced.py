"""增強型事件匯流排 — 支援 WebSocket 廣播和事件持久化。

職責說明：
    擴展原有 EventBus，增加：
    - WebSocket 實時廣播（前端儀表盤）
    - 事件分類與過濾
    - 事件統計
    - 批量訂閱

使用範例：
    from opc.core.events_enhanced import EnhancedEventBus
    bus = EnhancedEventBus()
    bus.add_ws_broadcaster(ws_send_fn)
    await bus.publish(OPCEvent(event_type="task.completed", data={...}))
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.core.events import EventBus, Listener
from opc.core.models import OPCEvent


# 事件分類
EVENT_CATEGORIES = {
    # 生命週期
    "company":      ["company.started", "company.completed", "company.failed", "company.paused"],
    "task":         ["task.created", "task.started", "task.completed", "task.failed", "task.reworked"],
    "work_item":    ["work_item.created", "work_item.started", "work_item.completed", "work_item.blocked"],

    # 角色
    "role":         ["role.activated", "role.blocked", "role.idle", "role.error"],

    # LLM
    "llm":          ["llm.call_start", "llm.call_end", "llm.tool_call", "llm.error"],

    # 成本與預算
    "cost":         ["cost.update", "cost.milestone"],
    "budget":       ["budget.alert", "budget.usage", "budget.exceeded"],

    # 質量
    "review":       ["review.approved", "review.rejected", "review.score"],

    # 系統
    "system":       ["system.init", "system.error", "system.config_change"],
}

# 反向映射：事件類型 → 分類
_EVENT_TO_CATEGORY: dict[str, str] = {}
for category, event_types in EVENT_CATEGORIES.items():
    for et in event_types:
        _EVENT_TO_CATEGORY[et] = category


@dataclass
class EventStats:
    """事件統計。"""
    total_published: int = 0
    by_category: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_event_time: float = 0.0
    ws_broadcasts: int = 0
    ws_errors: int = 0


# WebSocket 發送函數類型
WSBroadcaster = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class EnhancedEventBus(EventBus):
    """增強型事件匯流排。

    在原有 EventBus 基礎上增加：
    - WebSocket 實時廣播
    - 事件分類統計
    - 事件過濾訂閱
    - 批量歷史查詢
    """

    def __init__(self) -> None:
        super().__init__()
        self._ws_broadcasters: list[WSBroadcaster] = []
        self._stats = EventStats()
        self._category_listeners: dict[str, list[Listener]] = defaultdict(list)
        self._max_history = 500  # 最大歷史記錄數

    def add_ws_broadcaster(self, broadcaster: WSBroadcaster) -> None:
        """添加 WebSocket 廣播器。"""
        self._ws_broadcasters.append(broadcaster)

    def remove_ws_broadcaster(self, broadcaster: WSBroadcaster) -> None:
        """移除 WebSocket 廣播器。"""
        try:
            self._ws_broadcasters.remove(broadcaster)
        except ValueError:
            pass

    def subscribe_category(self, category: str, listener: Listener) -> None:
        """按事件分類訂閱。

        參數：
            category: 事件分類（company, task, role, llm, cost, budget, review, system）
            listener: 監聽器函數
        """
        self._category_listeners[category].append(listener)

    async def publish(self, event: OPCEvent) -> None:
        """發布事件（覆蓋父類方法，增加廣播和統計）。"""
        # 調用父類 publish（處理原有監聽者）
        await super().publish(event)

        # 更新統計
        self._stats.total_published += 1
        self._stats.last_event_time = time.time()
        category = _EVENT_TO_CATEGORY.get(event.event_type, "unknown")
        self._stats.by_category[category] += 1
        self._stats.by_type[event.event_type] += 1

        # 觸發分類監聽者
        category_listeners = self._category_listeners.get(category, [])
        if category_listeners:
            tasks = [fn(event) for fn in category_listeners]
            await asyncio.gather(*tasks, return_exceptions=True)

        # WebSocket 廣播
        if self._ws_broadcasters:
            ws_payload = self._format_ws_payload(event)
            for broadcaster in self._ws_broadcasters:
                try:
                    await broadcaster(ws_payload)
                    self._stats.ws_broadcasts += 1
                except Exception as e:
                    self._stats.ws_errors += 1
                    logger.warning(f"WebSocket broadcast failed: {e}")

        # 限制歷史記錄大小
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    def get_stats(self) -> EventStats:
        """獲取事件統計。"""
        return self._stats

    def get_history_by_category(
        self, category: str, limit: int = 50
    ) -> list[OPCEvent]:
        """按分類查詢歷史事件。"""
        events = [
            e for e in self._history
            if _EVENT_TO_CATEGORY.get(e.event_type) == category
        ]
        return events[-limit:]

    def get_recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        """獲取最近事件（格式化為前端可用的 JSON）。"""
        recent = self._history[-limit:]
        return [self._format_ws_payload(e) for e in recent]

    def _format_ws_payload(self, event: OPCEvent) -> dict[str, Any]:
        """格式化事件為 WebSocket 廣播 payload。"""
        category = _EVENT_TO_CATEGORY.get(event.event_type, "unknown")
        return {
            "type": "event",
            "event_type": event.event_type,
            "category": category,
            "payload": event.payload if isinstance(event.payload, dict) else {},
            "timestamp": event.timestamp.isoformat() if hasattr(event.timestamp, "isoformat") else str(event.timestamp),
            "event_id": event.event_id,
        }

    def _prune_history(self) -> None:
        """清理過期歷史記錄。"""
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
