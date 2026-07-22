"""控制器本地的活動任務執行嘗試註冊表模組。

職責說明：
    追蹤當前控制器（controller）中正在執行的任務嘗試（attempt）。
    持久化的任務行（資料庫）描述的是持久的工作流狀態，但無法證明擁有
    執行協程的控制器是否仍然存活。此註冊表刻意保存在記憶體中，
    由同一控制器擁有的所有引擎共享，用於：
    1. 防止同一任務被重複分發
    2. 優雅關機時等待所有執行完成
    3. 交接（handoff）机制：在關機期間安全地接受已排程的請求

關聯關係：
    - 被 opc/engine.py 在任務分發/取消時調用
    - 被 opc/layer2_organization/company_mode.py 在公司模式排程時調用
    - 被 opc/plugins/office_ui/ 的 WebSocket handler 在接收請求時調用

設計原則：
    - 純記憶體結構，不持久化（控制器重啟後自然清空）
    - 使用 ContextVar 在協程間安全傳遞 handoff/driver 上下文
    - 支援短暫重疊：取消和重新分發交叉時，同一任務可有多個 attempt
"""

from __future__ import annotations  # 啟用延遲型別註解評估

import asyncio  # 標準庫：提供 Lock（範圍鎖）和 Event（handoff 排空信號）
from contextlib import contextmanager  # 標準庫：建立上下文管理器（bind_handoff、bind_driver_attempt）
from contextvars import ContextVar  # 標準庫：協程安全的上下文變數，傳遞 handoff/driver 令牌
import uuid  # 標準庫：產生唯一的 attempt/handoff 令牌
from collections.abc import Iterator  # 標準庫：contextmanager 的返回型別註解


# 當前協程的 handoff（交接）上下文變數。
# 型別：tuple[ActiveTaskRunRegistry, str] | None
# 當 WS handler 接受了一個請求但尚未開始執行時，透過此變數將
# handoff 令牌傳遞給下游的 register() 調用。
_CURRENT_HANDOFF: ContextVar[tuple[object, str] | None] = ContextVar(
    "opc_active_task_run_handoff",
    default=None,
)

# 當前協程的 driver attempt（驅動嘗試）上下文變數。
# 型別：tuple[ActiveTaskRunRegistry, str] | None
# 當排程器（scheduler）正在執行且需要建立子協程時，透過此變數
# 允許子協程的 register() 在 admission 關閉後仍可註冊。
_CURRENT_DRIVER_ATTEMPT: ContextVar[tuple[object, str] | None] = ContextVar(
    "opc_active_task_run_driver_attempt",
    default=None,
)


class ActiveTaskRunAdmissionClosed(RuntimeError):
    """當執行註冊在關機准入關閉後嘗試啟動時拋出的異常。

    使用場景：
        控制器開始優雅關機後，新的任務執行請求會被拒絕。
        此異常告知呼叫者應停止嘗試分發新任務。
    """


