import type { AgentInfo, EmployeeAssignment, SocketStatus } from '../types/visual'
import type { AgentAnimStatus, KanbanPhase, KanbanTask, RoleAggregatedStatus, RoleWorkItemSummary, Session, TaskPreferredAgent } from '../types/kanban'
import type { AppExecMode } from '../types/app'
import { normalizeSessionCompanyProfile, normalizeSessionExecMode } from './sessionIdentity'
import { getExecutionTurnId } from './workItemRuntimeIds'
import { t } from './locale'

export function readOutdoorOverrideUi(): 'auto' | 'day' | 'night' {
  try {
    const o = localStorage.getItem('opc_outdoor_override')
    if (o === 'day' || o === 'night') return o
    if (localStorage.getItem('opc_outdoor_day') === '1') return 'day'
    if (localStorage.getItem('opc_outdoor_night') === '1') return 'night'
  } catch { /* private mode */ }
  return 'auto'
}

export function defaultWsUrl(): string {
  const wsProto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${wsProto}://${window.location.hostname}:${window.location.port || '8765'}/ws`
}

export function statusClass(status: SocketStatus): string {
  if (status === 'connected') return 'ok'
  if (status === 'connecting') return 'warn'
  if (status === 'error') return 'error'
  return 'off'
}

export function normalizeCompanyProfile(value?: string): 'corporate' | 'custom' {
  return normalizeSessionCompanyProfile(value)
}

export function normalizeExecMode(value?: string): AppExecMode {
  return normalizeSessionExecMode(value)
}

export function companyProfileForExecMode(mode: AppExecMode, profile?: string): 'corporate' | 'custom' | undefined {
  if (mode === 'task') return undefined
  if (mode === 'org') return 'custom'
  return 'corporate'
}

export function orgIdForExecMode(mode: AppExecMode, orgId?: string | null): string | undefined {
  if (mode !== 'org') return undefined
  const normalized = String(orgId ?? '').trim()
  return normalized || undefined
}

export function normalizeTaskPreferredAgent(value?: string): TaskPreferredAgent {
  const normalized = String(value ?? '').trim().toLowerCase().replace('-', '_')
  if (normalized === 'codex' || normalized === 'claude_code' || normalized === 'cursor' || normalized === 'opencode' || normalized === 'qwen_code') {
    return normalized
  }
  return 'native'
}

export function truncateJson(data: unknown, maxLen = 120): string {
  const s = JSON.stringify(data) ?? ''
  if (s.length <= maxLen) return s
  return s.slice(0, maxLen) + '\u2026'
}

export function mapAgentPayload(raw: Record<string, unknown>, previous?: AgentInfo): AgentInfo {
  const runtimeStatus = (
    typeof raw.runtime_status === 'string'
      ? raw.runtime_status
      : typeof raw.status === 'string'
        ? raw.status
        : previous?.runtime_status ?? previous?.status ?? 'idle'
  ) as AgentAnimStatus | string
  const appearance = raw.appearance && typeof raw.appearance === 'object'
    ? raw.appearance as AgentInfo['appearance']
    : (previous?.appearance ?? { palette: 0, hue_shift: 0, seat_zone: 'work_area' })
  const specialties = Array.isArray(raw.specialties)
    ? raw.specialties.filter((item): item is string => typeof item === 'string')
    : (previous?.specialties ?? [])
  const agentId = typeof raw.agent_id === 'string' ? raw.agent_id : (previous?.agent_id ?? '')

  return {
    agent_id: agentId,
    name: typeof raw.name === 'string' && raw.name
      ? raw.name
      : typeof raw.role_name === 'string' && raw.role_name
        ? raw.role_name
        : (previous?.name ?? agentId),
    description: typeof raw.description === 'string' ? raw.description : (previous?.description ?? ''),
    specialties,
    status: runtimeStatus,
    office_id: typeof raw.office_id === 'string' ? raw.office_id : previous?.office_id,
    appearance,
    employee_id: typeof raw.employee_id === 'string' ? raw.employee_id : previous?.employee_id,
    opc_role_id: typeof raw.opc_role_id === 'string' ? raw.opc_role_id : previous?.opc_role_id,
    runtime_status: runtimeStatus as AgentAnimStatus,
    current_tool: typeof raw.current_tool === 'string'
      ? raw.current_tool
      : raw.current_tool == null
        ? undefined
        : previous?.current_tool,
    current_task_id: typeof raw.current_task_id === 'string'
      ? raw.current_task_id
      : raw.current_task_id == null
        ? undefined
        : previous?.current_task_id,
  }
}

