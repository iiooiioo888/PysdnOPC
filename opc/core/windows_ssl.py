"""Windows 平台 SSL 環境修復模組。

職責說明：
    在 Windows 系統上，環境變數 SSLKEYLOGFILE 會導致 aiohttp/OpenSSL
    在導入時崩潰。本模組提供安全的移除與警告格式化功能，確保
    OPC 在 Windows 上的網路請求不會因該變數而異常中止。

關聯關係：
    - 被 opc/cli/app.py 在啟動外部代理子進程前調用
    - 被 opc/layer3_agent/external_broker.py 在建立連線前調用

使用範例：
    from opc.core.windows_ssl import sanitize_windows_sslkeylogfile
    removed = sanitize_windows_sslkeylogfile()
    if removed:
        logger.warning(format_windows_sslkeylog_warning("claude-code", removed))
"""

from __future__ import annotations  # 啟用延遲型別註解評估，支援 Python 3.10 以下的 X | Y 語法

import os  # 標準庫：用於讀取/移除環境變數及判斷作業系統類型

# 記憶體中暫存被移除的 SSLKEYLOGFILE 路徑值。
# 型別：str | None。初始為 None，移除環境變數後保存其原始值，
# 供後續警告訊息使用。僅保留第一次移除的值（避免重複警告）。
_REMOVED_SSLKEYLOGFILE: str | None = None


def sanitize_windows_sslkeylogfile() -> str | None:
    """在 Windows 上預先移除 SSLKEYLOGFILE 環境變數並記憶其值。

    功能：
        偵測目前是否為 Windows 平台，若是則從環境變數中移除
        SSLKEYLOGFILE，防止 aiohttp/OpenSSL 導入時崩潰。
        第一次移除時會將值存入模組級變數以供警告使用。

    參數：
        無

    返回值：
        str | None — 被移除的 SSLKEYLOGFILE 路徑值；
        若非 Windows 平台或變數不存在則返回 None。

    被誰引用：
        - opc/cli/app.py：啟動外部代理前調用
        - opc/layer3_agent/external_broker.py：建立子進程前調用
    """
    global _REMOVED_SSLKEYLOGFILE
    # 非 Windows 平台（os.name != "nt"）無需處理，直接返回
    if os.name != "nt":
        return None
    # 從環境變數中彈出（移除並返回值）SSLKEYLOGFILE
    removed = os.environ.pop("SSLKEYLOGFILE", None)
    # 僅在第一次移除時記憶，避免覆蓋原始值
    if removed and not _REMOVED_SSLKEYLOGFILE:
        _REMOVED_SSLKEYLOGFILE = removed
    return removed


def pop_windows_sslkeylogfile() -> str | None:
    """在 Windows 上移除 SSLKEYLOGFILE 並清除記憶狀態。

    功能：
        與 sanitize_windows_sslkeylogfile 類似，但會同時清除
        模組級的記憶變數。適用於需要完全重置狀態的場景，
        例如進程池中的 worker 重新初始化。

    參數：
        無

    返回值：
        str | None — 被移除的路徑值，或之前記憶的路徑值；
        若均不存在則返回 None。

    被誰引用：
        - opc/layer3_agent/external_broker.py：子進程結束後的清理階段
    """
    global _REMOVED_SSLKEYLOGFILE
    # 非 Windows 平台無需處理
    if os.name != "nt":
        return None
    # 嘗試從當前環境變數中移除
    removed = os.environ.pop("SSLKEYLOGFILE", None)
    if removed:
        # 成功移除當前環境變數，清除記憶並返回
        _REMOVED_SSLKEYLOGFILE = None
        return removed
    # 環境變數已不存在，返回之前記憶的值（若有）
    remembered = _REMOVED_SSLKEYLOGFILE
    _REMOVED_SSLKEYLOGFILE = None
    return remembered


def format_windows_sslkeylog_warning(command_label: str, keylog_path: str) -> str:
    """產生統一格式的 SSLKEYLOGFILE 警告訊息。

    功能：
        當 SSLKEYLOGFILE 必須被忽略時，產生一致性的警告字串，
        告知使用者該變數被忽略的原因。

    參數：
        command_label (str)：觸發警告的命令或代理名稱，
            例如 "claude-code"、"codex"。
        keylog_path (str)：被忽略的 SSLKEYLOGFILE 路徑值。

    返回值：
        str — 格式化的英文警告訊息（警告訊息保持英文以利於日誌檢索）。

    被誰引用：
        - opc/cli/app.py：顯示給使用者的警告輸出
        - opc/layer3_agent/external_broker.py：記錄到日誌系統
    """
    return (
        f"Warning: ignoring SSLKEYLOGFILE for `{command_label}` on Windows "
        f"({keylog_path}) because it can crash aiohttp/OpenSSL."
    )
