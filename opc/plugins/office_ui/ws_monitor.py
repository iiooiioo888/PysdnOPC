"""WebSocket 實時監控服務。

職責說明：
    提供 WebSocket 端點，實現：
    - 實時事件推送（任務狀態、成本更新、角色活動）
    - 儀表盤數據查詢
    - 雙向控制（暫停/恢復/調整預算）

使用方式：
    ws_monitor = WebSocketMonitor(engine_enhancer)
    await ws_monitor.start(port=8766)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Coroutine

from loguru import logger


class WebSocketMonitor:
    """WebSocket 實時監控服務。"""

    def __init__(self, enhancer: Any) -> None:
        self.enhancer = enhancer
        self._server: Any = None
        self._clients: set[Any] = set()
        self._running = False

    async def start(self, host: str = "0.0.0.0", port: int = 8766) -> None:
        """啟動 WebSocket 服務。"""
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed, WebSocket monitor disabled")
            return

        async def handler(ws: Any, path: str = "/") -> None:
            self._clients.add(ws)
            self.enhancer.add_ws_client(ws)
            logger.info(f"WebSocket client connected: {ws.remote_address}")

            try:
                # 發送初始狀態
                await ws.send(json.dumps({
                    "type": "init",
                    "data": self.enhancer.get_dashboard_data(),
                }, default=str))

                # 監聽客戶端消息
                async for message in ws:
                    await self._handle_client_message(ws, message)

            except Exception as e:
                logger.debug(f"WebSocket client disconnected: {e}")
            finally:
                self._clients.discard(ws)
                self.enhancer.remove_ws_client(ws)

        self._server = await websockets.serve(handler, host, port)
        self._running = True
        logger.info(f"WebSocket monitor started on ws://{host}:{port}")

    async def stop(self) -> None:
        """停止 WebSocket 服務。"""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("WebSocket monitor stopped")

    async def _handle_client_message(self, ws: Any, message: str) -> None:
        """處理客戶端消息。"""
        try:
            data = json.loads(message)
            msg_type = data.get("type", "")

            if msg_type == "get_status":
                await ws.send(json.dumps({
                    "type": "status",
                    "data": self.enhancer.get_dashboard_data(),
                }, default=str))

            elif msg_type == "get_insights":
                insights_data = {}
                if self.enhancer.insight_engine:
                    latest = self.enhancer.insight_engine._run_history[-1] if self.enhancer.insight_engine._run_history else None
                    if latest:
                        from opc.layer6_observability.insight_engine import format_run_analysis
                        insights_data = {
                            "score": latest.score,
                            "formatted": format_run_analysis(latest),
                        }
                await ws.send(json.dumps({
                    "type": "insights",
                    "data": insights_data,
                }, default=str))

            elif msg_type == "set_budget":
                new_budget = data.get("budget", 0)
                if self.enhancer.budget_guard:
                    self.enhancer.budget_guard.total_budget = float(new_budget)
                    await ws.send(json.dumps({
                        "type": "budget_updated",
                        "data": {"budget": new_budget},
                    }))

            elif msg_type == "set_quality":
                quality = data.get("quality", "balanced")
                if self.enhancer.model_router:
                    self.enhancer.model_router.quality_hint = quality
                    await ws.send(json.dumps({
                        "type": "quality_updated",
                        "data": {"quality": quality},
                    }))

            elif msg_type == "override_budget":
                if self.enhancer.budget_guard:
                    self.enhancer.budget_guard.set_user_override(True)
                    await ws.send(json.dumps({
                        "type": "budget_override",
                        "data": {"enabled": True},
                    }))

            else:
                await ws.send(json.dumps({
                    "type": "error",
                    "data": {"message": f"Unknown message type: {msg_type}"},
                }))

        except json.JSONDecodeError:
            await ws.send(json.dumps({
                "type": "error",
                "data": {"message": "Invalid JSON"},
            }))
        except Exception as e:
            logger.error(f"WebSocket message handler error: {e}")

    @property
    def client_count(self) -> int:
        return len(self._clients)


class DashboardDataFormatter:
    """儀表盤數據格式化器。"""

    @staticmethod
    def format_realtime_event(event_data: dict[str, Any]) -> dict[str, Any]:
        """格式化實時事件為前端友好的格式。"""
        event_type = event_data.get("event_type", "")
        payload = event_data.get("payload", {})
        category = event_data.get("category", "unknown")

        # 圖標映射
        icons = {
            "company": "🏢",
            "task": "📋",
            "work_item": "📝",
            "role": "👤",
            "llm": "🤖",
            "cost": "💰",
            "budget": "🛡️",
            "review": "✅",
            "system": "⚙️",
        }

        return {
            "id": event_data.get("event_id", ""),
            "icon": icons.get(category, "📌"),
            "type": event_type,
            "category": category,
            "message": DashboardDataFormatter._event_to_message(event_type, payload),
            "timestamp": event_data.get("timestamp", ""),
            "data": payload,
        }

    @staticmethod
    def _event_to_message(event_type: str, payload: dict) -> str:
        """將事件轉為人類可讀消息。"""
        messages = {
            "company.started": "🚀 公司運行啟動",
            "company.completed": "✅ 公司運行完成",
            "company.failed": "❌ 公司運行失敗",
            "task.created": f"📋 新任務: {payload.get('title', '')}",
            "task.started": f"🔄 任務開始: {payload.get('title', '')}",
            "task.completed": f"✅ 任務完成: {payload.get('title', '')}",
            "task.reworked": f"🔁 任務返工: {payload.get('title', '')}",
            "role.activated": f"👤 角色激活: {payload.get('role', '')}",
            "role.blocked": f"⏸️ 角色阻塞: {payload.get('role', '')}",
            "cost.update": f"💰 花費更新: ${payload.get('cost', 0):.2f}",
            "budget.alert": f"⚠️ 預算告警: {payload.get('usage_pct', 0):.0f}%",
            "review.approved": "✅ 評審通過",
            "review.rejected": "❌ 評審駁回",
        }
        return messages.get(event_type, f"📌 {event_type}")

    @staticmethod
    def format_dashboard_html(dashboard_data: dict[str, Any]) -> str:
        """生成簡單的儀表盤 HTML（嵌入式使用）。"""
        budget = dashboard_data.get("budget", {})
        events = dashboard_data.get("recent_events", [])
        insights = dashboard_data.get("insights", {})

        budget_pct = budget.get("pct", 0)
        budget_bar_width = min(100, budget_pct)
        budget_color = "#22c55e" if budget_pct < 70 else "#f59e0b" if budget_pct < 90 else "#ef4444"

        events_html = ""
        for event in events[-10:]:
            formatted = DashboardDataFormatter.format_realtime_event(event)
            events_html += f"""
            <div class="event-item">
                <span class="event-icon">{formatted['icon']}</span>
                <span class="event-msg">{formatted['message']}</span>
                <span class="event-time">{formatted['timestamp'][:19]}</span>
            </div>"""

        insights_html = ""
        if insights:
            score = insights.get("score", 0)
            score_color = "#22c55e" if score >= 80 else "#f59e0b" if score >= 60 else "#ef4444"
            insights_html = f"""
            <div class="insights-section">
                <h3>📊 洞察 (評分: <span style="color:{score_color}">{score:.0f}</span>)</h3>
                <ul>
                    {"".join(f"<li>{i['message']}</li>" for i in insights.get('insights', [])[:5])}
                </ul>
            </div>"""

        return f"""
        <div id="opc-dashboard" style="font-family: system-ui; max-width: 800px; margin: 0 auto; padding: 20px;">
            <h2>🏢 OpenOPC 實時儀表盤</h2>

            <div class="budget-section" style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 10px 0;">
                <h3>💰 預算狀態</h3>
                <div style="background: #e9ecef; border-radius: 4px; height: 20px; overflow: hidden;">
                    <div style="background: {budget_color}; height: 100%; width: {budget_bar_width}%; transition: width 0.3s;"></div>
                </div>
                <p>${budget.get('spent', 0):.2f} / ${budget.get('total', 0):.2f} ({budget_pct:.0f}%)</p>
            </div>

            <div class="events-section" style="margin: 10px 0;">
                <h3>📡 最近事件</h3>
                {events_html or "<p>暫無事件</p>"}
            </div>

            {insights_html}

            <style>
                .event-item {{ padding: 5px 0; border-bottom: 1px solid #eee; display: flex; gap: 10px; }}
                .event-icon {{ font-size: 1.2em; }}
                .event-msg {{ flex: 1; }}
                .event-time {{ color: #888; font-size: 0.85em; }}
            </style>
        </div>
        """
