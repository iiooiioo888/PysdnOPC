"""任務路由器 — 已棄用。

模式選擇現在由使用者 metadata 決定，不再使用 LLM 路由。
此模組僅為向後相容保留。匯入 TaskRouter 仍可運作，
但 route() 回傳預設的 ModeSelection(TASK_MODE)。
"""

from __future__ import annotations  # 啟用延遲型別註解評估

import warnings  # 標準庫：棄用警告
from typing import Any  # 標準庫：型別註解

from loguru import logger  # 第三方庫：結構化日誌

from opc.core.models import ExecutionMode, ModeSelection  # 領域模型
from opc.layer1_perception.context_loader import LoadedContext  # 上下文載入器

RouterDecision = ModeSelection  # 路由決策型別別名


class TaskRouter:
    """已棄用 — 僅為向後相容保留。

    route() 現在回傳預設的 ModeSelection(TASK_MODE)，
    不再進行任何 LLM 呼叫。呼叫者應遷移到直接從使用者 metadata 讀取模式。
    """

    def __init__(self, llm: Any = None) -> None:
        self.llm = llm
        warnings.warn(
            "TaskRouter is deprecated. Mode is now determined by user metadata.",
            DeprecationWarning,
            stacklevel=2,
        )

    async def route(
        self,
        user_message: str,
        context: LoadedContext | None = None,
        preferences: dict[str, Any] | None = None,
    ) -> ModeSelection:
        logger.debug("TaskRouter.route() called — returning default TASK_MODE")
        return ModeSelection(
            mode=ExecutionMode.TASK_MODE,
            domains=["general"],
        )