export function mapAgentListPayload(rawAgents: unknown[], previous: AgentInfo[] = []): AgentInfo[] {
  const prevById = new Map(previous.map((agent) => [agent.agent_id, agent]))
  return rawAgents
    .filter((raw): raw is Record<string, unknown> => !!raw && typeof raw === 'object')
    .map((raw) => {
      const agentId = typeof raw.agent_id === 'string' ? raw.agent_id : ''
      return mapAgentPayload(raw, prevById.get(agentId))
    })
    .filter((agent) => !!agent.agent_id)
}

export function mapEmployeeAssignmentPayload(raw: unknown): EmployeeAssignment | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const value = raw as Record<string, unknown>
  return {
    name: typeof value.name === 'string' ? value.name : undefined,
    employeeId: typeof value.employee_id === 'string'
      ? value.employee_id
      : typeof value.employeeId === 'string'
        ? value.employeeId
        : undefined,
    category: typeof value.category === 'string' ? value.category : undefined,
    experienceScore: typeof value.experience_score === 'number'
      ? value.experience_score
      : typeof value.experienceScore === 'number'
        ? value.experienceScore
        : undefined,
  }
}

export function hasOwnPayloadField(raw: Record<string, unknown>, field: string): boolean {
  return Object.prototype.hasOwnProperty.call(raw, field)
}

export function runtimeStatusClearsDisplayTool(status: unknown): boolean {
  const normalized = String(status ?? '').trim().toLowerCase()
  return normalized === 'idle'
    || normalized === 'done'
    || normalized === 'failed'
    || normalized === 'cancelled'
}

export function workItemIdentityPatchFromPayload(raw: Record<string, unknown>): Partial<KanbanTask> {
  const patch: Partial<KanbanTask> = {}
  const executionMode = typeof raw.execution_mode === 'string' ? raw.execution_mode : ''
  const isTaskModeRuntime = executionMode === 'task_mode' || raw.work_item_projection_id === 'task_mode_execution'
  if (isTaskModeRuntime) {
    const employeeAssignment = mapEmployeeAssignmentPayload(raw.employee_assignment ?? raw.employeeAssignment)
    if (employeeAssignment) patch.employeeAssignment = employeeAssignment
    if (typeof raw.selected_execution_agent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selected_execution_agent)
    else if (typeof raw.selectedExecutionAgent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selectedExecutionAgent)
    return patch
  }
  if (typeof raw.work_item_projection_id === 'string') {
    patch.workItemProjectionId = raw.work_item_projection_id
  } else if (typeof raw.workItemProjectionId === 'string') {
    patch.workItemProjectionId = raw.workItemProjectionId
  }
  if (typeof raw.work_item_turn_type === 'string') patch.workItemTurnType = raw.work_item_turn_type
  else if (typeof raw.workItemTurnType === 'string') patch.workItemTurnType = raw.workItemTurnType

  if (typeof raw.work_item_role_id === 'string') patch.workItemRoleId = raw.work_item_role_id
  else if (typeof raw.workItemRoleId === 'string') patch.workItemRoleId = raw.workItemRoleId

  if (typeof raw.work_item_role_name === 'string') patch.workItemRoleName = raw.work_item_role_name
  else if (typeof raw.workItemRoleName === 'string') patch.workItemRoleName = raw.workItemRoleName

  const employeeAssignment = mapEmployeeAssignmentPayload(raw.employee_assignment ?? raw.employeeAssignment)
  if (employeeAssignment) patch.employeeAssignment = employeeAssignment
  if (typeof raw.selected_execution_agent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selected_execution_agent)
  else if (typeof raw.selectedExecutionAgent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selectedExecutionAgent)
  return patch
}

