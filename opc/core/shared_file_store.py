"""共用文件庫儲存層 — 基於磁碟的共享檔案管理。

職責說明：
    管理公司共用文件庫的完整生命週期：上傳（base64 解碼或本地複製）→
    磁碟持久化 → SQLite 索引建立 → 讀取/搜尋/刪除。
    所有 AI 角色共享同一個文件空間，透過資料夾和標籤進行分類管理。

關聯關係：
    - 被 opc/layer4_tools/shared_files.py 的 Agent 工具調用
    - 被 opc/plugins/office_ui/services/file_library.py 的 UI 服務調用
    - 使用 opc/database/_store_shared_files.py 進行索引持久化
    - 儲存路徑：{opc_home}/shared_files/{folder}/{file_id}_{filename}

使用範例：
    store = SharedFileStore(opc_home=Path("~/.opc"))
    record = await store.upload_from_base64("report.pdf", b64_data, folder="reports", uploaded_by="ceo")
    content = store.read_bytes(record.file_id)
"""

from __future__ import annotations

import base64
import mimetypes
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from opc.core.models import SharedFileRecord

# 單一檔案大小上限：50 MB（共用文件庫比附件儲存寬鬆）
MAX_SHARED_FILE_SIZE = 50 * 1024 * 1024

# 允許的副檔名白名單
ALLOWED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff",
    ".txt", ".md", ".pdf", ".csv", ".json", ".yaml", ".yml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
    ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb", ".sh",
    ".xml", ".toml", ".ini", ".cfg", ".log",
    ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt",
    ".mp4", ".mpeg", ".mpg", ".mov", ".webm",
    ".zip", ".tar", ".gz", ".7z",
}

# 文字類型的 MIME 前綴（讀取時可直接回傳內容）
TEXT_MIME_PREFIXES = ("text/", "application/json", "application/x-yaml", "application/yaml", "application/xml")


