"""OPC 輕量級國際化（i18n）模組。

職責說明：
    為 OPC 系統提供統一的多語言字串翻譯機制。支援繁體中文（zh-TW，預設）
    與英文（en）兩種語系。透過環境變數 OPC_LOCALE 或執行時 set_locale()
    切換語言。所有使用者可見字串（CLI 訊息、TUI 標籤等）均透過 t() 函數取得。

關聯關係：
    - 語系字典定義於 opc/core/locales/zh_tw.py 和 opc/core/locales/en.py
    - 被 opc/cli/app.py、opc/plugins/cli_board/tui/app.py 等所有需要
      使用者可見字串的模組調用
    - 前端有獨立的 i18n 實作（opc/plugins/office_ui/frontend_src/lib/i18n.ts）

使用範例：
    from opc.core.i18n import t, set_locale

    print(t("cli.welcome"))           # 使用當前語系翻譯
    print(t("tui.msg.count", n=5))   # 帶參數的翻譯
    set_locale("en")                  # 執行時切換為英文
"""

from __future__ import annotations  # 啟用延遲型別註解評估，支援 X | Y 聯合型別語法

import os  # 標準庫：讀取環境變數 OPC_LOCALE 以決定初始語系
from typing import Any  # 標準庫：t() 函數 kwargs 的值型別註解

# 預設語系代碼。當環境變數未設定或為空時使用此值。
# 取值："zh-TW"（繁體中文）為系統預設語言。
_DEFAULT_LOCALE = "zh-TW"

# 當前活動語系代碼。
# 型別：str。初始值從環境變數 OPC_LOCALE 讀取，若未設定則使用 _DEFAULT_LOCALE。
# 可透過 set_locale() 在執行時動態切換。
_locale: str = os.environ.get("OPC_LOCALE", _DEFAULT_LOCALE).strip() or _DEFAULT_LOCALE

# 已載入的語系字典快取。鍵為語系代碼（如 "zh-TW"），值為該語系的完整翻譯字典。
# 避免重複導入模組，提升效能。
_strings: dict[str, dict[str, str]] = {}

# 已載入語系的代碼集合。用於判斷某語系是否已完成載入（避免重複 import）。
_loaded: set[str] = set()


def _load_locale(locale: str) -> dict[str, str]:
    """延遲載入指定語系的翻譯字典。

    功能：
        根據語系代碼動態導入對應的 locale 模組（zh_tw 或 en），
        將其 STRINGS 字典快取到 _strings 中。已載入的語系直接返回快取。

    參數：
        locale (str)：語系代碼，例如 "zh-TW"、"en"、"zh_TW"。
            內部會正規化為底線小寫格式進行模組匹配。

    返回值：
        dict[str, str] — 該語系的翻譯字典（鍵為點分隔的翻譯鍵，值為翻譯字串）。
        若模組導入失敗則返回空字典。

    被誰引用：
        - t()：每次翻譯時調用以取得當前語系字典
    """
    # 已載入過則直接返回快取，避免重複 import
    if locale in _loaded:
        return _strings.get(locale, {})
    # 正規化語系代碼：將連字號轉為底線並轉小寫（"zh-TW" → "zh_tw"）
    normalized = locale.replace("-", "_").lower()
    try:
        # 以 "zh" 開頭的語系載入繁體中文字典
        if normalized.startswith("zh"):
            from opc.core.locales.zh_tw import STRINGS
        else:
            # 其他所有語系回退到英文字典
            from opc.core.locales.en import STRINGS
    except ImportError:
        # 模組不存在時使用空字典（容錯處理）
        STRINGS = {}
    # 快載入結果並標記為已載入
    _strings[locale] = dict(STRINGS)
    _loaded.add(locale)
    return _strings[locale]


def get_locale() -> str:
    """取得當前活動語系代碼。

    功能：
        返回目前生效的語系代碼字串。

    參數：
        無

    返回值：
        str — 當前語系代碼，例如 "zh-TW" 或 "en"。

    被誰引用：
        - opc/cli/app.py：顯示當前語言設定
        - 測試用例：驗證語系切換是否生效
    """
    return _locale


def set_locale(locale: str) -> None:
    """在執行時動態切換活動語系。

    功能：
        將當前語系切換為指定值。傳入空字串或 None 時回退到預設語系。
        切換後所有 t() 調用將使用新語系的翻譯。

    參數：
        locale (str)：目標語系代碼，例如 "en"、"zh-TW"。
            傳入空字串或 None 會回退到 _DEFAULT_LOCALE。

    返回值：
        無

    被誰引用：
        - opc/cli/app.py：處理 --locale 命令列參數
        - 測試用例：測試不同語系的翻譯輸出
    """
    global _locale
    _locale = str(locale or _DEFAULT_LOCALE).strip() or _DEFAULT_LOCALE


def t(key: str, **kwargs: Any) -> str:
    """將點分隔的翻譯鍵轉換為當前語系的字串。

    功能：
        核心翻譯函數。根據當前語系查找翻譯字典，支援：
        1. 語系回退：當前語系找不到時回退到英文，再找不到則返回原始鍵名
        2. 參數插值：支援 str.format 風格的佔位符（如 "{title}"）

    參數：
        key (str)：點分隔的翻譯鍵，例如 "cli.welcome"、"tui.msg.triggered"。
            鍵名結構為 "模組.功能.描述"。
        **kwargs (Any)：可選的格式化參數，用於填充翻譯字串中的佔位符。
            例如 t("tui.msg.count", n=5) 會將 "{n}" 替換為 "5"。

    返回值：
        str — 翻譯後的字串。若鍵不存在則返回原始鍵名（方便開發時發現缺失翻譯）。
        若格式化失敗則返回未格式化的原始翻譯字串。

    被誰引用：
        - opc/cli/app.py：所有 CLI 輸出訊息
        - opc/plugins/cli_board/tui/app.py：TUI 介面標籤與狀態訊息
        - opc/engine.py：引擎執行過程中的使用者可見訊息
        - opc/layer2_organization/：組織層的使用者提示

    使用範例：
        t("cli.welcome")                    # → "歡迎使用 OpenOPC"
        t("tui.msg.triggered", title="任務A")  # → "已觸發 任務A。"
        t("nonexist.key")                   # → "nonexist.key"（鍵不存在時回退）
    """
    # 載入當前語系的翻譯字典
    table = _load_locale(_locale)
    text = table.get(key)
    # 第一層回退：當前語系找不到時嘗試英文
    if text is None and _locale != "en":
        en_table = _load_locale("en")
        text = en_table.get(key)
    # 第二層回退：英文也找不到時返回原始鍵名（開發者可見缺失翻譯）
    if text is None:
        return key
    # 有格式化參數時進行佔位符替換
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            # 格式化失敗時返回未格式化的字串（容錯，避免崩潰）
            return text
    return text
