"""對話紀錄可見性規則模組（共享邏輯）。

職責說明：
    定義對話紀錄（transcript）訊息在「摘要模式」與「完整模式」下的可見性
    判斷規則。資料庫分頁器（pager）與 Office UI 渲染器必須使用相同的
    可見性邊界，否則摘要請求可能翻頁到渲染器會丟棄的行，導致游標永遠
    無法推進可見時間軸。

關聯關係：
    - 被 opc/database/store.py 的分頁查詢調用（SQL 謂詞版本）
    - 被 opc/plugins/office_ui/ 的 WebSocket handler 調用（Python 判斷版本）
    - 被 opc/presentation/kanban.py 調用（渲染過濾）

設計原則：
    本模組同時提供 Python 函數版本和等價的 SQLite SQL 謂詞版本，
    確保記憶體過濾與資料庫過濾的結果完全一致。
"""

from __future__ import annotations  # 啟用延遲型別註解評估

from typing import Any, Literal, Mapping  # 標準庫：型別註解用


# 對話紀錄詳細程度型別。
# 取值範圍："summary"（摘要模式，僅顯示使用者可見訊息）或 "full"（完整模式，顯示所有執行細節）。
TranscriptDetailLevel = Literal["summary", "full"]

# 僅在「完整模式」下可見的對話紀錄種類集合（凍結集合，不可修改）。
# 這些 kind 代表執行時的中間過程訊息，對一般使用者隱藏：
#   - runtime_v2_user_turn：運行時 v2 的使用者輸入回合
#   - runtime_v2_intermediate_assistant：中間助手回應（非最終結果）
#   - runtime_v2_company_assistant：公司模式助手內部回應
#   - runtime_v2_tool_output：工具呼叫的原始輸出
FULL_DETAIL_ONLY_TRANSCRIPT_KINDS: frozenset[str] = frozenset({
    "runtime_v2_user_turn",
    "runtime_v2_intermediate_assistant",
    "runtime_v2_company_assistant",
    "runtime_v2_tool_output",
})


def normalize_transcript_detail_level(value: Any) -> TranscriptDetailLevel:
    """將任意輸入正規化為合法的詳細程度值。

    功能：
        接受任意型別的輸入（可能來自 API 參數、設定檔等），
        正規化為 "full" 或 "summary" 兩種合法值之一。

    參數：
        value (Any)：待正規化的值。字串 "full"（不分大小寫）→ "full"，
            其他所有值（None、空字串、其他字串）→ "summary"。

    返回值：
        TranscriptDetailLevel — "full" 或 "summary"。

    被誰引用：
        - transcript_metadata_visible()：判斷前正規化輸入
        - transcript_visibility_sql()：建立 SQL 前正規化輸入
        - rendered_transcript_metadata_visible()：判斷前正規化輸入
    """
    return "full" if str(value or "").strip().lower() == "full" else "summary"


def transcript_metadata_visible(
    metadata: Mapping[str, Any] | None,
    *,
    detail_level: TranscriptDetailLevel | str = "summary",
) -> bool:
    """判斷一筆對話紀錄在指定詳細程度下是否可見（Python 版本）。

    功能：
        根據訊息的 metadata 中的 kind 欄位和 company_final_turn 標記，
        決定該訊息在摘要模式下是否對使用者可見。

    判斷邏輯：
        1. 完整模式（full）→ 所有訊息均可見
        2. company_final_turn 為 True → 可見（公司角色的最終回覆是使用者可見結果，
           即使其 kind 通常屬於完整模式專用）
        3. kind 不在 FULL_DETAIL_ONLY_TRANSCRIPT_KINDS 中 → 可見
        4. 其他 → 不可見

    參數：
        metadata (Mapping[str, Any] | None)：訊息的後設資料字典，
            包含 "kind"（訊息類型）和可選的 "company_final_turn"（布林標記）。
        detail_level (TranscriptDetailLevel | str)：請求的詳細程度。
            預設 "summary"。

    返回值：
        bool — True 表示該訊息在指定模式下可見。

    被誰引用：
        - opc/plugins/office_ui/ WebSocket handler：過濾推送給前端的訊息
        - opc/presentation/kanban.py：看板渲染時的訊息過濾
    """
    # 完整模式下所有訊息均可見
    if normalize_transcript_detail_level(detail_level) == "full":
        return True
    normalized_metadata = dict(metadata or {})
    # 公司最終回覆強制可見（覆蓋 kind 分類）
    if normalized_metadata.get("company_final_turn") is True:
        return True
    # 檢查 kind 是否屬於完整模式專用類型
    kind = str(normalized_metadata.get("kind", "") or "").strip()
    return kind not in FULL_DETAIL_ONLY_TRANSCRIPT_KINDS


