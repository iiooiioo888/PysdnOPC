"""原生和外部代理共享的公司/運行時契約建構器。"""

from __future__ import annotations

from typing import Any, Literal

from opc.core.company_tools import resolve_company_turn_mode
from opc.core.models import Task
from opc.layer2_organization.prompt_contract import render_target_prompt_contract
from opc.layer2_organization.work_item_identity import turn_type_for_task
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer4_tools.output_budget import clip_text

ContractAudience = Literal["native", "external"]

_COMPANY_WORK_ITEM_GUIDELINES = """
## 公司工作項目契約
- 你正在公司模式中執行一個投影工作項目，而非獨自完成整個專案。
- 保持在當前工作項目邊界內，在重新解決先前工作之前使用上游交接、註釋和收件匣上下文。
- 優先使用非同步協作工具進行跨角色釐清。僅對真正的跨角色決策或衝突使用會議。
- 保持下游交接簡潔：在重要時保留決策、風險、開放問題、產出物和驗證狀態。
- 團隊協作必須透過協作工具和收件匣上下文進行。純助手文字不算成員間協調。
- 外部執行者（如 Codex / OpenCode / Claude Code）是你可以呼叫的臨時工作者；他們不是組織成員，也不取代你的角色所有權。
- 尊重任務元數據中的工作項目所有權和產出物契約。如果你無法滿足它們，明確提出而非默默擴大範圍。

## 主動協作要求
- 在完成工作項目之前，檢查注入的收件匣上下文中來自同儕、管理者或下游消費者的訊息；回覆待處理的問題或用 `inbox(action="ack")` 確認已處理的訊息。
- 如果你透過管理者看板或任務裁決處理了審批/審查請求，確認對應的收件匣訊息，除非 `reply_message` 已確認。
- 如果你的工作依賴或重疊於並行同儕的輸出，在最終確定之前使用 `send_dm` 或 `ask_peer_and_wait` 確認對齊。
- 如果你發現影響其他角色的差距、衝突或依賴，立即使用 `send_dm` 或 `broadcast_issue` — 不要默默交付不完整的工作。
- 如果你因同儕未完成而無法完成交付物，向他們發送訊息並繼續你能做的部分。使用 `ask_peer_and_wait` 搭配 `on_timeout="continue"`，這樣如果在逾時窗口內沒有回覆，你會自動恢復。
- 未嘗試協調就交付不完整的工作是策略違規。
- 沒有逾時回退就永遠等待也是策略違規 — 總是設定合理的 `on_timeout`（優先 `continue` 或 `manager`）。
"""

_MULTI_TEAM_ORG_GUIDELINES = """
## 組織運行時契約
- 你是公司模式中的一個席位；在你被分配的工作項目內工作。
- 運行時已為本輪準備了你的分配、信箱快照、看板上下文和工作區根目錄。
- 工作項目是協作的事實來源。使用工作項目 ID 進行協作；絕不使用運行時 Task ID 作為工作項目 ID。
- `workspace_root` 和 `comms_workspace_root` 已保證。`output_root` 可能為空；如果需要，在 `workspace_root` 下選擇合適的子資料夾並在交接或委派簡報中溝通。
- 看板狀態由運行時根據完成報告和審查裁決推進。不要手動翻轉看板狀態。
- 不需要輪詢工作項目進度：運行時監控狀態變化，並在委派或依賴工作完成後重新激活需要行動的角色。所以當你確認委派成功或審查裁決已推進工作後，如果沒有其他需要你自己的工作，結束當前運行 — 系統會代你監控。
- 僅對協調、問題、阻斷或交接使用信箱工具。
- 跨團隊協作是基於請求的；只有直屬管理者委派可執行工作。
- 如果你是根最終決策者，只有你完成的輪次才是權威的面向業主的結果。
"""

_MANAGER_RUNTIME_CONTRACT = """
## 管理者運行時契約
- 你有直屬下屬或允許的委派表面；優先委派和監控而非本地執行。
- 僅使用 `delegate_work` 為直屬下屬建立子工作項目。
- 當所有者仍然正確但簡報、交付物、驗收標準或依賴已變更時，使用 `modify_work_item` 修改現有子工作項目。
- 使用 `delete_work_item` 取消/隱藏過時或錯誤的子工作項目，使其不再阻擋父看板。
- 僅使用 `manager_board_read` 讀取子看板狀態。對於你當前的看板，省略 `parent_work_item_id` 或使用當前工作項目 ID；絕不使用運行時 Task ID。
- 不要向直屬下屬以外發送可執行命令；透過請求與同儕或其他團隊協調。
"""

