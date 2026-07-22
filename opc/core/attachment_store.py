"""附件儲存層 — 基於磁碟的檔案儲存與輕量級引用機制。

職責說明：
    管理使用者上傳附件的完整生命週期：接收（base64 解碼或本地複製）→
    磁碟持久化 → 輕量級引用（AttachmentRef）在系統中流通 →
    讀取時按需載入。設計原則為「引用流通、內容延遲載入」：
    引擎管道、資料庫、WebSocket 訊息中僅傳遞 AttachmentRef（無二進位內容），
    Base64 編碼僅在兩個端點執行：攝入時（解碼→磁碟）和 LLM 調用時（磁碟→編碼）。

關聯關係：
    - 被 opc/engine.py 在處理使用者訊息附件時調用
    - 被 opc/core/attachment_content.py 提供內容提取功能
    - 被 opc/plugins/office_ui/ 的 API 端點調用以提供檔案下載
    - 儲存路徑：{opc_home}/projects/{project_id}/attachments/{id}/

使用範例：
    store = AttachmentStore(opc_home=Path("~/.opc"), project_id="proj1")
    ref = await store.save_from_base64("photo.png", b64_data, "image/png")
    raw = store.read_bytes(ref)  # 需要時才讀取實際內容
"""

from __future__ import annotations  # 啟用延遲型別註解評估

import base64  # 標準庫：Base64 編碼/解碼，處理前端傳來的資料 URL
import mimetypes  # 標準庫：根據副檔名推斷 MIME 類型，或反向推斷副檔名
import os  # 標準庫：路徑操作、檔案名稱處理
import shutil  # 標準庫：檔案複製（save_from_path 使用）
import uuid  # 標準庫：產生唯一的附件 ID
from dataclasses import dataclass  # 標準庫：定義輕量級資料類別 AttachmentRef
from pathlib import Path  # 標準庫：跨平台路徑操作
from typing import Any  # 標準庫：型別註解

# 允許的 MIME 類型前綴白名單。
# 以此前綴開頭的 MIME 類型均允許上傳：
#   - image/：所有圖片格式
#   - text/：所有純文字格式
#   - application/pdf：PDF 文件
#   - application/json：JSON 資料
#   - application/x-yaml、application/yaml：YAML 設定檔
ALLOWED_MIME_PREFIXES = (
    "image/",
    "text/",
    "application/pdf",
    "application/json",
    "application/x-yaml",
    "application/yaml",
)

# 允許的副檔名白名單（完整列表）。
# 即使 MIME 類型不在前綴白名單中，副檔名在此集合中仍允許上傳。
# 涵蓋：圖片、文字/程式碼、設定檔、Office 文件、影片。
ALLOWED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff",
    ".txt", ".md", ".pdf", ".csv", ".json", ".yaml", ".yml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
    ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb", ".sh",
    ".xml", ".toml", ".ini", ".cfg", ".log",
    ".docx", ".xlsx", ".pptx",
    ".mp4", ".mpeg", ".mpg", ".mov", ".webm",
}

# 單一檔案大小上限：10 MB。
# 超過此限制的檔案將被拒絕上傳（拋出 ValueError）。
MAX_FILE_SIZE = 10 * 1024 * 1024

# 單一訊息附件總大小上限：20 MB。
# 一條訊息中所有附件的合計大小不得超過此值。
MAX_TOTAL_SIZE = 20 * 1024 * 1024

# 允許的完整 MIME 類型集合（用於不在前綴白名單中的特殊類型）。
# 主要為影片格式，因其 MIME 不以允許的前綴開頭。
ALLOWED_MIME_TYPES = {
    "video/mp4",
    "video/mpeg",
    "video/quicktime",
    "video/webm",
}

