"""Worker 訊息信封正規化工具模組。

職責說明：
    負責將來自不同來源（CLI、Office UI、外部代理）的 worker 訊息
    正規化為統一的「信封」格式。信封包含訊息分類（chat/protocol/notification）、
    協議類型、通知種類、是否需要回覆等路由資訊，供下游的訊息匯流排
    和協作系統正確分發。

關聯關係：
    - 被 opc/layer2_organization/comms.py 在發送訊息前調用
    - 被 opc/layer2_organization/communication.py 在接收訊息時調用
    - 被 opc/engine.py 在處理 worker 回報時調用

設計原則：
    所有正規化函數均為純函數（無副作用），可安全地在任何環境中調用。
    正規化邏輯採用「寬容接收、嚴格輸出」原則：接受各種格式的輸入，
    但輸出始終為標準化的信封欄位。
"""

from __future__ import annotations  # 啟用延遲型別註解評估

from typing import Any  # 標準庫：metadata 字典的值型別註解


# 訊息分類集合。所有 worker 訊息必須歸類為以下三種之一：
#   - "chat"：一般對話訊息（預設分類）
#   - "protocol"：協議層訊息（審批請求、關機指令等系統級通訊）
#   - "notification"：通知類訊息（狀態變更、完成通知等，通常不需要回覆）
MESSAGE_CLASSES = {"chat", "protocol", "notification"}

# 協議訊息的合法類型集合。
# 這些類型代表系統級的結構化通訊：
#   - "approval_request"：審批請求（需要上級或使用者批准）
#   - "shutdown_request"：關機請求（要求 worker 停止）
#   - "shutdown_response"：關機回應（worker 確認停止）
#   - "ack"：確認收到（通用的訊息確認）
# 注意：之前有 10 個已宣告但從未使用的協議類型已被清理移除。
_PROTOCOL_TYPES = {
    "approval_request",
    "shutdown_request",
    "shutdown_response",
    "ack",
}

# 通知訊息的合法種類集合。
# 這些種類代表 worker 向管理層回報的狀態通知：
#   - "idle"：worker 空閒，可接受新任務
#   - "task_complete"：任務完成
#   - "blocked"：worker 被阻塞，需要協助
#   - "handoff_ready"：交接準備就緒
#   - "completion"：整體完成通知
#   - "status_digest"：狀態摘要（定期回報）
#   - "permission_needed"：需要權限
#   - "error"：錯誤通知
_NOTIFICATION_KINDS = {
    "idle",
    "task_complete",
    "blocked",
    "handoff_ready",
    "completion",
    "status_digest",
    "permission_needed",
    "error",
}

# 語義類型 → 協議類型的映射表。
# 當訊息未明確指定 protocol_type 時，嘗試從 semantic_type 推斷。
_SEMANTIC_PROTOCOL_MAP = {
    "approval_request": "approval_request",
}

# 語義類型 → 通知種類的映射表。
# 當訊息未明確指定 notification_kind 時，嘗試從 semantic_type 推斷。
_SEMANTIC_NOTIFICATION_MAP = {
    "idle_notification": "idle",
    "handoff_ready": "handoff_ready",
    "work_item_result": "task_complete",
    "blocker": "blocked",
    "completion": "completion",
    "status_digest": "status_digest",
}


def _clean_text(value: Any) -> str:
    """將任意值安全地轉換為去除首尾空白的字串。

    參數：
        value (Any)：任意值（None、字串、數字等）。

    返回值：
        str — 去除首尾空白的字串。None 或空值返回空字串。
    """
    return str(value or "").strip()


def _clean_choice(value: Any, allowed: set[str]) -> str:
    """將值正規化並驗證是否在允許集合中。

    功能：
        將輸入轉為小寫字串後檢查是否屬於允許的選項集合。
        不在集合中則返回空字串（表示無效選擇）。

    參數：
        value (Any)：待驗證的值。
        allowed (set[str])：合法選項集合。

    返回值：
        str — 合法的小寫選項字串，或空字串（無效時）。
    """
    normalized = _clean_text(value).lower()
    return normalized if normalized in allowed else ""


