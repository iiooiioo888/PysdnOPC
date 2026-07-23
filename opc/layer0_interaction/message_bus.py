"""統一訊息匯流排 — 在頻道和系統之間路由訊息。

職責說明：
    提供非同步訊息佇列，將來自各互動頻道（CLI、Telegram 等）的
    使用者訊息路由到 OPC 引擎處理，並將回應發佈回頻道。

關聯關係：
    - 被 opc/engine.py 的 OPCEngine 建立和驅動
    - 被 opc/channels/ 的各頻道實例發佈訊息
"""

from __future__ import annotations  # 啟用延遲型別註解評估

import asyncio  # 標準庫：非同步事件循環
from typing import Any, Callable, Coroutine, Optional  # 標準庫：型別註解

from loguru import logger  # 第三方庫：結構化日誌

from opc.core.models import SystemMessage, UserMessage  # 領域模型


InboundHandler = Callable[[UserMessage], Coroutine[Any, Any, Optional[SystemMessage]]]  # 入站訊息處理器型別


class MessageBus:
    """非同步訊息匯流排 — 在互動頻道和 OPC 引擎之間路由訊息。

    職責說明：
        頻道發佈入站訊息；引擎處理後發佈出站回應。
    """

    def __init__(self) -> None:
        self._inbound_queue: asyncio.Queue[UserMessage] | None = None
        self._outbound_queue: asyncio.Queue[SystemMessage] | None = None
        self._inbound_handler: InboundHandler | None = None
        self._running = False

    def _inbound(self) -> asyncio.Queue[UserMessage]:
        if self._inbound_queue is None:
            self._inbound_queue = asyncio.Queue()
        return self._inbound_queue

    def _outbound(self) -> asyncio.Queue[SystemMessage]:
        if self._outbound_queue is None:
            self._outbound_queue = asyncio.Queue()
        return self._outbound_queue

    def set_handler(self, handler: InboundHandler) -> None:
        self._inbound_handler = handler

    async def publish_inbound(self, message: UserMessage) -> None:
        await self._inbound().put(message)

    async def publish_outbound(self, message: SystemMessage) -> None:
        await self._outbound().put(message)

    async def get_response(self, timeout: float = 600.0) -> SystemMessage | None:
        try:
            return await asyncio.wait_for(self._outbound().get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def start(self) -> None:
        """啟動入站訊息處理迴圈。"""
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self._inbound().get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if self._inbound_handler:
                try:
                    response = await self._inbound_handler(msg)
                    if response:
                        await self._outbound().put(response)
                except Exception as e:
                    logger.error(f"Message handler error: {e}")
                    await self._outbound().put(SystemMessage(
                        channel=msg.channel,
                        user_id=msg.user_id,
                        session_id=msg.session_id,
                        content=f"Error processing message: {e}",
                        message_type="reply",
                    ))

    def stop(self) -> None:
        self._running = False

    async def process_single(self, message: UserMessage) -> SystemMessage | None:
        """同步處理單條訊息（供 CLI 使用）。"""
        if self._inbound_handler:
            return await self._inbound_handler(message)
        return None