def transcript_visibility_sql(
    *,
    detail_level: TranscriptDetailLevel | str,
    metadata_column: str = "metadata",
) -> tuple[str, tuple[str, ...]]:
    """建立等價於 transcript_metadata_visible 的 SQLite 查詢謂詞。

    功能：
        產生 SQL WHERE 子句片段，在資料庫層面過濾不可見的對話紀錄，
        避免將大量資料載入記憶體後再過濾（提升分頁效能）。

    參數：
        detail_level (TranscriptDetailLevel | str)：請求的詳細程度。
            "full" 時返回空謂詞（不過濾）。
        metadata_column (str)：metadata JSON 欄位的列名。
            預設 "metadata"。注意：僅供內部靜態查詢建構使用，
            呼叫者不得傳入使用者可控的識別符（防 SQL 注入）。

    返回值：
        tuple[str, tuple[str, ...]] — (SQL 謂詞字串, 參數化查詢的綁定值)。
        完整模式時返回 ("", ())。
        摘要模式時返回包含 AND 條件的 SQL 片段及對應的 kind 值元組。

    被誰引用：
        - opc/database/store.py：分頁查詢對話紀錄時嵌入 WHERE 子句
    """
    # 完整模式無需過濾
    if normalize_transcript_detail_level(detail_level) == "full":
        return "", ()
    # 建立 IN (...) 的佔位符數量
    placeholders = ",".join("?" for _ in FULL_DETAIL_ONLY_TRANSCRIPT_KINDS)
    # SQL 謂詞：company_final_turn 為 1 或 kind 不在隱藏集合中
    predicate = (
        "AND (COALESCE(json_extract("
        f"{metadata_column}, '$.company_final_turn'), 0) = 1 "
        "OR COALESCE(json_extract("
        f"{metadata_column}, '$.kind'), '') NOT IN ({placeholders})) "
    )
    # 綁定值按字母排序確保穩定性
    return predicate, tuple(sorted(FULL_DETAIL_ONLY_TRANSCRIPT_KINDS))


def rendered_transcript_metadata_visible(
    metadata: Mapping[str, Any] | None,
    *,
    detail_level: TranscriptDetailLevel | str = "summary",
) -> bool:
    """根據渲染器寫入的可見性標記判斷訊息是否可見。

    功能：
        檢查 metadata 中由對話紀錄渲染器預先計算並寫入的
        "detail_visibility" 標記，決定摘要模式下是否顯示。
        與 transcript_metadata_visible 不同，本函數依賴預計算標記
        而非即時判斷 kind。

    參數：
        metadata (Mapping[str, Any] | None)：訊息的後設資料字典，
            包含可選的 "detail_visibility" 欄位（值為 "summary" 或 "full"）。
        detail_level (TranscriptDetailLevel | str)：請求的詳細程度。預設 "summary"。

    返回值：
        bool — True 表示可見。detail_visibility 為 "full" 時在摘要模式下不可見。

    被誰引用：
        - opc/plugins/office_ui/：前端渲染時的最終過濾
    """
    # 完整模式下所有訊息均可見
    if normalize_transcript_detail_level(detail_level) == "full":
        return True
    # 讀取渲染器預計算的可見性標記
    visibility = str(dict(metadata or {}).get("detail_visibility", "summary") or "summary")
    # 標記為 "full" 表示僅完整模式可見
    return visibility.strip().lower() != "full"


def rendered_transcript_visibility_sql(
    *,
    detail_level: TranscriptDetailLevel | str,
    metadata_column: str = "metadata",
) -> str:
    """建立等價於 rendered_transcript_metadata_visible 的 SQLite 查詢謂詞。

    功能：
        產生 SQL WHERE 子句片段，根據渲染器寫入的 detail_visibility
        標記在資料庫層面過濾訊息。

    參數：
        detail_level (TranscriptDetailLevel | str)：請求的詳細程度。
            "full" 時返回空字串（不過濾）。
        metadata_column (str)：metadata JSON 欄位的列名。預設 "metadata"。

    返回值：
        str — SQL 謂詞字串。完整模式時返回空字串；
        摘要模式時返回 " AND lower(...) != 'full'" 條件。

    被誰引用：
        - opc/database/store.py：已渲染對話紀錄的分頁查詢
    """
    # 完整模式無需過濾
    if normalize_transcript_detail_level(detail_level) == "full":
        return ""
    # 摘要模式：排除 detail_visibility 標記為 "full" 的訊息
    return (
        " AND lower(COALESCE(json_extract("
        f"{metadata_column}, '$.detail_visibility'), 'summary')) != 'full'"
    )
