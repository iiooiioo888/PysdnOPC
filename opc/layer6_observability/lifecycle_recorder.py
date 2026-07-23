"""規範化任務生命週期事件記錄器。

職責說明：
    將任務生命週期中的關鍵狀態轉換（建立、啟動、完成、失敗、取消）
    以結構化 JSON Lines 格式持久化到磁碟，供外部分析工具（如 Better Loop）
    評估重複工作流候選並路由至持久化所有者。

關聯關係：
    - 由 opc/engine/_core.py 在初始化時建立並注入
    - 在任務生命週期關鍵點被調用（建立、啟動、完成）
    - 輸出存放於 {opc_home}/logs/lifecycle/ 目錄

使用範例：
    from opc.layer6_observability.lifecycle_recorder import TaskLifecycleRecorder
    recorder = TaskLifecycleRecorder(opc_home)
    await recorder.record_task_created(task_id="t1", project_id="p1", mode="task")
    await recorder.record_task_completed(task_id="t1", project_id="p1", outcome="done", duration_ms=1234)
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


class TaskLifecycleRecorder:
    """結構化任務生命週期事件記錄器。

    將任務生命週期事件以 JSON Lines 格式寫入
    {opc_home}/logs/lifecycle/lifecycle_events.jsonl，
    每行一個結構化事件記錄，包含規範化欄位：
    - event: 事件類型（task_created / task_started / task_completed）
    - task_id: 任務 ID
    - project_id: 專案 ID
    - session_id: 工作階段 ID
    - mode: 執行模式（task / company）
    - outcome: 結果（done / failed / cancelled）
    - duration_ms: 執行耗時（毫秒）
    - agent: 執行代理
    - role_id: 角色 ID
    - timestamp: ISO 8601 時間戳
    - metadata: 額外結構化元資料
    """

    def __init__(self, opc_home: Path | str) -> None:
        self._opc_home = Path(opc_home)
        self._log_dir = self._opc_home / "logs" / "lifecycle"
        self._log_file = self._log_dir / "lifecycle_events.jsonl"
        # 規範化事件索引檔案（供外部分析工具如 Better Loop 發現）
        self._normalized_index = self._opc_home / "normalized_events.jsonl"
        self._start_times: dict[str, float] = {}
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        """確保日誌目錄存在。"""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.opt(exception=True).debug("TaskLifecycleRecorder: failed to create log directory")

    def _utcnow_iso(self) -> str:
        """返回當前 UTC 時間的 ISO 8601 字串。"""
        return datetime.now(timezone.utc).isoformat()

    async def _append_event(self, record: dict[str, Any]) -> None:
        """將結構化事件記錄追加到 JSONL 檔案（含規範化索引）。"""
        try:
            self._ensure_dir()
            line = json.dumps(record, ensure_ascii=False, default=str)
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            # 同步寫入規範化索引（供 Better Loop 等分析工具發現）
            with open(self._normalized_index, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            logger.opt(exception=True).debug("TaskLifecycleRecorder: failed to write lifecycle event")

    async def record_task_created(
        self,
        *,
        task_id: str,
        project_id: str = "default",
        session_id: str = "",
        mode: str = "task",
        title: str = "",
        agent: str = "",
        role_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """記錄任務建立事件。

        參數：
            task_id: 任務唯一 ID
            project_id: 專案 ID
            session_id: 工作階段 ID
            mode: 執行模式（task / company）
            title: 任務標題（截斷至 200 字元）
            agent: 指定執行代理
            role_id: 角色 ID
            metadata: 額外元資料
        """
        self._start_times[task_id] = time.monotonic()
        record = {
            "event": "task_created",
            "task_id": task_id,
            "project_id": project_id,
            "session_id": session_id,
            "mode": mode,
            "title": title[:200],
            "agent": agent,
            "role_id": role_id,
            "timestamp": self._utcnow_iso(),
            "metadata": metadata or {},
        }
        await self._append_event(record)

    async def record_task_started(
        self,
        *,
        task_id: str,
        project_id: str = "default",
        session_id: str = "",
        mode: str = "task",
        agent: str = "",
        role_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """記錄任務啟動事件。

        參數：
            task_id: 任務唯一 ID
            project_id: 專案 ID
            session_id: 工作階段 ID
            mode: 執行模式
            agent: 實際執行代理
            role_id: 角色 ID
            metadata: 額外元資料
        """
        if task_id not in self._start_times:
            self._start_times[task_id] = time.monotonic()
        record = {
            "event": "task_started",
            "task_id": task_id,
            "project_id": project_id,
            "session_id": session_id,
            "mode": mode,
            "agent": agent,
            "role_id": role_id,
            "timestamp": self._utcnow_iso(),
            "metadata": metadata or {},
        }
        await self._append_event(record)

    async def record_task_completed(
        self,
        *,
        task_id: str,
        project_id: str = "default",
        session_id: str = "",
        mode: str = "task",
        outcome: str = "done",
        duration_ms: int | None = None,
        agent: str = "",
        role_id: str = "",
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """記錄任務完成事件（含結果與耗時）。

        參數：
            task_id: 任務唯一 ID
            project_id: 專案 ID
            session_id: 工作階段 ID
            mode: 執行模式
            outcome: 結果狀態（done / failed / cancelled）
            duration_ms: 執行耗時（毫秒），None 則自動計算
            agent: 執行代理
            role_id: 角色 ID
            title: 任務標題
            metadata: 額外元資料
        """
        if duration_ms is None:
            start = self._start_times.pop(task_id, None)
            if start is not None:
                duration_ms = int((time.monotonic() - start) * 1000)
        else:
            self._start_times.pop(task_id, None)

        record = {
            "event": "task_completed",
            "task_id": task_id,
            "project_id": project_id,
            "session_id": session_id,
            "mode": mode,
            "outcome": outcome,
            "duration_ms": duration_ms,
            "agent": agent,
            "role_id": role_id,
            "title": title[:200],
            "timestamp": self._utcnow_iso(),
            "metadata": metadata or {},
        }
        await self._append_event(record)

    def get_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """讀取最近的生命週期事件記錄。

        參數：
            limit: 返回的最大事件數量

        返回值：
            最近 N 個結構化事件記錄列表。
        """
        if not self._log_file.exists():
            return []
        try:
            lines = self._log_file.read_text(encoding="utf-8").strip().splitlines()
            events = []
            for line in lines[-limit:]:
                try:
                    events.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
            return events
        except OSError:
            return []