_COMPANY_PLAN_WORK_ITEM_GUIDELINES = """
## 公司工作項目輪次：規劃 / 接收 / 派遣
- 先調查，然後將發現轉化為具體的工作項目計畫，包含排序、假設和驗證目標。
- 當程式碼庫切片較廣時，優先使用唯讀 `agent_spawn(profile='explore')` 探索。
- 除非任務明確將執行分配給此工作項目，否則避免實作級編輯。
- 在接收或初始派遣期間，不要對尚未有活躍工作包的角色使用 `ask_peer_and_wait`。先建立或委派工作，然後使用 `send_dm` 進行非阻斷協調。
- 如果你在委派存在之前需要另一個角色的觀點，發送非阻斷訊息或將問題包含在委派簡報中；不要停滯整個專案啟動。
"""

_COMPANY_EXECUTE_WORK_ITEM_GUIDELINES = """
## 公司工作項目輪次：執行
- 一旦方法明確，優先對分配的切片直接執行。
- 當能減少上下文雜訊時，使用 `agent_spawn(profile='explore')` 進行唯讀探索。
- 如果工作項目 swarm 工具可用，使用共享微任務看板將分配的切片分解為戰術工作項目，然後再 spawn 突發工作者。
- 交接前，留下審查者可快速驗證的證據：變更區域、產出物指標和任何剩餘風險。
- 將寫入範圍視為受所有權契約限制。除非任務元數據明確擴展，否則不要在該範圍外編輯。
- 你的完成標準高於「在我的輪次中有效」：留下滿足摘要、產出物索引、決策、風險、開放問題和驗證狀態的交接。
"""

_COMPANY_REVIEW_WORK_ITEM_GUIDELINES = """
## 公司工作項目輪次：審查

你正在審查下屬的交付物。運行時機械地應用你的裁決 — 批准將工作送至完成，拒絕將其退回給工作者並附帶你的 summary + blocking_issues 作為返工回饋。運行時不會質疑你裁決的形式或內容；你對此決定負責。

### 如何判斷
- 你有工作區的讀取權限。使用你的工具（file_read、bash、git_*、web_search 等）對照實際當前狀態驗證工作者的聲明。不要盲目信任交接，也不要盲目拒絕。
- 當前工作區證據是事實。先前的審查筆記和舊記憶是需要重新檢查的線索，而非事實。
- 僅對仍然存在的差距拒絕。如果先前的發現已修復，不要重複。
- 用通俗語言將交付物與原始簡報和驗收標準比較：被分配者是否產出了請求的輸出，還是僅提供了分析/規劃？如果簡報要求產出物而工作者只交付了計畫，拒絕。

### 裁決（建議 JSON 格式）
以一個獨立行的 JSON 物件結束你的輪次：

  批准：`{"review_verdict":"approve","summary":"<滿足標準的具體原因>"}`
  拒絕：`{"review_verdict":"reject","summary":"<整體原因>","blocking_issues":["<需要的具體變更>"],"followups":["<非阻斷性改進>"]}`

如果你無法被解析為批准或拒絕，運行時將再 spawn 一次審查嘗試並再次詢問你；之後將上報給人類。所以請輸出明確的標籤。

你可以選擇詳細程度。明確批准時簡短摘要即可。拒絕時，在 `blocking_issues` 中指明具體檔案/測試/產出物，以便工作者行動。
"""

_COMPANY_AGGREGATE_WORK_ITEM_GUIDELINES = """
## 公司工作項目輪次：彙整 / 交付
- 將上游輸出綜合成決策就緒的摘要，而非重複每個中間細節。
- 保留產出物指標、未解決風險和面向業主的下一步行動。
- 保持最終表面積足夠小，使下一個工作項目無需重播整個運行即可行動。
- 彙整不消除問責：在對後續重要時，保留哪個角色產出了哪個產出物或審查結論。
- 可能時，以緊湊的 JSON 物件結尾，如 `{"delivery_package":{"executive_summary":"...","delivered_items":[],"artifact_manifest":[],"risks":[],"open_issues":[],"next_steps":[]}}`，使最終交付保持結構化。
"""