class SharedFileStore:
    """公司共用文件庫的磁碟儲存管理器。

    負責實際檔案的磁碟讀寫，與 SQLite 索引（OPCStore）配合使用。
    """

    def __init__(self, opc_home: Path) -> None:
        self._root = Path(opc_home) / "shared_files"
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root_path(self) -> Path:
        """共用文件庫的根目錄路徑。"""
        return self._root

    def _file_path(self, record: SharedFileRecord) -> Path:
        """根據記錄計算檔案的實際磁碟路徑。"""
        folder_dir = self._root / record.folder if record.folder else self._root
        return folder_dir / f"{record.file_id}_{record.filename}"

    def _sanitize_folder(self, folder: str) -> str:
        """清理資料夾路徑，防止路徑遍歷攻擊。"""
        folder = str(folder or "").strip().strip("/\\")
        if not folder:
            return ""
        # 移除危險路徑組件
        parts = [p for p in folder.replace("\\", "/").split("/") if p and p != ".." and p != "."]
        return "/".join(parts)

    def _guess_mime_type(self, filename: str) -> str:
        """根據檔名推斷 MIME 類型。"""
        mime, _ = mimetypes.guess_type(filename)
        return mime or "application/octet-stream"

    def _validate_extension(self, filename: str) -> None:
        """驗證副檔名是否在白名單中。"""
        ext = Path(filename).suffix.lower()
        if ext and ext not in ALLOWED_EXTENSIONS:
            raise ValueError(f"不允許的檔案類型：{ext}。允許的類型：{', '.join(sorted(ALLOWED_EXTENSIONS))}")

    def _validate_size(self, size: int) -> None:
        """驗證檔案大小是否在限制內。"""
        if size > MAX_SHARED_FILE_SIZE:
            raise ValueError(
                f"檔案大小 {size / 1024 / 1024:.1f}MB 超過限制 {MAX_SHARED_FILE_SIZE / 1024 / 1024:.0f}MB"
            )

    # ------------------------------------------------------------------
    # 上傳
    # ------------------------------------------------------------------

    async def upload_from_base64(
        self,
        filename: str,
        b64_data: str,
        *,
        folder: str = "",
        tags: list[str] | None = None,
        description: str = "",
        uploaded_by: str = "",
    ) -> SharedFileRecord:
        """從 Base64 編碼資料上傳檔案到共用文件庫。"""
        self._validate_extension(filename)
        raw = base64.b64decode(b64_data)
        self._validate_size(len(raw))

        record = SharedFileRecord(
            file_id=str(uuid.uuid4()),
            filename=filename,
            folder=self._sanitize_folder(folder),
            mime_type=self._guess_mime_type(filename),
            size_bytes=len(raw),
            tags=tags or [],
            description=description,
            uploaded_by=uploaded_by,
        )

        # 寫入磁碟
        target = self._file_path(record)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        logger.info(f"共用文件庫：上傳 {filename} → {target} ({len(raw)} bytes)")
        return record

    async def upload_from_path(
        self,
        source_path: str | Path,
        *,
        filename: str | None = None,
        folder: str = "",
        tags: list[str] | None = None,
        description: str = "",
        uploaded_by: str = "",
    ) -> SharedFileRecord:
        """從本地路徑複製檔案到共用文件庫。"""
        src = Path(source_path)
        if not src.exists():
            raise FileNotFoundError(f"來源檔案不存在：{src}")

        actual_filename = filename or src.name
        self._validate_extension(actual_filename)
        size = src.stat().st_size
        self._validate_size(size)

        record = SharedFileRecord(
            file_id=str(uuid.uuid4()),
            filename=actual_filename,
            folder=self._sanitize_folder(folder),
            mime_type=self._guess_mime_type(actual_filename),
            size_bytes=size,
            tags=tags or [],
            description=description,
            uploaded_by=uploaded_by,
        )

        target = self._file_path(record)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(target))
        logger.info(f"共用文件庫：複製 {src} → {target} ({size} bytes)")
        return record

    # ------------------------------------------------------------------
    # 讀取
    # ------------------------------------------------------------------

    def read_bytes(self, record: SharedFileRecord) -> bytes:
        """讀取檔案的原始位元組內容。"""
        path = self._file_path(record)
        if not path.exists():
            raise FileNotFoundError(f"檔案不存在於磁碟：{path}")
        return path.read_bytes()

    def read_text(self, record: SharedFileRecord, *, encoding: str = "utf-8") -> str:
        """讀取文字檔案內容。"""
        path = self._file_path(record)
        if not path.exists():
            raise FileNotFoundError(f"檔案不存在於磁碟：{path}")
        return path.read_text(encoding=encoding, errors="replace")

    def read_as_base64(self, record: SharedFileRecord) -> str:
        """讀取檔案並回傳 Base64 編碼字串。"""
        return base64.b64encode(self.read_bytes(record)).decode("ascii")

    def is_text_file(self, record: SharedFileRecord) -> bool:
        """判斷檔案是否為文字類型。"""
        mime = record.mime_type or ""
        return any(mime.startswith(prefix) for prefix in TEXT_MIME_PREFIXES)

    def file_exists_on_disk(self, record: SharedFileRecord) -> bool:
        """檢查檔案是否存在於磁碟。"""
        return self._file_path(record).exists()

    # ------------------------------------------------------------------
    # 刪除
    # ------------------------------------------------------------------

    def delete_from_disk(self, record: SharedFileRecord) -> bool:
        """從磁碟刪除檔案，回傳是否成功。"""
        path = self._file_path(record)
        if path.exists():
            path.unlink()
            logger.info(f"共用文件庫：刪除 {path}")
            return True
        return False

    # ------------------------------------------------------------------
    # 資料夾操作
    # ------------------------------------------------------------------

    def list_folders_on_disk(self) -> list[str]:
        """列出磁碟上所有資料夾（相對路徑）。"""
        folders: list[str] = []
        for item in sorted(self._root.rglob("*")):
            if item.is_dir():
                rel = item.relative_to(self._root)
                folders.append(str(rel).replace("\\", "/"))
        return folders

    def create_folder(self, folder: str) -> Path:
        """建立資料夾。"""
        sanitized = self._sanitize_folder(folder)
        if not sanitized:
            return self._root
        target = self._root / sanitized
        target.mkdir(parents=True, exist_ok=True)
        return target

    def get_folder_stats(self, folder: str = "") -> dict[str, Any]:
        """取得資料夾統計資訊。"""
        target = self._root / folder if folder else self._root
        if not target.exists():
            return {"folder": folder, "file_count": 0, "total_size": 0}
        files = [f for f in target.iterdir() if f.is_file()]
        return {
            "folder": folder,
            "file_count": len(files),
            "total_size": sum(f.stat().st_size for f in files),
        }