class ActiveTaskRunRegistry:
    """活動任務執行嘗試註冊表，以 (project_id, task_id) 為鍵追蹤。

    職責說明：
        管理任務執行嘗試的註冊/取消註冊，以及關機期間的准入控制。
        一個任務可以短暫擁有多個重疊的嘗試（取消和重新分發交叉時），
        每個註冊獲得獨立令牌，任務在所有令牌移除後才視為非活動。

    關聯關係：
        - 由 opc/engine.py 建立並在所有子引擎間共享
        - 被 WebSocket handler 用於判斷任務是否正在執行
        - 被關機流程用於等待所有活動執行完成

    使用範例：
        registry = ActiveTaskRunRegistry()
        token = registry.register("proj1", "task-123")
        # ... 執行任務 ...
        registry.unregister("proj1", "task-123", token)

    線程安全：
        本註冊表設計為在單一事件循環內使用（asyncio 環境），
        不需要執行緒鎖。範圍鎖（scope_lock）用於序列化同一
        runtime session 的並發操作。
    """

    def __init__(self) -> None:
        """初始化註冊表的內部狀態。"""
        # 活動嘗試映射：鍵為 (project_id, task_id)，值為該任務的所有 attempt 令牌集合。
        # 使用集合而非計數器，因為每個 attempt 有獨立的生命週期。
        self._attempts: dict[tuple[str, str], set[str]] = {}
        # 範圍鎖映射：鍵為 (project_id, runtime_session_id)，值為 asyncio.Lock。
        # 用於序列化同一工作階段的並發操作（避免競態條件）。
        self._scope_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Handoff 引用計數：鍵為 handoff 令牌，值為剩餘引用數。
        # 當引用數歸零時表示該 handoff 已完成或被撤銷。
        self._handoff_refs: dict[str, int] = {}
        # Handoff 排空信號：當所有 handoff 完成時觸發（set），有未完成 handoff 時清除（clear）。
        self._handoffs_drained = asyncio.Event()
        self._handoffs_drained.set()  # 初始狀態：無待處理 handoff
        # 准入控制標記：True 表示已關閉准入，拒絕新的執行註冊。
        self._admission_closed = False

    @staticmethod
    def _key(project_id: str | None, task_id: str | None) -> tuple[str, str]:
        """建構正規化的查找鍵。

        參數：
            project_id (str | None)：專案 ID。None 或空字串回退為 "default"。
            task_id (str | None)：任務 ID。不可為空（拋出 ValueError）。

        返回值：
            tuple[str, str] — (project_id, task_id) 正規化鍵。
        """
        project = str(project_id or "default").strip() or "default"
        task = str(task_id or "").strip()
        if not task:
            raise ValueError("task_id is required")
        return project, task

    def register(self, project_id: str | None, task_id: str | None) -> str:
        """註冊一個新的任務執行嘗試。

        功能：
            為指定任務建立新的執行嘗試令牌。在准入關閉後，
            僅允許持有 handoff 令牌或 driver attempt 令牌的註冊
            （這些是關機前已接受的請求的延續）。

        參數：
            project_id (str | None)：專案 ID。
            task_id (str | None)：任務 ID。

        返回值：
            str — 唯一的 attempt 令牌（32 位十六進位字串）。
            任務完成後必須使用此令牌調用 unregister。

        異常：
            ActiveTaskRunAdmissionClosed：准入已關閉且無有效 handoff/driver 時。

        被誰引用：
            - opc/engine.py：任務開始執行時
            - opc/layer2_organization/company_mode.py：公司模式排程時
        """
        handoff_token = self._current_pending_handoff_token()
        driver_attempt_token = self._current_driver_attempt_token()
        # 准入關閉後，僅 handoff 或 driver attempt 的延續可以註冊
        if (
            self._admission_closed
            and handoff_token is None
            and driver_attempt_token is None
        ):
            raise ActiveTaskRunAdmissionClosed(
                "task execution admission is closed for controller shutdown"
            )
        key = self._key(project_id, task_id)
        attempt_token = uuid.uuid4().hex
        self._attempts.setdefault(key, set()).add(attempt_token)
        # 若有待處理的 handoff，在第一次實際執行註冊時結算它。
        # 預約本身刻意不被 is_active()/active_task_ids() 報告；僅此 attempt 是。
        if handoff_token is not None:
            self._settle_handoff(handoff_token)
        return attempt_token

    @contextmanager
    def bind_driver_attempt(self, attempt_token: str) -> Iterator[None]:
        """綁定 driver attempt 上下文，允許子協程在准入關閉後仍可註冊。

        功能：
            當排程器已在執行中（持有有效的 attempt），其建立的子協程
            需要註冊新的 attempt 時，透過此上下文管理器傳遞權限。
            解決的問題：關機關閉准入後，已运行的排程器可能正處於
            原子性的 WorkItem 認領和子協程建立之間。

        參數：
            attempt_token (str)：當前排程器的有效 attempt 令牌。

        異常：
            ValueError：attempt_token 不在活動嘗試中時拋出。

        使用範例：
            with registry.bind_driver_attempt(parent_token):
                child_token = registry.register(proj, child_task)
        """
        if not self._attempt_token_is_active(attempt_token):
            raise ValueError("driver attempt is not active")
        context_token = _CURRENT_DRIVER_ATTEMPT.set((self, attempt_token))
        try:
            yield
        finally:
            _CURRENT_DRIVER_ATTEMPT.reset(context_token)

    def reserve_handoff(self) -> str:
        """預約一個已接受的入口請求，直到其執行被註冊。

        功能：
            在 WS router 接受請求和 register() 之間建立橋樑。
            預約是控制器本地的同步機制，不會成為第二個存活來源。
            解決的問題：關機開始後，已接受但尚未開始執行的請求
            需要一個機制來通知關機流程「還有請求在排隊」。

        返回值：
            str — handoff 令牌（32 位十六進位字串）。

        異常：
            ActiveTaskRunAdmissionClosed：准入已關閉時拋出。

        被誰引用：
            - opc/plugins/office_ui/ WebSocket handler：接受使用者請求時
        """
        if self._admission_closed:
            raise ActiveTaskRunAdmissionClosed(
                "task execution admission is closed for controller shutdown"
            )
        token = uuid.uuid4().hex
        self._handoff_refs[token] = 1
        self._handoffs_drained.clear()  # 有未完成的 handoff，清除排空信號
        return token

    @contextmanager
    def bind_handoff(self, handoff_token: str) -> Iterator[None]:
        """將 handoff 預約傳播到入口處理常式建立的子任務中。

        功能：
            透過 ContextVar 將 handoff 令牌傳遞給下游協程，
            使其 register() 調用能自動結算此預約。

        參數：
            handoff_token (str)：由 reserve_handoff() 產生的令牌。

        異常：
            ValueError：令牌不在待處理列表中時拋出。
        """
        if handoff_token not in self._handoff_refs:
            raise ValueError("handoff reservation is not pending")
        context_token = _CURRENT_HANDOFF.set((self, handoff_token))
        try:
            yield
        finally:
            _CURRENT_HANDOFF.reset(context_token)

    def retain_current_handoff(self) -> str | None:
        """為新排程的協程保留當前綁定的 handoff 預約（增加引用計數）。

        功能：
            當一個入口處理常式需要將 handoff 傳遞給多個子協程時，
            每個子協程調用此方法增加引用計數。

        返回值：
            str | None — 當前 handoff 令牌，若無綁定則返回 None。
        """
        handoff_token = self._current_pending_handoff_token()
        if handoff_token is None:
            return None
        self._handoff_refs[handoff_token] += 1
        return handoff_token

    def release_current_handoff(self) -> bool:
        """釋放當前綁定的 handoff（表示該請求不會開始執行）。

        返回值：
            bool — True 表示成功釋放。無綁定時返回 False。
        """
        handoff_token = self._current_pending_handoff_token()
        if handoff_token is None:
            return False
        return self.release_handoff(handoff_token)

    def release_handoff(self, handoff_token: str) -> bool:
        """釋放一個 handoff 引用，引用歸零時結算該預約。

        參數：
            handoff_token (str)：要釋放的 handoff 令牌。

        返回值：
            bool — True 表示成功釋放。令牌不存在時返回 False。
        """
        refs = self._handoff_refs.get(handoff_token)
        if refs is None:
            return False
        if refs > 1:
            self._handoff_refs[handoff_token] = refs - 1
            return True
        self._settle_handoff(handoff_token)
        return True

    def revoke_handoff(self, handoff_token: str) -> bool:
        """撤銷 handoff 預約的所有引用（強制結算）。

        功能：
            控制器關機時使用：在同步取消一個尚未註冊執行的請求後，
            強制結算其 handoff。比 release_handoff 更強：
            即使回調因取消清理而延遲，被撤銷的請求也不會：
            1. 保持關機屏障開啟
            2. 在准入關閉後註冊工作

        參數：
            handoff_token (str)：要撤銷的 handoff 令牌。

        返回值：
            bool — True 表示成功撤銷。令牌不存在時返回 False。
        """
        if handoff_token not in self._handoff_refs:
            return False
        self._settle_handoff(handoff_token)
        return True

    def _current_pending_handoff_token(self) -> str | None:
        """取得當前協程綁定的待處理 handoff 令牌（內部方法）。

        返回值：
            str | None — 有效的 handoff 令牌，或 None（無綁定/已結算/非本註冊表）。
        """
        binding = _CURRENT_HANDOFF.get()
        if binding is None or binding[0] is not self:
            return None
        token = binding[1]
        return token if token in self._handoff_refs else None

    def _current_driver_attempt_token(self) -> str | None:
        """取得當前協程綁定的 driver attempt 令牌（內部方法）。

        返回值：
            str | None — 有效的 attempt 令牌，或 None。
        """
        binding = _CURRENT_DRIVER_ATTEMPT.get()
        if binding is None or binding[0] is not self:
            return None
        token = binding[1]
        return token if self._attempt_token_is_active(token) else None

    def _attempt_token_is_active(self, attempt_token: str) -> bool:
        """檢查 attempt 令牌是否仍在任何任務的活動集合中（內部方法）。"""
        return any(
            attempt_token in attempts
            for attempts in self._attempts.values()
        )

    def _settle_handoff(self, handoff_token: str) -> None:
        """結算一個 handoff 預約：移除引用並在全部排空時觸發信號（內部方法）。"""
        self._handoff_refs.pop(handoff_token, None)
        if not self._handoff_refs:
            self._handoffs_drained.set()  # 所有 handoff 已完成

    @property
    def admission_closed(self) -> bool:
        """准入是否已關閉（唯讀屬性）。"""
        return self._admission_closed

    def close_admission(self) -> None:
        """關閉准入：拒絕未來的嘗試，但不中斷已在執行中的嘗試。

        被誰引用：
            - close_admission_and_wait_for_handoffs()：作為第一步
            - opc/engine.py：關機流程開始時
        """
        self._admission_closed = True

    async def close_admission_and_wait_for_handoffs(self) -> None:
        """關閉准入並等待所有已接受的請求完成交接。

        功能：
            關機流程的核心等待點：
            1. 關閉准入（拒絕新請求）
            2. 等待所有待處理的 handoff 完成（register 或 release）
            注意：等待的是「交接」而非「執行完成」，
            因為執行可能很長時間。

        執行時機：
            控制器優雅關機時，在取消所有任務之前調用。
        """
        self.close_admission()
        while self._handoff_refs:
            await self._handoffs_drained.wait()

    @property
    def pending_handoff_count(self) -> int:
        """當前待處理的 handoff 數量（唯讀屬性）。"""
        return len(self._handoff_refs)

    def is_handoff_pending(self, handoff_token: str | None) -> bool:
        """檢查指定的 handoff 令牌是否仍在待處理中。

        參數：
            handoff_token (str | None)：handoff 令牌。

        返回值：
            bool — True 表示仍在待處理中。
        """
        return bool(handoff_token and handoff_token in self._handoff_refs)

    def scope_lock(
        self,
        project_id: str | None,
        runtime_session_id: str | None,
    ) -> asyncio.Lock:
        """取得指定運行時範圍的共享鎖。

        功能：
            返回 (project_id, runtime_session_id) 对应的 asyncio.Lock。
            同一範圍的所有操作共享同一把鎖，確保序列化執行。
            鎖按需建立並快取。

        參數：
            project_id (str | None)：專案 ID。None 回退為 "default"。
            runtime_session_id (str | None)：運行時工作階段 ID（必填）。

        返回值：
            asyncio.Lock — 該範圍的共享非同步鎖。

        異常：
            ValueError：runtime_session_id 為空時拋出。

        被誰引用：
            - opc/engine.py：序列化同一 session 的並發任務操作
        """
        project = str(project_id or "default").strip() or "default"
        session = str(runtime_session_id or "").strip()
        if not session:
            raise ValueError("runtime_session_id is required")
        key = (project, session)
        lock = self._scope_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._scope_locks[key] = lock
        return lock

    def unregister(
        self,
        project_id: str | None,
        task_id: str | None,
        attempt_token: str,
    ) -> bool:
        """取消註冊一個任務執行嘗試。

        功能：
            移除指定的 attempt 令牌。當任務的最後一個 attempt 被移除後，
            該任務從活動映射中完全刪除。

        參數：
            project_id (str | None)：專案 ID。
            task_id (str | None)：任務 ID。
            attempt_token (str)：由 register() 返回的令牌。

        返回值：
            bool — True 表示成功取消註冊。令牌不存在時返回 False。

        被誰引用：
            - opc/engine.py：任務執行完成或取消時
        """
        key = self._key(project_id, task_id)
        attempts = self._attempts.get(key)
        if not attempts or attempt_token not in attempts:
            return False
        attempts.remove(attempt_token)
        if not attempts:
            self._attempts.pop(key, None)  # 最後一個 attempt 移除後清理鍵
        return True

    def is_active(self, project_id: str | None, task_id: str | None) -> bool:
        """檢查指定任務是否有活動的執行嘗試。

        參數：
            project_id (str | None)：專案 ID。
            task_id (str | None)：任務 ID。

        返回值：
            bool — True 表示該任務有至少一個活動的 attempt。
        """
        return bool(self._attempts.get(self._key(project_id, task_id)))

    def active_task_ids(self, project_id: str | None) -> set[str]:
        """取得指定專案中所有活動任務的 ID 集合。

        參數：
            project_id (str | None)：專案 ID。

        返回值：
            set[str] — 活動任務 ID 的集合。

        被誰引用：
            - opc/plugins/office_ui/：顯示當前執行中的任務列表
            - opc/engine.py：關機時枚舉需要取消的任務
        """
        project = str(project_id or "default").strip() or "default"
        return {
            task_id
            for (candidate_project, task_id), attempts in self._attempts.items()
            if candidate_project == project and attempts
        }

    def attempt_count(self, project_id: str | None, task_id: str | None) -> int:
        """取得指定任務的活動嘗試數量。

        參數：
            project_id (str | None)：專案 ID。
            task_id (str | None)：任務 ID。

        返回值：
            int — 活動嘗試數量。通常為 0 或 1，短暫重疊時可能 > 1。
        """
        return len(self._attempts.get(self._key(project_id, task_id), ()))
