"""事件驅動的鉤子執行引擎，兼容 ECC hooks.json 格式。

職責說明：
    基於 OPC EventBus 建構觸發式自動化鉤子系統。當系統事件發生時，
    匹配已註冊的鉤子定義並執行對應動作（命令、腳本、內建函數）。

關聯關係：
    - 訂閱 opc/core/events.py 的 EventBus 事件
    - 鉤子定義儲存於 .opc/config/hooks.json
    - 內建鉤子實作於 opc/layer6_observability/builtin_hooks.py
    - 被 opc/cli/app.py 的 hooks 命令組呼叫

使用範例：
    engine = HookEngine(opc_home, event_bus)
    engine.load_hooks()
    # 鉤子會自動響應 EventBus 事件
    results = await engine.on_event(OPCEvent(event_type="session.started", data={}))
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.core.models import OPCEvent

# 鉤子動作類型
ACTION_COMMAND = "command"
ACTION_SCRIPT = "script"
ACTION_BUILTIN = "builtin"

# 鉤子設定檔等級
PROFILE_MINIMAL = "minimal"
PROFILE_STANDARD = "standard"
PROFILE_STRICT = "strict"

# 設定檔優先級（越高越嚴格，包含更多鉤子）
_PROFILE_LEVELS = {PROFILE_MINIMAL: 0, PROFILE_STANDARD: 1, PROFILE_STRICT: 2}

# 內建鉤子處理函數的型別
BuiltinHandler = Callable[[OPCEvent, dict[str, Any]], Coroutine[Any, Any, "HookResult"]]


@dataclass
class HookDefinition:
    """一個鉤子的完整定義。"""

    hook_id: str
    event_type: str
    matcher: str = ""
    action: str = ACTION_BUILTIN
    command: str = ""
    profile: str = PROFILE_STANDARD
    enabled: bool = True
    timeout_seconds: float = 30.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """單個鉤子執行的結果。"""

    hook_id: str
    success: bool = True
    output: str = ""
    error: str = ""
    duration_ms: float = 0.0
    skipped: bool = False


class HookEngine:
    """事件驅動的鉤子執行引擎。

    支援三種動作類型：
        - command: 執行 shell 命令
        - script: 執行腳本文件
        - builtin: 呼叫內建 Python 函數

    環境變量控制：
        - OPC_HOOK_PROFILE: 設定檔等級 (minimal|standard|strict)
        - OPC_DISABLED_HOOKS: 逗號分隔的停用鉤子 ID
    """

    def __init__(self, opc_home: Path, event_bus: Any | None = None) -> None:
        """初始化鉤子引擎。

        Args:
            opc_home: OPC 主目錄路徑。
            event_bus: OPC EventBus 實例（可選，用於自動訂閱）。
        """
        self.opc_home = Path(opc_home)
        self._event_bus = event_bus
        self._hooks: list[HookDefinition] = []
        self._builtin_handlers: dict[str, BuiltinHandler] = {}
        self._profile = os.environ.get("OPC_HOOK_PROFILE", PROFILE_STANDARD)
        disabled_raw = os.environ.get("OPC_DISABLED_HOOKS", "")
        self._disabled: set[str] = {h.strip() for h in disabled_raw.split(",") if h.strip()}
        self._config_path = self.opc_home / "config" / "hooks.json"
        self._subscribed = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def load_hooks(self, hooks_config: Path | None = None) -> int:
        """從配置檔案載入鉤子定義。

        Args:
            hooks_config: 配置檔案路徑。None 使用預設 .opc/config/hooks.json。

        Returns:
            int — 載入的鉤子數量。
        """
        config_path = hooks_config or self._config_path
        if not config_path.exists():
            logger.debug(f"HookEngine: no config at {config_path}")
            return 0

        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"HookEngine: failed to load config: {exc}")
            return 0

        hooks_list = data.get("hooks", [])
        if not isinstance(hooks_list, list):
            return 0

        loaded = 0
        for item in hooks_list:
            if not isinstance(item, dict):
                continue
            hook = HookDefinition(
                hook_id=str(item.get("id", f"hook-{loaded}")),
                event_type=str(item.get("event", "")),
                matcher=str(item.get("matcher", "")),
                action=str(item.get("action", ACTION_BUILTIN)),
                command=str(item.get("command", "")),
                profile=str(item.get("profile", PROFILE_STANDARD)),
                enabled=bool(item.get("enabled", True)),
                timeout_seconds=float(item.get("timeout_seconds", 30.0)),
                metadata=item.get("metadata", {}),
            )
            self._hooks.append(hook)
            loaded += 1

        logger.info(f"HookEngine: loaded {loaded} hooks from {config_path}")
        return loaded

    def register_builtin(self, hook_id: str, handler: BuiltinHandler) -> None:
        """註冊內建鉤子處理函數。

        Args:
            hook_id: 鉤子 ID（需與 HookDefinition.hook_id 對應）。
            handler: 非同步處理函數。
        """
        self._builtin_handlers[hook_id] = handler

    def add_hook(self, hook: HookDefinition) -> None:
        """動態添加一個鉤子定義。"""
        self._hooks.append(hook)

    def subscribe_to_bus(self) -> None:
        """將鉤子引擎訂閱到 EventBus（全域監聽）。"""
        if self._event_bus and not self._subscribed:
            self._event_bus.subscribe_all(self._on_bus_event)
            self._subscribed = True

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def on_event(self, event: OPCEvent) -> list[HookResult]:
        """事件觸發時執行匹配的鉤子。

        Args:
            event: OPC 系統事件。

        Returns:
            所有匹配鉤子的執行結果列表。
        """
        results: list[HookResult] = []
        matching = self._match_hooks(event)

        for hook in matching:
            result = await self._execute_hook(hook, event)
            results.append(result)

        return results

    def list_hooks(self) -> list[HookDefinition]:
        """列出所有已載入的鉤子定義。"""
        return list(self._hooks)

    def enable_hook(self, hook_id: str) -> bool:
        """啟用指定鉤子。"""
        for hook in self._hooks:
            if hook.hook_id == hook_id:
                hook.enabled = True
                self._disabled.discard(hook_id)
                return True
        return False

    def disable_hook(self, hook_id: str) -> bool:
        """停用指定鉤子。"""
        for hook in self._hooks:
            if hook.hook_id == hook_id:
                hook.enabled = False
                self._disabled.add(hook_id)
                return True
        return False

    def import_ecc_hooks(self, ecc_hooks_json: Path) -> int:
        """從 ECC hooks.json 格式導入鉤子定義。

        ECC hooks.json 格式：
        {
            "hooks": [
                {"matcher": "...", "hooks": [{"type": "command", "command": "..."}]}
            ]
        }

        轉換為 OPC 格式並追加到配置。

        Args:
            ecc_hooks_json: ECC hooks.json 檔案路徑。

        Returns:
            int — 導入的鉤子數量。
        """
        if not ecc_hooks_json.exists():
            return 0

        try:
            ecc_data = json.loads(ecc_hooks_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0

        ecc_hooks = ecc_data.get("hooks", [])
        imported = 0

        for idx, ecc_hook in enumerate(ecc_hooks):
            if not isinstance(ecc_hook, dict):
                continue
            matcher = str(ecc_hook.get("matcher", ""))
            sub_hooks = ecc_hook.get("hooks", [])
            for sub in sub_hooks:
                if not isinstance(sub, dict):
                    continue
                hook = HookDefinition(
                    hook_id=f"ecc-imported-{idx}-{imported}",
                    event_type=self._infer_event_type(matcher),
                    matcher=matcher,
                    action=ACTION_COMMAND,
                    command=str(sub.get("command", "")),
                    profile=PROFILE_STANDARD,
                    enabled=True,
                    metadata={"imported_from": "ecc", "original_matcher": matcher},
                )
                self._hooks.append(hook)
                imported += 1

        if imported:
            logger.info(f"HookEngine: imported {imported} hooks from ECC format")
        return imported

    def save_config(self) -> None:
        """將當前鉤子定義儲存到配置檔案。"""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        hooks_data = []
        for hook in self._hooks:
            hooks_data.append({
                "id": hook.hook_id,
                "event": hook.event_type,
                "matcher": hook.matcher,
                "action": hook.action,
                "command": hook.command,
                "profile": hook.profile,
                "enabled": hook.enabled,
                "timeout_seconds": hook.timeout_seconds,
            })
        payload = {"hooks": hooks_data}
        self._config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _on_bus_event(self, event: OPCEvent) -> None:
        """EventBus 全域監聽回調。"""
        await self.on_event(event)

    def _match_hooks(self, event: OPCEvent) -> list[HookDefinition]:
        """找出與事件匹配的啟用鉤子。"""
        event_type = getattr(event, "event_type", "") or ""
        data = getattr(event, "data", {}) or {}
        profile_level = _PROFILE_LEVELS.get(self._profile, 1)
        matched: list[HookDefinition] = []

        for hook in self._hooks:
            # Skip disabled
            if not hook.enabled or hook.hook_id in self._disabled:
                continue
            # Profile filter: hook's profile level must be <= current profile level
            hook_level = _PROFILE_LEVELS.get(hook.profile, 1)
            if hook_level > profile_level:
                continue
            # Event type match (supports prefix matching like "session.*")
            if hook.event_type:
                if hook.event_type.endswith(".*"):
                    prefix = hook.event_type[:-2]
                    if not event_type.startswith(prefix):
                        continue
                elif hook.event_type != event_type:
                    continue
            # Matcher expression (simple regex on data values)
            if hook.matcher and not self._evaluate_matcher(hook.matcher, data):
                continue
            matched.append(hook)

        return matched

    def _evaluate_matcher(self, matcher: str, data: dict[str, Any]) -> bool:
        """評估簡單的匹配表達式。

        支持格式：
            - "key matches 'regex'" — 對 data[key] 做正則匹配
            - "key == 'value'" — 精確匹配
            - 空字串 — 永遠匹配
        """
        if not matcher:
            return True

        # Try "key matches 'pattern'" format
        m = re.match(r"(\w+)\s+matches\s+['\"](.+?)['\"]", matcher)
        if m:
            key, pattern = m.group(1), m.group(2)
            value = str(data.get(key, ""))
            try:
                return bool(re.search(pattern, value))
            except re.error:
                return False

        # Try "key == 'value'" format
        m = re.match(r"(\w+)\s*==\s*['\"](.+?)['\"]", matcher)
        if m:
            key, expected = m.group(1), m.group(2)
            return str(data.get(key, "")) == expected

        # Fallback: treat as substring search in all data values
        all_values = " ".join(str(v) for v in data.values())
        return matcher.lower() in all_values.lower()

    async def _execute_hook(self, hook: HookDefinition, event: OPCEvent) -> HookResult:
        """執行單個鉤子。"""
        import time
        start = time.perf_counter()

        try:
            if hook.action == ACTION_BUILTIN:
                handler = self._builtin_handlers.get(hook.hook_id)
                if handler is None:
                    return HookResult(
                        hook_id=hook.hook_id, success=False,
                        error=f"No builtin handler registered for '{hook.hook_id}'",
                        skipped=True,
                    )
                data = getattr(event, "data", {}) or {}
                result = await asyncio.wait_for(
                    handler(event, data), timeout=hook.timeout_seconds
                )
                elapsed = (time.perf_counter() - start) * 1000
                return HookResult(
                    hook_id=hook.hook_id, success=result.success,
                    output=result.output, error=result.error,
                    duration_ms=elapsed,
                )
            elif hook.action in (ACTION_COMMAND, ACTION_SCRIPT):
                output = await self._run_command(hook, event)
                elapsed = (time.perf_counter() - start) * 1000
                return HookResult(
                    hook_id=hook.hook_id, success=True,
                    output=output, duration_ms=elapsed,
                )
            else:
                return HookResult(
                    hook_id=hook.hook_id, success=False,
                    error=f"Unknown action type: {hook.action}",
                )
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - start) * 1000
            return HookResult(
                hook_id=hook.hook_id, success=False,
                error=f"Hook timed out after {hook.timeout_seconds}s",
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            return HookResult(
                hook_id=hook.hook_id, success=False,
                error=str(exc), duration_ms=elapsed,
            )

    async def _run_command(self, hook: HookDefinition, event: OPCEvent) -> str:
        """執行 shell 命令或腳本。"""
        command = hook.command
        if not command:
            return ""

        # Simple variable substitution from event data
        data = getattr(event, "data", {}) or {}
        for key, value in data.items():
            command = command.replace(f"${{{key}}}", str(value))
            command = command.replace(f"${key}", str(value))

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.opc_home),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=hook.timeout_seconds
        )
        output = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            logger.warning(f"Hook '{hook.hook_id}' command failed: {err}")
        return output

    @staticmethod
    def _infer_event_type(matcher: str) -> str:
        """從 ECC matcher 表達式推斷事件類型。"""
        matcher_lower = matcher.lower()
        if "sessionstart" in matcher_lower or "session" in matcher_lower:
            return "session.started"
        if "stop" in matcher_lower or "sessionend" in matcher_lower:
            return "session.completed"
        if "bash" in matcher_lower or "shell" in matcher_lower:
            return "tool.shell.requested"
        if "edit" in matcher_lower or "file" in matcher_lower:
            return "tool.file.edited"
        if "mcp" in matcher_lower:
            return "tool.mcp.requested"
        return "unknown"
