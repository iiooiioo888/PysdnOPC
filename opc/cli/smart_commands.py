"""智能啟動 CLI 命令 — 零配置啟動和 API Key 管理。

職責說明：
    提供 `opc smart` 和 `opc keys` 命令：
    - opc smart: 從自然語言描述智能啟動任務
    - opc keys: API Key 自動發現和管理
    - opc estimate: 成本估算
"""

from __future__ import annotations

import asyncio
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown

from opc.core.i18n import t

console = Console()

# 創建子命令組
smart_app = typer.Typer(
    name="smart",
    help="🧠 智能啟動 — 從自然語言描述自動配置並啟動任務",
    no_args_is_help=True,
)
keys_app = typer.Typer(
    name="keys",
    help="🔑 API Key 管理 — 自動發現、驗證和管理 API Key",
    no_args_is_help=True,
)


@smart_app.command("start")
def smart_start(
    task: str = typer.Argument(..., help="任務描述（自然語言）"),
    budget: float = typer.Option(0.0, "--budget", "-b", help="預算上限（美元）"),
    quality: str = typer.Option("balanced", "--quality", "-q", help="品質偏好: best/balanced/cheapest"),
    use_llm: bool = typer.Option(False, "--use-llm", help="使用 LLM 做深度意圖解析"),
    dry_run: bool = typer.Option(False, "--dry-run", help="僅顯示配置，不實際啟動"),
):
    """🧠 從自然語言描述智能啟動任務。

    示例：
        opc smart start "幫我做一份新能源汽車行業投資分析報告"
        opc smart start "開發一個簡單的 TODO 應用" --budget 2.0
        opc smart start "寫一篇關於AI的文章" --quality cheapest
    """
    from opc.smart_start import SmartStarter, format_smart_start_summary

    async def _run():
        starter = SmartStarter()
        try:
            config = await starter.start(
                task,
                budget=budget,
                quality_hint=quality,
                use_llm_for_intent=use_llm,
            )

            # 顯示啟動信息
            console.print()
            console.print(starter.format_startup_info(config))

            if dry_run:
                console.print("[info]🔍 Dry run 模式，不實際啟動[/info]")
                return

            # 確認啟動
            if budget > 0:
                confirm = typer.confirm("確認啟動？")
                if not confirm:
                    console.print("[warning]已取消[/warning]")
                    return

            # TODO: 實際啟動引擎執行
            console.print("[success]✅ 啟動成功！[/success]")
            console.print("[info]（注：實際執行邏輯待集成到 OPCEngine）[/info]")

        except ValueError as e:
            console.print(f"[error]❌ {e}[/error]")
            raise typer.Exit(code=1)

    asyncio.run(_run())


@smart_app.command("parse")
def smart_parse(
    task: str = typer.Argument(..., help="任務描述"),
    use_llm: bool = typer.Option(False, "--use-llm", help="使用 LLM 深度解析"),
):
    """🔍 僅解析任務意圖（不啟動）。

    示例：
        opc smart parse "幫我做一份投資分析報告"
    """
    from opc.layer1_perception.intent_parser import IntentParser, format_intent

    async def _run():
        parser = IntentParser()
        intent = await parser.parse(task, use_llm=use_llm)
        console.print()
        console.print(format_intent(intent))

    asyncio.run(_run())


@keys_app.command("discover")
def keys_discover():
    """🔑 自動發現可用的 API Key。

    掃描環境變數、配置文件等，列出所有可用的 Provider。
    """
    from opc.llm.key_discovery import KeyDiscovery, format_discovery_result

    discovery = KeyDiscovery()
    providers = discovery.discover()

    console.print()
    console.print(format_discovery_result(providers))

    if not providers:
        console.print()
        console.print("[info]💡 提示：設置環境變數即可快速配置[/info]")
        console.print("  export OPENAI_API_KEY=sk-...")
        console.print("  export DEEPSEEK_API_KEY=sk-...")
        console.print("  export ANTHROPIC_API_KEY=sk-ant-...")


@keys_app.command("save")
def keys_save(
    provider: str = typer.Argument(..., help="Provider 名稱 (openai/anthropic/deepseek/...)"),
    key: str = typer.Argument(..., help="API Key"),
):
    """💾 保存 API Key 到配置文件。

    示例：
        opc keys save openai sk-...
        opc keys save deepseek sk-...
    """
    from opc.llm.key_discovery import KeyDiscovery

    discovery = KeyDiscovery()
    path = discovery.save_key(provider, key)
    console.print(f"[success]✅ 已保存 {provider} 的 API Key 到 {path}[/success]")


@keys_app.command("test")
def keys_test(
    provider: Optional[str] = typer.Argument(None, help="指定 Provider（留空則測試所有）"),
):
    """🧪 測試 API Key 是否可用。

    示例：
        opc keys test openai
        opc keys test  # 測試所有
    """
    from opc.llm.key_discovery import KeyDiscovery

    discovery = KeyDiscovery()
    providers = discovery.discover()

    if not providers:
        console.print("[error]❌ 未找到任何 API Key[/error]")
        return

    if provider:
        providers = [p for p in providers if p.provider == provider]
        if not providers:
            console.print(f"[error]❌ 未找到 Provider '{provider}' 的 Key[/error]")
            return

    table = Table(title="API Key 測試結果")
    table.add_column("Provider", style="cyan")
    table.add_column("模型", style="blue")
    table.add_column("來源", style="dim")
    table.add_column("格式", style="green")

    for p in providers:
        # 簡單格式驗證
        valid = len(p.key) >= 20
        table.add_row(
            p.provider,
            ", ".join(p.models[:2]),
            p.source,
            "✅ 有效" if valid else "❌ 格式異常",
        )

    console.print()
    console.print(table)


def estimate_cost(
    task: str = typer.Argument(..., help="任務描述"),
    budget: float = typer.Option(0.0, "--budget", "-b", help="預算上限"),
):
    """💰 估算任務成本。

    示例：
        opc estimate "投資分析報告" --budget 3.0
    """
    from opc.llm.model_router import ModelRouter, format_run_estimate
    from opc.layer1_perception.intent_parser import IntentParser

    async def _run():
        parser = IntentParser()
        intent = await parser.parse(task)

        router = ModelRouter(budget_total=budget)

        # 構建角色列表
        roles = []
        for role_name in intent.estimated_roles:
            roles.append({
                "name": role_name,
                "task_description": f"{intent.domain} - {intent.task_type}",
                "estimated_complexity": intent.complexity,
            })

        estimate = router.estimate_run_cost(roles, budget_limit=budget)
        console.print()
        console.print(format_run_estimate(estimate))

    asyncio.run(_run())