export function sessionRuntimePatchFromPayload(raw: Record<string, unknown>): Partial<import('../types/kanban').Session> {
  const patch: Partial<import('../types/kanban').Session> = {}
  if (raw.latest_notification && typeof raw.latest_notification === 'object') {
    patch.latestNotification = raw.latest_notification as import('../types/kanban').WorkerNotification
  }
  if (typeof raw.runtime_session_id === 'string') patch.runtimeSessionId = raw.runtime_session_id
  if (typeof raw.resume_cursor === 'number') patch.resumeCursor = raw.resume_cursor
  if (Array.isArray(raw.active_subagents)) patch.activeSubagents = raw.active_subagents as Array<Record<string, unknown>>
  if (Array.isArray(raw.permission_requests)) patch.permissionRequests = raw.permission_requests as Array<Record<string, unknown>>
  if (typeof raw.worktree_path === 'string') patch.worktreePath = raw.worktree_path
  if (typeof raw.current_tool === 'string') patch.currentTool = raw.current_tool
  // displayTool is the sticky "last command" label; an empty string between
  // tools must NOT clear it (that causes the header tool-pill to flicker once
  // per tool call). Only write a real, non-empty label here.
  if (typeof raw.display_tool === 'string' && raw.display_tool.trim()) patch.displayTool = raw.display_tool
  if (typeof raw.tool_elapsed_ms === 'number') patch.toolElapsedMs = raw.tool_elapsed_ms
  if (typeof raw.last_tool_summary === 'string') patch.lastToolSummary = raw.last_tool_summary
  if (typeof raw.context_tokens === 'number') patch.contextTokens = raw.context_tokens
  // Ignore a non-positive window: an intra-turn 0 would wipe the last known
  // window and hide the context ring until the next tool call (flicker).
  if (typeof raw.context_window === 'number' && raw.context_window > 0) patch.contextWindow = raw.context_window
  if (typeof raw.context_remaining_pct === 'number') patch.contextRemainingPct = raw.context_remaining_pct
  if (typeof raw.input_tokens === 'number') patch.inputTokens = raw.input_tokens
  else if (typeof raw.input_tokens_total === 'number') patch.inputTokens = raw.input_tokens_total
  else if (typeof raw.tokens_in === 'number') patch.inputTokens = raw.tokens_in
  if (typeof raw.output_tokens === 'number') patch.outputTokens = raw.output_tokens
  else if (typeof raw.output_tokens_total === 'number') patch.outputTokens = raw.output_tokens_total
  else if (typeof raw.tokens_out === 'number') patch.outputTokens = raw.tokens_out
  if (typeof raw.total_tokens === 'number') patch.totalTokens = raw.total_tokens
  else if (typeof raw.tokens_total === 'number') patch.totalTokens = raw.total_tokens
  if (typeof raw.turn_cost_usd === 'number') patch.turnCostUsd = raw.turn_cost_usd
  if (typeof raw.session_cost_usd === 'number') patch.sessionCostUsd = raw.session_cost_usd
  if (typeof raw.pending_permission_count === 'number') patch.pendingPermissionCount = raw.pending_permission_count
  if (typeof raw.drain_mode === 'string') patch.drainMode = raw.drain_mode
  if (typeof raw.resident_status === 'string') patch.residentStatus = raw.resident_status
  if (typeof raw.actionable_inbox_count === 'number') patch.actionableInboxCount = raw.actionable_inbox_count
  if (typeof raw.protocol_backlog_count === 'number') patch.protocolBacklogCount = raw.protocol_backlog_count
  if (typeof raw.notification_backlog_count === 'number') patch.notificationBacklogCount = raw.notification_backlog_count
  return patch
}

export function kanbanRuntimePatchFromPayload(raw: Record<string, unknown>): Partial<KanbanTask> {
  const patch: Partial<KanbanTask> = {}
  if (raw.latest_notification && typeof raw.latest_notification === 'object') {
    patch.latestNotification = raw.latest_notification as import('../types/kanban').WorkerNotification
  }
  if (typeof raw.current_tool === 'string') patch.currentTool = raw.current_tool
  // Sticky display label — see sessionRuntimePatchFromPayload above.
  if (typeof raw.display_tool === 'string' && raw.display_tool.trim()) patch.displayTool = raw.display_tool
  if (typeof raw.tool_elapsed_ms === 'number') patch.toolElapsedMs = raw.tool_elapsed_ms
  if (typeof raw.last_tool_summary === 'string') patch.lastToolSummary = raw.last_tool_summary
  if (typeof raw.context_tokens === 'number') patch.contextTokens = raw.context_tokens
  if (typeof raw.context_window === 'number' && raw.context_window > 0) patch.contextWindow = raw.context_window
  if (typeof raw.context_remaining_pct === 'number') patch.contextRemainingPct = raw.context_remaining_pct
  if (typeof raw.input_tokens === 'number') patch.inputTokens = raw.input_tokens
  else if (typeof raw.input_tokens_total === 'number') patch.inputTokens = raw.input_tokens_total
  else if (typeof raw.tokens_in === 'number') patch.inputTokens = raw.tokens_in
  if (typeof raw.output_tokens === 'number') patch.outputTokens = raw.output_tokens
  else if (typeof raw.output_tokens_total === 'number') patch.outputTokens = raw.output_tokens_total
  else if (typeof raw.tokens_out === 'number') patch.outputTokens = raw.tokens_out
  if (typeof raw.total_tokens === 'number') patch.totalTokens = raw.total_tokens
  else if (typeof raw.tokens_total === 'number') patch.totalTokens = raw.total_tokens
  if (typeof raw.turn_cost_usd === 'number') patch.turnCostUsd = raw.turn_cost_usd
  if (typeof raw.session_cost_usd === 'number') patch.sessionCostUsd = raw.session_cost_usd
  if (typeof raw.pending_permission_count === 'number') patch.pendingPermissionCount = raw.pending_permission_count
  if (typeof raw.drain_mode === 'string') patch.drainMode = raw.drain_mode
  if (typeof raw.resident_status === 'string') patch.residentStatus = raw.resident_status
  if (typeof raw.actionable_inbox_count === 'number') patch.actionableInboxCount = raw.actionable_inbox_count
  if (typeof raw.protocol_backlog_count === 'number') patch.protocolBacklogCount = raw.protocol_backlog_count
  if (typeof raw.notification_backlog_count === 'number') patch.notificationBacklogCount = raw.notification_backlog_count
  return patch
}

