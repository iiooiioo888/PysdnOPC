"""引擎增強 CLI 命令 — 管理智能增強功能。

職責說明：
    提供 `opc enhance` 命令組：
    - status: 查看增強狀態
    - dashboard: 啟動實時儀表盤
    - templates: 管理組織模板
    - insights: 查看洞察報告
"""

from __future__ import annotations

import asyncio
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from opc.core.i18n import t

console = Console()

enhance_app = typer.Typer(
    name="enhance",
    help="🧠 引擎增強管理 — 智能模型路由、預算控制、洞察分析",
    no_args_is_help=True,
)
template_app = typer.Typer(
    name="template",
    help="🏗️ 組織模板管理",
    no_args_is_help=True,
)


@enhance_app.command("status")
def enhance_status():
    """📊 查看引擎增強狀態。"""
    from opc.engine.enhancer import EngineEnhancer

    console.print()
    console.print("[info]引擎增強狀態[/info]")
    console.print("  📡 EnhancedEventBus: 待初始化")
    console.print("  🧠 ModelRouter: 待初始化")
    console.print("  🛡️ BudgetGuard: 待初始化")
    console.print("  📊 InsightEngine: 待初始化")
    console.print()
    console.print("[dim]提示：使用 `opc smart start` 啟動時自動啟用增強功能[/dim]")


@enhance_app.command("dashboard")
def enhance_dashboard(
    port: int = typer.Option(8766, "--port", "-p", help="WebSocket 端口"),
    host: str = typer.Option("0.0.0.0", "--host", help="綁定地址"),
):
    """📺 啟動實時儀表盤 WebSocket 服務。

    示例：
        opc enhance dashboard
        opc enhance dashboard --port 9000
    """
    from opc.plugins.office_ui.ws_monitor import WebSocketMonitor

    console.print(f"[info]📺 啟動實時儀表盤...[/info]")
    console.print(f"   WebSocket: ws://{host}:{port}")
    console.print(f"   [dim]按 Ctrl+C 停止[/dim]")

    async def _run():
        monitor = WebSocketMonitor(None)  # 需要 engine enhancer
        try:
            await monitor.start(host=host, port=port)
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            await monitor.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[info]已停止[/info]")


@template_app.command("list")
def template_list():
    """📋 列出所有可用的組織模板。"""
    from opc.org_template_loader import OrgTemplateLoader, format_template_list

    loader = OrgTemplateLoader()
    templates = loader.list_templates()
    console.print()
    console.print(format_template_list(templates))


@template_app.command("show")
def template_show(
    template_id: str = typer.Argument(..., help="模板 ID（如 finance/research_report）"),
):
    """🔍 查看模板詳情。"""
    from opc.org_template_loader import OrgTemplateLoader

    loader = OrgTemplateLoader()
    info = loader.get_template_info(template_id)

    if not info:
        console.print(f"[error]❌ 模板未找到: {template_id}[/error]")
        return

    console.print()
    console.print(Panel(
        f"[bold]{info['name']}[/bold]\n{info['description']}",
        title=f"🏗️ 模板: {template_id}",
    ))

    table = Table(title="角色配置")
    table.add_column("ID", style="cyan")
    table.add_column("名稱", style="blue")
    table.add_column("描述")
    table.add_column("模型等級", style="green")

    for role in info["roles"]:
        table.add_row(
            role["id"],
            role["name"],
            role["description"][:50],
            role["model_tier"],
        )

    console.print()
    console.print(table)


@template_app.command("apply")
def template_apply(
    template_id: str = typer.Argument(..., help="模板 ID"),
    org_id: Optional[str] = typer.Option(None, "--org", help="組織 ID（留空則自動生成）"),
):
    """🏗️ 應用組織模板到引擎。

    示例：
        opc template apply finance/research_report
        opc template apply dev/fullstack_app --org my-dev-team
    """
    from opc.org_template_loader import OrgTemplateLoader

    loader = OrgTemplateLoader()
    template = loader.load_template(template_id)

    if not template:
        console.print(f"[error]❌ 模板未找到: {template_id}[/error]")
        return

    # 應用模板
    success = loader.apply_to_engine(None, template_id, organization_id=org_id)

    if success:
        console.print(f"[success]✅ 模板 '{template_id}' 已應用[/success]")
    else:
        console.print(f"[error]❌ 模板應用失敗[/error]")


@template_app.command("search")
def template_search(
    query: str = typer.Argument(..., help="搜索關鍵詞"),
):
    """🔍 搜索組織模板。

    示例：
        opc template search 投資
        opc template search finance
    """
    from opc.org_template_loader import OrgTemplateLoader, format_template_list

    loader = OrgTemplateLoader()
    results = loader.search_templates(query)
    console.print()
    if results:
        console.print(format_template_list(results))
    else:
        console.print(f"[info]未找到匹配 '{query}' 的模板[/info]")


# 註冊子命令組
enhance_app.add_typer(template_app, name="template")