# MIME 類型 → 副檔名的覆蓋映射表。
# 優先於 mimetypes.guess_extension() 的結果，確保常用類型獲得正確的副檔名。
# 例如 image/jpeg 標準庫可能返回 ".jpe"，此處覆蓋為 ".jpg"。
_MIME_EXTENSION_OVERRIDES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/json": ".json",
    "application/x-yaml": ".yaml",
    "application/yaml": ".yaml",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpeg",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


@dataclass
class AttachmentRef:
    """附件的輕量級引用物件 — 可安全地序列化到 JSON / metadata 中。

    職責說明：
        代表一個已儲存在磁碟上的附件，但不包含實際的二進位內容。
        在引擎管道、資料庫記錄、WebSocket 訊息中流通的是此引用物件，
        而非檔案內容本身。需要讀取內容時透過 AttachmentStore 的方法。

    關聯關係：
        - 由 AttachmentStore.save_from_base64/save_from_path 建立
        - 被存入 opc/database/store.py 的訊息記錄中
        - 被 opc/layer1_perception/context_assembler.py 讀取以組裝 LLM 上下文

    使用範例：
        ref = AttachmentRef(
            attachment_id="abc123",
            filename="photo.png",
            mime_type="image/png",
            size_bytes=1024,
            disk_path="projects/proj1/attachments/abc123/photo.png",
        )
        if ref.is_image:
            b64 = store.read_base64(ref)
    """

    # 附件唯一識別碼（16 位十六進位字串，由 uuid4 截斷產生）
    attachment_id: str
    # 原始檔案名稱（已清理路徑成分和危險字元）
    filename: str
    # MIME 類型字串，例如 "image/png"、"text/plain"
    mime_type: str
    # 檔案大小（位元組）
    size_bytes: int
    # 相對於 opc_home 的磁碟路徑（用於定位實際檔案）
    disk_path: str

    @property
    def is_image(self) -> bool:
        """判斷附件是否為圖片類型。

        返回值：
            bool — MIME 類型以 "image/" 開頭時返回 True。
            用於決定是否以多模態方式傳遞給 LLM。
        """
        return self.mime_type.startswith("image/")

    def to_dict(self) -> dict[str, Any]:
        """序列化為字典（用於 JSON 儲存或 WebSocket 傳輸）。

        返回值：
            dict[str, Any] — 包含所有欄位的平面字典。
        """
        return {
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "disk_path": self.disk_path,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AttachmentRef:
        """從字典反序列化為 AttachmentRef 實例。

        參數：
            d (dict[str, Any])：包含附件欄位的字典（通常從資料庫讀取）。
                缺失的可選欄位使用安全的預設值。

        返回值：
            AttachmentRef — 重建的引用物件。
        """
        return cls(
            attachment_id=d["attachment_id"],
            filename=d["filename"],
            mime_type=d.get("mime_type", "application/octet-stream"),
            size_bytes=d.get("size_bytes", 0),
            disk_path=d.get("disk_path", ""),
        )


def _sanitize_filename(name: str) -> str:
    """清理檔案名稱，移除路徑成分和危險字元。

    功能：
        防止路徑遍歷攻擊（path traversal）：
        1. 僅保留最後一個路徑成分（basename）
        2. 移除 ".."、"/"、"\\" 等危險字元
        3. 空結果回退為 "upload"

    參數：
        name (str)：原始檔案名稱（可能包含路徑或惡意字元）。

    返回值：
        str — 安全的檔案名稱。
    """
    name = os.path.basename(name)
    name = name.replace("..", "").replace("/", "").replace("\\", "")
    return name or "upload"


def _check_mime(filename: str, mime: str) -> bool:
    """檢查檔案是否通過 MIME 類型/副檔名白名單驗證。

    功能：
        依序檢查三個條件（任一通過即允許）：
        1. 副檔名在 ALLOWED_EXTENSIONS 中
        2. MIME 類型在 ALLOWED_MIME_TYPES 中
        3. MIME 類型以 ALLOWED_MIME_PREFIXES 中的前綴開頭

    參數：
        filename (str)：檔案名稱（用於提取副檔名）。
        mime (str)：MIME 類型字串。

    返回值：
        bool — True 表示允許上傳。
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext in ALLOWED_EXTENSIONS:
        return True
    if mime in ALLOWED_MIME_TYPES:
        return True
    for prefix in ALLOWED_MIME_PREFIXES:
        if mime.startswith(prefix):
            return True
    return False


def _normalize_mime(mime_type: str | None) -> str:
    """正規化 MIME 類型字串（轉小寫、去空白）。

    參數：
        mime_type (str | None)：原始 MIME 字串。

    返回值：
        str — 正規化後的 MIME 字串。None 返回空字串。
    """
    return str(mime_type or "").strip().lower()


def _split_data_url(payload: str) -> tuple[str, str | None]:
    """解析 Data URL 格式，分離編碼內容和 MIME 類型。

    功能：
        處理前端傳來的 "data:image/png;base64,xxxxx" 格式：
        - 若為 Data URL：返回 (base64 內容, MIME 類型)
        - 若非 Data URL：返回 (原始字串, None)

    參數：
        payload (str)：可能是 Data URL 或純 base64 字串。

    返回值：
        tuple[str, str | None] — (base64 編碼內容, 推斷的 MIME 類型或 None)。
    """
    raw = str(payload or "").strip()
    if not raw.startswith("data:"):
        return raw, None
    # 分割標頭和內容（以第一個逗號為界）
    header, sep, encoded = raw.partition(",")
    if not sep:
        return raw, None
    # 從標頭提取 MIME（去除 ";base64" 等參數）
    mime = header[5:].split(";", 1)[0].strip().lower()
    return encoded, mime or None


def _extension_for_mime(mime_type: str) -> str:
    """根據 MIME 類型取得對應的副檔名。

    功能：
        優先使用 _MIME_EXTENSION_OVERRIDES 中的覆蓋值，
        其次使用標準庫 mimetypes.guess_extension() 的結果。

    參數：
        mime_type (str)：MIME 類型字串。

    返回值：
        str — 副檔名（含點號，如 ".png"）。無法推斷時返回空字串。
    """
    if not mime_type:
        return ""
    override = _MIME_EXTENSION_OVERRIDES.get(mime_type)
    if override:
        return override
    guessed = mimetypes.guess_extension(mime_type, strict=False) or ""
    return guessed.lower()


def _ensure_filename_extension(filename: str, mime_type: str) -> str:
    """確保檔案名稱有副檔名，若缺失則根據 MIME 類型補充。

    功能：
        檢查檔案名稱是否已有副檔名，若無則根據 MIME 類型推斷並附加。

    參數：
        filename (str)：檔案名稱。
        mime_type (str)：MIME 類型（用於推斷副檔名）。

    返回值：
        str — 帶有副檔名的檔案名稱。
    """
    if os.path.splitext(filename)[1]:
        return filename
    extension = _extension_for_mime(mime_type)
    if extension:
        return f"{filename}{extension}"
    return filename


class AttachmentStore:
    """附件儲存管理器 — 管理附件在磁碟上的完整生命週期。

    職責說明：
        提供附件的儲存（從 base64 或本地路徑）、讀取（bytes 或 base64）、
        路徑解析（絕對路徑和 HTTP 路徑）等功能。每個附件儲存在
        獨立的子目錄中：{opc_home}/projects/{project_id}/attachments/{id}/。

    關聯關係：
        - 由 opc/engine.py 在初始化時建立（綁定到特定專案）
        - 被 opc/plugins/office_ui/ 的 API 路由調用以提供檔案服務
        - 使用 AttachmentRef 作為輕量級引用在系統中流通

    使用範例：
        store = AttachmentStore(Path("~/.opc"), "my-project")
        ref = await store.save_from_base64("doc.pdf", b64_data)
        url = store.resolve_http_path(ref)  # → "/api/attachments/abc123/doc.pdf"
    """

    def __init__(self, opc_home: Path, project_id: str) -> None:
        """初始化附件儲存器。

        參數：
            opc_home (Path)：OPC 主目錄路徑（所有專案資料的根目錄）。
            project_id (str)：當前專案 ID，決定附件的儲存子目錄。
        """
        self.opc_home = opc_home
        self.project_id = project_id
        # 附件儲存的基礎目錄
        self.base_dir = opc_home / "projects" / project_id / "attachments"

    def _ensure_dir(self, attachment_id: str) -> Path:
        """確保附件的儲存目錄存在（不存在則建立）。

        參數：
            attachment_id (str)：附件 ID，作為子目錄名稱。

        返回值：
            Path — 附件目錄的絕對路徑。
        """
        d = self.base_dir / attachment_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def save_from_base64(
        self,
        filename: str,
        b64_data: str,
        mime_type: str | None = None,
    ) -> AttachmentRef:
        """從 Base64 編碼資料儲存附件到磁碟。

        功能：
            完整的攝入流程：
            1. 清理檔案名稱（防路徑遍歷）
            2. 解析 Data URL（若適用）
            3. 推斷/驗證 MIME 類型
            4. Base64 解碼
            5. 大小和白名單驗證
            6. 寫入磁碟並返回輕量級引用

        參數：
            filename (str)：原始檔案名稱。
            b64_data (str)：Base64 編碼的檔案內容（可為 Data URL 格式）。
            mime_type (str | None)：明確指定的 MIME 類型。
                若為 None 則從 Data URL 或副檔名推斷。

        返回值：
            AttachmentRef — 已儲存附件的輕量級引用。

        異常：
            ValueError：Base64 解碼失敗、檔案過大、或類型不允許時拋出。

        被誰引用：
            - opc/engine.py：處理 Office UI 上傳的附件
            - opc/plugins/office_ui/ API：WebSocket 附件上傳端點
        """
        filename = _sanitize_filename(filename)
        # 解析可能的 Data URL 格式
        b64_payload, inferred_mime = _split_data_url(b64_data)
        # MIME 類型優先順序：明確指定 > Data URL 推斷 > 副檔名推斷
        mime = _normalize_mime(mime_type) or inferred_mime or ""
        if not mime:
            guessed_mime, _ = mimetypes.guess_type(filename)
            mime = guessed_mime or "application/octet-stream"
        # 確保檔名有正確的副檔名
        filename = _ensure_filename_extension(filename, mime)
        # 若仍為通用二進位類型，嘗試從副檔名重新推斷
        if mime == "application/octet-stream":
            guessed_mime, _ = mimetypes.guess_type(filename)
            mime = guessed_mime or mime
        # Base64 解碼
        try:
            raw = base64.b64decode(b64_payload)
        except Exception as exc:
            raise ValueError(f"Invalid base64 data for {filename}: {exc}") from exc
        # 大小驗證
        size = len(raw)
        if size > MAX_FILE_SIZE:
            raise ValueError(f"File too large: {size} bytes (limit {MAX_FILE_SIZE})")

        # 白名單驗證
        if not _check_mime(filename, mime):
            raise ValueError(f"Unsupported file type: {filename} ({mime})")

        # 產生唯一 ID 並寫入磁碟
        aid = uuid.uuid4().hex[:16]
        dest_dir = self._ensure_dir(aid)
        dest = dest_dir / filename
        dest.write_bytes(raw)

        # 計算相對路徑並返回引用
        rel = dest.relative_to(self.opc_home)
        return AttachmentRef(
            attachment_id=aid,
            filename=filename,
            mime_type=mime,
            size_bytes=size,
            disk_path=str(rel),
        )

    async def save_from_path(self, file_path: Path) -> AttachmentRef:
        """從本地檔案路徑複製到附件儲存（CLI 使用）。

        功能：
            將本地磁碟上的檔案複製到附件儲存目錄。
            執行大小驗證和類型白名單檢查。

        參數：
            file_path (Path)：本地檔案的路徑（支援 ~ 展開）。

        返回值：
            AttachmentRef — 已儲存附件的輕量級引用。

        異常：
            FileNotFoundError：檔案不存在時拋出。
            ValueError：檔案過大或類型不允許時拋出。

        被誰引用：
            - opc/cli/app.py：CLI 的 --attach 參數處理
        """
        file_path = file_path.expanduser().resolve()
        if not file_path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        # 大小驗證
        size = file_path.stat().st_size
        if size > MAX_FILE_SIZE:
            raise ValueError(f"File too large: {size} bytes (limit {MAX_FILE_SIZE})")

        # 清理檔名並推斷 MIME
        filename = _sanitize_filename(file_path.name)
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "application/octet-stream"
        if not _check_mime(filename, mime):
            raise ValueError(f"Unsupported file type: {filename} ({mime})")

        # 複製檔案到儲存目錄
        aid = uuid.uuid4().hex[:16]
        dest_dir = self._ensure_dir(aid)
        dest = dest_dir / filename
        shutil.copy2(str(file_path), str(dest))  # copy2 保留時間戳記

        rel = dest.relative_to(self.opc_home)
        return AttachmentRef(
            attachment_id=aid,
            filename=filename,
            mime_type=mime,
            size_bytes=size,
            disk_path=str(rel),
        )

    def resolve_abs_path(self, ref: AttachmentRef) -> Path:
        """將附件引用解析為絕對磁碟路徑（含路徑遍歷防護）。

        功能：
            將 AttachmentRef 中的相對路徑解析為絕對路徑，
            並驗證結果路徑確實在附件目錄內（防止路徑遍歷攻擊）。

        參數：
            ref (AttachmentRef)：附件引用物件。

        返回值：
            Path — 附件檔案的絕對路徑。

        異常：
            ValueError：偵測到路徑遍歷（路徑超出附件目錄範圍）時拋出。
        """
        resolved = (self.opc_home / ref.disk_path).resolve()
        # 安全檢查：確保解析後的路徑在附件目錄內
        if not str(resolved).startswith(str(self.base_dir.resolve())):
            raise ValueError(f"Path traversal detected: {ref.disk_path}")
        return resolved

    def read_bytes(self, ref: AttachmentRef) -> bytes:
        """從磁碟讀取附件的原始位元組內容。

        參數：
            ref (AttachmentRef)：附件引用物件。

        返回值：
            bytes — 檔案的原始二進位內容。

        被誰引用：
            - read_base64()：Base64 編碼前讀取
            - opc/core/attachment_content.py：提取文字內容
        """
        return self.resolve_abs_path(ref).read_bytes()

    def read_base64(self, ref: AttachmentRef) -> str:
        """讀取附件並返回 Base64 編碼字串（僅在 LLM 調用時使用）。

        功能：
            延遲編碼策略：僅在實際需要將附件傳遞給 LLM 時
            才從磁碟讀取並編碼為 Base64。

        參數：
            ref (AttachmentRef)：附件引用物件。

        返回值：
            str — ASCII 編碼的 Base64 字串。

        被誰引用：
            - opc/layer1_perception/context_assembler.py：組裝多模態 LLM 請求
        """
        return base64.b64encode(self.read_bytes(ref)).decode("ascii")

    def resolve_http_path(self, ref: AttachmentRef) -> str:
        """產生附件的 HTTP 存取路徑（供前端顯示）。

        功能：
            返回前端可用於下載/顯示附件的 API 路徑。
            Office UI 的靜態檔案伺服器會處理此路徑的實際檔案讀取。

        參數：
            ref (AttachmentRef)：附件引用物件。

        返回值：
            str — HTTP 路徑，格式為 "/api/attachments/{id}/{filename}"。

        被誰引用：
            - opc/plugins/office_ui/：前端顯示附件圖片/下載連結
        """
        return f"/api/attachments/{ref.attachment_id}/{ref.filename}"