_COMPANY_REPORT_GENERATION_HEADER = """
## 工作項目輪次：報告產出

你剛完成執行被分配的工作。本輪專門為你的審查者撰寫自包含的交接報告。不要在本輪進行任何新的執行工作 — 你的執行已完成。不要委派、不要向同儕發訊息、不要重新執行原始任務。

使用你自己的工作階段上下文加上下方運行時注入的執行輪次摘要/證據。如果你的工作階段記憶不可用但注入的執行輪次摘要、輸出、產出物或驗證證據存在，使用那些注入的事實作為交接來源。不要僅因為這是僅報告輪次就聲稱缺乏上下文。

### 建議報告格式（JSON，非嚴格要求）
以恰好一個獨立行的 JSON 物件結束你的輪次：
  ```
  {
    "summary": "<2-3 句整體結果>",
    "deliverables": [
      {"name": "<交付物名稱>", "path": "<路徑或指標>", "status": "complete" | "partial" | "blocked"}
    ],
    "acceptance_status": [
      {"criterion": "<原始驗收標準>", "met": true | false, "evidence": "<檔案路徑 / 指令 / 證明>"}
    ],
    "risks": ["<已知風險或注意事項>"],
    "next_actions": ["<審查者或下游接下來應做什麼>"]
  }
  ```

如果結構化格式不適合你的情況，改寫清晰的敘述報告 — 運行時會將你的文字原樣傳遞給審查者。不要編造內容來填充 schema；省略欄位或退回敘述。

### 為何這很重要
審查者將收到此報告加上原始簡報，並會用自己的工具（file_read、bash 等）獨立驗證你的聲明。對部分工作和開放問題保持誠實 — 沉默的差距會被審查者發現並計入此交付。
""".strip()


_REVIEW_PENDING_HEADER = """
## 審查要求
- 你的一個或多個直屬下屬已提交完成的工作項目供你審查。你必須在派遣新子項、監控或執行本地工作之前清除審查佇列。
- 對每個待處理項目：將交付物與原始驗收標準和 non_overlap_guard 比較。檢查產出物、完成報告和任何跨團隊協調筆記。
- 同時用通俗語言將結果與原始工作項目簡報比較：被分配者是否產出了請求的輸出，還是僅提供了分析/規劃/搜尋筆記？如果簡報要求實際產出，拒絕僅規劃的提交並要求具體返工。
- 將當前檔案和指令結果視為事實。先前的審查發現僅是線索；在重複之前對照最新工作區驗證。

### 裁決（建議 JSON 格式，每項一個）
  批准：`{"review_verdict":"approve","summary":"<具體原因>"}`
  拒絕：`{"review_verdict":"reject","summary":"<原因>","blocking_issues":["<具體修復>"],"followups":["<非必要改進>"]}`

運行時機械地應用裁決。拒絕時，在 `blocking_issues` 中指明具體檔案/測試/產出物，以便工作者根據你的回饋行動。

- 如果審查依賴你缺乏的資訊（例如來自其他團隊的證據），發送針對性的 `send_dm` 或 `ask_peer_and_wait` 訊息，而非盲目批准。
- 不要僅為解除管線阻擋而批准；如果驗收標準未滿足，以具體、可操作的回饋拒絕。
""".strip()


_REVIEW_EXECUTE_HEADER = """
## 看板審查輪次

本輪是對一個子工作項目的專門審查。不要派遣新工作、不要重寫範圍、不要向同儕發訊息，除非你的審查依賴你無法自行獲取的資訊。

### 你擁有的輸入
- 下方的原始簡報（目標描述）。
- 下方的工作者交接報告。
- 你自己對此項目先前審查的工作階段記憶。
- 透過你的工具（file_read、bash、git_*、web_search 等）對工作區的讀取權限。

### 如何判斷
- 使用你的工具直接對照工作區驗證工作者的聲明。不要盲目信任交接，也不要盲目拒絕。當前工作區證據是事實。
- 將原始簡報視為契約。僅當提交的結果確實滿足請求的產出輸出時才批准；如果工作者在簡報要求產出物或實作時僅交付了計畫或概念備忘錄，拒絕並要求返工。
- 拒絕前，從最新報告重新檢查引用的路徑；不要重用過時的行號或已修復的發現。

### 裁決（建議 JSON 格式）
以一個獨立行的 JSON 物件結束你的輪次：

  批准：`{"review_verdict":"approve","summary":"<滿足標準的具體原因>"}`
  拒絕：`{"review_verdict":"reject","summary":"<整體原因>","blocking_issues":["<需要的具體變更>"],"followups":["<非阻斷性改進>"]}`

運行時機械地應用你的裁決 — 批准將子項移至完成，拒絕將其退回給工作者並附帶你的 summary + blocking_issues 作為返工回饋。運行時不會質疑你裁決的形式或內容。你對此決定負責。

如果你的輸出無法被解析為批准或拒絕，運行時將給你一次帶有解析失敗提示的額外審查嘗試，之後將上報給人類審查者。所以請輸出明確的標籤。
""".strip()


