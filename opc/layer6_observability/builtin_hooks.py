"""內建鉤子實作 — 對應 ECC 核心 hooks 的 OpenOPC 實現。

職責說明：
    提供 HookEngine 的內建鉤子處理函數，涵蓋 session 生命週期、
    安全檢查、編輯後驗證、成本預警等核心自動化行為。

關聯關係：
    - 被 opc/layer6_observability/hook_engine.py 的 HookEngine 註冊呼叫
    - 依賴 opc/layer5_memory/ 的記憶和本能模組
    - 依賴 opc/layer6_observability/budget_guard.py 的成本追蹤

使用範例：
    from opc.layer6_observability.builtin_hooks import register_all_builtin_hooks
    register_all_builtin_hooks(hook_engine, opc_home)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

from opc.core.models import OPCEvent
from opc.layer6_observability.hook_engine import (
    HookDefinition,
    HookEngine,
    HookResult,
    ACTION_BUILTIN,
    PROFILE_STANDARD,
    PROFILE_STRICT,
)


async def _hook_session_start_context(event: OPCEvent, data: dict[str, Any]) -> HookResult:
    """session.started — 載入專案記憶 + 本能注入。

    在 session 開始時自動載入相關記憶上下文，
    並注入高信心本能作為工作指引。
    """
    hook_id = "session-start-context"
    try:
        opc_home = Path(data.get("opc_home", ""))
        if not opc_home.exists():
            return HookResult(hook_id=hook_id, success=True, output="No opc_home, skipped")

        # Load instincts summary
        instincts_path = opc_home / "memory" / "instincts.json"
        context_parts: list[str] = []

        if instincts_path.exists():
            import json
            try:
                instincts_data = json.loads(instincts_path.read_text(encoding="utf-8"))
                high_confidence = [
                    inst for inst in instincts_data.values()
                    if isinstance(inst, dict) and inst.get("confidence", 0) >= 0.7
                ]
                if high_confidence:
                    context_parts.append(
                        f"[Instincts] {len(high_confidence)} high-confidence patterns available"
                    )
            except (json.JSONDecodeError, OSError):
                pass

        # Load vault summary
        vault_dir = opc_home / "memory" / "vault"
        if vault_dir.is_dir():
            vault_count = len(list(vault_dir.glob("*.md")))
            if vault_count:
                context_parts.append(f"[MemoryVault] {vault_count} portable memories available")

        output = "; ".join(context_parts) if context_parts else "Session context loaded"
        return HookResult(hook_id=hook_id, success=True, output=output)
    except Exception as exc:
        return HookResult(hook_id=hook_id, success=False, error=str(exc))


async def _hook_session_end_summary(event: OPCEvent, data: dict[str, Any]) -> HookResult:
    """session.completed — 觸發本能提取。

    在 session 結束時觸發 InstinctEngine 提取模式。
    """
    hook_id = "session-end-summary"
    try:
        session_id = str(data.get("session_id", ""))
        messages = data.get("messages", [])
        if not session_id or not messages:
            return HookResult(hook_id=hook_id, success=True, output="No session data, skipped")

        opc_home = Path(data.get("opc_home", ""))
        if not opc_home.exists():
            return HookResult(hook_id=hook_id, success=True, output="No opc_home, skipped")

        from opc.layer5_memory.instinct_engine import InstinctEngine
        engine = InstinctEngine(opc_home)
        result = await engine.extract_from_session(session_id, messages)
        output = (
            f"Extracted {len(result.new_instincts)} new, "
            f"reinforced {len(result.reinforced_ids)} existing"
        )
        return HookResult(hook_id=hook_id, success=True, output=output)
    except Exception as exc:
        return HookResult(hook_id=hook_id, success=False, error=str(exc))


async def _hook_pre_shell_safety(event: OPCEvent, data: dict[str, Any]) -> HookResult:
    """tool.shell.requested — 額外安全檢查。

    補充現有 approval.py 的 shell 安全規則，
    偵測潛在危險命令模式。
    """
    hook_id = "pre-shell-safety"
    command = str(data.get("command", ""))
    if not command:
        return HookResult(hook_id=hook_id, success=True, output="No command")

    # Dangerous patterns
    dangerous_patterns = [
        (r"\brm\s+-rf\s+/", "Recursive force delete from root"),
        (r"\bmkfs\b", "Filesystem format command"),
        (r"\bdd\s+.*of=/dev/", "Direct device write"),
        (r"\bchmod\s+777\s+/", "World-writable root permission"),
        (r"\bcurl\b.*\|\s*(ba)?sh", "Pipe remote content to shell"),
        (r"\bwget\b.*\|\s*(ba)?sh", "Pipe remote content to shell"),
    ]

    warnings: list[str] = []
    for pattern, description in dangerous_patterns:
        if re.search(pattern, command):
            warnings.append(description)

    if warnings:
        return HookResult(
            hook_id=hook_id, success=False,
            error=f"Security warning: {'; '.join(warnings)}",
            output=f"BLOCKED: {command[:100]}",
        )
    return HookResult(hook_id=hook_id, success=True, output="Shell command safe")


async def _hook_post_edit_typecheck(event: OPCEvent, data: dict[str, Any]) -> HookResult:
    """tool.file.edited — 對 .py 文件執行語法檢查。

    在 Python 文件被編輯後自動執行基本語法驗證。
    """
    hook_id = "post-edit-typecheck"
    file_path = str(data.get("file_path", ""))
    if not file_path or not file_path.endswith(".py"):
        return HookResult(hook_id=hook_id, success=True, output="Not a Python file, skipped")

    path = Path(file_path)
    if not path.exists():
        return HookResult(hook_id=hook_id, success=True, output="File not found, skipped")

    try:
        import py_compile
        py_compile.compile(str(path), doraise=True)
        return HookResult(hook_id=hook_id, success=True, output=f"Syntax OK: {path.name}")
    except py_compile.PyCompileError as exc:
        return HookResult(
            hook_id=hook_id, success=False,
            error=f"Syntax error in {path.name}: {exc}",
        )
    except Exception as exc:
        return HookResult(hook_id=hook_id, success=False, error=str(exc))


async def _hook_budget_warning(event: OPCEvent, data: dict[str, Any]) -> HookResult:
    """budget.threshold — 成本預警通知。

    當預算達到閾值時發出警告。
    """
    hook_id = "budget-warning"
    usage_pct = float(data.get("usage_percent", 0))
    budget_limit = data.get("budget_limit", "unknown")
    current_cost = data.get("current_cost", "unknown")

    if usage_pct >= 90:
        level = "CRITICAL"
    elif usage_pct >= 75:
        level = "WARNING"
    else:
        level = "INFO"

    output = f"[{level}] Budget usage: {usage_pct:.1f}% (cost: {current_cost}, limit: {budget_limit})"
    return HookResult(hook_id=hook_id, success=True, output=output)


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------

# 內建鉤子定義清單
BUILTIN_HOOK_DEFINITIONS: list[dict[str, Any]] = [
    {
        "id": "session-start-context",
        "event": "session.started",
        "action": "builtin",
        "profile": "standard",
    },
    {
        "id": "session-end-summary",
        "event": "session.completed",
        "action": "builtin",
        "profile": "standard",
    },
    {
        "id": "pre-shell-safety",
        "event": "tool.shell.requested",
        "action": "builtin",
        "profile": "minimal",
    },
    {
        "id": "post-edit-typecheck",
        "event": "tool.file.edited",
        "matcher": "file_path matches '\\.py$'",
        "action": "builtin",
        "profile": "strict",
    },
    {
        "id": "budget-warning",
        "event": "budget.threshold",
        "action": "builtin",
        "profile": "minimal",
    },
]


def register_all_builtin_hooks(engine: HookEngine, opc_home: Path | None = None) -> int:
    """將所有內建鉤子註冊到 HookEngine。

    Args:
        engine: HookEngine 實例。
        opc_home: OPC 主目錄（注入到事件 data 中）。

    Returns:
        int — 註冊的鉤子數量。
    """
    # Register handlers
    engine.register_builtin("session-start-context", _hook_session_start_context)
    engine.register_builtin("session-end-summary", _hook_session_end_summary)
    engine.register_builtin("pre-shell-safety", _hook_pre_shell_safety)
    engine.register_builtin("post-edit-typecheck", _hook_post_edit_typecheck)
    engine.register_builtin("budget-warning", _hook_budget_warning)

    # Register hook definitions
    for hook_def in BUILTIN_HOOK_DEFINITIONS:
        engine.add_hook(HookDefinition(
            hook_id=hook_def["id"],
            event_type=hook_def["event"],
            matcher=hook_def.get("matcher", ""),
            action=hook_def["action"],
            profile=hook_def["profile"],
            enabled=True,
        ))

    logger.info(f"Registered {len(BUILTIN_HOOK_DEFINITIONS)} builtin hooks")
    return len(BUILTIN_HOOK_DEFINITIONS)
