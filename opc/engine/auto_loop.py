"""自動循環引擎 — 自動重試、自癒、持續改進。

職責說明：
    提供多種自動化循環機制：
    1. 重試循環 (Retry Loop) — 失敗任務自動重試
    2. 自癒循環 (Self-Heal Loop) — 檢測問題 → 修復 → 驗證
    3. 質量門禁循環 (Quality Gate) — 產出 → 評審 → 返工 → 再評審
    4. 看門狗 (Watchdog) — 檢測卡住的任務並恢復
    5. 持續改進循環 (Improvement Loop) — 分析 → 優化 → 驗證

使用範例：
    from opc.engine.auto_loop import AutoLoopManager
    manager = AutoLoopManager(engine=engine)
    await manager.start()
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

from loguru import logger


class LoopType(Enum):
    """循環類型。"""
    RETRY = "retry"                # 失敗重試
    SELF_HEAL = "self_heal"        # 自癒
    QUALITY_GATE = "quality_gate"  # 質量門禁
    WATCHDOG = "watchdog"          # 看門狗
    IMPROVEMENT = "improvement"    # 持續改進


class LoopStatus(Enum):
    """循環狀態。"""
    IDLE = "idle"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    MAX_RETRIES = "max_retries"
    TIMEOUT = "timeout"


@dataclass
class LoopConfig:
    """循環配置。"""
    max_retries: int = 3              # 最大重試次數
    retry_delay_sec: float = 5.0      # 重試間隔（秒）
    backoff_multiplier: float = 1.5   # 退避倍數
    max_delay_sec: float = 60.0       # 最大延遲
    timeout_sec: float = 300.0        # 超時時間（秒）
    quality_threshold: float = 0.7    # 質量門檻 (0-1)
    auto_escalate: bool = True        # 超限後自動上報


@dataclass
class LoopRun:
    """單次循環運行記錄。"""
    loop_id: str
    loop_type: LoopType
    task_id: str
    role: str = ""
    status: LoopStatus = LoopStatus.IDLE
    attempt: int = 0
    max_attempts: int = 3
    started_at: float = 0.0
    last_attempt_at: float = 0.0
    completed_at: float = 0.0
    error_history: list[str] = field(default_factory=list)
    result: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AutoLoopManager:
    """自動循環管理器。

    管理所有自動化循環，提供統一的啟動/停止/監控接口。
    """

    def __init__(
        self,
        engine: Any = None,
        event_bus: Any = None,
        config: LoopConfig | None = None,
    ) -> None:
        self.engine = engine
        self.event_bus = event_bus
        self.config = config or LoopConfig()

        self._running = False
        self._watchdog_task: asyncio.Task[None] | None = None
        self._active_loops: dict[str, LoopRun] = {}
        self._loop_history: list[LoopRun] = []
        self._max_history = 200

        # 回調函數
        self._on_loop_complete: Callable[[LoopRun], Coroutine[Any, Any, None]] | None = None
        self._on_escalation: Callable[[LoopRun], Coroutine[Any, Any, None]] | None = None

    def set_callbacks(
        self,
        on_complete: Callable[[LoopRun], Coroutine[Any, Any, None]] | None = None,
        on_escalation: Callable[[LoopRun], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """設置回調函數。"""
        self._on_loop_complete = on_complete
        self._on_escalation = on_escalation

    async def start(self) -> None:
        """啟動自動循環管理器。"""
        if self._running:
            return
        self._running = True

        # 啟動看門狗
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        logger.info("AutoLoopManager started")

    async def stop(self) -> None:
        """停止自動循環管理器。"""
        self._running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        logger.info("AutoLoopManager stopped")

    # ── 重試循環 ──────────────────────────────────────────────────────

    async def retry_loop(
        self,
        task_id: str,
        execute_fn: Callable[..., Coroutine[Any, Any, Any]],
        *,
        role: str = "",
        max_retries: int | None = None,
        retry_delay: float | None = None,
        **kwargs: Any,
    ) -> LoopRun:
        """失敗重試循環。

        參數：
            task_id: 任務 ID
            execute_fn: 執行函數 (async)
            role: 角色名稱
            max_retries: 最大重試次數
            retry_delay: 重試間隔
            **kwargs: 傳遞給 execute_fn 的參數

        返回：
            LoopRun — 循環運行結果
        """
        loop_id = f"retry_{task_id}_{int(time.time())}"
        max_attempts = max_retries or self.config.max_retries
        delay = retry_delay or self.config.retry_delay_sec

        run = LoopRun(
            loop_id=loop_id,
            loop_type=LoopType.RETRY,
            task_id=task_id,
            role=role,
            max_attempts=max_attempts,
            started_at=time.time(),
        )
        self._active_loops[loop_id] = run

        for attempt in range(1, max_attempts + 1):
            run.attempt = attempt
            run.last_attempt_at = time.time()
            run.status = LoopStatus.RUNNING

            logger.info(f"[retry] task={task_id} attempt={attempt}/{max_attempts}")

            try:
                result = await execute_fn(**kwargs)
                run.status = LoopStatus.SUCCESS
                run.result = result
                run.completed_at = time.time()
                logger.info(f"[retry] task={task_id} succeeded on attempt {attempt}")
                break

            except Exception as e:
                error_msg = str(e)
                run.error_history.append(error_msg)
                logger.warning(f"[retry] task={task_id} attempt {attempt} failed: {error_msg}")

                if attempt < max_attempts:
                    # 指數退避
                    current_delay = min(delay * (self.config.backoff_multiplier ** (attempt - 1)), self.config.max_delay_sec)
                    logger.info(f"[retry] task={task_id} waiting {current_delay:.1f}s before retry")
                    await asyncio.sleep(current_delay)
                else:
                    run.status = LoopStatus.MAX_RETRIES
                    run.completed_at = time.time()
                    logger.error(f"[retry] task={task_id} exhausted {max_attempts} attempts")

        # 清理並記錄
        self._active_loops.pop(loop_id, None)
        self._record_history(run)

        # 發佈事件
        await self._emit_event("loop.completed", {
            "loop_id": loop_id,
            "loop_type": LoopType.RETRY.value,
            "task_id": task_id,
            "status": run.status.value,
            "attempts": run.attempt,
        })

        # 超限上報
        if run.status == LoopStatus.MAX_RETRIES and self.config.auto_escalate:
            await self._escalate(run)

        return run

    # ── 自癒循環 ──────────────────────────────────────────────────────

    async def self_heal_loop(
        self,
        task_id: str,
        detect_fn: Callable[..., Coroutine[Any, Any, Any]],
        fix_fn: Callable[..., Coroutine[Any, Any, Any]],
        verify_fn: Callable[..., Coroutine[Any, Any, Any]],
        *,
        role: str = "",
        max_cycles: int = 3,
        **kwargs: Any,
    ) -> LoopRun:
        """自癒循環：檢測問題 → 修復 → 驗證。

        參數：
            task_id: 任務 ID
            detect_fn: 檢測函數，返回問題描述或 None
            fix_fn: 修復函數
            verify_fn: 驗證函數，返回 True/False
            role: 角色名稱
            max_cycles: 最大循環次數
        """
        loop_id = f"heal_{task_id}_{int(time.time())}"

        run = LoopRun(
            loop_id=loop_id,
            loop_type=LoopType.SELF_HEAL,
            task_id=task_id,
            role=role,
            max_attempts=max_cycles,
            started_at=time.time(),
        )
        self._active_loops[loop_id] = run

        for cycle in range(1, max_cycles + 1):
            run.attempt = cycle
            run.status = LoopStatus.RUNNING

            logger.info(f"[self_heal] task={task_id} cycle={cycle}/{max_cycles}")

            try:
                # Step 1: 檢測
                issue = await detect_fn()
                if issue is None:
                    logger.info(f"[self_heal] task={task_id} no issue detected")
                    run.status = LoopStatus.SUCCESS
                    run.completed_at = time.time()
                    break

                logger.info(f"[self_heal] task={task_id} detected issue: {issue}")
                run.metadata["last_issue"] = str(issue)

                # Step 2: 修復
                await fix_fn(issue=issue)
                logger.info(f"[self_heal] task={task_id} fix applied")

                # Step 3: 驗證
                verified = await verify_fn()
                if verified:
                    logger.info(f"[self_heal] task={task_id} verified OK on cycle {cycle}")
                    run.status = LoopStatus.SUCCESS
                    run.completed_at = time.time()
                    break
                else:
                    logger.warning(f"[self_heal] task={task_id} verification failed on cycle {cycle}")

            except Exception as e:
                error_msg = str(e)
                run.error_history.append(error_msg)
                logger.error(f"[self_heal] task={task_id} cycle {cycle} error: {error_msg}")

            if cycle < max_cycles:
                await asyncio.sleep(self.config.retry_delay_sec)

        if run.status != LoopStatus.SUCCESS:
            run.status = LoopStatus.MAX_RETRIES
            run.completed_at = time.time()

        self._active_loops.pop(loop_id, None)
        self._record_history(run)
        await self._emit_event("loop.completed", {
            "loop_id": loop_id,
            "loop_type": LoopType.SELF_HEAL.value,
            "task_id": task_id,
            "status": run.status.value,
        })

        if run.status == LoopStatus.MAX_RETRIES and self.config.auto_escalate:
            await self._escalate(run)

        return run

    # ── 質量門禁循環 ──────────────────────────────────────────────────

    async def quality_gate_loop(
        self,
        task_id: str,
        produce_fn: Callable[..., Coroutine[Any, Any, Any]],
        review_fn: Callable[..., Coroutine[Any, Any, Any]],
        *,
        role: str = "",
        max_rounds: int = 3,
        quality_threshold: float | None = None,
        **kwargs: Any,
    ) -> LoopRun:
        """質量門禁循環：產出 → 評審 → 返工 → 再評審。

        參數：
            task_id: 任務 ID
            produce_fn: 產出函數，返回產出物
            review_fn: 評審函數，返回 (score, feedback)
            role: 角色名稱
            max_rounds: 最大輪次
            quality_threshold: 質量門檻
        """
        loop_id = f"quality_{task_id}_{int(time.time())}"
        threshold = quality_threshold or self.config.quality_threshold

        run = LoopRun(
            loop_id=loop_id,
            loop_type=LoopType.QUALITY_GATE,
            task_id=task_id,
            role=role,
            max_attempts=max_rounds,
            started_at=time.time(),
        )
        self._active_loops[loop_id] = run

        current_output = None
        feedback = None

        for round_num in range(1, max_rounds + 1):
            run.attempt = round_num
            run.status = LoopStatus.RUNNING

            logger.info(f"[quality_gate] task={task_id} round={round_num}/{max_rounds}")

            try:
                # Step 1: 產出
                current_output = await produce_fn(
                    previous_output=current_output,
                    feedback=feedback,
                )

                # Step 2: 評審
                score, feedback = await review_fn(
                    output=current_output,
                )

                logger.info(f"[quality_gate] task={task_id} score={score:.2f} threshold={threshold}")

                if score >= threshold:
                    logger.info(f"[quality_gate] task={task_id} passed quality gate")
                    run.status = LoopStatus.SUCCESS
                    run.result = current_output
                    run.metadata["final_score"] = score
                    run.completed_at = time.time()
                    break

                logger.info(f"[quality_gate] task={task_id} below threshold, feedback: {feedback[:100]}")
                run.metadata["last_score"] = score
                run.metadata["last_feedback"] = str(feedback)[:200]

            except Exception as e:
                error_msg = str(e)
                run.error_history.append(error_msg)
                logger.error(f"[quality_gate] task={task_id} round {round_num} error: {error_msg}")

        if run.status != LoopStatus.SUCCESS:
            run.status = LoopStatus.MAX_RETRIES
            run.completed_at = time.time()
            logger.warning(f"[quality_gate] task={task_id} failed quality gate after {max_rounds} rounds")

        self._active_loops.pop(loop_id, None)
        self._record_history(run)
        await self._emit_event("loop.completed", {
            "loop_id": loop_id,
            "loop_type": LoopType.QUALITY_GATE.value,
            "task_id": task_id,
            "status": run.status.value,
            "final_score": run.metadata.get("final_score", 0),
        })

        if run.status == LoopStatus.MAX_RETRIES and self.config.auto_escalate:
            await self._escalate(run)

        return run

    # ── 看門狗 ────────────────────────────────────────────────────────

    async def _watchdog_loop(self) -> None:
        """看門狗循環：定期檢查卡住的任務。"""
        while self._running:
            try:
                await self._watchdog_tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[watchdog] tick error: {e}")

            try:
                await asyncio.sleep(30)  # 每 30 秒檢查一次
            except asyncio.CancelledError:
                break

    async def _watchdog_tick(self) -> None:
        """看門狗單次檢查。"""
        if not self.engine:
            return

        # 檢查長時間運行的循環
        now = time.time()
        stalled = []

        for loop_id, run in list(self._active_loops.items()):
            if run.status != LoopStatus.RUNNING:
                continue
            elapsed = now - run.last_attempt_at
            if elapsed > self.config.timeout_sec:
                stalled.append(loop_id)

        for loop_id in stalled:
            run = self._active_loops.pop(loop_id, None)
            if run:
                run.status = LoopStatus.TIMEOUT
                run.completed_at = time.time()
                self._record_history(run)
                logger.warning(f"[watchdog] loop {loop_id} timed out after {self.config.timeout_sec}s")
                await self._emit_event("loop.timeout", {
                    "loop_id": loop_id,
                    "task_id": run.task_id,
                })

        # 檢查卡住的工作項（如果引擎可用）
        if hasattr(self.engine, 'store') and self.engine.store:
            try:
                await self._check_stalled_work_items()
            except Exception as e:
                logger.debug(f"[watchdog] stalled check error: {e}")

    async def _check_stalled_work_items(self) -> None:
        """檢查卡住的工作項。"""
        store = self.engine.store
        if not store:
            return

        try:
            from opc.core.models import TaskStatus
            running_tasks = await store.get_tasks(status=TaskStatus.RUNNING)
            now = time.time()

            for task in running_tasks:
                # 檢查是否超過 10 分鐘無更新
                updated = getattr(task, 'updated_at', None)
                if updated:
                    elapsed = now - updated.timestamp()
                    if elapsed > 600:  # 10 分鐘
                        logger.warning(f"[watchdog] task={task.id} stalled for {elapsed:.0f}s")
                        await self._emit_event("task.stalled", {
                            "task_id": task.id,
                            "elapsed_sec": elapsed,
                        })
        except Exception:
            pass

    # ── 持續改進循環 ──────────────────────────────────────────────────

    async def improvement_loop(
        self,
        task_id: str,
        analyze_fn: Callable[..., Coroutine[Any, Any, Any]],
        optimize_fn: Callable[..., Coroutine[Any, Any, Any]],
        execute_fn: Callable[..., Coroutine[Any, Any, Any]],
        *,
        role: str = "",
        max_cycles: int = 3,
        **kwargs: Any,
    ) -> LoopRun:
        """持續改進循環：分析 → 優化 → 執行 → 驗證。

        參數：
            task_id: 任務 ID
            analyze_fn: 分析函數，返回優化建議
            optimize_fn: 優化函數，應用建議
            execute_fn: 執行函數
            role: 角色名稱
            max_cycles: 最大循環次數
        """
        loop_id = f"improve_{task_id}_{int(time.time())}"

        run = LoopRun(
            loop_id=loop_id,
            loop_type=LoopType.IMPROVEMENT,
            task_id=task_id,
            role=role,
            max_attempts=max_cycles,
            started_at=time.time(),
        )
        self._active_loops[loop_id] = run

        for cycle in range(1, max_cycles + 1):
            run.attempt = cycle
            run.status = LoopStatus.RUNNING

            logger.info(f"[improve] task={task_id} cycle={cycle}/{max_cycles}")

            try:
                # Step 1: 分析
                suggestions = await analyze_fn()
                logger.info(f"[improve] task={task_id} suggestions: {str(suggestions)[:100] if suggestions else 'none'}")

                if not suggestions:
                    run.status = LoopStatus.SUCCESS
                    run.completed_at = time.time()
                    break

                # Step 2: 優化
                await optimize_fn(suggestions=suggestions)

                # Step 3: 執行
                result = await execute_fn()
                run.result = result

                run.status = LoopStatus.SUCCESS
                run.completed_at = time.time()
                logger.info(f"[improve] task={task_id} improvement cycle {cycle} completed")
                break

            except Exception as e:
                error_msg = str(e)
                run.error_history.append(error_msg)
                logger.error(f"[improve] task={task_id} cycle {cycle} error: {error_msg}")

        if run.status != LoopStatus.SUCCESS:
            run.status = LoopStatus.MAX_RETRIES
            run.completed_at = time.time()

        self._active_loops.pop(loop_id, None)
        self._record_history(run)
        await self._emit_event("loop.completed", {
            "loop_id": loop_id,
            "loop_type": LoopType.IMPROVEMENT.value,
            "task_id": task_id,
            "status": run.status.value,
        })

        return run

    # ── 狀態查詢 ──────────────────────────────────────────────────────

    def get_active_loops(self) -> list[dict[str, Any]]:
        """獲取所有活動循環。"""
        return [
            {
                "loop_id": r.loop_id,
                "type": r.loop_type.value,
                "task_id": r.task_id,
                "role": r.role,
                "status": r.status.value,
                "attempt": r.attempt,
                "max_attempts": r.max_attempts,
                "elapsed": time.time() - r.started_at if r.started_at else 0,
            }
            for r in self._active_loops.values()
        ]

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """獲取循環歷史。"""
        return [
            {
                "loop_id": r.loop_id,
                "type": r.loop_type.value,
                "task_id": r.task_id,
                "status": r.status.value,
                "attempts": r.attempt,
                "duration": (r.completed_at - r.started_at) if r.completed_at and r.started_at else 0,
                "errors": len(r.error_history),
            }
            for r in self._loop_history[-limit:]
        ]

    def get_stats(self) -> dict[str, Any]:
        """獲取統計信息。"""
        total = len(self._loop_history)
        success = sum(1 for r in self._loop_history if r.status == LoopStatus.SUCCESS)
        failed = sum(1 for r in self._loop_history if r.status in (LoopStatus.MAX_RETRIES, LoopStatus.TIMEOUT))

        by_type: dict[str, int] = {}
        for r in self._loop_history:
            by_type[r.loop_type.value] = by_type.get(r.loop_type.value, 0) + 1

        return {
            "total_runs": total,
            "success": success,
            "failed": failed,
            "success_rate": (success / total * 100) if total > 0 else 0,
            "active_loops": len(self._active_loops),
            "by_type": by_type,
        }

    # ── 內部方法 ──────────────────────────────────────────────────────

    def _record_history(self, run: LoopRun) -> None:
        """記錄到歷史。"""
        self._loop_history.append(run)
        if len(self._loop_history) > self._max_history:
            self._loop_history = self._loop_history[-self._max_history:]

    async def _emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """發佈事件。"""
        if self.event_bus:
            try:
                from opc.core.models import OPCEvent
                await self.event_bus.publish(OPCEvent(
                    event_type=event_type,
                    payload=data,
                ))
            except Exception:
                pass

    async def _escalate(self, run: LoopRun) -> None:
        """上報失敗。"""
        logger.warning(
            f"[escalate] loop {run.loop_id} failed after {run.attempt} attempts, escalating"
        )

        if self._on_escalation:
            await self._on_escalation(run)

        await self._emit_event("loop.escalated", {
            "loop_id": run.loop_id,
            "loop_type": run.loop_type.value,
            "task_id": run.task_id,
            "attempts": run.attempt,
            "errors": run.error_history,
        })


def format_loop_stats(stats: dict[str, Any]) -> str:
    """格式化循環統計。"""
    lines = ["🔄 自動循環統計\n"]

    total = stats.get("total_runs", 0)
    success = stats.get("success", 0)
    rate = stats.get("success_rate", 0)

    emoji = "🟢" if rate >= 80 else "🟡" if rate >= 50 else "🔴"
    lines.append(f"  {emoji} 成功率: {rate:.0f}% ({success}/{total})")
    lines.append(f"  🔄 活動循環: {stats.get('active_loops', 0)}")

    by_type = stats.get("by_type", {})
    if by_type:
        lines.append("\n  按類型:")
        type_names = {
            "retry": "🔁 重試",
            "self_heal": "🩹 自癒",
            "quality_gate": "✅ 質量門禁",
            "watchdog": "🐕 看門狗",
            "improvement": "📈 持續改進",
        }
        for t, count in by_type.items():
            lines.append(f"    {type_names.get(t, t)}: {count}")

    return "\n".join(lines)