// ── Legacy session helpers ──────────────────────────────────────────

export function shouldRefreshLiveSession(taskId: string, sessionStore: import('../stores/SessionStore').SessionStoreState | null): boolean {
  if (!sessionStore || !taskId) return false
  if (sessionStore.activeSessionId === taskId) return true
  const active = sessionStore.activeSession
  if (!active) return false
  if (active.taskId === taskId || active.parentSessionId === taskId || active.sessionId === taskId) {
    return true
  }

  const target = sessionStore.sessions.find((session) => session.taskId === taskId)
  if (!target) return false

  const activeKeys = new Set(
    [String(active.taskId ?? '').trim(), String(active.sessionId ?? '').trim()].filter(Boolean),
  )
  const targetParent = String(target.parentSessionId ?? '').trim()
  if (targetParent && activeKeys.has(targetParent)) {
    return true
  }

  const activeParent = String(active.parentSessionId ?? '').trim()
  if (!activeParent) return false
  return String(target.taskId ?? '').trim() === activeParent || String(target.sessionId ?? '').trim() === activeParent
}

export function legacyPhaseFromSessionStatus(status: string): KanbanPhase {
  if (status === 'done' || status === 'delivered') return 'approved'
  if (status === 'failed') return 'failed'
  if (status === 'cancelled') return 'cancelled'
  if (status === 'pending') return 'queued'
  if (status === 'awaiting_manager_review' || status === 'awaiting_review') {
    return 'awaiting_manager_review'
  }
  if (status === 'awaiting_human') return 'awaiting_human'
  return 'running'
}

export function legacyColumnForPhase(phase: KanbanPhase): string {
  if (phase === 'approved' || phase === 'failed' || phase === 'cancelled') return 'done'
  if (phase === 'awaiting_manager_review' || phase === 'awaiting_human') return 'in-review'
  if (phase === 'queued' || phase === 'ready' || phase === 'ready_for_rework' || phase === 'waiting_dependencies') return 'todo'
  return 'in-progress'
}

export function legacyAggregatedStatus(status: string): RoleAggregatedStatus {
  if (status === 'done' || status === 'delivered') return 'done'
  if (status === 'failed' || status === 'cancelled') return 'failed'
  if (status === 'pending') return 'pending'
  if (status === 'awaiting_manager_review' || status === 'awaiting_review' || status === 'awaiting_human') return 'waiting'
  return 'active'
}

export function legacyRuntimeStatus(status: string | undefined): AgentAnimStatus {
  if (status === 'reflecting' || status === 'tool_active' || status === 'idle') return status
  return 'idle'
}

export function roleSummaryFromLegacySession(session: Session): RoleWorkItemSummary {
  const executionTurnId = getExecutionTurnId(session) || session.taskId
  const roleId = session.workItemRoleId || session.assigneeIds[0] || session.taskId
  const roleName = session.workItemRoleName || roleId.replace(/[_-]/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
  const phase = legacyPhaseFromSessionStatus(session.status)
  return {
    roleKey: roleId,
    roleId,
    roleName,
    runtimeStatus: legacyRuntimeStatus(session.agentStatus),
    aggregatedStatus: legacyAggregatedStatus(session.status),
    workItems: [
      {
        workItemId: session.workItemProjectionId || executionTurnId,
        workItemProjectionId: session.workItemProjectionId,
        phase,
        kanbanColumn: legacyColumnForPhase(phase),
        title: session.title || roleName,
        kind: session.workItemTurnType,
        executorRoleId: roleId,
        executorRoleName: roleName,
        createdAt: session.createdAt,
        updatedAt: session.updatedAt,
        executionTurnId,
        progressLog: session.progressLog,
        activitySections: session.progressLog.length > 0
          ? [{
              kind: 'activity',
              title: t('app.runtimeActivity'),
              roleName,
              runtimeTaskId: executionTurnId,
              entries: session.progressLog,
            }]
          : [],
      },
    ],
  }
}
