"""OPC 系統進程內事件匯流排模組。

職責說明：
    提供輕量級的非同步發布/訂閱（pub/sub）事件機制，用於各架構層
    之間的解耦通訊。例如 layer2（組織層）可發布事件通知 layer6（觀測層）
    記錄指標，而無需直接依賴。

關聯關係：
    - 被 opc/engine.py 在初始化時建立實例並注入各層
    - 事件模型 OPCEvent 定義於 opc/core/models.py
    - 被 layer0 ~ layer6 各層用於跨層通訊

使用範例：
    from opc.core.events import EventBus
    bus = EventBus()
    bus.subscribe("task.completed", my_handler)
    await bus.publish(OPCEvent(event_type="task.completed", data={...}))
"""

from __future__ import annotations  # 啟用延遲型別註解評估，支援 X | Y 聯合型別語法

import asyncio  # 標準庫：提供非同步 Lock 與 gather 並發執行能力
from collections import defaultdict  # 標準庫：自動初始化缺失鍵的字典，簡化監聽者註冊
from typing import Any, Callable, Coroutine  # 標準庫：型別註解用，定義監聽者函數簽名

from opc.core.models import OPCEvent  # 匯入 OPC 統一事件模型，包含 event_type、data、timestamp 等欄位


# 監聽者函數型別別名。
# 型別：非同步函數，接收一個 OPCEvent 參數，返回 Coroutine[Any, Any, None]。
# 所有事件監聽者必須符合此簽名：async def handler(event: OPCEvent) -> None
Listener = Callable[[OPCEvent], Coroutine[Any, Any, None]]


class EventBus:
    """簡易非同步發布/訂閱事件匯流排，用於跨層解耦通訊。

    職責說明：
        管理事件監聽者的註冊與事件的分發。支援兩種訂閱模式：
        1. 按事件類型訂閱（subscribe）：僅接收指定類型的事件
        2. 全域訂閱（subscribe_all）：接收所有類型的事件

    關聯關係：
        - 由 opc/engine.py 建立並注入各子系統
        - 事件資料結構為 opc/core/models.py 中的 OPCEvent

    使用範例：
        bus = EventBus()
        bus.subscribe("task.started", on_task_started)
        bus.subscribe_all(audit_logger)
        await bus.publish(OPCEvent(event_type="task.started", data={"id": "t1"}))

    線程安全：
        使用 asyncio.Lock 保護監聽者列表的讀取，確保在發布事件期間
        不會因並發訂閱/取消訂閱而產生迭代錯誤。
    """

    def __init__(self) -> None:
        """初始化事件匯流排內部狀態。

        建立空的監聽者映射、全域監聽者列表、歷史記錄與非同步鎖。
        """
        # 按事件類型分組的監聽者字典。鍵為事件類型字串，值為該類型的監聽者列表。
        # 使用 defaultdict(list) 避免註冊時手動檢查鍵是否存在。
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        # 全域監聽者列表：接收所有類型的事件（用於審計、日誌等）
        self._global_listeners: list[Listener] = []
        # 事件歷史記錄：保存所有已發布的事件，供 get_history 查詢
        self._history: list[OPCEvent] = []
        # 延遲初始化的非同步鎖（避免在 __init__ 時綁定事件循環）
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        """取得或延遲建立非同步鎖。

        功能：
            採用延遲初始化模式，確保 Lock 在第一次使用時才建立，
            避免在模組導入階段綁定到錯誤的事件循環。

        返回值：
            asyncio.Lock — 用於保護監聽者列表讀取的非同步互斥鎖。
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def subscribe(self, event_type: str, listener: Listener) -> None:
        """註冊指定事件類型的監聽者。

        功能：
            將監聽者函數加入指定事件類型的訂閱列表。
            同一監聽者可重複註冊（會收到多次通知）。

        參數：
            event_type (str)：要訂閱的事件類型字串，
                例如 "task.started"、"task.completed"、"agent.error"。
            listener (Listener)：非同步回調函數，簽名為
                async def handler(event: OPCEvent) -> None。

        返回值：
            無
        """
        self._listeners[event_type].append(listener)

    def subscribe_all(self, listener: Listener) -> None:
        """註冊全域監聽者，接收所有類型的事件。

        功能：
            將監聽者加入全域列表，無論事件類型為何都會被通知。
            適用於審計日誌、指標收集等需要觀察所有事件的場景。

        參數：
            listener (Listener)：非同步回調函數，簽名同 subscribe。

        返回值：
            無
        """
        self._global_listeners.append(listener)

    async def publish(self, event: OPCEvent) -> None:
        """發布事件，通知所有匹配的監聽者。

        功能：
            1. 在鎖保護下將事件加入歷史記錄
            2. 快照匹配的監聽者列表（避免迭代期間被修改）
            3. 在鎖外並發執行所有監聽者（避免長時間持鎖）
            4. 使用 return_exceptions=True 確保單一監聽者異常不影響其他

        參數：
            event (OPCEvent)：要發布的事件物件，包含 event_type 和 data。

        返回值：
            無

        執行時機：
            由引擎或各層在狀態變更時調用，例如任務完成、代理錯誤等。
        """
        async with self._get_lock():
            self._history.append(event)
            # 在鎖內快照監聽者列表，避免發布期間其他協程修改列表
            typed = list(self._listeners.get(event.event_type, []))
            globl = list(self._global_listeners)
        # 在鎖外執行監聽者，避免非同步操作期間持有鎖造成阻塞
        tasks = [fn(event) for fn in typed + globl]
        if tasks:
            # 並發執行所有監聽者；return_exceptions=True 使異常不會中斷其他監聽者
            await asyncio.gather(*tasks, return_exceptions=True)

    def get_history(self, event_type: str | None = None, limit: int = 50) -> list[OPCEvent]:
        """查詢已發布的事件歷史記錄。

        功能：
            返回最近的事件列表，可選按事件類型過濾。
            用於除錯、觀測面板顯示最近活動。

        參數：
            event_type (str | None)：過濾條件。若指定則僅返回該類型事件；
                若為 None 則返回所有類型。預設 None。
            limit (int)：返回的最大事件數量，從最新往回取。
                取值範圍：正整數，預設 50。

        返回值：
            list[OPCEvent] — 符合條件的最近 N 個事件列表（時間升序）。

        被誰引用：
            - opc/layer6_observability/：觀測面板查詢最近事件
            - opc/cli/app.py：CLI 除錯命令顯示事件歷史
        """
        events = self._history
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        return events[-limit:]