def _format_pending_review_items(items: list[Any]) -> str:
    lines: list[str] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        work_item_id = str(entry.get("work_item_id") or "").strip()
        if not work_item_id:
            continue
        title = str(entry.get("title") or "").strip() or "(untitled)"
        role_id = str(entry.get("role_id") or "").strip() or "unknown"
        deliverable_summary = str(entry.get("deliverable_summary") or "").strip()
        phase = str(entry.get("phase") or "").strip() or "awaiting_manager_review"
        line = f"- work_item_id=`{work_item_id}` role=`{role_id}` title=`{title}` phase=`{phase}`"
        if deliverable_summary:
            preview = clip_text(
                deliverable_summary.replace("\n", " "),
                limit=200,
                marker="deliverable summary preview truncated",
                prefer_newline=False,
            ).text
            line += f" deliverable_preview=`{preview}`"
        review_evidence = dict(entry.get("review_evidence", {}) or {})
        output_paths = [
            str(item).strip()
            for item in list(review_evidence.get("output_paths", []) or [])
            if str(item).strip()
        ]
        verification_status = dict(review_evidence.get("verification_results", {}) or {}).get("status", {})
        verification_label = str(verification_status.get("label", "") or "").strip()
        if verification_label:
            line += f" verification=`{verification_label}`"
        if output_paths:
            line += f" outputs=`{', '.join(output_paths[:3])}`"
        lines.append(line)
    return "\n".join(lines)