def _coerce_bool(value: Any) -> bool | None:
    """將任意值強制轉換為布林值。

    功能：
        支援多種布林表示法：
        - 真值：True、"true"、"1"、"yes"、"y"、"on"
        - 假值：False、"false"、"0"、"no"、"n"、"off"
        - 無法判斷時返回 None

    參數：
        value (Any)：待轉換的值。

    返回值：
        bool | None — 轉換結果。無法判斷時返回 None。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    return None


def normalize_worker_envelope_metadata(
    metadata: dict[str, Any] | None = None,
    *,
    msg_type: str = "",
    semantic_type: str = "",
    transport_kind: str = "",
    from_agent: str = "",
    reply_needed: bool = False,
    worker_id: str = "",
    task_id: str = "",
    projection_id: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """正規化 worker 訊息信封的 metadata，產生路由所需的標準欄位。

    功能：
        接收原始 metadata 和各種上下文字段，透過多層推斷邏輯產生：
        1. message_class：訊息分類（chat/protocol/notification）
        2. protocol_type：協議類型（僅 protocol 類訊息）
        3. notification_kind：通知種類（僅 notification 類訊息）
        4. actionable：是否需要接收方採取行動
        5. worker_id：來源 worker 識別碼
        6. origin_*：來源追蹤欄位（task_id、projection_id、session_id）

    參數：
        metadata (dict[str, Any] | None)：原始 metadata 字典。預設 None。
        msg_type (str)：訊息類型提示（如 "ack" 會推斷為協議訊息）。
        semantic_type (str)：語義類型（用於推斷 protocol_type 或 notification_kind）。
        transport_kind (str)：傳輸層類型標記。
        from_agent (str)：來源代理 ID（作為 worker_id 的回退值）。
        reply_needed (bool)：是否標記為需要回覆。預設 False。
        worker_id (str)：明確指定的 worker ID。
        task_id (str)：來源任務 ID。
        projection_id (str)：來源投影 ID。
        session_id (str)：來源工作階段 ID。

    返回值：
        dict[str, Any] — 合併後的 metadata 字典，包含所有正規化欄位。

    被誰引用：
        - envelope_fields_from_message()：從完整訊息中提取信封欄位
        - opc/layer2_organization/comms.py：發送訊息前正規化
    """
    merged = dict(metadata or {})

    # ── 推斷 protocol_type ──
    # 優先使用 metadata 中明確指定的值
    protocol_type = _clean_choice(merged.get("protocol_type"), _PROTOCOL_TYPES)
    # 其次從 semantic_type 映射推斷
    if not protocol_type:
        protocol_type = _SEMANTIC_PROTOCOL_MAP.get(_clean_text(semantic_type).lower(), "")
    # 最後：msg_type 為 "ack" 時推斷為確認協議
    if not protocol_type and _clean_text(msg_type).lower() == "ack":
        protocol_type = "ack"

    # ── 推斷 notification_kind ──
    # 優先使用 metadata 中明確指定的值
    notification_kind = _clean_choice(merged.get("notification_kind"), _NOTIFICATION_KINDS)
    # 其次從 semantic_type 映射推斷
    if not notification_kind:
        notification_kind = _SEMANTIC_NOTIFICATION_MAP.get(_clean_text(semantic_type).lower(), "")
    # 從 resident_status 推斷（idle/blocked）
    resident_status = _clean_text(merged.get("resident_status")).lower()
    if not notification_kind and resident_status in {"idle", "blocked"}:
        notification_kind = resident_status
    # 從 status 欄位推斷錯誤通知
    if not notification_kind and _clean_text(merged.get("status")).lower() in {"failed", "error"}:
        notification_kind = "error"

    # ── 推斷 message_class ──
    message_class = _clean_choice(merged.get("message_class"), MESSAGE_CLASSES)
    if not message_class:
        if protocol_type:
            message_class = "protocol"  # 有協議類型 → protocol 類
        elif notification_kind:
            message_class = "notification"  # 有通知種類 → notification 類
        else:
            message_class = "chat"  # 預設為一般對話

    # ── 推斷 actionable（是否需要接收方行動）──
    actionable = _coerce_bool(merged.get("actionable"))
    if actionable is None:
        # 預設：notification 類不需要行動，其他需要
        actionable = message_class != "notification"

    # ── 解析 worker_id（多來源回退鏈）──
    resolved_worker_id = (
        _clean_text(merged.get("worker_id"))          # metadata 中的 worker_id
        or _clean_text(worker_id)                      # 參數傳入的 worker_id
        or _clean_text(merged.get("member_session_id"))  # 成員工作階段 ID
        or _clean_text(merged.get("runtime_session_id"))  # 運行時工作階段 ID
        or _clean_text(from_agent)                     # 來源代理 ID
    )

    # ── 解析來源追蹤欄位 ──
    origin_task_id = _clean_text(merged.get("origin_task_id")) or _clean_text(task_id)
    origin_projection_id = _clean_text(merged.get("origin_projection_id")) or _clean_text(projection_id)
    origin_session_id = _clean_text(merged.get("origin_session_id")) or _clean_text(session_id)

    # ── 寫入正規化結果 ──
    merged.update(
        {
            "message_class": message_class,
            "protocol_type": protocol_type or None,       # 空字串轉為 None（JSON 友好）
            "notification_kind": notification_kind or None,
            "actionable": bool(actionable),
            "worker_id": resolved_worker_id,
            "origin_task_id": origin_task_id or None,
            "origin_projection_id": origin_projection_id or None,
            "origin_session_id": origin_session_id or None,
        }
    )
    # 特殊規則：notification 類但標記需要回覆時，強制設為 actionable
    if reply_needed and message_class == "notification":
        merged["actionable"] = True
    # 保留傳輸層和語義類型標記（若提供）
    if _clean_text(transport_kind):
        merged.setdefault("transport_kind", _clean_text(transport_kind).lower())
    if _clean_text(semantic_type):
        merged.setdefault("semantic_type", _clean_text(semantic_type).lower())
    return merged


def envelope_fields_from_message(message: dict[str, Any]) -> dict[str, Any]:
    """從完整的 worker 訊息中提取正規化的信封欄位。

    功能：
        接收一個完整的 worker 訊息字典（包含 metadata、msg_type、
        from_agent 等頂層欄位），調用 normalize_worker_envelope_metadata
        產生標準化的信封欄位集合。

    參數：
        message (dict[str, Any])：完整的 worker 訊息字典，可能包含：
            metadata、msg_type、semantic_type、transport_kind、
            from_agent/from、reply_needed、worker_id、
            origin_task_id/task_id、origin_projection_id/projection_id、
            origin_session_id/session_id。

    返回值：
        dict[str, Any] — 包含以下欄位的信封字典：
            message_class、protocol_type、notification_kind、actionable、
            worker_id、origin_task_id、origin_projection_id、
            origin_session_id、metadata（完整正規化後的 metadata）。

    被誰引用：
        - classify_worker_message()：產生完整的分類訊息
        - worker_message_is_actionable()：判斷訊息是否需要行動
    """
    metadata = normalize_worker_envelope_metadata(
        dict(message.get("metadata", {}) or {}),
        msg_type=_clean_text(message.get("msg_type")),
        semantic_type=_clean_text(message.get("semantic_type")),
        transport_kind=_clean_text(message.get("transport_kind")),
        from_agent=_clean_text(message.get("from_agent") or message.get("from")),
        reply_needed=bool(message.get("reply_needed")),
        worker_id=_clean_text(message.get("worker_id")),
        task_id=_clean_text(message.get("origin_task_id") or message.get("task_id")),
        projection_id=_clean_text(message.get("origin_projection_id") or message.get("projection_id")),
        session_id=_clean_text(message.get("origin_session_id") or message.get("session_id")),
    )
    return {
        "message_class": metadata.get("message_class", "chat"),
        "protocol_type": metadata.get("protocol_type"),
        "notification_kind": metadata.get("notification_kind"),
        "actionable": bool(metadata.get("actionable", True)),
        "worker_id": metadata.get("worker_id"),
        "origin_task_id": metadata.get("origin_task_id"),
        "origin_projection_id": metadata.get("origin_projection_id"),
        "origin_session_id": metadata.get("origin_session_id"),
        "metadata": metadata,
    }


def classify_worker_message(message: dict[str, Any]) -> dict[str, Any]:
    """產生帶有正規化信封欄位的訊息淺拷貝。

    功能：
        將原始訊息與正規化後的信封欄位合併，返回新的字典。
        原始訊息不被修改（純函數）。

    參數：
        message (dict[str, Any])：原始 worker 訊息字典。

    返回值：
        dict[str, Any] — 新的字典，包含原始欄位加上正規化的信封欄位。
        metadata 欄位被替換為正規化後的版本。

    被誰引用：
        - opc/layer2_organization/comms.py：發送前分類訊息
        - opc/engine.py：處理 worker 回報時分類
    """
    merged = dict(message)
    envelope = envelope_fields_from_message(message)
    # 合併信封欄位（排除 metadata，因為它需要完整替換）
    merged.update({k: v for k, v in envelope.items() if k != "metadata"})
    merged["metadata"] = envelope["metadata"]
    return merged


def worker_message_is_actionable(message: dict[str, Any]) -> bool:
    """判斷 worker 訊息是否需要接收方採取行動。

    功能：
        綜合考慮 actionable 標記和 message_class：
        僅當 actionable 為 True 且訊息分類為 "chat" 或 "protocol" 時
        才視為需要行動。notification 類即使標記為 actionable 也不觸發行動。

    參數：
        message (dict[str, Any])：worker 訊息字典。

    返回值：
        bool — True 表示需要接收方採取行動（例如回覆、審批）。

    被誰引用：
        - opc/layer2_organization/communication.py：決定是否觸發接收方處理
        - opc/engine.py：決定是否喚醒等待中的 worker
    """
    envelope = envelope_fields_from_message(message)
    return bool(envelope.get("actionable", True)) and envelope.get("message_class") in {"chat", "protocol"}