def _review_pending_block(task: Task) -> str:
    runtime_model = str(task.metadata.get("runtime_model", "") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    if resolve_company_turn_mode(task) != "review_pending":
        return ""
    pending_items = list(task.metadata.get("pending_review_items", []) or [])
    if not pending_items:
        pending_items = list(task.context_snapshot.get("pending_review_items", []) or [])
    rendered = _format_pending_review_items(pending_items)
    if not rendered:
        return _REVIEW_PENDING_HEADER
    return f"{_REVIEW_PENDING_HEADER}\n\n### Pending Review Queue\n{rendered}"


def _review_execute_block(task: Task) -> str:
    runtime_model = str(task.metadata.get("runtime_model", "") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    turn_mode = resolve_company_turn_mode(task)
    work_item_turn_type = turn_type_for_task(task, fallback="")
    explicit_review_turn = bool(
        task.metadata.get("review_execution_work_item", False)
        or task.metadata.get("review_task", False)
        or str(task.metadata.get("review_target_work_item_id", "") or "").strip()
    )
    if not explicit_review_turn:
        return ""
    if turn_mode != "review_execute" and work_item_turn_type != "review":
        return ""
    target_work_item_id = str(
        task.metadata.get("review_target_work_item_id")
        or linked_work_item_id_for_task(task)
        or ""
    ).strip()
    if not target_work_item_id:
        return _REVIEW_EXECUTE_HEADER
    title = str(task.metadata.get("review_target_title", "") or "").strip()
    worker_role_id = str(task.metadata.get("review_target_worker_role_id", "") or "").strip()
    completion_report = str(task.metadata.get("review_completion_report", "") or "").strip()
    review_evidence = dict(task.metadata.get("review_evidence", {}) or {})
    prompt_contract = dict(task.metadata.get("prompt_contract", {}) or {})
    target_contract = dict(task.metadata.get("review_target_prompt_contract", {}) or prompt_contract.get("target_contract", {}) or {})
    lines = [
        _REVIEW_EXECUTE_HEADER,
        "",
        "### Target Child Work Item",
        f"- work_item_id=`{target_work_item_id}`",
    ]
    if title:
        lines.append(f"- title=`{title}`")
    if worker_role_id:
        lines.append(f"- worker_role=`{worker_role_id}`")
    rendered_target_contract = render_target_prompt_contract(target_contract)
    if rendered_target_contract:
        lines.append("")
        lines.append(rendered_target_contract)
    if completion_report:
        lines.append("")
        lines.append("### Completion Report")
        lines.append(clip_text(
            completion_report,
            limit=2000,
            marker="completion report preview truncated",
        ).text)
    artifact_manifest = [
        dict(item)
        for item in list(review_evidence.get("artifact_manifest", []) or [])
        if isinstance(item, dict)
    ]
    changed_areas = [
        str(item).strip()
        for item in list(review_evidence.get("changed_areas", []) or [])
        if str(item).strip()
    ]
    verification_results = dict(review_evidence.get("verification_results", {}) or {})
    verification_status = dict(verification_results.get("status", {}) or {})
    verification_checks = [
        dict(item)
        for item in list(verification_results.get("checks", []) or [])
        if isinstance(item, dict)
    ]
    key_commands = [
        str(item).strip()
        for item in list(review_evidence.get("key_commands", []) or [])
        if str(item).strip()
    ]
    output_paths = [
        str(item).strip()
        for item in list(review_evidence.get("output_paths", []) or [])
        if str(item).strip()
    ]
    open_risks = [
        str(item).strip()
        for item in list(review_evidence.get("open_risks", []) or [])
        if str(item).strip()
    ]
    if artifact_manifest:
        lines.append("")
        lines.append("### Artifact Manifest")
        for item in artifact_manifest[:10]:
            label = str(item.get("label", "") or item.get("kind", "") or "artifact").strip()
            value = str(item.get("value", "") or "").strip()
            lines.append(f"- {label}: `{value}`" if value else f"- {label}")
    if changed_areas:
        lines.append("")
        lines.append("### Changed Areas")
        lines.extend(f"- `{item}`" for item in changed_areas[:10])
    if verification_status or verification_checks:
        lines.append("")
        lines.append("### Verification")
        if verification_status:
            label = str(verification_status.get("label", "") or "").strip()
            summary = str(verification_status.get("summary", "") or "").strip()
            if label:
                lines.append(f"- status=`{label}`")
            if summary:
                lines.append(f"- summary={clip_text(summary, limit=300, marker='verification summary preview truncated').text}")
        for item in verification_checks[:6]:
            command = str(item.get("command", "") or "").strip()
            status = str(item.get("status", "") or "").strip()
            summary = str(item.get("summary", "") or "").strip()
            rendered = f"- `{command}` -> `{status}`" if command else f"- `{status}`"
            if summary:
                rendered += f" :: {summary[:220]}"
            lines.append(rendered)
    if key_commands:
        lines.append("")
        lines.append("### Key Commands")
        lines.extend(f"- `{item}`" for item in key_commands[:8])
    if output_paths:
        lines.append("")
        lines.append("### Output Paths")
        lines.extend(f"- `{item}`" for item in output_paths[:10])
    if open_risks:
        lines.append("")
        lines.append("### Open Risks")
        lines.extend(f"- {item}" for item in open_risks[:8])
    return "\n".join(lines)


def _render_report_source_evidence(task: Task) -> str:
    evidence = dict(task.metadata.get("report_source_evidence", {}) or {})
    summary = str(task.metadata.get("report_source_summary", "") or "").strip()
    result_content = str(task.metadata.get("report_source_result_content", "") or "").strip()
    lines: list[str] = []

    if summary:
        lines.append("### Last Execute-Turn Summary")
        lines.append(clip_text(summary, limit=2000, marker="execute summary preview truncated").text)
    if result_content and result_content != summary:
        lines.append("")
        lines.append("### Last Execute-Turn Output")
        lines.append(clip_text(result_content, limit=2000, marker="execute output preview truncated").text)

    artifact_manifest = [
        dict(item)
        for item in list(evidence.get("artifact_manifest", []) or [])
        if isinstance(item, dict)
    ]
    changed_areas = [
        str(item).strip()
        for item in list(evidence.get("changed_areas", []) or [])
        if str(item).strip()
    ]
    verification_results = dict(evidence.get("verification_results", {}) or {})
    verification_status = dict(verification_results.get("status", {}) or {})
    verification_checks = [
        dict(item)
        for item in list(verification_results.get("checks", []) or [])
        if isinstance(item, dict)
    ]
    key_commands = [
        str(item).strip()
        for item in list(evidence.get("key_commands", []) or [])
        if str(item).strip()
    ]
    output_paths = [
        str(item).strip()
        for item in list(evidence.get("output_paths", []) or [])
        if str(item).strip()
    ]
    open_risks = [
        str(item).strip()
        for item in list(evidence.get("open_risks", []) or [])
        if str(item).strip()
    ]

    if artifact_manifest:
        lines.append("")
        lines.append("### Known Artifacts From Execute Turn")
        for item in artifact_manifest[:10]:
            label = str(item.get("label", "") or item.get("kind", "") or "artifact").strip()
            value = str(item.get("value", "") or "").strip()
            lines.append(f"- {label}: `{value}`" if value else f"- {label}")
    if changed_areas:
        lines.append("")
        lines.append("### Changed Areas")
        lines.extend(f"- `{item}`" for item in changed_areas[:10])
    if verification_status or verification_checks:
        lines.append("")
        lines.append("### Verification Evidence From Execute Turn")
        if verification_status:
            label = str(verification_status.get("label", "") or "").strip()
            summary_text = str(verification_status.get("summary", "") or "").strip()
            if label:
                lines.append(f"- status=`{label}`")
            if summary_text:
                lines.append(f"- summary={clip_text(summary_text, limit=300, marker='verification summary preview truncated').text}")
        for item in verification_checks[:6]:
            command = str(item.get("command", "") or "").strip()
            status = str(item.get("status", "") or "").strip()
            summary_text = str(item.get("summary", "") or "").strip()
            rendered = f"- `{command}` -> `{status}`" if command else f"- `{status}`"
            if summary_text:
                rendered += f" :: {summary_text[:220]}"
            lines.append(rendered)
    if key_commands:
        lines.append("")
        lines.append("### Key Commands")
        lines.extend(f"- `{item}`" for item in key_commands[:8])
    if output_paths:
        lines.append("")
        lines.append("### Output Paths")
        lines.extend(f"- `{item}`" for item in output_paths[:10])
    if open_risks:
        lines.append("")
        lines.append("### Open Risks")
        lines.extend(f"- {item}" for item in open_risks[:8])

    if not lines:
        return ""
    return "\n".join(lines).strip()


def _report_execute_block(task: Task) -> str:
    """Return the report-generation prompt block when this turn is the
    hidden auxiliary report card spawned after a worker DONE.

    Two-turn worker→review handoff: instead of the worker's last execute
    turn prose being treated as the report, the runtime spawns a separate
    report card that resumes the same worker session and asks for an
    explicit structured handoff. This block is the prompt for that turn.
    """
    runtime_model = str(task.metadata.get("runtime_model", "") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    turn_mode = resolve_company_turn_mode(task)
    work_item_turn_type = turn_type_for_task(task, fallback="")
    is_report_turn = (
        turn_mode == "report_required"
        or work_item_turn_type == "report"
        or bool(task.metadata.get("report_execution_work_item", False))
    )
    if not is_report_turn:
        return ""
    prompt_contract = dict(task.metadata.get("prompt_contract", {}) or {})
    target_contract = dict(task.metadata.get("report_target_prompt_contract", {}) or prompt_contract.get("target_contract", {}) or {})
    rendered_target_contract = render_target_prompt_contract(
        target_contract,
        heading="### Work Item Contract To Report Against",
    )
    source_evidence = _render_report_source_evidence(task)
    parts = [_COMPANY_REPORT_GENERATION_HEADER]
    if rendered_target_contract:
        parts.append(rendered_target_contract)
    if source_evidence:
        parts.append(source_evidence)
    return "\n\n".join(part for part in parts if part)


def _multi_team_manager_capable(task: Task) -> bool:
    """Return whether this seat has a management/delegation surface.

    This deliberately keys off role capability, not the current turn mode:
    a middle manager can be in an execute/integrate/review turn and still
    need manager guidance, while a leaf worker should not receive delegation
    planning rules.
    """
    metadata = dict(task.metadata or {})
    for key in ("direct_report_seat_ids", "allowed_delegate_role_ids", "direct_report_role_ids"):
        if [str(item).strip() for item in list(metadata.get(key, []) or []) if str(item).strip()]:
            return True
    if str(metadata.get("managed_team_id", "") or "").strip():
        return True

    topology = dict(metadata.get("runtime_topology", {}) or {})
    seats = [
        dict(item)
        for item in list(topology.get("seats", []) or [])
        if isinstance(item, dict)
    ]
    current_seat_id = str(metadata.get("delegation_seat_id", "") or metadata.get("seat_id", "") or "").strip()
    current_role_id = str(task.assigned_to or metadata.get("work_item_role_id", "") or "").strip()
    current_seat: dict[str, Any] = {}
    for seat in seats:
        if current_seat_id and str(seat.get("seat_id", "") or "").strip() == current_seat_id:
            current_seat = seat
            break
    if not current_seat and current_role_id:
        for seat in seats:
            if str(seat.get("role_id", "") or "").strip() == current_role_id:
                current_seat = seat
                break
    if not current_seat:
        return False
    for key in ("direct_report_seat_ids", "allowed_delegate_role_ids", "direct_report_role_ids"):
        if [str(item).strip() for item in list(current_seat.get(key, []) or []) if str(item).strip()]:
            return True
    return bool(str(current_seat.get("managed_team_id", "") or "").strip())


def _dispatch_requirement_block(task: Task) -> str:
    runtime_model = str(task.metadata.get("runtime_model", "") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    if resolve_company_turn_mode(task) != "dispatch_required":
        return ""
    if not _multi_team_manager_capable(task):
        return ""
    lines = [
        """
## 派遣規劃契約
- 本輪目前處於 `dispatch_required`。
- 範圍優先：保留上游目標、請求的交付物形式、要求的路徑、限制和硬性依賴。
- 委派基於結果的子工作項目；規劃或清單工作不得取代請求的產出工作。
- 將硬性阻斷因素與可開始的準備工作分開，僅當階段有不同的輸出、阻斷因素或交接點時才拆分階段。
- 如果在此環境中無法進行產出，派遣或上報阻斷因素而非替代為計畫。
- 之後，恰好執行以下之一：
  1. 使用 `delegate_work` 為至少一個直屬下屬建立下游子工作。
  2. 如果確實需要本地執行（因為沒有下游席位適合），在你的最終回覆中恰好包含一行：
     `NO_DELEGATION_JUSTIFICATION: <具體原因>`
- 如果你在沒有委派或理由行的情況下本地執行，運行時將拒絕本輪並要求你先派遣。
- 如果這是對現有看板的後續，用 `manager_board_read` 檢查它，並在建立額外工作之前使用 `modify_work_item` / `delete_work_item` 處理錯誤的現有項目。
""".strip()
    ]
    metadata = dict(task.metadata or {})
    snapshot = dict(task.context_snapshot or {})
    followup_text = str(snapshot.get("user_supplied_input", "") or "").strip()
    is_final_decider_followup = bool(metadata.get("followup_routed_to_final_decider", False)) or bool(followup_text)
    if is_final_decider_followup:
        if followup_text:
            followup_preview = clip_text(followup_text, limit=800, marker="follow-up truncated").text
            lines.append(
                "\n".join(
                    [
                        "## 使用者後續看板對帳",
                        f"使用者後續：{followup_preview}",
                    ]
                )
            )
        else:
            lines.append("## 使用者後續看板對帳")
        lines.append(
            "\n".join(
                [
                    "- 你正在以新的業主指令恢復此相同角色工作階段；依賴你已可用的工作階段歷史。",
                    "- 根據當前狀態和可用的協作工具決定下一步：直接回答、適當時關閉審查、檢查或修改看板、委派更多工作，或採取其他支援的運行時操作。",
                    "- 變更現有看板時，用 `manager_board_read` 檢查它，並在新增更多工作之前優先使用 `modify_work_item` / `delete_work_item` 處理過時或錯誤的子工作。",
                    "- 如果你建立替代子工作，也解決過時的兄弟項目，使舊工作不會繼續運行或阻擋完成。",
                    "- 保持你的最終回覆聚焦於你決定或變更了什麼。",
                ]
            )
        )
    mutation_action = str(metadata.get("manager_mutation_action", "") or "").strip()
    mutation_reason = str(metadata.get("manager_mutation_reason", "") or "").strip()
    mutation_user_input = str(
        metadata.get("latest_user_directive", "")
        or metadata.get("manager_mutation_user_input", "")
        or ""
    ).strip()
    if mutation_action == "modify" or mutation_user_input:
        mutation_lines = ["## 上游工作項目變更對帳"]
        if mutation_user_input:
            mutation_lines.append(
                f"最新上游使用者指示：{clip_text(mutation_user_input, limit=800, marker='upstream user input truncated').text}"
            )
        if mutation_reason:
            mutation_lines.append(
                f"管理者變更原因：{clip_text(mutation_reason, limit=500, marker='mutation reason truncated').text}"
            )
        mutation_lines.extend(
            [
                "- 你當前的工作項目必須遵循此最新上游指示。在建立替代子工作之前，用 `manager_board_read` 檢查你現有的子看板。",
                "- 對每個現有子項，決定它是否仍支持修訂後的父簡報。對應在新範圍下繼續的子項使用 `modify_work_item`。",
                "- 對仍描述舊方向的過時子項使用 `delete_work_item`，特別是 Stop 之前遺留的暫停/運行中子項。",
                "- 如果你委派替代子項，也解決過時子項，使舊工作不會繼續運行或保留在看板上。",
            ]
        )
        lines.append("\n".join(mutation_lines))
    return "\n\n".join(lines)


_EXTERNAL_TOOL_WORDING_REPLACEMENTS = {
    "當程式碼庫切片較廣時，優先使用唯讀 `agent_spawn(profile='explore')` 探索。": (
        "當工作區切片較廣時，優先使用你的外部代理自身的搜尋、"
        "檢查或上下文隔離能力進行唯讀探索。"
    ),
    "當能減少上下文雜訊時，使用 `agent_spawn(profile='explore')` 進行唯讀探索。": (
        "當能減少上下文雜訊時，使用你的外部代理自身的搜尋、檢查或上下文隔離"
        "能力進行唯讀探索。"
    ),
    "你有工作區的讀取權限。使用你的工具（file_read、bash、git_*、web_search 等）對照實際當前狀態驗證工作者的聲明。不要盲目信任交接，也不要盲目拒絕。": (
        "你有工作區的讀取權限。使用你的外部代理可用的"
        "檢查、搜尋、shell、版本控制、瀏覽器和"
        "驗證能力對照實際當前狀態驗證工作者的聲明。不要盲目信任交接，也不要盲目拒絕。"
    ),
    "審查者將收到此報告加上原始簡報，並會用自己的工具（file_read、bash 等）獨立驗證你的聲明。對部分工作和開放問題保持誠實 — 沉默的差距會被審查者發現並計入此交付。": (
        "審查者將收到此報告加上原始簡報，並會"
        "用自己的工作區檢查和驗證能力獨立驗證你的聲明。對部分工作和開放"
        "問題保持誠實 — 沉默的差距會被審查者發現並計入此交付。"
    ),
    "- 透過你的工具（file_read、bash、git_*、web_search 等）對工作區的讀取權限。": (
        "- 透過你的外部代理可用的檢查、搜尋、shell、版本控制、瀏覽器和驗證能力對工作區的讀取權限。"
    ),
}


def _normalize_contract_audience(audience: str | None) -> ContractAudience:
    return "external" if str(audience or "").strip().lower() == "external" else "native"


def _contract_text_for_audience(text: str, audience: ContractAudience) -> str:
    if audience != "external":
        return text
    rendered = text
    for old, new in _EXTERNAL_TOOL_WORDING_REPLACEMENTS.items():
        rendered = rendered.replace(old, new)
    return rendered


def build_company_work_item_contract(
    task: Task,
    *,
    audience: str | None = "native",
) -> str:
    """Return the shared company/runtime contract for a task."""
    resolved_audience = _normalize_contract_audience(audience)
    if str(task.metadata.get("runtime_model", "") or "").strip() == "multi_team_org":
        # Hidden auxiliary cards are single-purpose and take precedence over
        # the generic multi-team guidelines: the seat is here for exactly
        # one job (write the report, or apply the verdict) and nothing else.
        report_execute_block = _report_execute_block(task)
        if report_execute_block:
            return _contract_text_for_audience(report_execute_block, resolved_audience)
        review_execute_block = _review_execute_block(task)
        if review_execute_block:
            return _contract_text_for_audience(review_execute_block, resolved_audience)
        parts = [_MULTI_TEAM_ORG_GUIDELINES.strip()]
        if _multi_team_manager_capable(task):
            parts.append(_MANAGER_RUNTIME_CONTRACT.strip())
        review_block = _review_pending_block(task)
        if review_block:
            parts.append(review_block)
        dispatch_block = _dispatch_requirement_block(task)
        if dispatch_block:
            parts.append(dispatch_block)
        return _contract_text_for_audience("\n\n".join(parts), resolved_audience)

    turn_type = turn_type_for_task(task, fallback="execute")
    work_item_name = str(task.title or "").strip()
    orchestration = str(task.metadata.get("work_item_orchestration_profile", "") or "").strip()
    verification_required = bool(task.metadata.get("work_item_verification_required", False))
    header = [
        _COMPANY_WORK_ITEM_GUIDELINES.strip(),
        f"Current work item: `{work_item_name or 'projected work item'}`",
        f"Work item turn type: `{turn_type}`",
    ]
    if orchestration:
        header.append(f"Orchestration profile: `{orchestration}`")
    header.append(
        "Work item verification requirement: "
        + ("required before completion." if verification_required else "not automatically required for this work item.")
    )
    if turn_type in {"intake", "plan", "dispatch"}:
        header.append(_COMPANY_PLAN_WORK_ITEM_GUIDELINES.strip())
    elif turn_type == "review":
        header.append(_COMPANY_REVIEW_WORK_ITEM_GUIDELINES.strip())
    elif turn_type in {"aggregate", "deliver"}:
        header.append(_COMPANY_AGGREGATE_WORK_ITEM_GUIDELINES.strip())
    else:
        header.append(_COMPANY_EXECUTE_WORK_ITEM_GUIDELINES.strip())
    return _contract_text_for_audience("\n\n".join(part for part in header if part), resolved_audience)


def build_external_company_work_item_contract(task: Task) -> str:
    """Return the company/runtime contract with external-agent tool wording."""
    return build_company_work_item_contract(task, audience="external")
